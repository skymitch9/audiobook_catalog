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
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.core.ingest_queue import TRANSCRIPTS_DIR, load_catalog  # noqa: E402
from app.core.review_join import normalise_title  # noqa: E402

TRAINING_ROOT = Path(os.getenv("ESTATE_TRAINING_ROOT", r"C:\Users\nbasl\estate-training-data"))
WHISPER_PYTHON = Path(os.getenv(
    "ESTATE_WHISPER_PYTHON", str(TRAINING_ROOT / "whisper-venv" / "Scripts" / "python.exe")))
WORK_DIR = TRAINING_ROOT / "work"
LIBRARY_ROOT = Path(os.getenv("ROOT_DIR", r"C:\Users\nbasl\OpenAudible\books"))

MODEL = "large-v3"
COMPUTE_TYPE = "float16"
MIN_SPAN_RATIO = 0.95   # the truncation gate; see the module docstring

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
    """Title -> the m4b on disk, joined on the normalised title.

    ⚠️ Normalised, never exact. `The Primal Hunter 9: A LitRPG Adventure` is on
    disk as `...9- A LitRPG Adventure.m4b` because Windows forbids a colon in a
    filename; an exact match loses that book and says only "not found".
    """
    want = normalise_title(title)
    for path in LIBRARY_ROOT.rglob("*.m4b"):
        if normalise_title(path.stem) == want:
            return path
    # Second pass: the catalog may know an author folder the title does not.
    for row in load_catalog():
        if normalise_title(row.get("title", "")) == want:
            for path in LIBRARY_ROOT.rglob("*.m4b"):
                if normalise_title(path.stem) == normalise_title(row.get("title", "")):
                    return path
    raise FileNotFoundError(f"no .m4b under {LIBRARY_ROOT} matches {title!r}")


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
    stem = normalise_title(title).replace(" ", "_")[:120] or m4b.stem
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
        proc = subprocess.run(
            [str(WHISPER_PYTHON), str(worker_path()), str(wav), str(m4b),
             str(out_json), str(out_txt), f"{duration:.3f}", str(batch_size),
             MODEL, COMPUTE_TYPE],
            cwd=str(WORK_DIR))
        if proc.returncode != 0:
            raise RuntimeError(f"whisper worker rc={proc.returncode}")
    finally:
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
