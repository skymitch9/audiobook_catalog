# app/core/book_ocr.py
# The OCR lane: an image-scan PDF -> the same `ExtractedBook` shape every other
# source produces, plus a MEASUREMENT of how good the reading was.
#
# Owner approval 2026-09-01 ("yes do it"), lifting the QUEUED-LAST ordering the
# 2026-08-18 amendment put on these files. OCR is CPU-only: it never polls the
# GPU, never competes with transcription, and rides the ordinary CPU lane of the
# run loop (window, pause, do-not-disturb, CPU guard) rather than a new daemon.
#
# ⚠️ THE ENGINE IS `rapidocr-onnxruntime`, AND THAT IS A MEASUREMENT.
# Measured on this PC 2026-09-01 against the estate's own scan PDFs:
#
#   engine load                0.3 s (once per process)
#   raster (PyMuPDF, 300 dpi)  0.10 - 0.25 s / page
#   recognise                  1.1 - 2.1 s / page
#   mean recognition confidence 0.98 - 0.99 on card/prose pages
#
# It is pip-installable into the repo venv, pure CPU, needs no system installer
# and costs nothing - which is the whole reason it was tried before Tesseract.
# ⚠️ NO PAID / VISION-MODEL OCR. Per-page billing is an owner decision and is
# not in this build; if free OCR ever measurably fails, that is a question to
# raise, not a default to reach for.
#
# 🔴 300 dpi IS NOT A ROUND NUMBER, IT IS THE FIX FOR A MEASURED DEFECT.
# At 200 dpi the recogniser drops WORD SPACES on tight justified lines - a real
# page came back as `Raisesanundeadskeletonwarriortoservethebearerofthiscard.`
# At 300 the same page reads `Raises an undead skeleton warrior to serve the
# bearer of this card.` Spaceless text matches no lexical query and no stat
# detector, so it is silently useless rather than visibly broken; that is
# exactly the failure this module's quality gate exists to catch, and lowering
# the dpi to save a second a page reintroduces it.
#
# ⚠️ A PARTIAL READ IS NEVER PACKED. Every abort path in here raises; nothing
# returns a half-read book. A book GABI has "read" half of is worse than one it
# has not read at all, because nothing downstream can tell the difference - the
# same rule the transcriber's 95% truncation gate encodes.

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from app.core.book_text import ExtractedBook, ExtractedChapter, clean_text

# ⚠️ The provenance string. It is stamped into the pack's `source`, into the
# content digest, and into the state/index row, so a pack's origin is never
# ambiguous: `pdf-text` came off a real text layer, `pdf-ocr` was READ OFF AN
# IMAGE by a machine and carries the error rate that implies.
SOURCE_PDF_OCR = "pdf-ocr"

# See the header - 300 is the measured floor for word spacing, not a default.
OCR_DPI = 300

# ⚠️ A HARD CEILING ON ONE BOOK, because mid-book work always completes and an
# OCR job that has decided to take four hours would take them inside the owner's
# morning. 45 min at the MEASURED 2.0 s/page (568 s over 288 pages, 2026-09-01)
# is ~1,350 pages, i.e. far beyond anything on this shelf - the largest file
# here is 39 pages and took 41 s. It is a runaway guard for a future true
# scanned novel, not a scheduling policy, and it has never fired.
OCR_BOOK_BUDGET_SECONDS = 45 * 60

# --------------------------------------------------------------------------
# the quality bars
# --------------------------------------------------------------------------
#
# ⚠️ EVERY ONE OF THESE IS A CHOSEN NUMBER AND IS LABELLED AS ONE. They are
# calibrated against the estate's own 16 scan PDFs (measured 2026-09-01) and
# they exist for one reason: A PACK OF OCR NOISE POISONS GABI SILENTLY. A bad
# transcript is audibly bad; bad OCR reads as confident prose that says nothing
# true, with a citation that looks correct.

# Below this the "book" is a cover plate or an art page with a title on it, not
# a document. MEASURED: `The Wandering Inn`'s PDF is 2 pages of cover art and
# yields 14.5 chars/page; a real card/prose page yields 190-400.
OCR_MIN_CHARS_PER_PAGE = 120.0

# A whole pack under this is not worth a bookId in the index.
OCR_MIN_TOTAL_CHARS = 500

# The recogniser's own per-line confidence, averaged. MEASURED: 0.98-0.99 on
# clean pages, 0.86-0.89 on stylised cover lettering. 0.80 sits below every
# clean reading and above nothing we would want to keep.
OCR_MIN_MEAN_CONFIDENCE = 0.80

# Characters that are neither letters, digits, punctuation nor whitespace -
# the mojibake signature. A clean read measures ~0.000.
OCR_MAX_GARBAGE_RATIO = 0.05


class OcrUnavailable(RuntimeError):
    """No OCR engine on this interpreter. Carries the install line."""


class OcrRefused(RuntimeError):
    """The read happened and is NOT good enough to pack. Carries the numbers."""


@dataclass
class OcrQuality:
    """What the read actually measured. Every field is a number, on purpose -
    a report that says 'looks fine' is the one thing this must never produce."""

    pages: int = 0
    pages_with_text: int = 0
    chars: int = 0
    lines: int = 0
    mean_confidence: float = 0.0
    garbage_ratio: float = 0.0
    seconds: float = 0.0

    @property
    def chars_per_page(self) -> float:
        return (self.chars / self.pages) if self.pages else 0.0

    def to_dict(self) -> dict:
        return {
            "ocr_pages": self.pages,
            "ocr_pages_with_text": self.pages_with_text,
            "ocr_chars": self.chars,
            "ocr_chars_per_page": round(self.chars_per_page, 1),
            "ocr_mean_confidence": round(self.mean_confidence, 3),
            "ocr_garbage_ratio": round(self.garbage_ratio, 4),
            "ocr_seconds": round(self.seconds, 1),
        }

    def words(self) -> str:
        return (f"{self.pages} pages, {self.chars:,} chars "
                f"({self.chars_per_page:.0f}/page), {self.lines} lines, "
                f"mean confidence {self.mean_confidence:.3f}, "
                f"garbage {self.garbage_ratio:.4f}, {self.seconds:.0f}s")


def quality_refusal(q: OcrQuality) -> Optional[str]:
    """The worded reason this read must not be packed, or None to proceed.

    ⚠️ FOUR SEPARATE BARS, EACH SAYING WHICH ONE FAILED. A single "quality
    score" would hide which defect fired, and the four mean different things:
    too little text is a cover plate, low confidence is an unreadable page,
    garbage is a broken decode, and no page with text at all is a raster that
    silently produced nothing.
    """
    if q.pages <= 0:
        return "the PDF has no pages"
    if q.pages_with_text == 0:
        return f"OCR read no text at all on any of {q.pages} pages"
    if q.chars < OCR_MIN_TOTAL_CHARS:
        return (f"OCR yielded {q.chars} chars over {q.pages} pages, under the "
                f"{OCR_MIN_TOTAL_CHARS}-char floor - this is a cover/art plate, "
                f"not a document")
    if q.chars_per_page < OCR_MIN_CHARS_PER_PAGE:
        return (f"OCR yielded {q.chars_per_page:.0f} chars/page, under the "
                f"{OCR_MIN_CHARS_PER_PAGE:.0f} bar over {q.pages} pages")
    if q.mean_confidence < OCR_MIN_MEAN_CONFIDENCE:
        return (f"mean recognition confidence {q.mean_confidence:.3f} is under "
                f"the {OCR_MIN_MEAN_CONFIDENCE:.2f} bar - the page images are "
                f"not being read reliably")
    if q.garbage_ratio > OCR_MAX_GARBAGE_RATIO:
        return (f"garbage-character ratio {q.garbage_ratio:.4f} is over the "
                f"{OCR_MAX_GARBAGE_RATIO:.2f} bar")
    return None


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------
#
# An OCR engine here is one callable: `png_bytes -> [(text, confidence), ...]`
# in reading order. That shape is the whole seam - the production engine is
# rapidocr, tests inject a stub, and neither the chapter logic nor the quality
# measurement can tell the difference. It also means swapping in Tesseract later
# is one function, not a rewrite.

OcrEngine = Callable[[bytes], List[Tuple[str, float]]]

_engine_singleton = None

INSTALL_LINE = "pip install rapidocr-onnxruntime==1.4.4"


def ocr_available() -> Tuple[bool, str]:
    """(usable, words). ⚠️ Never raises - callers use it to log a HOLD."""
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception as exc:
        return False, (f"no OCR engine on this interpreter "
                       f"({type(exc).__name__}: {exc}); install with `{INSTALL_LINE}`")
    return True, "rapidocr-onnxruntime"


def _sorted_reading_order(raw) -> List[Tuple[str, float]]:
    """Detection results -> reading order (top band, then left to right).

    ⚠️ The detector returns boxes in ITS order, which is close to reading order
    and not guaranteed to be it. Text whose lines arrive shuffled reads as
    nonsense to a human and chunks into nonsense too, and nothing downstream
    reports it. Banding by the median box height is what keeps two columns of a
    card, or a page header, from interleaving.
    """
    boxes = []
    for entry in raw or []:
        try:
            box, text, score = entry[0], entry[1], entry[2]
        except (TypeError, IndexError, KeyError):
            continue
        try:
            ys = [float(p[1]) for p in box]
            xs = [float(p[0]) for p in box]
        except (TypeError, ValueError, IndexError):
            ys, xs = [0.0], [0.0]
        boxes.append({
            "text": str(text or ""),
            "score": float(score or 0.0),
            "top": min(ys), "height": max(ys) - min(ys), "left": min(xs),
        })
    if not boxes:
        return []
    heights = sorted(b["height"] for b in boxes)
    median_h = heights[len(heights) // 2] or 1.0
    band = max(1.0, median_h * 0.6)
    boxes.sort(key=lambda b: (round(b["top"] / band), b["left"]))
    return [(b["text"], b["score"]) for b in boxes]


def rapidocr_engine() -> OcrEngine:
    """The production engine, loaded once per process.

    ⚠️ Loaded LAZILY and cached. Importing onnxruntime costs ~0.3 s and pulls
    ~60 MB of shared libraries into the process; every ingest run that never
    meets an OCR book (which is most of them) must not pay for it.
    """
    global _engine_singleton
    if _engine_singleton is not None:
        return _engine_singleton
    ok, words = ocr_available()
    if not ok:
        raise OcrUnavailable(words)
    from rapidocr_onnxruntime import RapidOCR

    reader = RapidOCR()

    def run(png: bytes) -> List[Tuple[str, float]]:
        result, _elapsed = reader(png)
        return _sorted_reading_order(result)

    _engine_singleton = run
    return run


# --------------------------------------------------------------------------
# reading a PDF
# --------------------------------------------------------------------------

def _is_garbage(ch: str) -> bool:
    return not (ch.isalnum() or ch.isspace() or 32 <= ord(ch) < 127 or ch in "‘’“”—–…")


def _garbage_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for ch in text if _is_garbage(ch)) / len(text)


@dataclass
class OcrPage:
    number: int          # 1-based, the PDF's own page number
    text: str
    confidences: List[float] = field(default_factory=list)


def ocr_pages(path: str, engine: Optional[OcrEngine] = None, dpi: int = OCR_DPI,
              budget_seconds: float = OCR_BOOK_BUDGET_SECONDS,
              page_limit: int = 0) -> Tuple[List[OcrPage], OcrQuality]:
    """Rasterise every page and read it. Returns (pages, quality).

    ⚠️ RAISES on a budget overrun rather than returning what it has. See the
    module header: a partial read must never reach a pack.
    """
    import fitz  # PyMuPDF - already a dependency; it is what rasterises here

    engine = engine or rapidocr_engine()
    started = time.time()
    pages: List[OcrPage] = []
    all_text: List[str] = []
    confidences: List[float] = []
    lines = 0

    doc = fitz.open(path)
    try:
        total = doc.page_count
        if page_limit:
            total = min(total, page_limit)
        for index in range(total):
            elapsed = time.time() - started
            if elapsed > budget_seconds:
                raise OcrRefused(
                    f"OCR exceeded the {budget_seconds / 60:.0f}-minute per-book "
                    f"budget at page {index + 1} of {doc.page_count}; nothing was "
                    f"packed (a partial read is never packed)")
            pixmap = doc[index].get_pixmap(dpi=dpi)
            png = pixmap.tobytes("png")
            results = engine(png)
            text = clean_text("\n".join(t for t, _ in results))
            pages.append(OcrPage(number=index + 1, text=text,
                                 confidences=[c for _, c in results]))
            if text:
                all_text.append(text)
            lines += len(results)
            confidences.extend(c for _, c in results)
    finally:
        doc.close()

    joined = "\n".join(all_text)
    quality = OcrQuality(
        pages=len(pages),
        pages_with_text=sum(1 for p in pages if p.text.strip()),
        chars=len(joined),
        lines=lines,
        mean_confidence=(sum(confidences) / len(confidences)) if confidences else 0.0,
        garbage_ratio=_garbage_ratio(joined),
        seconds=time.time() - started,
    )
    return pages, quality


# 🔴 AN OUTLINE ENTRY IS OFTEN THE SOURCE FILENAME, NOT A CHAPTER TITLE.
# MEASURED 2026-09-01 across the estate's 16 scan PDFs: four carry an outline
# and THREE of those four are assembly artifacts left by whatever merged the
# pages - `JMM3 1.9.pdf` / `card1.pdf` / `card2.pdf`, `Wate.pdf`, and eleven
# entries reading `The Last Tide_For Review_2_Page_01` … `_Page_11`.
#
# ⚠️ That is worse than having no outline at all, because a citation would then
# read "chapter: card1.pdf" - which LOOKS like a real chapter title and is a
# working file on somebody's desktop. `Page 2` is honest; `card1.pdf` is not.
_FILENAME_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif",
                  ".psd", ".ai", ".indd", ".eps", ".webp", ".bmp")

_TRAILING_INDEX_RE = re.compile(r"[\s_\-.#]*\d+\s*$")

# The words a SCANNER numbers by. ⚠️ `chapter`, `part`, `book` and `section` are
# deliberately absent - those are what an AUTHOR numbers by, and rejecting them
# would throw away a real table of contents.
_PAGE_WORD_RE = re.compile(r"(?:^|[\s_\-.])(page|pg|img|image|scan|slide|sheet|plate)$",
                           re.IGNORECASE)


def _looks_like_a_filename(title: str) -> bool:
    """One entry that is a file on somebody's disk rather than a chapter."""
    return str(title or "").strip().lower().endswith(_FILENAME_EXTS)


def _outline_is_page_numbering(titles: Sequence[str]) -> bool:
    """⚠️ WHOLE-OUTLINE judgement: a scanner's page naming, not a contents list.

    It has to be judged across the document because no single entry looks wrong
    on its own - `The Last Tide_For Review_2_Page_07` is only obviously not a
    chapter title once you have seen the other ten.

    🔴 TWO CONDITIONS, AND THE SECOND ONE IS THE WHOLE CARE OF THIS FUNCTION.
    "Every entry is the same stem plus a number" is NOT enough on its own -
    `Chapter 1`, `Chapter 2`, `Chapter 3` is exactly that shape and is the most
    common REAL table of contents there is. Rejecting it would throw away good
    titles on precisely the file type section 5a says should use its outline: a
    true scanned novel. So the stem must ALSO end in a page-ish word, which is
    what separates a scanner from an author.
    """
    if len(titles) < 2:
        return False
    stems = {_TRAILING_INDEX_RE.sub("", str(t or "").strip()) for t in titles}
    if len(stems) != 1:
        return False
    return bool(_PAGE_WORD_RE.search(stems.pop()))


def _outline_bounds(path: str, page_count: int) -> List[Tuple[int, int, str]]:
    """(first_page, last_page, title) runs from the PDF outline, or [].

    Returns [] when the outline is really a scanner's page numbering, and
    replaces any single filename-shaped title with '' so the caller falls back
    to `Page N` for that entry alone - a mixed outline (measured: *Beautiful
    Creatures* reads `Wate.pdf`, `Duchannes`, `Ravenwood`) keeps its two real
    titles instead of losing them to one bad neighbour.
    """
    import fitz

    doc = fitz.open(path)
    try:
        toc = doc.get_toc() or []
    except Exception:
        toc = []
    finally:
        doc.close()

    starts = sorted(
        (int(e[2]), str(e[1]))
        for e in toc
        if len(e) >= 3 and isinstance(e[2], int) and 0 < int(e[2]) <= page_count
    )
    if not starts:
        return []
    if _outline_is_page_numbering([name for _, name in starts]):
        return []
    starts = [(page, "" if _looks_like_a_filename(name) else name)
              for page, name in starts]
    bounds = []
    for i, (first, name) in enumerate(starts):
        last = starts[i + 1][0] - 1 if i + 1 < len(starts) else page_count
        if last >= first:
            bounds.append((first, last, name))
    return bounds


def extract_pdf_ocr(path: str, book_id: str, title: str,
                    engine: Optional[OcrEngine] = None,
                    dpi: int = OCR_DPI,
                    budget_seconds: float = OCR_BOOK_BUDGET_SECONDS,
                    engine_name: str = "rapidocr-onnxruntime") -> Tuple[ExtractedBook, OcrQuality]:
    """Image-scan PDF -> `ExtractedBook(source="pdf-ocr")` + its measurement.

    ⚠️ CHAPTER ANCHORS: THE OUTLINE IF THE FILE HAS A TRUSTWORTHY ONE, ELSE ONE
    CHAPTER PER PAGE - and which one shipped is recorded in the pack's `notes`,
    so a reader never has to guess. 🔴 "Trustworthy" is doing real work there:
    three of the four outlines on this shelf are PDF-assembly artifacts
    (`card1.pdf`, `_Page_07`) and are rejected - see `_outline_bounds`.
    Page-per-chapter is the right shape for what is
    ACTUALLY on this shelf (2-39 page supplements where each page is a discrete
    unit - one ability card, one map, one family tree) and it gives every chunk
    an EXACT `page`, which is the only anchor an image scan can honestly supply.
    ⚠️ For a future TRUE scanned novel it is the wrong shape: no chunk spans a
    chapter boundary, so page-per-chapter would stop the retrieval side's +/-1
    stitch from crossing a page break, which is where sentences actually run.
    Such a file should carry an outline; if one ever arrives without, group
    pages into runs here rather than shipping 400 one-page chapters.

    ⚠️ THIS DOES NOT DECIDE WHETHER THE READ IS GOOD ENOUGH. It measures and
    returns; `quality_refusal()` is the judge, and the caller is what refuses to
    pack. Keeping the measurement and the verdict apart is what lets `--status`
    style tooling report numbers without a policy opinion.
    """
    pages, quality = ocr_pages(path, engine=engine, dpi=dpi,
                               budget_seconds=budget_seconds)

    book = ExtractedBook(book_id=book_id, title=title, source=SOURCE_PDF_OCR)
    bounds = _outline_bounds(path, len(pages))
    # ⚠️ Say how many outline titles were THROWN AWAY, not just that an outline
    # was used. `Jake's Magical Market 3` keeps its outline's page boundaries
    # while every one of its 15 titles is a discarded filename - a note reading
    # only "outline-based" would let a reader think those titles came from the
    # book.
    replaced = sum(1 for _, _, name in bounds if not name)
    anchor_words = "outline" if bounds else "page"
    if not bounds:
        bounds = [(p.number, p.number, f"Page {p.number}") for p in pages]

    for first, last, name in bounds:
        run = [p for p in pages if first <= p.number <= last and p.text.strip()]
        if not run:
            continue
        book.chapters.append(ExtractedChapter(
            index=len(book.chapters),
            title=clean_text(name) or f"Page {run[0].number}",
            text="\n\n".join(p.text for p in run),
            page=run[0].number,
        ))

    # ⚠️ Provenance in WORDS as well as in `source`. A pack that says only
    # `pdf-ocr` tells a reader the origin; these tell it the error rate, the
    # settings that produced it, and which anchor scheme it got - the three
    # things somebody debugging a wrong answer six months from now will want.
    book.notes.append(
        f"ocr: {engine_name} at {dpi} dpi - text READ OFF PAGE IMAGES, not a text layer")
    book.notes.append(f"ocr measured: {quality.words()}")
    book.notes.append(
        f"{anchor_words}-based chapter anchors"
        + (f" ({replaced} filename-shaped outline title"
           f"{'' if replaced == 1 else 's'} replaced with page numbers)"
           if replaced else ""))
    # ⚠️ THE MEASUREMENT TRAVELS WITH THE BOOK, BY CONSTRUCTION. It is also the
    # second return value, but a caller that only keeps the book must not
    # thereby lose the numbers the quality gate judges on - an earlier draft
    # attached this one layer up, and a test that called this function directly
    # got a book whose read was never judged. Anything holding an ExtractedBook
    # whose source is `pdf-ocr` can ask it how well it was read.
    book.ocr_quality = quality
    return book, quality
