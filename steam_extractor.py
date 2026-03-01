#!/usr/bin/env python3
"""
Steam Extractor - Extracts game reviews from Steam.
"""

import argparse
from pathlib import Path

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

def main():
    # Create argument parser
    parser = argparse.ArgumentParser(
        description="Steam Extractor - Extracts game reviews from Steam.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  %(prog)s 1091500 pt-br    # Brazilian Portuguese only (Cyberpunk 2077)
  %(prog)s 1091500 all      # All languages (Cyberpunk 2077)
  %(prog)s 730 en           # English only (CS2)
        """
    )

    # Required arguments
    parser.add_argument("appid", help="Game ID (e.g., 1091500 for Cyberpunk 2077)")
    parser.add_argument("language", help="Language (e.g., pt-br, en-us)")

    # Parse arguments
    args = parser.parse_args()

    # Map language
    steam_lang = map_language(args.language)

    print("Steam Extractor v0.3.0")
    print(f"Game ID: {args.appid}")
    print(f"Input language: {args.language}")
    print(f"Steam language: {steam_lang}")
    print("Language mapping completed!")

if __name__ == "__main__":
    main()