# Steam Extractor 🕹️

**Multi-tool suite for extracting and analyzing Steam game reviews**, with support for tag-based discovery, country filtering, and SteamSpy catalog integration.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/github/license/murillonunes/steam-extractor)](LICENSE)

## Overview

This project is a Python package with five CLI tools that work together:

| Command | Description |
|---------|-------------|
| `steamspy-catalog` | Builds a local SteamSpy catalog with tags for all games (run once) |
| `steam-reviews` | Extracts reviews for a single game by appid, language and date range |
| `steam-reviews-sync` | Incrementally archives and versions reviews in local SQLite |
| `steam-profiles-sync` | Resumably enriches archived review authors with country metadata |
| `steam-extractor` | Full pipeline: discovers games by tag, collects reviews, enriches with country |

## Features

- 🏷️ **Tag-based discovery** — find games by genre/category (e.g. `Action FPS Singleplayer`)
- 🌍 **Country filtering** — keep only reviews from specific countries (via Steam Web API)
- 📥 **Full review extraction** with robust pagination (`cursor`-based) and retry/backoff
- 📅 **Date range filtering** with early-stop pagination to avoid unnecessary requests
- 🔁 **Multi-pass deduplication** — re-runs merged by `recommendationid` to recover reviews missed by cursor drift
- 💾 **CSV / JSON output** via pandas
- 🧾 **Research manifest** with query provenance, coverage and completeness diagnostics
- 🗄️ **Incremental SQLite archive** with checkpoints, resume, deduplication and versions
- 🧩 **Language-partitioned archival** for `--language all`, with independent cursors and consolidated coverage
- 🚫 **Off-topic reviews excluded** from incremental research collections
- 🗂️ **Incremental catalog saves** — SteamSpy fetcher resumes interrupted runs
- 🕐 Timestamps converted to `dd/mm/yyyy` readable format

## Requirements

- Python 3.11+
- A [Steam Web API key](https://steamcommunity.com/dev/apikey) (for country enrichment)

## Installation

```bash
git clone https://github.com/murillonunes/steam-extractor.git
cd steam-extractor
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

## Usage

### Step 1 — Build the local catalog (once)

```bash
# Quick test (~2,000 apps):
steamspy-catalog --max-pages 2

# Full catalog (~24h, ~80,000 apps):
steamspy-catalog

# Resume an interrupted run:
steamspy-catalog --resume
```

### Step 2a — Extract reviews for a single game

```bash
steam-reviews <appid> <language> <start_date> <end_date> [--format json|csv] [--passes N]
```

| Argument | Description | Example |
|----------|-------------|---------|
| `appid` | Steam Game ID | `1091500` |
| `language` | Language code | `pt-br`, `en`, `all` |
| `start_date` | Start date (dd/mm/yyyy) | `01/01/2021` |
| `end_date` | End date (dd/mm/yyyy) | `31/12/2021` |
| `--format` | Output format (default: `json`) | `json` or `csv` |
| `--passes N` | Independent collection passes merged via deduplication (default: `1`). Use `3+` for high-volume games. | `3` |

```bash
# Cyberpunk 2077 — Brazilian Portuguese reviews (2021) as CSV
steam-reviews 1091500 pt-br 01/01/2021 31/12/2021 --format csv

# CS2 — All languages (2024), 3 passes for consistency
steam-reviews 730 all 01/01/2024 31/12/2024 --passes 3

# Elden Ring — English reviews (2022-2023) as CSV
steam-reviews 1245620 en 01/01/2022 31/12/2023 --format csv
```

### Step 2b — Extract reviews by tag + country (full pipeline)

```bash
steam-extractor --tags <TAG...> --countries <CC...> \
    --start dd/mm/yyyy --end dd/mm/yyyy [--api-key YOUR_KEY] [options]
```

The API key can also be provided via the `STEAM_API_KEY` environment variable instead of `--api-key`.

| Argument | Description | Default |
|----------|-------------|---------|
| `--tags` | Genre/category tags (AND logic) | required |
| `--countries` | ISO country codes to filter | all countries |
| `--start` | Start date (dd/mm/yyyy) | required |
| `--end` | End date (dd/mm/yyyy) | required |
| `--api-key` | Steam Web API key (or set `STEAM_API_KEY`) | — |
| `--appids` | Skip catalog lookup, use explicit app IDs | — |
| `--max-games` | Max games per tag | `10` |
| `--min-reviews` | Min total reviews per game | `0` |
| `--passes N` | Collection passes per game merged via deduplication (default: `1`). Use `3+` to recover reviews missed by cursor drift. | `1` |
| `--game-delay S` | Seconds to wait between games (default: `1.5`). Increase to reduce session throttling when processing multiple high-volume games. | `1.5` |
| `--format` | `csv` or `json` | `csv` |
| `--output` | Output filename (no extension) | auto-generated |
| `--db` | Path to local SteamSpy catalog | `steamspy_catalog.json` |
| `--review-db` | Optional SQLite archive reused when interval coverage is complete | — |
| `--allow-unverified-cache` | Reuse resumed cursor coverage, marking the dataset incomplete | disabled |
| `--profile-db` | Persistent SQLite cache for country lookup results (defaults to `--review-db`, then `steam_profiles.sqlite`) | automatic |
| `--profile-max-batches N` | Pause country enrichment after N API batches; completed batches remain cached | unlimited |
| `--profile-max-runtime SECONDS` | Pause country enrichment after the requested duration | unlimited |
| `--refresh-profiles` | Query terminal cached profiles again | disabled |

```bash
# Set API key once in the environment (keeps it out of shell history):
export STEAM_API_KEY=YOUR_KEY

# MVP test: Action games from Brazil and US (December 2025), 2 games
steam-extractor --tags Action --countries BR US \
    --start 01/12/2025 --end 31/12/2025 --max-games 2

# Larger run: FPS games with at least 10,000 reviews (2024), 2 passes
steam-extractor --tags Action FPS --countries BR US \
    --start 01/01/2024 --end 31/12/2024 \
    --max-games 10 --min-reviews 10000 --passes 2 --game-delay 30

# Explicit app IDs, skip catalog lookup
steam-extractor --appids 730 1091500 1245620 \
    --countries BR --start 01/01/2024 --end 31/12/2024
```

### Incremental archive for high-volume games

SQLite is embedded: no database server or continuously running process is required. Each
CLI invocation opens the file, commits pages transactionally and closes it on exit.

`--language all` partitions the archive into one independent cursor stream for every
Steam-supported review language. This avoids premature exhaustion of a single very large
`language=all` stream. Rows are still merged into one archive by `recommendationid`, and
the synthetic `all` coverage is marked verified only after every language partition is
verified. Steam's off-topic activity remains excluded.

```bash
# Archive reviews down to the beginning of 2024, pausing after 30 minutes if necessary
steam-reviews-sync 730 --language all --start 01/01/2024 \
    --database steam_reviews.sqlite --max-runtime 1800

# Continue a paused/failed cursor checkpoint
steam-reviews-sync 730 --language all --start 01/01/2024 \
    --database steam_reviews.sqlite --max-runtime 1800 --resume

# Collect only one language (aliases such as pt-br and en are accepted)
steam-reviews-sync 1091500 --language pt-br \
    --database cyberpunk_reviews.sqlite
```

Each page and its next cursor are committed in the same SQLite transaction. A repeated
`recommendationid` is not duplicated. When mutable review content changes, the current row
is updated and the observed version is appended to `review_versions`.

An empty API page is not sufficient to declare the entire history complete. The synchronizer
confirms an unexpected empty page three times at the same cursor and continues if any retry
returns reviews. It also compares the cumulative number of reviews received with Steam's
`total_reviews`, allowing only a small tolerance for live-stream drift. If all confirmations
remain empty with a material deficit, the run is marked `incomplete` with reason
`api_exhausted_before_expected_total`; reviews and verified temporal bounds already collected
remain available in SQLite.

On later non-resume runs, synchronization starts at the live head and stops after three
fully known pages by default. The `updated` ordering is also checked after an existing
archive is present. A resumed cursor is followed by a head reconciliation because Steam's
review stream is live. That reconciliation is a full independent verification pass, not
only a short overlap check. If it finds new recommendation IDs, another full pass starts
from the live head until a pass adds no IDs or the configured verification limit is reached.

The command reports two distinct completion indicators:

- `operational_complete`: the requested cursor traversal reached its stopping condition;
- `research_verified`: an uninterrupted traversal satisfied the expected-total check, and
  recovery verification (when required) converged without adding another recommendation ID.

Runs recovered from an `incomplete` state also restart at the live head and use the same
convergence rule. The SQLite `sync_runs` table records whether each run was a verification
pass, both completion indicators, and cumulative progress across resumed processes for later
auditing. Totals for languages with zero reviews are persisted even though no review page is
written.

Useful limits:

| Argument | Description |
|----------|-------------|
| `--language CODE` | `all` partitions all supported languages; a code collects only that language |
| `--max-pages N` | Pause after N pages in this invocation |
| `--max-runtime SECONDS` | Pause after the requested wall-clock duration |
| `--resume` | Continue a paused or failed checkpoint |
| `--overlap-pages N` | Known consecutive pages required before incremental stop |
| `--verification-passes N` | Maximum full verification passes after a resumed or incomplete run (default: `3`) |
| `--no-sync-updates` | Skip the separate check for recently edited reviews |

Pressing `Ctrl+C` rolls back an unfinished page, marks the synchronization as
`paused` with reason `user_interrupted`, and preserves the last committed cursor.
Run the same command with `--resume` to continue from that checkpoint.

Once the database contains verified coverage for a requested interval, the country/tag
pipeline can reuse it without downloading the reviews again:

```bash
steam-extractor --tags Action --appids 730 --countries BR US \
    --start 01/01/2024 --end 31/12/2024 \
    --review-db steam_reviews.sqlite --output cs2_2024_br_us
```

If verified local coverage is unavailable, `steam-extractor` logs that fact and falls back
to the Steam API. The research manifest records whether each game came from `sqlite_cache`
or `steam_api`.

Country enrichment is also resumable. Each completed profile batch is persisted in the
profile database. Authors are classified as `country_available`, `country_unavailable`
(profile returned without a declared country), `profile_unavailable` (profile not returned),
or `request_failed` (transient lookup failure). Later runs reuse terminal results and retry
only failures or authors not yet checked. Rate limits and transport errors such as timeouts
use bounded retries; `Retry-After` is honored when Steam provides it. The normal request
delay increases after HTTP 429 and decreases gradually after successful batches. Runtime
limits also apply to retry backoff, leaving an unfinished batch pending instead of exceeding
the configured deadline. Results report rate-limit events, retry waits, and initial/final
request delays. The manifest records the profile batch, runtime and refresh options used by
the command.

When `--countries` is used, reviews with another known country are filtered out, but reviews
whose country is unavailable, pending, or failed are retained with an explicit
`country_status`. This prevents missing profile information from being silently discarded.
The research manifest reports author and review status counts, the known-country
distribution, and country coverage by language and year. Coverage metrics keep
`processing_coverage_percent` (classified records divided by all requested records)
separate from `country_availability_among_classified_percent` (records with a country
divided only by successfully classified records), so a partial run is not mistaken for
low profile availability.

For large archives, use the dedicated profile command. It reads only distinct author IDs
from SQLite, does not load review text, and does not generate a CSV:

```bash
steam-profiles-sync 1091500 \
    --database data/cyberpunk_2077_reviews.sqlite \
    --start 10/12/2020 --end 26/07/2026 \
    --max-runtime 1800
```

Repeat the same command to continue. Terminal profile states are reused, transient failures
are retried, and every invocation prints JSON summaries for the state before and after the
run, including status counts, processing coverage, country availability and known-country
distribution. `--max-batches N` can be used instead of or together with the runtime limit.
Use `--refresh-profiles` only when previously classified profiles should be queried again.

A cursor resumed across separate processes cannot by itself prove that the live Steam stream
did not shift at the boundary. Such data becomes verified only after the independent
full-pass convergence described above. Data that has not converged remains stored but is not
reused by default. For exploratory work, `--allow-unverified-cache` permits reuse while
forcing `dataset_complete: false` in the manifest; confirmatory research should retain the
conservative default.

## Output Schema (`steam-extractor`)

| Column | Description |
|--------|-------------|
| `app_id` | Steam app ID |
| `app_name` | Game name |
| `recommendation_id` | Unique Steam review ID used for auditing and deduplication |
| `review_version` | Observed archive version (`1` for a newly observed live review) |
| `review_content_hash` | SHA-256 of research-relevant mutable review fields |
| `user_id` | Steam user ID (steamid64) |
| `country_code` | ISO 3166-1 alpha-2 (e.g. `BR`, `US`) |
| `country_status` | Country lookup status; distinguishes available, unavailable, missing profiles and transient failures |
| `language` | Review language |
| `review_text` | Review content |
| `voted_up` | `True` / `False` |
| `tag` | Tag combination used to find the game |
| `date_created` | Review date (dd/mm/yyyy) |

## Research Manifest

Every extraction writes a JSON sidecar next to the dataset:

```text
reviews_action_br_01-01-2024_31-12-2024.csv
reviews_action_br_01-01-2024_31-12-2024.metadata.json
```

The manifest records:

- package version, Git commit and whether the working tree had uncommitted changes;
- query parameters, Steam filters and requested collection passes;
- SteamSpy catalog snapshot metadata;
- per-game pages, expected/scanned reviews, reviews in range, completion reason and drift;
- country-profile coverage, failed batches and rows retained by the country filter;
- output filename, format and row count;
- an aggregate `dataset_complete` flag.

If a query returns no rows, no empty CSV/JSON is created, but the metadata sidecar is still
written so the null result remains auditable.

## Project Structure

```
steam-extractor/
├── src/
│   └── steam_extractor/
│       ├── __init__.py
│       ├── tag_extractor.py        # Full pipeline: tags → reviews → country enrichment
│       ├── reviews_fetcher.py      # Low-level review fetcher (single game)
│       └── catalog_fetcher.py      # SteamSpy catalog builder
├── pyproject.toml
├── requirements.txt
├── LICENSE
└── logs/                           # Auto-created; log files written here
```
