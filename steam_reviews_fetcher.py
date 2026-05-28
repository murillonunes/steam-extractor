#!/usr/bin/env python3
"""
Steam Reviews Fetcher - Extracts game reviews from Steam.
"""

import argparse
import logging
import requests
import json
import time
import pandas as pd
from datetime import datetime, date, timezone
from typing import List, Dict

log = logging.getLogger(__name__)

LANGUAGE_MAP = {
    "all": "all",
    "pt-br": "brazilian",
    "pt": "portuguese",
    "en": "english",
    "en-us": "english",
    "es": "spanish",
    "es-la": "latam",
    "fr": "french",
    "de": "german",
    "ja": "japanese",
    "ru": "russian",
    "zh": "schinese",
    "ko": "koreana"
}

BASE_URL = "https://store.steampowered.com/appreviews/"

def map_language(user_lang: str) -> str:
    user_lang = user_lang.lower().strip()
    return LANGUAGE_MAP.get(user_lang, user_lang)

def parse_date(date_str: str) -> date:
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").date()
    except ValueError:
        raise ValueError(f"Invalid date format. Use dd/mm/yyyy (e.g. 30/12/2021)")

def timestamp_to_ddmmyyyy(timestamp: int | str | None) -> str | None:
    if timestamp is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date()
        return dt.strftime("%d/%m/%Y")
    except (ValueError, TypeError, OSError):
        return None

def fetch_reviews_by_date_range(
        appid: str,
        language: str,
        start_date: date,
        end_date: date,
        max_reviews: int = 2000000
    ) -> List[dict]:
    """
    Fetch reviews with pagination and early stop based on date range.

    Retry logic:
    - Connection errors (refused, timeout): wait 60s before retrying
    - Other errors (JSON parse, API error): exponential backoff
    - Max 10 retries per page before giving up
    """
    cursor           = "*"
    collected_reviews = []
    num_per_page     = 100
    seen_cursors     = set()
    max_retries      = 10
    last_progress    = 0

    log.info("Fetching reviews with early date filtering...")

    while len(collected_reviews) < max_reviews:
        page_reviews = None

        for attempt in range(max_retries):
            params = {
                "json": 1,
                "language": language,
                "filter": "recent",
                "review_type": "all",
                "purchase_type": "all",
                "num_per_page": num_per_page,
                "cursor": cursor,
            }

            try:
                response = requests.get(
                    f"{BASE_URL}{appid}",
                    params=params,
                    timeout=30
                )

                # Decode with utf-8-sig to handle BOM responses
                text = response.content.decode("utf-8-sig").strip()

                # Guard against HTML error pages
                if not text.startswith("{"):
                    raise ValueError(f"Non-JSON response (possible rate limit page): {text[:80]}")

                data = json.loads(text)

                if data.get("success") != 1:
                    raise ValueError(f"API error: {data}")

                page_reviews = data.get("reviews", [])
                next_cursor  = data.get("cursor", "*")
                break

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                # Connection-level error: Steam likely rate-limited us
                wait_time = 60
                log.warning(f"Retry {attempt + 1}/{max_retries}: Connection error — waiting {wait_time}s: {e}")
                time.sleep(wait_time)

            except Exception as e:
                # Other errors: exponential backoff
                wait_time = min(2 ** attempt, 60)
                log.warning(f"Retry {attempt + 1}/{max_retries}: {e} (wait {wait_time}s)")
                time.sleep(wait_time)

        else:
            log.error("Max retries exceeded. Stopping pagination.")
            break

        if not page_reviews:
            log.info("No more reviews.")
            break

        page_has_in_range          = False
        page_has_older_than_start  = False

        # Date of oldest review on this page (for progress feedback)
        page_dates = []
        for review in page_reviews:
            ts = review.get("timestamp_created")
            if ts is None:
                continue

            review_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            page_dates.append(review_date)

            if start_date <= review_date <= end_date:
                collected_reviews.append(review)
                page_has_in_range = True
            elif review_date < start_date:
                page_has_older_than_start = True

        oldest_on_page = min(page_dates).strftime("%d/%m/%Y") if page_dates else "?"
        newest_on_page = max(page_dates).strftime("%d/%m/%Y") if page_dates else "?"
        log.info(f"Page: {len(page_reviews)} reviews [{newest_on_page} -> {oldest_on_page}] | in range: {len(collected_reviews):,}")

        milestone = (len(collected_reviews) // 100) * 100
        if milestone > last_progress:
            last_progress = milestone

        if page_has_older_than_start and not page_has_in_range:
            log.info("Reached reviews older than start_date. Stopping early.")
            break

        cursor = next_cursor
        if cursor in seen_cursors:
            log.warning("Cursor loop detected. Stopping pagination.")
            break

        seen_cursors.add(cursor)
        time.sleep(0.5)

    # Deduplicate by unique review ID in case Steam returned the same review
    # multiple times across pages (can happen with filter=recent)
    before = len(collected_reviews)
    collected_reviews = list({r["recommendationid"]: r for r in collected_reviews}.values())
    dupes = before - len(collected_reviews)
    if dupes:
        log.info(f"Removed {dupes} duplicate reviews (recommendationid)")

    return collected_reviews

def save_reviews(reviews: List[Dict], filename: str, format_type: str = 'json'):
    processed_reviews = []
    for r in reviews:
        processed = r.copy()
        processed['date_created'] = timestamp_to_ddmmyyyy(r.get('timestamp_created'))
        processed['date_updated'] = timestamp_to_ddmmyyyy(r.get('timestamp_updated'))
        processed['date_release'] = timestamp_to_ddmmyyyy(r.get('app_release_date'))
        author = processed.setdefault('author', {})
        author['last_played_date'] = timestamp_to_ddmmyyyy(author.get('last_played'))
        processed_reviews.append(processed)

    if format_type == 'csv':
        df = pd.json_normalize(processed_reviews)
        output_file = f"{filename}.csv"
        df.to_csv(output_file, index=False, encoding="utf-8")
        log.info(f"Saved CSV: {output_file} ({len(df)} rows, {len(df.columns)} columns)")
    else:
        output_file = f"{filename}.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(processed_reviews, f, ensure_ascii=False, indent=2)
        log.info(f"Saved JSON: {output_file} ({len(processed_reviews)} reviews)")

def filter_reviews_by_date(reviews: List[Dict], start_date: date, end_date: date) -> List[Dict]:
    filtered = []
    for review in reviews:
        ts = review.get('timestamp_created')
        if ts is None:
            continue
        review_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if start_date <= review_date <= end_date:
            filtered.append(review)
    return filtered

def main():
    parser = argparse.ArgumentParser(
        description="Steam Reviews Fetcher - Extracts game reviews from Steam.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 1091500 pt-br 01/01/2021 31/12/2021
  %(prog)s 730 all 01/01/2024 31/12/2024
        """
    )

    parser.add_argument("appid",      help="Game ID (e.g., 1091500 for Cyberpunk 2077)")
    parser.add_argument("language",   help="Language (e.g., pt-br, en-us, all)")
    parser.add_argument("start_date", help="Start date (dd/mm/yyyy)")
    parser.add_argument("end_date",   help="End date (dd/mm/yyyy)")
    parser.add_argument("--format", "-f", choices=["json", "csv"], default="json")

    args = parser.parse_args()
    steam_lang = map_language(args.language)

    try:
        start_date = parse_date(args.start_date)
        end_date   = parse_date(args.end_date)
        if start_date > end_date:
            raise ValueError("start_date must be before or equal to end_date")
    except ValueError as e:
        log.error(f"Error: {e}")
        return 1

    log.info("Steam Reviews Fetcher v2.0.0")
    log.info(f"Game ID   : {args.appid}")
    log.info(f"Language  : {steam_lang}")
    log.info(f"Date range: {args.start_date} to {args.end_date}")

    try:
        reviews = fetch_reviews_by_date_range(
            args.appid, steam_lang, start_date, end_date
        )

        log.info(f"Pagination complete! {len(reviews):,} reviews collected")

        if reviews:
            safe_start  = args.start_date.replace("/", "-")
            safe_end    = args.end_date.replace("/", "-")
            output_name = f"reviews_{args.appid}_{steam_lang}_{safe_start}_{safe_end}"
            save_reviews(reviews, output_name, args.format)
            log.info(f"Complete! {len(reviews):,} reviews saved")
        else:
            log.info("No reviews found in date range.")

    except Exception as e:
        log.error(f"Fetch error: {e}")
        return 1

    return 0

if __name__ == "__main__":
    main()