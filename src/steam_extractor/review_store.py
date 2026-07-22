"""Persistent, versioned SQLite storage for Steam reviews and sync checkpoints."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


SCHEMA_VERSION = 1
EXPECTED_TOTAL_TOLERANCE_RATIO = 0.001
EXPECTED_TOTAL_MIN_TOLERANCE = 100


def expected_total_reached(received: int, expected: int | None) -> bool:
    """Returns whether a completed stream is reasonably close to Steam's total.

    The total can drift while a long synchronization is running, so large streams allow
    a small 0.1% difference (at least one API page). Streams of at most one page must
    match exactly. Large deficits must never be interpreted as complete history.
    """
    if expected is None:
        return False
    if expected <= EXPECTED_TOTAL_MIN_TOLERANCE:
        tolerance = 0
    else:
        tolerance = max(
            EXPECTED_TOTAL_MIN_TOLERANCE,
            math.ceil(expected * EXPECTED_TOTAL_TOLERANCE_RATIO),
        )
    return received >= max(expected - tolerance, 0)


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def review_content_hash(review: dict) -> str:
    """Hashes research-relevant mutable fields, excluding volatile author statistics."""
    content = {
        "language": review.get("language"),
        "review": review.get("review"),
        "timestamp_updated": review.get("timestamp_updated"),
        "voted_up": review.get("voted_up"),
        "developer_response": review.get("developer_response"),
        "timestamp_dev_responded": review.get("timestamp_dev_responded"),
    }
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ReviewStore:
    """SQLite-backed review archive; opening the class does not require a server."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> ReviewStore:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_info (
                version INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reviews (
                recommendation_id TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                user_id TEXT,
                language TEXT,
                review_text TEXT,
                voted_up INTEGER,
                timestamp_created INTEGER NOT NULL,
                timestamp_updated INTEGER,
                content_hash TEXT NOT NULL,
                current_version INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                possibly_removed INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS reviews_app_date
                ON reviews(app_id, timestamp_created);
            CREATE INDEX IF NOT EXISTS reviews_app_language_date
                ON reviews(app_id, language, timestamp_created);

            CREATE TABLE IF NOT EXISTS review_versions (
                recommendation_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (recommendation_id, version),
                FOREIGN KEY (recommendation_id)
                    REFERENCES reviews(recommendation_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
                run_id TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                language TEXT NOT NULL,
                filter_type TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                completion_reason TEXT,
                pages INTEGER NOT NULL DEFAULT 0,
                received INTEGER NOT NULL DEFAULT 0,
                inserted INTEGER NOT NULL DEFAULT 0,
                unchanged INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                duplicates INTEGER NOT NULL DEFAULT 0,
                expected_reviews INTEGER,
                oldest_timestamp INTEGER,
                newest_timestamp INTEGER
            );

            CREATE TABLE IF NOT EXISTS run_reviews (
                run_id TEXT NOT NULL,
                recommendation_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                PRIMARY KEY (run_id, recommendation_id),
                FOREIGN KEY (run_id) REFERENCES sync_runs(run_id) ON DELETE CASCADE,
                FOREIGN KEY (recommendation_id, version)
                    REFERENCES review_versions(recommendation_id, version)
            );

            CREATE TABLE IF NOT EXISTS sync_jobs (
                job_key TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                language TEXT NOT NULL,
                filter_type TEXT NOT NULL,
                cursor TEXT NOT NULL,
                status TEXT NOT NULL,
                pages INTEGER NOT NULL DEFAULT 0,
                received INTEGER NOT NULL DEFAULT 0,
                expected_reviews INTEGER,
                oldest_timestamp INTEGER,
                newest_timestamp INTEGER,
                last_page_ids TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS coverage (
                app_id TEXT NOT NULL,
                language TEXT NOT NULL,
                filter_type TEXT NOT NULL,
                oldest_timestamp INTEGER,
                newest_timestamp INTEGER,
                complete_history INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (app_id, language, filter_type)
            );

            CREATE TABLE IF NOT EXISTS player_profiles (
                steam_id TEXT PRIMARY KEY,
                country_code TEXT,
                status TEXT NOT NULL,
                checked_at TEXT NOT NULL
            );
            """
        )
        row = self.connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,)
            )
        elif row["version"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported review database schema {row['version']} (expected {SCHEMA_VERSION})"
            )
        self._reclassify_premature_history_completions()
        self.connection.commit()

    def _reclassify_premature_history_completions(self) -> None:
        """Repairs legacy runs that treated any empty page as complete history."""
        suspect_runs = self.connection.execute(
            """
            SELECT * FROM sync_runs
            WHERE filter_type = 'recent'
              AND status = 'complete'
              AND completion_reason = 'end_of_history'
            """
        ).fetchall()
        affected_keys: set[tuple[str, str, str]] = set()
        for run in suspect_runs:
            if expected_total_reached(run["received"], run["expected_reviews"]):
                continue
            self.connection.execute(
                """
                UPDATE sync_runs
                SET status = 'incomplete',
                    completion_reason = 'api_exhausted_before_expected_total'
                WHERE run_id = ?
                """,
                (run["run_id"],),
            )
            key = (run["app_id"], run["language"], run["filter_type"])
            affected_keys.add(key)
            self.connection.execute(
                """
                UPDATE sync_jobs
                SET status = 'incomplete', updated_at = ?
                WHERE job_key = ? AND status = 'complete'
                  AND received = ?
                """,
                (
                    utc_now(),
                    self.job_key(*key),
                    run["received"],
                ),
            )

        for app_id, language, filter_type in affected_keys:
            valid_runs = self.connection.execute(
                """
                SELECT received, expected_reviews FROM sync_runs
                WHERE app_id = ? AND language = ? AND filter_type = ?
                  AND status = 'complete' AND completion_reason = 'end_of_history'
                """,
                (app_id, language, filter_type),
            ).fetchall()
            if any(
                expected_total_reached(run["received"], run["expected_reviews"])
                for run in valid_runs
            ):
                continue
            self.connection.execute(
                """
                UPDATE coverage
                SET complete_history = 0, updated_at = ?
                WHERE app_id = ? AND language = ? AND filter_type = ?
                """,
                (utc_now(), app_id, language, filter_type),
            )

    @staticmethod
    def job_key(app_id: str, language: str, filter_type: str) -> str:
        return f"{app_id}:{language}:{filter_type}"

    def has_review(self, recommendation_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM reviews WHERE recommendation_id = ?", (str(recommendation_id),)
        ).fetchone()
        return row is not None

    def known_review_ids(self, recommendation_ids: Iterable[str]) -> set[str]:
        ids = [str(value) for value in recommendation_ids]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT recommendation_id FROM reviews WHERE recommendation_id IN ({placeholders})",
            ids,
        ).fetchall()
        return {str(row["recommendation_id"]) for row in rows}

    def get_job(self, app_id: str, language: str, filter_type: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM sync_jobs WHERE job_key = ?",
            (self.job_key(app_id, language, filter_type),),
        ).fetchone()
        return dict(row) if row else None

    def start_run(self, run_id: str, app_id: str, language: str, filter_type: str) -> None:
        self.connection.execute(
            """
            INSERT INTO sync_runs(run_id, app_id, language, filter_type, started_at, status)
            VALUES (?, ?, ?, ?, ?, 'running')
            """,
            (run_id, app_id, language, filter_type, utc_now()),
        )
        self.connection.commit()

    def _upsert_review(
        self, connection: sqlite3.Connection, app_id: str, review: dict, observed_at: str
    ) -> tuple[str, int]:
        recommendation_id = str(review["recommendationid"])
        content_hash = review_content_hash(review)
        raw_json = json.dumps(review, ensure_ascii=False, sort_keys=True)
        author = review.get("author") or {}
        existing = connection.execute(
            "SELECT content_hash, current_version FROM reviews WHERE recommendation_id = ?",
            (recommendation_id,),
        ).fetchone()

        if existing is None:
            version_number = 1
            connection.execute(
                """
                INSERT INTO reviews(
                    recommendation_id, app_id, user_id, language, review_text, voted_up,
                    timestamp_created, timestamp_updated, content_hash, current_version,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recommendation_id,
                    str(app_id),
                    author.get("steamid"),
                    review.get("language"),
                    review.get("review"),
                    int(bool(review.get("voted_up"))),
                    int(review["timestamp_created"]),
                    review.get("timestamp_updated"),
                    content_hash,
                    version_number,
                    observed_at,
                    observed_at,
                ),
            )
            status = "inserted"
        elif existing["content_hash"] == content_hash:
            version_number = int(existing["current_version"])
            connection.execute(
                """
                UPDATE reviews SET last_seen_at = ?, possibly_removed = 0
                WHERE recommendation_id = ?
                """,
                (observed_at, recommendation_id),
            )
            status = "unchanged"
        else:
            version_number = int(existing["current_version"]) + 1
            connection.execute(
                """
                UPDATE reviews SET
                    user_id = ?, language = ?, review_text = ?, voted_up = ?,
                    timestamp_updated = ?, content_hash = ?, current_version = ?,
                    last_seen_at = ?, possibly_removed = 0
                WHERE recommendation_id = ?
                """,
                (
                    author.get("steamid"),
                    review.get("language"),
                    review.get("review"),
                    int(bool(review.get("voted_up"))),
                    review.get("timestamp_updated"),
                    content_hash,
                    version_number,
                    observed_at,
                    recommendation_id,
                ),
            )
            status = "updated"

        if status != "unchanged":
            connection.execute(
                """
                INSERT INTO review_versions(
                    recommendation_id, version, observed_at, content_hash, raw_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (recommendation_id, version_number, observed_at, content_hash, raw_json),
            )
        return status, version_number

    def save_page(
        self,
        *,
        run_id: str,
        app_id: str,
        language: str,
        filter_type: str,
        reviews: list[dict],
        next_cursor: str,
        expected_reviews: int | None,
        page_number: int,
        total_received: int,
    ) -> dict[str, int]:
        """Writes one page and advances its checkpoint in the same transaction."""
        observed_at = utc_now()
        counts = {"inserted": 0, "unchanged": 0, "updated": 0, "duplicates": 0}
        page_seen: set[str] = set()
        timestamps: list[int] = []

        with self.transaction() as connection:
            for review in reviews:
                recommendation_id = str(review.get("recommendationid", ""))
                if not recommendation_id or recommendation_id in page_seen:
                    counts["duplicates"] += 1
                    continue
                page_seen.add(recommendation_id)
                if review.get("timestamp_created") is not None:
                    timestamps.append(int(review["timestamp_created"]))
                status, version_number = self._upsert_review(
                    connection, app_id, review, observed_at
                )
                counts[status] += 1
                connection.execute(
                    """
                    INSERT INTO run_reviews(run_id, recommendation_id, version)
                    VALUES (?, ?, ?)
                    ON CONFLICT(run_id, recommendation_id)
                    DO UPDATE SET version = excluded.version
                    """,
                    (run_id, recommendation_id, version_number),
                )

            previous = self.get_job(app_id, language, filter_type)
            previous_oldest = previous.get("oldest_timestamp") if previous else None
            previous_newest = previous.get("newest_timestamp") if previous else None
            oldest = min(timestamps) if timestamps else previous_oldest
            newest = max(timestamps) if timestamps else previous_newest
            if previous_oldest is not None and oldest is not None:
                oldest = min(previous_oldest, oldest)
            if previous_newest is not None and newest is not None:
                newest = max(previous_newest, newest)

            connection.execute(
                """
                INSERT INTO sync_jobs(
                    job_key, app_id, language, filter_type, cursor, status, pages,
                    received, expected_reviews, oldest_timestamp, newest_timestamp,
                    last_page_ids, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_key) DO UPDATE SET
                    cursor = excluded.cursor, status = excluded.status,
                    pages = excluded.pages, received = excluded.received,
                    expected_reviews = COALESCE(
                        excluded.expected_reviews, sync_jobs.expected_reviews
                    ),
                    oldest_timestamp = excluded.oldest_timestamp,
                    newest_timestamp = excluded.newest_timestamp,
                    last_page_ids = excluded.last_page_ids, updated_at = excluded.updated_at
                """,
                (
                    self.job_key(app_id, language, filter_type),
                    str(app_id),
                    language,
                    filter_type,
                    next_cursor,
                    page_number,
                    total_received,
                    expected_reviews,
                    oldest,
                    newest,
                    json.dumps(list(page_seen)),
                    observed_at,
                ),
            )
            connection.execute(
                """
                UPDATE sync_runs SET
                    pages = pages + 1, received = received + ?, inserted = inserted + ?,
                    unchanged = unchanged + ?, updated = updated + ?,
                    duplicates = duplicates + ?, expected_reviews = COALESCE(?, expected_reviews),
                    oldest_timestamp = CASE
                        WHEN oldest_timestamp IS NULL OR ? < oldest_timestamp THEN ?
                        ELSE oldest_timestamp END,
                    newest_timestamp = CASE
                        WHEN newest_timestamp IS NULL OR ? > newest_timestamp THEN ?
                        ELSE newest_timestamp END
                WHERE run_id = ?
                """,
                (
                    len(reviews),
                    counts["inserted"],
                    counts["unchanged"],
                    counts["updated"],
                    counts["duplicates"],
                    expected_reviews,
                    min(timestamps) if timestamps else None,
                    min(timestamps) if timestamps else None,
                    max(timestamps) if timestamps else None,
                    max(timestamps) if timestamps else None,
                    run_id,
                ),
            )
        return counts

    def finish_run(
        self,
        run_id: str,
        app_id: str,
        language: str,
        filter_type: str,
        *,
        status: str,
        reason: str,
        complete_history: bool = False,
        verified_coverage: bool = False,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE sync_runs SET finished_at = ?, status = ?, completion_reason = ?
                WHERE run_id = ?
                """,
                (utc_now(), status, reason, run_id),
            )
            run = connection.execute(
                "SELECT * FROM sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            job = connection.execute(
                "SELECT 1 FROM sync_jobs WHERE job_key = ?",
                (self.job_key(app_id, language, filter_type),),
            ).fetchone()
            if job:
                connection.execute(
                    "UPDATE sync_jobs SET status = ?, updated_at = ? WHERE job_key = ?",
                    (status, utc_now(), self.job_key(app_id, language, filter_type)),
                )
            if run and verified_coverage:
                connection.execute(
                    """
                    INSERT INTO coverage(
                        app_id, language, filter_type, oldest_timestamp,
                        newest_timestamp, complete_history, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(app_id, language, filter_type) DO UPDATE SET
                        oldest_timestamp = CASE
                            WHEN coverage.oldest_timestamp IS NULL
                              OR excluded.oldest_timestamp < coverage.oldest_timestamp
                            THEN excluded.oldest_timestamp ELSE coverage.oldest_timestamp END,
                        newest_timestamp = CASE
                            WHEN coverage.newest_timestamp IS NULL
                              OR excluded.newest_timestamp > coverage.newest_timestamp
                            THEN excluded.newest_timestamp ELSE coverage.newest_timestamp END,
                        complete_history = MAX(
                            coverage.complete_history, excluded.complete_history
                        ),
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(app_id),
                        language,
                        filter_type,
                        run["oldest_timestamp"],
                        run["newest_timestamp"],
                        int(complete_history),
                        utc_now(),
                    ),
                )

    def get_run(self, run_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM sync_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def get_reviews(
        self,
        app_id: str,
        start_date: date,
        end_date: date,
        language: str = "all",
    ) -> list[dict]:
        start_timestamp = int(
            datetime(
                start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc
            ).timestamp()
        )
        end_timestamp = int(
            datetime(
                end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc
            ).timestamp()
        )
        parameters: list[object] = [str(app_id), start_timestamp, end_timestamp]
        language_clause = ""
        if language != "all":
            language_clause = " AND r.language = ?"
            parameters.append(language)
        rows = self.connection.execute(
            f"""
            SELECT v.raw_json, r.current_version, r.content_hash
            FROM reviews AS r
            JOIN review_versions AS v
              ON v.recommendation_id = r.recommendation_id
             AND v.version = r.current_version
            WHERE r.app_id = ? AND r.timestamp_created BETWEEN ? AND ?{language_clause}
            ORDER BY r.timestamp_created DESC
            """,
            parameters,
        ).fetchall()
        result = []
        for row in rows:
            review = json.loads(row["raw_json"])
            review["_archive_version"] = int(row["current_version"])
            review["_archive_content_hash"] = row["content_hash"]
            result.append(review)
        return result

    def has_coverage(
        self, app_id: str, language: str, start_date: date, end_date: date
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT oldest_timestamp, newest_timestamp, complete_history
            FROM coverage WHERE app_id = ? AND language = ? AND filter_type = 'recent'
            """,
            (str(app_id), language),
        ).fetchone()
        if row is None or row["oldest_timestamp"] is None or row["newest_timestamp"] is None:
            return False
        start_timestamp = int(
            datetime(
                start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc
            ).timestamp()
        )
        end_timestamp = int(
            datetime(
                end_date.year,
                end_date.month,
                end_date.day,
                23,
                59,
                59,
                tzinfo=timezone.utc,
            ).timestamp()
        )
        return bool(row["complete_history"]) or (
            row["oldest_timestamp"] <= start_timestamp and row["newest_timestamp"] >= end_timestamp
        )

    def has_unverified_job_coverage(
        self, app_id: str, language: str, start_date: date, end_date: date
    ) -> bool:
        """Checks checkpoint bounds without claiming that cursor continuity was verified."""
        job = self.get_job(app_id, language, "recent")
        if (
            job is None
            or job["status"] != "complete"
            or job["oldest_timestamp"] is None
            or job["newest_timestamp"] is None
        ):
            return False
        start_timestamp = int(
            datetime(
                start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc
            ).timestamp()
        )
        end_timestamp = int(
            datetime(
                end_date.year,
                end_date.month,
                end_date.day,
                23,
                59,
                59,
                tzinfo=timezone.utc,
            ).timestamp()
        )
        return (
            job["oldest_timestamp"] <= start_timestamp
            and job["newest_timestamp"] >= end_timestamp
        )

    def get_coverage(self, app_id: str, language: str, filter_type: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM coverage WHERE app_id = ? AND language = ? AND filter_type = ?",
            (str(app_id), language, filter_type),
        ).fetchone()
        return dict(row) if row else None

    def count_reviews(self, app_id: str | None = None) -> int:
        if app_id is None:
            row = self.connection.execute("SELECT COUNT(*) AS count FROM reviews").fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM reviews WHERE app_id = ?", (str(app_id),)
            ).fetchone()
        return int(row["count"])
