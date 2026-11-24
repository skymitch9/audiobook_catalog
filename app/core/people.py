# app/core/people.py
import re
from typing import Any, Optional


def bytes_to_str(b: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return b.decode(enc).strip()
        except Exception:
            pass
    return b.decode("utf-8", errors="ignore").strip()


def first_str(val: Any) -> Optional[str]:
    v = val[0] if isinstance(val, list) and val else val
    if isinstance(v, bytes):
        return bytes_to_str(v)
    if isinstance(v, tuple):
        return str(v[0])
    return (str(v).strip()) if v is not None else None


# "M.A." / "m.a." / "D.k." / "J.R.R." / "T." — dotted initials.
_INITIALS = re.compile(r"^(?:[^\W\d_]\.)+[^\W\d_]?\.?$")
_ROMAN = re.compile(r"^(?=[IVX])X{0,3}(?:IX|IV|V?I{0,3}),?$")


def _case_word(word: str) -> str:
    """Case ONE word of a person's name.

    ⚠️ Until 2026-11-24 every word went through ``str.capitalize()``, which
    lower-cases everything after the first letter. That is what turned the
    tags ``M.A. Wardell`` / ``D.K. Holmberg`` / ``Bob McGough`` into the
    catalogue's ``M.a. Wardell`` / ``D.k. Holmberg`` / ``Bob Mcgough`` — and
    made the row stop matching its own Drive folder (``M.A. Wardell``, named by
    ``app/author_names.py``, whose per-word rule already kept initials).

    The rule now, in order:
      1. dotted initials are upper-cased (``m.a.`` -> ``M.A.``);
      2. a word the tag already writes in MIXED case with an inner capital
         (``McGough``, ``DeCamp``, ``O'Regan``, ``MacLeod``) is the author's
         own spelling and is kept;
      3. an all-caps token of <= 2 letters (``XX``, ``JJ``, ``EA``) or a
         roman numeral (``III``) is kept — ``DOE`` is not;
      4. anything else is capitalised as before, so ``john`` -> ``John`` and
         ``JANE`` -> ``Jane``.
    """
    if _INITIALS.match(word):
        return word.upper()
    letters = [c for c in word if c.isalpha()]
    if letters and not word.isupper() and not word.islower() and any(c.isupper() for c in word[1:]):
        return word
    if word.isupper() and (len(letters) <= 2 or _ROMAN.match(word)):
        return word
    return word.capitalize()


def normalize_people_field(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    parts = re.split(r"[;,/&]| and ", s, flags=re.IGNORECASE)
    cleaned, seen = [], set()
    for p in parts:
        name = re.sub(r"\s+", " ", p).strip()
        if not name:
            continue
        norm = name if (name.isupper() and len(name) <= 5) else " ".join(_case_word(w) for w in name.split())
        key = norm.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(norm)
    return ", ".join(cleaned) if cleaned else None


# Alias for test compatibility
normalize_people_list = normalize_people_field
