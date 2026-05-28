#!/usr/bin/env python3

"""
SteamSpy Catalog Fetcher - Builds a local SteamSpy catalog with user tags.

Two-step process using only the SteamSpy API:
    1. request=all (paginated) → full list of app IDs + basic info
    2. request=appdetails      → user tags for each app

No dependency on steam_catalog.json or the Steam API.

Rate limits:
    - request=all:        1 req/min  (~1,000 apps/min)
    - request=appdetails: 1 req/s    (~3,600 apps/h)

Estimated total time for full run: ~24h (appdetails is the bottleneck).
Saves incrementally every SAVE_INTERVAL apps so progress is never lost.

Usage:
    # Quick test (first 2 pages = 2,000 apps, then appdetails for each):
    python3 steamspy_catalog_fetcher.py --max-pages 2

    # Full run:
    python3 steamspy_catalog_fetcher.py

    # Resume an interrupted run:
    python3 steamspy_catalog_fetcher.py --resume

    # Custom output:
    python3 steamspy_catalog_fetcher.py --output data/steamspy_catalog.json
"""

import argparse
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

STEAMSPY_URL       = "https://steamspy.com/api.php"
DEFAULT_OUTPUT     = "steamspy_catalog.json"
ALL_PAGE_DELAY     = 61    # seconds between request=all pages (rate limit: 1/min)
SAVE_INTERVAL      = 100   # save every N newly processed apps
MAX_RETRIES        = 3
DEFAULT_WORKERS    = 10    # parallel threads for appdetails


# ---------------------------------------------------------------------------
# Step 1: fetch all app IDs via request=all
# ---------------------------------------------------------------------------

def fetch_all_appids(max_pages: int | None) -> dict:
    """
    Paginates through request=all to collect all app IDs and basic info.
    Returns a dict of appid → basic info (name, positive, negative, owners).
    """
    print("[SteamSpy] Fetching full app list via request=all...")
    all_apps = {}
    page     = 0

    while True:
        if max_pages is not None and page >= max_pages:
            print(f"[SteamSpy] Reached max_pages limit ({max_pages}).")
            break

        print(f"  Page {page}...", end=" ", flush=True)

        try:
            r = requests.get(
                STEAMSPY_URL,
                params={"request": "all", "page": page},
                timeout=30,
            )
            r.raise_for_status()

            # Empty or whitespace-only response means end of data
            if not r.text or not r.text.strip():
                print("empty response — last page reached.")
                break

            data = r.json()

        except ValueError:
            # JSON parse error = empty/invalid body = end of data
            print("invalid JSON — last page reached.")
            break
        except Exception as e:
            print(f"Error: {e}. Retrying in 30s...")
            time.sleep(30)
            continue

        if not data:
            print("empty — last page reached.")
            break

        all_apps.update({
            str(appid): {
                "name":     info.get("name", ""),
                "positive": info.get("positive", 0),
                "negative": info.get("negative", 0),
                "owners":   info.get("owners", ""),
            }
            for appid, info in data.items()
        })

        print(f"{len(data)} apps. Total: {len(all_apps):,}")
        page += 1

        if not (max_pages is not None and page >= max_pages):
            print(f"  Waiting {ALL_PAGE_DELAY}s...", flush=True)
            time.sleep(ALL_PAGE_DELAY)

    print(f"[SteamSpy] App list complete: {len(all_apps):,} apps\n")
    return all_apps


# ---------------------------------------------------------------------------
# Step 2: fetch tags via request=appdetails
# ---------------------------------------------------------------------------

def fetch_appdetails(appid: str) -> dict | None:
    """
    Fetches user tags for a single app via request=appdetails.
    Returns dict with tags field, or None on failure.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(
                STEAMSPY_URL,
                params={"request": "appdetails", "appid": appid},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()

            if not data or "appid" not in data:
                return None

            return {"tags": data.get("tags") or {}}

        except Exception as e:
            if attempt < MAX_RETRIES:
                wait = attempt * 2
                print(f"    Retry {attempt}/{MAX_RETRIES} (wait {wait}s): {e}")
                time.sleep(wait)
            else:
                return None

    return None


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_existing(output_path: str) -> tuple[dict, set]:
    """Loads existing catalog for resuming. Returns (games, processed_appids)."""
    path = Path(output_path)
    if not path.exists():
        return {}, set()

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    games     = data.get("games", {})
    processed = set(data.get("processed_appids", []))
    print(f"[Resume] Found {len(games):,} games, "
          f"{len(processed):,} apps already processed.")
    return games, processed


def save_catalog(games: dict, processed: set, output_path: str) -> None:
    """Saves the current catalog to disk."""
    output = {
        "metadata": {
            "downloaded_at":   datetime.now(tz=timezone.utc).isoformat(),
            "total_games":     len(games),
            "total_processed": len(processed),
        },
        "processed_appids": list(processed),
        "games": games,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def fetch_catalog(
    output_path: str,
    max_pages: int | None,
    resume: bool,
    workers: int = DEFAULT_WORKERS,
) -> None:
    """
    Full pipeline:
        1. Paginate request=all → collect all appids
        2. For each appid, fetch request=appdetails → get tags
        3. Save incrementally
    """
    print("=" * 55)
    print("SteamSpy Catalog Fetcher")
    print(f"Output    : {output_path}")
    print(f"Max pages : {max_pages if max_pages else 'all'}")
    print(f"Resume    : {resume}")
    print(f"Workers   : {workers} threads")
    print("=" * 55 + "\n")

    # ------------------------------------------------------------------
    # Step 1: get full app list
    # ------------------------------------------------------------------
    app_list = fetch_all_appids(max_pages)
    appids   = list(app_list.keys())
    total    = len(appids)

    # ------------------------------------------------------------------
    # Step 2: enrich with tags via appdetails
    # ------------------------------------------------------------------
    games, processed = load_existing(output_path) if resume else ({}, set())

    fetched  = 0
    failed   = 0
    skipped  = 0
    lock     = Lock()

    # Filter out already-processed appids
    pending = [a for a in appids if a not in processed]
    skipped = len(appids) - len(pending)

    print(f"[SteamSpy] Fetching tags for {len(pending):,} apps "
          f"({workers} threads)...\n")

    def process(appid: str) -> tuple[str, dict | None]:
        return appid, fetch_appdetails(appid)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process, appid): appid for appid in pending}

        for future in as_completed(futures):
            appid, details = future.result()

            with lock:
                processed.add(appid)

                if details:
                    games[appid] = {**app_list[appid], **details}
                    fetched += 1
                    tags_preview = list(details["tags"].keys())[:3]
                    i = len(processed)
                    print(f"  [{i:>{len(str(total))}}/{total}] "
                          f"{app_list[appid]['name'][:40]:<40} "
                          f"| reviews: {(app_list[appid]['positive'] or 0) + (app_list[appid]['negative'] or 0):>6,} "
                          f"| tags: {tags_preview}")
                else:
                    failed += 1

                # Save every SAVE_INTERVAL newly processed apps
                newly_processed = fetched + failed
                if newly_processed > 0 and newly_processed % SAVE_INTERVAL == 0:
                    save_catalog(games, processed, output_path)
                    pct = len(processed) / total * 100
                    print(f"\n  --- Saved ({pct:.1f}% complete, "
                          f"{len(games):,} games so far) ---\n")

    # Final save
    save_catalog(games, processed, output_path)

    print(f"\n{'=' * 55}")
    print(f"[Done]")
    print(f"  Total apps       : {total:,}")
    print(f"  Games saved      : {len(games):,}")
    print(f"  Failed           : {failed:,}")
    print(f"  Skipped (resume) : {skipped:,}")
    print(f"  Output           : {output_path}")
    print(f"{'=' * 55}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetches full SteamSpy catalog with user tags.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick test (2 pages = ~2,000 apps):
  python3 steamspy_catalog_fetcher.py --max-pages 2

  # Full run (~24h):
  python3 steamspy_catalog_fetcher.py

  # Resume after interruption:
  python3 steamspy_catalog_fetcher.py --resume

  # Custom output:
  python3 steamspy_catalog_fetcher.py --output data/steamspy_catalog.json
        """
    )

    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help=f"Output file (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="Limit request=all pages (1 page = 1,000 apps)"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume a previously interrupted run"
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Parallel threads for appdetails (default: {DEFAULT_WORKERS})"
    )

    args = parser.parse_args()

    fetch_catalog(
        output_path=args.output,
        max_pages=args.max_pages,
        resume=args.resume,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()