#!/usr/bin/env python3
"""Nightly book-knowledge ingester - EPUB / text-PDF / audio transcript -> chunk
packs in the gated bucket.

Owner's order, 2026-08-18, verbatim: *"We should set up a task to process books
from 12am Arizona time to 8am. Start with books that have reviews on the
audiobook site. Also do all the EPUBs. Those should be easy. If my pc has above
50% gpu usage don't start a task until it comes down."* - amended the same day
with *"Do the EPUBs now, do the PDFs that have plain text also, if a pdf is
going to be complicated delay it until after we finish all the audiobooks with a
review"*, the twin-skip, batch 16 inside the window, opportunistic idle runs, and
a dashboard pause control.

Design of record: catalog-platform/docs/info/gabi-book-knowledge-design.md.
Operational contract: docs/info/book-ingestion.md. This file is an ORCHESTRATOR -
the pipeline lives in app/core/{book_text,book_chunker,ingest_pack,
ingest_control,ingest_queue}.py and every rule with a reason is documented there.

USAGE
-----
    python -m app.tools.ingest_books --status          # what would happen now
    python -m app.tools.ingest_books --run             # obey window+guard+pause
    python -m app.tools.ingest_books --cpu-only --now  # EPUB/PDF, ignore window
    python -m app.tools.ingest_books --pack-transcripts  # pack what is on disk
    python -m app.tools.ingest_books --requeue-failed --dry-run   # what would go back
    python -m app.tools.ingest_books --requeue-failed             # put them back
    python -m app.tools.ingest_books --dry-run ...     # build, never upload
    python -m app.tools.ingest_books --limit N

⚠️ `--now` BYPASSES THE WINDOW AND NOTHING ELSE. The pause control, the GPU
guard, the CPU guard and the deadline all still apply, because those protect the
owner's machine and the owner's evening; the window only protects his daytime. A
hand-run at build time is exactly what it is for.

Exit 0 = the run finished (including "nothing to do" and "correctly refused").
Exit 1 = something failed in a way that needs eyes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from app.core import book_text
from app.core.book_chunker import chunk_book
from app.core.ingest_control import (
    ControlState, StartDecision, batch_size_for, clear_requeue, decide_start,
    gpu_utilisation, in_window, machine_tz_is_phoenix, may_start_new_book,
    pause_mode_words, phoenix_now, read_control,
)
from app.core.ingest_pack import (
    INGESTER_VERSION, PackRefused, build_index, build_pack, pack_stats,
    upload_pack, write_pack_gz,
)
from app.core.ingest_queue import (
    PACKS_DIR, RECEIPTS_DIR, STATUS_DONE, STATUS_FAILED, STATUS_NEEDS_OCR,
    STATE_PATH, TIER_NEEDS_OCR, TRANSCRIPTS_DIR, QueueItem, apply_requeue,
    build_queue, count_reviews_by_book_id, load_chapters, load_state, mark,
    save_state, transcript_filename_stem,
)
from app.core.ingest_queue_summary import build_queue_summary, write_queue_summary
from app.core.ingest_transcripts import (
    load_ledger as load_transcript_ledger,
    save_ledger as save_transcript_ledger,
    upload_transcript,
)
# ⚠️ REUSE, do not duplicate: pid_alive() is the estate's single canonical
# PID-liveness decision (Windows OpenProcess + GetExitCodeProcess done right;
# see app/core/pipeline_lock.py's module docstring for why os.kill is unsafe on
# Windows). The estate forbids a second copy of a decision-making function, so
# this lock borrows that one primitive rather than re-implementing it here.
from app.core.pipeline_lock import pid_alive
from app.core.review_join import normalise_title


LOCK_PATH = Path(__file__).resolve().parents[2] / "output_files" / "ingest_books.lock"
LOCK_STALE_HOURS = 12   # longer than the 8 h window, so a live run is never stolen


def log(msg: str) -> None:
    print(f"[{phoenix_now().strftime('%Y-%m-%d %H:%M:%S')} MST] {msg}", flush=True)


def _this_host() -> str:
    # Same env probe pipeline_lock uses (a trivial read, not a decision), so the
    # host recorded in the lock and the host we compare against agree.
    return os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or "unknown"


def _read_lock_holder(path: Path) -> Optional[dict]:
    """Best-effort read of the lock's {pid, host, at}. None if missing OR
    unparseable (a crash mid-write can leave a truncated file); the caller then
    falls back to the file-mtime age ceiling."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            "pid": int(raw["pid"]),
            "host": str(raw.get("host", "?")),
            "at": str(raw.get("at", "?")),
        }
    except FileNotFoundError:
        return None
    except Exception:
        return None  # corrupt/partial write -> caller uses mtime backstop


def _lock_age_hours(path: Path) -> float:
    try:
        return (time.time() - path.stat().st_mtime) / 3600.0
    except OSError:
        return float("inf")


def _lock_is_stale(path: Path) -> tuple[bool, str]:
    """Is the ingest lock at `path` safe to reclaim? Returns (stale, reason).

    Two independent signals, mirroring app/core/pipeline_lock.py — either one
    alone clears the lock:

      1. **PID-liveness (the fast path).** If the recorded holder pid is
         provably NOT running, the lock died the instant the holder did — a hard
         crash, a kill, or a power loss mid-window — and is reclaimable
         immediately, regardless of age. This is the whole point of the fix:
         without it a crash mid-window stranded the 00:00-08:00 ingestion window
         for up to LOCK_STALE_HOURS (12 h), the exact failure the 30-minute
         cadence exists to prevent.

      2. **Age ceiling (LOCK_STALE_HOURS, the backstop).** Covers a holder that
         is still alive but wedged, AND every case where liveness CANNOT be
         trusted — a corrupt/unreadable lock file, or a lock recorded on a
         DIFFERENT host (a pid is meaningless on another machine, and a foreign
         or recycled pid must never be read as 'alive enough to steal').

    ⚠️ Fail-safe direction preserved: when liveness is uncertain we DO NOT steal
    — only a pid that is provably dead ON THIS HOST short-circuits the ceiling.
    A pid that reads alive (including a recycled pid that now belongs to some
    unrelated process) is left alone until the age ceiling elapses.
    """
    holder = _read_lock_holder(path)
    if holder is not None and holder["pid"] > 0 and holder["host"] == _this_host():
        if not pid_alive(holder["pid"]):
            return True, f"holder pid {holder['pid']} is dead"
        # Alive on this host: only the age ceiling below can reclaim it.
    age_h = _lock_age_hours(path)
    if age_h >= LOCK_STALE_HOURS:
        return True, f"{age_h:.1f} h old, past the {LOCK_STALE_HOURS} h ceiling"
    return False, ""


class _Lock:
    """Single-flight guard, because the scheduled task fires every 30 minutes.

    ⚠️ Without this, the 02:00 invocation starts a second transcription while the
    00:00 one is still running - two Whisper processes on a 16 GB card, both of
    which then OOM or thrash, and neither of which finishes.

    Stale-lock recovery has TWO independent signals (see _lock_is_stale): a
    provably-dead holder pid reclaims the lock instantly, and LOCK_STALE_HOURS is
    the age backstop for a wedged-but-alive holder or a lock whose pid cannot be
    trusted. Before the pid check was added this was time-only, so a crash /
    kill / power loss mid-window left the whole 00:00-08:00 window stranded for
    up to 12 h — the exact failure the 30-minute cadence claims to prevent
    (docs/info/pipeline-sanctity-2026-08-24.md, finding #1).
    """

    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            stale, reason = _lock_is_stale(self.path)
            if not stale:
                holder = _read_lock_holder(self.path)
                who = (
                    f"pid {holder['pid']} on {holder['host']}, started {holder['at']}"
                    if holder else "unreadable lock file"
                )
                log(f"another ingest run holds the lock ({who}, "
                    f"{_lock_age_hours(self.path):.1f} h old); exiting")
                return self
            log(f"stale lock - taking it ({reason})")
        self.path.write_text(
            json.dumps({
                "pid": os.getpid(),
                "host": _this_host(),
                "at": phoenix_now().isoformat(),
            }),
            encoding="utf-8")
        self.acquired = True
        return self

    def __exit__(self, *exc):
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


# --------------------------------------------------------------------------
# packing one book
# --------------------------------------------------------------------------

_SOURCE_M4B_RE = __import__("re").compile(r'"source_m4b"\s*:\s*"((?:[^"\\]|\\.)*)"')
_transcript_index: Optional[dict] = None


def _transcript_source(path: Path) -> str:
    """The m4b a transcript came from, read from the file's HEAD.

    ⚠️ A transcript is ~13 MB of word timings and `meta` is its first key, so
    reading 8 KB answers this in microseconds where `json.load` costs ~1.5 s.
    That matters: an earlier version parsed every transcript once per queue item,
    which is 1,200 x 9 full parses on a 1,229-item queue - the run appeared to
    hang. Falls back to a full parse if the head does not contain the field.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(8192)
    except OSError:
        return ""
    m = _SOURCE_M4B_RE.search(head)
    if m:
        # ⚠️ Decode the captured JSON string body AS JSON. The old
        # `.encode().decode("unicode_escape")` round-trip mojibaked every
        # non-ASCII character (UTF-8 bytes re-read as Latin-1): the curly
        # apostrophe in `Sorcerer's Stone` became `â€™`, the index key
        # missed, and a freshly-transcribed book failed its own pack with
        # "no transcript on disk" (2026-08-18, first daytime run).
        try:
            return json.loads('"' + m.group(1) + '"')
        except ValueError:
            pass  # malformed escape in the head - fall through to full parse
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("meta", {}).get("source_m4b", "") or ""
    except Exception:
        return ""


def _build_transcript_index() -> dict:
    from pathlib import PureWindowsPath

    index: dict = {}
    if not TRANSCRIPTS_DIR.exists():
        return index
    for path in sorted(TRANSCRIPTS_DIR.glob("*.json")):
        src = _transcript_source(path)
        # ⚠️ PureWindowsPath, not Path: `source_m4b` is a STORED WINDOWS PATH
        # whatever platform this code runs on. Plain Path() on a POSIX runner
        # treats the backslashes as filename characters, mangles the stem, and
        # the index key misses - which is how the 4f1b6b0 regression test went
        # red on Linux CI while production (Windows) was fine. PureWindowsPath
        # also accepts forward slashes, so nothing is lost.
        stem = PureWindowsPath(src).stem if src else path.stem
        key = normalise_title(stem)
        if key:
            index.setdefault(key, path)
    return index


def _transcript_for(title: str) -> Optional[Path]:
    """Find a transcript on disk for this title, by normalised title.

    ⚠️ Normalised, not exact: chapters.json carries `The Primal Hunter 9: A
    LitRPG Adventure` while the file on disk is `...9- A LitRPG Adventure.m4b`,
    because a colon cannot appear in a Windows filename. An exact join silently
    loses that book and every other title with punctuation the filesystem
    refuses.

    The index is built once per process. A transcript written DURING a run (by
    this run's own transcription step) is picked up because `transcribe()`
    invalidates the cache on success.
    """
    global _transcript_index
    if _transcript_index is None:
        _transcript_index = _build_transcript_index()
    hit = _transcript_index.get(normalise_title(title))
    if hit:
        return hit
    # ⚠️ The queue title may carry a " - Series, Book N" tail the m4b filename
    # (and therefore the index key) does not — the SAME shape resolve_m4b's
    # third pass handles, one layer down. Found live 2026-08-18 14:14: ACOTAR
    # Part 1 transcribed successfully (the resolver's tail-strip found the
    # file) and then FAILED ITS OWN PACK here, because this lookup still used
    # the full tailed title. Strip one " - " segment at a time, rightmost
    # first; the index keys are unique per book so a hit is unambiguous.
    stripped = title
    while " - " in stripped:
        stripped = stripped.rsplit(" - ", 1)[0]
        key = normalise_title(stripped)
        if not key:
            break
        hit = _transcript_index.get(key)
        if hit:
            return hit
    # ⚠️ LAST RUNG (regression 2026-08-27): the file the TRANSCRIBER ITSELF
    # would have written for this title. The index keys on each transcript's
    # source-m4b stem, but transcripts are NAMED by queue title — and when the
    # m4b filename drops a ':' subtitle the two keys never meet: transcribe()
    # exits 0 with "already have ...; nothing to do" while this function
    # raised "no transcript on disk", instantly, on every retry (378 log
    # lines the night the ingester was disabled). The tail-strip above only
    # handles " - " tails. Probing with the writer's own shared formula
    # (transcript_filename_stem) closes the loop for exactly the titles the
    # writer would satisfy; an empty stem means no probe is possible.
    stem = transcript_filename_stem(title)
    if stem:
        direct = TRANSCRIPTS_DIR / f"{stem}.json"
        if direct.exists():
            return direct
    return None


def extract_for(item: QueueItem, chapters: dict) -> book_text.ExtractedBook:
    if item.source == "epub":
        return book_text.extract_epub(item.path, item.book_id, item.title)
    if item.source == "pdf-text":
        return book_text.extract_pdf(item.path, item.book_id, item.title)
    if item.source == "transcript":
        path = _transcript_for(item.title)
        if not path:
            raise FileNotFoundError(f"no transcript on disk for {item.title!r}")
        entry = chapters.get(item.title) or {}
        return book_text.extract_transcript(str(path), item.book_id, item.title,
                                            entry.get("chapters"))
    raise ValueError(f"unknown source {item.source!r}")


def pack_one(item: QueueItem, chapters: dict, state: dict,
             dry_run: bool = False) -> Optional[dict]:
    """Extract -> chunk -> pack -> gzip -> upload -> record. Returns stats."""
    book = extract_for(item, chapters)
    if not book.chapters:
        log(f"  SKIP {item.title!r}: extracted no text")
        # Titles on the non-done rows too: publish_index() serves EVERY row of
        # the state, not just the packed ones, and `item.title` is a measured
        # title off the catalog — not a de-slugged book_id.
        mark(state, item.book_id, STATUS_FAILED, reason="no text extracted",
             title=item.title, source=item.source)
        return None

    chunks, refs = chunk_book(book)
    extra = {"twin_of": item.twin_of} if item.twin_of else None
    try:
        pack = build_pack(book, chunks, refs, extra=extra)
    except PackRefused as exc:
        log(f"  REFUSED {item.title!r}: {exc}")
        mark(state, item.book_id, STATUS_FAILED, reason=str(exc),
             title=item.title, source=item.source)
        return None

    gz = write_pack_gz(pack, PACKS_DIR)
    stats = pack_stats(pack, gz)
    if stats["warn"]:
        log(f"  WARN {item.title!r}: {stats['text_bytes']:,} bytes of text")

    if dry_run:
        log(f"  DRY {item.title}  {stats['chunks']} chunks  "
            f"{stats['gz_bytes']:,}B gz  ratio {stats['gzip_ratio']}")
        return stats

    key = upload_pack(gz, item.book_id)
    # ⚠️ `title` IS PART OF THE RECORD, and leaving it out is not free.
    # publish_index() serves this entry verbatim, so a state row with no title
    # becomes an index row with no title — which is how all 182 rows reached the
    # serving layer nameless (found 2026-08-18). It is `book.title`, not
    # `item.title`, ON PURPOSE: that is the exact string build_pack() writes into
    # the pack, so this row and its pack spell the book one way rather than two.
    mark(state, item.book_id, STATUS_DONE, source=book.source,
         title=book.title,
         chunks=stats["chunks"], chapters=stats["chapters"],
         text_bytes=stats["text_bytes"], gz_bytes=stats["gz_bytes"],
         key=key, ingester_version=INGESTER_VERSION,
         twin_of=item.twin_of)
    log(f"  OK {item.title}  {stats['chunks']} chunks  {stats['gz_bytes']:,}B gz "
        f"(ratio {stats['gzip_ratio']})  -> {key}")
    _back_up_transcript(item, book)
    return stats


def _back_up_transcript(item: QueueItem, book) -> None:
    """Third copy of the transcript, in the same gated bucket as the packs.

    ⚠️ SOFT-FAIL, ALWAYS, AND THAT IS THE WHOLE CONTRACT OF THIS FUNCTION. A
    backup step that can stop an ingest run is a liability, not a safety net:
    the books are the job, and losing a night of transcription to a flaky
    upload would cost more GPU hours than the copy protects. It logs what
    happened and returns — `upload_transcript` does not raise, and this wrapper
    catches anything that escapes anyway.

    Only transcript-sourced books have one; an EPUB pack has no transcript
    behind it, so there is nothing to copy.
    """
    if getattr(book, "source", None) != "transcript":
        return
    try:
        path = _transcript_for(item.title)
        if not path:
            log(f"  transcript-backup SKIP {item.title!r}: no transcript resolved")
            return
        ledger = load_transcript_ledger()
        res = upload_transcript(path, ledger)
        if res["status"] == "uploaded":
            save_transcript_ledger(ledger)
            log(f"  transcript-backup OK {path.name}  "
                f"{res['gz_bytes']:,}B gz -> {res['key']}")
        elif res["status"] == "skipped":
            log(f"  transcript-backup already stored: {res['key']}")
        else:
            log(f"  transcript-backup FAILED {path.name}: {res.get('error')}")
    except Exception as exc:  # never propagate — see the docstring
        log(f"  transcript-backup FAILED for {item.title!r}: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# transcription (the GPU half)
# --------------------------------------------------------------------------

def transcribe(item: QueueItem, batch_size: int) -> bool:
    """convert -> transcribe -> delete WAV, via scripts/transcribe_audiobook.py.

    A subprocess on purpose: the Whisper venv is a separate interpreter with its
    own CUDA DLL bootstrap, and a crash inside it must not take the scheduler's
    process down mid-queue.
    """
    import subprocess

    script = Path(__file__).resolve().parents[2] / "scripts" / "transcribe_audiobook.py"
    cmd = [sys.executable, str(script), "--title", item.title,
           "--batch-size", str(batch_size)]
    log(f"  transcribing {item.title!r} (batch {batch_size})")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        log(f"  transcription FAILED rc={proc.returncode}: {(proc.stderr or '')[-600:]}")
        return False
    global _transcript_index
    _transcript_index = None   # a new transcript exists; rebuild on next lookup
    return True


# --------------------------------------------------------------------------
# the run loop
# --------------------------------------------------------------------------

def consume_requeue(control: ControlState, state: dict, dry_run: bool = False) -> dict:
    """Apply and then CLEAR the dashboard's `requeue` list. Returns the outcome.

    ⚠️ THIS RUNS ONCE PER RUN, AT THE START, AND BEFORE `build_queue()`. Before,
    because a book only re-enters the queue by ceasing to be `failed`, and
    `build_queue` reads the state it is handed. Once, because unlike the pause
    (re-read before every book, deliberately) a requeue is an EVENT: re-reading
    it mid-run would consume entries the owner added seconds ago, applying them
    to a queue this run has already built and cannot revisit.

    ⚠️ THE STATE IS SAVED BEFORE THE LIST IS CLEARED, never the other way round.
    A crash between the two re-applies a requeue that already landed, which
    marks a pending book pending. The opposite order loses the owner's request
    with nothing recording that it was ever made.
    """
    ids = list(control.requeue or [])
    # ⚠️ THE UNUSABLE ENTRIES ARE SWEPT WITH THE GOOD ONES, and they are the
    # reason this line exists rather than just `ids`. MEASURED 2026-08-18 in the
    # first live round trip: `clear_requeue` uses ArrayRemove, which deletes only
    # the values it names, so an entry the READER dropped survived every clear
    # and warned again on the next read - and the control is read before every
    # book, so one malformed entry becomes a thousand log lines a night. They
    # are raw values, matched against what Firestore actually holds.
    rejected = list(control.requeue_rejected or [])
    if not ids and not rejected:
        return {}
    if rejected and not ids:
        # Nothing to apply, but the document still needs tidying - and saying so
        # is what stops a future session hunting a retry that never existed.
        log(f"  requeue: {len(rejected)} unusable entr"
            f"{'y' if len(rejected) == 1 else 'ies'} swept out of the control document")
        if not dry_run:
            clear_requeue(rejected)
        return {}
    outcome = apply_requeue(state, ids)
    if outcome["requeued"]:
        if not dry_run:
            save_state(state)
        log(f"requeued {len(outcome['requeued'])} book"
            f"{'' if len(outcome['requeued']) == 1 else 's'} to pending "
            f"(dashboard requeue): {outcome['requeued'][:5]}"
            f"{' …' if len(outcome['requeued']) > 5 else ''}")
    # ⚠️ Each non-applied bucket says WHY in its own words. "Nothing happened"
    # is the one report a retry button must never give.
    if outcome["unknown"]:
        log(f"  requeue: {len(outcome['unknown'])} unknown book id"
            f"{'' if len(outcome['unknown']) == 1 else 's'} dropped "
            f"(no such book in the state file): {outcome['unknown'][:5]}")
    if outcome["skipped_done"]:
        log(f"  requeue: {len(outcome['skipped_done'])} already done and left alone "
            f"- re-ingesting a finished book is not what a retry button does: "
            f"{outcome['skipped_done'][:5]}")
    if outcome["skipped_other"]:
        log(f"  requeue: {len(outcome['skipped_other'])} already pending, nothing to do: "
            f"{outcome['skipped_other'][:5]}")
    if dry_run:
        # A dry run must not consume the owner's request, and must say so -
        # otherwise the button looks pressed and the list is still full.
        log("  requeue: --dry-run, so nothing was written: the state file is "
            "unchanged and the control list is NOT cleared")
        return outcome
    clear_requeue(ids + rejected)
    return outcome


def requeue_failed(dry_run: bool = False, rows=None, root=None) -> dict:
    """`--requeue-failed`: put back every `failed` book whose file NOW resolves.

    ⚠️ WHY A FLAG AND NOT A HAND EDIT. `ingest_state.json` lives outside every
    repo and is written by a running pipeline; editing it by hand is the
    "establish who wrote it" incident waiting to happen. The dashboard's
    `requeue` control is the other supported route, but it needs the dashboard
    and a Firestore round trip — this is the local equivalent, and it goes
    through the SAME primitive (`ingest_queue.apply_requeue`), so `done` is
    still untouchable and the previous failure reason is still kept.

    ⚠️ AND ONLY THE ONES THAT NOW RESOLVE. A blanket retry re-queues books that
    will fail again for the same reason in the same window, which is how a
    retry button becomes a nightly log of the same twelve errors. A book whose
    title still does not reach a file is reported, by name, and left `failed`.

    Read-only with `--dry-run`: nothing is written and the report says so.
    """
    from app.core.m4b_resolver import SCAN_EXTS, resolve_book_file
    from app.config import ROOT_DIR
    from app.core.ingest_queue import load_catalog

    root = Path(root) if root else ROOT_DIR
    rows = load_catalog() if rows is None else rows
    # ⚠️ Scanned ONCE and handed to every lookup. An rglob per book over ~1,080
    # files on a OneDrive-backed tree is minutes of pointless IO.
    files = [p for p in root.rglob("*") if p.suffix.lower() in SCAN_EXTS]

    state = load_state()
    resolvable, unresolved = [], []
    for book_id, entry in sorted((state.get("books") or {}).items()):
        if (entry or {}).get("status") != STATUS_FAILED:
            continue
        title = (entry or {}).get("title") or ""
        if not title:
            unresolved.append((book_id, "no title recorded on the state entry"))
            continue
        try:
            resolve_book_file(title, rows=rows, root=root, files=files)
        except FileNotFoundError as exc:
            unresolved.append((book_id, str(exc)))
            continue
        resolvable.append(book_id)

    outcome = apply_requeue(state, resolvable)
    if outcome["requeued"] and not dry_run:
        save_state(state)
    log(f"requeue-failed: {len(outcome['requeued'])} of "
        f"{len(resolvable) + len(unresolved)} failed book"
        f"{'' if len(resolvable) + len(unresolved) == 1 else 's'} put back to pending")
    for book_id in outcome["requeued"]:
        log(f"  requeued {book_id}")
    for book_id, why in unresolved:
        log(f"  LEFT FAILED {book_id}: {why}")
    for bucket, words in (("unknown", "not in the state file"),
                          ("skipped_done", "already done"),
                          ("skipped_other", "not in a requeueable status")):
        if outcome[bucket]:
            log(f"  {len(outcome[bucket])} {words}: {outcome[bucket][:5]}")
    if dry_run:
        log("  --dry-run, so nothing was written; the state file is unchanged")
    outcome["left_failed"] = [book_id for book_id, _ in unresolved]
    return outcome


def run(args) -> int:
    state = load_state()
    chapters = load_chapters()
    control = ControlState() if args.ignore_control else read_control()

    consume_requeue(control, state, dry_run=args.dry_run)

    reviews = {} if args.no_reviews else count_reviews_by_book_id()
    if not reviews and not args.no_reviews:
        log("WARN: review counts unreadable - priority falls back to tier order. "
            "This is 'unknown', not 'no book has reviews'.")

    if control.priority_front:
        log(f"priority: {len(control.priority_front)} dashboard entr"
            f"{'y' if len(control.priority_front) == 1 else 'ies'} move to the front "
            f"of the queue: {control.priority_front[:5]}"
            f"{' …' if len(control.priority_front) > 5 else ''}")

    queue = build_queue(state=state, review_counts=reviews,
                        pdf_classifier=book_text.classify_pdf,
                        priority_front=control.priority_front)
    if args.cpu_only:
        queue = [i for i in queue if not i.needs_gpu]
    if args.limit:
        queue = queue[:args.limit]

    log(f"queue: {len(queue)} books "
        f"({sum(1 for i in queue if not i.needs_gpu)} CPU, "
        f"{sum(1 for i in queue if i.needs_gpu)} GPU)")

    # The same work list, counted per tier, for /status/processing — which drew
    # the whole audio shelf as one lane because the reviewed-vs-rest split lived
    # only inside build_queue(). Written HERE, immediately after the log line, so
    # the file and that line describe the SAME queue: processing-board.mjs
    # cross-checks `audiobook-with-review + audiobook` against the GPU bucket it
    # parses out of the log, and shows the split only when the two agree.
    # ⚠️ Never raises — a status artefact must not be able to stop an ingest run.
    write_queue_summary(build_queue_summary(queue, ingester_version=INGESTER_VERSION))

    done = failed = skipped = 0
    for item in queue:
        if item.tier == TIER_NEEDS_OCR:
            # ⚠️ Recorded, not attempted. OCR is not built (owner, 2026-08-18);
            # this row is what stops the shelf looking as though the book is
            # simply missing.
            mark(state, item.book_id, STATUS_NEEDS_OCR, source="pdf-ocr",
                 title=item.title,
                 reason=item.note or "image-scan PDF", blocker="OCR processor not built")
            save_state(state)
            continue

        # ⚠️ Re-read the control document before EVERY book - that is the
        # documented contract (info/book-ingestion.md section 1), and until
        # 2026-08-18 it was violated: run() read it once at start, so a pause
        # written mid-run never stopped a long opportunistic run, and the
        # 19:00 evening window would have been ignored by any run that began
        # before it. One small Firestore read per book start is the cost.
        if not args.ignore_control:
            control = read_control()

        # A transcript that already exists on disk makes this a CPU job, so it
        # must not be gated on the graphics card. Getting this wrong would make a
        # pack-only pass wait for a GPU it never touches.
        will_transcribe = item.needs_gpu and not args.no_transcribe and not _transcript_for(item.title)

        verdict, words, batch = _gate(args, control, item, will_transcribe, skipped)
        if verdict != "go":
            log(f"{verdict.upper()} before {item.title!r}: {words}")
            if verdict == "stop":
                break
            skipped += 1
            continue
        skipped = 0   # consecutive, not cumulative: a start clears the run

        try:
            if item.needs_gpu and not _transcript_for(item.title):
                if args.no_transcribe:
                    # Pack-only pass: everything whose text already exists gets
                    # packed, and nothing spends a GPU-minute. This is how a
                    # finished transcript set is turned into packs outside the
                    # window, and it is cheap enough to run any time.
                    continue
                if not transcribe(item, batch):
                    mark(state, item.book_id, STATUS_FAILED, title=item.title,
                         reason="transcription failed")
                    failed += 1
                    save_state(state)
                    continue
            stats = pack_one(item, chapters, state, dry_run=args.dry_run)
            done += 1 if stats else 0
            failed += 0 if stats else 1
        except Exception as exc:  # one bad book never ends the night
            log(f"  ERROR {item.title!r}: {type(exc).__name__}: {exc}")
            mark(state, item.book_id, STATUS_FAILED, title=item.title,
                 reason=f"{type(exc).__name__}: {exc}"[:300])
            failed += 1
        save_state(state)

    _write_receipt(state, done, failed, args.dry_run)
    log(f"run complete: {done} packed, {failed} failed")
    return 0


MAX_ITEM_SKIPS = 25


def _gate(args, control: ControlState, item: QueueItem, will_transcribe: bool,
          skipped: int) -> tuple:
    """`("go"|"skip"|"stop", words, batch_size)` for one book.

    The whole gate decision for one item, in one place, so `run()` stays a loop
    over books rather than a loop with a policy engine inside it. Two paths
    because `--now` waives the window and nothing else - see `_control_or_guard`.
    """
    if not args.now:
        decision = decide_start(control, needs_gpu=will_transcribe,
                                allow_opportunistic=args.opportunistic,
                                audio_seconds=item.duration_sec)
        if decision.may_start:
            return "go", decision.reason, decision.batch_size
        verdict, words = _stop_or_skip(decision, skipped)
        return verdict, words, decision.batch_size

    blocked = (None if args.ignore_control else
               _control_or_guard(control, will_transcribe, item.duration_sec))
    return ("stop", blocked, batch_size_for()) if blocked else \
           ("go", "--now", batch_size_for())


def _stop_or_skip(decision: StartDecision, skipped: int) -> tuple:
    """`("stop"|"skip", words)` for a refusal. ⚠️ NOT every refusal ends a run.

    🔴 THE INCIDENT THIS ENCODES (2026-08-18, 15:30): every refusal used to be
    global - a pause, a busy machine, the wrong hour - so `break` was right and
    this function did not need to exist. The deadline gate broke that: "this
    book is too long to finish by 08:00" says nothing about the 40-minute book
    behind it, and stopping on it throws away the rest of the night. The same
    shape had already cost an afternoon for a different reason - one pack-only
    book at the head of the queue refused, and 1,028 GPU books behind it never
    got looked at.

    So: an ITEM-SPECIFIC refusal skips to the next book, a global one stops.
    ⚠️ Bounded, because a skip is not free - each one costs a control re-read -
    and near a boundary EVERY remaining book will refuse. After MAX_ITEM_SKIPS
    consecutive skips the run stops and says so, rather than walking 1,000 books
    to discover the obvious.
    """
    if not decision.item_specific:
        return "stop", decision.reason
    if skipped >= MAX_ITEM_SKIPS:
        return "stop", (f"{decision.reason} - and {skipped} books in a row did "
                        f"not fit; stopping rather than scanning the queue")
    return "skip", f"{decision.reason}; trying the next book"


def _control_or_guard(control: ControlState, needs_gpu: bool,
                      audio_seconds: Optional[float] = None) -> Optional[str]:
    """The gates that still bind under `--now` (which waives only the window).

    ⚠️ FIVE OF THE SIX STILL BIND, and the two added 2026-08-18 are among them.
    `--now` waives the WINDOW, because the window protects the owner's daytime
    and a hand-run is a deliberate daytime act. It does not waive the machine
    guards (they protect the PC he is sitting at) and it does not waive the
    DEADLINE, because outside the window the deadline is the dashboard's next
    pause window - i.e. the owner's evening, which is exactly what `--now` must
    not walk into.

    ⚠️ THIS IS A SECOND GATE CHAIN, NOT A CALL INTO `decide_start`, so every
    new gate has to be added HERE TOO or it silently does not exist on the
    `--now` path. The PRESENCE guard (2026-09-01) is the third one to learn
    that: the design's rule is "any listed process running means no new book
    starts of ANY kind", and a hand-run is a kind. The escape hatch is the one
    that already exists - `--ignore-control` skips this whole function - so a
    deliberate "yes, I know the game is open, run anyway" costs one flag rather
    than editing the list.
    """
    from app.core.ingest_control import (
        GPU_BUSY_PCT, control_blocks_start, control_defers_check, cpu_busy_words,
        cpu_guard, deadline_blocks_start, process_blocks_start,
    )

    # ⚠️ `window_ok=False` IS DELIBERATE AND IS THE POINT OF THIS CALL SITE
    # (owner ask 2026-08-23). This is the `--now` path — a person at a keyboard
    # who has already waived the window — so it is a MANUAL start by
    # definition, and a manual start is refused under BOTH pause modes. The
    # owner's "let the scheduled window continue" exempts the 12am-8am window,
    # not a hand-run that has just declared it is ignoring that window.
    # Passing the real clock here would let `--now` at 2am walk through a pause.
    blocked = control_defers_check(control) or control_blocks_start(control, window_ok=False)
    if blocked:
        return blocked
    in_use = process_blocks_start(control)
    if in_use:
        return in_use
    if not needs_gpu:
        cpu = cpu_guard()
        return cpu_busy_words(cpu) if cpu.busy else None
    over = deadline_blocks_start(control, audio_seconds=audio_seconds)
    if over:
        return over
    util = gpu_utilisation()
    if util is None:
        return "GPU utilisation unreadable - treating as busy"
    if util > GPU_BUSY_PCT:
        return f"GPU at {util}% (> {GPU_BUSY_PCT}%)"
    cpu = cpu_guard()
    return cpu_busy_words(cpu) if cpu.busy else None


def _write_receipt(state: dict, done: int, failed: int, dry_run: bool) -> None:
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    books = state.get("books", {})
    receipt = {
        "run_at": phoenix_now().isoformat(),
        "ingester_version": INGESTER_VERSION,
        "dry_run": dry_run,
        "packed_this_run": done,
        "failed_this_run": failed,
        "total_done": sum(1 for b in books.values() if b.get("status") == STATUS_DONE),
        "total_needs_ocr": sum(1 for b in books.values() if b.get("status") == STATUS_NEEDS_OCR),
        "total_failed": sum(1 for b in books.values() if b.get("status") == STATUS_FAILED),
    }
    path = RECEIPTS_DIR / f"ingest-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=1)
    log("receipt: " + json.dumps(receipt))


def publish_index(state: dict, dry_run: bool = False) -> None:
    entries = [
        {"book_id": bid, **{k: v for k, v in e.items() if k != "reason"}}
        for bid, e in state.get("books", {}).items()
    ]
    index = build_index(entries)
    gz = write_pack_gz({**index, "book_id": "_index"}, PACKS_DIR)
    if not dry_run:
        from app.core.ingest_pack import INDEX_KEY, r2_put

        r2_put(INDEX_KEY, gz)
        log(f"index published: {index['count']} books -> {INDEX_KEY}")


def status(args) -> int:
    from app.core.ingest_control import (
        CPU_BUSY_PCT, GPU_BUSY_PCT, cpu_utilisation, estimate_against_deadline,
        next_boundary, realtime_factor, recent_realtime_factors,
    )

    control = read_control()
    now = phoenix_now()
    util = gpu_utilisation()
    cpu = cpu_utilisation()
    state = load_state()
    boundary, boundary_label = next_boundary(control, now)
    factors = recent_realtime_factors()
    factor, factor_words = realtime_factor(factors)
    # A median-length book (12.1 h, measured over the 1,079 catalog rows) as the
    # worked example, so `--status` shows the gate's arithmetic rather than just
    # its constants.
    example = estimate_against_deadline(control, now, 12.1 * 3600, factors)
    print(json.dumps({
        "phoenix_now": now.isoformat(),
        "machine_tz_is_phoenix": machine_tz_is_phoenix(),
        "in_window": in_window(now),
        "may_start_new_book": may_start_new_book(now),
        "batch_size_now": batch_size_for(now),
        "gpu_pct": util,
        "gpu_busy_above": GPU_BUSY_PCT,
        # ⚠️ null means UNREADABLE, which the guard treats as BUSY - never idle.
        "cpu_pct": cpu,
        "cpu_busy_above": CPU_BUSY_PCT,
        "deadline": {
            "next_boundary": boundary.isoformat() if boundary else None,
            "next_boundary_is": boundary_label,
            "realtime_factor_used": round(factor, 1),
            "realtime_factor_basis": factor_words,
            "recent_measured_factors": [round(f, 1) for f in factors],
            "example_12h_book": example.words,
        },
        "control": {
            "paused": control.paused, "paused_until": control.paused_until,
            # ⚠️ `paused: true` alone no longer says what the pause MEANS — the
            # mode is half the answer, so --status prints both or it is lying
            # by omission (owner ask 2026-08-23).
            "pause_mode": control.pause_mode,
            "pause_mode_means": pause_mode_words(control.pause_mode),
            "dont_check_until": control.dont_check_until,
            "pause_windows": control.pause_windows,
            # ⚠️ `requeue` is what is PENDING CONSUMPTION, not what was applied -
            # a non-empty list here means the next run will act on it, and an
            # empty one after a run means it already did.
            "requeue_pending": control.requeue,
            "priority_front": control.priority_front,
            "readable": control.readable, "error": control.error,
        },
        "state_path": str(STATE_PATH),
        "books_done": sum(1 for b in state.get("books", {}).values()
                          if b.get("status") == STATUS_DONE),
        "books_needs_ocr": sum(1 for b in state.get("books", {}).values()
                               if b.get("status") == STATUS_NEEDS_OCR),
        "books_failed": sum(1 for b in state.get("books", {}).values()
                            if b.get("status") == STATUS_FAILED),
    }, indent=1))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--status", action="store_true", help="print gates and state, do nothing")
    p.add_argument("--run", action="store_true", help="process the queue")
    p.add_argument("--now", action="store_true", help="bypass the WINDOW only")
    p.add_argument("--cpu-only", action="store_true", help="EPUB/PDF only, no GPU work")
    p.add_argument("--dry-run", action="store_true", help="build packs, upload nothing")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--opportunistic", action="store_true",
                   help="allow idle-time starts outside the window")
    p.add_argument("--no-reviews", action="store_true", help="skip the review-count read")
    p.add_argument("--no-transcribe", action="store_true",
                   help="pack only what already has text; never start the GPU")
    p.add_argument("--ignore-control", action="store_true",
                   help="⚠️ ignore the dashboard pause (break-glass only)")
    p.add_argument("--publish-index", action="store_true")
    p.add_argument("--requeue-failed", action="store_true",
                   help="put back every `failed` book whose file NOW resolves, and "
                        "only those; combine with --dry-run to see the list first")
    args = p.parse_args(argv)

    # Before every GATE (window, GPU, pause, CPU, deadline) — this transcribes
    # nothing and starts no GPU, and it must be runnable while ingestion is
    # PAUSED, which is exactly when somebody wants it. It returns here rather
    # than falling through to a run: the next window picks the books up.
    #
    # ⚠️ BUT IT TAKES THE LOCK TO WRITE. A live run holds `state` in memory for
    # its whole night and calls `save_state` after every book, so a requeue
    # written underneath it is silently overwritten by the next book that
    # finishes — the change looks applied, the file disagrees an hour later.
    # `--dry-run` needs no lock because it writes nothing, and that is the
    # form to reach for while a run is in flight.
    if args.requeue_failed:
        if args.dry_run:
            requeue_failed(dry_run=True)
            return 0
        with _Lock() as lock:
            if not lock.acquired:
                log("requeue-failed: NOT APPLIED — an ingest run holds the lock, and a "
                    "requeue written under a live run is overwritten by its next "
                    "save_state. Re-run when it finishes, or use --dry-run to look.")
                return 1
            requeue_failed(dry_run=False)
        return 0

    if args.status or not (args.run or args.publish_index):
        return status(args)

    with _Lock() as lock:
        if not lock.acquired:
            return 0
        rc = run(args) if args.run else 0
        if args.publish_index:
            publish_index(load_state(), dry_run=args.dry_run)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
