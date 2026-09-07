"""Where is the `moov` atom in our m4b files — the head, or the tail?

⚠️ WHY THIS QUESTION DECIDES SOMETHING. An MP4/M4B keeps its index (`moov`)
either before the audio (`mdat`) or after it. Before = "faststart": a browser
reads a few hundred KB from byte 0 and can seek anywhere. After = the player
must issue a second range request for the END of the file before it can play or
seek at all. On a 601 MB book over a phone connection that is the difference
between "press play and it starts" and "press play and nothing happens for a
while" — and it lands on `audiobook-worker`'s `GET /api/audio/:anchor/file`,
where every range is a separate authorised request.

Design of record: `catalog-platform/docs/info/audio-player-design.md`. Phase 2
listed this survey as a measurement it could not take (docs/TODO.md, "Phase 2's
own leftovers", item 2). Everything the design says about ranging the tail has
been REASONED, never measured, and this is the script that measures it.

HOW IT MEASURES, and why not ffprobe alone:

  · PRIMARY — a top-level box walk. An MP4 is a flat list of boxes at the top
    level, each announcing its own size, so the offsets of `moov` and `mdat`
    are found by reading 8-16 bytes per box and SEEKING past the payload.
    ⚠️ It never reads the audio: surveying 1,084 books costs about as much I/O
    as listing the directory. It also yields the number the player actually
    needs — the exact byte offset and size of `moov` — which ffprobe does not
    print at any verbosity.
  · CROSS-CHECK — `ffprobe -v trace`, which logs each box as it parses it, on a
    sample (`--ffprobe-sample N`, default 12). ⚠️ Present because a hand-rolled
    parser that is confidently wrong would produce a clean, plausible table,
    and this whole survey exists to replace reasoning with measurement. If the
    two disagree on any sampled file, that is reported loudly and the run exits
    non-zero.

READ-ONLY on the media, permanently: it opens files 'rb', seeks, and writes
nothing anywhere near the library.

Usage:
    python scripts/survey_moov_atoms.py                  # the whole library
    python scripts/survey_moov_atoms.py --limit 50       # first 50, for a smoke test
    python scripts/survey_moov_atoms.py --json out.json  # full per-file detail
    python scripts/survey_moov_atoms.py --ffprobe-sample 0   # skip the cross-check

Exit code: 0 = surveyed cleanly; 1 = the ffprobe cross-check disagreed with the
box walk on at least one file (trust neither number until that is understood);
2 = nothing could be surveyed at all.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # the repo's own pattern — build_ebook_manifest.py:154

EXIT_OK = 0
EXIT_DISAGREEMENT = 1
EXIT_NOTHING = 2

# A container box we walk into is not interesting here — only the top level is,
# because that is where `moov` and `mdat` sit in every file a normal muxer
# writes. A `moov` nested somewhere else is not a thing this format allows.
HEAD = "head"
TAIL = "tail"
UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# pure helpers — pinned by tests/test_survey_moov_atoms.py
# ---------------------------------------------------------------------------
def classify(moov_offset: Optional[int], mdat_offset: Optional[int]) -> str:
    """head / tail / unknown, from two offsets.

    ⚠️ UNKNOWN IS NOT A THIRD KIND OF FILE, it is the absence of an answer, and
    it must never be folded into either count. A file whose `moov` we could not
    find is a file we cannot say anything about — reporting it as `tail`
    ("safe") would inflate the very number this survey exists to establish, and
    reporting it as `head` would hide a real seek cost.
    """
    if moov_offset is None:
        return UNKNOWN
    if mdat_offset is None:
        # A `moov` and no `mdat` is a container with no media payload at the
        # top level. Nothing to be before or after, so nothing to claim.
        return UNKNOWN
    return HEAD if moov_offset < mdat_offset else TAIL


def tail_read_bytes(size: int, moov_offset: Optional[int]) -> Optional[int]:
    """How many bytes from the END a player must fetch to reach `moov`.

    This is the number the design needs and the only one that translates
    directly into a range request. None when we do not know where `moov` is —
    ⚠️ never 0, which would read as "no extra fetch needed".
    """
    if moov_offset is None:
        return None
    return max(0, size - moov_offset)


def walk_top_level(fh, file_size: int, max_boxes: int = 4096) -> List[Tuple[str, int, int]]:
    """[(box type, offset, size)] for the top-level boxes, in file order.

    ⚠️ Every arm of the size encoding is handled, because getting one wrong
    desynchronises the walk and then every later offset is fiction:
      · size 1  → the real size is the 64-bit `largesize` that follows the
                  type. This is NOT rare here: an `mdat` over 4 GiB must use
                  it, and so may a muxer that simply prefers it.
      · size 0  → the box runs to end of file (legal for the last box).
      · size < 8 and not 0/1 → corrupt. We stop rather than guess; a walk that
                  invents a step forward reports a moov position it made up.
    """
    boxes: List[Tuple[str, int, int]] = []
    offset = 0
    while offset < file_size and len(boxes) < max_boxes:
        fh.seek(offset)
        header = fh.read(8)
        if len(header) < 8:
            break
        size = struct.unpack(">I", header[:4])[0]
        box_type = header[4:8].decode("latin-1")
        header_len = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                break
            size = struct.unpack(">Q", ext)[0]
            header_len = 16
        elif size == 0:
            size = file_size - offset
        if size < header_len:
            break  # corrupt: refuse to invent a step
        boxes.append((box_type, offset, size))
        offset += size
    return boxes


def summarise(rows: List[dict]) -> dict:
    """Counts + the numbers a reader needs, from the per-file rows."""
    counts = {HEAD: 0, TAIL: 0, UNKNOWN: 0}
    for row in rows:
        counts[row["placement"]] = counts.get(row["placement"], 0) + 1
    tails = [r for r in rows if r["placement"] == TAIL]
    tails_by_size = sorted(tails, key=lambda r: r["size"], reverse=True)
    tail_reads = [r["tail_read_bytes"] for r in tails if r.get("tail_read_bytes") is not None]
    return {
        "files": len(rows),
        "counts": counts,
        "largest_tail_moov": [
            {
                "path": r["path"],
                "size": r["size"],
                "moov_offset": r["moov_offset"],
                "tail_read_bytes": r["tail_read_bytes"],
            }
            for r in tails_by_size[:10]
        ],
        "tail_read_bytes_max": max(tail_reads) if tail_reads else None,
        "tail_read_bytes_median": (sorted(tail_reads)[len(tail_reads) // 2] if tail_reads else None),
        "bytes_surveyed": sum(r["size"] for r in rows),
    }


def human(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    val = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if val < 1024 or unit == "TiB":
            return f"{int(val):,} B" if unit == "B" else f"{val:,.1f} {unit}"
        val /= 1024
    return f"{n} B"


# ---------------------------------------------------------------------------
def probe_file(path: Path) -> dict:
    size = path.stat().st_size
    row = {
        "path": str(path),
        "size": size,
        "moov_offset": None,
        "moov_size": None,
        "mdat_offset": None,
        "top_level": [],
        "error": None,
    }
    try:
        with open(path, "rb") as fh:
            boxes = walk_top_level(fh, size)
    except OSError as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["placement"] = UNKNOWN
        row["tail_read_bytes"] = None
        return row

    row["top_level"] = [b[0] for b in boxes]
    for box_type, offset, box_size in boxes:
        if box_type == "moov" and row["moov_offset"] is None:
            row["moov_offset"] = offset
            row["moov_size"] = box_size
        elif box_type == "mdat" and row["mdat_offset"] is None:
            row["mdat_offset"] = offset
    row["placement"] = classify(row["moov_offset"], row["mdat_offset"])
    row["tail_read_bytes"] = (
        tail_read_bytes(size, row["moov_offset"]) if row["placement"] == TAIL else None
    )
    return row


def ffprobe_placement(path: Path, timeout: int = 120) -> Optional[str]:
    """head / tail from `ffprobe -v trace`, or None when it could not answer.

    ⚠️ The CROSS-CHECK, not the measurement. It parses ffmpeg's own trace log,
    whose wording is not a stable interface — so an unparseable trace returns
    None (no opinion) rather than a disagreement. A None is reported as
    "ffprobe had no opinion", never as agreement.
    """
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "trace", "-i", str(path)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    seen: List[str] = []
    for line in (proc.stderr or "").splitlines():
        for name in ("moov", "mdat"):
            if f"type:'{name}'" in line and "parent:'root'" in line:
                if name not in seen:
                    seen.append(name)
    if "moov" not in seen or "mdat" not in seen:
        return None
    return HEAD if seen.index("moov") < seen.index("mdat") else TAIL


def find_m4b(root: Path) -> List[Path]:
    return sorted(p for p in root.rglob("*.m4b") if p.is_file())


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, default=None, help="library root (default: app.config.ROOT_DIR)")
    ap.add_argument("--limit", type=int, default=0, help="survey only the first N files (0 = all)")
    ap.add_argument("--json", type=Path, default=None, help="write full per-file detail here")
    ap.add_argument(
        "--ffprobe-sample",
        type=int,
        default=12,
        help="cross-check this many files with ffprobe -v trace (0 = skip)",
    )
    args = ap.parse_args(argv)

    root = args.root
    if root is None:
        from app.config import ROOT_DIR  # imported late: --help must not need .env

        root = ROOT_DIR
    if not root.exists():
        print(f"[BLOCKED] library root {root} does not exist — nothing surveyed.", file=sys.stderr)
        return EXIT_NOTHING

    files = find_m4b(root)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"[BLOCKED] no .m4b under {root} — nothing surveyed.", file=sys.stderr)
        return EXIT_NOTHING

    print(f"Surveying {len(files):,} .m4b under {root} — header reads only, no audio is read.")
    rows = [probe_file(p) for p in files]
    stats = summarise(rows)

    counts = stats["counts"]
    print("\n--- moov placement -------------------------------------------------")
    print(f"  head (faststart) : {counts.get(HEAD, 0):,}")
    print(f"  tail             : {counts.get(TAIL, 0):,}")
    # ⚠️ Printed even at zero, and never merged into either column above.
    print(f"  unknown          : {counts.get(UNKNOWN, 0):,}  (no answer, NOT a third kind of file)")
    print(f"  bytes surveyed   : {human(stats['bytes_surveyed'])}")

    if counts.get(TAIL, 0):
        print("\n--- largest tail-moov files (the expensive ones to seek) ------------")
        for row in stats["largest_tail_moov"]:
            print(
                f"  {human(row['size']):>10}  tail read {human(row['tail_read_bytes']):>10}  "
                f"{Path(row['path']).name}"
            )
        print(f"\n  worst tail read : {human(stats['tail_read_bytes_max'])}")
        print(f"  median tail read: {human(stats['tail_read_bytes_median'])}")

    bad = [r for r in rows if r["error"]]
    if bad:
        print(f"\n--- unreadable ({len(bad)}) ---")
        for row in bad[:10]:
            print(f"  {Path(row['path']).name}: {row['error']}")

    disagreements = []
    if args.ffprobe_sample and shutil.which("ffprobe"):
        # Sample across the list rather than the first N: the first N are one
        # author's shelf, and a per-author muxer difference would be invisible.
        step = max(1, len(rows) // args.ffprobe_sample)
        sample = rows[::step][: args.ffprobe_sample]
        checked = agreed = no_opinion = 0
        for row in sample:
            verdict = ffprobe_placement(Path(row["path"]))
            checked += 1
            if verdict is None:
                no_opinion += 1
            elif verdict == row["placement"]:
                agreed += 1
            else:
                disagreements.append(
                    {"path": row["path"], "box_walk": row["placement"], "ffprobe": verdict}
                )
        print("\n--- ffprobe -v trace cross-check -----------------------------------")
        print(f"  checked {checked} · agreed {agreed} · ffprobe had no opinion {no_opinion} · "
              f"DISAGREED {len(disagreements)}")
        for d in disagreements:
            print(f"  🔴 {Path(d['path']).name}: box walk says {d['box_walk']}, ffprobe says {d['ffprobe']}")
    elif args.ffprobe_sample:
        print("\n--- ffprobe cross-check SKIPPED: ffprobe is not on PATH. The box-walk numbers "
              "above are unverified by a second tool.")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        with args.json.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump({"root": str(root), "summary": stats, "files": rows}, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        print(f"\nfull detail → {args.json}")

    return EXIT_DISAGREEMENT if disagreements else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
