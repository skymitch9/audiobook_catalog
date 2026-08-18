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
    python -m app.tools.ingest_books --dry-run ...     # build, never upload
    python -m app.tools.ingest_books --limit N

⚠️ `--now` BYPASSES THE WINDOW AND NOTHING ELSE. The pause control and the GPU
guard still apply, because those protect the owner's machine and the owner's
evening; the window only protects his daytime. A hand-run at build time is
exactly what it is for.

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
    ControlState, StartDecision, batch_size_for, decide_start, gpu_utilisation,
    in_window, machine_tz_is_phoenix, may_start_new_book, phoenix_now, read_control,
)
from app.core.ingest_pack import (
    INGESTER_VERSION, PackRefused, build_index, build_pack, pack_stats,
    upload_pack, write_pack_gz,
)
from app.core.ingest_queue import (
    PACKS_DIR, RECEIPTS_DIR, STATUS_DONE, STATUS_FAILED, STATUS_NEEDS_OCR,
    STATE_PATH, TIER_NEEDS_OCR, TRANSCRIPTS_DIR, QueueItem, build_queue,
    count_reviews_by_book_id, load_chapters, load_state, mark, save_state,
)
from app.core.review_join import normalise_title


LOCK_PATH = Path(__file__).resolve().parents[2] / "output_files" / "ingest_books.lock"
LOCK_STALE_HOURS = 12   # longer than the 8 h window, so a live run is never stolen


def log(msg: str) -> None:
    print(f"[{phoenix_now().strftime('%Y-%m-%d %H:%M:%S')} MST] {msg}", flush=True)


class _Lock:
    """Single-flight guard, because the scheduled task fires every 30 minutes.

    ⚠️ Without this, the 02:00 invocation starts a second transcription while the
    00:00 one is still running - two Whisper processes on a 16 GB card, both of
    which then OOM or thrash, and neither of which finishes. Stale after
    LOCK_STALE_HOURS so a machine that lost power does not stay locked forever.
    """

    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            age_h = (time.time() - self.path.stat().st_mtime) / 3600
            if age_h < LOCK_STALE_HOURS:
                log(f"another ingest run holds the lock ({age_h:.1f} h old); exiting")
                return self
            log(f"stale lock ({age_h:.1f} h) - taking it")
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "at": phoenix_now().isoformat()}),
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
        return m.group(1).encode().decode("unicode_escape")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("meta", {}).get("source_m4b", "") or ""
    except Exception:
        return ""


def _build_transcript_index() -> dict:
    index: dict = {}
    if not TRANSCRIPTS_DIR.exists():
        return index
    for path in sorted(TRANSCRIPTS_DIR.glob("*.json")):
        src = _transcript_source(path)
        stem = Path(src).stem if src else path.stem
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
    return _transcript_index.get(normalise_title(title))


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
        mark(state, item.book_id, STATUS_FAILED, reason="no text extracted",
             source=item.source)
        return None

    chunks, refs = chunk_book(book)
    extra = {"twin_of": item.twin_of} if item.twin_of else None
    try:
        pack = build_pack(book, chunks, refs, extra=extra)
    except PackRefused as exc:
        log(f"  REFUSED {item.title!r}: {exc}")
        mark(state, item.book_id, STATUS_FAILED, reason=str(exc), source=item.source)
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
    mark(state, item.book_id, STATUS_DONE, source=book.source,
         chunks=stats["chunks"], chapters=stats["chapters"],
         text_bytes=stats["text_bytes"], gz_bytes=stats["gz_bytes"],
         key=key, ingester_version=INGESTER_VERSION,
         twin_of=item.twin_of)
    log(f"  OK {item.title}  {stats['chunks']} chunks  {stats['gz_bytes']:,}B gz "
        f"(ratio {stats['gzip_ratio']})  -> {key}")
    return stats


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

def run(args) -> int:
    state = load_state()
    chapters = load_chapters()
    control = ControlState() if args.ignore_control else read_control()

    reviews = {} if args.no_reviews else count_reviews_by_book_id()
    if not reviews and not args.no_reviews:
        log("WARN: review counts unreadable - priority falls back to tier order. "
            "This is 'unknown', not 'no book has reviews'.")

    queue = build_queue(state=state, review_counts=reviews,
                        pdf_classifier=book_text.classify_pdf)
    if args.cpu_only:
        queue = [i for i in queue if not i.needs_gpu]
    if args.limit:
        queue = queue[:args.limit]

    log(f"queue: {len(queue)} books "
        f"({sum(1 for i in queue if not i.needs_gpu)} CPU, "
        f"{sum(1 for i in queue if i.needs_gpu)} GPU)")

    done = failed = 0
    for item in queue:
        if item.tier == TIER_NEEDS_OCR:
            # ⚠️ Recorded, not attempted. OCR is not built (owner, 2026-08-18);
            # this row is what stops the shelf looking as though the book is
            # simply missing.
            mark(state, item.book_id, STATUS_NEEDS_OCR, source="pdf-ocr",
                 reason=item.note or "image-scan PDF", blocker="OCR processor not built")
            save_state(state)
            continue

        # A transcript that already exists on disk makes this a CPU job, so it
        # must not be gated on the graphics card. Getting this wrong would make a
        # pack-only pass wait for a GPU it never touches.
        will_transcribe = item.needs_gpu and not args.no_transcribe and not _transcript_for(item.title)

        if not args.now:
            decision = decide_start(control, needs_gpu=will_transcribe,
                                    allow_opportunistic=args.opportunistic)
            if not decision.may_start:
                log(f"STOP before {item.title!r}: {decision.reason}")
                break
            batch = decision.batch_size
        else:
            # --now skips the window; the pause and the guard still bind.
            blocked = None if args.ignore_control else _control_or_guard(control, will_transcribe)
            if blocked:
                log(f"STOP before {item.title!r}: {blocked}")
                break
            batch = batch_size_for()

        try:
            if item.needs_gpu and not _transcript_for(item.title):
                if args.no_transcribe:
                    # Pack-only pass: everything whose text already exists gets
                    # packed, and nothing spends a GPU-minute. This is how a
                    # finished transcript set is turned into packs outside the
                    # window, and it is cheap enough to run any time.
                    continue
                if not transcribe(item, batch):
                    mark(state, item.book_id, STATUS_FAILED, reason="transcription failed")
                    failed += 1
                    save_state(state)
                    continue
            stats = pack_one(item, chapters, state, dry_run=args.dry_run)
            done += 1 if stats else 0
            failed += 0 if stats else 1
        except Exception as exc:  # one bad book never ends the night
            log(f"  ERROR {item.title!r}: {type(exc).__name__}: {exc}")
            mark(state, item.book_id, STATUS_FAILED, reason=f"{type(exc).__name__}: {exc}"[:300])
            failed += 1
        save_state(state)

    _write_receipt(state, done, failed, args.dry_run)
    log(f"run complete: {done} packed, {failed} failed")
    return 0


def _control_or_guard(control: ControlState, needs_gpu: bool) -> Optional[str]:
    """The gates that still bind under `--now` (which waives only the window)."""
    from app.core.ingest_control import GPU_BUSY_PCT, control_blocks_start, control_defers_check

    blocked = control_defers_check(control) or control_blocks_start(control)
    if blocked:
        return blocked
    if not needs_gpu:
        return None
    util = gpu_utilisation()
    if util is None:
        return "GPU utilisation unreadable - treating as busy"
    return f"GPU at {util}% (> {GPU_BUSY_PCT}%)" if util > GPU_BUSY_PCT else None


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
    control = read_control()
    now = phoenix_now()
    util = gpu_utilisation()
    state = load_state()
    print(json.dumps({
        "phoenix_now": now.isoformat(),
        "machine_tz_is_phoenix": machine_tz_is_phoenix(),
        "in_window": in_window(now),
        "may_start_new_book": may_start_new_book(now),
        "batch_size_now": batch_size_for(now),
        "gpu_pct": util,
        "control": {
            "paused": control.paused, "paused_until": control.paused_until,
            "dont_check_until": control.dont_check_until,
            "pause_windows": control.pause_windows,
            "readable": control.readable, "error": control.error,
        },
        "state_path": str(STATE_PATH),
        "books_done": sum(1 for b in state.get("books", {}).values()
                          if b.get("status") == STATUS_DONE),
        "books_needs_ocr": sum(1 for b in state.get("books", {}).values()
                               if b.get("status") == STATUS_NEEDS_OCR),
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
    args = p.parse_args(argv)

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
