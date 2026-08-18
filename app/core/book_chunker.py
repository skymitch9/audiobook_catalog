# app/core/book_chunker.py
# Chapter-anchored chunking at 800 chars / 100 overlap.
#
# ⚠️ THIS IS A PERSISTED-KEY DECISION, NOT A TUNABLE.
# Every `ord` this module emits is the coordinate the spoiler-scoping contract
# compares a reader's position against (design section 4.3). The same number 405
# means "end of chapter 32" at 1,500/200 and "chapter ~15" at 800/100 - the
# pilot measured a ceiling carried across a re-chunk leaking twenty-eight
# chapters of book 2 past the reader's position, with no error anywhere and
# nothing in the answer looking wrong. Changing CHUNK_CHARS or CHUNK_OVERLAP is
# therefore a MIGRATION: bump INGESTER_VERSION, re-ingest every book, and never
# let a pack at one version be read beside a position derived at another.
#
# WHY 800/100 AND NOT THE DESIGN'S ORIGINAL 1,500/200
# ---------------------------------------------------
# Measured 2026-08-18 over Primal Hunter books 1-3 (58.4 h, 205 chapters,
# 3.79 M chars) across three axes that do not agree:
#
#   (a) block integrity - bigger is better  (800/100 alone: 75.7% of stat
#       sheets survive whole; 1,500/200: 94.9%)
#   (b) retrieval precision - smaller is better  (800/100: 6/9 top-3 hits;
#       1,500/200: 4/9; 3,000/300: 4/9)
#   (c) citation precision - smaller is better  (800/100: 27.5 s median error;
#       3,000/300: 102.2 s)
#
# INDEX SMALL, RETURN WIDE resolves all three at once: index at 800/100 and let
# the RETRIEVAL side stitch a hit with its +/-1 neighbours (~2,160 chars), which
# scores 100% block integrity while keeping (b) and (c). ⚠️ The stitch is a
# retrieval-time behaviour and is deliberately NOT done here - this module emits
# small chunks and nothing else. Baking the neighbours into stored chunks would
# reintroduce every problem (b) and (c) measure.
#
# ORD CEILINGS ARE DERIVED, NEVER STORED. Nothing in this module or in a pack
# records a per-reader ceiling; `ord` is a coordinate, and the ceiling is
# recomputed from the reader's position through the chapter table every turn.

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import List, Optional

from app.core.book_text import ExtractedBook, ExtractedChapter

# ⚠️ Persisted-key constants. See the module header before touching either.
CHUNK_CHARS = 800
CHUNK_OVERLAP = 100

_WORD_BOUNDARY_RE = re.compile(r"\s")


@dataclass
class Chunk:
    ord: int
    chapter_index: int
    text: str
    spine_index: Optional[int] = None
    page: Optional[int] = None
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ChapterRef:
    index: int
    title: str
    first_chunk: int
    last_chunk: int
    spine_index: Optional[int] = None
    page: Optional[int] = None
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def split_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Cut `text` into ~`size`-char pieces overlapping by ~`overlap`.

    ⚠️ Cuts land on WORD boundaries. A mid-word cut breaks the lexical path this
    design makes primary: a stat key sliced into "Willpo" + "wer" matches no
    detector and no query, and it is the exact class of term the retrieval
    contract promises to find literally.

    The window always advances by at least one character, so a pathological
    input (one enormous unbroken token) terminates instead of looping.
    """
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be >= 0 and < size")

    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    out: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # Walk back to the last whitespace so the cut is on a word edge.
            window = text[start:end]
            m = None
            for m in _WORD_BOUNDARY_RE.finditer(window):
                pass
            if m and m.start() > overlap:
                end = start + m.start()
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        nxt = end - overlap
        start = nxt if nxt > start else start + 1
    return out


def _interpolate_span(chapter: ExtractedChapter, frac_start: float, frac_end: float):
    """Map a fraction of a chapter's text onto real audio seconds.

    Uses the chapter's own word timings when it has them (transcripts do), which
    makes a chunk's start_sec a real spoken moment rather than arithmetic. Falls
    back to linear interpolation across the chapter's span otherwise.
    """
    if chapter.start_sec is None:
        return None, None
    if chapter.words:
        n = len(chapter.words)
        i = max(0, min(n - 1, int(frac_start * n)))
        j = max(0, min(n - 1, int(frac_end * n)))
        return round(chapter.words[i]["s"], 3), round(chapter.words[j]["e"], 3)
    if chapter.end_sec is None:
        return round(chapter.start_sec, 3), None
    span = chapter.end_sec - chapter.start_sec
    return (round(chapter.start_sec + span * frac_start, 3),
            round(chapter.start_sec + span * frac_end, 3))


def chunk_book(book: ExtractedBook, size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> tuple:
    """Chapter-anchored chunking. Returns (chunks, chapter_refs).

    ⚠️ NO CHUNK EVER SPANS A CHAPTER BOUNDARY. Each chapter is chunked
    independently, so a straddling chunk cannot exist - which is what makes
    every chunk citable ("chapter 19") and scopeable (a ceiling is a chapter
    range). The retrieval side's +/-1 stitch must clamp to
    `first_chunk..last_chunk` for the same reason.
    """
    chunks: List[Chunk] = []
    refs: List[ChapterRef] = []

    for chapter in book.chapters:
        pieces = split_text(chapter.text, size=size, overlap=overlap)
        if not pieces:
            continue
        first = len(chunks)
        total = float(len(chapter.text)) or 1.0
        cursor = 0
        for piece in pieces:
            found = chapter.text.find(piece, max(0, cursor - overlap))
            if found < 0:
                found = cursor
            frac_start = found / total
            frac_end = min(1.0, (found + len(piece)) / total)
            cursor = found + max(1, len(piece) - overlap)

            start_sec, end_sec = _interpolate_span(chapter, frac_start, frac_end)
            chunks.append(
                Chunk(
                    ord=len(chunks),
                    chapter_index=chapter.index,
                    text=piece,
                    spine_index=chapter.spine_index,
                    page=chapter.page,
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
            )
        refs.append(
            ChapterRef(
                index=chapter.index,
                title=chapter.title,
                first_chunk=first,
                last_chunk=len(chunks) - 1,
                spine_index=chapter.spine_index,
                page=chapter.page,
                start_sec=chapter.start_sec,
                end_sec=chapter.end_sec,
            )
        )
    return chunks, refs
