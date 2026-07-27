import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, call, patch

import pandas as pd
import requests

from steam_extractor.reviews_fetcher import (
    PaginationResult,
    ReviewFetchResult,
    save_manifest,
)
from steam_extractor.review_store import ReviewStore
from steam_extractor.profiles_sync import sync_profiles
from steam_extractor.tag_extractor import (
    CountryFetchResult,
    extract_reviews_by_tags,
    fetch_country_codes_with_metadata,
    save_output,
)


class ManifestTests(unittest.TestCase):
    @patch("steam_extractor.tag_extractor.time.sleep")
    @patch("steam_extractor.tag_extractor.requests.get")
    def test_dedicated_profile_sync_reuses_cache_without_loading_review_texts(
        self, get, _sleep
    ):
        response = get.return_value
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "response": {
                "players": [{"steamid": "user-2", "loccountrycode": "US"}]
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(database) as store:
                store.start_run("seed", "1091500", "all", "recent")
                store.save_page(
                    run_id="seed",
                    app_id="1091500",
                    language="all",
                    filter_type="recent",
                    reviews=[
                        {
                            "recommendationid": "1",
                            "author": {"steamid": "user-1"},
                            "timestamp_created": 1704067200,
                            "language": "english",
                            "review": "first",
                            "voted_up": True,
                        },
                        {
                            "recommendationid": "2",
                            "author": {"steamid": "user-2"},
                            "timestamp_created": 1704153600,
                            "language": "english",
                            "review": "second",
                            "voted_up": True,
                        },
                    ],
                    next_cursor="end",
                    expected_reviews=2,
                    page_number=1,
                    total_received=2,
                )
                store.save_player_profiles(
                    [
                        {
                            "steam_id": "user-1",
                            "country_code": "BR",
                            "status": "country_available",
                        }
                    ]
                )

            result = sync_profiles(
                database=database,
                app_id="1091500",
                api_key="key",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
                max_batches=1,
            )

        self.assertEqual(result["before"]["classified_users"], 1)
        self.assertEqual(result["run"]["newly_checked_users"], 1)
        self.assertEqual(result["after"]["classified_users"], 2)
        self.assertEqual(
            result["after"]["known_country_distribution"],
            {"BR": 1, "US": 1},
        )
        self.assertTrue(result["after"]["complete"])
        self.assertEqual(get.call_count, 1)

    @patch("steam_extractor.tag_extractor.time.sleep")
    @patch("steam_extractor.tag_extractor.requests.get")
    def test_profile_enrichment_persists_and_reuses_all_outcomes(
        self, get, _sleep
    ):
        response = get.return_value
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "response": {
                "players": [
                    {"steamid": "1", "loccountrycode": "br"},
                    {"steamid": "2"},
                ]
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "profiles.sqlite")
            with ReviewStore(database) as store:
                first = fetch_country_codes_with_metadata(
                    ["1", "2", "3"], "key", store=store
                )
                get.reset_mock()
                second = fetch_country_codes_with_metadata(
                    ["1", "2", "3"], "key", store=store
                )

        self.assertEqual(first.country_map, {"1": "BR", "2": "", "3": ""})
        self.assertEqual(first.status_map["1"], "country_available")
        self.assertEqual(first.status_map["2"], "country_unavailable")
        self.assertEqual(first.status_map["3"], "profile_unavailable")
        self.assertEqual(second.cached_users, 3)
        self.assertEqual(second.newly_checked_users, 0)
        second_metadata = second.to_metadata()
        self.assertEqual(second_metadata["classified_users"], 3)
        self.assertEqual(second_metadata["processing_coverage_percent"], 100.0)
        self.assertEqual(
            second_metadata["country_availability_among_classified_percent"],
            33.33,
        )
        self.assertTrue(second_metadata["complete"])
        get.assert_not_called()

    @patch("steam_extractor.tag_extractor.time.sleep")
    @patch("steam_extractor.tag_extractor.requests.get")
    def test_profile_enrichment_retries_cached_request_failures(
        self, get, _sleep
    ):
        get.side_effect = RuntimeError("temporary")
        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "profiles.sqlite")
            with ReviewStore(database) as store:
                failed = fetch_country_codes_with_metadata(
                    ["1"], "key", store=store
                )
                response = Mock()
                response.raise_for_status.return_value = None
                response.json.return_value = {
                    "response": {
                        "players": [{"steamid": "1", "loccountrycode": "US"}]
                    }
                }
                get.side_effect = None
                get.return_value = response
                recovered = fetch_country_codes_with_metadata(
                    ["1"], "key", store=store
                )

        self.assertEqual(failed.status_map["1"], "request_failed")
        self.assertFalse(failed.to_metadata()["complete"])
        self.assertEqual(recovered.country_map["1"], "US")
        self.assertEqual(recovered.status_map["1"], "country_available")

    @patch("steam_extractor.tag_extractor.time.sleep")
    @patch("steam_extractor.tag_extractor.requests.get")
    def test_profile_enrichment_retries_transport_timeout(
        self, get, _sleep
    ):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "response": {
                "players": [{"steamid": "1", "loccountrycode": "BR"}]
            }
        }
        get.side_effect = [requests.exceptions.ReadTimeout(), response]

        result = fetch_country_codes_with_metadata(["1"], "key")

        self.assertEqual(get.call_count, 2)
        self.assertEqual(result.status_map["1"], "country_available")
        self.assertEqual(result.failed_batches, 0)

    @patch("steam_extractor.tag_extractor.time.sleep")
    @patch("steam_extractor.tag_extractor.requests.get")
    def test_profile_rate_limit_uses_retry_after_and_recovers_delay(
        self, get, sleep
    ):
        limited_response = Mock(status_code=429)
        limited_response.headers = {"Retry-After": "7"}
        limited = requests.exceptions.HTTPError(response=limited_response)
        success = Mock()
        success.raise_for_status.return_value = None
        success.json.side_effect = [
            {
                "response": {
                    "players": [
                        {
                            "steamid": str(index),
                            "loccountrycode": "BR",
                        }
                        for index in range(offset, offset + 100)
                    ]
                }
            }
            for offset in range(0, 1000, 100)
        ]
        get.side_effect = [limited] + [success] * 10

        result = fetch_country_codes_with_metadata(
            [str(index) for index in range(1000)], "key"
        )

        self.assertIn(call(7.0), sleep.call_args_list)
        self.assertEqual(result.rate_limit_events, 1)
        self.assertEqual(result.retry_after_events, 1)
        self.assertEqual(result.retry_wait_seconds, 7.0)
        self.assertEqual(result.final_request_delay, 0.9)
        self.assertEqual(result.failed_batches, 0)

    @patch("steam_extractor.tag_extractor.time.sleep")
    @patch("steam_extractor.tag_extractor.requests.get")
    def test_profile_final_rate_limit_does_not_wait_ninety_seconds(
        self, get, sleep
    ):
        limited_response = Mock(status_code=429)
        limited_response.headers = {}
        get.side_effect = requests.exceptions.HTTPError(
            response=limited_response
        )

        result = fetch_country_codes_with_metadata(["1"], "key")

        waits = [args.args[0] for args in sleep.call_args_list]
        self.assertEqual(get.call_count, 3)
        self.assertEqual(waits[:2], [30, 60])
        self.assertNotIn(90, waits)
        self.assertEqual(result.rate_limit_events, 3)
        self.assertEqual(result.retry_wait_seconds, 90)
        self.assertEqual(result.failed_batches, 1)

    @patch(
        "steam_extractor.tag_extractor.time.monotonic",
        side_effect=[0.0, 0.0, 0.0, 2.0],
    )
    @patch("steam_extractor.tag_extractor.time.sleep")
    @patch("steam_extractor.tag_extractor.requests.get")
    def test_profile_runtime_deadline_leaves_interrupted_batch_pending(
        self, get, sleep, _monotonic
    ):
        get.side_effect = requests.exceptions.ReadTimeout()

        result = fetch_country_codes_with_metadata(
            ["1"], "key", max_runtime=1
        )

        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(result.newly_checked_users, 0)
        self.assertEqual(result.pending_users, 1)
        self.assertNotIn("1", result.status_map)

    @patch("steam_extractor.tag_extractor.time.sleep")
    @patch("steam_extractor.tag_extractor.requests.get")
    def test_profile_batch_limit_preserves_pending_work_for_next_run(
        self, get, _sleep
    ):
        users = [str(index) for index in range(101)]
        first_response = Mock()
        first_response.raise_for_status.return_value = None
        first_response.json.return_value = {
            "response": {
                "players": [
                    {"steamid": steam_id, "loccountrycode": "BR"}
                    for steam_id in users[:100]
                ]
            }
        }
        second_response = Mock()
        second_response.raise_for_status.return_value = None
        second_response.json.return_value = {
            "response": {
                "players": [{"steamid": users[-1], "loccountrycode": "US"}]
            }
        }
        get.side_effect = [first_response, second_response]

        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "profiles.sqlite")
            with ReviewStore(database) as store:
                paused = fetch_country_codes_with_metadata(
                    users, "key", store=store, max_batches=1
                )
                resumed = fetch_country_codes_with_metadata(
                    users, "key", store=store
                )

        self.assertEqual(paused.newly_checked_users, 100)
        self.assertEqual(paused.pending_users, 1)
        paused_metadata = paused.to_metadata()
        self.assertEqual(paused_metadata["processing_coverage_percent"], 99.01)
        self.assertEqual(
            paused_metadata["country_availability_among_classified_percent"],
            100.0,
        )
        self.assertFalse(paused_metadata["complete"])
        self.assertEqual(resumed.cached_users, 100)
        self.assertEqual(resumed.newly_checked_users, 1)
        self.assertEqual(resumed.country_map[users[-1]], "US")
        self.assertEqual(get.call_count, 2)

    def test_manifest_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            filename = str(Path(temp_dir) / "dataset")
            path = save_manifest(filename, {"schema_version": 1, "dataset_complete": True})

            with open(path, encoding="utf-8") as file:
                metadata = json.load(file)

            self.assertEqual(metadata["schema_version"], 1)
            self.assertTrue(metadata["dataset_complete"])
            self.assertEqual(list(Path(temp_dir).glob(".dataset.metadata.json.*")), [])

    def test_empty_dataset_still_writes_manifest(self):
        frame = pd.DataFrame()
        frame.attrs["extraction_metadata"] = {
            "schema_version": 1,
            "dataset_complete": True,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            filename = str(Path(temp_dir) / "empty")
            save_output(frame, filename)

            self.assertFalse(Path(f"{filename}.csv").exists())
            with open(f"{filename}.metadata.json", encoding="utf-8") as file:
                metadata = json.load(file)

        self.assertEqual(metadata["output"]["rows"], 0)
        self.assertIsNone(metadata["output"]["file"])

    @patch("steam_extractor.tag_extractor.software_metadata", return_value={"test": True})
    @patch("steam_extractor.tag_extractor.fetch_country_codes_with_metadata")
    @patch("steam_extractor.tag_extractor.fetch_reviews_with_metadata")
    def test_pipeline_manifest_preserves_provenance_and_review_id(
        self, fetch_reviews, fetch_countries, _software
    ):
        pagination = PaginationResult(
            reviews=[],
            complete=True,
            reason="end_of_history",
            pages=1,
            scanned_reviews=1,
            expected_reviews=1,
            oldest_date=date(2024, 3, 28),
            drift_detected=False,
        )
        fetch_reviews.return_value = ReviewFetchResult(
            reviews=[
                {
                    "recommendationid": "42",
                    "author": {"steamid": "765"},
                    "timestamp_created": 1711584000,
                    "language": "brazilian",
                    "review": "Ótimo",
                    "voted_up": True,
                }
            ],
            passes=[pagination],
        )
        fetch_countries.return_value = CountryFetchResult(
            country_map={"765": "BR"},
            requested_users=1,
            returned_profiles=1,
            country_available=1,
            failed_batches=0,
            failed_users=0,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = Path(temp_dir) / "catalog.json"
            catalog.write_text(
                json.dumps(
                    {
                        "metadata": {"downloaded_at": "2026-01-01", "total_games": 1},
                        "games": {"2076580": {"name": "Pepper Grinder", "tags": {"Action": 1}}},
                    }
                ),
                encoding="utf-8",
            )
            frame = extract_reviews_by_tags(
                tags=["Action"],
                countries=["BR"],
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
                api_key="key",
                db_path=str(catalog),
                appids=["2076580"],
                game_delay=0,
            )

        metadata = frame.attrs["extraction_metadata"]
        self.assertEqual(frame.iloc[0]["recommendation_id"], "42")
        self.assertTrue(metadata["dataset_complete"])
        self.assertEqual(metadata["games"][0]["passes"][0]["scanned_reviews"], 1)
        self.assertEqual(metadata["country_enrichment"]["reviews_before_filter"], 1)
        self.assertEqual(metadata["country_enrichment"]["reviews_matching_filter"], 1)
        self.assertIsNone(metadata["query"]["profile_max_batches"])
        self.assertIsNone(metadata["query"]["profile_max_runtime"])
        self.assertFalse(metadata["query"]["refresh_profiles"])

    @patch("steam_extractor.tag_extractor.software_metadata", return_value={"test": True})
    @patch("steam_extractor.tag_extractor.fetch_country_codes_with_metadata")
    @patch("steam_extractor.tag_extractor.fetch_reviews_with_metadata")
    def test_country_filter_retains_reviews_with_unavailable_country(
        self, fetch_reviews, fetch_countries, _software
    ):
        pagination = PaginationResult(
            reviews=[],
            complete=True,
            reason="end_of_history",
            pages=1,
            scanned_reviews=3,
            expected_reviews=3,
            oldest_date=date(2024, 3, 28),
            drift_detected=False,
        )
        fetch_reviews.return_value = ReviewFetchResult(
            reviews=[
                {
                    "recommendationid": str(index),
                    "author": {"steamid": steam_id},
                    "timestamp_created": 1711584000,
                    "language": "english",
                    "review": "text",
                    "voted_up": True,
                }
                for index, steam_id in enumerate(("br", "us", "unknown"), start=1)
            ],
            passes=[pagination],
        )
        fetch_countries.return_value = CountryFetchResult(
            country_map={"br": "BR", "us": "US", "unknown": ""},
            requested_users=3,
            returned_profiles=3,
            country_available=2,
            failed_batches=0,
            failed_users=0,
            status_map={
                "br": "country_available",
                "us": "country_available",
                "unknown": "country_unavailable",
            },
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = Path(temp_dir) / "catalog.json"
            catalog.write_text(
                json.dumps(
                    {
                        "metadata": {"downloaded_at": "2026-01-01"},
                        "games": {"1091500": {"name": "Cyberpunk 2077"}},
                    }
                ),
                encoding="utf-8",
            )
            frame = extract_reviews_by_tags(
                tags=["Action"],
                countries=["BR"],
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
                api_key="key",
                db_path=str(catalog),
                appids=["1091500"],
                game_delay=0,
            )

        self.assertEqual(set(frame["user_id"]), {"br", "unknown"})
        unknown = frame[frame["user_id"] == "unknown"].iloc[0]
        self.assertEqual(unknown["country_code"], "")
        self.assertEqual(unknown["country_status"], "country_unavailable")
        metadata = frame.attrs["extraction_metadata"]["country_enrichment"]
        self.assertEqual(metadata["reviews_matching_filter"], 1)
        self.assertEqual(metadata["reviews_unresolved_retained"], 1)
        self.assertEqual(
            metadata["coverage_by_language"]["english"][
                "processing_coverage_percent"
            ],
            100.0,
        )
        self.assertEqual(
            metadata["coverage_by_language"]["english"][
                "country_availability_among_classified_percent"
            ],
            66.67,
        )
        self.assertEqual(
            metadata["known_country_distribution_reviews"],
            {"BR": 1, "US": 1},
        )

    @patch("steam_extractor.tag_extractor.software_metadata", return_value={"test": True})
    @patch("steam_extractor.tag_extractor.fetch_country_codes_with_metadata")
    @patch("steam_extractor.tag_extractor.fetch_reviews_with_metadata")
    def test_pipeline_reuses_verified_sqlite_coverage(
        self, fetch_reviews, fetch_countries, _software
    ):
        fetch_reviews.side_effect = AssertionError("Steam fallback should not run")
        fetch_countries.return_value = CountryFetchResult(
            country_map={"765": "BR"},
            requested_users=1,
            returned_profiles=1,
            country_available=1,
            failed_batches=0,
            failed_users=0,
        )
        cached_review = {
            "recommendationid": "42",
            "author": {"steamid": "765"},
            "timestamp_created": 1711584000,
            "timestamp_updated": 1711584000,
            "language": "brazilian",
            "review": "Ótimo",
            "voted_up": True,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            database = str(Path(temp_dir) / "reviews.sqlite")
            with ReviewStore(database) as store:
                store.start_run("seed", "2076580", "all", "recent")
                store.save_page(
                    run_id="seed",
                    app_id="2076580",
                    language="all",
                    filter_type="recent",
                    reviews=[cached_review],
                    next_cursor="end",
                    expected_reviews=1,
                    page_number=1,
                    total_received=1,
                )
                store.finish_run(
                    "seed",
                    "2076580",
                    "all",
                    "recent",
                    status="complete",
                    reason="end_of_history",
                    complete_history=True,
                    verified_coverage=True,
                )
            catalog = Path(temp_dir) / "catalog.json"
            catalog.write_text(
                json.dumps(
                    {
                        "metadata": {"downloaded_at": "2026-01-01"},
                        "games": {"2076580": {"name": "Pepper Grinder", "tags": {"Action": 1}}},
                    }
                ),
                encoding="utf-8",
            )
            frame = extract_reviews_by_tags(
                tags=["Action"],
                countries=["BR"],
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
                api_key="key",
                db_path=str(catalog),
                appids=["2076580"],
                game_delay=0,
                review_database=database,
            )

        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["review_version"], 1)
        self.assertEqual(len(frame.iloc[0]["review_content_hash"]), 64)
        self.assertEqual(frame.attrs["extraction_metadata"]["games"][0]["source"], "sqlite_cache")
        fetch_reviews.assert_not_called()


if __name__ == "__main__":
    unittest.main()
