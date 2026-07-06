"""
Extract chapter/part structure for every audiobook and write site/chapters.json,
which the book-club Start Read modal uses for chapter-based milestones.

Source chain per book (first hit wins, recorded in the entry's "source"):
  1. m4b metadata  — ffprobe (if installed) or mutagen's chapter atom
  2. Hardcover     — scaffold only: their public GraphQL API does not expose
                     chapter lists today; hook kept so a working query is a
                     small edit (needs HARDCOVER_API_TOKEN)
  3. Claude LLM    — asks claude-opus-4-8 for the chapter list; entries are
                     marked source "llm" and carry no timestamps
                     (needs the 'Claude-llm' or ANTHROPIC_API_KEY env var and
                     the 'anthropic' package)

Runs on the machine that has the audio library (not CI). Incremental: books
already in chapters.json are skipped unless --force. Books where every source
failed are recorded as source "none" and skipped unless --retry-missing.

Usage:
    python -m app.tools.extract_chapters              # extract new books
    python -m app.tools.extract_chapters --force      # redo everything
    python -m app.tools.extract_chapters --no-llm     # skip the Claude fallback
    python -m app.tools.extract_chapters --limit 10   # first N books only
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from mutagen.mp4 import MP4

from app.config import EXTS, OUTPUT_DIR, ROOT_DIR, SITE_DIR
from app.metadata import walk_library

CHAPTERS_PATH = SITE_DIR / "chapters.json"
# Cache of file -> tags so reruns skip processed files WITHOUT opening them
# (opening can re-hydrate OneDrive placeholder files). Local-only, gitignored.
TAG_CACHE_PATH = OUTPUT_DIR / "chapter_tag_cache.json"
CLAUDE_API_KEY = os.getenv("Claude-llm") or os.getenv("ANTHROPIC_API_KEY")
HARDCOVER_API_TOKEN = os.getenv("HARDCOVER_API_TOKEN")
MAX_REASONABLE_CHAPTERS = 400

PART_TITLE_RE = re.compile(
    r"^\s*(part|book|disc|volume)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Source 1: m4b metadata
# ---------------------------------------------------------------------------

def chapters_from_ffprobe(path: Path):
    """Read chapters with ffprobe. Returns [{'title', 'start_min'}] or None."""
    ffprobe = os.getenv("FFPROBE_PATH") or shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_chapters", str(path)],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        if result.returncode != 0:
            return None
        raw = json.loads(result.stdout or "{}").get("chapters", [])
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None
    chapters = []
    for i, ch in enumerate(raw):
        title = (ch.get("tags") or {}).get("title") or f"Chapter {i + 1}"
        try:
            start_min = round(float(ch.get("start_time", 0)) / 60, 1)
        except (TypeError, ValueError):
            start_min = None
        chapters.append({"title": title.strip(), "start_min": start_min})
    return chapters or None


def chapters_from_mutagen(path: Path):
    """Read the chapter atom via mutagen. Returns [{'title', 'start_min'}] or None.
    Note: mutagen reads Nero-style chapter atoms; QuickTime chapter *tracks*
    (common in some Audible-derived files) are not visible to it — ffprobe is
    the more complete reader when available."""
    try:
        audio = MP4(str(path))
        raw = getattr(audio, "chapters", None)
        if not raw:
            return None
        chapters = []
        for i, ch in enumerate(raw):
            title = (getattr(ch, "title", "") or f"Chapter {i + 1}").strip()
            start = getattr(ch, "start", None)
            start_min = round(float(start) / 60, 1) if start is not None else None
            chapters.append({"title": title, "start_min": start_min})
        return chapters or None
    except Exception:
        return None


def detect_parts(chapters):
    """Group chapters under 'Part N' / 'Book N' style headings when present.
    Returns [{'label', 'start_index', 'end_index'}] (inclusive) or []."""
    boundaries = [
        (i, ch["title"]) for i, ch in enumerate(chapters) if PART_TITLE_RE.match(ch["title"])
    ]
    if len(boundaries) < 2:
        return []
    parts = []
    for n, (idx, title) in enumerate(boundaries):
        end = (boundaries[n + 1][0] - 1) if n + 1 < len(boundaries) else len(chapters) - 1
        parts.append({"label": title.strip(), "start_index": idx, "end_index": end})
    return parts


# ---------------------------------------------------------------------------
# Source 2: Hardcover (scaffold)
# ---------------------------------------------------------------------------

def chapters_from_hardcover(title: str, author: str):
    """Hardcover's public GraphQL API (https://api.hardcover.app/v1/graphql,
    Authorization: Bearer <token>) exposes books/editions/pages but — as of
    2026-07 — no chapter lists, so this source is a scaffold that always
    returns None. If Hardcover adds chapter data, implement the query here.
    """
    if not HARDCOVER_API_TOKEN:
        return None
    # Intentionally unimplemented — no chapter data in the public schema.
    return None


# ---------------------------------------------------------------------------
# Source 3: Claude LLM
# ---------------------------------------------------------------------------

CHAPTER_SCHEMA = {
    "type": "object",
    "properties": {
        "known": {
            "type": "boolean",
            "description": "True only if you actually know this book's real chapter list.",
        },
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["known", "chapters"],
    "additionalProperties": False,
}


def chapters_from_llm(title: str, author: str):
    """Ask Claude for the chapter list. Returns [{'title', 'start_min': None}] or None."""
    if not CLAUDE_API_KEY:
        return None
    try:
        import anthropic
    except ImportError:
        print("  [LLM] 'anthropic' package not installed (pip install anthropic) — skipping")
        return None

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    try:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=16000,
            output_config={"format": {"type": "json_schema", "schema": CHAPTER_SCHEMA}},
            messages=[{
                "role": "user",
                "content": (
                    f'List the chapters of the book "{title}" by {author}, in order, '
                    "as they appear in the published book (chapter numbers and titles "
                    "if the chapters are titled, otherwise just 'Chapter N'). "
                    "Only set known to true if you genuinely know this specific book's "
                    "structure — do not invent a plausible-looking chapter list. "
                    "If unsure, set known to false and chapters to []."
                ),
            }],
        )
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text)
    except Exception as e:  # network, auth, parse — all non-fatal per book
        print(f"  [LLM] request failed: {e}")
        return None

    if not data.get("known") or not data.get("chapters"):
        return None
    chapters = [
        {"title": (c.get("title") or "").strip(), "start_min": None}
        for c in data["chapters"]
        if (c.get("title") or "").strip()
    ]
    if not (1 < len(chapters) <= MAX_REASONABLE_CHAPTERS):
        return None
    return chapters


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def read_tags(path: Path):
    """Return (title, author) from the file's MP4 tags — same title source
    the catalog uses, so chapters.json keys match catalog.csv titles."""
    try:
        tags = MP4(str(path)).tags or {}
    except Exception:
        return None, None

    def tag(key):
        v = tags.get(key)
        return str(v[0]).strip() if v else ""
    return tag("\xa9nam") or None, tag("\xa9ART") or ""


def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def save_json_with_retry(obj, path: Path, retries: int = 5) -> bool:
    """Write via a temp file then swap it in, retrying — OneDrive's sync can
    briefly lock the target and would otherwise crash the run."""
    tmp = path.with_suffix(".tmp")
    for attempt in range(retries):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=1, ensure_ascii=False)
            if path.exists():
                path.unlink()
            tmp.rename(path)
            return True
        except (PermissionError, OSError):
            time.sleep(0.5 * (attempt + 1))
    print(f"  [WARN] could not write {path.name} after {retries} attempts (file locked?) — retrying on next save")
    return False


def read_tags_cached(path: Path, cache: dict):
    """read_tags with an mtime/size cache so unchanged, already-seen files
    are never opened again (fast resume; no OneDrive re-hydration)."""
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return None, ""
    entry = cache.get(key)
    if entry and entry.get("mtime") == st.st_mtime and entry.get("size") == st.st_size:
        return entry.get("title"), entry.get("author") or ""
    title, author = read_tags(path)
    cache[key] = {"mtime": st.st_mtime, "size": st.st_size, "title": title, "author": author or ""}
    return title, author or ""


def run_extraction(force=False, retry_missing=False, no_llm=False, limit=0):
    """Extract chapters for books not yet in chapters.json. Safe to call from
    the sync pipeline — already-done books are skipped via the tag cache
    without opening their files. Returns the stats dict (None if no library).
    """
    data = load_json(CHAPTERS_PATH, {})
    tag_cache = load_json(TAG_CACHE_PATH, {})

    files = walk_library(ROOT_DIR, EXTS)
    if not files:
        print(f"No audio files found under {ROOT_DIR} — run this on the machine with the library.")
        return None

    stats = {"m4b": 0, "hardcover": 0, "llm": 0, "none": 0, "skipped": 0, "errors": 0}
    processed = 0
    cache_dirty = 0
    for path in files:
        if limit and processed >= limit:
            break
        try:
            cache_size_before = len(tag_cache)
            title, author = read_tags_cached(path, tag_cache)
            cache_dirty += len(tag_cache) - cache_size_before
            if not title:
                continue
            existing = data.get(title)
            if existing and not force:
                if not (retry_missing and existing.get("source") == "none"):
                    stats["skipped"] += 1
                    if cache_dirty >= 50:
                        save_json_with_retry(tag_cache, TAG_CACHE_PATH)
                        cache_dirty = 0
                    continue
            processed += 1
            print(f"[{processed}] {title}")

            chapters = chapters_from_ffprobe(path) or chapters_from_mutagen(path)
            source = "m4b" if chapters else None
            if not chapters:
                chapters = chapters_from_hardcover(title, author)
                source = "hardcover" if chapters else None
            if not chapters and not no_llm:
                chapters = chapters_from_llm(title, author)
                source = "llm" if chapters else None

            if chapters:
                entry = {"source": source, "chapters": chapters, "parts": detect_parts(chapters)}
                print(f"  -> {len(chapters)} chapters via {source}"
                      + (f", {len(entry['parts'])} parts" if entry["parts"] else ""))
                stats[source] += 1
            else:
                entry = {"source": "none", "chapters": [], "parts": []}
                print("  -> no chapter data from any source")
                stats["none"] += 1
            data[title] = entry

            # Incremental save so an interrupted run keeps its progress
            save_json_with_retry(data, CHAPTERS_PATH)
            if cache_dirty:
                save_json_with_retry(tag_cache, TAG_CACHE_PATH)
                cache_dirty = 0
        except Exception as e:  # one bad file must not kill the run
            stats["errors"] += 1
            print(f"  [WARN] failed on {path.name}: {e}")

    if cache_dirty:
        save_json_with_retry(tag_cache, TAG_CACHE_PATH)

    print(f"\nDone. m4b: {stats['m4b']}, llm: {stats['llm']}, no data: {stats['none']}, "
          f"already done: {stats['skipped']}, errors: {stats['errors']}")
    print(f"Wrote {CHAPTERS_PATH}")
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--force", action="store_true", help="re-extract books already in chapters.json")
    parser.add_argument("--retry-missing", action="store_true", help="retry books recorded as source 'none'")
    parser.add_argument("--no-llm", action="store_true", help="skip the Claude fallback")
    parser.add_argument("--limit", type=int, default=0, help="process at most N books")
    args = parser.parse_args()
    stats = run_extraction(
        force=args.force, retry_missing=args.retry_missing,
        no_llm=args.no_llm, limit=args.limit,
    )
    return 0 if stats is not None else 1


if __name__ == "__main__":
    sys.exit(main())
