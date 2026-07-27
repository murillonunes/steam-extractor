"""Dedicated CLI for resumable Steam player-country enrichment."""

from __future__ import annotations

import argparse
import json
import logging
import os

from steam_extractor.review_store import ReviewStore
from steam_extractor.reviews_fetcher import parse_date
from steam_extractor.tag_extractor import fetch_country_codes_with_metadata


log = logging.getLogger(__name__)


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


def sync_profiles(
    *,
    database: str,
    app_id: str,
    api_key: str,
    start_date=None,
    end_date=None,
    max_batches: int | None = None,
    max_runtime: float | None = None,
    refresh_profiles: bool = False,
) -> dict:
    """Enriches only author IDs and returns auditable progress diagnostics."""
    candidate_limit = max_batches * 100 if max_batches is not None else None
    with ReviewStore(database) as store:
        before = store.get_profile_enrichment_summary(
            app_id, start_date, end_date
        )
        candidates = store.get_profile_candidates(
            app_id,
            start_date,
            end_date,
            refresh_profiles=refresh_profiles,
            limit=candidate_limit,
        )
        log.info(
            "Profile sync app=%s authors=%s candidates=%s classified=%s",
            app_id,
            before["unique_authors"],
            len(candidates),
            before["classified_users"],
        )
        interrupted = False
        try:
            run = fetch_country_codes_with_metadata(
                candidates,
                api_key,
                store=store,
                max_batches=max_batches,
                max_runtime=max_runtime,
                refresh_profiles=refresh_profiles,
            )
        except KeyboardInterrupt:
            interrupted = True
            log.warning(
                "Profile synchronization interrupted; completed batches remain cached"
            )
            run = None
        after = store.get_profile_enrichment_summary(
            app_id, start_date, end_date
        )
    return {
        "database": database,
        "app_id": str(app_id),
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "max_batches": max_batches,
        "max_runtime": max_runtime,
        "refresh_profiles": refresh_profiles,
        "interrupted": interrupted,
        "before": before,
        "run": run.to_metadata() if run else None,
        "after": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize Steam author country profiles without loading review texts "
            "or generating a dataset."
        )
    )
    parser.add_argument("appid", help="Steam application ID")
    parser.add_argument(
        "--database",
        required=True,
        help="SQLite review archive and persistent profile cache",
    )
    parser.add_argument("--start", help="Review interval start (dd/mm/yyyy)")
    parser.add_argument("--end", help="Review interval end (dd/mm/yyyy)")
    parser.add_argument(
        "--api-key",
        default=None,
        help="Steam Web API key (or set STEAM_API_KEY)",
    )
    parser.add_argument("--max-batches", type=positive_int, default=None)
    parser.add_argument(
        "--max-runtime",
        type=positive_float,
        default=None,
        metavar="SECONDS",
    )
    parser.add_argument("--refresh-profiles", action="store_true")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("STEAM_API_KEY")
    if not api_key:
        parser.error("Steam API key required via --api-key or STEAM_API_KEY")
    start_date = parse_date(args.start) if args.start else None
    end_date = parse_date(args.end) if args.end else None
    if start_date and end_date and start_date > end_date:
        parser.error("--start must be before or equal to --end")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%d/%m/%Y %H:%M:%S",
    )
    result = sync_profiles(
        database=args.database,
        app_id=args.appid,
        api_key=api_key,
        start_date=start_date,
        end_date=end_date,
        max_batches=args.max_batches,
        max_runtime=args.max_runtime,
        refresh_profiles=args.refresh_profiles,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 130 if result["interrupted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
