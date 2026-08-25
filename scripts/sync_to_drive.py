"""
Audiobook Pipeline: Sort + Upload to Google Drive.

Workflow:
1. Sort: Move audiobook files from OpenAudible export into author-named folders
2. Catalog Drive: Read all existing folders from Google Drive to avoid duplicates
3. Detect new: Compare local library against upload manifest to find un-uploaded files
4. Match: Use fuzzy matching + Claude LLM to resolve author names to existing Drive folders
5. Upload: Push new files to Google Drive, creating author folders only when truly new

Usage:
    python scripts/sync_to_drive.py                # Full pipeline (sort + upload)
    python scripts/sync_to_drive.py --sort-only    # Just sort, don't upload
    python scripts/sync_to_drive.py --upload-only  # Just upload new files (skip sort)
    python scripts/sync_to_drive.py --dry-run      # Preview without making changes
    python scripts/sync_to_drive.py --rebuild-only # Rebuild+publish only (metadata fix
                                                    # on an already-uploaded book; skips
                                                    # sort/detect/upload entirely)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field

# Fix Windows console encoding for non-ASCII filenames
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
SYNC_DATA_DIR = Path(os.getenv("SYNC_DATA_DIR", str(SCRIPTS_DIR)))
MANIFEST_PATH = SYNC_DATA_DIR / "upload_manifest.json"
DRIVE_FOLDERS_CACHE_PATH = SYNC_DATA_DIR / "drive_folders_cache.json"
AUTHOR_ALIASES_PATH = SCRIPTS_DIR / "author_aliases.json"

# Load .env
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# Live status for the admin panel. Import is guarded so this script still runs
# on a machine where app/ or firebase-admin is unavailable; the fallback shim
# makes every pstatus.* call a no-op instead of an AttributeError.
try:
    from app import pipeline_status as pstatus
except Exception:  # pragma: no cover - defensive
    class _NoStatus:
        def __getattr__(self, _name):
            return lambda *a, **k: ""
    pstatus = _NoStatus()

# Single-flight lock + scheduled-trigger defer/retry (2026-08-16,
# docs/info/ROLES.md §1c/§1d). See app/core/pipeline_lock.py and
# app/core/pipeline_schedule.py for the full design; run_pipeline() and
# run_rebuild_only() below are the two functions that actually take the
# lock, so every entry point (normal run, --sort-only, --upload-only,
# --rebuild-only, and the remote-trigger watcher, which runs this same
# script as a subprocess) is covered no matter how it's invoked.
from app.core import pipeline_lock
from app.core import pipeline_schedule

# OpenAudible export location
OPENAUDIBLE_BOOKS_DIR = Path(os.getenv("ROOT_DIR", r"C:\Users\nbasl\OpenAudible\books"))
# Books downloaded by the Dockerized OpenAudible (scratch runtime dir) get
# ingested into the library by the same sort step.
CONTAINER_BOOKS_DIR = Path(__file__).resolve().parent.parent / "runtime" / "openaudible" / "books"

# Extensions to process
AUDIOBOOK_EXTS: set[str] = {".m4b", ".m4a", ".mp4"}


def _min_file_age_seconds() -> int:
    """Read MIN_FILE_AGE_SECONDS, tolerating blank/invalid values.

    Default 300 matches .env.example and docker-compose.sync.yml so a direct
    (non-Docker) run gets the same partially-converted-file protection.
    """
    try:
        return max(0, int(os.getenv("MIN_FILE_AGE_SECONDS", "300")))
    except ValueError:
        return 300


MIN_FILE_AGE_SECONDS = _min_file_age_seconds()

# Fuzzy match threshold (0-100). Below this, ask Claude.
FUZZY_THRESHOLD = 80

# Claude API key for resolving ambiguous author matches
CLAUDE_API_KEY: str | None = os.getenv("Claude-llm")

# Google Drive parent folder ID for all audiobook author folders.
DRIVE_PARENT_FOLDER_ID: str = "1yZHU_UryCZkuhg9zFzu5uOadx3NI0FJv"


# ---------------------------------------------------------------------------
# Manifest (tracks what's been uploaded)
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON to `path` atomically (F3, 2026-08-24).

    Dump to a temp file in the SAME directory, flush + fsync, then
    ``os.replace()`` — an atomic swap on both NTFS and POSIX. A crash, reboot
    or kill *during* the write therefore leaves the PREVIOUS file fully intact
    rather than a half-written, truncated one. That matters most for
    upload_manifest.json: a truncated manifest makes the next run's
    ``load_manifest()`` raise and halts EVERY future run until a human repairs
    the file. The temp file shares the target's directory so the replace stays
    on one filesystem (a cross-device replace is not atomic and can raise)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup; never mask the original error.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_manifest() -> dict:
    """Load upload manifest. Structure: {relative_path: {uploaded_at, drive_file_id}}.

    A corrupt, truncated or otherwise unreadable manifest degrades to EMPTY
    with a loud WARN rather than crashing every future run (F3, 2026-08-24).
    Drive dedup makes a rebuilt-from-empty manifest self-correcting: already
    uploaded files are re-detected by ``check_file_exists_on_drive`` and their
    entries re-recorded. Losing the manifest must degrade to 're-scan', never
    to 'halt'."""
    if not MANIFEST_PATH.exists():
        return {}
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        print(
            f"  [WARN] Upload manifest at {MANIFEST_PATH} is unreadable ({e}); "
            "treating it as EMPTY and rebuilding from Drive. Already-uploaded "
            "files will be re-detected and skipped, not re-uploaded."
        )
        return {}
    if not isinstance(data, dict):
        print(
            f"  [WARN] Upload manifest at {MANIFEST_PATH} is not a JSON object "
            "(unexpected shape); treating it as EMPTY."
        )
        return {}
    return data


def save_manifest(manifest: dict) -> None:
    """Persist the upload manifest atomically (F3): a crash mid-write leaves
    the previous manifest intact rather than a truncated file that would halt
    every future run at ``load_manifest()``."""
    _atomic_write_json(MANIFEST_PATH, manifest)


# ---------------------------------------------------------------------------
# Author aliases (maps alternate names to canonical name)
# ---------------------------------------------------------------------------


def load_author_aliases() -> dict[str, str]:
    """Load author aliases. Maps alternate name -> canonical name."""
    if AUTHOR_ALIASES_PATH.exists():
        with open(AUTHOR_ALIASES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Remove the description key
            return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def resolve_alias(author_name: str, aliases: dict[str, str]) -> tuple[str, str | None]:
    """
    Resolve an author name through the alias map.
    Returns (canonical_name, folder_id_override).
    folder_id_override is set when the alias maps directly to a Drive folder ID.
    """
    # Case-insensitive lookup
    for alias, canonical in aliases.items():
        if alias.lower() == author_name.lower():
            # Check if it's a direct folder ID mapping
            if canonical.startswith("__FOLDER_ID__:"):
                folder_id = canonical.split(":", 1)[1]
                print(f"  [ALIAS] '{author_name}' -> direct folder ID ({folder_id})")
                return (author_name, folder_id)
            if alias.lower() != canonical.lower():
                print(f"  [ALIAS] '{author_name}' -> '{canonical}'")
            return (canonical, None)
    return (author_name, None)


# ---------------------------------------------------------------------------
# Drive folder catalog (reads ALL existing folders from Drive)
# ---------------------------------------------------------------------------


def fetch_all_drive_folders(service) -> dict[str, str]:
    """
    Fetch all folders in the Drive parent directory.
    Returns {folder_name: folder_id} for every folder.
    """
    print("  Scanning Google Drive for existing folders...")
    folders = {}
    page_token = None

    while True:
        query = (
            f"'{DRIVE_PARENT_FOLDER_ID}' in parents "
            f"and mimeType='application/vnd.google-apps.folder' "
            f"and trashed=false"
        )
        results = service.files().list(
            q=query,
            pageSize=1000,
            fields="nextPageToken, files(id, name)",
            orderBy="name",
            pageToken=page_token,
        ).execute()

        for f in results.get("files", []):
            folders[f["name"]] = f["id"]

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    print(f"  Found {len(folders)} existing folders on Drive.")
    return folders


def save_drive_folders_cache(folders: dict) -> None:
    """Cache Drive folders locally for faster subsequent lookups.

    Written atomically (F3) for the same reason as the manifest: a crash
    mid-write must not leave a truncated cache. This one is less critical —
    ``load_drive_folders_cache`` already expires it after an hour and a fresh
    Drive listing rebuilds it — but the atomic write is free and consistent."""
    _atomic_write_json(DRIVE_FOLDERS_CACHE_PATH, folders)


def load_drive_folders_cache() -> dict | None:
    """Load cached Drive folders if recent (less than 1 hour old)."""
    if not DRIVE_FOLDERS_CACHE_PATH.exists():
        return None
    # Check age
    age = time.time() - DRIVE_FOLDERS_CACHE_PATH.stat().st_mtime
    if age > 3600:  # 1 hour
        return None
    try:
        with open(DRIVE_FOLDERS_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # A corrupt cache must never crash the run — treat it as absent and
        # fall back to a fresh Drive listing (F3, 2026-08-24).
        print(f"  [WARN] Drive folder cache unreadable ({e}); re-listing from Drive.")
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Sort: Move files from OpenAudible into author folders
# ---------------------------------------------------------------------------


def sort_books(dry_run: bool = False) -> list[Path]:
    """
    Sort audiobook files from OpenAudible export into author-named subfolders.
    Returns list of files that were moved (or would be moved in dry-run).

    ⚠️ Scope is the WHOLE LIBRARY, not just new downloads: OPENAUDIBLE_BOOKS_DIR
    and ROOT_DIR are the same path (both come from $ROOT_DIR), and this rglobs
    the source. Every already-filed book is re-evaluated on every run, so the
    shelf map below rewrites the library, not an inbox. Treat an entry added to
    it as a bulk move and dry-run it first.
    """
    from app.author_names import get_author_name, load_shelf_aliases, resolve_shelf_author
    from app.config import ROOT_DIR

    source_dirs = [OPENAUDIBLE_BOOKS_DIR]
    if CONTAINER_BOOKS_DIR.exists() and CONTAINER_BOOKS_DIR != OPENAUDIBLE_BOOKS_DIR:
        source_dirs.append(CONTAINER_BOOKS_DIR)
    if not OPENAUDIBLE_BOOKS_DIR.exists():
        print(f"[ERROR] OpenAudible books directory not found: {OPENAUDIBLE_BOOKS_DIR}")
        return []

    target_root = ROOT_DIR

    files = [
        p
        for src in source_dirs
        for p in src.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIOBOOK_EXTS
        and "welcome to openaudible" not in p.name.lower()  # container sample book
    ]

    if not files:
        print("  No new audiobook files found in OpenAudible export.")
        return []

    # Tag spelling is not shelf spelling. Without this the sorter fights the
    # library: a book tagged "Alex Toxic" that lives in "Nadya Lee/" gets pulled
    # back out on the next run, which is why the superseded whole-library sorter
    # kept being hand-run to undo it. Deliberately NOT author_aliases.json —
    # that map answers a different question and one of its answers ("this pen
    # name is that human") is wrong for shelving. See app/author_names.py.
    shelf_aliases = load_shelf_aliases()

    moved = []
    for f in files:
        author = get_author_name(f)
        if not author:
            print(f"  [SKIP] No author metadata: {f.name}")
            continue

        shelved = resolve_shelf_author(author, shelf_aliases)
        aliased_from = author if shelved != author else None
        author = shelved

        author_folder = target_root / author
        dest = author_folder / f.name

        if dest.exists():
            print(f"  [EXISTS] {author}/{f.name} - already in library")
            continue

        if aliased_from:
            print(f"  [SHELF] '{aliased_from}' -> '{author}' (author_shelf_aliases.json)")
        print(f"  [MOVE] {f.name} -> {author}/{f.name}")
        if not dry_run:
            author_folder.mkdir(parents=True, exist_ok=True)
            import shutil
            try:
                shutil.move(str(f), str(dest))
                moved.append(dest)
            except Exception as e:
                print(f"  [ERROR] Failed to move {f.name}: {e}")
        else:
            moved.append(dest)

    return moved


def _companion_norm(s: str) -> str:
    """Normalize a filename stem for companion<->audiobook matching."""
    import re
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def sort_companion_files(dry_run: bool = False) -> list[Path]:
    """File loose companion files (PDF/EPUB/MOBI...) next to the audiobook
    they belong to, matched by normalized filename stem.

    OpenAudible drops companion docs loose in the books root; sort_books only
    moves audio, so these get orphaned — never listed in the catalog's
    companion_files and never uploaded. This looks at companions sitting loose
    in a source root (not already inside an author folder) and moves each into
    its matching book's author folder. Companions already nested in a folder
    are left alone (standalone ebooks with no audiobook live there on purpose).
    Idempotent. Returns the destination Paths that were moved.
    """
    import shutil

    from app.config import ROOT_DIR
    from app.metadata import COMPANION_EXTS

    target_root = ROOT_DIR
    source_dirs = [OPENAUDIBLE_BOOKS_DIR]
    if CONTAINER_BOOKS_DIR.exists() and CONTAINER_BOOKS_DIR != OPENAUDIBLE_BOOKS_DIR:
        source_dirs.append(CONTAINER_BOOKS_DIR)

    # Index every audiobook by normalized stem so we can match companions to it.
    audio_by_stem: dict[str, Path] = {}
    for p in target_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in AUDIOBOOK_EXTS:
            audio_by_stem.setdefault(_companion_norm(p.stem), p)

    moved: list[Path] = []
    unmatched = 0
    for src in source_dirs:
        # Only loose files directly in the source root — that's where orphans land.
        for f in sorted(src.iterdir()):
            if not (f.is_file() and f.suffix.lower() in COMPANION_EXTS):
                continue
            book = audio_by_stem.get(_companion_norm(f.stem))
            if not book:
                unmatched += 1
                continue
            dest = book.parent / f.name
            if dest.exists():
                continue
            print(f"  [COMPANION] {f.name} -> {book.parent.name}/{f.name}")
            if not dry_run:
                book.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(f), str(dest))
                    moved.append(dest)
                except Exception as e:
                    print(f"  [ERROR] Failed to move {f.name}: {e}")
            else:
                moved.append(dest)

    if unmatched:
        print(f"  [companions] {unmatched} loose file(s) had no matching "
              "audiobook — left in place (standalone ebooks)")
    return moved


# ---------------------------------------------------------------------------
# Detect new books (not yet uploaded)
# ---------------------------------------------------------------------------


def detect_new_books(
    manifest: dict, just_moved: frozenset[Path] = frozenset()
) -> list[Path]:
    """
    Walk the library and find audiobook files not yet in the upload manifest.
    Returns list of Paths to upload.

    just_moved: files this run's sort step moved into the library. They are
    exempt from the age guard — a Windows rename fails while a writer still
    has the file open, so a successful move means the download is complete,
    and holding them MIN_FILE_AGE_SECONDS would push books auto_acquire just
    downloaded to the next scheduled run.
    """
    from app.config import ROOT_DIR

    library_root = ROOT_DIR
    if not library_root.exists():
        print(f"[ERROR] Library root not found: {library_root}")
        return []

    now = time.time()

    def _settled(p: Path) -> bool:
        """True when the file has been unchanged long enough to upload safely.

        Files can vanish mid-scan (OpenAudible replaces them during
        conversion); treat those as not settled instead of crashing the run.
        """
        if p in just_moved:
            return True
        try:
            return now - p.stat().st_mtime >= MIN_FILE_AGE_SECONDS
        except OSError:
            return False

    # Audiobooks AND their companion docs (PDF/EPUB/...) so companions reach
    # Drive too; upload_file_to_drive dedups by name, so already-uploaded
    # companions are skipped rather than duplicated.
    from app.metadata import COMPANION_EXTS
    uploadable = AUDIOBOOK_EXTS | COMPANION_EXTS
    all_files = [
        p
        for p in library_root.rglob("*")
        if p.is_file() and p.suffix.lower() in uploadable and _settled(p)
    ]

    new_files = []
    for f in all_files:
        rel_path = str(f.relative_to(library_root))
        if rel_path not in manifest:
            new_files.append(f)

    return new_files


# ---------------------------------------------------------------------------
# Claude LLM for author resolution
# ---------------------------------------------------------------------------


def ask_claude_for_match(
    author_name: str, drive_folder_names: list[str]
) -> str | None:
    """
    Ask Claude to determine if an author name matches any existing Drive folder.
    Returns the matched folder name, or None if it's a genuinely new author.
    """
    if not CLAUDE_API_KEY:
        return None

    import requests

    # Only send top 20 candidates (pre-filtered by fuzzy) to keep prompt small
    prompt = f"""I have an audiobook library on Google Drive organized by author folders.
I need to file a book by the author "{author_name}".

Here are the existing folder names on Drive (some may have multiple authors, dashes with extra info, or slight name variations):

{json.dumps(drive_folder_names[:50], indent=2)}

Does "{author_name}" match any of these existing folders? Consider:
- Name variations (J.K. Rowling vs JK Rowling vs J. K. Rowling)
- Folders with multiple authors separated by & or "and" or commas
- Folders with extra info after a dash (e.g., "Author Name - Series Name")
- Case differences
- Missing/extra punctuation or initials

Respond with ONLY one of:
1. The exact folder name from the list that matches (copy it exactly)
2. The word "NONE" if this is a genuinely new author not in any folder

Your response (just the folder name or NONE):"""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if response.status_code == 200:
            result = response.json()
            answer = result["content"][0]["text"].strip()
            if answer == "NONE" or answer == '"NONE"':
                return None
            # Verify the answer is actually in our folder list
            if answer in drive_folder_names:
                return answer
            # Try stripping quotes
            cleaned = answer.strip('"').strip("'")
            if cleaned in drive_folder_names:
                return cleaned
            return None
        else:
            print(f"  [WARN] Claude API error {response.status_code}: {response.text[:100]}")
            return None
    except Exception as e:
        print(f"  [WARN] Claude API call failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Author -> Drive folder resolution
# ---------------------------------------------------------------------------


def resolve_author_to_drive_folder(
    author_name: str,
    drive_folders: dict[str, str],
    dry_run: bool = False,
) -> tuple[str, str] | None:
    """
    Resolve a local author name to an existing Drive folder.
    Uses: exact match -> fuzzy match -> Claude LLM -> user prompt -> create new.

    Returns (folder_name, folder_id) or None on failure.
    """
    # 1. Exact match (case-insensitive)
    for folder_name, folder_id in drive_folders.items():
        if folder_name.lower() == author_name.lower():
            return (folder_name, folder_id)

    # 2. Normalized match (strip all non-alphanumeric, compare base before " - ")
    import re

    def _normalize(s: str) -> str:
        return re.sub(r'[^a-z0-9]', '', s.lower())

    author_norm = _normalize(author_name)
    for folder_name, folder_id in drive_folders.items():
        # Compare against the base (before first " - ") and against the full name
        folder_base = folder_name.split(" - ")[0].strip()
        if _normalize(folder_base) == author_norm:
            print(f"  [NORM] '{author_name}' -> '{folder_name}' (normalized match)")
            return (folder_name, folder_id)
        if _normalize(folder_name) == author_norm:
            print(f"  [NORM] '{author_name}' -> '{folder_name}' (normalized match)")
            return (folder_name, folder_id)

    # 3. Check if author name is contained in a folder name (handles "Author - Series" pattern)
    for folder_name, folder_id in drive_folders.items():
        # Check if author is the prefix before a dash or slash
        parts = folder_name.replace("/", " - ").split(" - ")
        for part in parts:
            if part.strip().lower() == author_name.lower():
                return (folder_name, folder_id)
        # Check if folder contains author in multi-author format (& separated)
        for sep in [" & ", " and ", ", "]:
            if sep in folder_name:
                sub_authors = [a.strip() for a in folder_name.split(sep)]
                for sub in sub_authors:
                    if sub.lower() == author_name.lower():
                        return (folder_name, folder_id)

    # 4. Fuzzy match
    from thefuzz import fuzz

    scored = []
    for folder_name in drive_folders:
        score = fuzz.token_sort_ratio(author_name.lower(), folder_name.lower())
        if score >= FUZZY_THRESHOLD:
            scored.append((folder_name, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    # If we have a very high fuzzy match (>=92), use it directly
    if scored and scored[0][1] >= 92:
        match_name = scored[0][0]
        print(f"  [MATCH] '{author_name}' -> '{match_name}' (score: {scored[0][1]})")
        return (match_name, drive_folders[match_name])

    # 5. Ask Claude for ambiguous cases
    if CLAUDE_API_KEY:
        # Send all folder names for Claude to consider
        all_folder_names = list(drive_folders.keys())
        claude_match = ask_claude_for_match(author_name, all_folder_names)
        if claude_match and claude_match in drive_folders:
            print(f"  [CLAUDE] '{author_name}' -> '{claude_match}'")
            return (claude_match, drive_folders[claude_match])

    # 6. If we have a decent fuzzy match, confirm with a HUMAN — but only when
    # one is actually at the console. (F2, 2026-08-24)
    #
    # The scheduled pipeline runs headless (wscript -> .vbs -> .bat, no
    # console), so stdin is not a TTY. Calling input() there does NOT pause for
    # an answer that will never come — it raises EOFError immediately, which is
    # UNCAUGHT in the upload loop and aborts the ENTIRE upload batch (every book
    # queued after this one is skipped that run). In a context where stdin
    # blocks rather than EOFs, it hangs instead, holding the single-flight lock
    # until STALE_LOCK_HOURS reclaims it. Either way one ambiguous author must
    # never take down the run. This path is far more reachable than it looks
    # whenever the Claude key is dead (ask_claude_for_match returns None on any
    # API error), which is exactly the state commit db62d65 flags.
    #
    # So: only prompt when sys.stdin.isatty(). Otherwise skip the guess and fall
    # through to "no match" (return None), which creates a fresh tag-named
    # folder — the safe, already-idempotent path — and log it LOUDLY for later
    # human review (add an author_shelf_aliases.json entry to collapse the two).
    if scored:
        best_name = scored[0][0]
        best_score = scored[0][1]
        if not sys.stdin.isatty():
            print(
                f"  [REVIEW] '{author_name}' ~ '{best_name}' (score: {best_score}) "
                "is ambiguous and no human is attached to confirm. Creating a new "
                "folder rather than guessing or blocking the batch. If they are the "
                "same author, add an author_shelf_aliases.json entry."
            )
            return None
        print(f"\n  [FUZZY] '{author_name}' ~ '{best_name}' (score: {best_score})")
        response = input(f"  Use '{best_name}'? (y/n): ").strip().lower()
        if response in ("y", "yes", ""):
            return (best_name, drive_folders[best_name])

    # 7. No match - create new folder
    return None


def create_drive_folder(
    service, author_name: str, drive_folders: dict, dry_run: bool = False
) -> tuple[str, str] | None:
    """Create a new folder on Drive and update the local cache."""
    if dry_run:
        print(f"  [DRY-RUN] Would create Drive folder: '{author_name}'")
        return (author_name, "dry-run-folder-id")

    print(f"  [CREATE] New Drive folder: '{author_name}'")
    try:
        file_metadata = {
            "name": author_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [DRIVE_PARENT_FOLDER_ID],
        }
        folder = (
            service.files()
            .create(body=file_metadata, fields="id")
            .execute()
        )
        folder_id = folder.get("id")
        drive_folders[author_name] = folder_id
        print(f"  [OK] Created '{author_name}' -> {folder_id}")
        return (author_name, folder_id)
    except Exception as e:
        print(f"  [ERROR] Failed to create folder '{author_name}': {e}")
        return None


AUTHOR_MAP_PATH = PROJECT_ROOT / "author_drive_map.json"


def persist_author_links(resolved_links: dict[str, str]) -> int:
    """Write resolved author -> Drive folder links into author_drive_map.json.

    Every author whose file we uploaded gets its folder link recorded here so a
    brand-new author (new Drive folder created this run) resolves in the next
    catalog rebuild and passes the prod promote audit — without this the map
    only ever grew by hand and new authors broke auto-promote.

    Returns the number of entries added or changed.
    """
    try:
        existing = json.loads(AUTHOR_MAP_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        existing = {}
    except Exception as e:
        print(f"  [WARN] Could not read {AUTHOR_MAP_PATH.name}: {e}")
        return 0

    changed = 0
    for author, folder_id in resolved_links.items():
        if not author or not folder_id or str(folder_id).startswith("dry-run"):
            continue
        url = f"https://drive.google.com/drive/folders/{folder_id}"
        if existing.get(author) != url:
            existing[author] = url
            changed += 1

    if changed:
        # Keep the file sorted so diffs stay readable in git.
        ordered = {k: existing[k] for k in sorted(existing, key=str.lower)}
        AUTHOR_MAP_PATH.write_text(
            json.dumps(ordered, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  [MAP] Recorded {changed} author drive link(s) in {AUTHOR_MAP_PATH.name}")
    return changed


# ---------------------------------------------------------------------------
# Google Drive upload with duplicate check
# ---------------------------------------------------------------------------


def check_file_exists_on_drive(service, file_name: str, folder_id: str) -> str | None:
    """
    Check if a file with the same name already exists in the Drive folder.
    Returns the existing file's ID if found, None otherwise.
    """
    try:
        # Escape single quotes in filename for the query
        safe_name = file_name.replace("'", "\\'")
        query = (
            f"name='{safe_name}' "
            f"and '{folder_id}' in parents "
            f"and trashed=false"
        )
        results = service.files().list(
            q=query,
            fields="files(id, name)",
            pageSize=1,
        ).execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]
        return None
    except Exception as e:
        print(f"  [WARN] Could not check for duplicates: {e}")
        return None


def upload_file_to_drive(
    service, file_path: Path, folder_id: str, dry_run: bool = False,
    max_retries: int = 3, item_index: int = 0, item_total: int = 0,
) -> tuple[str | None, bool]:
    """
    Upload a file to a specific Google Drive folder using resumable upload.
    Checks for duplicates first — skips if file already exists on Drive.
    Retries on transient failures with exponential backoff.

    Returns (drive_file_id, already_existed). already_existed is True when
    the file was found on Drive without uploading anything — the caller
    uses it to keep "already on Drive" and "just uploaded" honestly
    separate instead of lumping both into one count (they used to collapse
    into the same bucket because nothing downstream could tell them apart).
    drive_file_id is None on a real failure (network/API, after retries).
    """
    if dry_run:
        size_mb = file_path.stat().st_size / (1024 * 1024)
        print(f"  [DRY-RUN] Would upload: {file_path.name} ({size_mb:.1f} MB)")
        return "dry-run-file-id", False

    # Check if file already exists on Drive
    existing_id = check_file_exists_on_drive(service, file_path.name, folder_id)
    if existing_id:
        print(f"  [SKIP] Already on Drive: {file_path.name}")
        return existing_id, True

    from googleapiclient.http import MediaFileUpload

    size_mb = file_path.stat().st_size / (1024 * 1024)

    for attempt in range(1, max_retries + 1):
        file_metadata = {
            "name": file_path.name,
            "parents": [folder_id],
        }

        # Use resumable upload for large audiobook files
        media = MediaFileUpload(
            str(file_path),
            resumable=True,
            chunksize=10 * 1024 * 1024,  # 10 MB chunks
        )

        try:
            label = f"  [UPLOAD] {file_path.name} ({size_mb:.1f} MB)"
            if attempt > 1:
                label += f" (attempt {attempt}/{max_retries})"
            print(f"{label} ...", end="", flush=True)

            request = service.files().create(
                body=file_metadata, media_body=media, fields="id"
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    print(f"\r{label} ... {pct}%", end="", flush=True)
                    pstatus.upload_progress(
                        file_path.name, pct, item_index, item_total, size_mb
                    )

            file_id = response.get("id")
            print(f"\r{label} ... done ({file_id})")
            return file_id, False

        except Exception as e:
            print(f"\n  [ERROR] Upload failed for {file_path.name}: {e}")
            if attempt < max_retries:
                backoff = 2 ** (attempt - 1)  # 1s, 2s, 4s
                print(f"  [RETRY] Waiting {backoff}s before retry...")
                time.sleep(backoff)
            else:
                print(f"  [FAILED] All {max_retries} attempts exhausted for {file_path.name}")
                return None, False

    return None, False


# ---------------------------------------------------------------------------
# Upload outcome classification
#
# Ebooks are a first-class upload path now (they feed library_catalog's ebook
# lane), so "uploaded only ebooks, no new m4bs this run" must read as an
# ordinary success — not as a degraded or partial one. Four honest classes:
#   uploaded         — new file, pushed to Drive this run
#   already_on_drive — dedup found it there already; not a failure
#   misplaced        — loose file at the library root, no <Author>/ folder;
#                       a WARNING, reported by name, never a failure
#   failed           — a real failure: network/API error, or no Drive folder
#                       could be resolved/created for the author
# Only `failed` may ever push a run to "partial". See _file_is_misplaced()
# and _upload_new_files() for where each class is assigned.
# ---------------------------------------------------------------------------


@dataclass
class UploadOutcome:
    """Tally + names for one STEP 4 run. See module note above for classes."""

    uploaded: list[str] = field(default_factory=list)
    already_on_drive: list[str] = field(default_factory=list)
    misplaced: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def uploaded_count(self) -> int:
        return len(self.uploaded)

    @property
    def already_count(self) -> int:
        return len(self.already_on_drive)

    @property
    def misplaced_count(self) -> int:
        return len(self.misplaced)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    def warnings(self) -> list[str]:
        """Human-readable lines for pipeline_status's 'warnings' field."""
        return [f"Not in author folder: {name}" for name in self.misplaced]

    def run_state(self) -> str:
        """'success' unless a REAL failure occurred. Misplaced files are
        warnings, not failures — a run that only found misplaced/already/
        uploaded files (the morning-of-2026-08-15 scenario: 9 misplaced
        epubs, 0 uploaded, 0 failed) is a full success."""
        return "success" if self.failed_count == 0 else "partial"


def _file_is_misplaced(rel: Path) -> bool:
    """True when a candidate upload sits directly under the library root
    (no <Author>/ folder) rather than filed under an author. Step 1 sorts
    loose files into author folders on the NEXT run — this function just
    recognizes the state, it does not fix it. See the judgment-guard note
    in _upload_new_files()."""
    return len(rel.parts) <= 1


def upload_run_state(failed_count: int) -> str:
    """Standalone wrapper around UploadOutcome.run_state() for callers that
    only have the failure tally (e.g. tests exercising the classification
    rule in isolation)."""
    return "success" if failed_count == 0 else "partial"


def _upload_new_files(
    new_files: list[Path],
    root_dir: Path,
    aliases: dict[str, str],
    drive_folders: dict,
    service,
    dry_run: bool = False,
) -> tuple[dict[str, dict], UploadOutcome, list[str], dict[str, str]]:
    """
    Resolve each candidate file to a Drive author folder and upload it,
    classifying the result. Returns (manifest_updates, outcome,
    new_folders_created, resolved_author_links).

    ⚠️ Judgment guard: a file with no author folder is reported as
    'misplaced' and skipped — this function NEVER auto-files it into an
    author folder itself. Sorting a loose book is a human call (the
    2026-08-15 morning fix for 9 misplaced epubs was made by the owner via
    the coordinator, not by this script); the pipeline's job is to report
    clearly, not to guess where a book belongs.
    """
    manifest_updates: dict[str, dict] = {}
    outcome = UploadOutcome()
    new_folders_created: list[str] = []
    resolved_links: dict[str, str] = {}
    total = len(new_files)

    for i, file_path in enumerate(new_files, 1):
        rel = file_path.relative_to(root_dir)

        if _file_is_misplaced(rel):
            print(f"\n  [{i}/{total}] [MISPLACED] File not in author folder: {rel}")
            outcome.misplaced.append(str(rel))
            continue

        author_name = rel.parts[0]

        # Resolve through alias map first
        canonical_author, folder_id_override = resolve_alias(author_name, aliases)

        print(f"\n  [{i}/{total}] {rel}")

        # If alias provided a direct folder ID, use it
        if folder_id_override:
            folder_name = canonical_author
            folder_id = folder_id_override
        else:
            # Resolve author to a Drive folder
            result = resolve_author_to_drive_folder(canonical_author, drive_folders, dry_run=dry_run)

            if result:
                folder_name, folder_id = result
            else:
                # Create new folder
                created = create_drive_folder(service, canonical_author, drive_folders, dry_run=dry_run)
                if created:
                    folder_name, folder_id = created
                    new_folders_created.append(canonical_author)
                else:
                    print(f"  [SKIP] Could not resolve Drive folder for '{canonical_author}'")
                    outcome.failed.append(str(rel))
                    continue

        # Record the author -> Drive folder link so the next rebuild embeds it
        # and the prod audit can resolve this author (esp. brand-new folders).
        if folder_id and not str(folder_id).startswith("dry-run"):
            resolved_links[canonical_author] = folder_id

        # Upload the file
        drive_file_id, already_existed = upload_file_to_drive(
            service, file_path, folder_id, dry_run=dry_run,
            item_index=i, item_total=total,
        )

        if drive_file_id:
            manifest_updates[str(rel)] = {
                "uploaded_at": datetime.now().isoformat(),
                "drive_file_id": drive_file_id,
                "drive_folder": folder_name,
                "author": author_name,
            }
            if already_existed:
                outcome.already_on_drive.append(str(rel))
            else:
                outcome.uploaded.append(str(rel))
        else:
            outcome.failed.append(str(rel))

    return manifest_updates, outcome, new_folders_created, resolved_links


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    sort_only: bool = False,
    upload_only: bool = False,
    dry_run: bool = False,
    trigger: str = "manual",
) -> None:
    """Public entry point: takes the single-flight lock (see
    app/core/pipeline_lock.py), then runs _run_pipeline_body().

    ⚠️ --dry-run is the one deliberate exception: it makes no filesystem,
    Drive, or git changes (see the `dry_run` branches in sort_books() and
    upload_file_to_drive(), and the `if not dry_run` guard around STEP 6's
    commit/push), so there is nothing for mutual exclusion to protect —
    requiring it to queue behind or refuse next to a real run would only
    get in the way of using it as a safe-anytime preview.

    trigger == "scheduled" is the ONLY value that defers instead of failing
    immediately when blocked — see app/core/pipeline_schedule.py. It is set
    exclusively by scripts/sync_pipeline_8h.bat (`set PIPELINE_TRIGGER=scheduled`),
    the real 8-hourly Task Scheduler job. Every other trigger (the default
    "manual", "manual-rebuild", or the watcher's "manual") fails LOUDLY the
    instant the lock is held — see pipeline_lock.PipelineLockHeld below.
    """
    if dry_run:
        _run_pipeline_body(sort_only=sort_only, upload_only=upload_only, dry_run=dry_run, trigger=trigger)
        return

    if trigger == "scheduled":
        pipeline_schedule.run_with_defer(
            lambda: _run_pipeline_body(
                sort_only=sort_only, upload_only=upload_only, dry_run=dry_run, trigger=trigger
            )
        )
        return

    try:
        lock = pipeline_lock.acquire(trigger)
    except pipeline_lock.PipelineLockHeld as held:
        print(f"\n[LOCK] BLOCKED: pipeline lock held by {held.holder.describe()}")
        print("[LOCK] Refusing to start — another run is already in flight. "
              "(Only the scheduled 8h trigger retries; this one does not.)")
        pstatus.blocked_run(trigger, held.holder.describe())
        # Re-raise (not sys.exit here) so main()'s dispatcher can tell "blocked,
        # already reported" apart from a genuine crash — see main()'s specific
        # `except pipeline_lock.PipelineLockHeld` clause, which must NOT also
        # call pstatus.fail_run() and clobber the message just written above.
        raise

    try:
        _run_pipeline_body(sort_only=sort_only, upload_only=upload_only, dry_run=dry_run, trigger=trigger)
    finally:
        lock.release()


def _run_pipeline_body(
    sort_only: bool = False,
    upload_only: bool = False,
    dry_run: bool = False,
    trigger: str = "manual",
) -> None:
    """Run the full audiobook pipeline. Callers MUST already hold the
    single-flight lock (or be in --dry-run mode, which needs none) — use
    run_pipeline() above, never this directly, outside of a test."""
    print("=" * 60)
    print("  Audiobook Pipeline - Sort & Upload to Google Drive")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if dry_run:
        print("  MODE: DRY RUN (no changes will be made)")
    print("=" * 60)

    # Live status for the admin panel. Entirely best-effort: no credentials or
    # no network means these calls are no-ops (see app/pipeline_status.py).
    pstatus.start_run(trigger=trigger)
    print(f"  {pstatus.status_note()}")

    # -----------------------------------------------------------------------
    # Step 0: Purchase audit — are recent Audible purchases missing locally?
    # (Book sort breaks OpenAudible's own tracking; this diff is the real
    # signal. Report-only, never blocks the sync.)
    # -----------------------------------------------------------------------
    print("\n[STEP 0] Auditing recent purchases vs catalog...")
    pstatus.step("audit")
    try:
        from app.tools.audit_new_purchases import run_audit
        run_audit()
    except Exception as e:
        print(f"  [WARN] Purchase audit failed: {e}")

    # -----------------------------------------------------------------------
    # Step 0b: Pull Drive-only books to local — EARLY, before sort, so a book
    # someone dropped straight into Drive is on disk before detect/sort/catalog
    # run and ingests from this cycle on. Placed ahead of the STEP 2 idle
    # early-return so it ALSO runs on idle cycles: a Drive-only drop is
    # uncorrelated with whether THIS machine gained a local file. Subprocess,
    # never-raises, kill-switch — see _run_drive_pull() for the full rationale
    # (incident, churn, timeout, DRIVE_PULL_ENABLED).
    # -----------------------------------------------------------------------
    pstatus.step("drive-pull")
    if dry_run:
        print("\n[STEP 0b] Drive → local pull skipped (--dry-run).")
        pstatus.step_detail("drive-pull", "skipped (dry-run)")
    else:
        pulled = _run_drive_pull()
        pstatus.step_detail("drive-pull", f"{pulled} pulled")

    # -----------------------------------------------------------------------
    # Step 1: Sort books from OpenAudible into author folders
    # -----------------------------------------------------------------------
    just_moved: frozenset[Path] = frozenset()
    if not upload_only:
        # Step 1a: Rename ASIN-named epubs to Title - Author.epub
        from scripts.rename_epubs import get_epub_metadata, sanitize_filename
        epub_source = OPENAUDIBLE_BOOKS_DIR
        epubs_renamed = 0
        for epub in sorted(epub_source.glob("*.epub")):
            meta = get_epub_metadata(epub)
            if not meta or not meta["title"]:
                continue
            title = meta["title"]
            author = meta.get("author", "")
            new_name = sanitize_filename(f"{title} - {author}.epub" if author else f"{title}.epub")
            new_path = epub.parent / new_name
            if new_path != epub and not new_path.exists():
                if not dry_run:
                    epub.rename(new_path)
                epubs_renamed += 1
        if epubs_renamed:
            print(f"\n[STEP 1a] Renamed {epubs_renamed} ASIN-named epub(s)")

        print("\n[STEP 1] Sorting books from OpenAudible export...")
        pstatus.step("sort")
        moved = sort_books(dry_run=dry_run)
        print(f"  Sorted {len(moved)} file(s).")
        filed = sort_companion_files(dry_run=dry_run)
        if filed:
            print(f"  Filed {len(filed)} orphaned companion file(s).")
        just_moved = frozenset(moved) | frozenset(filed)
        pstatus.step_detail("sort", f"{len(moved)} sorted, {len(filed)} companions filed")
        pstatus.set_summary(sorted=len(moved))
    else:
        print("\n[STEP 1] Skipped (--upload-only)")
        pstatus.step_detail("sort", "skipped (--upload-only)")

    # -----------------------------------------------------------------------
    # Step 1b: Refresh the ebook manifest (site/ebooks.json)
    #
    # This is the file library_catalog's ebook importer reads — its ingest
    # route has documented this as "sync step 1b" since it was built, but the
    # step was never actually wired in, so the manifest only moved when a
    # person ran scripts/build_ebook_manifest.py by hand and new EPUBs sat
    # stranded (9 of them, found 2026-08-14). Runs AFTER sort + companions so
    # the scan sees files at their final paths, and runs even under
    # --upload-only because the scan is independent of sorting. site/ebooks.json
    # is already in the auto-commit's `git add` list (step 6), so a refreshed
    # manifest ships with the same push as the catalog it describes.
    #
    # Soft on purpose: a manifest failure must never block the audiobook sync
    # — same stance as steps 0, 5.5–5.7.
    # -----------------------------------------------------------------------
    print("\n[STEP 1b] Refreshing ebook manifest (site/ebooks.json)...")
    try:
        from scripts.build_ebook_manifest import build_manifest
        rc = build_manifest(dry=dry_run)
        if rc != 0:
            print("  [WARN] Ebook manifest build reported failure — see above.")
        elif not dry_run:
            # RECORD THAT IT HAPPENED (2026-08-16, owner-approved).
            #
            # ⚠️ This exists because heygabi.ai/status was GUESSING. Its ebook
            # row had to answer "should the published manifest have moved?" and
            # nothing in the status doc said so, so it inferred — first from
            # wall-clock freshness, then from the run's `trigger` string, then
            # from `steps[].publish.state`. Each inference was correct about the
            # case that prompted it and wrong about the next one; the owner
            # reported the same false amber three separate times.
            #
            # The inference was also a CROSS-REPO string contract: the reader
            # lives in catalog-platform, while the trigger names live in
            # sync_pipeline_8h.bat and pipeline_watcher.py here. Renaming one
            # silently degraded the row with no error anywhere.
            #
            # So state the fact instead of leaving it to be deduced. Read back
            # what was actually written rather than stamping "now" — the file's
            # own generated_at is what the status page compares against, and a
            # timestamp invented here could differ from it.
            #
            # ⚠️ Soft, like the build above: set_summary() force-pushes and
            # swallows its own exceptions, and this whole block is inside the
            # try. Recording a fact must never be able to break a sync.
            #
            # ⚠️ This says the manifest was BUILT, not PUBLISHED — and the gap
            # between those two is exactly the 2026-08-16 bug (a run that finds
            # nothing new skips `publish`, so the built manifest never reaches
            # the site). The status page needs BOTH this field and
            # steps[].publish.state === 'done'. Do not let a later change treat
            # this field alone as "the site is up to date".
            try:
                import json as _json
                _m = _json.loads((PROJECT_ROOT / "site" / "ebooks.json").read_text(encoding="utf-8"))
                pstatus.set_summary(
                    ebookManifestAt=_m.get("generated_at"),
                    ebookCount=_m.get("count"),
                )
                print(f"  [status] ebookManifestAt={_m.get('generated_at')} count={_m.get('count')}")
            except Exception as e:  # noqa: BLE001 - recording is best-effort
                print(f"  [WARN] Could not record ebookManifestAt: {e}")
    except Exception as e:
        print(f"  [WARN] Ebook manifest refresh failed: {e}")

    # -----------------------------------------------------------------------
    # Step 2: Detect new (un-uploaded) books
    # -----------------------------------------------------------------------
    print("\n[STEP 2] Detecting new books to upload...")
    pstatus.step("detect")
    manifest = load_manifest()
    new_files = detect_new_books(manifest, just_moved=just_moved)
    print(f"  Found {len(new_files)} new file(s) to upload.")
    pstatus.step_detail("detect", f"{len(new_files)} to upload")
    pstatus.set_summary(toUpload=len(new_files))

    if not new_files:
        print("\n  Nothing to upload. All books are synced!")
        # ⚠️ An idle run STILL pushes the estate index (STEP 7) — this is the
        # self-healing pass, and the index is a REMOTE system that can have
        # failed last cycle for its own reasons. Without this, a push that
        # failed while the library was quiet would not be retried until the
        # next new book arrived, which can be days. It costs one PUT of an
        # unchanged snapshot (replace semantics: a no-op but for `pushed_at`),
        # and it keeps `/api/health`'s `audiobook.pushed_at` an honest
        # heartbeat of this machine rather than of book-buying.
        # `record_step=False`: nothing was committed on this path, so the
        # `publish` step legitimately stays un-run and must not be dressed up
        # with a detail line that reads as though it happened.
        if not dry_run:
            _push_estate_index(record_step=False)
            # STEP 8 — parity runs on the idle path for a stronger reason than
            # the index does: it has NOTHING to do with books. Drift arrives
            # when a person is demoted or signs up, which is uncorrelated with
            # whether the library got a new file this cycle. Wiring it only to
            # the busy path would mean "always match" held only on the cycles
            # that happened to upload something.
            _run_drive_parity()
            # STEP 9 — the estate docs snapshot, on the IDLE path for a
            # stronger reason still than parity's: docs have nothing to do with
            # books, and the days nobody buys an audiobook are precisely the
            # days the docs move most. Wiring this only to the busy path is
            # exactly how the corpus would go stale while every dashboard read
            # green — see _publish_docs_snapshot()'s own header.
            _publish_docs_snapshot()
            # STEP 10 — the off-Cloudflare backup mirror, on the IDLE path for
            # the plainest reason of all four: the backup workflow runs DAILY
            # whether or not this library gained a book, so a mirror that only
            # refreshed on busy cycles would track the estate's backups exactly
            # as often as the owner buys audiobooks. See its own header.
            _mirror_estate_backups()
            # STEP 11 — link the sibling catalogues. 🔴 THE IDLE PATH IS THE
            # MORE IMPORTANT OF THE TWO for this step, and its reason is
            # stronger than STEP 8/9/10's: those merely have nothing to do with
            # books, whereas this one is driven by books in the OTHER
            # CATALOGUE. The link drifts when the LIBRARY gains a print or
            # ebook title — a scan session there adds dozens on a day nobody
            # buys audio — which is completely uncorrelated with whether THIS
            # machine gained an audiobook. Wiring it only to the busy path
            # would refresh the link exactly as often as the owner buys
            # audiobooks: not a fix for the staleness that created this step,
            # but the same bug on a longer fuse. See its own header.
            # `mark_step=False` for the STEP 7 reason one door along: pstatus
            # .step() would mark sort/upload/catalog/publish 'done', and on
            # this path none of them ran. The detail line is still written —
            # the sweep really did run, and that is a real result.
            _run_sibling_link(mark_step=False)
            # F1: the idle path never commits, but it IS the self-healing pass
            # for a push that failed on an earlier busy run. Without this, a
            # commit stranded local-only stays unpushed until the next NEW book
            # arrives — days, if the library is quiet. Retry it here.
            idle_publish_ok = _push_pending_commits()
        else:
            idle_publish_ok = True
        print("=" * 60)
        # The common case: an idle scheduled run. Report it as a real success
        # so the panel shows "checked, nothing new" rather than a stale run —
        # unless a stranded commit could not be pushed, which is a real
        # partial failure the panel must show (F1).
        pstatus.set_summary(idle=True)
        if idle_publish_ok:
            pstatus.finish_run("success")
        else:
            pstatus.finish_run(
                "partial",
                "unpushed local commit(s) could not be pushed to origin — "
                "prod is behind (will retry next run)",
            )
        return

    if sort_only:
        print("\n[STEP 3] Skipped (--sort-only)")
        print("=" * 60)
        pstatus.finish_run("success")
        return

    # -----------------------------------------------------------------------
    # Step 3: Catalog existing Drive folders
    # -----------------------------------------------------------------------
    print("\n[STEP 3] Reading existing Google Drive folders...")
    pstatus.step("folders")

    if not dry_run:
        from drive_auth import build_drive_service
        service = build_drive_service()
        if not service:
            print("  [ERROR] Failed to authenticate with Google Drive.")
            print("  Run this script interactively first to complete OAuth setup.")
            # Surfacing this matters: OAuth needs an interactive browser step,
            # so a scheduled run can silently stall here for days otherwise.
            pstatus.finish_run(
                "failed",
                "Google Drive auth failed - run scripts/sync_to_drive.py "
                "interactively to refresh the OAuth token",
            )
            return

        # Try cache first, refresh if stale
        drive_folders = load_drive_folders_cache()
        if drive_folders is None:
            drive_folders = fetch_all_drive_folders(service)
            save_drive_folders_cache(drive_folders)
        else:
            print(f"  Using cached folder list ({len(drive_folders)} folders)")
        pstatus.step_detail("folders", f"{len(drive_folders)} folders")
    else:
        service = None
        drive_folders = {}
        print("  [DRY-RUN] Skipping Drive catalog")

    # -----------------------------------------------------------------------
    # Step 4: Upload files, resolving authors to Drive folders
    # -----------------------------------------------------------------------
    print(f"\n[STEP 4] Uploading {len(new_files)} file(s) to Google Drive...")
    pstatus.step("upload", f"0/{len(new_files)}")

    from app.config import ROOT_DIR

    aliases = load_author_aliases()
    start_time = time.time()

    manifest_updates, outcome, new_folders_created, resolved_links = _upload_new_files(
        new_files, ROOT_DIR, aliases, drive_folders, service, dry_run=dry_run,
    )
    manifest.update(manifest_updates)

    uploaded_count = outcome.uploaded_count
    skipped_count = outcome.already_count
    failed_count = outcome.failed_count

    # Save manifest
    if not dry_run:
        save_manifest(manifest)
        # Also update the Drive folders cache
        save_drive_folders_cache(drive_folders)
        # Persist author -> Drive folder links so new authors resolve in the
        # next rebuild and don't break the prod promote audit.
        persist_author_links(resolved_links)

    # Summary
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print(
        f"  COMPLETE: {uploaded_count} uploaded, {skipped_count} already on Drive, "
        f"{outcome.misplaced_count} misplaced, {failed_count} failed"
    )
    print(f"  Time: {elapsed:.1f}s")
    pstatus.step_detail(
        "upload",
        f"{uploaded_count} uploaded, {skipped_count} already there, "
        f"{outcome.misplaced_count} misplaced, {failed_count} failed",
    )
    # `warnings` is new: misplaced files are named here but never move the
    # run out of success (see UploadOutcome.run_state()). skipped/uploaded/
    # failed/misplaced field KEYS are unchanged — the admin panel and status
    # page already read them; only the classification feeding them changed.
    pstatus.set_summary(
        uploaded=uploaded_count, skipped=skipped_count,
        misplaced=outcome.misplaced_count, misplacedFiles=outcome.misplaced,
        failed=failed_count, warnings=outcome.warnings(),
        uploadSec=round(elapsed),
    )

    # Report new folders created (so user can spot discrepancies)
    if new_folders_created:
        unique_new = sorted(set(new_folders_created))
        print(f"\n  NEW FOLDERS CREATED ({len(unique_new)}):")
        print("  " + "-" * 40)
        for name in unique_new:
            print(f"    - {name}")
        print()
        print("  Review these for duplicates/typos.")
        print("  To merge authors, add entries to: scripts/author_aliases.json")

    # Report misplaced files (so a human can file them — the pipeline never
    # does this itself; see the judgment-guard note on _upload_new_files()).
    if outcome.misplaced:
        print(f"\n  MISPLACED ({outcome.misplaced_count}) — not in an author folder, left in place:")
        print("  " + "-" * 40)
        for name in outcome.misplaced:
            print(f"    - {name}")
        print()
        print("  Not moved automatically. File each into an <Author>/ folder")
        print("  and the next run will pick it up.")

    print("=" * 60)

    # -----------------------------------------------------------------------
    # Step 5: Rebuild catalog (so deploy can detect new books for Discord)
    #
    # Only worth doing when something NEW landed on Drive this run — a
    # misplaced-only or all-already-there run has nothing new for the
    # audiobook catalog to reflect. This gate is fine to keep narrow because,
    # unlike the old STEP 6, nothing here is needed to publish an ebook-only
    # or misplaced-only run's other changes (see STEP 6 below, which is NOT
    # gated on uploaded_count for exactly that reason).
    # -----------------------------------------------------------------------
    if not dry_run and uploaded_count > 0:
        print("\n[STEP 5] Rebuilding catalog...")
        pstatus.step("catalog")
        try:
            from app.main import main as catalog_main
            catalog_main()
            print("  Catalog rebuilt.")
            try:
                import csv as _csv
                from app.config import SITE_DIR as _SD
                with open(_SD / "catalog.csv", encoding="utf-8") as _f:
                    _total = sum(1 for _ in _csv.DictReader(_f))
                pstatus.step_detail("catalog", f"{_total} books")
                pstatus.set_summary(books=_total)
            except Exception:
                pass
        except Exception as e:
            print(f"  [WARN] Catalog rebuild failed: {e}")
            pstatus.step_detail("catalog", f"FAILED: {e}")

        # Extract chapters for the new books (already-done books are skipped
        # via the tag cache, so this only touches what just arrived)
        print("\n[STEP 5.5] Extracting chapters for new books...")
        chapter_stats = None
        try:
            from app.tools.extract_chapters import run_extraction
            chapter_stats = run_extraction()
        except Exception as e:
            print(f"  [WARN] Chapter extraction failed: {e}")

        # Content warnings for the books that just arrived (Hardcover ->
        # DoesTheDogDie free passes, Claude web-search backfill). Never
        # blocks the sync.
        new_books = (chapter_stats or {}).get("new_books") or []
        pstatus.set_summary(newBooks=[t for t, _a in new_books] if new_books else [])
        if new_books:
            print(f"\n[STEP 5.6] Content warnings for {len(new_books)} new book(s)...")
            try:
                from app.tools.fetch_content_warnings import check_new_books
                check_new_books(new_books)
            except Exception as e:
                print(f"  [WARN] Content-warning fetch failed: {e}")

    elif uploaded_count > 0:
        print("\n[STEP 5] Skipped catalog rebuild (dry-run)")

    # -----------------------------------------------------------------------
    # STEPS 5.7 / 5.75 / 5.8 — publish surfaces. ⚠️ NOT gated on
    # uploaded_count, and that is a FIX (2026-08-18), not a preference: these
    # lived inside the `uploaded_count > 0` block above, so a run with no new
    # Drive uploads silently skipped them — while STEP 1b happily rebuilt
    # site/ebooks.json every run. The dashboard caught the symptom: a manifest
    # BUILT hours ago that was never PUBLISHED (last publish a day old), i.e.
    # readers on a stale shelf with nothing logging a failure. All three are
    # idempotent diffs (sha256 / size+sha256 / digest), so an every-run call
    # costs seconds when there is nothing to do. Same class as the STEP 5.9
    # and STEP 6 gates' history — a publish step gated on an unrelated
    # counter is a silent-staleness machine.
    # -----------------------------------------------------------------------
    if not dry_run:
        # Covers are not in git (docs/info/covers-r2.md); the site links
        # straight at the bucket. Upload first, commit second.
        print("\n[STEP 5.7] Uploading new covers to R2...")
        try:
            from scripts.upload_covers_r2 import main as upload_covers_main
            rc = upload_covers_main([])
            if rc != 0:
                print("  [WARN] Some covers failed to upload — they will retry next run.")
        except Exception as e:
            print(f"  [WARN] Cover upload failed: {e}")

        # STEP 5.75 - the ebook FILES to the private estate-ebooks bucket,
        # BEFORE 5.8 publishes the manifest that names them. Reversing the
        # order gives a reader a Read button that 404s. A failure is a WARN,
        # not a stop. See docs/info/ebooks-r2-ingest.md.
        print("\n[STEP 5.75] Uploading ebook files to R2...")
        try:
            from scripts.upload_ebooks_r2 import main as upload_ebooks_main
            rc = upload_ebooks_main(["--commit"])
            if rc != 0:
                print("  [WARN] Some ebook files failed to upload - they will retry next run.")
        except Exception as e:
            print(f"  [WARN] Ebook file upload failed: {e}")

        # STEP 5.8 - the ebook manifest to its PRIVATE bucket. site/ebooks.json
        # is gitignored and out of the public deployment (owner directive:
        # "I don't want people scraping my books"), so this upload is the ONLY
        # way a refreshed shelf reaches a reader. A failure is a WARN, not a
        # stop: the previously published manifest keeps serving.
        print("\n[STEP 5.8] Publishing ebook manifest to the gated bucket...")
        try:
            from scripts.publish_ebooks_manifest import main as publish_ebooks_main
            rc = publish_ebooks_main([])
            if rc != 0:
                print("  [WARN] Ebook manifest not published - the previous one still serves.")
        except Exception as e:
            print(f"  [WARN] Ebook manifest publish failed: {e}")

    # -----------------------------------------------------------------------
    # STEP 5.9 — fulfil the ON-DEMAND audiobook ingest queue.
    #
    # ⚠️ NOT gated on uploaded_count, and that is the point. A request is made
    # on the SITE by a person who wants to listen to a book this library has
    # held for two years; it has nothing to do with whether anything new
    # arrived on Drive this run. Gating it on new arrivals would leave a
    # requested book sitting in the queue for however long nobody buys
    # anything — the same class of bug the 2026-08-15 STEP 6 gate was.
    #
    # Soft, exactly like steps 5.5-5.8: a Firestore listing failure, an
    # unresolvable title or a dead uplink WARNs and the run continues. The
    # request is kept (never cleared on failure), so the next run retries it.
    #
    # ⚠️ NOTHING HERE IS COMMITTED, deliberately: the only output is
    # site/audio_manifest.json, which is GITIGNORED because it lists the
    # household's books by filename (see .gitignore, and site/ebooks.json's
    # story above it). So this step adds NO entry to _auto_commit_and_push's
    # _ALLOWLIST — that omission is a decision, not an oversight.
    # -----------------------------------------------------------------------
    if not dry_run:
        print("\n[STEP 5.9] Fulfilling audiobook ingest requests...")
        try:
            from app.tools.fulfill_audio_requests import fulfill_requests
            stats = fulfill_requests(commit=True)
            if stats.get("unresolved"):
                print(f"  [WARN] {stats['unresolved']} request(s) name a book this "
                      "library cannot resolve — kept for review.")
        except Exception as e:
            print(f"  [WARN] Audio request fulfilment failed: {e}")

    # -----------------------------------------------------------------------
    # Step 6: Auto-commit & push
    #
    # ⚠️ NOT gated on uploaded_count — that was the morning-of-2026-08-15 bug.
    # STEP 1b refreshes site/ebooks.json unconditionally, earlier, straight
    # from what's on disk; an all-misplaced run (0 uploaded, e.g. 9 loose
    # epubs at the library root) still leaves a freshly-rewritten ebooks.json
    # that needs to ship. _auto_commit_and_push() already no-ops safely via
    # `git status --porcelain` when nothing changed, so calling it
    # unconditionally here costs nothing on a genuinely idle run and fixes
    # the case where uploaded_count == 0 but real local changes exist —
    # exactly the ebook-only / misplaced-only path this run must still
    # publish (manifest, index push, commit) in full.
    # -----------------------------------------------------------------------
    publish_ok = True
    if not dry_run:
        print("\n[STEP 6] Auto-commit & push...")
        pstatus.step("publish")
        # F1: a failed push (commit stays local-only) must move the run state,
        # not just print a WARN. Captured here and folded into finish_run below.
        publish_ok = _auto_commit_and_push()

        # STEP 7 — refresh the shared estate index. Unconditional for the same
        # reason STEP 6 is: step 1b rewrote site/ebooks.json earlier in this
        # cycle whether or not anything uploaded, and the push is a snapshot
        # REPLACE, so skipping it on a quiet run is how the index goes stale.
        _push_estate_index()

        # STEP 8 — Drive ⇄ role parity. Last, and outside every book-shaped
        # gate: it reconciles PEOPLE, so nothing earlier in this cycle is a
        # precondition for it and nothing later depends on it.
        _run_drive_parity()

        # STEP 9 — the estate docs snapshot (GABI docs assistant, phase 5).
        # Genuinely last: nothing in this cycle is a precondition for it and
        # nothing depends on it. Idempotent by content, so on a cycle where no
        # doc changed it walks ~119 files and skips the upload.
        _publish_docs_snapshot()

        # STEP 10 — the off-Cloudflare backup mirror. Last of all, and the
        # longest-running step: nothing in this cycle is a precondition for it
        # and nothing depends on it, so a slow mirror delays nothing.
        # Idempotent by size+checksum — a cycle where the backup workflow has
        # not run since the last mirror skips all 12 objects and costs one
        # workflow-log read.
        _mirror_estate_backups()

        # STEP 11 — link the sibling catalogues (audiobook ⇄ library). Last,
        # and outside every book-shaped gate for the same reason STEP 8 is: it
        # reconciles the OTHER catalogue's rows against this one's CSV, so
        # nothing earlier in this cycle is a precondition beyond STEP 5 having
        # rewritten site/catalog.csv — which is exactly why it sits after the
        # commit rather than before it. ⚠️ The idle-path copy of this call is
        # the one that matters more; see its comment there and the header.
        _run_sibling_link()

    # Fulfill any flagged books (site's "Request AI check" button or
    # cw_requests.txt) — full chain including Claude. Runs on EVERY non-dry
    # sync (not just when new books arrived) so the 8-hourly scheduled task
    # clears requests within a day.
    if not dry_run:
        try:
            from app.tools.fetch_content_warnings import fulfill_requests
            fulfill_requests()
        except Exception as e:
            print(f"  [WARN] Warning-request fulfillment failed: {e}")

    # A run that uploaded but could not publish is still a partial failure the
    # panel should show, so key the outcome on failed_count rather than just
    # "we reached the end". Misplaced files are excluded from failed_count
    # (see UploadOutcome.run_state()), so a misplaced-only run reports success.
    #
    # F1: a failed push is ALSO a partial failure. The commit is local-only,
    # deploy.yml never fired, and prod is behind — the panel must not read
    # green. Fold publish_ok into the state and surface why.
    final_state = outcome.run_state()
    push_error = None
    if not publish_ok:
        push_error = (
            "git push failed — catalog committed locally but not pushed to "
            "origin; deploy did not fire and prod is behind (will retry next run)"
        )
        if final_state == "success":
            final_state = "partial"
    pstatus.finish_run(final_state, push_error)


# ---------------------------------------------------------------------------
# Rebuild-only (for metadata-only fixes on books already uploaded)
#
# Why this exists (2026-08-16): STEP 2 gates the normal pipeline on "are
# there new files to upload?" — a tag fix on an ALREADY-uploaded book (e.g.
# adding a missing narrator to its m4b) changes nothing on disk that STEP 2
# looks at, so a normal run prints "Nothing to upload. All books are
# synced!" and exits before STEP 5 ever rebuilds the catalog. The only
# previous fix was calling `python -m app.main` directly, which rebuilds the
# catalog but does not commit/push it — undocumented tribal knowledge, and a
# rebuilt-but-uncommitted site is easy to leave behind by accident.
#
# This function runs the same STEP 5 (catalog rebuild) through STEP 6
# (commit + push) as a normal pipeline run, plus content-warning request
# fulfillment (which already runs unconditionally on every non-dry run) —
# and nothing before STEP 5. It deliberately does NOT touch sort, detect, or
# upload, and does NOT call _upload_new_files, so the upload manifest is
# untouched and no book can be mistaken for newly-uploaded here.
#
# Per-step reasoning for what belongs in a rebuild-only run:
#   STEP 5   (catalog rebuild)      INCLUDE — the whole point of the flag:
#            refresh catalog.csv/index.html/stats.html from the tag fix.
#   STEP 5.5 (chapter extraction)   INCLUDE — keyed off `title` already
#            present in site/chapters.json (app/tools/extract_chapters.py),
#            not off the upload manifest. An already-processed book is
#            skipped via that cache same as always; harmless to call.
#   STEP 5.6 (content warnings)     INCLUDE — gated on STEP 5.5's own
#            `new_books` list (titles that just got a first-time chapters
#            entry), which will be empty for an existing book. Naturally a
#            no-op here; included so a rebuild-only run stays complete for
#            the rare case a book's chapters genuinely hadn't been
#            extracted yet.
#   STEP 5.7 (covers -> R2)         INCLUDE — idempotent sha256 diff against
#            covers_manifest.json, independent of the manifest/new-book
#            concept; a tag fix can include a corrected cover, so this must
#            run for the fix to actually publish.
#   STEP 5.75 (ebook files -> R2)   INCLUDE — idempotent size+sha256 diff
#            against the bucket manifest; a rebuild can carry a book whose
#            bytes never landed, and this is what retries them.
#   STEP 5.8 (gated ebook manifest)  INCLUDE — the manifest on disk is the
#            one readers ever see (site/ebooks.json is gitignored), so a
#            rebuild that does not publish it ships nothing to the shelf.
#   STEP 5.9 (audio ingest queue)   EXCLUDE — --rebuild-only is "republish
#            this tag fix", and fulfilling the queue is neither a rebuild nor
#            a publish: it pushes hundreds of MB up a household uplink per
#            requested book. Nothing is lost by waiting — the request is
#            keyed on the book and stays pending until the next scheduled
#            run (3x/day) picks it up. ⚠️ If that ever feels too slow, the
#            fix is a dedicated `--fulfill-audio` entry point, NOT quietly
#            making a tag fix upload 600 MB.
#   STEP 6   (commit+push)          INCLUDE — required so the tag fix
#            actually ships instead of sitting rebuilt-but-uncommitted, same
#            failure mode this flag exists to close.
#   STEP 7   (estate index push)    INCLUDE — since 2026-08-17 this machine
#            is the index's ONLY writer (see _push_estate_index), so a fix
#            that never runs it never reaches estate search.
#   STEP 8   (Drive ⇄ role parity)  EXCLUDE — ⚠️ the one place this ledger
#            deliberately parts company with STEP 7. Parity mutates PEOPLE'S
#            ACCESS, and --rebuild-only is a human saying "republish this tag
#            fix"; quietly demoting someone's Drive permission as a side
#            effect of that is a surprise with a blast radius, not a
#            convenience. It costs nothing to omit: the 8h cycle runs parity
#            on BOTH its paths, so the longest a fix waits is one tick.
#   STEP 9   (estate docs snapshot) EXCLUDE — and for the plainest reason in
#            this ledger: --rebuild-only means "republish the CATALOG from
#            what is on disk", and the docs corpus is not the catalog. It
#            shares no input with anything this flag rebuilds and is not made
#            stale by anything this flag fixes. Unlike STEP 8 this omission
#            carries no risk either way (the step mutates one R2 object and
#            nobody's access), so the tie is broken on scope rather than on
#            blast radius. Same "costs nothing to omit" argument as parity's:
#            the 8h cycle publishes docs on BOTH its paths, so the longest a
#            doc edit waits is one tick — and every answer built on the
#            snapshot carries its publish date, so even that is visible.
#   fulfill_requests()              INCLUDE — already runs unconditionally
#            on every non-dry run regardless of uploaded_count; excluding it
#            here would make --rebuild-only behave differently from a normal
#            publish for no reason.
#   STEP 0 (purchase audit), STEP 1/1a (sort), STEP 1b (ebook manifest),
#   STEP 2 (detect), STEP 3 (Drive folders), STEP 4 (upload)  EXCLUDED —
#            these are precisely the sort/detect/upload path the flag is
#            named to skip.
#
# NOT a false "new book": additions_log.update_additions_log() keys off
# title|author already present in site/additions_log.json — re-tagging an
# existing book's narrator does not change that key, so no new entry is
# logged. The Discord "new book" announcement is a separate CI step
# (app/tools/detect_new_books.py in .github/workflows/deploy.yml) that
# diffs site/catalog.csv by the same title|author key against its own
# snapshot — it also never looks at the upload manifest, so nothing here
# can trigger a false "new book" Discord post for a book everyone already
# has.
# ---------------------------------------------------------------------------


def run_rebuild_only(trigger: str = "manual") -> None:
    """Public entry point: takes the single-flight lock, then runs
    _run_rebuild_only_body(). Always fails LOUDLY and immediately when the
    lock is held — never defers, regardless of `trigger`. The owner's defer
    rule (app/core/pipeline_schedule.py) is specifically for the 8h
    scheduled trigger, and scripts/sync_pipeline_8h.bat never passes
    --rebuild-only for that slot, so there is no real path by which this
    function should ever wait rather than refuse."""
    try:
        lock = pipeline_lock.acquire(trigger)
    except pipeline_lock.PipelineLockHeld as held:
        print(f"\n[LOCK] BLOCKED: pipeline lock held by {held.holder.describe()}")
        print("[LOCK] Refusing to start --rebuild-only — another run is already in flight.")
        pstatus.blocked_run(trigger, held.holder.describe())
        # Re-raise (see the matching comment in run_pipeline()) so main() can
        # set a nonzero exit code without pstatus.fail_run() clobbering the
        # blocked-status message just written above.
        raise

    try:
        _run_rebuild_only_body(trigger=trigger)
    finally:
        lock.release()


def _run_rebuild_only_body(trigger: str = "manual") -> None:
    """Rebuild the catalog/site from what's on disk and publish it, WITHOUT
    sort/detect/upload. See the module note above this function for the
    per-step reasoning. Callers MUST already hold the single-flight lock —
    use run_rebuild_only() above, never this directly, outside of a test."""
    print("=" * 60)
    print("  Audiobook Pipeline - Rebuild Only (no sort/detect/upload)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    pstatus.start_run(trigger=trigger)
    print(f"  {pstatus.status_note()}")

    print("\n[REBUILD-ONLY] Rebuilding catalog (STEP 5)...")
    pstatus.step("catalog")
    try:
        from app.main import main as catalog_main
        catalog_main()
        print("  Catalog rebuilt.")
        try:
            import csv as _csv
            from app.config import SITE_DIR as _SD
            with open(_SD / "catalog.csv", encoding="utf-8") as _f:
                _total = sum(1 for _ in _csv.DictReader(_f))
            pstatus.step_detail("catalog", f"{_total} books")
            pstatus.set_summary(books=_total)
        except Exception:
            pass
    except Exception as e:
        print(f"  [WARN] Catalog rebuild failed: {e}")
        pstatus.step_detail("catalog", f"FAILED: {e}")
        pstatus.finish_run("failed", f"Catalog rebuild failed: {e}")
        return

    # STEP 5.5: chapters for any book whose chapters.json entry is still
    # missing (independent of the upload manifest — see module note above).
    print("\n[REBUILD-ONLY] Extracting chapters (STEP 5.5)...")
    chapter_stats = None
    try:
        from app.tools.extract_chapters import run_extraction
        chapter_stats = run_extraction()
    except Exception as e:
        print(f"  [WARN] Chapter extraction failed: {e}")

    new_books = (chapter_stats or {}).get("new_books") or []
    pstatus.set_summary(newBooks=[t for t, _a in new_books] if new_books else [])
    if new_books:
        print(f"\n[REBUILD-ONLY] Content warnings for {len(new_books)} book(s) (STEP 5.6)...")
        try:
            from app.tools.fetch_content_warnings import check_new_books
            check_new_books(new_books)
        except Exception as e:
            print(f"  [WARN] Content-warning fetch failed: {e}")

    # STEP 5.7: covers -> R2, before the commit (same ordering as the normal
    # pipeline — an uncommitted-but-uploaded cover is fine, the reverse is a
    # broken image).
    print("\n[REBUILD-ONLY] Uploading covers to R2 (STEP 5.7)...")
    try:
        from scripts.upload_covers_r2 import main as upload_covers_main
        rc = upload_covers_main([])
        if rc != 0:
            print("  [WARN] Some covers failed to upload — they will retry next run.")
    except Exception as e:
        print(f"  [WARN] Cover upload failed: {e}")

    # STEP 5.75 - the ebook FILES to the private estate-ebooks bucket, BEFORE
    # 5.8 publishes the manifest that names them. Reversing the order gives a
    # reader a Read button that 404s. A failure is a WARN, not a stop: the
    # missing file is named in the log and retried next run, and every other
    # book still works. See docs/info/ebooks-r2-ingest.md.
    print("\n[REBUILD-ONLY] Uploading ebook files to R2 (STEP 5.75)...")
    try:
        from scripts.upload_ebooks_r2 import main as upload_ebooks_main
        rc = upload_ebooks_main(["--commit"])
        if rc != 0:
            print("  [WARN] Some ebook files failed to upload - they will retry next run.")
    except Exception as e:
        print(f"  [WARN] Ebook file upload failed: {e}")

    # STEP 5.8 - the ebook manifest to its PRIVATE bucket. Since 2026-08-17
    # site/ebooks.json is gitignored and out of the public deployment (owner
    # directive: "I don't want people scraping my books"), so this upload is
    # the ONLY way a refreshed shelf reaches a reader. A failure is a WARN,
    # not a stop: the previously published manifest keeps serving, so readers
    # get a stale shelf rather than none, and the commit still lands.
    print("\n[REBUILD-ONLY] Publishing ebook manifest to the gated bucket (STEP 5.8)...")
    try:
        from scripts.publish_ebooks_manifest import main as publish_ebooks_main
        rc = publish_ebooks_main([])
        if rc != 0:
            print("  [WARN] Ebook manifest not published - the previous one still serves.")
    except Exception as e:
        print(f"  [WARN] Ebook manifest publish failed: {e}")

    print("\n[REBUILD-ONLY] Auto-commit & push (STEP 6)...")
    pstatus.step("publish")
    publish_ok = _auto_commit_and_push()  # F1: capture push outcome

    _push_estate_index("[REBUILD-ONLY] Pushing to the shared estate index (STEP 7)")

    # Same as run_pipeline: clears any flagged content-warning requests,
    # unconditional on every non-dry run.
    try:
        from app.tools.fetch_content_warnings import fulfill_requests
        fulfill_requests()
    except Exception as e:
        print(f"  [WARN] Warning-request fulfillment failed: {e}")

    print("=" * 60)
    # F1: a failed push on a rebuild-only run is a partial failure too.
    if publish_ok:
        pstatus.finish_run("success")
    else:
        pstatus.finish_run(
            "partial",
            "git push failed — catalog committed locally but not pushed to "
            "origin; deploy did not fire and prod is behind (will retry next run)",
        )


# ---------------------------------------------------------------------------
# Fine-grained manual step controls (owner ask 2026-08-16, catalog-platform
# /status Operations section: "give us fine control over each part of the
# pipeline in case we need to do part way steps... make sure we cant break
# stuff"). Each of the 8 stages pipeline_status.STEPS already names can be
# run ALONE, on demand — but every one of them goes through run_step()
# below, which takes the EXACT SAME single-flight lock
# (app/core/pipeline_lock.py) as the scheduled 8h run, the full manual
# pipeline, and --rebuild-only, so a manual step and any other run can NEVER
# overlap. THAT LOCK is the actual safety mechanism.
#
# STEP_INFO's "kind" classification below only drives the UI's confirmation
# tier on the catalog-platform side (ops.ts / admin.js / status.js) — it
# MUST mirror that repo's copy (there is no shared module between the two
# repos, same as KNOWN_BACKUP_PREFIXES's documented duplication story on
# that side). kind is one of:
#   read-only  — audit, detect: no local/Drive/git mutation.
#   mutating   — sort (moves local files), folders (writes the local Drive
#                folder cache + reads Drive), upload (real Drive writes).
#   publishing — catalog (rebuilds site/ on disk), publish (git commit+push,
#                which is what actually ships to the live site), link (writes
#                ANOTHER APP's production D1 — see STEP 11's header; a button
#                that reaches into a different application's live database
#                earns the top confirmation tier, not `mutating`).
#
# Like run_pipeline()'s non-scheduled path, a manual step NEVER defers —
# blocked means refused immediately, loudly, and reported to pipeline_status
# (blocked_run()) so the status page shows who holds the lock and since when.
# ---------------------------------------------------------------------------

STEP_INFO: dict[str, dict[str, str]] = {
    "audit": {"label": "Purchase audit", "kind": "read-only"},
    "sort": {"label": "Sort books", "kind": "mutating"},
    "detect": {"label": "Detect new books", "kind": "read-only"},
    "folders": {"label": "Read Drive folders", "kind": "mutating"},
    "upload": {"label": "Upload to Drive", "kind": "mutating"},
    "catalog": {"label": "Rebuild catalog", "kind": "publishing"},
    "publish": {"label": "Commit & deploy", "kind": "publishing"},
    "link": {"label": "Link sibling catalogues", "kind": "publishing"},
}
STEP_CHOICES: tuple[str, ...] = tuple(STEP_INFO.keys())


def _step_audit() -> None:
    """Read-only: audits recent Audible purchases against the catalog.
    Writes no local files, moves nothing, uploads nothing."""
    from app.tools.audit_new_purchases import run_audit
    run_audit()
    pstatus.step_detail("audit", "checked")


def _step_sort() -> None:
    """Mutating: moves files on local disk (OpenAudible export -> author
    folders) and files loose companion docs. Idempotent — an already-filed
    book is skipped, not re-moved."""
    moved = sort_books(dry_run=False)
    filed = sort_companion_files(dry_run=False)
    pstatus.step_detail("sort", f"{len(moved)} sorted, {len(filed)} companions filed")
    pstatus.set_summary(sorted=len(moved), companionsFiled=len(filed))


def _step_detect() -> None:
    """Read-only: scans the library against the upload manifest and reports
    how many files are new. Writes nothing."""
    manifest = load_manifest()
    new_files = detect_new_books(manifest)
    pstatus.step_detail("detect", f"{len(new_files)} to upload")
    pstatus.set_summary(toUpload=len(new_files))


def _step_folders() -> None:
    """Mutating: reads every author folder from Google Drive and refreshes
    the local cache file (drive_folders_cache.json) — no book file is
    touched, but it is real network I/O against a live Drive folder and a
    real local write, hence 'mutating' rather than 'read-only'."""
    from scripts import drive_auth
    service = drive_auth.build_drive_service()
    if not service:
        raise RuntimeError(
            "Google Drive auth failed — run scripts/sync_to_drive.py interactively "
            "first to complete OAuth setup."
        )
    folders = fetch_all_drive_folders(service)
    save_drive_folders_cache(folders)
    pstatus.step_detail("folders", f"{len(folders)} folders")
    pstatus.set_summary(folders=len(folders))


def _step_upload() -> None:
    """Mutating: uploads whatever detect_new_books() currently finds. Always
    runs its own fresh detect internally (never trusts a stale count) so
    this is safe and correct to click on its own — the 'needs detect first'
    UI hint is an advisory, not a hard requirement, precisely because this
    step is self-sufficient. A no-op (0 new files) is a success, not a
    failure — mirrors run_pipeline()'s own STEP 2 gate."""
    from app.config import ROOT_DIR
    from scripts import drive_auth

    manifest = load_manifest()
    new_files = detect_new_books(manifest)
    pstatus.step_detail("upload", f"0/{len(new_files)}")
    if not new_files:
        pstatus.set_summary(idle=True, uploaded=0, toUpload=0)
        pstatus.step_detail("upload", "nothing to upload (0 new files)")
        return

    service = drive_auth.build_drive_service()
    if not service:
        raise RuntimeError(
            "Google Drive auth failed — run scripts/sync_to_drive.py interactively "
            "first to complete OAuth setup."
        )

    drive_folders = load_drive_folders_cache()
    if drive_folders is None:
        drive_folders = fetch_all_drive_folders(service)
        save_drive_folders_cache(drive_folders)

    aliases = load_author_aliases()
    manifest_updates, outcome, _new_folders, resolved_links = _upload_new_files(
        new_files, ROOT_DIR, aliases, drive_folders, service, dry_run=False,
    )
    manifest.update(manifest_updates)
    save_manifest(manifest)
    save_drive_folders_cache(drive_folders)
    persist_author_links(resolved_links)

    pstatus.step_detail(
        "upload",
        f"{outcome.uploaded_count} uploaded, {outcome.already_count} already there, "
        f"{outcome.misplaced_count} misplaced, {outcome.failed_count} failed",
    )
    pstatus.set_summary(
        uploaded=outcome.uploaded_count, skipped=outcome.already_count,
        misplaced=outcome.misplaced_count, misplacedFiles=outcome.misplaced,
        failed=outcome.failed_count, warnings=outcome.warnings(),
    )
    if outcome.failed_count:
        raise RuntimeError(f"{outcome.failed_count} file(s) failed to upload — see the log above")


def _step_catalog() -> None:
    """Publishing (local): rebuilds site/ from what's on disk (catalog,
    chapters, content warnings, covers -> R2) but does NOT commit/push —
    that is the separate 'publish' step. Same STEP 5/5.5/5.7 bodies as
    --rebuild-only, split out so a rebuild can be inspected before shipping."""
    from app.main import main as catalog_main
    catalog_main()
    try:
        import csv as _csv
        from app.config import SITE_DIR as _SD
        with open(_SD / "catalog.csv", encoding="utf-8") as _f:
            total = sum(1 for _ in _csv.DictReader(_f))
        pstatus.step_detail("catalog", f"{total} books")
        pstatus.set_summary(books=total)
    except Exception:
        pass

    try:
        from app.tools.extract_chapters import run_extraction
        run_extraction()
    except Exception as e:
        print(f"  [WARN] Chapter extraction failed: {e}")

    try:
        from scripts.upload_covers_r2 import main as upload_covers_main
        rc = upload_covers_main([])
        if rc != 0:
            print("  [WARN] Some covers failed to upload — they will retry next run.")
    except Exception as e:
        print(f"  [WARN] Cover upload failed: {e}")


def _step_publish() -> None:
    """Publishing (live): commits + pushes whatever is currently staged on
    disk (site/catalog.csv, index.html, etc. — the same explicit allowlist
    _auto_commit_and_push() always used), refreshes the shared estate index
    (STEP 7), and clears any flagged content-warning requests. A no-op when
    nothing changed, same as every other caller of _auto_commit_and_push().

    The index push belongs HERE and not in `catalog`: since 2026-08-17 this
    step is the one place a manual run reaches the outside world, and the
    `catalog` step's contract is "rebuild on disk, ship nothing".

    Returns a run-state string so a failed push marks the single-step card
    'partial' rather than green (F1)."""
    publish_ok = _auto_commit_and_push()
    _push_estate_index()
    try:
        from app.tools.fetch_content_warnings import fulfill_requests
        fulfill_requests()
    except Exception as e:
        print(f"  [WARN] Warning-request fulfillment failed: {e}")
    return "success" if publish_ok else "partial"


def _step_link() -> None:
    """Publishing (live, and in ANOTHER APP): runs library_catalog's
    backfill-audiobook-holdings.mjs against its REMOTE D1, so the library site
    knows which of its books the household already owns on audio.

    Nothing in THIS repo changes — it reads site/catalog.csv and writes the
    sibling's database — so there is nothing to commit and this step
    deliberately does not call _auto_commit_and_push().

    Same body as the 8-hourly STEP 11, and like it, it never raises: a failure
    is one named WARN and the previously linked holdings still stand (the sweep
    marks vanished matches `stale_at` rather than deleting them). A single-step
    run that could not reach the sibling therefore finishes 'success' with a
    `skipped` detail, which is the honest report — the step ran, the machine
    could not reach the other repo.

    ⚠️ `mark_step=False`, like every other _step_*() handler: start_step_run()
    scaffolds a ONE-ENTRY steps list already 'active' and says in its own
    docstring that no separate step() call is needed. Calling it here would be
    worse than redundant — step() marks everything before the key's index in
    the FULL STEPS list done, and against a one-entry scaffold that means
    marking this very step 'done' the moment it starts."""
    _run_sibling_link("[STEP] Linking sibling catalogues (audiobook ⇄ library)", mark_step=False)


_STEP_HANDLERS = {
    "audit": _step_audit,
    "sort": _step_sort,
    "detect": _step_detect,
    "folders": _step_folders,
    "upload": _step_upload,
    "catalog": _step_catalog,
    "publish": _step_publish,
    "link": _step_link,
}


def run_step(step: str, trigger: str = "manual-step") -> None:
    """Public entry point for ONE isolated pipeline stage. Takes the
    single-flight lock (see the module note above) and always fails
    LOUDLY+IMMEDIATELY when it is held — a manual step never defers, same
    stance as run_pipeline()'s non-scheduled path and run_rebuild_only()."""
    if step not in STEP_INFO:
        raise ValueError(f"unknown pipeline step {step!r} — choices: {', '.join(STEP_CHOICES)}")

    try:
        lock = pipeline_lock.acquire(trigger)
    except pipeline_lock.PipelineLockHeld as held:
        print(f"\n[LOCK] BLOCKED: pipeline lock held by {held.holder.describe()}")
        print(f"[LOCK] Refusing to start step '{step}' — another run is already in flight.")
        pstatus.blocked_run(trigger, held.holder.describe())
        raise

    try:
        _run_step_body(step, trigger)
    finally:
        lock.release()


def _run_step_body(step: str, trigger: str) -> None:
    """Callers MUST already hold the single-flight lock — use run_step()
    above, never this directly, outside of a test."""
    info = STEP_INFO[step]
    print("=" * 60)
    print(f"  Audiobook Pipeline — single step: {step} ({info['label']})")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    pstatus.start_step_run(step, info["label"], trigger)
    print(f"  {pstatus.status_note()}")
    # A handler may return a run-state string (e.g. 'partial' from a failed
    # push in the publish step, F1); handlers that return None mean 'success'.
    handler_state = _STEP_HANDLERS[step]()
    pstatus.finish_run(handler_state if isinstance(handler_state, str) else "success")
    print("=" * 60)


# ---------------------------------------------------------------------------
# STEP 7 — push the catalog+ebook snapshot to the shared estate index
#
# ⚠️ THIS MACHINE IS THE ONE WRITER, by owner decision 2026-08-17 ("option A").
# The push used to be a step in .github/workflows/deploy.yml; that step was
# DELETED and its INDEX_PUSH_TOKEN repo secret retired, because CI can never
# do this job correctly: the snapshot carries the EBOOK rows, site/ebooks.json
# is gitignored (public repo, owner: "I don't want people scraping my books"),
# so a CI checkout has no manifest — and since the push REPLACES the whole
# `audiobook` source, a CI push silently deleted all 168 ebook rows from
# estate search. Never re-add a second pusher: two writers of a
# replace-semantics snapshot means whichever ran last wins, and the one that
# knows least would usually be CI.
#
# Placement: LAST, after STEP 6, and never gated on uploaded_count —
#   * after 1b so the ebook rows are this cycle's,
#   * after 5.7 so every cover_url it publishes is already in R2,
#   * after 5.8/6 so the index can only ever point at something published,
#   * unconditional because a quiet run still rewrote the manifest at 1b, and
#     a snapshot nobody refreshes is a snapshot that goes stale silently.
# It also runs on the IDLE path (STEP 2's early return, where nothing was new
# to upload), because the index is a remote system with its own failure modes:
# without that, a push that failed while the library was quiet would go
# un-retried until the next new book arrived, which can be days.
#
# Failure domain: its OWN. A failed push warns and the cycle continues — the
# previous snapshot keeps serving (replace semantics means a missed run costs
# freshness only) and the next cycle retries 8h later. Exactly one named line
# is printed either way, and the outcome lands on the `publish` step's detail
# so the /status card shows it without a new pipeline_status step key.
# ---------------------------------------------------------------------------


def _push_estate_index(
    label: str = "[STEP 7] Pushing the catalog snapshot to the shared estate index",
    record_step: bool = True,
) -> None:
    """Refresh index.heygabi.ai from site/catalog.csv + site/ebooks.json.

    Never raises: the estate index must never be able to cost a pipeline run.
    Exactly one named line is printed on every path — pushed, skipped, failed.

    `record_step=False` for the idle path, where the `publish` step never ran:
    the run summary still carries the counts, but the step keeps its own state.
    """
    def _detail(text: str) -> None:
        if record_step:
            pstatus.step_detail("publish", text)

    print(f"\n{label}...")
    try:
        from app.index_push import push_from_disk
        summary = push_from_disk()
    except Exception as e:  # noqa: BLE001 — independent failure domain, by design
        print(f"  [WARN] Index push FAILED: {e}")
        print("  [WARN] The previous snapshot still serves; the next cycle retries.")
        _detail(f"index push FAILED: {e}")
        return

    if summary.get("skipped"):
        # Not an error, but not silent either: a machine that never pushes is
        # indistinguishable from a working one unless it says so every run.
        print(f"  [INFO] Index push skipped: {summary['skipped']} (see docs/access/PIPELINE.md)")
        _detail(f"index push skipped ({summary['skipped']})")
        return

    print(
        f"  Estate index updated: {summary['rows']} rows "
        f"({summary['audiobooks']} audiobook + {summary['ebooks']} ebook)."
    )
    _detail(f"index {summary['rows']} rows ({summary['audiobooks']} audiobook + {summary['ebooks']} ebook)")
    pstatus.set_summary(indexRows=summary["rows"], indexEbookRows=summary["ebooks"])


# ---------------------------------------------------------------------------
# STEP 8 — Drive ⇄ role parity: report both directions, AUTO-APPLY role→Drive
#
# Owner order 2026-08-17: "Wire it… with auto apply", fulfilling the standing
# rule recorded in docs/info/ROLES.md §2: "I want this always to match."
# Before today, scripts/drive_role_parity.py was a thing a human ran and read.
# Now the 8-hourly cycle runs it, and Drive gets edited with nobody watching.
#
# ⚠️ SUBPROCESS, NOT AN IMPORT, and the three reasons are all load-bearing:
#   1. The parity script's failure mode is sys.exit() with a FATAL message —
#      that is CORRECT for a script whose missing exception list must never
#      degrade to "no exceptions". Imported, that same correctness would take
#      the pipeline down with it. A subprocess turns another domain's fatal
#      into this domain's WARN without weakening either.
#   2. PYTHONIOENCODING=utf-8 must be forced on the CHILD. The report prints
#      em-dashes and ⚠️; captured through a cp1252 pipe it dies mid-report
#      with UnicodeEncodeError. The script's own stdout.reconfigure() fixes a
#      terminal, not a captured pipe (see docs/info/gotchas.md).
#   3. A hard timeout is the only defence against drive_auth.get_credentials()
#      deciding the token needs re-authorising: it calls run_local_server(),
#      which opens a BROWSER and blocks forever on an unattended machine. A
#      killed child is a named skip; a blocked import is a dead 8-hourly run.
#
# Placement: LAST, after STEP 7, and on BOTH cycle paths — the normal one and
# the idle early-return at STEP 2 (the STEP 7 lesson: a step wired only beside
# the commit skips most cycles, and most cycles are idle). Parity has nothing
# to do with books, so an idle cycle is exactly as good a tick for it.
# Deliberately NOT wired into --rebuild-only or the manual `publish` step,
# unlike STEP 7: those are book-shaped operations a human triggers to
# republish a tag fix, and silently editing a person's Drive access as a side
# effect of "rebuild the catalog" is a surprise, not a feature. The 8h cycle
# is the tick.
#
# Failure domain: its OWN, like STEP 7. A parity failure is one named WARN and
# the cycle continues — the previous permission state simply stands, which is
# the safe direction, and the next cycle retries.
#
# Reporting: pipeline_status SUMMARY fields, not a `publish` step detail.
#   * STEP 7 already owns that detail line, and step_detail() overwrites;
#   * the idle path never runs `publish` at all, so a detail written there
#     would be invisible on exactly the cycles that are most common;
#   * summary survives both paths untouched.
#   ⚠️ COUNTS ONLY — never names. pipeline_status/current is world-readable
#   and the /status page renders it; the emails live in this local log.
# ---------------------------------------------------------------------------

DRIVE_PARITY_TIMEOUT_S = 300
DRIVE_PARITY_SCRIPT = SCRIPTS_DIR / "drive_role_parity.py"
DRIVE_PARITY_TOKEN = SCRIPTS_DIR / "token.json"


def _parity_report(state: str, detail: str) -> None:
    """One place that writes the parity outcome, so every path reports."""
    pstatus.set_summary(
        driveParityState=state,
        driveParityDetail=detail,
        driveParityAt=datetime.now().isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# STEP 9 — publish the estate docs snapshot (GABI docs assistant, phase 5).
#
# Owner decision 2026-08-18, design §9 question 2, verbatim answer "a": the
# publisher RIDES THIS PIPELINE rather than getting a fourth Windows scheduled
# task. Design of record: catalog-platform docs/info/gabi-docs-assistant-
# design.md §2.2.
#
# ⚠️ WHY IT RUNS ON BOTH PATHS, and why that is not optional. Hanging an
# estate-wide job off a BOOK pipeline means the docs snapshot silently stops
# refreshing whenever this pipeline is quiet — and a quiet cycle is the COMMON
# case: STEP 2 returns early at "Nothing to upload" long before STEP 5. Docs
# change on days nobody buys an audiobook; in fact they change most on exactly
# those days. So this runs on the idle path too, for the same reason STEP 7 and
# STEP 8 do, and it is called from both places rather than being moved earlier
# (it is genuinely last: nothing in the cycle depends on it, and it depends on
# nothing in the cycle).
#
# ⚠️ SOFT, LIKE STEPS 5.5-5.8: a failure is one WARN, the previously published
# snapshot keeps serving, and the next cycle retries. That is the right
# direction — every answer built on the snapshot carries its publish date, so a
# stale corpus is VISIBLE in the reply rather than silently believed.
#
# ⚠️ NOTHING HERE IS COMMITTED, deliberately. The outputs go to R2
# (estate-docs-gated), and the publisher's own bookkeeping
# (scripts/.docs_published.json) is gitignored — it lists every included doc
# path, and many of those are this repo's LOCAL-ONLY docs, which must never
# become tracked in a PUBLIC repo. So this step adds NO entry to
# _auto_commit_and_push's _ALLOWLIST; that omission is a decision, not an
# oversight, exactly as STEP 5.9's own note says of itself.
#
# ⚠️ THE SCANNER STAYS IN SHADOW HERE. It is the publisher's default and this
# call passes no override: shadow logs would-refuse findings and publishes
# anyway. Flipping to enforce is a deliberate act after measured zero false
# refusals — never a side effect of an unattended run, where a false positive
# would stop the corpus refreshing with nobody watching. (Measured on the first
# real run, 2026-08-18: 5 findings, ALL false — a plain MDN link and three
# `secret:list:friend` npm script names — both classes since tuned out, leaving
# zero. That is one clean data point, not a week of them.)
# ---------------------------------------------------------------------------
def _publish_docs_snapshot(label: str = "[STEP 9] Publishing the estate docs snapshot") -> None:
    """Publish the three-repo docs corpus to the private estate-docs-gated bucket.

    Never raises. Idempotent by content — an unchanged corpus skips the upload,
    so the common quiet cycle costs a walk of ~119 files and nothing else.
    """
    print(f"\n{label}...")
    try:
        from scripts.publish_docs_snapshot import main as publish_docs_main
        rc = publish_docs_main([])
        if rc != 0:
            print("  [WARN] Docs snapshot not published — the previous one still serves. "
                  "Every answer carries its publish date, so the staleness is visible.")
    except Exception as e:  # noqa: BLE001 — own failure domain, by design
        print(f"  [WARN] Docs snapshot publish failed: {e}")


# ---------------------------------------------------------------------------
# STEP 10 — the off-Cloudflare backup mirror.
#
# Closes the restore drill's owner step #7 (catalog-platform
# docs/access/RECOVERY.md §9.7): everything the estate protects AND everything
# protecting it lived in one Cloudflare account. Owner decision 2026-08-18,
# verbatim: "Do a and b, don't store in GABI tho store in a new folder called
# GABI_backup on drive" — both a local PC mirror and a Drive mirror, the Drive
# copy in a NEW top-level folder outside the book tree.
#
# TWO HALVES, TWO REPOS, EACH WHERE ITS CREDENTIALS ALREADY ARE:
#   1. catalog-platform/scripts/mirror-estate-backups.mjs — `estate-backups`
#      R2 -> C:\Users\nbasl\OneDrive\Documents\estate-backups-mirror\. Node,
#      because it speaks the R2 key grammar its sibling backup scripts define;
#      invoked by ABSOLUTE PATH as a subprocess (the drive_role_parity.py
#      precedent) because it is a different repo and a different runtime.
#      OneDrive syncing that folder is the second cloud, for free.
#   2. scripts/mirror_to_drive.py — that folder -> Google Drive /GABI_backup.
#      Imported and called, the publish_ebooks_manifest.py / STEP 9 precedent,
#      because the Drive OAuth token lives in THIS repo's scripts/token.json.
#
# ⚠️ WHY IT RUNS ON BOTH PATHS. Identical to STEP 9's argument and stronger:
# backups have NOTHING to do with whether a book was bought. The backup
# workflow runs daily at 09:12 UTC regardless, so a mirror wired only to the
# busy path would go stale on exactly the quiet days — and a disaster-recovery
# copy that silently stops tracking is worse than none, because the dashboard
# still says a mirror exists.
#
# ⚠️ SOFT, LIKE STEPS 5.5-5.9 AND 9: a failure is one WARN and the PREVIOUSLY
# mirrored generation keeps standing. That is the right direction — the mirror
# is a second copy of something that still exists in R2, so a failed mirror
# cycle costs freshness, never data. The next cycle retries. It must never be
# able to fail the pipeline: the estate's book sync going red because a
# cover tarball did not copy would be a self-inflicted outage.
#
# ⚠️ NOTHING HERE IS COMMITTED, and that is deliberate — the same note STEP 5.9
# and STEP 9 each make about themselves. Every output lands OUTSIDE every repo
# (the mirror root is under OneDrive\Documents\, not under vs-code-repos\), so
# this step adds NO entry to _auto_commit_and_push's _ALLOWLIST. There is
# nothing to stage, which is the point: ~515 MiB of database dumps and cover
# tarballs must never approach a public repo.
#
# ⚠️ RETENTION IS "FOLLOW THE BUCKET", NOT "KEEP FOREVER". Both halves keep the
# newest N generations — N read out of backup.yml's own --keep argument so it
# cannot drift — and delete older ones. A copy meant to outlive the bucket's
# retention must be taken by hand and put somewhere neither script manages.
# ---------------------------------------------------------------------------

MIRROR_TIMEOUT_S = 1800  # ~515 MiB over a home connection; the first run took ~5 min
MIRROR_SCRIPT = PROJECT_ROOT.parent.parent / "catalog-platform" / "scripts" / "mirror-estate-backups.mjs"


def _mirror_estate_backups(label: str = "[STEP 10] Mirroring the estate backups off Cloudflare") -> None:
    """Refresh the local + Google Drive copies of `estate-backups`.

    Never raises. Each half is its own failure domain: the Drive upload is
    still attempted when the R2 pull fails, because a previously-pulled
    generation that has not yet reached Drive should still get there.
    """
    import subprocess

    print(f"\n{label}...")

    # --- half 1: estate-backups R2 -> the local OneDrive-synced mirror -------
    if not MIRROR_SCRIPT.exists():
        # A named skip, never silence. The usual cause is a machine that does
        # not have the sibling repo checked out beside this one.
        print(f"  [INFO] R2 mirror SKIPPED: no script at {MIRROR_SCRIPT} "
              "(catalog-platform must be checked out beside audiobook_catalog).")
    else:
        try:
            proc = subprocess.run(
                ["node", str(MIRROR_SCRIPT)],
                cwd=str(MIRROR_SCRIPT.parent.parent),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=MIRROR_TIMEOUT_S,
            )
            for line in (proc.stdout or "").splitlines():
                if line.startswith(("stores mirrored", "objects ", "mirror holds", "  [WARN]", "⚠️")):
                    print(f"  | {line.strip()}")
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()
                print(f"  [WARN] R2 mirror incomplete (exit {proc.returncode}): "
                      f"{tail[-1] if tail else 'no output'}")
                print("  [WARN] The previously mirrored generation still stands; next cycle retries.")
        except subprocess.TimeoutExpired:
            print(f"  [WARN] R2 mirror TIMED OUT after {MIRROR_TIMEOUT_S}s and was killed. "
                  "A killed fetch leaves a partial file, which the next run re-downloads "
                  "(the manifest records the expected size).")
        except Exception as e:  # noqa: BLE001 — own failure domain, by design
            print(f"  [WARN] R2 mirror could not be started: {e}")

    # --- half 2: the local mirror -> Google Drive /GABI_backup ---------------
    try:
        from scripts.mirror_to_drive import main as mirror_to_drive_main
        rc = mirror_to_drive_main([])
        if rc != 0:
            print(f"  [WARN] Drive mirror incomplete (rc={rc}) — the previous Drive copy "
                  "still stands. Next cycle retries.")
    except Exception as e:  # noqa: BLE001 — own failure domain, by design
        print(f"  [WARN] Drive mirror failed: {e}")


def _run_drive_parity(label: str = "[STEP 8] Drive ⇄ role parity (report + auto-apply role→Drive)") -> None:
    """Run scripts/drive_role_parity.py for this cycle.

    Never raises. Exactly one named line is printed on every path — applied,
    in sync, fuse tripped, skipped, failed.
    """
    import subprocess

    print(f"\n{label}...")

    if not DRIVE_PARITY_TOKEN.exists():
        # A named skip, never silence: a machine that never reconciles must be
        # distinguishable from one that reconciles and finds nothing.
        print(f"  [INFO] Parity SKIPPED: no Drive token at {DRIVE_PARITY_TOKEN} "
              "(run `python scripts/drive_auth.py` once on this machine).")
        _parity_report("skipped", "no Drive token on this machine")
        return

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # see reason 2 in the block above
    cmd = [
        sys.executable,
        str(DRIVE_PARITY_SCRIPT),
        "--commit",
        "--apply-to-drive",   # the ONE direction that may write. Drive→role
                              # is report-only forever and is not requested.
        "--json-summary",
    ]

    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=DRIVE_PARITY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"  [WARN] Parity TIMED OUT after {DRIVE_PARITY_TIMEOUT_S}s and was killed. "
              "The usual cause is Drive OAuth wanting an interactive browser flow — "
              "run `python scripts/drive_auth.py` on this machine.")
        _parity_report("skipped", f"timed out after {DRIVE_PARITY_TIMEOUT_S}s (Drive auth may need a human)")
        return
    except Exception as e:  # noqa: BLE001 — independent failure domain, by design
        print(f"  [WARN] Parity could not be started: {e}")
        _parity_report("failed", f"could not start: {e}")
        return

    # The report itself goes to the local log in full — names, levels, every
    # bucket. This is the audit trail for an unattended mutation, so it is
    # printed whether the run succeeded or not.
    if proc.stdout:
        for line in proc.stdout.splitlines():
            if not line.startswith("PARITY_JSON "):
                print(f"  | {line}")

    summary = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("PARITY_JSON "):
            try:
                summary = json.loads(line[len("PARITY_JSON "):])
            except json.JSONDecodeError:
                summary = None

    if summary is None:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = tail[-1] if tail else f"exit {proc.returncode}, no output"
        # A credentials-shaped failure is a SKIP (nothing is wrong with the
        # world, this machine just is not set up); anything else is a WARN,
        # and a missing exception list must always land here rather than in
        # the skip bucket — silently not-reconciling because the list is gone
        # is the exact failure that list exists to make loud.
        creds_shaped = any(
            s in tail for s in ("Drive credentials", "credentials.json", "service account")
        )
        if creds_shaped:
            print(f"  [INFO] Parity SKIPPED: {tail}")
            _parity_report("skipped", tail[:200])
        else:
            print(f"  [WARN] Parity FAILED: {tail}")
            print("  [WARN] Permissions are unchanged; the next cycle retries.")
            _parity_report("failed", tail[:200])
        return

    _report_parity_summary(summary)


def _report_parity_summary(summary: dict) -> None:
    """Turn STEP 8's PARITY_JSON line into one named console line and one
    pipeline_status summary entry. Split out from _run_drive_parity() so the
    process handling and the outcome vocabulary stay separately readable."""
    state = summary.get("state", "unknown")
    applied = summary.get("applied", [])
    counts = summary.get("counts", {})
    drift = counts.get("mismatch", 0) + counts.get("drive_only_untriaged", 0)

    if state == "applied":
        # Names to the LOCAL log only — this is the audit trail for a change
        # made with nobody watching.
        print(f"  Parity APPLIED {len(applied)} change(s): {', '.join(applied)}")
        _parity_report("applied", f"{len(applied)} applied, {drift} drift row(s) reported")
    elif state == "fuse-tripped":
        print(f"  [WARN] Parity FUSE TRIPPED: {summary.get('fuseReason', '')}")
        _parity_report(
            "fuse-tripped",
            f"{summary.get('planned', 0)} changes wanted, cap {summary.get('cap')} — nothing applied",
        )
    elif state == "in-sync":
        print(f"  Parity in sync — nothing to apply ({drift} row(s) reported for review).")
        _parity_report("in-sync", f"in sync, {drift} row(s) reported for review")
    elif state == "failed":
        failed = summary.get("failed", [])
        print(f"  [WARN] Parity could not apply {len(failed)} planned change(s).")
        _parity_report("failed", f"{len(failed)} change(s) failed to apply")
    else:
        print(f"  [WARN] Parity finished in an unexpected state: {state}")
        _parity_report(state, f"unexpected state {state}")

    if summary.get("estateUnreadable"):
        # Not fatal and not a failure — the script degrades on purpose — but a
        # cycle that could not read the estate directory made NO role→Drive
        # decisions, and that must be visible rather than read as "in sync".
        print(f"  [WARN] Estate directory was unreadable this cycle: {summary['estateUnreadable']}")


# ---------------------------------------------------------------------------
# STEP 0b — Drive → local pull. Bring down books someone dropped straight into
# Drive so they ingest here.
#
# 🔴 WHY THIS RUNS AT ALL: the pipeline only ever pushed local → Drive. A file
# added to Drive by hand (or by Justin's box) never came DOWN, so it never
# sorted, catalogued, indexed or reached the sites — the exact gap the
# 2026-08-24 duplicate incident exposed. This is the missing pull, now
# ENFORCING (owner: "end users expect books fast, we're safe to rip the drive
# pull down right away"). The matcher is all-format, copy-safe and series-safe;
# see app/core/drive_pull.py's header for the three rules it enforces.
#
# Shelled out to scripts/drive_pull.py (not imported), for STEP 8's reasons:
# it owns its own Drive client (reused from audit_drive_vs_local), a subprocess
# gives a hard timeout against an interactive-OAuth hang, and its failure domain
# stays independent — a pull that dies must never cost the sort/upload run. Like
# every side-step here it NEVER raises: exactly one named line on every path.
#
# KILL SWITCH: DRIVE_PULL_ENABLED (default ON). Set it to 0/false/no/off to stop
# pulling without a code change. DRIVE_PULL_TIMEOUT_S (default 1800) bounds a run.
#
# CHURN: a pulled file lands in ROOT_DIR/<Author>/ but is NOT in
# upload_manifest.json, so a later cycle's STEP 2 detect flags it once. STEP 4
# then finds it already on Drive (check_file_exists_on_drive → already_existed),
# SKIPS the upload (no re-upload, no duplicate) and records a manifest entry —
# after which detect never flags it again. Bounded to one detect+list, never a
# per-run loop. (It is not detected the SAME cycle it is pulled: the file is
# freshly written, so detect's age guard holds it until it has settled.)
# ---------------------------------------------------------------------------
DRIVE_PULL_SCRIPT = SCRIPTS_DIR / "drive_pull.py"
DRIVE_PULL_TIMEOUT_S = int(os.getenv("DRIVE_PULL_TIMEOUT_S", "1800"))


def _drive_pull_enabled() -> bool:
    """The kill switch. Default ON (owner: enforce now). Only an explicit
    off-word disables it, so a typo fails safe toward pulling."""
    return os.getenv("DRIVE_PULL_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off", ""
    )


def _run_drive_pull(label: str = "[STEP 0b] Drive → local pull (enforcing)") -> int:
    """Pull Drive-only books to local for this cycle. Returns the number pulled
    (0 on disabled/skip/timeout/failure). Never raises — same contract as
    _run_drive_parity(); modelled on it line for line."""
    import subprocess

    print(f"\n{label}...")

    if not _drive_pull_enabled():
        print("  [INFO] Pull DISABLED via DRIVE_PULL_ENABLED — nothing pulled.")
        return 0

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # see reason 2 in STEP 8's block above
    cmd = [
        sys.executable,
        str(DRIVE_PULL_SCRIPT),
        "--enforce",       # the owner wants it actually pulling, not report-only
        "--json-summary",
    ]

    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=DRIVE_PULL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"  [WARN] Pull TIMED OUT after {DRIVE_PULL_TIMEOUT_S}s and was killed. "
              "The usual cause is Drive OAuth wanting an interactive browser flow — "
              "run `python scripts/drive_auth.py` on this machine. Nothing was left "
              "half-ingested: downloads stage to a temp file and only atomic-move on "
              "completion.")
        return 0
    except Exception as e:  # noqa: BLE001 — independent failure domain, by design
        print(f"  [WARN] Pull could not be started: {e}")
        return 0

    # Full output to the local log — the audit trail for an unattended download.
    if proc.stdout:
        for line in proc.stdout.splitlines():
            if not line.startswith("PULL_JSON "):
                print(f"  | {line}")

    summary = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("PULL_JSON "):
            try:
                summary = json.loads(line[len("PULL_JSON "):])
            except json.JSONDecodeError:
                summary = None

    if summary is None:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = tail[-1] if tail else f"exit {proc.returncode}, no output"
        print(f"  [WARN] Pull produced no summary: {tail}")
        print("  [WARN] Nothing pulled this cycle; the next cycle retries.")
        return 0

    pulled = int(summary.get("pulled", 0))
    if pulled:
        print(f"  Pulled {pulled} Drive-only file(s) to local "
              f"(they ingest from the next detect on).")
    else:
        print("  Pull in sync — nothing new on Drive to bring down "
              f"({summary.get('present', 0)} already local, "
              f"{summary.get('skippedCopies', 0)} copies skipped).")
    return pulled


# ---------------------------------------------------------------------------
# STEP 11 — link the sibling catalogues (audiobook ⇄ library)
#
# 🔴 WHY THIS STEP EXISTS AT ALL: the sweep was hand-run, and that WAS the bug.
# `library_catalog/scripts/backfill-audiobook-holdings.mjs` writes the
# `audiobook_holding` / `audiobook_series_holding` tables that tell the library
# site "you already own this on audio". Nothing ran it on a schedule, so it
# drifted the moment either catalogue grew: measured 2026-08-22, **401 of 493
# works had arrived since its last run**, and work 514 (Elantris) showed no
# audio at all despite the household owning two editions of it. Re-running it by
# hand fixed that day and fixed nothing about the next one. A sweep whose
# freshness depends on somebody remembering is a sweep that is stale.
#
# ⚠️ IT MUST RUN ON THE IDLE PATH, AND THAT IS THE MORE IMPORTANT HALF — a
# stronger argument than STEP 8's, STEP 9's or STEP 10's. Those steps merely
# have nothing to do with books; this one is driven by books in the WRONG
# CATALOGUE. The drift arrives when the **library** gains a print or ebook
# title, which is completely uncorrelated with whether THIS machine gained an
# audiobook — and a scan session in the library adds dozens of works on a day
# nobody buys audio. Wiring this only to the busy path would mean the link
# refreshed exactly as often as the owner buys audiobooks, which reproduces the
# precise failure being fixed here rather than repairing it.
#
# Shelled out, not imported, for STEP 8's reasons (see its header): it is
# another repo's Node program with its own failure domain and its own fatal
# exits, PYTHONIOENCODING must be forced on the child because the report prints
# ⚠️ and em-dashes that die on a captured cp1252 pipe, and a hard timeout is the
# only defence against `wrangler` deciding it wants an interactive login on an
# unattended machine.
#
# ⚠️ THE EXECUTABLE IS RESOLVED, NOT HARDCODED. The documented command is
# `npx tsx scripts/backfill-audiobook-holdings.mjs --remote --commit`, but a
# bare "npx" in an argv list does NOT start on Windows — npx is `npx.cmd`, and
# CreateProcess only ever appends `.exe`. So this uses the same idiom
# publish_ebooks_manifest.py / publish_audio_manifest.py already use for
# wrangler: prefer the repo-local `node_modules/.bin/tsx(.cmd)`, fall back to
# `npx --yes tsx`. `tsx` rather than plain `node` because the script imports
# `packages/core/src/titles.ts`.
#
# ⚠️ ONE FAILURE DOMAIN, AND IT NEVER RAISES. A failed link leaves the PREVIOUS
# holdings standing — the sweep marks vanished matches `stale_at` rather than
# deleting them (migration 0003's rule), so a skipped cycle costs freshness and
# never data. Exactly one named line is printed on every path: applied, in sync,
# skipped, or failed.
#
# ⚠️ IT WRITES ANOTHER APP'S PRODUCTION D1 (`--remote --commit`), which is why
# the manual `link` step is classified `publishing` and not `mutating`: the
# top confirmation tier is right for a button that reaches into a different
# application's live database.
#
# Reporting: a `link` step DETAIL, not a summary field — unlike STEP 8, which
# had to use set_summary() because it had no step key of its own and could not
# borrow `publish`'s (the idle path never runs `publish`). This step DOES have
# its own key in pipeline_status.STEPS, so the detail line is visible on both
# paths and on a manual single-step run.
#
# ⚠️ The step key `link` is MIRRORED BY HAND in four other places and there is
# no shared module — see STEP_INFO's comment above. app/pipeline_status.py's
# STEPS, app/tools/pipeline_watcher.py's PIPELINE_STEP_CHOICES and
# firestore.rules' validPipelineStep() on this side; auth-worker's ops.ts
# PIPELINE_STEPS and heygabi-home's status/pipelines/pipelines.js on the
# catalog-platform side. Miss one and the step renders unlabelled, or the
# remote trigger is rejected as an unknown step.
# ---------------------------------------------------------------------------

# ~493 works against REMOTE D1 plus an npx cold start. The 2026-08-22 hand-run
# sent 296 statements; 15 minutes is generous rather than measured-tight, which
# is the right side to err on for a step that can only ever cost freshness.
LINK_TIMEOUT_S = 900
LINK_SCRIPT_REL = "scripts/backfill-audiobook-holdings.mjs"

try:
    from app.config import LIBRARY_CATALOG_DIR
except Exception:  # pragma: no cover — same defensive stance as the pstatus import
    LIBRARY_CATALOG_DIR = None  # type: ignore[assignment]


def _link_report(state: str, detail: str) -> None:
    """One place that writes the STEP 11 outcome, so every path reports.

    Mirrors _parity_report()'s shape, but writes a step DETAIL as well —
    `link` is a real key in pipeline_status.STEPS, which parity never was.
    """
    pstatus.step_detail("link", detail)
    pstatus.set_summary(
        siblingLinkState=state,
        siblingLinkDetail=detail,
        siblingLinkAt=datetime.now().isoformat(timespec="seconds"),
    )


def _link_tsx_cmd(repo: Path) -> list[str] | None:
    """The argv prefix that runs a .mjs through tsx in `repo`, or None when
    this machine has no way to run one. See the header: a bare "npx" cannot be
    exec'd on Windows, so the repo-local binary is preferred."""
    import shutil

    local = repo / "node_modules" / ".bin" / ("tsx.cmd" if os.name == "nt" else "tsx")
    if local.exists():
        return [str(local)]
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        return None
    return [npx, "--yes", "tsx"]


def _run_sibling_link(
    label: str = "[STEP 11] Linking sibling catalogues (audiobook ⇄ library)",
    mark_step: bool = True,
) -> None:
    """Run library_catalog's backfill-audiobook-holdings.mjs for this cycle.

    Never raises. Exactly one named line is printed on every path — applied,
    in sync, skipped, failed.

    ⚠️ `mark_step=False` on the IDLE path, and this is not cosmetic.
    pstatus.step() marks every entry BEFORE the named one 'done' — true on the
    busy path, where sort/upload/catalog/publish really did run, and a
    fabrication on the idle path, where STEP 2 returned early and none of them
    did. So the idle cycle writes the DETAIL (the sweep genuinely ran, and its
    result is a real fact) without claiming the five steps above it happened.
    Same stance as _push_estate_index()'s `record_step`, arrived at from the
    other side: that step suppresses a detail it did not earn; this one
    suppresses a position it did not reach.
    """
    import subprocess

    print(f"\n{label}...")
    if mark_step:
        pstatus.step("link")

    # --- three named skips, never silence. A machine that CANNOT reach the
    # sibling must be distinguishable from one that reached it and found
    # nothing to do; "0 statements" and "no checkout" are different facts and
    # the status page must not render them the same. -----------------------
    if LIBRARY_CATALOG_DIR is None:
        print("  [INFO] Sibling link SKIPPED: app/config.py could not be imported, so "
              "this machine cannot name a library_catalog checkout.")
        _link_report("skipped", "app/config unavailable — no library_catalog path")
        return

    repo = Path(LIBRARY_CATALOG_DIR)
    script = repo / LINK_SCRIPT_REL
    if not script.exists():
        print(f"  [INFO] Sibling link SKIPPED: no library_catalog checkout at {repo} "
              "(check out bookbuddy/library_catalog beside this repo, or set "
              "LIBRARY_CATALOG_DIR).")
        _link_report("skipped", f"no library_catalog checkout at {repo}")
        return

    prefix = _link_tsx_cmd(repo)
    if prefix is None:
        print(f"  [INFO] Sibling link SKIPPED: neither {repo / 'node_modules' / '.bin'} nor "
              "PATH has tsx/npx (run `npm install` in library_catalog, or install Node.js).")
        _link_report("skipped", "no tsx/npx on this machine")
        return

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # see reason 2 in STEP 8's block above
    cmd = prefix + [LINK_SCRIPT_REL, "--remote", "--commit"]

    try:
        proc = subprocess.run(
            cmd, cwd=str(repo), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=LINK_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"  [WARN] Sibling link TIMED OUT after {LINK_TIMEOUT_S}s and was killed. "
              "The usual cause is wrangler wanting an interactive login — run "
              "`npx wrangler whoami` in library_catalog on this machine.")
        _link_report("skipped", f"timed out after {LINK_TIMEOUT_S}s (wrangler may need a human)")
        return
    except Exception as e:  # noqa: BLE001 — independent failure domain, by design
        print(f"  [WARN] Sibling link could not be started: {e}")
        _link_report("failed", f"could not start: {e}")
        return

    # The sweep's own report goes to the LOCAL log in full — match counts, the
    # containment matches it wants a human to read, the titles it could not
    # place. This is the audit trail for an unattended write to another app's
    # production database, so it is printed whether the run succeeded or not.
    for line in (proc.stdout or "").splitlines():
        if line.strip():
            print(f"  | {line}")

    summary = _parse_link_summary(proc.stdout or "")

    if summary is None:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = tail[-1] if tail else f"exit {proc.returncode}, no output"
        print(f"  [WARN] Sibling link FAILED: {tail}")
        print("  [WARN] The previous holdings still stand; the next cycle retries.")
        _link_report("failed", tail[:200])
        return

    sent, detail = summary
    if sent == 0:
        print(f"  Sibling link in sync — nothing to write ({detail}).")
        _link_report("in-sync", f"in sync — {detail}")
    else:
        print(f"  Sibling link APPLIED {sent} statement(s): {detail}")
        _link_report("applied", f"{sent} statement(s) applied — {detail}")


# The sweep's last line, verbatim from backfill-audiobook-holdings.mjs:
#   "296 statement(s) run. 121 live holding(s) of 154 row(s), and 12 live audio
#    rung(s) of 14, in the REMOTE database."
# ⚠️ Parsed rather than trusted-by-position: the script also exits 0 EARLY when
# the database has no works at all, printing no such line. Finding no line is
# therefore a FAILED/unknown outcome, never a silent success — that early exit
# is exactly the case where reporting "in sync" would be a lie.
_LINK_SUMMARY_RE = re.compile(
    r"^(?P<sent>\d+) statement\(s\) run\.\s*(?P<rest>.*)$"
)


def _parse_link_summary(stdout: str) -> tuple[int, str] | None:
    """(statements_run, the rest of the sweep's final line), or None."""
    for line in reversed(stdout.splitlines()):
        m = _LINK_SUMMARY_RE.match(line.strip())
        if m:
            return int(m.group("sent")), m.group("rest").strip().rstrip(".")
    return None


# ---------------------------------------------------------------------------
# Auto-commit and push (for autonomous operation)
# ---------------------------------------------------------------------------


def _count_unpushed_commits() -> int | None:
    """How many local commits are ahead of the upstream branch, or None when
    that can't be determined (no upstream configured, detached HEAD, git
    error). None means 'unknown' — callers treat it as 'nothing stranded' so a
    read failure never manufactures a false run failure (F1, 2026-08-24)."""
    import subprocess

    res = subprocess.run(
        ["git", "rev-list", "--count", "@{u}..HEAD"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    if res.returncode != 0:
        return None
    try:
        return int(res.stdout.strip())
    except ValueError:
        return None


def _push_pending_commits() -> bool:
    """Self-heal a previously stranded commit (F1, 2026-08-24).

    A transient push failure on an earlier run can leave the catalog commit
    local-only; because the idle path never calls ``_auto_commit_and_push``,
    that commit would otherwise sit unpushed until the *next* busy run — days,
    if the library is quiet. This checks for local commits ahead of origin and,
    if any exist, retries the push (matching how STEP 7 already self-heals a
    remote push). Returns True when nothing is stranded (or the state is
    unknown), False when commits remain unpushed after the retry."""
    import subprocess

    count = _count_unpushed_commits()
    if not count:  # None (unknown) or 0 (level with upstream)
        return True
    print(f"  [WARN] {count} local commit(s) not on origin — retrying push...")
    subprocess.run(
        ["git", "pull", "--rebase", "--autostash"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    push = subprocess.run(
        ["git", "push"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    if push.returncode != 0:
        print(f"  [WARN] Retry push failed: {push.stderr.strip()}")
    remaining = _count_unpushed_commits()
    return not remaining


def _auto_commit_and_push() -> bool:
    """Commit updated catalog/site files and push to trigger deploy + Discord.

    Returns True when the catalog is fully published — either nothing needed
    committing (and no earlier commit is stranded) or the commit reached
    origin. Returns False when a local commit exists that did NOT reach origin
    (a commit or push failure, or an exception). The caller MUST fold a False
    into the run outcome so the /status panel stops reading green on a run whose
    work never left this machine (F1, 2026-08-24): a failed push used to print
    one WARN and return, while finish_run keyed only on upload failures, so the
    panel went green, deploy.yml never fired, and prod silently fell behind."""
    import subprocess

    try:
        # Check if there are changes to commit
        status = subprocess.run(
            ["git", "status", "--porcelain", "site/", "author_drive_map.json"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if not status.stdout.strip():
            print("  No catalog changes to commit.")
            # Even with nothing new to commit, an earlier run may have left a
            # commit stranded locally (transient push failure). Retry it here so
            # a quiet run heals it instead of leaving prod behind indefinitely.
            return _push_pending_commits()

        # Stage site files.
        # ⚠️ site/covers/ is deliberately NOT here: covers live in Cloudflare
        # R2 and the directory is gitignored. Naming an ignored path makes
        # `git add` exit 1 with "paths are ignored by .gitignore" (measured:
        # the other paths DO still get staged, so this was noise rather than
        # breakage — but returncode is not checked here, so the noise would
        # have been invisible too).
        # site/covers_manifest.json is its replacement — the committed record
        #
        # ⚠️ site/ebooks.json LEFT THIS LIST on 2026-08-17, for the same class
        # of reason and a sharper one: the manifest is now gitignored because
        # this repo is PUBLIC, so committing it would publish the whole shelf
        # at a raw URL regardless of what the deployment serves (owner
        # directive: "I don't want people scraping my books"). Naming an
        # ignored path here would make `git add` exit 1 - the same noise
        # site/covers/ used to make. It reaches readers through STEP 5.8.
        # of what is in the bucket, and what the promote audit checks.
        # ⚠️ THE SAME LIST IS PASSED TO `git commit` AS A PATHSPEC BELOW.
        # Incident 2026-08-17: `git add <allowlist>` followed by a bare
        # `git commit -m` commits the ENTIRE index — and a concurrent agent
        # had its own files STAGED at that moment, so the 16:00:46 auto-commit
        # (1c3c2af) swept three of its reader modules and two test files into
        # a "catalog refresh". Staging an allowlist is only half the rule;
        # the commit must be scoped too, or it inherits whatever anyone else
        # left in the shared index.
        #
        # ⚠️ site/audio_manifest.json is DELIBERATELY not here (STEP 5.9,
        # 2026-08-17). It is gitignored — it lists the household's books by
        # filename — so naming it would make `git add` exit 1, and committing
        # it would publish 630 GB worth of paths from a PUBLIC repo.
        # ⚠️ MIRRORED IN .github/workflows/auto-promote.yml (the `allow=`
        # regex). What the pipeline may COMMIT and what the promote gate will
        # PROMOTE are the same list at two altitudes, and they drifted by one
        # entry on 2026-08-19: "site/ebooks_status.json" was added HERE and not
        # there, so every commit carrying it was refused at the gate. Three
        # days of books reached /dev/ and never prod, silently, because the
        # runs in between reported "skipped" rather than failing. ADD TO BOTH
        # IN THE SAME COMMIT.
        _ALLOWLIST = [
            "site/catalog.csv", "site/index.html", "site/ebooks.html",
            "site/covers_manifest.json", "site/covers-base.js",
            "site/stats.html", "site/chapters.json", "site/content_warnings.json",
            "site/additions_log.json", "site/ebooks_status.json", "author_drive_map.json",
        ]
        subprocess.run(
            ["git", "add", *_ALLOWLIST],
            cwd=str(PROJECT_ROOT), capture_output=True,
        )

        # Count new books for commit message
        changed_files = status.stdout.strip().split("\n")
        num_changes = len(changed_files)

        # Commit — scoped to the allowlist pathspec (`--`), so other writers'
        # staged files stay in the index, untouched and uncommitted.
        commit_msg = f"feat(catalog): Auto-update catalog ({num_changes} file changes)"
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg, "--", *_ALLOWLIST],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            print(f"  [WARN] Commit failed: {result.stderr.strip()}")
            # There WERE catalog changes (status check above), so a failed
            # commit means they never got published — a real failure state.
            return False

        print(f"  Committed: {commit_msg}")

        # Pull with rebase to avoid push failures when remote has diverged.
        # --autostash (2026-08-16): without it, ANY uncommitted file in the
        # tree at this moment (a human editing, an agent mid-work elsewhere
        # in the repo) makes the rebase fail outright — the code below then
        # just warns and "attempts push anyway", so the catalog commit made
        # above stays local-only forever while the log reads WARN and the
        # run otherwise looks successful. --autostash stashes any
        # uncommitted changes before the rebase and reapplies them after, so
        # someone else's in-progress edit no longer blocks this push. It
        # does not change what gets staged/committed above (still the
        # explicit allowlist a few lines up) — it only makes the pull step
        # tolerant of unrelated uncommitted files sitting in the tree.
        pull_result = subprocess.run(
            ["git", "pull", "--rebase", "--autostash"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if pull_result.returncode != 0:
            print(f"  [WARN] Pull --rebase --autostash failed: {pull_result.stderr.strip()}")
            print("  Attempting push anyway...")

        # Push
        result = subprocess.run(
            ["git", "push"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            print(f"  [WARN] Push failed: {result.stderr.strip()}")
            print(
                "  [WARN] The catalog commit is LOCAL-ONLY — origin/main did not "
                "advance, so deploy.yml will not fire and prod is behind. The run "
                "is marked degraded; the next run will retry the push."
            )
            # Signal the failure up so finish_run does not report success (F1).
            return False

        print("  Pushed to origin. Deploy + Discord notification will fire.")
        return True

    except Exception as e:
        print(f"  [ERROR] Auto-commit failed: {e}")
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Audiobook pipeline: sort from OpenAudible and upload to Google Drive"
    )
    parser.add_argument(
        "--sort-only",
        action="store_true",
        help="Only sort books into author folders, don't upload",
    )
    parser.add_argument(
        "--upload-only",
        action="store_true",
        help="Only upload new books, skip sorting step",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would happen without making changes",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Force refresh of Drive folder cache",
    )
    parser.add_argument(
        "--rebuild-only",
        action="store_true",
        help=(
            "Skip sort/detect/upload entirely and just rebuild + publish the "
            "catalog/site from what's already on disk (STEP 5 onward). For "
            "metadata-only fixes on a book that's already uploaded — e.g. "
            "adding a missing narrator tag — which the normal pipeline can't "
            "see since STEP 2 only looks for new files to upload."
        ),
    )
    parser.add_argument(
        "--step",
        choices=STEP_CHOICES,
        default=None,
        help=(
            "Run ONE pipeline stage in isolation (fine-grained manual control, "
            "catalog-platform /status Operations section) instead of the whole "
            "pipeline: audit, sort, detect, folders, upload, catalog, "
            "publish, or link (STEP 11 — re-links the sibling library "
            "catalogue; writes ANOTHER APP's production D1). Takes the exact "
            "same single-flight lock as every other "
            "run, so it can never overlap another run — it fails loudly and "
            "immediately (never defers) if the lock is held. Mutually "
            "exclusive with --sort-only/--upload-only/--rebuild-only/--dry-run."
        ),
    )
    args = parser.parse_args()

    if args.sort_only and args.upload_only:
        print("ERROR: Cannot use --sort-only and --upload-only together.")
        sys.exit(1)

    if args.rebuild_only and (args.sort_only or args.upload_only):
        print("ERROR: Cannot use --rebuild-only with --sort-only or --upload-only.")
        sys.exit(1)

    if args.rebuild_only and args.dry_run:
        # app.main.main() (STEP 5) has no dry-run mode of its own — it always
        # writes catalog.csv/index.html/etc — so there is nothing honest for
        # --dry-run to preview here. Fail loudly rather than silently
        # ignoring one of the two flags.
        print("ERROR: --rebuild-only has no dry-run mode (app.main always writes).")
        sys.exit(1)

    if args.step and (args.sort_only or args.upload_only or args.rebuild_only or args.dry_run):
        print("ERROR: --step cannot be combined with --sort-only/--upload-only/--rebuild-only/--dry-run.")
        sys.exit(1)

    if args.refresh_cache and DRIVE_FOLDERS_CACHE_PATH.exists():
        DRIVE_FOLDERS_CACHE_PATH.unlink()
        print("Drive folder cache cleared.")

    # A crash must still close out the status doc, otherwise the panel shows a
    # run stuck "running" forever and there is no way to tell from off-site.
    #
    # ⚠️ PIPELINE_TRIGGER default changed 2026-08-16 from "scheduled" to
    # "manual" (docs/info/ROLES.md §1d): the "scheduled" value is now
    # reserved EXCLUSIVELY for the true 8h Task Scheduler firing — it is the
    # one trigger that defers-and-retries for up to 2h instead of failing
    # immediately when the pipeline lock is held (app/core/pipeline_schedule.py).
    # scripts/sync_pipeline_8h.bat sets PIPELINE_TRIGGER=scheduled explicitly
    # for that reason. Before this change, a human typing this command by
    # hand got the SAME "scheduled" default — meaning a blocked manual run
    # would have silently sat retrying for up to 2 hours instead of telling
    # them right away that something else is running. Defaulting to "manual"
    # makes every other invocation (a human at a terminal, --rebuild-only's
    # own "manual-rebuild" default, the watcher's explicit "manual") fail
    # loudly and immediately instead, which is what the owner's spec asks
    # for outside the one real scheduled slot.
    try:
        if args.step:
            run_step(args.step, trigger=f"manual-step:{args.step}")
        elif args.rebuild_only:
            run_rebuild_only(trigger=os.getenv("PIPELINE_TRIGGER", "manual-rebuild"))
        else:
            run_pipeline(
                sort_only=args.sort_only,
                upload_only=args.upload_only,
                dry_run=args.dry_run,
                trigger=os.getenv("PIPELINE_TRIGGER", "manual"),
            )
    except pipeline_lock.PipelineLockHeld:
        # Already printed and reported to pipeline_status inside run_pipeline()/
        # run_rebuild_only() — calling pstatus.fail_run() here would overwrite
        # that clear "blocked by X since Y" message with a generic traceback.
        sys.exit(1)
    except BaseException as e:
        pstatus.fail_run(e)
        raise


if __name__ == "__main__":
    main()
