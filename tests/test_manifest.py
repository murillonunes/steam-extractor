import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from steam_extractor.reviews_fetcher import (
    PaginationResult,
    ReviewFetchResult,
    save_manifest,
)
from steam_extractor.tag_extractor import (
    CountryFetchResult,
    extract_reviews_by_tags,
    save_output,
)


class ManifestTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
