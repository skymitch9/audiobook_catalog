import sys
import json
from pathlib import Path
from mutagen.mp4 import MP4, MP4FreeForm

def main():
    if len(sys.argv) < 2:
        print("Usage: python inspect_m4b_tags.py <path-to-file.m4b>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    audio = MP4(str(path))
    tags = audio.tags or {}

    print("="*80)
    print(f"File: {path}")
    info = getattr(audio, "info", None)
    if info:
        length = getattr(info, "length", None)
        bitrate = getattr(info, "bitrate", None)
        print(f"Duration (sec): {int(length) if length else 'N/A'}")
        print(f"Bitrate (bps):  {int(bitrate) if bitrate else 'N/A'}")
    print("="*80)
    print("Raw tag keys (as seen by Mutagen):")
    print(", ".join(sorted(k for k in tags.keys() if isinstance(k, str))))
    print("="*80)

    # Helper to decode bytes sensibly
    def bytes_to_str(b: bytes) -> str:
        for enc in ("utf-8", "utf-16", "latin-1"):
            try:
                return b.decode(enc).strip()
            except Exception:
                pass
        return b.decode("utf-8", errors="ignore").strip()

    # Collect a serializable dump as well
    dump = {
        "file": str(path),
        "duration_sec": int(getattr(info, "length", 0)) if info and getattr(info, "length", None) else None,
        "bitrate_bps": int(getattr(info, "bitrate", 0)) if info and getattr(info, "bitrate", None) else None,
        "tags": {}
    }

    # Pretty print each tag
    for key, val in tags.items():
        print(f"\n--- {key} ---")
        # Values are often lists
        out_list = []
        if isinstance(val, list):
            iter_vals = val
        else:
            iter_vals = [val]

        for i, v in enumerate(iter_vals):
            if isinstance(v, MP4FreeForm):
                raw = bytes(v)
                decoded = bytes_to_str(raw)
                print(f"[{i}] MP4FreeForm  len={len(raw)}  decoded='{decoded}'")
                out_list.append({"type": "MP4FreeForm", "len": len(raw), "decoded": decoded})
            elif isinstance(v, bytes):
                decoded = bytes_to_str(v)
                print(f"[{i}] bytes       len={len(v)}  decoded='{decoded}'")
                out_list.append({"type": "bytes", "len": len(v), "decoded": decoded})
            elif isinstance(v, tuple):
                # e.g., track/disc numbers look like [(2, 12)]
                print(f"[{i}] tuple       value={v}")
                out_list.append({"type": "tuple", "value": list(v)})
            else:
                print(f"[{i}] {type(v).__name__}  value='{v}'")
                out_list.append({"type": type(v).__name__, "value": str(v)})

        dump["tags"][key] = out_list

    # Spotlight likely fields for Series & Index (best-guess conventions)
    print("\n" + "="*80)
    print("Likely Series-related keys (best guesses):")
    likely_suffixes = ("series", "seriesname", "series_index", "seriessequence", "series number", "series_no")
    for key, val in tags.items():
        if isinstance(key, str) and key.startswith("----"):
            tail = key.split(":")[-1].lower()
            if any(tail.endswith(sfx) for sfx in likely_suffixes):
                print(f"  candidate free-form -> {key}")
        if key in ("\xa9alb", "aART", "\xa9wrt", "\xa9ART"):
            print(f"  candidate std atom  -> {key}")

    # Write JSON dump
    out_json = path.with_suffix(".tagdump.json")
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)
    print("="*80)
    print(f"Wrote JSON tag dump: {out_json}")

if __name__ == "__main__":
    main()
