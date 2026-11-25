"""
Build scripts/audible_backfill.json from an audible-cli library export.

READ-ONLY on the library: it opens every m4b for its tags and writes ONE repo
file (and only with --write). It does not call Audible itself — run the export
first, by hand, once:

    python -m audible_cli -P <profile> library export -f json -o <dir>/<profile>.json

and, for DESCRIPTIONS (2026-11-24), the library with product text — the
export above has no description field:

    python -m audible_cli -P <profile> api 1.0/library \
        -p response_groups=product_desc,product_extended_attrs,product_attrs \
        -p num_results=1000 -f json -o <dir>/<profile>_desc.json

then:

    python -m app.tools.build_audible_backfill <dir>/<p>.json ... --summaries <dir>/<p>_desc.json ...          # report only
    python -m app.tools.build_audible_backfill <dir>/<p>.json ... --summaries <dir>/<p>_desc.json ... --write  # write the file

⚠️ Without --summaries every ``desc`` is dropped from the rebuilt file, so
always pass it.

WHICH FILES GET AN ENTRY
------------------------
Files whose tags carry one of Audible's container placeholders — genre
"Audiobook" (or none), or a placeholder year — and (2026-11-24) files that
carry NO description tag at all; see app/core/audible_backfill.py. A
description-only entry gets ``desc`` and nothing it changes otherwise: its
genre/year are only ever applied over a placeholder.
A file is matched to ONE export row by, in order:

1. its own CDEK tag (exact ASIN);
2. otherwise its title — the ``©nam`` with "(Unabridged)" dropped, compared
   (alphanumerics only, lowercased) with the export's ``title`` or
   ``title: subtitle`` — AND an author overlap AND a runtime within
   max(3 min, 3 %) of the export's ``runtime_length_min``.

Anything ambiguous (two candidate rows) or unmatched gets NO entry and is
listed, so a guess never reaches the catalogue.

⚠️ PUBLIC REPO: an entry stores product facts only (title, subtitle, authors,
genre, release date, runtime, Audible's public publisher summary). Never
purchase dates, listening progress or account names.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from app.core import audible_backfill as ab


def _norm_title(s: Optional[str]) -> str:
    s = re.sub(r"\(\s*unabridged\s*\)\s*$", "", s or "", flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _norm_person(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def load_exports(paths: List[Path]) -> Dict[str, dict]:
    rows: Dict[str, dict] = {}
    for p in paths:
        for r in json.loads(Path(p).read_text(encoding="utf-8")):
            if r.get("asin"):
                rows.setdefault(r["asin"], r)
    return rows


def load_summaries(paths: List[Path]) -> Dict[str, str]:
    """{asin: Audible's publisher_summary, as PLAIN TEXT} from ``audible api
    1.0/library`` JSON (``{"items": [...]}``). Only that one public field is
    read; everything else in the response (purchase dates, progress) is
    ignored and never stored."""
    from app.metadata import _html_to_plain_text

    out: Dict[str, str] = {}
    for p in paths:
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        for it in (doc.get("items") if isinstance(doc, dict) else doc) or []:
            asin = (it or {}).get("asin")
            text = _html_to_plain_text((it or {}).get("publisher_summary") or "")
            if asin and text:
                out.setdefault(asin, text)
    return out


def _authors_overlap(file_artist: str, export_authors: str) -> bool:
    fa = {_norm_person(a) for a in re.split(r"[,;&]", file_artist or "") if a.strip()}
    ea = {_norm_person(a) for a in re.split(r",", export_authors or "") if a.strip()}
    return bool(fa & ea)


def _runtime_ok(file_sec: Optional[float], export_min) -> bool:
    try:
        em = float(export_min)
    except (TypeError, ValueError):
        return False
    if not file_sec:
        return False
    fm = file_sec / 60.0
    return abs(fm - em) <= max(3.0, 0.03 * em)


def match_file(info: dict, exports: Dict[str, dict], by_title: Dict[str, List[str]]):
    """(asin, why) or (None, why-not)."""
    if info["cdek"]:
        return (info["cdek"], "cdek") if info["cdek"] in exports else (None, "CDEK not in export")
    cands = by_title.get(_norm_title(info["nam"]), [])
    cands = [a for a in cands if _authors_overlap(info["art"], exports[a].get("authors"))]
    cands = [a for a in cands if _runtime_ok(info["sec"], exports[a].get("runtime_length_min"))]
    cands = sorted(set(cands))
    if len(cands) == 1:
        return cands[0], "title+author+runtime"
    return None, ("no candidate" if not cands else f"ambiguous: {cands}")


def scan_library(root: Path) -> List[dict]:
    from mutagen.mp4 import MP4

    from app.config import EXTS
    from app.metadata import K_ARTIST, K_ASIN, K_DAY, K_GENRE, K_TITLE, _extract_description, get_tag_any, walk_library

    out = []
    for p in sorted(walk_library(root, set(EXTS))):
        try:
            a = MP4(str(p))
        except Exception:  # noqa: BLE001 — an unreadable file is reported, not fatal
            continue
        t = a.tags or {}
        out.append({
            "rel": os.path.relpath(str(p), str(root)).replace("\\", "/"),
            "nam": get_tag_any(t, [K_TITLE]) or "",
            "art": get_tag_any(t, [K_ARTIST]) or "",
            "cdek": get_tag_any(t, [K_ASIN]) or "",
            "gen": get_tag_any(t, [K_GENRE]) or "",
            "day": get_tag_any(t, [K_DAY]) or "",
            "sec": getattr(getattr(a, "info", None), "length", None),
            "has_desc": bool(_extract_description(t)),
        })
    return out


def known_genre_paths(files: List[dict]) -> List[str]:
    """Every real ``Top:Sub`` ladder the library's OpenAudible tags carry."""
    return sorted({f["gen"] for f in files if ":" in f["gen"]})


def build(files: List[dict], exports: Dict[str, dict], summaries: Optional[Dict[str, str]] = None) -> tuple:
    paths = known_genre_paths(files)
    by_title: Dict[str, List[str]] = {}
    for a, r in exports.items():
        keys = {_norm_title(r.get("title"))}
        if r.get("subtitle"):
            keys.add(_norm_title(f"{r['title']}: {r['subtitle']}"))
        for k in keys:
            by_title.setdefault(k, []).append(a)
    entries, unmatched = [], []
    summaries = summaries or {}
    for f in files:
        placeholder = ab.is_placeholder_genre(f["gen"]) or ab.is_placeholder_year(f["day"])
        wants_desc = bool(summaries) and not f.get("has_desc", True)
        if not (placeholder or wants_desc):
            continue
        asin, why = match_file(f, exports, by_title)
        if not asin:
            unmatched.append((f["rel"], why))
            continue
        r = exports[asin]
        genre = ab.first_known_ladder(r.get("genres") or "", paths)
        rd = (r.get("release_date") or "").strip()
        if ab.is_placeholder_year(rd):
            rd = ""
        match = {"asin": f["cdek"]} if f["cdek"] else {"title_tag": f["nam"], "artist_tag": f["art"]}
        desc = summaries.get(asin, "") if wants_desc else ""
        if not placeholder and not desc:
            continue  # description-only candidate, but Audible has no summary
        entries.append({
            "asin": asin,
            "book": f"{r.get('title')} - {r.get('authors')}",
            "match": match,
            "genre": genre,
            "release_date": rd,
            "evidence": {
                "matched_by": why,
                "audible_title": r.get("title") or "",
                "audible_subtitle": r.get("subtitle") or "",
                "audible_authors": r.get("authors") or "",
                "audible_genres": r.get("genres") or "",
                "audible_runtime_min": r.get("runtime_length_min"),
                "file_runtime_min": round(f["sec"] / 60) if f["sec"] else None,
                "tags_read": {"©gen": f["gen"], "©day": f["day"]},
            },
        })
        if desc:
            entries[-1]["desc"] = desc
    entries.sort(key=lambda e: (e["book"].lower(), e["asin"]))
    return entries, unmatched, paths


def main(argv=None) -> int:
    from app.config import ROOT_DIR

    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("exports", nargs="+", type=Path, help="audible-cli library export JSON file(s)")
    ap.add_argument("--summaries", nargs="*", type=Path, default=[],
                    help="audible-cli `api 1.0/library` JSON with product_desc (publisher summaries)")
    ap.add_argument("--write", action="store_true", help=f"write {ab.BACKFILL_PATH.name}")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    exports = load_exports(args.exports)
    files = scan_library(Path(ROOT_DIR))
    summaries = load_summaries(args.summaries)
    entries, unmatched, paths = build(files, exports, summaries)
    no_desc = sum(1 for f in files if not f["has_desc"])
    with_desc = sum(1 for e in entries if e.get("desc"))
    print(f"{len(files)} files read, {len(exports)} export rows, {len(summaries)} summaries; "
          f"{len(entries)} entries, {len(unmatched)} unmatched")
    print(f"descriptions: {no_desc} files have none; {with_desc} get Audible's summary")
    for rel, why in unmatched:
        print(f"  [UNMATCHED] {rel} — {why}")
    for e in entries:
        if not e["genre"]:
            print(f"  [NO GENRE] {e['book']} — no known ladder in {e['evidence']['audible_genres']!r}")
    if not args.write:
        print("report only — pass --write to write the file")
        return 0
    doc = {
        "_description": "GENERATED by `python -m app.tools.build_audible_backfill` from a read-only audible-cli "
                        "library export. Do not hand-edit: re-run the builder. Read by app/core/audible_backfill.py.",
        "_what_it_does": "Fills genre/year ONLY where a file's own tags carry Audible's container placeholders "
                         "(genre 'Audiobook', year '2000' or a future year), and desc ONLY where the file has no "
                         "description tag at all. Never overrides a real tag; "
                         "scripts/catalog_overrides.json still wins over it.",
        "_public_repo": "Product facts only (incl. Audible's public publisher summary). No purchase dates, "
                        "no account or profile names.",
        "_built": dt.date.today().isoformat(),
        "genre_paths": paths,
        "entries": entries,
    }
    tmp = ab.BACKFILL_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, ab.BACKFILL_PATH)
    print(f"wrote {ab.BACKFILL_PATH} ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
