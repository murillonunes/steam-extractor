#!/usr/bin/env python3
"""
Steam Reviews Fetcher - Extracts game reviews from Steam.
"""

import argparse
import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests

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


@dataclass
class PaginationResult:
    """Outcome and diagnostics for one cursor-based pagination pass."""

    reviews: list[dict]
    complete: bool
    reason: str
    pages: int
    scanned_reviews: int
    expected_reviews: int | None
    oldest_date: date | None
    drift_detected: bool

    def to_metadata(self) -> dict:
        return {
            "complete": self.complete,
            "reason": self.reason,
            "pages": self.pages,
            "scanned_reviews": self.scanned_reviews,
            "expected_reviews": self.expected_reviews,
            "reviews_in_date_range": len(self.reviews),
            "oldest_review": self.oldest_date.isoformat() if self.oldest_date else None,
            "drift_detected": self.drift_detected,
        }


@dataclass
class ReviewFetchResult:
    """Merged reviews and diagnostics from all pagination passes."""

    reviews: list[dict]
    passes: list[PaginationResult]

    @property
    def complete(self) -> bool:
        return any(result.complete and not result.drift_detected for result in self.passes)

    def to_metadata(self) -> dict:
        last_pass = self.passes[-1] if self.passes else None
        return {
            "complete": self.complete,
            "completion_reason": last_pass.reason if last_pass else "no_passes",
            "passes_executed": len(self.passes),
            "unique_reviews_in_date_range": len(self.reviews),
            "drift_detected": any(result.drift_detected for result in self.passes),
            "passes": [result.to_metadata() for result in self.passes],
        }


def software_metadata() -> dict:
    """Returns best-effort package and source-control provenance."""
    try:
        package_version = version("steam-extractor")
    except PackageNotFoundError:
        package_version = "unknown"

    repository_root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        git_commit = result.stdout.strip() or None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        git_dirty = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        git_commit = None
        git_dirty = None

    return {
        "package_version": package_version,
        "git_commit": git_commit,
        "git_dirty": git_dirty,
    }


def save_manifest(filename: str, metadata: dict) -> str:
    """Atomically writes a JSON metadata sidecar and returns its path."""
    path = Path(f"{filename}.metadata.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as file:
            temp_name = file.name
            json.dump(metadata, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return str(path)


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
) -> PaginationResult:
    """
    Single cursor-based pagination pass (filter=recent, newest → oldest).

    Returns reviews together with completeness diagnostics. drift_detected=True
    means at least one between-page date jump exceeded 5× the
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
    scanned_ids: set[str] = set()
    expected_reviews: int | None = None
    last_page_size: int | None = None
    complete = False
    reason = "unknown"

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

                if expected_reviews is None:
                    reported_total = data.get("query_summary", {}).get("total_reviews")
                    try:
                        expected_reviews = int(reported_total)
                    except (TypeError, ValueError):
                        expected_reviews = None

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
            reason = "max_retries_exceeded"
            break

        if not page_reviews:
            reached_reported_total = (
                expected_reviews is not None and len(scanned_ids) >= expected_reviews
            )
            ended_with_partial_page = (
                expected_reviews is None
                and last_page_size is not None
                and last_page_size < num_per_page
            )

            if reached_reported_total or ended_with_partial_page:
                complete = True
                reason = "end_of_history"
                date_note = (
                    " The requested start_date predates the first available review."
                    if prev_page_oldest is not None and prev_page_oldest > start_date
                    else ""
                )
                log.info(
                    f"[{pass_label}] Review history ended normally at "
                    f"{prev_page_oldest.strftime('%d/%m/%Y') if prev_page_oldest else 'unknown date'}."
                    f"{date_note}"
                )
            elif prev_page_oldest is not None and prev_page_oldest <= start_date:
                complete = True
                reason = "date_range_complete"
                log.info(f"[{pass_label}] Requested date range is complete.")
            else:
                reason = "premature_empty_response"
                missing = (
                    f"; scanned {len(scanned_ids):,} of approximately "
                    f"{expected_reviews:,} reviews"
                    if expected_reviews is not None
                    else ""
                )
                log.warning(
                    f"[{pass_label}] API returned an unexpected empty page before the "
                    f"requested range was completed{missing}. Result is incomplete."
                )
            break

        last_page_size = len(page_reviews)

        page_has_in_range = False
        page_has_older_than_start = False
        page_dates: list[date] = []

        for review in page_reviews:
            recommendation_id = review.get("recommendationid")
            if recommendation_id is not None:
                scanned_ids.add(str(recommendation_id))
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
            complete = True
            reason = "start_date_reached"
            break

        cursor = next_cursor
        if cursor in seen_cursors:
            log.warning(f"[{pass_label}] Cursor loop detected. Stopping.")
            reason = "cursor_loop"
            break
        seen_cursors.add(cursor)
        time.sleep(0.5)

    if reason == "unknown":
        reason = "max_reviews_reached"

    log.info(
        f"[{pass_label}] Done: {len(collected):,} reviews, "
        f"scanned={len(scanned_ids):,}, expected={expected_reviews}, "
        f"complete={complete}, reason={reason}, drift_detected={gap_detected}"
    )
    return PaginationResult(
        reviews=collected,
        complete=complete,
        reason=reason,
        pages=total_pages,
        scanned_reviews=len(scanned_ids),
        expected_reviews=expected_reviews,
        oldest_date=prev_page_oldest,
        drift_detected=gap_detected,
    )


def fetch_reviews_with_metadata(
    appid: str,
    language: str,
    start_date: date,
    end_date: date,
    max_reviews: int = 2_000_000,
    passes: int = 1,
) -> ReviewFetchResult:
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
    pass_results: list[PaginationResult] = []

    for i in range(passes):
        label = f"Pass {i+1}/{passes}"
        pagination = _paginate_once(
            appid, language, start_date, end_date, max_reviews, pass_label=label
        )
        pass_results.append(pagination)
        pass_reviews = pagination.reviews

        prev_count  = len(merged)
        for r in pass_reviews:
            merged[r["recommendationid"]] = r
        new_unique = len(merged) - prev_count

        log.info(
            f"[{label}] Merged: {len(pass_reviews):,} reviews this pass, "
            f"{new_unique:,} new unique (running total: {len(merged):,})"
        )

        if i < passes - 1:
            if pagination.drift_detected or not pagination.complete:
                log.info(
                    f"[{label}] Incomplete pass or drift detected — running next pass "
                    f"to recover missing reviews."
                )
            else:
                log.info(f"[{label}] No drift detected — stopping early at pass {i+1}.")
                break

    result = list(merged.values())
    log.info(f"All passes complete: {len(result):,} unique reviews")
    return ReviewFetchResult(reviews=result, passes=pass_results)


def fetch_reviews_by_date_range(
    appid: str,
    language: str,
    start_date: date,
    end_date: date,
    max_reviews: int = 2_000_000,
    passes: int = 1,
) -> list[dict]:
    """Backward-compatible wrapper returning only the merged reviews."""
    return fetch_reviews_with_metadata(
        appid=appid,
        language=language,
        start_date=start_date,
        end_date=end_date,
        max_reviews=max_reviews,
        passes=passes,
    ).reviews


def save_reviews(
    reviews: List[Dict],
    filename: str,
    format_type: str = 'json',
    metadata: dict | None = None,
):
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

    if metadata is not None:
        manifest_file = save_manifest(filename, metadata)
        log.info(f"Saved metadata: {manifest_file}")

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
        extraction = fetch_reviews_with_metadata(
            args.appid, steam_lang, start_date, end_date, passes=args.passes
        )
        reviews = extraction.reviews

        log.info(f"Pagination complete! {len(reviews):,} reviews collected")

        safe_start  = args.start_date.replace("/", "-")
        safe_end    = args.end_date.replace("/", "-")
        output_name = f"reviews_{args.appid}_{steam_lang}_{safe_start}_{safe_end}"
        metadata = {
            "schema_version": 1,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "software": software_metadata(),
            "query": {
                "appid": str(args.appid),
                "language": steam_lang,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "passes_requested": args.passes,
                "steam_filter": "recent",
                "offtopic_included": False,
            },
            "pagination": extraction.to_metadata(),
            "output": {
                "rows": len(reviews),
                "format": args.format,
                "file": f"{output_name}.{args.format}" if reviews else None,
            },
            "dataset_complete": extraction.complete,
        }

        if reviews:
            save_reviews(reviews, output_name, args.format, metadata=metadata)
            log.info(f"Complete! {len(reviews):,} reviews saved")
        else:
            log.info("No reviews found in date range.")
            manifest_file = save_manifest(output_name, metadata)
            log.info(f"Saved metadata: {manifest_file}")

    except Exception as e:
        log.error(f"Fetch error: {e}")
        return 1

    return 0

if __name__ == "__main__":
    main()
