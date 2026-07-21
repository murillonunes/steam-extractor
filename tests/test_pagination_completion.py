import json
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

from steam_extractor.reviews_fetcher import _paginate_once


def timestamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def response(reviews: list[dict], cursor: str, total: int | None = None) -> Mock:
    payload = {"success": 1, "cursor": cursor, "reviews": reviews}
    if total is not None:
        payload["query_summary"] = {"total_reviews": total}
    result = Mock()
    result.content = json.dumps(payload).encode()
    return result


class PaginationCompletionTests(unittest.TestCase):
    @patch("steam_extractor.reviews_fetcher.time.sleep")
    @patch("steam_extractor.reviews_fetcher.requests.get")
    def test_partial_last_page_is_normal_end_of_history(self, get, _sleep):
        reviews = [
            {"recommendationid": str(i), "timestamp_created": timestamp(date(2024, 3, 28))}
            for i in range(58)
        ]
        get.side_effect = [response(reviews, "next", total=58), response([], "next")]

        result = _paginate_once(
            "2076580", "all", date(2024, 1, 1), date(2024, 12, 31), 2_000_000
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.reason, "end_of_history")
        self.assertEqual(result.scanned_reviews, 58)
        self.assertEqual(result.expected_reviews, 58)
        self.assertEqual(result.oldest_date, date(2024, 3, 28))

    @patch("steam_extractor.reviews_fetcher.time.sleep")
    @patch("steam_extractor.reviews_fetcher.requests.get")
    def test_full_page_then_empty_before_range_is_incomplete(self, get, _sleep):
        reviews = [
            {"recommendationid": str(i), "timestamp_created": timestamp(date(2026, 1, 1))}
            for i in range(100)
        ]
        get.side_effect = [response(reviews, "next", total=500), response([], "next")]

        result = _paginate_once(
            "730", "all", date(2024, 1, 1), date(2024, 1, 2), 2_000_000
        )

        self.assertFalse(result.complete)
        self.assertEqual(result.reason, "premature_empty_response")
        self.assertEqual(result.scanned_reviews, 100)
        self.assertEqual(result.expected_reviews, 500)

    @patch("steam_extractor.reviews_fetcher.time.sleep")
    @patch("steam_extractor.reviews_fetcher.requests.get")
    def test_partial_page_does_not_override_conflicting_reported_total(self, get, _sleep):
        reviews = [
            {"recommendationid": str(i), "timestamp_created": timestamp(date(2026, 1, 1))}
            for i in range(58)
        ]
        get.side_effect = [response(reviews, "next", total=500), response([], "next")]

        result = _paginate_once(
            "730", "all", date(2024, 1, 1), date(2024, 1, 2), 2_000_000
        )

        self.assertFalse(result.complete)
        self.assertEqual(result.reason, "premature_empty_response")
        self.assertEqual(result.scanned_reviews, 58)


if __name__ == "__main__":
    unittest.main()
