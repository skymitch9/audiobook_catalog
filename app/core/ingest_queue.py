# app/core/ingest_queue.py
# What gets ingested, in what order, and what has already been done.
#
# THE ORDER, AND WHY EACH TIER SITS WHERE IT DOES
# -----------------------------------------------
#   1. EPUBs                  - owner: "do all the EPUBs, those should be easy"
#   2. text-layer PDFs        - owner: "do the PDFs that have plain text also"
#   3. twin-satisfied audio   - free: the EPUB text already answers for them
#   4. reviewed audiobooks    - owner: "start with books that have reviews",
#                               ordered by review count desc
#   5. the rest of the audio  - recently-added first (see below)
#   6. needs-OCR PDFs         - owner: "if a pdf is going to be complicated
#                               delay it until after we finish all the
#                               audiobooks with a review"
#
# Tiers 1-3 cost seconds of CPU and no GPU, so they all clear on night one.
# Tier 4 is the owner's stated priority and the first real GPU spend.
#
# ⚠️ TIER 5's ORDER IS A CHOICE AND IT IS "RECENTLY ADDED FIRST".
# Stated rather than left implicit, because it is arbitrary and a future session
# may reasonably disagree. The reasoning: a book bought last month is one
# somebody in the house is likely reading NOW, and the whole feature is worth
# more on a book someone is mid-way through than on one finished in 2019. The
# alternative (longest-first, to bank the most hours per night) optimises a
# statistic nobody asked about. `site/additions_log.json` is the append-only
# record of when each book arrived and is the source of truth for this - never
# file mtime, which OneDrive rewrites.
#
# ⚠️ TIER 6 IS RE-QUEUED, NOT ABSENT. Design decision 7 said the 25 image-scan
# PDFs were "deliberately absent". The owner amended that on 2026-08-18 - they
# are last in line and marked `needs-ocr`. Recording them as a queued state with
# a named blocker is what stops a future session concluding the shelf simply
# lacks them.
#
# ✅ THE OCR PROCESSOR EXISTS AS OF 2026-09-01 (owner: "yes do it"), and tier 6
# is still LAST - OCR is CPU-only, so it rides the CPU lane outside the GPU
# window and never competes with transcription. ⚠️ It is OPT-IN PER BOOK: a
# tier-6 item is read only once its state row has been moved out of `needs-ocr`
# by `apply_requeue` (`--requeue-ocr`, or the dashboard's `requeue` list). A
# scan PDF that lands on the shelf tomorrow is still RECORDED with a named
# blocker and never read unattended - app/core/book_ocr.py has the reasoning
# (OCR quality varies enormously by what is on the page, and it fails silently).
#
# THE TWIN SKIP - THE SINGLE BIGGEST CUT
# --------------------------------------
# An audiobook whose work also exists as an EPUB is never transcribed: its pack
# is built from the EPUB text, which is cleaner than any transcript and already
# free. The design measured 67 titles / 1,175 hours qualifying on a deliberately
# loose title join, and says the join understates twinning - which is the safe
# direction, since a missed twin costs GPU time rather than a wrong answer.

from __future__ import annotations

import csv
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from app.core.review_join import book_id_from_title, normalise_title

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SITE = PROJECT_ROOT / "site"

# ⚠️ OUTSIDE EVERY GIT REPO, AND THAT IS THE MECHANICAL GUARD.
# Transcripts and state are derived full text of books the household owns; the
# owner's words, 2026-08-18: *"this is data that could lead to piracy if it were
# to get out"*. `audiobook_catalog` is a PUBLIC repo. A gitignore entry is a
# promise a future `git add -f` can break; a path that is not inside any
# repository cannot be committed by any command run in one.
TRAINING_ROOT = Path(os.getenv("ESTATE_TRAINING_ROOT", r"C:\Users\nbasl\estate-training-data"))
TRANSCRIPTS_DIR = TRAINING_ROOT / "transcripts"
PACKS_DIR = TRAINING_ROOT / "packs"
STATE_PATH = TRAINING_ROOT / "ingest_state.json"
RECEIPTS_DIR = TRAINING_ROOT / "receipts"

STATUS_DONE = "done"
STATUS_PENDING = "pending"
STATUS_NEEDS_OCR = "needs-ocr"
STATUS_FAILED = "failed"


def transcript_filename_stem(title: str) -> str:
    """The exact filename stem `scripts/transcribe_audiobook.py` writes for a
    title. ⚠️ ONE FORMULA, TWO CALLERS — the transcriber names its output with
    this, and `_transcript_for`'s last rung probes with it, so it must stay a
    single function. Until 2026-08-31 it was two hand-copies that agreed only
    by luck: a title whose m4b filename drops the ':' subtitle (e.g. 'Bunny
    Girl Evolution 2: A Monster Evolution LitRPG' from 'Bunny Girl Evolution
    2.m4b') transcribed fine, then failed EVERY pack with "no transcript on
    disk" — instantly and forever, because the transcript index keys on the
    m4b stem while the file on disk is named by title, and the transcriber's
    "already have it" check made every retry a sub-second no-op. 378 such
    error lines on 2026-08-27, the night the ingester was disabled.

    Empty when the title normalises to nothing; the WRITER falls back to the
    m4b stem there, which a title-only reader cannot reproduce — callers must
    treat '' as "no probe possible", never as a filename."""
    return normalise_title(title).replace(" ", "_")[:120]

TIER_EPUB = 1
TIER_PDF_TEXT = 2
TIER_TWIN = 3
TIER_REVIEWED_AUDIO = 4
TIER_REST_AUDIO = 5
TIER_NEEDS_OCR = 6


@dataclass
class QueueItem:
    book_id: str
    title: str
    tier: int
    source: str                    # epub | pdf-text | transcript | pdf-ocr
    path: Optional[str] = None     # the file to read (epub/pdf), or the m4b
    review_count: int = 0
    needs_gpu: bool = False
    twin_of: Optional[str] = None  # the EPUB path answering for an audiobook
    added_at: Optional[str] = None
    note: Optional[str] = None
    # ⚠️ The audiobook's runtime in seconds, and the ONLY honest source for it.
    # The deadline gate (ingest_control section 5) divides this by a measured
    # realtime factor to decide whether a book can finish before the next
    # boundary. It comes from the catalog's `duration_hhmm`, never from
    # chapters.json - that file knows only where the LAST CHAPTER STARTS, which
    # is minutes short of the runtime on every book, and a short duration is an
    # optimistic estimate, which is the one direction that gate must not lean.
    duration_sec: Optional[float] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return {"version": 1, "books": {}, "runs": []}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)   # atomic: a killed run never leaves half a state file


def mark(state: dict, book_id: str, status: str, **extra) -> None:
    entry = state.setdefault("books", {}).setdefault(book_id, {})
    entry["status"] = status
    entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry.update(extra)


def is_done(state: dict, book_id: str) -> bool:
    return (state.get("books", {}).get(book_id, {}) or {}).get("status") == STATUS_DONE


# --------------------------------------------------------------------------
# the dashboard's two fine controls: RE-QUEUE and PRIORITY
# --------------------------------------------------------------------------
#
# Owner-approved 2026-08-18. Both arrive in the SAME control document the pause
# lives in (`ingestion_control/state`; the contract is
# docs/info/book-ingestion.md section 1) and both are read defensively, because
# a different repo's Worker writes that document.

# Statuses a re-queue is allowed to reset. ⚠️ `done` is NOT in this set and its
# absence is the whole safety property: a stray id in the list must never be
# able to make the ingester spend twenty GPU-minutes re-transcribing a book that
# is already in GABI's knowledge base. Re-doing a finished book is a deliberate
# act that belongs behind a different, louder control than a retry button.
REQUEUABLE_STATUSES = (STATUS_FAILED, STATUS_NEEDS_OCR)


def apply_requeue(state: dict, book_ids) -> Dict[str, List[str]]:
    """Put failed books back to `pending`. Returns what happened, per outcome.

    `{"requeued": [...], "unknown": [...], "skipped_done": [...],
      "skipped_other": [...]}` - four buckets rather than a count, because the
    caller logs them with different words and they mean different things:

      unknown       an id this processor has never heard of. Logged and dropped
                    (the brief's rule) - it is almost always a stale row on a
                    dashboard someone left open, not an error to stop for.
      skipped_done  the book already succeeded. See REQUEUABLE_STATUSES.
      skipped_other something is already pending or in flight; re-marking it
                    pending would be a no-op dressed up as an action.

    ⚠️ THE PREVIOUS FAILURE REASON IS KEPT, under `previous_reason`. A retry
    that erases why the book failed the first time destroys the only evidence
    available when it fails the same way again.
    """
    out = {"requeued": [], "unknown": [], "skipped_done": [], "skipped_other": []}
    books = state.setdefault("books", {})
    for raw in book_ids or []:
        bid = str(raw).strip()
        if not bid:
            continue
        entry = books.get(bid)
        if not entry:
            out["unknown"].append(bid)
            continue
        status = (entry or {}).get("status")
        if status == STATUS_DONE:
            out["skipped_done"].append(bid)
            continue
        if status not in REQUEUABLE_STATUSES:
            out["skipped_other"].append(bid)
            continue
        previous = entry.get("reason") or entry.get("blocker")
        mark(state, bid, STATUS_PENDING,
             requeued_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             previous_status=status,
             **({"previous_reason": str(previous)[:300]} if previous else {}))
        out["requeued"].append(bid)
    return out


def priority_matcher(needles) -> Optional[callable]:
    """A `QueueItem -> bool` for the dashboard's `priority_front` list, or None.

    ⚠️ TWO KINDS OF NEEDLE, AND THE ORDER OF THE TESTS IS THE SAFETY PROPERTY.
    The owner may name a book (its exact `book_id`) or a series ("the primal
    hunter"). An exact id match is tried FIRST and is unambiguous; only if that
    fails is the needle treated as a series name and matched as a normalised
    substring of the title.

    ⚠️ THE SUBSTRING TEST IS DELIBERATELY NOT USED FOR IDs and is bounded below
    at three characters. A one- or two-letter needle would match most of the
    shelf, and a priority list that promotes everything is the same as no
    priority list at all - except that it looks like it is working. A too-short
    needle is therefore matched EXACTLY or not at all.

    Matching is on `normalise_title()`, the same normaliser the review join and
    the twin index use, so "The Primal Hunter" and "primal hunter" are one
    needle and a future change to normalisation moves all three together.
    """
    ids = set()
    fragments = []
    for needle in needles or []:
        text = str(needle).strip()
        if not text:
            continue
        ids.add(text)
        norm = normalise_title(text)
        if len(norm) >= 3:
            fragments.append(norm)
    if not ids:
        return None

    def matches(item) -> bool:
        if item.book_id in ids:
            return True
        title_norm = normalise_title(item.title or "")
        return any(f in title_norm for f in fragments)

    return matches


# --------------------------------------------------------------------------
# corpus readers
# --------------------------------------------------------------------------

def load_ebooks() -> List[dict]:
    path = SITE / "ebooks.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh).get("ebooks", [])


def ebooks_root() -> Path:
    path = SITE / "ebooks.json"
    if not path.exists():
        return Path(".")
    with open(path, "r", encoding="utf-8") as fh:
        return Path(json.load(fh).get("root", "."))


def load_catalog() -> List[dict]:
    path = SITE / "catalog.csv"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_chapters() -> dict:
    path = SITE / "chapters.json"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_additions_log() -> Dict[str, str]:
    """bookId -> first-seen ISO date, from the append-only additions log.

    ⚠️ Never file mtime (memory: "additions log / upload history"). OneDrive
    rewrites mtimes on sync, so mtime answers "when did OneDrive touch this",
    which is a different question with a plausible-looking answer.
    """
    path = SITE / "additions_log.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    entries = data if isinstance(data, list) else data.get("entries", [])
    out: Dict[str, str] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        title = e.get("title") or e.get("book") or ""
        when = e.get("date") or e.get("added_at") or e.get("ts") or ""
        if not title or not when:
            continue
        bid = book_id_from_title(title)
        # First sighting wins: the log is append-only and a re-upload must not
        # make an old book look new.
        out.setdefault(bid, str(when))
    return out


_DURATION_RE = re.compile(r"^(\d+):(\d{2})(?::(\d{2}))?$")


def parse_duration_hhmm(value: Optional[str]) -> Optional[float]:
    """`"10:07"` -> 36420.0 seconds. `"1:02:03"` is accepted too.

    ⚠️ Returns None for anything else, and None means UNKNOWN - never zero. A
    zero-second book would look instantaneous to the deadline gate and start at
    07:44. MEASURED 2026-08-18: all 1,079 catalog rows parse, so None is a
    contingency rather than a path anything relies on.
    """
    match = _DURATION_RE.match(str(value or "").strip())
    if not match:
        return None
    hours, minutes, seconds = match.group(1), match.group(2), match.group(3) or "0"
    total = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    return float(total) if total > 0 else None


def count_reviews_by_book_id(timeout: float = 30.0) -> Dict[str, int]:
    """Live count of review documents per bookId.

    Read-only, over the public REST path with the public web API key - the same
    door `club_books.py` and the CW probes use. Returns {} on any failure, and
    the caller must treat {} as "unknown", never as "no book has reviews": the
    difference decides whether the owner's stated priority is honoured or
    silently dropped.
    """
    import urllib.request

    base = ("https://firestore.googleapis.com/v1/projects/audiobook-catalog"
            "/databases/(default)/documents")
    key = "AIzaSyDgAblkxzVxl7nFbd7jXOo6PpuNPsJw11Y"  # public web API key
    counts: Dict[str, int] = {}
    token = None
    try:
        while True:
            url = f"{base}/reviews?key={key}&pageSize=300"
            if token:
                url += f"&pageToken={token}"
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = json.load(resp)
            for doc in data.get("documents", []):
                bid = (doc.get("fields", {}).get("bookId", {}) or {}).get("stringValue")
                if bid:
                    counts[bid] = counts.get(bid, 0) + 1
            token = data.get("nextPageToken")
            if not token:
                break
    except Exception:
        return {}
    return counts


# --------------------------------------------------------------------------
# the twin join
# --------------------------------------------------------------------------

# Series boilerplate an audiobook title carries and its EPUB usually does not.
# Stripped only for the TWIN join, never for an identity - `book_id_from_title`
# stays untouched.
#
# ⚠️ `Book N` AND `Volume N` ARE DELIBERATELY ABSENT FROM THIS LIST, and removing
# that restraint reintroduces a measured bug. An earlier draft stripped a bare
# trailing "Book N", which folded `Space Knight`, `Space Knight Book 3`,
# `Space Knight Book 4`, `Space Knight Book 7` and `Space Knight Book 10` onto
# ONE key - so four audiobooks would have been packed from a single EPUB's text,
# and GABI would have answered questions about book 10 from book 1 with a
# correct-looking citation. A bare volume number is IDENTITY, not boilerplate.
_BOILERPLATE_RE = re.compile(
    r"\s*[-:,(]?\s*("
    r"a litrpg adventure|part \d+ of \d+|"
    r"dramatized adaptation|full-cast edition|a novel"
    r").*$",
    re.IGNORECASE,
)


# The estate's dominant audiobook naming convention: "Title - Series, Book N".
# Anchored at the end and REQUIRING the ", Book N" marker, so it cannot eat a
# subtitle that is part of a book's actual identity - "Legion: The Many Lives of
# Stephen Leeds" has no such marker and survives untouched.
_SERIES_TAIL_RE = re.compile(r"\s*[-–]\s*[^-–]+,\s*book\s+\d+\s*$", re.IGNORECASE)


def strip_series_boilerplate(title: str) -> str:
    current = _SERIES_TAIL_RE.sub("", title or "")
    previous = None
    while previous != current:
        previous = current
        current = _BOILERPLATE_RE.sub("", current).strip(" -:,()")
    return normalise_title(current)


def build_twin_index(ebooks: List[dict]) -> Dict[str, dict]:
    """normalised audiobook title -> the EPUB entry that answers for it.

    ⚠️ THIS JOIN IS DELIBERATELY CONSERVATIVE, AND THAT IS A DEPARTURE FROM THE
    DESIGN'S FIGURE. Section 1.4 reports 67 twins from a "deliberately loose"
    join and argues a miss is safe because it only overstates the transcription
    bill. That reasoning is correct for a PLANNING estimate and wrong for this
    code, because the two errors are not symmetric here:

        a MISSED twin  -> the audiobook is transcribed. Costs ~15 GPU-minutes.
        a FALSE twin   -> the audiobook's pack is built from a DIFFERENT BOOK's
                          text, and GABI then answers questions about it
                          confidently and wrongly, with a correct-looking
                          citation. Nothing reports this.

    So the join runs on the manifest's own curated `audiobook_title` first (30
    of 138 EPUBs carry one), then exact normalised titles, then a
    boilerplate-stripped comparison - and stops there. MEASURED 2026-08-18:
    30 / 31 / 37 twins at those three widths. 37 is what this build uses; the
    remaining gap to 67 is left as GPU time rather than bought with a fuzzy
    match nobody can audit.
    """
    index: Dict[str, dict] = {}
    for e in ebooks:
        if (e.get("format") or "").lower() != "epub":
            continue
        for candidate in (e.get("audiobook_title"), e.get("title")):
            if not candidate:
                continue
            exact = normalise_title(candidate)
            if exact:
                index.setdefault(exact, e)
            stripped = strip_series_boilerplate(candidate)
            # A one- or two-character residue is not a title; refuse to join on it.
            if stripped and len(stripped) >= 4:
                index.setdefault(stripped, e)
    return index


# --------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------

def build_queue(state: Optional[dict] = None, review_counts: Optional[Dict[str, int]] = None,
                pdf_classifier=None,
                priority_front: Optional[List[str]] = None) -> List[QueueItem]:
    """The full ordered work list. Already-done books are excluded.

    `pdf_classifier` is injected so tests can run without PyMuPDF and without
    touching 641 MB of files; production passes `book_text.classify_pdf`.

    `priority_front` is the dashboard's list of books/series to move to the head
    (`ingestion_control/state.priority_front`).

    ⚠️ IT IS AN ABSOLUTE HEAD, NOT A WITHIN-TIER BUMP, AND THAT IS THE CHOICE.
    The two readings differ exactly when the owner names a GPU audiobook while
    138 EPUBs are still queued in tier 1: a within-tier bump would leave his
    book behind every one of them, i.e. behind the entire night, which is not
    what anybody means by "front of queue". It also matches the precedent he set
    himself - *"For now finish the primal hunter"* moved a tier-5 audiobook
    ahead of the whole shelf, not ahead of its own tier.

    ⚠️ It does NOT waive a single gate. A prioritised book still faces the
    window, the pause, the GPU and CPU guards and the deadline; priority decides
    what is asked first, never what is allowed. A prioritised needs-OCR PDF
    stays blocked until it is ARMED - the OCR lane exists as of 2026-09-01 but
    is opt-in per book, so priority moves such a book to the head of a queue in
    which it still refuses to start, and says so in words.
    """
    state = state if state is not None else load_state()
    review_counts = review_counts if review_counts is not None else {}
    ebooks = load_ebooks()
    root = ebooks_root()
    items: List[QueueItem] = []

    # --- tiers 1 and 2: the ebook shelf -----------------------------------
    for e in ebooks:
        fmt = (e.get("format") or "").lower()
        title = e.get("title") or e.get("filename") or ""
        if not title:
            continue
        bid = book_id_from_title(title)
        if is_done(state, bid):
            continue
        path = str(root / e["path"]) if e.get("path") else None
        if fmt == "epub":
            items.append(QueueItem(bid, title, TIER_EPUB, "epub", path))
        elif fmt == "pdf":
            verdict = pdf_classifier(path) if (pdf_classifier and path) else {"ok": False,
                                                                              "reason": "unclassified"}
            if verdict.get("ok"):
                items.append(QueueItem(bid, title, TIER_PDF_TEXT, "pdf-text", path,
                                       note=verdict.get("reason")))
            else:
                items.append(QueueItem(bid, title, TIER_NEEDS_OCR, "pdf-ocr", path,
                                       note=verdict.get("reason")))

    # --- tiers 3, 4, 5: the audio shelf ------------------------------------
    twins = build_twin_index(ebooks)
    added = load_additions_log()
    catalog = load_catalog()
    for row in catalog:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        bid = book_id_from_title(title)
        if is_done(state, bid):
            continue
        # An EPUB already queued under the same identity IS this book's pack.
        if any(i.book_id == bid and i.tier in (TIER_EPUB, TIER_PDF_TEXT) for i in items):
            continue
        twin = twins.get(normalise_title(title)) or twins.get(strip_series_boilerplate(title))
        count = review_counts.get(bid, 0)
        duration = parse_duration_hhmm(row.get("duration_hhmm"))
        if twin:
            items.append(QueueItem(bid, title, TIER_TWIN, "epub",
                                   str(root / twin["path"]),
                                   twin_of=twin.get("path"),
                                   review_count=count,
                                   note="twin-satisfied: EPUB text answers for the audiobook",
                                   duration_sec=duration))
            continue
        items.append(QueueItem(
            bid, title,
            TIER_REVIEWED_AUDIO if count > 0 else TIER_REST_AUDIO,
            "transcript", None, review_count=count, needs_gpu=True,
            added_at=added.get(bid), duration_sec=duration,
        ))

    return sorted(demote_contested_twins(items),
                  key=priority_sort_key(priority_matcher(priority_front)))


def demote_contested_twins(items: List[QueueItem]) -> List[QueueItem]:
    """⚠️ MECHANICAL GUARD: one EPUB may answer for at most ONE audiobook.

    A twin claim asserts "this EPUB *is* this audiobook's text". If two
    audiobooks claim the same EPUB, at most one of them can be right and nothing
    here can tell which - so BOTH lose the claim and fall back to transcription.
    The cost of being wrong is a book answered from another book's text with a
    citation that looks correct; the cost of this guard is GPU minutes.

    This is written as a guard rather than as care in the join because the join
    will be edited again: an earlier draft folded five Space Knight volumes onto
    one key, and a rule that catches that class after the fact survives the next
    well-meaning widening of the regex.
    """
    claims: Dict[str, int] = {}
    for item in items:
        if item.tier == TIER_TWIN and item.twin_of:
            claims[item.twin_of] = claims.get(item.twin_of, 0) + 1

    out: List[QueueItem] = []
    for item in items:
        if item.tier == TIER_TWIN and claims.get(item.twin_of, 0) > 1:
            out.append(QueueItem(
                item.book_id, item.title,
                TIER_REVIEWED_AUDIO if item.review_count > 0 else TIER_REST_AUDIO,
                "transcript", None, review_count=item.review_count, needs_gpu=True,
                added_at=item.added_at,
                # ⚠️ carried, not dropped: a demoted twin becomes a GPU book and
                # the deadline gate cannot estimate one whose runtime is missing.
                duration_sec=item.duration_sec,
                note=f"twin refused: {item.twin_of!r} was claimed by "
                     f"{claims[item.twin_of]} audiobooks",
            ))
        else:
            out.append(item)
    return out


def _added_rank(value: Optional[str]) -> float:
    """Newest-first rank. ⚠️ Unknown sorts LAST, never as new and never as old.

    Returned as a negative epoch so an ascending sort puts the newest book
    first; an unparseable or absent date becomes +inf, which lands it behind
    every book whose arrival is actually known.
    """
    if not value:
        return float("inf")
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text[:10]):
        try:
            from datetime import datetime

            return -datetime.fromisoformat(candidate).timestamp()
        except ValueError:
            continue
    return float("inf")


def _sort_key(item: QueueItem):
    # Within tier 4: most-reviewed first. Within tier 5: most recently added
    # first, with unknown-date books last (an unknown date must not masquerade
    # as either new or old - it sorts to the back and says so by position).
    if item.tier == TIER_REVIEWED_AUDIO:
        return (item.tier, -item.review_count, 0.0, item.title)
    if item.tier == TIER_REST_AUDIO:
        return (item.tier, 0, _added_rank(item.added_at), item.title)
    return (item.tier, 0, 0.0, item.title)


def priority_sort_key(matches=None):
    """`_sort_key` with the dashboard's priority as a rank AHEAD of the tier.

    Returns `_sort_key` itself when nothing is prioritised, so the ordinary path
    is byte-for-byte the order it has always been - a feature nobody has switched
    on must not be able to change the queue.

    ⚠️ WITHIN the promoted group, the ordinary key still decides. Naming three
    books does not make their relative order arbitrary; they keep tier, review
    count and arrival date among themselves, which is what makes promoting a
    whole series ("the primal hunter") come out in a sensible order rather than
    alphabetically by accident.
    """
    if matches is None:
        return _sort_key

    def key(item: QueueItem):
        return (0 if matches(item) else 1,) + tuple(_sort_key(item))

    return key
