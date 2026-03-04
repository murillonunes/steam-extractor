#!/usr/bin/env python3
"""
Steam Extractor - Extracts game reviews from Steam.
"""

import argparse
import requests
import json
import time
import pandas as pd
from datetime import datetime, date, timezone
from typing import List, Dict

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

def timestamp_to_ddmmyyyy(timestamp: int | None) -> str | None:
    """
    Convert Unix timestamp to dd/mm/yyyy string (UTC timezone).
    """
    if timestamp is None:
        return None
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
    return dt.strftime("%d/%m/%Y")

def fetch_all_reviews(appid: str, language: str, max_reviews: int = 2000000) -> List[dict]:
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
    seen_cursors = set()
    max_retries = 5

    print("    Fetching ALL reviews with pagination...")

    last_progress = 0

    while len(all_reviews) < max_reviews:
        for attempt in range(max_retries):
            params = {
                "json": 1,
                "language": language,
                "filter": "recent",
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

                        milestone = (len(all_reviews) // 500) * 500
                        if milestone > last_progress:
                            print(f"Progress: {len(all_reviews):,} reviews fetched...")
                            last_progress = milestone
                        break
                    else:
                        print("No more reviews")
                        return all_reviews
                else:
                    raise ValueError(f"API error: {data}")

            except Exception as e:
                wait_time = 2 ** attempt # 1s, 2s, 4s, 8s...
                print(f"Retry {attempt+1}/{max_retries}: {e} (wait {wait_time}s)")
                time.sleep(wait_time)
                continue
        
        else:
            print("Max retries exceeded")
            break

        # Rate limiting + cursor safety
        if cursor in seen_cursors:
            print("Cursor loop detected - slowing down...")
            time.sleep(2)
        seen_cursors.add(cursor)
        time.sleep(0.5)

    return all_reviews

def save_reviews(reviews: List[Dict], filename: str, format_type: str = 'json'):
    """
    Save reviews to JSON or CSV with timestamps converted to dd/mm/yyyy.

    Args:
        reviews: list of review dicts
        filename: base filename (auto-append .json or .csv)
        format_type: 'json' or 'csv'
    """
    processed_reviews = []
    for r in reviews:
        processed = r.copy()
        # Convert all timestamps to dd/mm/yyyy strings
        processed['date_created'] = timestamp_to_ddmmyyyy(r.get('timestamp_created'))
        processed['date_updated'] = timestamp_to_ddmmyyyy(r.get('timestamp_updated'))
        release_ts = r.get('release_date', {}).get('date')
        processed['date_release'] = timestamp_to_ddmmyyyy(release_ts)
        author = processed.setdefault('author', {})
        author['last_played_date'] = timestamp_to_ddmmyyyy(author.get('last_played'))
        processed_reviews.append(processed)

    if format_type == 'csv':
        df = pd.json_normalize(processed_reviews)
        output_file = f"{filename}.csv"
        df.to_csv(output_file, index=False, encoding="utf-8")
        print(f"Saved CSV: {output_file} ({len(df)} rows, {len(df.columns)} columns)")
    else:
        output_file = f"{filename}.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(processed_reviews, f, ensure_ascii=False, indent=2)
        print(f"Saved JSON: {output_file} ({len(processed_reviews)} reviews)")

def filter_reviews_by_date(reviews: List[Dict], start_date: date, end_date: date) -> List[Dict]:
    """
    Filter reviews within date range using timestamp_created.
    """
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

    parser.add_argument("appid", help="Game ID (e.g., 1091500 for Cyberpunk 2077)")
    parser.add_argument("language", help="Language (e.g., pt-br, en-us)")
    parser.add_argument("start_date", help="Start date (dd/mm/yyyy)")
    parser.add_argument("end_date", help="End date (dd/mm/yyyy)")
    parser.add_argument(
        "--format", "-f",
        choices=["json", "csv"],
        default="json",
        help="Output format (json/csv, default: json)"
    )

    args = parser.parse_args()

    steam_lang = map_language(args.language)

    try:
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)

        if start_date > end_date:
            raise ValueError("start_date must be before or equal to end_date")
        
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    print("Steam Extractor v1.0.0")
    print(f"Game ID: {args.appid}")
    print(f"Language: {steam_lang}")
    print(f"Date range: {args.start_date} to {args.end_date}")

    try:
        print("    Fetching ALL reviews...")
        all_reviews = fetch_all_reviews(args.appid, steam_lang)

        print("Pagination complete!")
        print(f"Total reviews fetched: {len(all_reviews):,}")
        
        print("    Filtering by date range...")
        filtered_reviews = filter_reviews_by_date(
            all_reviews, start_date, end_date
        )

        print(f"Filtered reviews: {len(filtered_reviews):,}")

        if filtered_reviews:
            sample = filtered_reviews[0]
            sample_date = datetime.fromtimestamp(
                sample["timestamp_created"], timezone.utc
            ).strftime("%Y-%m-%d")
            print(f"Sample ({sample_date}): {sample.get('review', 'N/A')[:80]}...")
        
            safe_start = args.start_date.replace("/", "-")
            safe_end = args.end_date.replace("/", "-")            
            save_reviews(filtered_reviews, 
                         f"reviews_{args.appid}_{steam_lang}_{safe_start}_{safe_end}", 
                         args.format)

            print(f"Complete! {len(filtered_reviews):,} reviews saved.")
        else:
            print("No reviews found in date range")
    
    except Exception as e:
        print(f"Fetch error: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    main()