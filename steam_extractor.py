#!/usr/bin/env python3
"""
Steam Extractor - Extracts game reviews from Steam.
"""

import argparse
import requests
import json
import time
from pathlib import Path
from datetime import datetime, date, timezone
from typing import List

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
    """
    Maps user language code to Steam API format.

    Args:
        user_lang: user input (pt-br, en, all, etc.)

    Returns:
        Steam API language code
    """
    user_lang = user_lang.lower().strip()
    return LANGUAGE_MAP.get(user_lang, user_lang)

def parse_date(date_str: str) -> date:
    """
    Parses dd/mm/yyyy format to date object.

    Args:
        date_str: date string (e.g., '30/12/2021')

    Returns:
        date object
    
    Raises:
        ValueError: invalid date format
    """
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").date()
    except ValueError:
        raise ValueError(f"Invalid date format. Use dd/mm/yyyy (e.g. 30/12/2021)")
    
def validate_date_range(start: date, end: date) -> bool:
    """
    Validates start_date <= end_date.

    Returns:
        True if valid
    """
    return start <= end

def fetch_all_reviews(appid: str, language: str, max_reviews: int = 50000) -> List[dict]:
    """
    Robust pagination - fetches ALL reviews with retries.

    Args:
        appid: Game ID
        language: Steam language
        max_reviews: safety limit to prevent infinite loops

    Returns:
        List of ALL reviews dicts
    """
    cursor = "*"
    all_reviews = []
    num_per_page = 100
    seen_cursors = set() # Prevent infinite loops
    retry_count = 0
    max_retries = 5

    print("    Fetching ALL reviews with pagination...")

    while len(all_reviews) < max_reviews:
        for attempt in range(max_retries):
            params = {
                "json": 1,
                "language": language,
                "filter": "all",
                "review_type": "all",
                "purchase_type": "all",
                "num_per_page": num_per_page,
                "cursor": cursor
            }

            try:
                response = requests.get(f"{BASE_URL}{appid}", params=params, timeout=30)
                data = response.json()

                if data.get("success") == 1:
                    page_reviews = data.get("reviews", [])
                    if page_reviews:
                        all_reviews.extend(page_reviews)
                        cursor = data.get("cursor", "*")

                        # Reset retry on success
                        retry_count = 0

                        print(f"Page: {len(all_reviews):,} reviews (cursor: {cursor[:20]}...)")
                        break # Success, next page
                    else:
                        print("No more reviews")
                        return all_reviews
                else:
                    raise ValueError(f"API error: {data}")

            except Exception as e:
                retry_count += 1
                wait_time = 2 ** attempt # 1s, 2s, 4s, 8s...
                print(f"Retry {attempt+1}/{max_retries}: {e} (wait {wait_time}s)")
                time.sleep(wait_time)
                continue
        
        else:
            # All retries failed
            print("Max retries exceeded")
            break

        # Rate limiting + cursor safety
        if cursor in seen_cursors:
            print("Cursor loop detected - slowing down")
            time.sleep(2)
        seen_cursors.add(cursor)

        time.sleep(0.5) # Gentle rate limiting

    return all_reviews

def save_reviews(reviews: list, appid: str, language: str, 
                 start_date: str, end_date: str) -> str:
    """
    Saves reviews to JSON file with automatic filename.

    Args:
        reviews: list of review dicts
        appid, language, dates: for filename

    Returns:
        Filename created
    """
    # Clean filename
    safe_lang = language.replace("/", "_")
    safe_start = start_date.replace("/", "-")
    safe_end = end_date.replace("/", "-")

    filename = f"reviews_{appid}_{safe_lang}_{safe_start}_{safe_end}.json"

    # Save with pretty formatting
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)

    filesize = Path(filename).stat().st_size / 1024 # KB
    print(f"Saved: {filename} ({filesize:.1f} KB)")
    return filename

def filter_reviews_by_date(reviews: list, start_date: date, end_date: date) -> list:
    """
    Filters reviews by timestamp_created date range.

    Args:
        reviews: full list of reviews
        start_date, end_date: date objects

    Returns:
        Filtered reviews within date range
    """
    filtered = []

    for review in reviews:
        timestamp = review.get("timestamp_created")
        if timestamp is None:
            continue

        # Convert Unix timestamp to date
        review_date = datetime.fromtimestamp(timestamp, timezone.utc).date()

        if start_date <= review_date <= end_date:
            filtered.append(review)
    
    return filtered

def main():
    # Create argument parser
    parser = argparse.ArgumentParser(
        description="Steam Extractor - Extracts game reviews from Steam.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  %(prog)s 1091500 pt-br 01/01/2021 31/12/2021    # Brazilian Portuguese only (Cyberpunk 2077)
  %(prog)s 730 all 01/01/2024 31/12/2024          # All languages (CS2)
        """
    )

    # Required arguments
    parser.add_argument("appid", help="Game ID (e.g., 1091500 for Cyberpunk 2077)")
    parser.add_argument("language", help="Language (e.g., pt-br, en-us)")
    parser.add_argument("start_date", help="Start date (dd/mm/yyyy)")
    parser.add_argument("end_date", help="End date (dd/mm/yyyy)")

    # Parse arguments
    args = parser.parse_args()

    # Map language
    steam_lang = map_language(args.language)

    # Parse dates
    try:
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)

        if not validate_date_range(start_date, end_date):
            raise ValueError("start_date must be before or equal to end_date")
        
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    print("Steam Extractor v0.8.0")
    print(f"Game ID: {args.appid}")
    print(f"Language: {steam_lang}")
    print(f"Date range: {args.start_date} to {args.end_date}")

    # Fetch ALL reviews
    try:
        print("    Fetching ALL reviews...")
        all_reviews = fetch_all_reviews(args.appid, steam_lang)

        print("Pagination complete!")
        print(f"Total reviews fetched: {len(all_reviews):,}")

        # Filter by date range
        print("    Filtering by date range...")
        filtered_reviews = filter_reviews_by_date(
            all_reviews, start_date, end_date
        )

        print(f"Filtered reviews: {len(filtered_reviews):,}")

        if filtered_reviews:
            # Show sample
            sample = filtered_reviews[0]
            sample_date = datetime.fromtimestamp(
                sample["timestamp_created"], timezone.utc
            ).strftime("%Y-%m-%d")
            print(f"Sample ({sample_date}): {sample.get('review', 'N/A')[:80]}...")
        
            # Save filtered reviews
            filename = save_reviews(
                filtered_reviews, args.appid, steam_lang, 
                args.start_date, args.end_date
            )
            print(f"Complete! {len(filtered_reviews):,} reviews saved.")
        else:
            print("No reviews found in date range")
    
    except Exception as e:
        print(f"Fetch error: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    main()