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
from pathlib import Path

from mutagen.mp4 import MP4

from app.config import EXTS, ROOT_DIR, SITE_DIR
from app.metadata import walk_library

CHAPTERS_PATH = SITE_DIR / "chapters.json"
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


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--force", action="store_true", help="re-extract books already in chapters.json")
    parser.add_argument("--retry-missing", action="store_true", help="retry books recorded as source 'none'")
    parser.add_argument("--no-llm", action="store_true", help="skip the Claude fallback")
    parser.add_argument("--limit", type=int, default=0, help="process at most N books")
    args = parser.parse_args()

    data = {}
    if CHAPTERS_PATH.exists():
        with open(CHAPTERS_PATH, encoding="utf-8") as f:
            data = json.load(f)

    files = walk_library(ROOT_DIR, EXTS)
    if not files:
        print(f"No audio files found under {ROOT_DIR} — run this on the machine with the library.")
        return 1

    stats = {"m4b": 0, "llm": 0, "none": 0, "skipped": 0}
    processed = 0
    for path in files:
        if args.limit and processed >= args.limit:
            break
        title, author = read_tags(path)
        if not title:
            continue
        existing = data.get(title)
        if existing and not args.force:
            if not (args.retry_missing and existing.get("source") == "none"):
                stats["skipped"] += 1
                continue
        processed += 1
        print(f"[{processed}] {title}")

        chapters = chapters_from_ffprobe(path) or chapters_from_mutagen(path)
        source = "m4b" if chapters else None
        if not chapters:
            chapters = chapters_from_hardcover(title, author)
            source = "hardcover" if chapters else None
        if not chapters and not args.no_llm:
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
        with open(CHAPTERS_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)

    print(f"\nDone. m4b: {stats['m4b']}, llm: {stats['llm']}, "
          f"no data: {stats['none']}, already done: {stats['skipped']}")
    print(f"Wrote {CHAPTERS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
