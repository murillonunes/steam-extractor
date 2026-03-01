#!/usr/bin/env python3
"""
Steam Extractor - Extracts game reviews from Steam.
"""

import argparse
from pathlib import Path
from datetime import datetime, date

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

    print("Steam Extractor v0.4.0")
    print(f"Game ID: {args.appid}")
    print(f"Input language: {args.language}")
    print(f"Steam language: {steam_lang}")
    print(f"Date range: {args.start_date} to {args.end_date}")
    print(f"Period: {start_date} to {end_date}")
    print("Language mapping completed!")
    print("Date parsing completed!")

if __name__ == "__main__":
    main()