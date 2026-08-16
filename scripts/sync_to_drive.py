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
import sys
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


def load_manifest() -> dict:
    """Load upload manifest. Structure: {relative_path: {uploaded_at, drive_file_id}}"""
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(manifest: dict) -> None:
    """Persist the upload manifest."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


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
    """Cache Drive folders locally for faster subsequent lookups."""
    with open(DRIVE_FOLDERS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(folders, f, ensure_ascii=False, indent=2)


def load_drive_folders_cache() -> dict | None:
    """Load cached Drive folders if recent (less than 1 hour old)."""
    if not DRIVE_FOLDERS_CACHE_PATH.exists():
        return None
    # Check age
    age = time.time() - DRIVE_FOLDERS_CACHE_PATH.stat().st_mtime
    if age > 3600:  # 1 hour
        return None
    with open(DRIVE_FOLDERS_CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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

    # 6. If we have a decent fuzzy match, confirm with user
    if scored:
        best_name = scored[0][0]
        best_score = scored[0][1]
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
    trigger: str = "scheduled",
) -> None:
    """Run the full audiobook pipeline."""
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
        print("=" * 60)
        # The common case: an idle scheduled run. Report it as a real success
        # so the panel shows "checked, nothing new" rather than a stale run.
        pstatus.set_summary(idle=True)
        pstatus.finish_run("success")
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

        # Push the new books' cover art to R2 BEFORE committing. Covers are
        # not in git any more (see .gitignore / docs/info/covers-r2.md); the
        # site links straight at the bucket, so an uncommitted-but-uploaded
        # cover is fine and a committed-but-unuploaded one is a broken image.
        # Upload first, commit second — the same ordering as the migration.
        print("\n[STEP 5.7] Uploading new covers to R2...")
        try:
            from scripts.upload_covers_r2 import main as upload_covers_main
            rc = upload_covers_main([])
            if rc != 0:
                print("  [WARN] Some covers failed to upload — they will retry next run.")
        except Exception as e:
            print(f"  [WARN] Cover upload failed: {e}")
    elif uploaded_count > 0:
        print("\n[STEP 5] Skipped catalog rebuild (dry-run)")

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
    if not dry_run:
        print("\n[STEP 6] Auto-commit & push...")
        pstatus.step("publish")
        _auto_commit_and_push()

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
    pstatus.finish_run(outcome.run_state())


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
#   index push / STEP 6 (commit+push) INCLUDE — required so the tag fix
#            actually ships instead of sitting rebuilt-but-uncommitted, same
#            failure mode this flag exists to close.
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
    """Rebuild the catalog/site from what's on disk and publish it, WITHOUT
    sort/detect/upload. See the module note above this function for the
    per-step reasoning."""
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

    print("\n[REBUILD-ONLY] Auto-commit & push (STEP 6)...")
    pstatus.step("publish")
    _auto_commit_and_push()

    # Same as run_pipeline: clears any flagged content-warning requests,
    # unconditional on every non-dry run.
    try:
        from app.tools.fetch_content_warnings import fulfill_requests
        fulfill_requests()
    except Exception as e:
        print(f"  [WARN] Warning-request fulfillment failed: {e}")

    print("=" * 60)
    pstatus.finish_run("success")


# ---------------------------------------------------------------------------
# Auto-commit and push (for autonomous operation)
# ---------------------------------------------------------------------------


def _auto_commit_and_push() -> None:
    """Commit updated catalog/site files and push to trigger deploy + Discord."""
    import subprocess

    try:
        # Check if there are changes to commit
        status = subprocess.run(
            ["git", "status", "--porcelain", "site/", "author_drive_map.json"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if not status.stdout.strip():
            print("  No catalog changes to commit.")
            return

        # Stage site files.
        # ⚠️ site/covers/ is deliberately NOT here: covers live in Cloudflare
        # R2 and the directory is gitignored. Naming an ignored path makes
        # `git add` exit 1 with "paths are ignored by .gitignore" (measured:
        # the other paths DO still get staged, so this was noise rather than
        # breakage — but returncode is not checked here, so the noise would
        # have been invisible too).
        # site/covers_manifest.json is its replacement — the committed record
        # of what is in the bucket, and what the promote audit checks.
        subprocess.run(
            ["git", "add", "site/catalog.csv", "site/index.html",
             "site/covers_manifest.json", "site/covers-base.js",
             "site/stats.html", "site/chapters.json", "site/content_warnings.json",
             "site/additions_log.json", "site/ebooks.json", "author_drive_map.json"],
            cwd=str(PROJECT_ROOT), capture_output=True,
        )

        # Count new books for commit message
        changed_files = status.stdout.strip().split("\n")
        num_changes = len(changed_files)

        # Commit
        commit_msg = f"feat(catalog): Auto-update catalog ({num_changes} file changes)"
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            print(f"  [WARN] Commit failed: {result.stderr.strip()}")
            return

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
            return

        print("  Pushed to origin. Deploy + Discord notification will fire.")

    except Exception as e:
        print(f"  [ERROR] Auto-commit failed: {e}")


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

    if args.refresh_cache and DRIVE_FOLDERS_CACHE_PATH.exists():
        DRIVE_FOLDERS_CACHE_PATH.unlink()
        print("Drive folder cache cleared.")

    # A crash must still close out the status doc, otherwise the panel shows a
    # run stuck "running" forever and there is no way to tell from off-site.
    try:
        if args.rebuild_only:
            run_rebuild_only(trigger=os.getenv("PIPELINE_TRIGGER", "manual-rebuild"))
        else:
            run_pipeline(
                sort_only=args.sort_only,
                upload_only=args.upload_only,
                dry_run=args.dry_run,
                trigger=os.getenv("PIPELINE_TRIGGER", "scheduled"),
            )
    except BaseException as e:
        pstatus.fail_run(e)
        raise


if __name__ == "__main__":
    main()
