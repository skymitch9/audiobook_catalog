# app/parsers/title_patterns.py
# Centralized construction of regex patterns used to parse series/index from titles.
# No parsing logic here—just the compiled patterns.

import re
from typing import List, Pattern

def build_title_patterns() -> List[Pattern]:
    """
    Conservative fallback patterns (used only if tags don’t give series/index).
    Order matters: more specific/structured patterns first, then looser ones.
    """
    return [
        # 1) "Series (Volume 1)" / "Series (Book 2)" / "Series (Part IV)"
        re.compile(r"""
            ^\s*
            (?P<series>.+?)
            \s*\(\s*
            (?:(?:book|bk\.?|volume|vol\.?|novella|part)\s*)
            (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
            \s*\)
            \s*$
        """, re.IGNORECASE | re.X),

        # 2) "Title (Series #3)"
        re.compile(r"""
            .+?\(
            (?P<series>[^()#]+?)\s*[,#]?\s*#\s*
            (?P<idx>\d+|[IVXLCM]+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
            \)
        """, re.IGNORECASE | re.X),

        # 3) "Title – Series, Book 3"
        re.compile(r"""
            .+?\s[-–—]\s
            (?P<series>[^,()]+?)\s*,\s*
            (?:(?:book|bk\.?|volume|vol\.?|novella|part)s?\s*)
            (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?(?:\s*[-–—]\s*\d+)?)
            \b
        """, re.IGNORECASE | re.X),

        # 4) Colon w/ keyword — "Title - Series: Novella 1" / "Series: Book 3"
        re.compile(r"""
            (?:^.+?\s[-–—]\s)?     # optional "Title – "
            (?P<series>[^:()]+?)\s*:\s*
            (?:(?:book|bk\.?|volume|vol\.?|novella|part)s?\s*)
            (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
            (?:\b|$)
        """, re.IGNORECASE | re.X),

        # 5) Minimal colon — "Title - Series: 3"
        re.compile(r"""
            (?:^.+?\s[-–—]\s)?
            (?P<series>[^:()]+?)\s*:\s*
            (?P<idx>[IVXLCM]+|\d+|[A-Za-z]+(?:\s+[A-Za-z]+)?)
            (?:\b|$)
        """, re.IGNORECASE | re.X),

        # 6) "Title - Book Five of the Stormlight Archive"
        re.compile(r"""
            .+?\s[-–—]\s
            (?:(?:book|bk\.?|volume|vol\.?|novella|part)\s*(?P<idx>[IVXLCM]+|\d+|[A-Za-z]+))\s*
            (?:of|in)\s+the\s+
            (?P<series>[^,()]+?)\s*$
        """, re.IGNORECASE | re.X),
    ]
