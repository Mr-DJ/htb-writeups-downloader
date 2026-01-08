"""
HackTheBox Writeups Downloader

Automates downloading PDF writeups for retired HackTheBox machines.
"""

import requests
import os
import sys
import json
import time
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class Colors:
    """ANSI color codes for terminal output"""
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

class ProgressTracker:
    """Thread-safe progress tracker"""
    def __init__(self, total: int):
        self.total = total
        self.current = 0
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.lock = threading.Lock()

    def increment(self, status: str = 'success'):
        with self.lock:
            self.current += 1
            if status == 'success':
                self.success += 1
            elif status == 'failed':
                self.failed += 1
            elif status == 'skipped':
                self.skipped += 1

    def get_stats(self) -> Dict:
        with self.lock:
            return {
                'total': self.total,
                'current': self.current,
                'success': self.success,
                'failed': self.failed,
                'skipped': self.skipped,
                'percent': (self.current / self.total * 100) if self.total > 0 else 0
            }

class HTBDatabase:
    """SQLite database for managing HTB machines and writeups"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = None
        self.init_database()

    def connect(self):
        """Create database connection with proper settings"""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def init_database(self):
        """Initialize database schema"""
        conn = self.connect()
        cursor = conn.cursor()

        # Machines table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS machines (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                os TEXT,
                difficulty TEXT,
                difficulty_rating INTEGER,
                points INTEGER,
                rating REAL,
                rating_count INTEGER,
                release_date TEXT,
                retired_date TEXT,
                user_owns_count INTEGER,
                root_owns_count INTEGER,
                free BOOLEAN,
                avatar TEXT,
                first_creator_id INTEGER,
                first_creator_name TEXT,
                synopsis TEXT,
                recommended BOOLEAN,
                price_tier INTEGER,
                is_single_flag BOOLEAN,
                sp_flag INTEGER,
                first_discovered TEXT,
                last_updated TEXT,
                UNIQUE(id)
            )
        """)

        # Writeups table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS writeups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER,
                downloaded_at TEXT,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                last_error TEXT,
                FOREIGN KEY (machine_id) REFERENCES machines(id),
                UNIQUE(machine_id)
            )
        """)

        # Download statistics table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS download_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                total_machines INTEGER,
                downloaded INTEGER,
                failed INTEGER,
                skipped INTEGER,
                duration_seconds REAL
            )
        """)

        # Metadata table for tracking cache freshness
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)

        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_machines_os ON machines(os)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_machines_difficulty ON machines(difficulty)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_machines_rating ON machines(rating)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_writeups_status ON writeups(status)')

        conn.commit()
        conn.close()

    def get_cache_age(self) -> Optional[timedelta]:
        """Get age of machine list cache"""
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute("SELECT value, updated_at FROM metadata WHERE key = 'machines_last_fetched'")
        row = cursor.fetchone()
        conn.close()

        if row:
            last_fetched = datetime.fromisoformat(row['updated_at'])
            return datetime.now() - last_fetched
        return None

    def update_cache_timestamp(self):
        """Update cache timestamp"""
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO metadata (key, value, updated_at)
            VALUES ('machines_last_fetched', 'true', ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, (datetime.now().isoformat(),))

        conn.commit()
        conn.close()

    def get_all_machines_from_db(self) -> List[Dict]:
        """Get all machines from database as dict list"""
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT 
                id, name, os, difficulty as difficultyText, 
                difficulty_rating as difficulty, points, rating, 
                rating_count as ratingCount, release_date as releaseDate,
                retired_date as retiredDate, user_owns_count as userOwnsCount,
                root_owns_count as rootOwnsCount, free, avatar,
                first_creator_id, first_creator_name, recommended,
                price_tier as priceTier, is_single_flag as isSingleFlag,
                sp_flag as spFlag
            FROM machines
            ORDER BY id
        """)

        machines = []
        for row in cursor.fetchall():
            machine = dict(row)
            # Reconstruct creator structure
            if machine.get('first_creator_id'):
                machine['maker'] = {
                    'id': machine['first_creator_id'],
                    'name': machine['first_creator_name']
                }
            machines.append(machine)

        conn.close()
        return machines

    def upsert_machine(self, machine_data: Dict):
        """Insert or update machine information"""
        conn = self.connect()
        cursor = conn.cursor()

        try:
            first_creator = machine_data.get('firstCreator') or machine_data.get('maker')

            data = {
                'id': machine_data.get('id'),
                'name': machine_data.get('name'),
                'os': machine_data.get('os'),
                'difficulty': machine_data.get('difficultyText'),
                'difficulty_rating': machine_data.get('difficulty'),
                'points': machine_data.get('points'),
                'rating': machine_data.get('rating'),
                'rating_count': machine_data.get('ratingCount'),
                'release_date': machine_data.get('releaseDate'),
                'retired_date': machine_data.get('retiredDate'),
                'user_owns_count': machine_data.get('userOwnsCount'),
                'root_owns_count': machine_data.get('rootOwnsCount'),
                'free': machine_data.get('free'),
                'avatar': machine_data.get('avatar'),
                'recommended': machine_data.get('recommended'),
                'price_tier': machine_data.get('priceTier'),
                'is_single_flag': machine_data.get('isSingleFlag'),
                'sp_flag': machine_data.get('spFlag'),
                'first_creator_id': first_creator.get('id') if first_creator else None,
                'first_creator_name': first_creator.get('name') if first_creator else None,
                'synopsis': None,
                'last_updated': datetime.now().isoformat()
            }

            cursor.execute('SELECT first_discovered FROM machines WHERE id = ?', (data['id'],))
            existing = cursor.fetchone()

            if existing:
                data['first_discovered'] = existing['first_discovered']
            else:
                data['first_discovered'] = datetime.now().isoformat()

            cursor.execute("""
                INSERT INTO machines (
                    id, name, os, difficulty, difficulty_rating, points, rating, rating_count,
                    release_date, retired_date, user_owns_count, root_owns_count, free, avatar,
                    first_creator_id, first_creator_name, synopsis, recommended, price_tier,
                    is_single_flag, sp_flag, first_discovered, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    os = excluded.os,
                    difficulty = excluded.difficulty,
                    rating = excluded.rating,
                    rating_count = excluded.rating_count,
                    user_owns_count = excluded.user_owns_count,
                    root_owns_count = excluded.root_owns_count,
                    last_updated = excluded.last_updated
            """, (
                data['id'], data['name'], data['os'], data['difficulty'], 
                data['difficulty_rating'], data['points'], data['rating'], data['rating_count'],
                data['release_date'], data['retired_date'], data['user_owns_count'], 
                data['root_owns_count'], data['free'], data['avatar'],
                data['first_creator_id'], data['first_creator_name'], data['synopsis'], 
                data['recommended'], data['price_tier'],
                data['is_single_flag'], data['sp_flag'], data['first_discovered'], 
                data['last_updated']
            ))

            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"{Colors.RED}✗ DB error: {e}{Colors.RESET}")
        finally:
            conn.close()

    def upsert_writeup(self, machine_id: int, file_path: str, file_size: int, 
                      status: str = 'success', error_msg: str = None):
        """Record writeup download"""
        conn = self.connect()
        cursor = conn.cursor()

        try:
            cursor.execute('SELECT retry_count FROM writeups WHERE machine_id = ?', (machine_id,))
            row = cursor.fetchone()
            retry_count = row['retry_count'] + 1 if row else 0

            cursor.execute("""
                INSERT INTO writeups (machine_id, file_path, file_size, downloaded_at, status, retry_count, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(machine_id) DO UPDATE SET
                    file_path = excluded.file_path,
                    file_size = excluded.file_size,
                    downloaded_at = excluded.downloaded_at,
                    status = excluded.status,
                    retry_count = excluded.retry_count,
                    last_error = excluded.last_error
            """, (machine_id, file_path, file_size, datetime.now().isoformat(), status, retry_count, error_msg))

            conn.commit()
        except Exception as e:
            conn.rollback()
        finally:
            conn.close()

    def get_downloaded_machine_ids(self) -> set:
        """Get set of machine IDs that have been downloaded"""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT machine_id FROM writeups WHERE status = 'success'")
        ids = {row['machine_id'] for row in cursor.fetchall()}
        conn.close()
        return ids

    def get_all_machine_ids(self) -> set:
        """Get all known machine IDs from database"""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM machines")
        ids = {row['id'] for row in cursor.fetchall()}
        conn.close()
        return ids

    def get_pending_downloads(self, machines: List[Dict]) -> List[Dict]:
        """Get machines that need downloading"""
        downloaded_ids = self.get_downloaded_machine_ids()
        return [m for m in machines if m['id'] not in downloaded_ids]

    def record_download_session(self, stats: Dict, duration: float):
        """Record download session statistics"""
        conn = self.connect()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO download_stats (timestamp, total_machines, downloaded, failed, skipped, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                stats['total'],
                stats['success'],
                stats['failed'],
                stats['skipped'],
                duration
            ))
            conn.commit()
        except:
            pass
        finally:
            conn.close()

    def get_statistics(self) -> Dict:
        """Get comprehensive database statistics"""
        conn = self.connect()
        cursor = conn.cursor()

        stats = {}

        try:
            cursor.execute("SELECT COUNT(*) as count FROM machines")
            stats['total_machines'] = cursor.fetchone()['count']

            cursor.execute("SELECT COUNT(*) as count FROM writeups WHERE status = 'success'")
            stats['downloaded'] = cursor.fetchone()['count']

            cursor.execute("SELECT COUNT(*) as count FROM writeups WHERE status = 'failed'")
            stats['failed'] = cursor.fetchone()['count']

            cursor.execute("SELECT os, COUNT(*) as count FROM machines GROUP BY os")
            stats['by_os'] = {row['os']: row['count'] for row in cursor.fetchall()}

            cursor.execute("SELECT difficulty, COUNT(*) as count FROM machines GROUP BY difficulty")
            stats['by_difficulty'] = {row['difficulty']: row['count'] for row in cursor.fetchall()}

            cursor.execute("""
                SELECT name, rating, difficulty, os 
                FROM machines 
                WHERE rating IS NOT NULL 
                ORDER BY rating DESC 
                LIMIT 10
            """)
            stats['top_rated'] = [dict(row) for row in cursor.fetchall()]

            cursor.execute("""
                SELECT timestamp, total_machines, downloaded, failed, duration_seconds
                FROM download_stats 
                ORDER BY timestamp DESC 
                LIMIT 5
            """)
            stats['recent_sessions'] = [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            print(f"{Colors.RED}✗ Error getting statistics: {e}{Colors.RESET}")
        finally:
            conn.close()

        return stats

class HTBWriteupsDownloader:
    """Main downloader class"""

    def __init__(self, api_token: str, output_dir: str = "./htb_writeups", cache_hours: int = 24*365):
        self.api_token = api_token
        self.output_dir = Path(output_dir)
        self.db = HTBDatabase(self.output_dir / 'htb_machines.db')
        self.cache_hours = cache_hours
        self.base_url = "https://labs.hackthebox.com"

        # Configure session with retry logic
        self.session = requests.Session()
        retry_strategy = Retry(
            total=5,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "htb-writeups-downloader/1.0"
        })

        self.rate_limit_delay = 0.5
        self.last_request_time = 0
        self.rate_limit_lock = threading.Lock()
        self.consecutive_failures = 0
        self.max_consecutive_failures = 10

    def _rate_limit(self):
        """Implement adaptive rate limiting"""
        with self.rate_limit_lock:
            elapsed = time.time() - self.last_request_time
            delay = self.rate_limit_delay * (1 + self.consecutive_failures * 0.5)
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self.last_request_time = time.time()

    def _make_request(self, url: str, stream: bool = False, max_retries: int = 3) -> Optional[requests.Response]:
        """Make HTTP request with comprehensive error handling"""
        self._rate_limit()

        for attempt in range(max_retries):
            try:
                response = self.session.get(url, stream=stream, timeout=60)

                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 60))
                    print(f"{Colors.YELLOW}[!] Rate limited. Waiting {retry_after}s...{Colors.RESET}")
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                self.consecutive_failures = 0
                return response

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    return None
                if attempt == max_retries - 1:
                    print(f"{Colors.YELLOW}[!] HTTP error: {e}{Colors.RESET}")

            except requests.exceptions.ConnectionError:
                if attempt == max_retries - 1:
                    print(f"{Colors.YELLOW}[!] Connection error{Colors.RESET}")

            except requests.exceptions.Timeout:
                if attempt == max_retries - 1:
                    print(f"{Colors.YELLOW}[!] Timeout{Colors.RESET}")

            except Exception as e:
                if attempt == max_retries - 1:
                    print(f"{Colors.RED}✗ Error: {e}{Colors.RESET}")

            if attempt < max_retries - 1:
                time.sleep((2 ** attempt) * 5)

        self.consecutive_failures += 1
        return None

    def get_machines(self, force_refresh: bool = False) -> List[Dict]:
        """Get machines from cache or API"""

        # Check cache age
        cache_age = self.db.get_cache_age()
        cache_valid = cache_age and cache_age < timedelta(hours=self.cache_hours)

        if not force_refresh and cache_valid:
            print(f"{Colors.GREEN}✓ Loading machines from database cache (age: {cache_age.seconds//3600}h){Colors.RESET}")
            machines = self.db.get_all_machines_from_db()
            if machines:
                return machines
            print(f"{Colors.YELLOW}[!] Cache empty, fetching from API...{Colors.RESET}")

        # Fetch from API
        print(f"{Colors.CYAN}[*] Fetching retired machines from API...{Colors.RESET}")

        all_machines = []
        page = 1

        while True:
            url = f"{self.base_url}/api/v5/machines?per_page=100&state=retired&page={page}"
            response = self._make_request(url)

            if not response:
                break

            try:
                data = response.json()
                machines = data.get('data', [])

                if not machines:
                    break

                all_machines.extend(machines)

                meta = data.get('meta', {})
                current_page = meta.get('current_page', page)
                last_page = meta.get('last_page', page)

                print(f"{Colors.BLUE}[*] Fetched page {current_page}/{last_page} ({len(machines)} machines){Colors.RESET}")

                if current_page >= last_page:
                    break

                page += 1

            except json.JSONDecodeError:
                break

        if all_machines:
            print(f"{Colors.GREEN}✓ Fetched {len(all_machines)} machines from API{Colors.RESET}")
            # Save to database
            print(f"{Colors.CYAN}[*] Updating database cache...{Colors.RESET}")
            for machine in all_machines:
                self.db.upsert_machine(machine)
            self.db.update_cache_timestamp()
            print(f"{Colors.GREEN}✓ Database cache updated{Colors.RESET}")

        return all_machines

    def download_writeup(self, machine_id: int, machine_name: str, output_path: Path) -> Tuple[bool, int, str]:
        """Download writeup PDF for a machine"""
        url = f"{self.base_url}/api/v4/machine/writeup/{machine_id}"

        response = self._make_request(url, stream=True)
        if not response:
            return False, 0, "Failed to fetch writeup"

        content_type = response.headers.get('Content-Type', '')
        if 'pdf' not in content_type.lower():
            return False, 0, f"Invalid content type: {content_type}"

        try:
            file_size = 0
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        file_size += len(chunk)

            if file_size == 0:
                return False, 0, "Empty file"

            return True, file_size, None

        except Exception as e:
            return False, 0, str(e)

    def organize_machine_folder(self, machine: Dict) -> Path:
        """Create organized folder structure"""
        name = machine.get('name', 'Unknown')
        os_type = machine.get('os', 'Unknown')
        difficulty = machine.get('difficultyText', 'Unknown')

        machine_folder = self.output_dir / 'writeups' / os_type / difficulty / name
        machine_folder.mkdir(parents=True, exist_ok=True)

        return machine_folder

    def download_machine_writeup(self, machine: Dict, skip_existing: bool = True) -> str:
        """Download writeup for a single machine"""
        machine_id = machine.get('id')
        machine_name = machine.get('name', 'Unknown')

        machine_folder = self.organize_machine_folder(machine)
        writeup_path = machine_folder / f"{machine_name}.pdf"

        downloaded_ids = self.db.get_downloaded_machine_ids()
        if skip_existing and machine_id in downloaded_ids and writeup_path.exists():
            return 'skipped'

        success, file_size, error_msg = self.download_writeup(machine_id, machine_name, writeup_path)

        if success:
            self.db.upsert_writeup(machine_id, str(writeup_path), file_size, 'success')
            return 'success'
        else:
            self.db.upsert_writeup(machine_id, str(writeup_path), 0, 'failed', error_msg)
            return 'failed'

    def download_all_writeups(self, max_workers: int = 3, skip_existing: bool = True,
                             filter_difficulty: Optional[str] = None,
                             filter_os: Optional[str] = None,
                             limit: Optional[int] = None,
                             force_refresh: bool = False):
        """Download all writeups with smart caching"""

        start_time = time.time()

        print(f"{Colors.BOLD}{'='*60}{Colors.RESET}")
        print(f"{Colors.BOLD}HTB Writeups Downloader{Colors.RESET}")
        print(f"{Colors.BOLD}{'='*60}{Colors.RESET}\n")

        # Get machines (from cache or API)
        machines = self.get_machines(force_refresh)

        if not machines:
            print(f"{Colors.RED}✗ No machines available{Colors.RESET}")
            return

        # Get pending downloads
        machines_to_download = self.db.get_pending_downloads(machines)

        print(f"{Colors.CYAN}📊 Status: {len(machines_to_download)}/{len(machines)} machines need downloading{Colors.RESET}")

        # Apply filters
        if filter_difficulty:
            machines_to_download = [m for m in machines_to_download 
                                   if m.get('difficultyText', '').lower() == filter_difficulty.lower()]
            print(f"{Colors.YELLOW}[*] Filtered to {len(machines_to_download)} {filter_difficulty} machines{Colors.RESET}")

        if filter_os:
            machines_to_download = [m for m in machines_to_download 
                                   if m.get('os', '').lower() == filter_os.lower()]
            print(f"{Colors.YELLOW}[*] Filtered to {len(machines_to_download)} {filter_os} machines{Colors.RESET}")

        if limit:
            machines_to_download = machines_to_download[:limit]
            print(f"{Colors.YELLOW}[*] Limited to {limit} machines{Colors.RESET}")

        if not machines_to_download:
            print(f"{Colors.GREEN}✓ All machines already downloaded!{Colors.RESET}")
            return

        progress = ProgressTracker(len(machines_to_download))

        print(f"\n{Colors.BOLD}Downloading {len(machines_to_download)} writeups...{Colors.RESET}\n")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.download_machine_writeup, machine, skip_existing): machine 
                for machine in machines_to_download
            }

            for future in as_completed(futures):
                machine = futures[future]
                machine_name = machine.get('name', 'Unknown')

                try:
                    status = future.result()
                    progress.increment(status)

                    stats = progress.get_stats()

                    if status == 'success':
                        print(f"{Colors.GREEN}✓{Colors.RESET} [{stats['current']}/{stats['total']}] {machine_name}")
                    elif status == 'skipped':
                        print(f"{Colors.YELLOW}⊘{Colors.RESET} [{stats['current']}/{stats['total']}] {machine_name}")
                    else:
                        print(f"{Colors.RED}✗{Colors.RESET} [{stats['current']}/{stats['total']}] {machine_name}")

                    if self.consecutive_failures >= self.max_consecutive_failures:
                        print(f"\n{Colors.RED}[!] Too many failures. Stopping.{Colors.RESET}")
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

                except Exception as e:
                    progress.increment('failed')

        duration = time.time() - start_time
        stats = progress.get_stats()
        self.db.record_download_session(stats, duration)

        print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
        print(f"{Colors.BOLD}Complete!{Colors.RESET}")
        print(f"{Colors.GREEN}✓ Success: {stats['success']}{Colors.RESET} | "
              f"{Colors.YELLOW}⊘ Skipped: {stats['skipped']}{Colors.RESET} | "
              f"{Colors.RED}✗ Failed: {stats['failed']}{Colors.RESET}")
        print(f"Duration: {duration:.1f}s ({duration/60:.1f} min)")
        print(f"\nWriteups: {(self.output_dir / 'writeups').absolute()}")
        print(f"{Colors.BOLD}{'='*60}{Colors.RESET}\n")

    def show_statistics(self):
        """Display database statistics"""
        stats = self.db.get_statistics()

        print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
        print(f"{Colors.BOLD}HTB Collection Statistics{Colors.RESET}")
        print(f"{Colors.BOLD}{'='*60}{Colors.RESET}\n")

        total = stats.get('total_machines', 0)
        downloaded = stats.get('downloaded', 0)

        print(f"{Colors.CYAN}📊 Overall{Colors.RESET}")
        print(f"  Total:      {total}")
        print(f"  Downloaded: {downloaded}")
        print(f"  Remaining:  {total - downloaded}")
        if total > 0:
            print(f"  Progress:   {downloaded/total*100:.1f}%\n")

        if stats.get('by_os'):
            print(f"{Colors.CYAN}💻 By OS{Colors.RESET}")
            for os_name, count in sorted(stats['by_os'].items()):
                print(f"  {os_name:12} {count}")
            print()

        if stats.get('by_difficulty'):
            print(f"{Colors.CYAN}🎯 By Difficulty{Colors.RESET}")
            for diff, count in sorted(stats['by_difficulty'].items()):
                print(f"  {diff:12} {count}")

        print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}\n")

def main():
    parser = argparse.ArgumentParser(description='HTB Writeups Downloader')

    parser.add_argument('--token', required=True, help='HTB API token')
    parser.add_argument('--output', default='./htb_writeups')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--difficulty', choices=['easy', 'medium', 'hard', 'insane'])
    parser.add_argument('--os', choices=['linux', 'windows', 'freebsd', 'openbsd', 'android'])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--no-skip', action='store_true')
    parser.add_argument('--refresh', action='store_true', help='Force refresh from API')
    parser.add_argument('--stats', action='store_true')
    parser.add_argument('--delay', type=float, default=0.5)
    parser.add_argument('--cache-hours', type=int, default=24*365, help='Cache duration in hours (default ~1 year)')

    args = parser.parse_args()

    if args.workers < 1 or args.workers > 10:
        args.workers = 3

    downloader = HTBWriteupsDownloader(args.token, args.output, args.cache_hours)
    downloader.rate_limit_delay = args.delay

    if args.stats:
        downloader.show_statistics()
        return

    try:
        downloader.download_all_writeups(
            max_workers=args.workers,
            skip_existing=not args.no_skip,
            filter_difficulty=args.difficulty,
            filter_os=args.os,
            limit=args.limit,
            force_refresh=args.refresh
        )
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}[!] Interrupted. Progress saved.{Colors.RESET}")
        sys.exit(0)

if __name__ == "__main__":
    main()
