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

def _paginate_once(
    appid: str,
    language: str,
    start_date: date,
    end_date: date,
    max_reviews: int,
    pass_label: str = "Pass 1",
) -> tuple[list[dict], bool]:
    """
    Single cursor-based pagination pass (filter=recent, newest → oldest).

    Returns (collected_reviews, gap_detected).
    gap_detected=True means at least one between-page date jump exceeded 5× the
    running average days-per-page, which is the fingerprint of cursor drift:
    new reviews were inserted mid-scrape, shifting cursor positions and causing
    one or more pages to be silently skipped.

    Retry logic:
    - Connection errors (refused, timeout): wait 60s before retrying
    - Other errors (JSON parse, API error): exponential backoff
    - Max 10 retries per page before giving up
    """
    cursor = "*"
    collected: list[dict] = []
    num_per_page = 100
    seen_cursors: set[str] = set()
    max_retries = 10

    prev_page_oldest: date | None = None
    total_days_covered: int = 0
    total_pages: int = 0
    gap_detected = False

    log.info(f"[{pass_label}] Starting pagination (filter=recent, newest → oldest)...")

    while len(collected) < max_reviews:
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

                text = response.content.decode("utf-8-sig").strip()

                if not text.startswith("{"):
                    raise ValueError(f"Non-JSON response (possible rate limit): {text[:80]}")

                data = json.loads(text)

                if data.get("success") != 1:
                    raise ValueError(f"API error: {data}")

                page_reviews = data.get("reviews", [])
                next_cursor  = data.get("cursor", "*")
                break

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                log.warning(f"[{pass_label}] Retry {attempt+1}/{max_retries}: connection error, waiting 60s: {e}")
                time.sleep(60)

            except Exception as e:
                wait_time = min(2 ** attempt, 60)
                log.warning(f"[{pass_label}] Retry {attempt+1}/{max_retries}: {e} (wait {wait_time}s)")
                time.sleep(wait_time)

        else:
            log.error(f"[{pass_label}] Max retries exceeded. Stopping.")
            break

        if not page_reviews:
            if prev_page_oldest is not None and prev_page_oldest > start_date:
                days_short = (prev_page_oldest - start_date).days
                log.warning(
                    f"[{pass_label}] API returned no more reviews at "
                    f"{prev_page_oldest.strftime('%d/%m/%Y')} — {days_short} day(s) short of "
                    f"start_date ({start_date.strftime('%d/%m/%Y')}). "
                    f"Likely session throttling — result is incomplete for this pass."
                )
                gap_detected = True
            else:
                log.info(f"[{pass_label}] No more reviews.")
            break

        page_has_in_range = False
        page_has_older_than_start = False
        page_dates: list[date] = []

        for review in page_reviews:
            ts = review.get("timestamp_created")
            if ts is None:
                continue
            review_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            page_dates.append(review_date)
            if start_date <= review_date <= end_date:
                collected.append(review)
                page_has_in_range = True
            elif review_date < start_date:
                page_has_older_than_start = True

        if page_dates:
            page_oldest = min(page_dates)
            page_newest = max(page_dates)
            page_span   = max((page_newest - page_oldest).days, 0)

            # Cursor-drift detection: compare this page's newest date with the
            # previous page's oldest date. Under normal pagination they should
            # be consecutive; a large jump means pages were skipped.
            if prev_page_oldest is not None:
                jump_days = (prev_page_oldest - page_newest).days
                avg_days_per_page = total_days_covered / total_pages if total_pages > 0 else 1
                drift_threshold = max(avg_days_per_page * 5, 1)
                if jump_days > drift_threshold:
                    log.warning(
                        f"[{pass_label}] Cursor drift detected: {jump_days:.0f}-day gap between pages "
                        f"(expected ≤{drift_threshold:.0f} d based on avg {avg_days_per_page:.1f} d/page). "
                        f"Reviews in this window may be missing — consider --passes 3."
                    )
                    gap_detected = True

            total_days_covered += page_span
            total_pages += 1
            prev_page_oldest = page_oldest

            log.info(
                f"[{pass_label}] Page {total_pages}: {len(page_reviews)} reviews "
                f"[{page_newest.strftime('%d/%m/%Y')} → {page_oldest.strftime('%d/%m/%Y')}] "
                f"| in range: {len(collected):,}"
            )

        if page_has_older_than_start and not page_has_in_range:
            log.info(f"[{pass_label}] All reviews on this page are before start_date. Stopping.")
            break

        cursor = next_cursor
        if cursor in seen_cursors:
            log.warning(f"[{pass_label}] Cursor loop detected. Stopping.")
            break
        seen_cursors.add(cursor)
        time.sleep(0.5)

    log.info(f"[{pass_label}] Done: {len(collected):,} reviews, gap_detected={gap_detected}")
    return collected, gap_detected


def fetch_reviews_by_date_range(
    appid: str,
    language: str,
    start_date: date,
    end_date: date,
    max_reviews: int = 2_000_000,
    passes: int = 1,
) -> list[dict]:
    """
    Fetch reviews with multi-pass deduplication to recover reviews missed by cursor drift.

    Steam's filter=recent paginates a live stream. When new reviews are posted while
    scraping, cursor positions shift and entire pages get silently skipped — this is
    why identical runs can yield wildly different counts (e.g., 11k vs 64k).

    Running multiple passes and merging via recommendationid deduplication recovers
    the skipped reviews, since drift shifts differently in each independent pass.

    passes=1  : single pass, original behaviour (fast but potentially incomplete)
    passes=3+ : recommended for high-volume games or when consistent counts matter
    """
    merged: dict[str, dict] = {}  # recommendationid → review

    for i in range(passes):
        label = f"Pass {i+1}/{passes}"
        pass_reviews, gap_detected = _paginate_once(
            appid, language, start_date, end_date, max_reviews, pass_label=label
        )

        prev_count  = len(merged)
        for r in pass_reviews:
            merged[r["recommendationid"]] = r
        new_unique = len(merged) - prev_count

        log.info(
            f"[{label}] Merged: {len(pass_reviews):,} reviews this pass, "
            f"{new_unique:,} new unique (running total: {len(merged):,})"
        )

        if i < passes - 1:
            if gap_detected:
                log.info(f"[{label}] Drift detected — running next pass to recover missing reviews.")
            else:
                log.info(f"[{label}] No drift detected — stopping early at pass {i+1}.")
                break

    result = list(merged.values())
    log.info(f"All passes complete: {len(result):,} unique reviews")
    return result

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
    parser.add_argument(
        "--passes", "-p", type=int, default=1, metavar="N",
        help=(
            "Number of independent collection passes (default: 1). "
            "Each pass restarts from cursor=* and results are merged via deduplication. "
            "Use 3+ for high-volume games to compensate for cursor drift."
        )
    )

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
            args.appid, steam_lang, start_date, end_date, passes=args.passes
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