# app/core/book_text.py
# Text extraction for the book-knowledge ingester: EPUB, PDF and Whisper
# transcript -> a common `ExtractedBook` shape the chunker consumes.
#
# Design of record: catalog-platform/docs/info/gabi-book-knowledge-design.md
# sections 7.2 (EPUB), 1.1 (the PDF scan finding), 7.3 (chapter anchoring).
#
# THREE SOURCES, ONE SHAPE
# ------------------------
# Everything here returns `ExtractedBook(chapters=[ExtractedChapter, ...])`
# where a chapter carries its own text plus whatever positional anchor its
# format can honestly supply:
#
#     epub        -> spine_index   (section 4.4 - the CFI joint)
#     pdf-text    -> page          (exact)
#     transcript  -> start_sec/end_sec from chapters.json (exact, from the
#                    m4b container - NOT from Whisper's own segment times)
#
# WHY THE SPINE IS READ AND NOT `sorted(namelist())`
# --------------------------------------------------
# The design measured that a sorted-namelist walk happens to work on THIS
# shelf and warned it will not generalise. It is also a persisted-key hazard:
# `spine_index` is stored in every EPUB chunk and a re-ingest that orders
# documents differently is a MIGRATION, not an edit (section 4.4). So the OPF
# spine is the ordering of record here, and `ingester_version` in the pack is
# what lets a future session detect a pack built under the old rule.
#
# WHY A PDF'S TEXT YIELD IS MEASURED RATHER THAN ASSUMED
# ------------------------------------------------------
# 25 of the estate's 30 PDFs are image-only scans (measured; The Way of Kings
# is 48.6 MB and yields 640 bytes). Owner amendment 2026-08-18: those are not
# "deliberately absent" any more, they are RE-QUEUED behind the reviewed
# audiobooks with a needs-ocr marker. A file is classified by what it actually
# extracts, never by its extension - a PDF that looks text-bearing and yields
# garbage counts as complicated. See `classify_pdf`.

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Sequence
from xml.etree import ElementTree as ET

# A PDF must clear BOTH bars to count as text-bearing. The design measured the
# scan population's median at 6,304 bytes and the five real text PDFs carrying
# ~3.9 MB between them, so the gap is wide and these thresholds sit inside it.
PDF_MIN_TOTAL_CHARS = 20_000
PDF_MIN_CHARS_PER_PAGE = 200.0

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")
_HEADING_RE = re.compile(r"^\s*(chapter|prologue|epilogue|part|interlude)\b", re.I)


@dataclass
class ExtractedChapter:
    """One chapter's worth of text plus its format-native anchor."""

    index: int
    title: str
    text: str
    spine_index: Optional[int] = None
    page: Optional[int] = None
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None
    # Word-level timings, transcript only: [{"w":..., "s":..., "e":...}, ...].
    # Used to give a chunk a real start_sec rather than interpolating.
    words: Optional[List[dict]] = None


@dataclass
class ExtractedBook:
    book_id: str
    title: str
    source: str  # "epub" | "pdf-text" | "transcript"
    chapters: List[ExtractedChapter] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def text_bytes(self) -> int:
        return sum(len(c.text.encode("utf-8")) for c in self.chapters)

    @property
    def full_text(self) -> str:
        return "\n\n".join(c.text for c in self.chapters)


# --------------------------------------------------------------------------
# shared cleaning
# --------------------------------------------------------------------------

def clean_text(raw: str) -> str:
    """Collapse horizontal whitespace, cap blank-line runs, strip edges.

    Deliberately preserves single newlines: a LitRPG stat block is a run of
    short lines and the detector the retrieval side will run keys on them
    (design section 6.1). Flattening them into a paragraph would destroy the one
    structural signal that class of question depends on.
    """
    t = raw.replace("\r\n", "\n").replace("\r", "\n")
    t = _WS_RE.sub(" ", t)
    t = "\n".join(line.strip() for line in t.split("\n"))
    t = _NL_RE.sub("\n\n", t)
    return t.strip()


def _html_to_text(markup: str) -> str:
    """Prefer a real parser; fall back to a tag strip if bs4 is unavailable.

    The design's 5-second measurement was taken with a regex, and explicitly
    said that is a cost-class measurement and not a recommendation to ship one.
    bs4 is already a repo dependency, so the parser is the normal path and the
    regex exists only so a machine without it degrades instead of crashing.
    """
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        return clean_text(_TAG_RE.sub(" ", markup))

    soup = BeautifulSoup(markup, "html.parser")
    for bad in soup(["script", "style"]):
        bad.decompose()
    # Block-level tags become newlines so headings and paragraphs do not run
    # together into one wall of prose.
    for tag in soup.find_all(["p", "div", "br", "h1", "h2", "h3", "h4", "li", "tr"]):
        tag.append("\n")
    return clean_text(soup.get_text())


def _title_from_markup(markup: str, fallback: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(markup, "html.parser")
        for sel in ("h1", "h2", "h3", "title"):
            el = soup.find(sel)
            if el:
                t = clean_text(el.get_text())
                if t and len(t) < 200:
                    return t
    except Exception:
        pass
    return fallback


# --------------------------------------------------------------------------
# EPUB
# --------------------------------------------------------------------------

def _opf_path(zf: zipfile.ZipFile) -> Optional[str]:
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
    except KeyError:
        return None
    try:
        root = ET.fromstring(container)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.endswith("rootfile"):
            path = el.attrib.get("full-path")
            if path:
                return path
    return None


def _spine_hrefs(zf: zipfile.ZipFile) -> List[str]:
    """Document order from the OPF spine. Empty list = no usable spine."""
    opf = _opf_path(zf)
    if not opf:
        return []
    try:
        root = ET.fromstring(zf.read(opf).decode("utf-8", "replace"))
    except (KeyError, ET.ParseError):
        return []

    base = os.path.dirname(opf)
    manifest: dict = {}
    spine: List[str] = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "item":
            iid, href = el.attrib.get("id"), el.attrib.get("href")
            media = (el.attrib.get("media-type") or "").lower()
            if iid and href and ("html" in media or href.lower().endswith((".xhtml", ".html", ".htm"))):
                manifest[iid] = href
        elif tag == "itemref":
            idref = el.attrib.get("idref")
            if idref:
                spine.append(idref)

    names = set(zf.namelist())
    out: List[str] = []
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        full = os.path.normpath(os.path.join(base, href)).replace("\\", "/")
        # An EPUB may percent-encode hrefs; try both forms before giving up.
        if full not in names:
            from urllib.parse import unquote

            alt = os.path.normpath(os.path.join(base, unquote(href))).replace("\\", "/")
            full = alt if alt in names else full
        if full in names:
            out.append(full)
    return out


def extract_epub(path: str, book_id: str, title: str) -> ExtractedBook:
    """One chapter per spine document, in spine order.

    Spine document == chapter is the mapping section 4.4 relies on: it is the
    granularity a stored CFI can be resolved to, and finer precision would mean
    running foliate-js server-side, which the design rules out.
    """
    book = ExtractedBook(book_id=book_id, title=title, source="epub")
    with zipfile.ZipFile(path) as zf:
        hrefs = _spine_hrefs(zf)
        if not hrefs:
            book.notes.append("no OPF spine; fell back to sorted namelist order")
            hrefs = sorted(
                n for n in zf.namelist()
                if n.lower().endswith((".xhtml", ".html", ".htm"))
            )
        for spine_index, href in enumerate(hrefs):
            try:
                markup = zf.read(href).decode("utf-8", "replace")
            except KeyError:
                continue
            text = _html_to_text(markup)
            if not text.strip():
                continue
            book.chapters.append(
                ExtractedChapter(
                    index=len(book.chapters),
                    title=_title_from_markup(markup, f"Section {spine_index + 1}"),
                    text=text,
                    spine_index=spine_index,
                )
            )
    return book


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

def classify_pdf(path: str) -> dict:
    """Measure a PDF's real text yield and say whether it is ingestible.

    Returns {ok, pages, chars, chars_per_page, reason}. `ok=False` means the
    file needs OCR - which this build deliberately does NOT provide (owner,
    2026-08-18). The caller queues it with a needs-ocr marker rather than
    recording a failure.
    """
    try:
        import fitz  # PyMuPDF
    except Exception as exc:  # pragma: no cover - depends on the machine
        return {"ok": False, "pages": 0, "chars": 0, "chars_per_page": 0.0,
                "reason": f"PyMuPDF unavailable ({exc})"}

    try:
        doc = fitz.open(path)
    except Exception as exc:
        return {"ok": False, "pages": 0, "chars": 0, "chars_per_page": 0.0,
                "reason": f"unreadable ({exc})"}

    chars = 0
    pages = doc.page_count
    with doc:
        for page in doc:
            chars += len(page.get_text("text") or "")
    cpp = (chars / pages) if pages else 0.0

    if chars < PDF_MIN_TOTAL_CHARS:
        reason = f"image-scan: {chars} chars total, under {PDF_MIN_TOTAL_CHARS}"
    elif cpp < PDF_MIN_CHARS_PER_PAGE:
        reason = f"image-scan: {cpp:.0f} chars/page, under {PDF_MIN_CHARS_PER_PAGE:.0f}"
    else:
        reason = "text-bearing"
    return {"ok": reason == "text-bearing", "pages": pages, "chars": chars,
            "chars_per_page": round(cpp, 1), "reason": reason}


def extract_pdf(path: str, book_id: str, title: str) -> ExtractedBook:
    """Text-layer PDF -> chapters from the TOC where one exists, else one
    pseudo-chapter per page run. Every chunk carries an exact `page`."""
    import fitz

    book = ExtractedBook(book_id=book_id, title=title, source="pdf-text")
    doc = fitz.open(path)
    with doc:
        page_text = [(i + 1, clean_text(p.get_text("text") or "")) for i, p in enumerate(doc)]
        try:
            toc = doc.get_toc() or []
        except Exception:
            toc = []

    starts: List[tuple] = []
    for entry in toc:
        if len(entry) >= 3 and isinstance(entry[2], int) and entry[2] > 0:
            starts.append((int(entry[2]), str(entry[1])))
    starts.sort()

    if starts:
        bounds = []
        for i, (pg, name) in enumerate(starts):
            end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(page_text)
            bounds.append((pg, end, name))
    else:
        bounds = [(1, len(page_text), title)]

    for idx, (first, last, name) in enumerate(bounds):
        pages = [(n, t) for n, t in page_text if first <= n <= last and t.strip()]
        if not pages:
            continue
        book.chapters.append(
            ExtractedChapter(
                index=len(book.chapters),
                title=clean_text(name) or f"Section {idx + 1}",
                text="\n\n".join(t for _, t in pages),
                page=pages[0][0],
            )
        )
    return book


# --------------------------------------------------------------------------
# Whisper transcript
# --------------------------------------------------------------------------

def extract_transcript(
    transcript_path: str,
    book_id: str,
    title: str,
    chapter_table: Optional[Sequence[dict]] = None,
) -> ExtractedBook:
    """Whisper JSON -> chapter-partitioned text anchored on `chapters.json`.

    THE ANCHOR OF RECORD IS THE CONTAINER, NOT WHISPER (design section 7.4).
    `chapters.json`'s `start_sec` comes from the m4b's own chapter atom and is
    exact; Whisper's word times position text WITHIN a chapter but never decide
    where a chapter begins. The pilot measured the spoken "Chapter N" landing a
    mean +0.27 s after the container's boundary with no accumulation over 20
    hours, so partitioning the word stream by container time is accurate to
    well under a second - and it cannot drift, which the alternative can.

    With no chapter table the whole book becomes one chapter; that is honest
    (the timestamps are still real) and the chunker still cuts it at 800/100.
    """
    with open(transcript_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    words: List[dict] = []
    for seg in payload.get("segments", []):
        segment_words = seg.get("words") or []
        if segment_words:
            words.extend(
                {"w": w.get("w", ""), "s": float(w.get("s", 0.0)), "e": float(w.get("e", 0.0))}
                for w in segment_words
            )
        else:
            # A segment with no word timings still carries text and a span.
            words.append({"w": seg.get("text", ""), "s": float(seg.get("start", 0.0)),
                          "e": float(seg.get("end", 0.0))})

    book = ExtractedBook(book_id=book_id, title=title, source="transcript")
    if not words:
        book.notes.append("transcript carried no segments")
        return book

    audio_end = words[-1]["e"]
    table = [c for c in (chapter_table or []) if c.get("start_sec") is not None]
    if not table:
        book.notes.append("no chapters.json entry; whole book is one chapter")
        table = [{"title": title, "start_sec": 0.0}]

    table = sorted(table, key=lambda c: float(c["start_sec"]))
    for i, chap in enumerate(table):
        start = float(chap["start_sec"])
        end = float(table[i + 1]["start_sec"]) if i + 1 < len(table) else audio_end + 1.0
        # A word belongs to the chapter its START falls inside.
        chunk_words = [w for w in words if start <= w["s"] < end]
        if not chunk_words:
            continue
        text = clean_text("".join(w["w"] for w in chunk_words))
        if not text:
            continue
        book.chapters.append(
            ExtractedChapter(
                index=len(book.chapters),
                title=str(chap.get("title") or f"Chapter {i + 1}"),
                text=text,
                start_sec=round(chunk_words[0]["s"], 3),
                end_sec=round(chunk_words[-1]["e"], 3),
                words=chunk_words,
            )
        )
    return book


# --------------------------------------------------------------------------
# per-book alias map (transcripts only)
# --------------------------------------------------------------------------

def build_alias_map(book: ExtractedBook, min_count: int = 3) -> dict:
    """Proper-noun variant counts for THIS book, for the retrieval side.

    ⚠️ PER BOOK, NEVER PER SERIES - the pilot measured `Villy` splitting four
    ways in Primal Hunter 3 while book 2 rendered only `Vili`, so a map authored
    once from book 1 silently misses 9 of 31 mentions by book 3 (design section
    6.4). Building it from the book's own transcript is the only shape that
    cannot go stale.

    This records CANDIDATES with counts. It does not decide that two spellings
    are the same name - that needs a human or a glossary, and the design's
    `Sylphian Ayas` -> "Sylphie and Ayas" finding shows why a machine guess is
    dangerous: that one is a meaning change, not a spelling variant.
    """
    counts: dict = {}
    for chapter in book.chapters:
        # ⚠️ Only tokens NOT at a sentence start count. English capitalises the
        # first word of every sentence, so a naive scan returns `The` 1,419
        # times and buries the actual names - measured on Primal Hunter 2, where
        # `The`, `His`, `But` and `And` all outranked `Viper`. Requiring a
        # preceding word that is not sentence-final is what separates a proper
        # noun from a capitalised article.
        for match in re.finditer(r"(?<![.!?\"\n]\s)(?<!^)\b([A-Z][a-z]{2,})\b",
                                 chapter.text, re.MULTILINE):
            token = match.group(1)
            if token.lower() in _ALIAS_STOPWORDS:
                continue
            counts[token] = counts.get(token, 0) + 1
    return {k: v for k, v in sorted(counts.items(), key=lambda kv: -kv[1]) if v >= min_count}


# Capitalised words that are grammar, not names. Kept short and literal: this is
# a denoiser for a CANDIDATE list, not a linguistic model.
_ALIAS_STOPWORDS = {
    "the", "and", "but", "his", "her", "its", "their", "they", "this", "that",
    "then", "there", "these", "those", "she", "him", "you", "your", "not",
    "for", "with", "was", "were", "had", "has", "have", "did", "does", "what",
    "when", "where", "who", "why", "how", "all", "one", "two", "even", "just",
    "still", "after", "before", "however", "instead", "though", "while",
}
