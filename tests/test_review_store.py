import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from steam_extractor.review_store import ReviewStore, expected_total_reached
from steam_extractor.reviews_sync import sync_filter


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
        self.assertIsNotNone(coverage["oldest_timestamp"])
        self.assertEqual(self.store.count_reviews("730"), 1)


class SyncResumeTests(unittest.TestCase):
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
