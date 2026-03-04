# Steam Extractor 🕹️

**Automatic extractor of game reviews from Steam**, with language and date range filters.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/github/license/murillonunes/steam-extractor)](LICENSE)

## Features

- 📥 Extracts **all** reviews from any game via Steam API
- 🌍 Filters by language (`pt-br`, `en-us`, `all`, etc.)
- 📅 Filters by **date range** (e.g. 01/01/2021 to 31/12/2023)
- 💾 Exports to **JSON** or **CSV** (via pandas)
- 🕐 Converts Unix timestamps to `dd/mm/yyyy` readable format
- 🔄 Robust pagination via `cursor` with retry/backoff

## Requirements

- Python 3.10+
- pip packages: `requests`, `pandas`

## Installation

```bash
git clone https://github.com/murillonunes/steam-extractor.git
cd steam-extractor
python3 -m venv .venv
source .venv/bin/activate
pip install requests pandas
```

## Usage

```bash
python3 steam_extractor.py <appid> <language> <start_date> <end_date> [--format json|csv]
```

### Arguments

| Argument | Description | Example |
|----------|-------------|---------|
| `appid` | Steam Game ID | `1091500` |
| `language` | Language code | `pt-br`, `en`, `all` |
| `start_date` | Start date (dd/mm/yyyy) | `01/01/2021` |
| `end_date` | End date (dd/mm/yyyy) | `31/12/2021` |
| `--format` | Output format (default: `json`) | `json` or `csv` |

### Examples

```bash
# Cyberpunk 2077 - Brazilian Portuguese reviews (2021) as CSV
python3 steam_extractor.py 1091500 pt-br 01/01/2021 31/12/2021 --format csv

# CS2 - All languages (2024) as JSON
python3 steam_extractor.py 730 all 01/01/2024 31/12/2024

# Elden Ring - English reviews (2022-2023) as CSV
python3 steam_extractor.py 1245620 en 01/01/2022 31/12/2023 --format csv
```