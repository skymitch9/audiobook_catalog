#!/usr/bin/env python3
"""Transcribe ONE audiobook: m4b -> 16 kHz mono WAV -> Whisper -> JSON, WAV deleted.

The lasting home of the pilot machinery that produced the Primal Hunter
transcripts on 2026-08-18. ⚠️ THE PILOT LIVED IN `.claude/jobs/*/tmp/`, WHICH IS
SWEPT - the scripts, the model settings and the venv all had to move somewhere a
cleanup cannot reach, or the first tmp prune would delete the only working
copy of the machinery and leave 180 MB of transcripts nobody could reproduce.

MEASURED SETTINGS, NOT CHOSEN ONES (pilot, RTX 4080 SUPER 16 GB):

    large-v3 / float16 / batch 8   ->  85.3x realtime, 10.3 GB VRAM peak
    large-v3 / float16 / batch 16  -> 102.6x realtime, 12.8 GB VRAM peak

⚠️ Batch 16 is for the NIGHTLY WINDOW ONLY. 12.8 GB of 16 GB leaves too little
for anyone using the machine, and the point of the window is that nobody is.
`app/core/ingest_control.batch_size_for()` picks; this script just obeys
`--batch-size`. Measured across books 1-7 in production the sustained rate is
80-86x, i.e. the pilot figure holds.

⚠️ A SUBPROCESS, AND A SEPARATE INTERPRETER. The Whisper venv is its own Python
with a CUDA DLL bootstrap (`_cuda_boot`) that the repo's interpreter neither has
nor should acquire; and running each book in a fresh process is what guarantees
VRAM is fully released between books and that a CUDA crash kills one book rather
than the night.

⚠️ THE OUTPUT NEVER TOUCHES A GIT REPO. Transcripts land in
`C:\\Users\\nbasl\\estate-training-data\\transcripts` - outside every repository,
which is the guard itself rather than a gitignore line someone can `git add -f`
past. Owner, 2026-08-18: *"this is data that could lead to piracy if it were to
get out"*, and this repo is PUBLIC.

⚠️ THIS SCRIPT IS THE ONE PLACE PROGRESS CAN BE OBSERVED, which is why the
progress file is written HERE and not in `app/tools/ingest_books.py`. Both
invocation paths pass through this file: the nightly's `transcribe()` runs it as
a subprocess, and a hand-run chain calls it directly with `--m4b`. Putting the
tee in the nightly would have left every hand run invisible - and hand runs are
exactly the ones somebody is watching. See PROGRESS_PATH below.

USAGE
    python scripts/transcribe_audiobook.py --title "The Primal Hunter 12 - A LitRPG Adventure"
    python scripts/transcribe_audiobook.py --m4b "C:/path/to/book.m4b" --batch-size 16
    python scripts/transcribe_audiobook.py --title "..." --dry-run

Exit 0 = a complete transcript is on disk. Exit 3 = the transcript was
TRUNCATED (span under 95% of the container's duration) and has been deleted -
⚠️ a short transcript that reports success is worse than a failure, because it
silently becomes a book GABI has "read" only the first half of.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.core.ingest_queue import (  # noqa: E402
    TRANSCRIPTS_DIR, load_catalog, transcript_filename_stem,
)
from app.core.m4b_resolver import resolve_book_file  # noqa: E402

TRAINING_ROOT = Path(os.getenv("ESTATE_TRAINING_ROOT", r"C:\Users\nbasl\estate-training-data"))
WHISPER_PYTHON = Path(os.getenv(
    "ESTATE_WHISPER_PYTHON", str(TRAINING_ROOT / "whisper-venv" / "Scripts" / "python.exe")))
WORK_DIR = TRAINING_ROOT / "work"
LIBRARY_ROOT = Path(os.getenv("ROOT_DIR", r"C:\Users\nbasl\OpenAudible\books"))

MODEL = "large-v3"
COMPUTE_TYPE = "float16"
MIN_SPAN_RATIO = 0.95   # the truncation gate; see the module docstring

# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------
# ⚠️ WHY THIS FILE EXISTS. https://heygabi.ai/status/processing shows the book
# being transcribed right now, and the owner asked for a per-book PERCENTAGE.
# Until this, there was none to give: the Whisper worker prints a real progress
# line every 60 s, but the nightly ran it with `capture_output=True`, so those
# lines sat unread in a pipe until the book finished. Nothing on disk counted
# finished units mid-book, so the status pusher omitted `percent` entirely
# rather than publish an elapsed-time guess (the page draws a bar from that
# field and promises never to estimate one).
#
# ⚠️ THE PERCENTAGE IS A MEASUREMENT AND MUST STAY ONE. It is
# `transcribed span / container duration` - the SAME ratio the truncation gate
# above uses to decide whether a finished transcript is complete. The span comes
# from the model's own segment timestamps; the duration from ffprobe. Neither is
# a rate, an extrapolation, or a clock reading, and nothing here may quietly
# become one.
PROGRESS_PATH = WORK_DIR / "transcribe_progress.json"

# ⚠️ The worker's own line, and the format is load-bearing:
#     [whisper] 1.25h audio | 12.3min wall | 61.0x rt | 18234 words
# `%.2fh audio` is `s.end / 3600` - the END TIMESTAMP of the last segment
# handled, i.e. how much of the BOOK is done, not how long we have been running.
# The "model loaded in 12.3s" line also starts with `[whisper]` and deliberately
# does not match: model loading is not transcription progress.
_WHISPER_PROGRESS_RE = re.compile(
    r"^\[whisper\]\s+([0-9.]+)h audio\s*\|\s*([0-9.]+)min wall\s*\|\s*([0-9.]+)x rt\s*\|\s*(\d+) words")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_whisper_progress(line: str) -> dict | None:
    """One worker stdout line -> its numbers, or None if it is not a progress line."""
    match = _WHISPER_PROGRESS_RE.match(line.strip())
    if not match:
        return None
    return {
        "audio_hours_done": float(match.group(1)),
        "wall_minutes": float(match.group(2)),
        "realtime_factor": float(match.group(3)),
        "words": int(match.group(4)),
    }


def write_progress(m4b, title: str, container_s: float, started_at: str,
                   audio_hours_done: float, wall_minutes: float | None = None,
                   realtime_factor: float | None = None, words: int | None = None,
                   path: Path | None = None, now: str | None = None) -> bool:
    """Publish one progress snapshot, atomically, and NEVER raise.

    ⚠️ A STATUS WRITE MUST NOT BE ABLE TO KILL A TRANSCRIPTION. A full disk, a
    locked file or a permissions change is a reason for the status page to go
    quiet - the page says so honestly - and never a reason to lose twenty GPU
    minutes. Hence the deliberately broad except and the boolean return: the
    caller logs a warning at most.

    ⚠️ Written tmp-then-`os.replace`, which is atomic on NTFS, because the
    reader is a pusher polling every 15 minutes and a half-written file would
    surface as "the pipeline is not pushing" rather than as an error.
    """
    target = PROGRESS_PATH if path is None else path
    span_s = audio_hours_done * 3600.0
    percent = None
    if container_s and container_s > 0:
        percent = round(max(0.0, min(100.0, span_s / container_s * 100.0)), 1)
    record = {
        "source_m4b": str(m4b),
        "title": title,
        "audio_seconds_done": round(span_s, 3),
        "audio_hours_done": audio_hours_done,
        "container_duration_s": container_s,
        "percent": percent,
        "started_at": started_at,
        "updated_at": now or _utc_now(),
    }
    # Nice-to-haves. Absent rather than null when the line did not carry them,
    # so a reader can never mistake "not reported" for "measured as nothing".
    if wall_minutes is not None:
        record["wall_minutes"] = wall_minutes
    if realtime_factor is not None:
        record["realtime_factor"] = realtime_factor
    if words is not None:
        record["words"] = words
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except Exception:
        return False


def clear_progress(path: Path | None = None) -> None:
    """Remove the progress file. ⚠️ A STALE ONE MUST NOT OUTLIVE ITS RUN.

    Called on EVERY exit from the worker section - success, non-zero worker,
    truncation and exception alike - because a progress file left behind claims
    a book is being transcribed when the GPU is idle, and that is a lie the
    status page would render as fact. The reader applies a staleness cut-off
    too; this is the belt and that is the braces.
    """
    target = PROGRESS_PATH if path is None else path
    try:
        if target.exists():
            target.unlink()
    except OSError:
        pass


def _echo(raw: bytes) -> None:
    """Pass one worker stdout line through, BYTE FOR BYTE.

    ⚠️ BYTES, NOT TEXT, AND THAT IS THE WHOLE POINT. Before this relay existed
    the worker inherited the parent's stdout handle and its bytes reached the
    console or the nightly's log untouched. Decoding and re-encoding them here
    would invent a UnicodeEncodeError on any console whose codepage is not
    UTF-8 - a brand-new way for a twenty-minute book to die, in the name of a
    status page. The worker's `DONE {json}` line carries a book's file path, and
    those are not ASCII.
    """
    try:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
    except (AttributeError, ValueError):
        # No binary buffer (a captured or wrapped stdout). Lossy is acceptable
        # HERE and nowhere else: this branch only runs under test capture.
        sys.stdout.write(raw.decode("utf-8", "replace"))
        sys.stdout.flush()


def run_worker(cmd, m4b, title: str, container_s: float,
               started_at: str | None = None, popen=subprocess.Popen) -> int:
    """Run the Whisper worker, relaying its stdout and publishing progress.

    ⚠️ THE RELAY MUST NOT CHANGE WHAT ANYONE SEES. The nightly captures this
    process's stdout into `output_files/ingest_nightly.log`; a hand-run chain
    watches it in a console. Both keep every byte the worker wrote, in order,
    flushed per line - the only difference is that this process now also reads
    them on the way past.

    ⚠️ STDERR IS NOT PIPED, deliberately. It stays inherited, so a traceback
    still lands where it always did, and there is no second pipe to deadlock on
    while this loop is blocked reading the first.
    """
    started_at = started_at or _utc_now()
    clear_progress()   # a previous run killed outright cannot leave one behind
    proc = popen(cmd, cwd=str(WORK_DIR), stdout=subprocess.PIPE)
    try:
        for raw in proc.stdout:
            _echo(raw)
            hit = parse_whisper_progress(raw.decode("utf-8", "replace"))
            if hit:
                write_progress(m4b=m4b, title=title, container_s=container_s,
                               started_at=started_at, **hit)
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()
    return proc.returncode


# The worker that runs inside the Whisper venv. Written to disk next to the venv
# rather than imported, because this repo's interpreter cannot import
# faster_whisper and must never try.
_WORKER = r'''
import json, os, sys, time
_base = os.path.join(os.path.dirname(os.path.dirname(sys.executable)),
                     "Lib", "site-packages", "nvidia")
for _sub in ("cublas", "cudnn", "cuda_nvrtc"):
    _d = os.path.join(_base, _sub, "bin")
    if os.path.isdir(_d):
        os.add_dll_directory(_d)
        os.environ["PATH"] = _d + os.pathsep + os.environ["PATH"]

from faster_whisper import WhisperModel, BatchedInferencePipeline

WAV, SRC_M4B, OUT_JSON, OUT_TXT = sys.argv[1:5]
CONT_DUR = float(sys.argv[5]); BS = int(sys.argv[6])
MODEL, CT = sys.argv[7], sys.argv[8]

t0 = time.time()
pipe = BatchedInferencePipeline(model=WhisperModel(MODEL, device="cuda", compute_type=CT))
print("[whisper] model loaded in %.1fs" % (time.time() - t0), flush=True)

t1 = time.time()
segments, info = pipe.transcribe(WAV, language="en", batch_size=BS,
                                 word_timestamps=True, vad_filter=True)
segs, nwords, last = [], 0, time.time()
for s in segments:
    words = [{"w": w.word, "s": round(w.start, 3), "e": round(w.end, 3),
              "p": round(w.probability, 3)} for w in (s.words or [])]
    nwords += len(words)
    segs.append({"id": s.id, "start": round(s.start, 3), "end": round(s.end, 3),
                 "text": s.text, "words": words,
                 "avg_logprob": round(s.avg_logprob, 4),
                 "no_speech_prob": round(s.no_speech_prob, 4),
                 "compression_ratio": round(s.compression_ratio, 3)})
    if time.time() - last > 60:
        el = time.time() - t1
        print("[whisper] %.2fh audio | %.1fmin wall | %.1fx rt | %d words"
              % (s.end / 3600, el / 60, s.end / el, nwords), flush=True)
        last = time.time()

wall = time.time() - t1
span = segs[-1]["end"] if segs else 0
meta = {"source_m4b": SRC_M4B, "model": MODEL, "compute_type": CT,
        "batch_size": BS, "device": "cuda",
        "gpu": "NVIDIA GeForce RTX 4080 SUPER (16 GB)",
        "vad_filter": True, "word_timestamps": True, "initial_prompt": None,
        "container_duration_s": CONT_DUR, "audio_duration_s": info.duration,
        "transcribed_span_s": span, "wall_clock_s": round(wall, 1),
        "realtime_factor": round(span / wall, 1) if wall else 0,
        "n_segments": len(segs), "n_words": nwords,
        "run_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

json.dump({"meta": meta, "segments": segs},
          open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False)
with open(OUT_TXT, "w", encoding="utf-8") as f:
    for s in segs:
        f.write("[%02d:%02d:%06.3f] %s\n" % (s["start"] // 3600,
                s["start"] % 3600 // 60, s["start"] % 60, s["text"].strip()))
print("DONE " + json.dumps(meta), flush=True)
'''


def worker_path() -> Path:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    path = WORK_DIR / "_whisper_worker.py"
    if not path.exists() or path.read_text(encoding="utf-8") != _WORKER:
        path.write_text(_WORKER, encoding="utf-8")
    return path


def resolve_m4b(title: str) -> Path:
    """Title -> the m4b on disk. A THIN WRAPPER; the rules live one layer down.

    ⚠️ THE DECISION MOVED, 2026-08-26. Every rule this function used to hold is
    now in `app/core/m4b_resolver.py`, because a title->file join that lives in
    a script is a join the nightly ingester cannot share, and the 12 books that
    read `transcription failed` on 2026-08-25 all failed HERE — on a filename
    guess, never on transcription. Read that module's header for the tiers and
    the measured filename shapes; it is the single canonical implementation and
    the estate forbids a second copy.

    What is kept, unchanged, is this function's contract: it resolves or it
    raises, and it NEVER picks between candidates. `AmbiguousBookFile` and
    `BookFileNotFound` are both `FileNotFoundError`, so every existing caller
    and every existing except-clause behaves exactly as before.

    The module globals are read at CALL time on purpose — the resolver tests
    monkeypatch `LIBRARY_ROOT` and `load_catalog` on this module.
    """
    return resolve_book_file(title, rows=load_catalog(), root=LIBRARY_ROOT)


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {proc.stderr[-400:]}")
    return float(proc.stdout.strip())


def to_wav(src: Path, wav: Path) -> float:
    if wav.exists():
        wav.unlink()
    wav.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
         "-vn", str(wav)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg rc={proc.returncode}: {proc.stderr[-1200:]}")
    return time.time() - t0


def remove(path: Path, tries: int = 5) -> bool:
    """A 2 GB WAV that will not delete is a disk-space bug two books later."""
    for _ in range(tries):
        try:
            if path.exists():
                path.unlink()
            return True
        except OSError:
            time.sleep(2)
    return not path.exists()


def transcribe(title: str, m4b: Path, batch_size: int, dry_run: bool = False) -> int:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    # ⚠️ The stem comes from the SHARED formula so the packer's lookup can
    # probe for exactly this file — see transcript_filename_stem's docstring.
    stem = transcript_filename_stem(title) or m4b.stem
    out_json = TRANSCRIPTS_DIR / f"{stem}.json"
    out_txt = TRANSCRIPTS_DIR / f"{stem}.txt"
    if out_json.exists():
        print(f"[transcribe] already have {out_json.name}; nothing to do")
        return 0

    duration = probe_duration(m4b)
    print(f"[transcribe] {title!r}\n  m4b: {m4b}\n  duration: {duration/3600:.2f} h"
          f"  batch: {batch_size}")
    if dry_run:
        print("[transcribe] --dry-run: stopping before any GPU work")
        return 0

    if not WHISPER_PYTHON.exists():
        raise SystemExit(
            f"Whisper interpreter not found at {WHISPER_PYTHON}. Set "
            f"ESTATE_WHISPER_PYTHON, or recreate the venv "
            f"(`py -m venv`, then `pip install faster-whisper`).")

    wav = WORK_DIR / f"{stem}_16k.wav"
    try:
        secs = to_wav(m4b, wav)
        print(f"[transcribe] wav ready in {secs:.0f}s ({wav.stat().st_size:,} bytes)")
        rc = run_worker(
            [str(WHISPER_PYTHON), str(worker_path()), str(wav), str(m4b),
             str(out_json), str(out_txt), f"{duration:.3f}", str(batch_size),
             MODEL, COMPUTE_TYPE],
            m4b=m4b, title=title, container_s=duration)
        if rc != 0:
            raise RuntimeError(f"whisper worker rc={rc}")
    finally:
        # ⚠️ BOTH cleanups belong here and not after the truncation check below.
        # This block runs on success, on a non-zero worker, and on any exception
        # — which is exactly the set of ways a progress file could otherwise be
        # left behind claiming a book is transcribing while the GPU sits idle.
        clear_progress()
        if not remove(wav):
            print(f"[transcribe] WARN: could not delete {wav}")

    meta = json.loads(out_json.read_text(encoding="utf-8"))["meta"]
    span, container = meta["transcribed_span_s"], meta["container_duration_s"]
    if span < MIN_SPAN_RATIO * container:
        for path in (out_json, out_txt):
            if path.exists():
                path.unlink()
        print(f"[transcribe] TRUNCATED: {span:.0f}s of {container:.0f}s "
              f"(< {MIN_SPAN_RATIO:.0%}); transcript deleted so it cannot be "
              f"packed as if complete")
        return 3
    print(f"[transcribe] OK {meta['realtime_factor']}x realtime, "
          f"{meta['n_words']:,} words -> {out_json}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--title", help="catalog title; resolved to an .m4b")
    g.add_argument("--m4b", help="explicit path to the .m4b")
    p.add_argument("--batch-size", type=int, default=8,
                   help="8 outside the window (default), 16 inside it")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    m4b = Path(args.m4b) if args.m4b else resolve_m4b(args.title)
    title = args.title or m4b.stem
    return transcribe(title, m4b, args.batch_size, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
