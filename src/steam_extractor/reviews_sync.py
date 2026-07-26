"""Incremental Steam review synchronization CLI."""

from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

import requests

from steam_extractor.review_store import ReviewStore, expected_total_reached
from steam_extractor.reviews_fetcher import BASE_URL, map_language, parse_date


log = logging.getLogger(__name__)
DEFAULT_DATABASE = "steam_reviews.sqlite"
EMPTY_PAGE_CONFIRMATIONS = 3
SUPPORTED_REVIEW_LANGUAGES = (
    "arabic",
    "bulgarian",
    "schinese",
    "tchinese",
    "czech",
    "danish",
    "dutch",
    "english",
    "finnish",
    "french",
    "german",
    "greek",
    "hungarian",
    "indonesian",
    "italian",
    "japanese",
    "koreana",
    "malay",
    "norwegian",
    "polish",
    "portuguese",
    "brazilian",
    "romanian",
    "russian",
    "spanish",
    "latam",
    "swedish",
    "thai",
    "turkish",
    "ukrainian",
    "vietnamese",
)


@dataclass
class SyncResult:
    run_id: str
    status: str
    reason: str
    pages: int
    received: int
    inserted: int
    unchanged: int
    updated: int
    duplicates: int
    resumed: bool
    coverage_verified: bool
    operational_complete: bool
    research_verified: bool
    verification_pass: bool
    expected_reviews: int | None
    cumulative_received: int


def _request_page(
    app_id: str,
    language: str,
    filter_type: str,
    cursor: str,
    max_retries: int = 5,
) -> dict:
    params = {
        "json": 1,
        "language": language,
        "filter": filter_type,
        "review_type": "all",
        "purchase_type": "all",
        "filter_offtopic_activity": 1,
        "num_per_page": 100,
        "cursor": cursor,
    }
    for attempt in range(max_retries):
        try:
            response = requests.get(f"{BASE_URL}{app_id}", params=params, timeout=30)
            response.raise_for_status()
            text = response.content.decode("utf-8-sig").strip()
            if not text.startswith("{"):
                raise ValueError(f"Non-JSON Steam response: {text[:80]}")
            data = json.loads(text)
            if data.get("success") != 1:
                raise ValueError(f"Steam API error: {data}")
            return data
        except (requests.RequestException, ValueError) as error:
            if attempt == max_retries - 1:
                raise RuntimeError(
                    f"Failed to fetch {filter_type} page after {max_retries} attempts"
                ) from error
            wait = min(2 ** attempt, 30)
            log.warning(
                "Retry %s/%s for %s page in %ss: %s",
                attempt + 1,
                max_retries,
                filter_type,
                wait,
                error,
            )
            time.sleep(wait)

    raise AssertionError("unreachable")


def _reported_total(data: dict) -> int | None:
    reported = data.get("query_summary", {}).get("total_reviews")
    try:
        return int(reported)
    except (TypeError, ValueError):
        return None


def sync_filter(
    store: ReviewStore,
    *,
    app_id: str,
    language: str,
    filter_type: str,
    start_date: date | None = None,
    resume: bool = False,
    max_pages: int | None = None,
    max_runtime: float | None = None,
    overlap_pages: int = 3,
    allow_unverified_overlap: bool = False,
    verification_pass: bool = False,
) -> SyncResult:
    """Synchronizes one Steam ordering with transactional page checkpoints."""
    run_id = uuid.uuid4().hex
    existing_job = store.get_job(app_id, language, filter_type)
    existing_coverage = store.get_coverage(app_id, language, filter_type)
    if filter_type == "updated":
        incremental_overlap_allowed = True
    elif allow_unverified_overlap:
        incremental_overlap_allowed = True
    elif existing_coverage is None:
        incremental_overlap_allowed = False
    elif existing_coverage["complete_history"]:
        incremental_overlap_allowed = True
    elif start_date is not None and existing_coverage["oldest_timestamp"] is not None:
        requested_start = int(
            datetime(
                start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc
            ).timestamp()
        )
        incremental_overlap_allowed = existing_coverage["oldest_timestamp"] <= requested_start
    else:
        incremental_overlap_allowed = False
    resumed = bool(resume and existing_job and existing_job["status"] in {"paused", "failed"})
    cursor = existing_job["cursor"] if resumed else "*"
    page_number = int(existing_job["pages"]) if resumed else 0
    total_received = int(existing_job["received"]) if resumed else 0
    expected_reviews = existing_job.get("expected_reviews") if resumed else None
    seen_cursors = {cursor}
    consecutive_overlap = 0
    started = time.monotonic()
    totals = {"inserted": 0, "unchanged": 0, "updated": 0, "duplicates": 0}
    status = "running"
    reason = "unknown"
    complete_history = False

    store.start_run(
        run_id,
        app_id,
        language,
        filter_type,
        verification_pass=verification_pass,
    )
    log.info(
        "Sync %s app=%s language=%s filter=%s cursor=%s",
        "resume" if resumed else "start",
        app_id,
        language,
        filter_type,
        cursor[:30],
    )

    try:
        while True:
            run_pages = page_number - (int(existing_job["pages"]) if resumed else 0)
            if max_pages is not None and run_pages >= max_pages:
                status, reason = "paused", "max_pages_reached"
                break
            if max_runtime is not None and time.monotonic() - started >= max_runtime:
                status, reason = "paused", "max_runtime_reached"
                break

            data = _request_page(app_id, language, filter_type, cursor)
            reviews = data.get("reviews") or []
            if expected_reviews is None:
                expected_reviews = _reported_total(data)

            if not reviews and not expected_total_reached(
                total_received, expected_reviews
            ):
                for confirmation in range(2, EMPTY_PAGE_CONFIRMATIONS + 1):
                    wait = min(2 ** (confirmation - 2), 5)
                    log.warning(
                        "Unexpected empty page at cursor; confirming %s/%s in %ss "
                        "(received=%s expected=%s)",
                        confirmation,
                        EMPTY_PAGE_CONFIRMATIONS,
                        wait,
                        total_received,
                        expected_reviews,
                    )
                    time.sleep(wait)
                    candidate = _request_page(
                        app_id, language, filter_type, cursor
                    )
                    if expected_reviews is None:
                        expected_reviews = _reported_total(candidate)
                    if candidate.get("reviews"):
                        data = candidate
                        reviews = candidate["reviews"]
                        log.info(
                            "Empty page was transient; synchronization continues"
                        )
                        break
                    if expected_total_reached(total_received, expected_reviews):
                        break

            if not reviews:
                if expected_total_reached(total_received, expected_reviews):
                    status, reason = "complete", "end_of_history"
                    complete_history = filter_type == "recent"
                else:
                    status = "incomplete"
                    reason = (
                        "api_exhausted_before_expected_total"
                        if expected_reviews is not None
                        else "api_exhausted_without_expected_total"
                    )
                    log.warning(
                        "API stream ended before completion: received=%s expected=%s",
                        total_received,
                        expected_reviews,
                    )
                break

            page_ids = [
                str(review["recommendationid"])
                for review in reviews
                if review.get("recommendationid") is not None
            ]
            known_before = len(store.known_review_ids(page_ids))
            next_cursor = data.get("cursor") or "*"
            page_number += 1
            total_received += len(reviews)
            counts = store.save_page(
                run_id=run_id,
                app_id=app_id,
                language=language,
                filter_type=filter_type,
                reviews=reviews,
                next_cursor=next_cursor,
                expected_reviews=expected_reviews,
                page_number=page_number,
                total_received=total_received,
            )
            for key in totals:
                totals[key] += counts[key]

            timestamps = [
                int(review["timestamp_created"])
                for review in reviews
                if review.get("timestamp_created") is not None
            ]
            oldest_date = (
                datetime.fromtimestamp(min(timestamps), tz=timezone.utc).date()
                if timestamps
                else None
            )
            log.info(
                "Page %s: received=%s new=%s changed=%s known=%s oldest=%s",
                page_number,
                len(reviews),
                counts["inserted"],
                counts["updated"],
                known_before,
                oldest_date,
            )

            if start_date is not None and oldest_date is not None and oldest_date < start_date:
                status, reason = "complete", "start_date_reached"
                break

            if (
                not verification_pass
                and not resumed
                and incremental_overlap_allowed
                and known_before == len(reviews)
            ):
                consecutive_overlap += 1
            else:
                consecutive_overlap = 0
            if not resumed and consecutive_overlap >= overlap_pages:
                status, reason = "complete", "known_overlap_reached"
                break

            if next_cursor in seen_cursors:
                status, reason = "failed", "cursor_loop"
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            time.sleep(0.5)
    except KeyboardInterrupt:
        status, reason = "paused", "user_interrupted"
        log.warning("Synchronization interrupted by user; checkpoint preserved for --resume")
    except Exception:
        status, reason = "failed", "request_error"
        store.finish_run(
            run_id,
            app_id,
            language,
            filter_type,
            status=status,
            reason=reason,
            expected_reviews=expected_reviews,
            cumulative_received=total_received,
        )
        raise

    operational_complete = status == "complete"
    expected_total_verified = (
        reason == "start_date_reached"
        or (
            reason == "end_of_history"
            and expected_total_reached(total_received, expected_reviews)
        )
    )
    inherited_verified_coverage = bool(
        existing_coverage
        and reason == "known_overlap_reached"
        and operational_complete
    )
    research_verified = (
        filter_type == "recent"
        and (
            inherited_verified_coverage
            or (
                not resumed
                and operational_complete
                and expected_total_verified
                and (not verification_pass or totals["inserted"] == 0)
            )
        )
    )
    complete_history = (
        filter_type == "recent"
        and reason == "end_of_history"
        and research_verified
    )
    coverage_verified = (
        not resumed
        and not allow_unverified_overlap
        and (not verification_pass or research_verified)
        and reason
        in {
            "end_of_history",
            "start_date_reached",
            "known_overlap_reached",
            "api_exhausted_before_expected_total",
            "api_exhausted_without_expected_total",
        }
    )
    store.finish_run(
        run_id,
        app_id,
        language,
        filter_type,
        status=status,
        reason=reason,
        complete_history=complete_history,
        verified_coverage=coverage_verified,
        operational_complete=operational_complete,
        research_verified=research_verified,
        expected_reviews=expected_reviews,
        cumulative_received=total_received,
    )
    return SyncResult(
        run_id=run_id,
        status=status,
        reason=reason,
        pages=page_number - (int(existing_job["pages"]) if resumed else 0),
        received=sum(totals.values()),
        resumed=resumed,
        coverage_verified=coverage_verified,
        operational_complete=operational_complete,
        research_verified=research_verified,
        verification_pass=verification_pass,
        expected_reviews=expected_reviews,
        cumulative_received=total_received,
        **totals,
    )


def sync_reviews(
    database: str,
    app_id: str,
    language: str = "all",
    start_date: date | None = None,
    resume: bool = False,
    max_pages: int | None = None,
    max_runtime: float | None = None,
    overlap_pages: int = 3,
    sync_updates: bool = True,
    verification_passes: int = 3,
) -> dict:
    """Synchronizes reviews and verifies recovery runs with independent full passes."""
    command_started = time.monotonic()

    def remaining_runtime() -> float | None:
        if max_runtime is None:
            return None
        return max(max_runtime - (time.monotonic() - command_started), 0.0)

    with ReviewStore(database) as store:
        pages_used = 0
        had_reviews = store.count_reviews(app_id, language) > 0
        previous_job = store.get_job(str(app_id), language, "recent")
        previous_run = store.get_latest_run(str(app_id), language, "recent")
        recovery_required = bool(
            (
                previous_job
                and previous_job["status"] in {"paused", "failed", "incomplete"}
            )
            or (
                previous_run
                and previous_run["operational_complete"]
                and not previous_run["research_verified"]
            )
        )
        cursor_resume_expected = bool(
            resume
            and previous_job
            and previous_job["status"] in {"paused", "failed"}
        )
        recent = sync_filter(
            store,
            app_id=str(app_id),
            language=language,
            filter_type="recent",
            start_date=start_date,
            resume=resume,
            max_pages=max_pages,
            max_runtime=remaining_runtime(),
            overlap_pages=overlap_pages,
            verification_pass=recovery_required and not cursor_resume_expected,
        )
        pages_used += recent.pages
        verification_results: list[SyncResult] = []
        recovery_requires_verification = recovery_required
        while (
            recovery_requires_verification
            and recent.operational_complete
            and not (
                recent.research_verified
                or any(result.research_verified for result in verification_results)
            )
            and len(verification_results) < verification_passes
        ):
            remaining = remaining_runtime()
            remaining_pages = None if max_pages is None else max(max_pages - pages_used, 0)
            if (
                (remaining is not None and remaining <= 0)
                or (remaining_pages is not None and remaining_pages <= 0)
            ):
                break
            pass_number = len(verification_results) + 1
            log.info(
                "Research verification pass %s/%s from the live head",
                pass_number,
                verification_passes,
            )
            verification = sync_filter(
                store,
                app_id=str(app_id),
                language=language,
                filter_type="recent",
                start_date=start_date,
                resume=False,
                max_pages=remaining_pages,
                max_runtime=remaining,
                overlap_pages=overlap_pages,
                verification_pass=True,
            )
            verification_results.append(verification)
            pages_used += verification.pages
            if not verification.operational_complete:
                break
        research_verified = recent.research_verified or any(
            result.research_verified for result in verification_results
        )
        operational_complete = recent.operational_complete
        reconciliation = verification_results[0] if verification_results else None
        updated = None
        remaining = remaining_runtime()
        remaining_pages = None if max_pages is None else max(max_pages - pages_used, 0)
        if (
            sync_updates
            and had_reviews
            and recent.reason == "known_overlap_reached"
            and (not recovery_requires_verification or research_verified)
            and (remaining is None or remaining > 0)
            and (remaining_pages is None or remaining_pages > 0)
        ):
            updated = sync_filter(
                store,
                app_id=str(app_id),
                language=language,
                filter_type="updated",
                overlap_pages=overlap_pages,
                max_pages=remaining_pages,
                max_runtime=remaining,
            )
        return {
            "database": database,
            "app_id": str(app_id),
            "language": language,
            "operational_complete": operational_complete,
            "research_verified": research_verified,
            "recent": recent.__dict__,
            "reconciliation": reconciliation.__dict__ if reconciliation else None,
            "verification_passes": [
                result.__dict__ for result in verification_results
            ],
            "updated": updated.__dict__ if updated else None,
            "stored_reviews": store.count_reviews(app_id),
        }


def _pages_used(result: dict) -> int:
    phases = [
        result["recent"],
        *result["verification_passes"],
        result["updated"],
    ]
    return sum(phase["pages"] for phase in phases if phase)


def _result_phases(result: dict) -> list[dict | None]:
    if result.get("partition_strategy") == "language":
        return [
            phase
            for partition in result["partitions"]
            for phase in _result_phases(partition)
        ]
    return [
        result["recent"],
        *result["verification_passes"],
        result["updated"],
    ]


def sync_partitioned_reviews(
    database: str,
    app_id: str,
    start_date: date | None = None,
    resume: bool = False,
    max_pages: int | None = None,
    max_runtime: float | None = None,
    overlap_pages: int = 3,
    sync_updates: bool = True,
    verification_passes: int = 3,
) -> dict:
    """Synchronizes every supported review language as an independent cursor stream."""
    command_started = time.monotonic()
    pages_used = 0
    partitions: list[dict] = []

    for language in SUPPORTED_REVIEW_LANGUAGES:
        remaining_runtime = (
            None
            if max_runtime is None
            else max(max_runtime - (time.monotonic() - command_started), 0.0)
        )
        remaining_pages = (
            None if max_pages is None else max(max_pages - pages_used, 0)
        )
        if (
            (remaining_runtime is not None and remaining_runtime <= 0)
            or (remaining_pages is not None and remaining_pages <= 0)
        ):
            break
        log.info(
            "Language partition %s/%s: %s",
            len(partitions) + 1,
            len(SUPPORTED_REVIEW_LANGUAGES),
            language,
        )
        result = sync_reviews(
            database=database,
            app_id=app_id,
            language=language,
            start_date=start_date,
            resume=resume,
            max_pages=remaining_pages,
            max_runtime=remaining_runtime,
            overlap_pages=overlap_pages,
            sync_updates=sync_updates,
            verification_passes=verification_passes,
        )
        partitions.append(result)
        pages_used += _pages_used(result)
        if any(
            phase and phase["reason"] == "user_interrupted"
            for phase in _result_phases(result)
        ):
            break

    with ReviewStore(database) as store:
        completed_languages = {
            language
            for language in SUPPORTED_REVIEW_LANGUAGES
            if (
                (latest := store.get_latest_run(str(app_id), language, "recent"))
                and latest["research_verified"]
            )
        }
    all_verified = completed_languages == set(SUPPORTED_REVIEW_LANGUAGES)
    if all_verified:
        with ReviewStore(database) as store:
            store.set_partitioned_coverage(
                str(app_id),
                SUPPORTED_REVIEW_LANGUAGES,
                filter_type="recent",
            )

    expected_totals = [
        result["recent"]["expected_reviews"]
        for result in partitions
        if result["recent"]["expected_reviews"] is not None
    ]
    with ReviewStore(database) as store:
        stored_reviews = store.count_reviews(app_id)
    return {
        "database": database,
        "app_id": str(app_id),
        "language": "all",
        "partition_strategy": "language",
        "offtopic_reviews_included": False,
        "languages_total": len(SUPPORTED_REVIEW_LANGUAGES),
        "languages_attempted": len(partitions),
        "languages_verified": len(completed_languages),
        "pending_languages": [
            language
            for language in SUPPORTED_REVIEW_LANGUAGES
            if language not in completed_languages
        ],
        "operational_complete": all_verified
        or (
            len(partitions) == len(SUPPORTED_REVIEW_LANGUAGES)
            and all(result["operational_complete"] for result in partitions)
        ),
        "research_verified": all_verified,
        "expected_reviews_sum": sum(expected_totals),
        "partitions": partitions,
        "stored_reviews": stored_reviews,
    }


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Incrementally synchronize Steam reviews into a local SQLite archive."
    )
    parser.add_argument("appid", help="Steam application ID")
    parser.add_argument(
        "--language",
        default="all",
        help="Steam review language; 'all' uses independent language partitions",
    )
    parser.add_argument("--start", help="Stop after reaching this date (dd/mm/yyyy)")
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="SQLite database path")
    parser.add_argument("--resume", action="store_true", help="Resume a paused checkpoint")
    parser.add_argument("--max-pages", type=positive_int, default=None)
    parser.add_argument("--max-runtime", type=positive_float, default=None, metavar="SECONDS")
    parser.add_argument("--overlap-pages", type=positive_int, default=3)
    parser.add_argument(
        "--verification-passes",
        type=positive_int,
        default=3,
        help="Maximum full passes used to verify a resumed/incomplete collection (default: 3)",
    )
    parser.add_argument("--no-sync-updates", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%d/%m/%Y %H:%M:%S",
    )
    start_date = parse_date(args.start) if args.start else None
    language = map_language(args.language)
    common_arguments = {
        "database": args.database,
        "app_id": args.appid,
        "start_date": start_date,
        "resume": args.resume,
        "max_pages": args.max_pages,
        "max_runtime": args.max_runtime,
        "overlap_pages": args.overlap_pages,
        "sync_updates": not args.no_sync_updates,
        "verification_passes": args.verification_passes,
    }
    if language == "all":
        result = sync_partitioned_reviews(**common_arguments)
    else:
        result = sync_reviews(language=language, **common_arguments)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    phases = _result_phases(result)
    if any(phase and phase["reason"] == "user_interrupted" for phase in phases):
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
