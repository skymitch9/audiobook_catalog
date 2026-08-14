# app/core/review_join.py
# The audiobook side's own review-join key, and a live (read-only) count of
# reviews joined to it today - what edit_overrides.py's key-move guard (Phase
# A2, catalog-platform/docs/info/edit-audit-design.md sec 3.4 and sec 6) warns
# about before a title/author edit silently orphans a book's reviews.
#
# Two related keys, and why both appear:
#   * bookId  - what THIS repo's own site (site/reviews.js:bookIdFromTitle)
#               actually joins reviews on today. Every review doc stores it,
#               so it is the only one a live query can count.
#   * workKey - the estate-wide join (library_catalog/packages/core/src/
#               titles.ts:workKeyFor) that the library side's book page and
#               read-state sweep use. No review doc carries this field yet
#               (0 of ~870 measured 2026-08-14, per the design doc) - it can
#               only be COMPUTED and shown, never queried directly.
#
# Both move together whenever title or author changes, which is the point of
# the warning: a retitle here breaks the same book's join on both sides, just
# through two different mechanisms - one countable today, one not yet.
#
# Nothing here ever writes. A failed read returns None ("unknowable"), never
# an exception - this is a warning aid on top of an edit the owner is trying
# to make, not a gate that should ever crash it.

from __future__ import annotations

import json
import re
import unicodedata
import urllib.error
import urllib.request
from typing import Optional

FIRESTORE_BASE = "https://firestore.googleapis.com/v1/projects/audiobook-catalog/databases/(default)/documents"

_SLUG_NONALNUM_RE = re.compile(r"[^a-z0-9]+")
_SLUG_DASH_RUN_RE = re.compile(r"-{2,}")
_SLUG_EDGE_DASH_RE = re.compile(r"^-|-$")

_ARTICLE_RE = re.compile(r"^(the|a|an)\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_MULTI_SPACE_RE = re.compile(r"\s{2,}")
_AUTHOR_SPLIT_RE = re.compile(r"[;,/&]|\sand\s", re.IGNORECASE)
_AUTHOR_ROLE_RE = re.compile(r"\s*-\s*(Translator|Narrator|Editor)\s*$", re.IGNORECASE)


def book_id_from_title(title: Optional[str]) -> str:
    """
    Mirror of site/reviews.js:bookIdFromTitle - what the site's own review
    join actually uses today: lowercase, collapse any run of non-alphanumeric
    characters to one hyphen, trim leading/trailing hyphens. Keeps a leading
    article ("The Lake House" -> "the-lake-house"), unlike normalise_title
    below.

    Re-derived here rather than imported, because the JS runs in the browser
    only; app/tools/import_pagebound_reviews.py:book_id() is the same fold,
    already ported once for the same reason. Changing this is a migration
    (it produces stored Firestore document ids), not a refactor.
    """
    t = (title or "").lower()
    t = _SLUG_NONALNUM_RE.sub("-", t)
    t = _SLUG_DASH_RUN_RE.sub("-", t)
    return _SLUG_EDGE_DASH_RE.sub("", t)


def normalise_title(raw: Optional[str]) -> str:
    """
    Port of library_catalog/packages/core/src/titles.ts:normaliseTitle.

    NFD-fold diacritics away, lowercase, '&' -> ' and ', collapse every
    non-alphanumeric run to a single space, drop a leading article, collapse
    whitespace. Kept in lockstep with the TS source by hand - there is no
    package shared across the two runtimes (Python here, Node in
    library_catalog) - so a change to titles.ts::normaliseTitle must be
    mirrored here too.
    """
    t = unicodedata.normalize("NFD", raw or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower().replace("&", " and ")
    t = _NON_ALNUM_RE.sub(" ", t)
    t = _ARTICLE_RE.sub("", t)
    t = _MULTI_SPACE_RE.sub(" ", t)
    return t.strip()


def split_authors(raw: Optional[str]) -> list:
    """Port of titles.ts:splitAuthors - the audiobook catalog's own display
    rule for a multi-author field, with a trailing '- Translator/Narrator/
    Editor' credit stripped so it can never become the primary author."""
    parts = [_AUTHOR_ROLE_RE.sub("", p).strip() for p in _AUTHOR_SPLIT_RE.split(raw or "")]
    return [p for p in parts if p]


def primary_author(raw: Optional[str]) -> str:
    """Port of titles.ts:primaryAuthor - the first credited name, falling
    back to the raw (stripped) string when splitting yields nothing, so a
    strange author field still folds into A key rather than an empty half."""
    names = split_authors(raw)
    return names[0] if names else (raw or "").strip()


def work_key_for(title: Optional[str], author: Optional[str]) -> str:
    """
    Port of titles.ts:workKeyFor - 'normaliseTitle(title)|normaliseTitle(
    primaryAuthor(author))', the estate-wide review-join key.

    NOT workKeyForAudiobookRow: that variant additionally strips series/
    volume decoration from the title (cleanTitleWithSeries) before folding.
    This module does not replicate that step - edit_overrides.py already
    works with the corrected, series-free title field (CORRECTABLE_FIELDS),
    not raw decorated tag text, so the gap only matters for a title that
    still carries series/volume wording at the moment of the edit. The
    key-move warning says so rather than silently claiming exactness (also
    unlike the library side, this catalog has no authorless sentinel -
    every credited book here has a real author string).
    """
    return f"{normalise_title(title)}|{normalise_title(primary_author(author))}"


def count_reviews_for_book_id(book_id: str, timeout: float = 15.0) -> Optional[int]:
    """
    How many review docs are joined to this book today, via the SAME bookId
    field site/reviews.js queries on. A single read-only Firestore structured
    query - never a write, never a scan of the whole collection.

    Returns None ("unknowable") on ANY failure - offline, timeout, a changed
    API shape - rather than raising. See the module docstring: this backs a
    warning, and a network hiccup must never be the reason an edit fails.
    """
    if not book_id:
        return None
    from app.tools.club_books import API_KEY as _fs_key  # local import: avoid a hard dep at module load

    body = {
        "structuredQuery": {
            "from": [{"collectionId": "reviews"}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": "bookId"},
                    "op": "EQUAL",
                    "value": {"stringValue": book_id},
                }
            },
        }
    }
    url = f"{FIRESTORE_BASE}:runQuery?key={_fs_key}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    return sum(1 for row in rows if isinstance(row, dict) and "document" in row)
