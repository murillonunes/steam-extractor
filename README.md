# Steam Extractor 🕹️

**Multi-tool suite for extracting and analyzing Steam game reviews**, with support for tag-based discovery, country filtering, and SteamSpy catalog integration.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/github/license/murillonunes/steam-extractor)](LICENSE)

## Overview

This project consists of three independent scripts that work together:

| Script | Description |
|--------|-------------|
| `steamspy_catalog_fetcher.py` | Builds a local SteamSpy catalog with tags for all games (run once) |
| `steam_reviews_fetcher.py` | Extracts reviews for a single game by appid, language and date range |
| `steam_extractor.py` | Full pipeline: discovers games by tag, collects reviews, enriches with country |

## Features

- 🏷️ **Tag-based discovery** — find games by genre/category (e.g. `Action FPS Singleplayer`)
- 🌍 **Country filtering** — keep only reviews from specific countries (via Steam Web API)
- 📥 **Full review extraction** with robust pagination (`cursor`-based) and retry/backoff
- 📅 **Date range filtering** with early-stop pagination to avoid unnecessary requests
- 🔁 **Deduplication** by `recommendationid`
- 💾 **CSV / JSON output** via pandas
- 🗂️ **Incremental catalog saves** — SteamSpy fetcher resumes interrupted runs
- 🕐 Timestamps converted to `dd/mm/yyyy` readable format

## Requirements

- Python 3.10+
- pip packages: `requests`, `pandas`, `numpy`
- A [Steam Web API key](https://steamcommunity.com/dev/apikey) (for country enrichment)

## Installation

```bash
git clone https://github.com/murillonunes/steam-extractor.git
cd steam-extractor
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

### Step 1 — Build the local catalog (once)

```bash
# Quick test (~2,000 apps):
python3 steamspy_catalog_fetcher.py --max-pages 2

# Full catalog (~24h, ~80,000 apps):
python3 steamspy_catalog_fetcher.py

# Resume an interrupted run:
python3 steamspy_catalog_fetcher.py --resume
```

### Step 2a — Extract reviews for a single game

```bash
python3 steam_reviews_fetcher.py <appid> <language> <start_date> <end_date> [--format json|csv]
```

| Argument | Description | Example |
|----------|-------------|---------|
| `appid` | Steam Game ID | `1091500` |
| `language` | Language code | `pt-br`, `en`, `all` |
| `start_date` | Start date (dd/mm/yyyy) | `01/01/2021` |
| `end_date` | End date (dd/mm/yyyy) | `31/12/2021` |
| `--format` | Output format (default: `json`) | `json` or `csv` |

```bash
# Cyberpunk 2077 — Brazilian Portuguese reviews (2021) as CSV
python3 steam_reviews_fetcher.py 1091500 pt-br 01/01/2021 31/12/2021 --format csv

# CS2 — All languages (2024) as JSON
python3 steam_reviews_fetcher.py 730 all 01/01/2024 31/12/2024

# Elden Ring — English reviews (2022-2023) as CSV
python3 steam_reviews_fetcher.py 1245620 en 01/01/2022 31/12/2023 --format csv
```

### Step 2b — Extract reviews by tag + country (full pipeline)

```bash
python3 steam_extractor.py --tags <TAG...> --countries <CC...> \
    --start dd/mm/yyyy --end dd/mm/yyyy --api-key YOUR_KEY [options]
```

| Argument | Description | Default |
|----------|-------------|---------|
| `--tags` | Genre/category tags (AND logic) | required |
| `--countries` | ISO country codes to filter | all countries |
| `--start` | Start date (dd/mm/yyyy) | required |
| `--end` | End date (dd/mm/yyyy) | required |
| `--api-key` | Steam Web API key | required |
| `--appids` | Skip catalog lookup, use explicit app IDs | — |
| `--max-games` | Max games per tag | `10` |
| `--min-reviews` | Min total reviews per game | `0` |
| `--format` | `csv` or `json` | `csv` |
| `--output` | Output filename (no extension) | auto-generated |
| `--db` | Path to local SteamSpy catalog | `steamspy_catalog.json` |

```bash
# MVP test: Action games from Brazil and US (December 2025), 2 games
python3 steam_extractor.py --tags Action --countries BR US \
    --start 01/12/2025 --end 31/12/2025 \
    --max-games 2 --api-key YOUR_KEY

# Larger run: FPS games with at least 10,000 reviews (2024)
python3 steam_extractor.py --tags Action FPS --countries BR US \
    --start 01/01/2024 --end 31/12/2024 \
    --max-games 10 --min-reviews 10000 --api-key YOUR_KEY

# Explicit app IDs, skip catalog lookup
python3 steam_extractor.py --appids 730 1091500 1245620 \
    --countries BR --start 01/01/2024 --end 31/12/2024 \
    --api-key YOUR_KEY
```

## Output Schema (`steam_extractor.py`)

| Column | Description |
|--------|-------------|
| `app_id` | Steam app ID |
| `app_name` | Game name |
| `user_id` | Steam user ID (steamid64) |
| `country_code` | ISO 3166-1 alpha-2 (e.g. `BR`, `US`) |
| `language` | Review language |
| `review_text` | Review content |
| `voted_up` | `True` / `False` |
| `tag` | Tag combination used to find the game |
| `date_created` | Review date (dd/mm/yyyy) |

## Project Structure

```
steam-extractor/
├── steam_extractor.py          # Main pipeline: tags → reviews → country enrichment
├── steam_reviews_fetcher.py    # Low-level review fetcher (single game)
├── steamspy_catalog_fetcher.py # SteamSpy catalog builder
├── requirements.txt
├── LICENSE
└── logs/                       # Auto-created; log files written here
```
