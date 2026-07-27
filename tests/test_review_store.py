import sqlite3
import tempfile
import unittest
from argparse import Namespace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from steam_extractor.review_store import ReviewStore, expected_total_reached
from steam_extractor.reviews_sync import (
    _request_page,
    main,
    sync_filter,
    sync_partitioned_reviews,
    sync_reviews,
)


def review(review_id: str, text: str = "original", timestamp: int = 1704067200) -> dict:
    return {
        "recommendationid": review_id,
        "author": {"steamid": f"user-{review_id}"},
        "language": "english",
        "review": text,
        "voted_up": True,
        "timestamp_created": timestamp,
        "timestamp_updated": timestamp,
    }


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp_dir.name) / "reviews.sqlite")
        self.store = ReviewStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def save(self, run_id: str, reviews: list[dict], cursor: str = "next") -> dict:
        self.store.start_run(run_id, "730", "all", "recent")
        return self.store.save_page(
            run_id=run_id,
            app_id="730",
            language="all",
            filter_type="recent",
            reviews=reviews,
            next_cursor=cursor,
            expected_reviews=len(reviews),
            page_number=1,
            total_received=len(reviews),
        )

    def test_new_unchanged_and_updated_reviews_are_versioned(self):
        first = self.save("run-1", [review("1")])
        self.store.finish_run(
            "run-1", "730", "all", "recent", status="complete", reason="test"
        )
        second = self.save("run-2", [review("1")])
        self.store.finish_run(
            "run-2", "730", "all", "recent", status="complete", reason="test"
        )
        third = self.save("run-3", [review("1", text="edited")])

        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(third["updated"], 1)
        self.assertEqual(self.store.count_reviews("730"), 1)
        versions = self.store.connection.execute(
            "SELECT version FROM review_versions WHERE recommendation_id = '1' ORDER BY version"
        ).fetchall()
        self.assertEqual([row["version"] for row in versions], [1, 2])
        current = self.store.get_reviews(
            "730", date(2024, 1, 1), date(2024, 1, 1)
        )
        self.assertEqual(current[0]["review"], "edited")

    def test_duplicate_ids_in_page_are_not_inserted_twice(self):
        counts = self.save("run-1", [review("1"), review("1")])

        self.assertEqual(counts["inserted"], 1)
        self.assertEqual(counts["duplicates"], 1)
        self.assertEqual(self.store.count_reviews(), 1)

    def test_page_and_checkpoint_rollback_together(self):
        malformed = review("2")
        del malformed["timestamp_created"]
        self.store.start_run("run-1", "730", "all", "recent")

        with self.assertRaises(KeyError):
            self.store.save_page(
                run_id="run-1",
                app_id="730",
                language="all",
                filter_type="recent",
                reviews=[review("1"), malformed],
                next_cursor="next",
                expected_reviews=2,
                page_number=1,
                total_received=2,
            )

        self.assertEqual(self.store.count_reviews(), 0)
        self.assertIsNone(self.store.get_job("730", "all", "recent"))

    def test_coverage_allows_local_date_query(self):
        self.save("run-1", [review("1")])
        self.store.finish_run(
            "run-1",
            "730",
            "all",
            "recent",
            status="complete",
            reason="end_of_history",
            complete_history=True,
            verified_coverage=True,
        )

        self.assertTrue(
            self.store.has_coverage("730", "all", date(2020, 1, 1), date(2024, 1, 1))
        )

    def test_expected_total_allows_only_small_live_drift(self):
        self.assertTrue(expected_total_reached(9_730_000, 9_735_939))
        self.assertFalse(expected_total_reached(1_057_976, 9_735_939))
        self.assertTrue(expected_total_reached(0, 0))
        self.assertFalse(expected_total_reached(0, 50))
        self.assertFalse(expected_total_reached(100, None))

    def test_legacy_premature_completion_is_reclassified_without_losing_coverage(self):
        self.save("legacy", [review("1")])
        self.store.connection.execute(
            "UPDATE sync_runs SET expected_reviews = 10000 WHERE run_id = 'legacy'"
        )
        self.store.connection.commit()
        self.store.finish_run(
            "legacy",
            "730",
            "all",
            "recent",
            status="complete",
            reason="end_of_history",
            complete_history=True,
            verified_coverage=True,
            operational_complete=True,
            research_verified=True,
        )
        self.store.close()

        self.store = ReviewStore(self.path)

        self.assertEqual(self.store.get_run("legacy")["status"], "incomplete")
        self.assertEqual(
            self.store.get_run("legacy")["completion_reason"],
            "api_exhausted_before_expected_total",
        )
        self.assertEqual(self.store.get_job("730", "all", "recent")["status"], "incomplete")
        coverage = self.store.get_coverage("730", "all", "recent")
        self.assertEqual(coverage["complete_history"], 0)
        self.assertEqual(self.store.get_run("legacy")["operational_complete"], 0)
        self.assertEqual(self.store.get_run("legacy")["research_verified"], 0)
        self.assertIsNotNone(coverage["oldest_timestamp"])
        self.assertEqual(self.store.count_reviews("730"), 1)

    def test_schema_v2_migrates_cumulative_received(self):
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute("ALTER TABLE sync_runs DROP COLUMN cumulative_received")
        connection.execute("UPDATE schema_info SET version = 2")
        connection.commit()
        connection.close()

        self.store = ReviewStore(self.path)

        columns = {
            row["name"]
            for row in self.store.connection.execute("PRAGMA table_info(sync_runs)")
        }
        version = self.store.connection.execute(
            "SELECT version FROM schema_info"
        ).fetchone()["version"]
        self.assertIn("cumulative_received", columns)
        self.assertEqual(version, 3)

    def test_partitioned_coverage_is_consolidated_as_all_languages(self):
        for language, timestamp in (("english", 100), ("brazilian", 200)):
            self.store.start_run(f"run-{language}", "730", language, "recent")
            self.store.save_page(
                run_id=f"run-{language}",
                app_id="730",
                language=language,
                filter_type="recent",
                reviews=[review(language, timestamp=timestamp)],
                next_cursor="end",
                expected_reviews=1,
                page_number=1,
                total_received=1,
            )
            self.store.finish_run(
                f"run-{language}",
                "730",
                language,
                "recent",
                status="complete",
                reason="end_of_history",
                complete_history=True,
                verified_coverage=True,
                operational_complete=True,
                research_verified=True,
            )

        self.store.set_partitioned_coverage(
            "730", ("english", "brazilian"), filter_type="recent"
        )

        aggregate = self.store.get_coverage("730", "all", "recent")
        self.assertEqual(aggregate["complete_history"], 1)
        self.assertEqual(aggregate["oldest_timestamp"], 100)
        self.assertEqual(aggregate["newest_timestamp"], 200)

    def test_player_profiles_are_persisted_with_unavailable_statuses(self):
        self.store.save_player_profiles(
            [
                {
                    "steam_id": "available",
                    "country_code": "BR",
                    "status": "country_available",
                },
                {
                    "steam_id": "no-country",
                    "country_code": None,
                    "status": "country_unavailable",
                },
            ]
        )

        profiles = self.store.get_player_profiles(
            ["available", "no-country", "unknown"]
        )

        self.assertEqual(profiles["available"]["country_code"], "BR")
        self.assertEqual(profiles["available"]["status"], "country_available")
        self.assertIsNone(profiles["no-country"]["country_code"])
        self.assertEqual(
            profiles["no-country"]["status"], "country_unavailable"
        )
        self.assertNotIn("unknown", profiles)

    def test_profile_candidates_exclude_terminal_states_and_respect_dates(self):
        self.save(
            "profile-seed",
            [
                review("available", timestamp=1704067200),
                review("failed", timestamp=1706745600),
                review("outside", timestamp=1672531200),
            ],
        )
        self.store.save_player_profiles(
            [
                {
                    "steam_id": "user-available",
                    "country_code": "BR",
                    "status": "country_available",
                },
                {
                    "steam_id": "user-failed",
                    "country_code": None,
                    "status": "request_failed",
                },
            ]
        )

        candidates = self.store.get_profile_candidates(
            "730", date(2024, 1, 1), date(2024, 12, 31)
        )
        summary = self.store.get_profile_enrichment_summary(
            "730", date(2024, 1, 1), date(2024, 12, 31)
        )

        self.assertEqual(candidates, ["user-failed"])
        self.assertEqual(summary["unique_authors"], 2)
        self.assertEqual(summary["classified_users"], 1)
        self.assertEqual(summary["request_failed_users"], 1)
        self.assertEqual(summary["known_country_distribution"], {"BR": 1})
        self.assertFalse(summary["complete"])


class SyncResumeTests(unittest.TestCase):
    @patch("steam_extractor.reviews_sync.time.sleep")
    @patch("steam_extractor.reviews_sync._request_page")
    def test_pending_verification_survives_until_the_next_command(
        self, request_page, _sleep
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(path) as store:
                store.start_run("operational", "730", "all", "recent")
                store.save_page(
                    run_id="operational",
                    app_id="730",
                    language="all",
                    filter_type="recent",
                    reviews=[review("1")],
                    next_cursor="end",
                    expected_reviews=1,
                    page_number=1,
                    total_received=1,
                )
                store.finish_run(
                    "operational",
                    "730",
                    "all",
                    "recent",
                    status="complete",
                    reason="end_of_history",
                    operational_complete=True,
                    research_verified=False,
                )

            request_page.side_effect = [
                {
                    "success": 1,
                    "cursor": "verification-end",
                    "query_summary": {"total_reviews": 1},
                    "reviews": [review("1")],
                },
                {"success": 1, "cursor": "verification-end", "reviews": []},
            ]
            result = sync_reviews(path, "730", sync_updates=False)

        self.assertTrue(result["recent"]["verification_pass"])
        self.assertTrue(result["research_verified"])
        self.assertEqual(result["verification_passes"], [])

    @patch("steam_extractor.reviews_sync.time.sleep")
    @patch("steam_extractor.reviews_sync._request_page")
    def test_resumed_collection_requires_full_converged_verification(
        self, request_page, _sleep
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(path) as store:
                store.start_run("paused", "730", "all", "recent")
                store.save_page(
                    run_id="paused",
                    app_id="730",
                    language="all",
                    filter_type="recent",
                    reviews=[review("1")],
                    next_cursor="resume-here",
                    expected_reviews=1,
                    page_number=1,
                    total_received=1,
                )
                store.finish_run(
                    "paused",
                    "730",
                    "all",
                    "recent",
                    status="paused",
                    reason="user_interrupted",
                )

            request_page.side_effect = [
                # Finish the cursor-based operational resume.
                {"success": 1, "cursor": "resume-here", "reviews": []},
                # Verification 1 finds a review missed at the interruption boundary.
                {
                    "success": 1,
                    "cursor": "verify-1-end",
                    "query_summary": {"total_reviews": 2},
                    "reviews": [review("1"), review("2")],
                },
                {"success": 1, "cursor": "verify-1-end", "reviews": []},
                # Verification 2 converges without adding another recommendation ID.
                {
                    "success": 1,
                    "cursor": "verify-2-end",
                    "query_summary": {"total_reviews": 2},
                    "reviews": [review("1"), review("2")],
                },
                {"success": 1, "cursor": "verify-2-end", "reviews": []},
            ]
            result = sync_reviews(
                path,
                "730",
                resume=True,
                sync_updates=False,
            )

            with ReviewStore(path) as store:
                runs = store.connection.execute(
                    """
                    SELECT verification_pass, operational_complete, research_verified
                    FROM sync_runs WHERE run_id != 'paused' ORDER BY started_at, rowid
                    """
                ).fetchall()
                coverage = store.get_coverage("730", "all", "recent")

        self.assertTrue(result["operational_complete"])
        self.assertTrue(result["research_verified"])
        self.assertEqual(len(result["verification_passes"]), 2)
        self.assertEqual(result["verification_passes"][0]["inserted"], 1)
        self.assertFalse(result["verification_passes"][0]["research_verified"])
        self.assertEqual(result["verification_passes"][1]["inserted"], 0)
        self.assertTrue(result["verification_passes"][1]["research_verified"])
        self.assertEqual(
            [tuple(row) for row in runs],
            [(0, 1, 0), (1, 1, 0), (1, 1, 1)],
        )
        self.assertEqual(coverage["complete_history"], 1)


class LanguagePartitionTests(unittest.TestCase):
    @patch("steam_extractor.reviews_sync.requests.get")
    def test_api_request_explicitly_excludes_offtopic_reviews(self, get):
        response = Mock()
        response.content = b'{"success": 1, "reviews": []}'
        response.raise_for_status.return_value = None
        get.return_value = response

        _request_page("730", "english", "recent", "*")

        self.assertEqual(
            get.call_args.kwargs["params"]["filter_offtopic_activity"], 1
        )

    @patch("steam_extractor.reviews_sync.SUPPORTED_REVIEW_LANGUAGES", ("english", "brazilian"))
    @patch("steam_extractor.reviews_sync.sync_reviews")
    def test_all_languages_are_independent_partitions(self, sync_one):
        def result_for_language(*, language, **_kwargs):
            return {
                "language": language,
                "operational_complete": False,
                "research_verified": False,
                "recent": {
                    "pages": 1,
                    "expected_reviews": 10,
                    "reason": "api_exhausted_before_expected_total",
                },
                "verification_passes": [],
                "updated": None,
            }

        sync_one.side_effect = result_for_language
        with tempfile.TemporaryDirectory() as temp_dir:
            result = sync_partitioned_reviews(
                str(Path(temp_dir) / "reviews.sqlite"),
                "730",
                sync_updates=False,
            )

        self.assertEqual(
            [call.kwargs["language"] for call in sync_one.call_args_list],
            ["english", "brazilian"],
        )
        self.assertEqual(result["partition_strategy"], "language")
        self.assertFalse(result["offtopic_reviews_included"])
        self.assertEqual(result["expected_reviews_sum"], 20)

    @patch("steam_extractor.reviews_sync.sync_partitioned_reviews")
    @patch("steam_extractor.reviews_sync.sync_reviews")
    @patch("steam_extractor.reviews_sync.argparse.ArgumentParser.parse_args")
    @patch("builtins.print")
    def test_explicit_language_does_not_use_partitioned_mode(
        self, _print, parse_args, sync_one, sync_all
    ):
        parse_args.return_value = Namespace(
            appid="730",
            language="pt-br",
            start=None,
            database="reviews.sqlite",
            resume=False,
            max_pages=None,
            max_runtime=None,
            overlap_pages=3,
            verification_passes=3,
            no_sync_updates=True,
        )
        sync_one.return_value = {
            "recent": {"reason": "end_of_history"},
            "verification_passes": [],
            "updated": None,
        }

        self.assertEqual(main(), 0)
        self.assertEqual(sync_one.call_args.kwargs["language"], "brazilian")
        sync_all.assert_not_called()


class SyncResumeContinuedTests(unittest.TestCase):
    @patch("steam_extractor.reviews_sync.time.sleep")
    @patch("steam_extractor.reviews_sync._request_page")
    def test_ctrl_c_marks_checkpoint_paused_and_allows_resume(self, request_page, _sleep):
        request_page.side_effect = [
            {
                "success": 1,
                "cursor": "cursor-2",
                "query_summary": {"total_reviews": 1},
                "reviews": [review("1")],
            },
            KeyboardInterrupt(),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(path) as store:
                interrupted = sync_filter(
                    store,
                    app_id="730",
                    language="all",
                    filter_type="recent",
                )
                job = store.get_job("730", "all", "recent")

                self.assertEqual(interrupted.status, "paused")
                self.assertEqual(interrupted.reason, "user_interrupted")
                self.assertEqual(job["status"], "paused")
                self.assertEqual(job["cursor"], "cursor-2")

                request_page.reset_mock()
                request_page.side_effect = None
                request_page.return_value = {
                    "success": 1,
                    "cursor": "cursor-2",
                    "reviews": [],
                }
                resumed = sync_filter(
                    store,
                    app_id="730",
                    language="all",
                    filter_type="recent",
                    resume=True,
                )

        self.assertTrue(resumed.resumed)
        self.assertEqual(resumed.status, "complete")
        self.assertEqual(request_page.call_args.args[3], "cursor-2")

    def test_ctrl_c_rolls_back_active_page_transaction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(path) as store:
                store.start_run("run-1", "730", "all", "recent")
                with self.assertRaises(KeyboardInterrupt):
                    with store.transaction() as connection:
                        connection.execute(
                            """
                            INSERT INTO reviews(
                                recommendation_id, app_id, timestamp_created,
                                content_hash, current_version, first_seen_at, last_seen_at
                            ) VALUES ('partial', '730', 1, 'hash', 1, 'now', 'now')
                            """
                        )
                        raise KeyboardInterrupt

                self.assertFalse(store.has_review("partial"))

    @patch("steam_extractor.reviews_sync.time.sleep")
    @patch("steam_extractor.reviews_sync._request_page")
    def test_empty_page_with_large_deficit_is_incomplete(self, request_page, _sleep):
        request_page.side_effect = [
            {
                "success": 1,
                "cursor": "cursor-2",
                "query_summary": {"total_reviews": 10_000},
                "reviews": [review("1")],
            },
            {"success": 1, "cursor": "cursor-2", "reviews": []},
            {"success": 1, "cursor": "cursor-2", "reviews": []},
            {"success": 1, "cursor": "cursor-2", "reviews": []},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(path) as store:
                result = sync_filter(
                    store,
                    app_id="730",
                    language="all",
                    filter_type="recent",
                )
                coverage = store.get_coverage("730", "all", "recent")

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.reason, "api_exhausted_before_expected_total")
        self.assertEqual(result.expected_reviews, 10_000)
        self.assertEqual(result.cumulative_received, 1)
        self.assertTrue(result.coverage_verified)
        self.assertEqual(coverage["complete_history"], 0)
        self.assertEqual(request_page.call_count, 4)

    @patch("steam_extractor.reviews_sync.time.sleep")
    @patch("steam_extractor.reviews_sync._request_page")
    def test_transient_empty_page_continues_from_same_cursor(
        self, request_page, _sleep
    ):
        request_page.side_effect = [
            {
                "success": 1,
                "cursor": "cursor-2",
                "query_summary": {"total_reviews": 2},
                "reviews": [review("1")],
            },
            {"success": 1, "cursor": "cursor-2", "reviews": []},
            {
                "success": 1,
                "cursor": "cursor-3",
                "reviews": [review("2")],
            },
            {"success": 1, "cursor": "cursor-3", "reviews": []},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(path) as store:
                result = sync_filter(
                    store,
                    app_id="730",
                    language="english",
                    filter_type="recent",
                )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.cumulative_received, 2)
        self.assertEqual(result.inserted, 2)
        self.assertEqual(request_page.call_args_list[2].args[3], "cursor-2")

    @patch("steam_extractor.reviews_sync.time.sleep")
    @patch("steam_extractor.reviews_sync._request_page")
    def test_zero_review_total_is_persisted_and_remains_verified(
        self, request_page, _sleep
    ):
        request_page.return_value = {
            "success": 1,
            "cursor": "*",
            "query_summary": {"total_reviews": 0},
            "reviews": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(path) as store:
                result = sync_filter(
                    store,
                    app_id="730",
                    language="arabic",
                    filter_type="recent",
                )
                run_id = result.run_id

            with ReviewStore(path) as reopened:
                persisted = reopened.get_run(run_id)

        self.assertEqual(result.status, "complete")
        self.assertTrue(result.research_verified)
        self.assertEqual(persisted["expected_reviews"], 0)
        self.assertEqual(persisted["status"], "complete")
        self.assertEqual(persisted["operational_complete"], 1)
        self.assertEqual(persisted["research_verified"], 1)

    @patch("steam_extractor.reviews_sync.time.sleep")
    @patch("steam_extractor.reviews_sync._request_page")
    def test_paused_sync_resumes_from_saved_cursor(self, request_page, _sleep):
        first_page = {
            "success": 1,
            "cursor": "cursor-2",
            "query_summary": {"total_reviews": 1},
            "reviews": [review("1")],
        }
        request_page.return_value = first_page

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(path) as store:
                paused = sync_filter(
                    store,
                    app_id="730",
                    language="all",
                    filter_type="recent",
                    max_pages=1,
                )
                self.assertEqual(paused.status, "paused")
                self.assertEqual(store.get_job("730", "all", "recent")["cursor"], "cursor-2")

                request_page.reset_mock()
                request_page.return_value = {"success": 1, "cursor": "cursor-2", "reviews": []}
                resumed = sync_filter(
                    store,
                    app_id="730",
                    language="all",
                    filter_type="recent",
                    resume=True,
                )

                self.assertTrue(resumed.resumed)
                self.assertEqual(resumed.status, "complete")
                self.assertFalse(resumed.coverage_verified)
                self.assertEqual(request_page.call_args.args[3], "cursor-2")
                self.assertEqual(store.count_reviews("730"), 1)

    @patch("steam_extractor.reviews_sync.time.sleep")
    @patch("steam_extractor.reviews_sync._request_page")
    def test_incremental_sync_stops_after_confirmed_known_overlap(self, request_page, _sleep):
        pages = [
            [review(str(offset + index), timestamp=1704067200 - offset - index)
             for index in range(100)]
            for offset in (0, 100, 200)
        ]
        request_page.side_effect = [
            {"success": 1, "cursor": f"cursor-{index}", "reviews": page}
            for index, page in enumerate(pages, start=1)
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(path) as store:
                store.start_run("seed", "730", "all", "recent")
                store.save_page(
                    run_id="seed",
                    app_id="730",
                    language="all",
                    filter_type="recent",
                    reviews=[item for page in pages for item in page],
                    next_cursor="seed-end",
                    expected_reviews=300,
                    page_number=1,
                    total_received=300,
                )
                store.finish_run(
                    "seed",
                    "730",
                    "all",
                    "recent",
                    status="complete",
                    reason="seed",
                    complete_history=True,
                    verified_coverage=True,
                )

                result = sync_filter(
                    store,
                    app_id="730",
                    language="all",
                    filter_type="recent",
                    overlap_pages=3,
                )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.reason, "known_overlap_reached")
        self.assertEqual(result.inserted, 0)
        self.assertEqual(result.unchanged, 300)
        self.assertEqual(request_page.call_count, 3)

    @patch("steam_extractor.reviews_sync.time.sleep")
    @patch("steam_extractor.reviews_sync._request_page")
    def test_overlap_does_not_stop_before_extending_older_coverage(self, request_page, _sleep):
        known = review("1", timestamp=1735689600)  # 01/01/2025 UTC
        request_page.return_value = {
            "success": 1,
            "cursor": "next",
            "reviews": [known],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(path) as store:
                store.start_run("seed", "730", "all", "recent")
                store.save_page(
                    run_id="seed",
                    app_id="730",
                    language="all",
                    filter_type="recent",
                    reviews=[known],
                    next_cursor="seed-end",
                    expected_reviews=1,
                    page_number=1,
                    total_received=1,
                )
                store.finish_run(
                    "seed",
                    "730",
                    "all",
                    "recent",
                    status="complete",
                    reason="start_date_reached",
                    verified_coverage=True,
                )

                result = sync_filter(
                    store,
                    app_id="730",
                    language="all",
                    filter_type="recent",
                    start_date=date(2024, 1, 1),
                    overlap_pages=1,
                    max_pages=1,
                )

        self.assertEqual(result.status, "paused")
        self.assertEqual(result.reason, "max_pages_reached")


if __name__ == "__main__":
    unittest.main()
