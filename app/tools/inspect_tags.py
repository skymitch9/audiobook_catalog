# app/tools/inspect_tags.py
# Recursively inspect MP4/M4B tags under a directory (from .env or CLI),
# and write JSON dumps under OUTPUT_DIR/json_tags/ mirroring the source tree.

from __future__ import annotations
import sys
import json
from pathlib import Path
from typing import Iterable, Tuple, Dict, Any, List

from mutagen.mp4 import MP4, MP4FreeForm

# Config: ROOT_DIR/EXTS are already in your project; OUTPUT_DIR is where we publish artifacts
from app.config import ROOT_DIR, EXTS, OUTPUT_DIR  # type: ignore

# Optional env-based default directory:
# If INSPECT_DIR is defined in config.py / .env, use it; otherwise fallback to ROOT_DIR.
try:
    from app.config import INSPECT_DIR  # type: ignore
    DEFAULT_DIR = INSPECT_DIR or ROOT_DIR
except Exception:
    DEFAULT_DIR = ROOT_DIR


def bytes_to_str(b: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return b.decode(enc).strip()
        except Exception:
            pass
    return b.decode("utf-8", errors="ignore").strip()


def gather_tags_for_file(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    """Return (dump_dict, console_lines) for a single file."""
    audio = MP4(str(path))
    tags = audio.tags or {}
    info = getattr(audio, "info", None)

    duration = int(getattr(info, "length", 0)) if info and getattr(info, "length", None) else None
    bitrate  = int(getattr(info, "bitrate", 0)) if info and getattr(info, "bitrate", None) else None

    dump: Dict[str, Any] = {
        "file": str(path),
        "duration_sec": duration,
        "bitrate_bps": bitrate,
        "tags": {}
    }

    lines: List[str] = []
    lines.append("=" * 80)
    lines.append(f"File: {path}")
    if info:
        lines.append(f"Duration (sec): {duration if duration is not None else 'N/A'}")
        lines.append(f"Bitrate (bps):  {bitrate if bitrate is not None else 'N/A'}")
    lines.append("=" * 80)
    lines.append("Raw tag keys (as seen by Mutagen):")
    lines.append(", ".join(sorted(k for k in tags.keys() if isinstance(k, str))))
    lines.append("=" * 80)

    # Pretty print each tag; also build JSON-dumpable structure
    for key, val in tags.items():
        lines.append(f"\n--- {key} ---")
        out_list = []
        iter_vals = val if isinstance(val, list) else [val]

        for i, v in enumerate(iter_vals):
            if isinstance(v, MP4FreeForm):
                raw = bytes(v)
                decoded = bytes_to_str(raw)
                lines.append(f"[{i}] MP4FreeForm  len={len(raw)}  decoded='{decoded}'")
                out_list.append({"type": "MP4FreeForm", "len": len(raw), "decoded": decoded})
            elif isinstance(v, (bytes, bytearray)):
                decoded = bytes_to_str(bytes(v))
                lines.append(f"[{i}] bytes       len={len(v)}  decoded='{decoded}'")
                out_list.append({"type": "bytes", "len": len(v), "decoded": decoded})
            elif isinstance(v, tuple):
                lines.append(f"[{i}] tuple       value={v}")
                out_list.append({"type": "tuple", "value": list(v)})
            else:
                lines.append(f"[{i}] {type(v).__name__}  value='{v}'")
                out_list.append({"type": type(v).__name__, "value": str(v)})

        dump["tags"][key] = out_list

    # Spotlight likely series fields (best guesses)
    lines.append("\n" + "=" * 80)
    lines.append("Likely Series-related keys (best guesses):")
    likely_suffixes = ("series", "seriesname", "series_index", "seriessequence", "series number", "series_no")
    for key, _ in tags.items():
        if isinstance(key, str) and key.startswith("----"):
            tail = key.split(":")[-1].lower()
            if any(tail.endswith(sfx) for sfx in likely_suffixes):
                lines.append(f"  candidate free-form -> {key}")
        if key in ("\xa9alb", "aART", "\xa9wrt", "\xa9ART"):
            lines.append(f"  candidate std atom  -> {key}")

    return dump, lines


def iter_audio_files(root: Path, exts: Iterable[str]) -> Iterable[Path]:
    exts_lc = {e.lower() for e in exts}
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts_lc:
            yield p


def write_dump_under_output(root_base: Path, src_file: Path, dump: Dict[str, Any]) -> Path:
    """
    Write JSON to OUTPUT_DIR/json_tags/<relative_path>/<filename>.tagdump.json
    where relative_path is src_file relative to root_base.
    """
    rel = src_file.relative_to(root_base)  # e.g., "Author/Book.m4b"
    out_dir = OUTPUT_DIR / "json_tags" / rel.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Keep original filename (with extension) for clarity, then append .tagdump.json
    out_name = rel.name + ".tagdump.json"  # e.g., "Book.m4b.tagdump.json"
    out_path = out_dir / out_name

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)

    return out_path


def main():
    # CLI usage:
    #   python -m app.tools.inspect_tags                 -> uses DEFAULT_DIR (env/config)
    #   python -m app.tools.inspect_tags /path/to/dir    -> recurse that directory
    #   python -m app.tools.inspect_tags /path/to/file.m4b
    if len(sys.argv) >= 2:
        target = Path(sys.argv[1]).expanduser()
    else:
        target = DEFAULT_DIR

    if not target.exists():
        print(f"[ERROR] Path not found: {target}")
        sys.exit(1)

    # Determine the base for relative paths (used to mirror into output_files/json_tags)
    if target.is_file():
        root_base = target.parent
        files = [target]
    else:
        root_base = target
        files = list(iter_audio_files(target, EXTS))

    if not files:
        print(f"No matching audio files found under: {target}")
        sys.exit(0)

    for f in files:
        try:
            dump, lines = gather_tags_for_file(f)
            print("\n".join(lines))  # console visibility
            out_json = write_dump_under_output(root_base, f, dump)
            print("=" * 80)
            print(f"Wrote JSON tag dump: {out_json}")
        except Exception as e:
            print(f"[WARN] Failed to inspect {f}: {e}")


if __name__ == "__main__":
    main()
