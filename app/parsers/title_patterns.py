# app/parsers/title_patterns.py
# Centralized construction of regex patterns used to parse series/index from titles.
# No parsing logic here—just the compiled patterns.

import re
from typing import List, Pattern


def build_title_patterns() -> List[Pattern]:
    """
    Conservative fallback patterns (used only if tags don't give series/index).
    Order matters: more specific/structured patterns first, then looser ones.
    """
    return [
        # 0a) "Book Title: Series Name, Book N" — the shape Audible's own
        #     container tags carry ("SOS Hotel: For a Supernaturally Safe
        #     Stay!: SOS Hotel, Book 1"). An explicit ", Book N" at the very
        #     END is the strongest signal a title gives, so it runs before the
        #     looser "Series N: Title" form below — otherwise "Mother of
        #     Learning Arc 1: Mother of Learning, Book 1" reads as the series
        #     "Mother of Learning Arc". The series is the text after the LAST
        #     colon, so a subtitle colon earlier in the title is not swallowed.
        #     Added 2026-11-24 (27 Audible downloads had no series at all).
        #     ⚠️ The series may not contain " - ": "The Gender Game 5: The
        #     Gender Fall - The Gender Game, Book 5" must fall through to (1),
        #     which reads it correctly as "The Gender Game".
        re.compile(
            r"""
            ^.+?:\s*
            (?P<series>(?:(?!\s[-–—]\s)[^:()])+?)\s*,\s*
            (?:book|bk\.?|volume|vol\.?)\s*
            (?P<idx>\d+(?:\.\d+)?)
            \s*$
        """,
            re.IGNORECASE | re.X,
        ),
        # 0b) "Book Title (Series Name, Book N)" — the parenthesised form
        #     ("The Hedge Wizard: A LitRPG ... (The Hedge Wizard, Book 1)").
        re.compile(
            r"""
            ^.+?\(\s*
            (?P<series>[^(),]+?)\s*,\s*
            (?:book|bk\.?|volume|vol\.?)\s*
            (?P<idx>\d+(?:\.\d+)?)
            \s*\)\s*$
        """,
            re.IGNORECASE | re.X,
        ),
        # 1) "Series Name #N: Book Title" (e.g., "The Gender Game 2: The Gender Secret")
        re.compile(
            r"""
            ^\s*
            (?P<series>.+?)\s+
            (?P<idx>\d+)\s*:\s*
            .+
            \s*$
        """,
            re.IGNORECASE | re.X,
        ),
        # 2) "Book Title - Series Name, Book N" (most common pattern)
        re.compile(
            r"""
            ^.+?\s[-–—]\s
            (?P<series>[^,()]+?)\s*,\s*
            (?:(?:book|bk\.?|volume|vol\.?|novella|part)s?\s*)
            (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
            (?:\s*$|[^\w])
        """,
            re.IGNORECASE | re.X,
        ),
        # 3) "Series Name (Book N)" / "Series Name (Volume N)" / "Series Name (Part N)"
        re.compile(
            r"""
            ^\s*
            (?P<series>.+?)
            \s*\(\s*
            (?:(?:book|bk\.?|volume|vol\.?|novella|part)\s*)
            (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
            \s*\)
            \s*$
        """,
            re.IGNORECASE | re.X,
        ),
        # 4) "Title (Series Name #N)"
        re.compile(
            r"""
            .+?\(
            (?P<series>[^()#]+?)\s*[,#]?\s*#\s*
            (?P<idx>\d+|[IVXLCM]+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
            \)
        """,
            re.IGNORECASE | re.X,
        ),
        # 5) "Series Name: Book N" / "Series Name: Volume N"
        re.compile(
            r"""
            ^\s*
            (?P<series>[^:()]+?)\s*:\s*
            (?:(?:book|bk\.?|volume|vol\.?|novella|part)s?\s*)
            (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
            (?:\s*$|[^\w])
        """,
            re.IGNORECASE | re.X,
        ),
        # 6) "Title - Series Name: Book N"
        re.compile(
            r"""
            ^.+?\s[-–—]\s
            (?P<series>[^:()]+?)\s*:\s*
            (?:(?:book|bk\.?|volume|vol\.?|novella|part)s?\s*)
            (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
            (?:\s*$|[^\w])
        """,
            re.IGNORECASE | re.X,
        ),
        # 7) "Title - Book N of the Series Name"
        re.compile(
            r"""
            ^.+?\s[-–—]\s
            (?:(?:book|bk\.?|volume|vol\.?|novella|part)\s*(?P<idx>[IVXLCM]+|\d+|[A-Za-z]+))\s*
            (?:of|in)\s+(?:the\s+)?
            (?P<series>[^,()]+?)\s*$
        """,
            re.IGNORECASE | re.X,
        ),
        # 8) "Series Name: N" (minimal colon pattern - be more restrictive)
        re.compile(
            r"""
            ^\s*
            (?P<series>[^:()]+?)\s*:\s*
            (?P<idx>[IVXLCM]+|\d+)
            \s*$
        """,
            re.IGNORECASE | re.X,
        ),
    ]


def build_exclusion_patterns() -> List[Pattern]:
    """
    Patterns that should NOT be considered series books.
    These help avoid false positives.
    """
    return [
        # Movie/TV tie-ins
        re.compile(r"\((?:movie|tv|television)\s+tie[-\s]?in\)", re.IGNORECASE),
        # Special editions
        re.compile(r"\((?:special|deluxe|collector'?s?|anniversary|limited)\s+edition\)", re.IGNORECASE),
        # Unabridged/Abridged
        re.compile(r"\((?:un)?abridged\)", re.IGNORECASE),
        # Standalone novels with descriptive parentheses
        re.compile(r"\([^)]*(?:novel|story|tale|memoir|biography|autobiography)[^)]*\)", re.IGNORECASE),
        # Years in parentheses
        re.compile(r"\(\d{4}\)", re.IGNORECASE),
        # Publisher/format info
        re.compile(r"\((?:audible|kindle|paperback|hardcover|audio)\s+[^)]*\)", re.IGNORECASE),
    ]
