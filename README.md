# HTB Writeups Downloader

Automates downloading PDF writeups for retired HackTheBox machines using the HTB API.

## Features

- Downloads writeups for all retired machines
- Cache machine info with SQLite database
- Multi-threaded downloads for faster processing
- Organized folder structure: `writeups/OS/Difficulty/Machine/`
- Skips already downloaded writeups on repeated runs
- Filtering options (difficulty, OS, limit)

## Requirements

- Python 3.7+
- Valid HTB VIP subscription
- HTB API bearer token

### Options

- `--token` (required): Your HTB API bearer token
- `--output`: Output directory (default: `./htb_writeups`)
- `--workers`: Number of concurrent downloads (default: 3)
- `--difficulty`: Filter by difficulty (easy, medium, hard, insane)
- `--os`: Filter by OS (linux, windows, freebsd, openbsd, android)
- `--limit`: Limit number of machines to download
- `--refresh`: Force refresh machine list from API
- `--delay`: Rate limit delay in seconds (default: 0.5)

### Usage Examples

```bash
# Download all writeups 
python htb-writeups-downloader.py --token YOUR_TOKEN

# Download only easy Linux machines
python htb-writeups-downloader.py --token YOUR_TOKEN --difficulty easy --os linux
```

## ToS

This script is compliant with HackTheBox's Terms of Service.
- The script only targets **retired** machines
- Users must have their own valid VIP subscription to generate a bearer token

## License

MIT License