"""
Audiobook Pipeline: Sort + Upload to Google Drive.

Workflow:
1. Sort: Move audiobook files from OpenAudible export into author-named folders
2. Catalog Drive: Read all existing folders from Google Drive to avoid duplicates
3. Detect new: Compare local library against upload manifest to find un-uploaded files
4. Match: Use fuzzy matching + Claude LLM to resolve author names to existing Drive folders
5. Upload: Push new files to Google Drive, creating author folders only when truly new

Usage:
    python scripts/sync_to_drive.py              # Full pipeline (sort + upload)
    python scripts/sync_to_drive.py --sort-only  # Just sort, don't upload
    python scripts/sync_to_drive.py --upload-only # Just upload new files (skip sort)
    python scripts/sync_to_drive.py --dry-run    # Preview without making changes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

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
    """
    from app.tools.book_sort import get_author_name
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

    moved = []
    for f in files:
        author = get_author_name(f)
        if not author:
            print(f"  [SKIP] No author metadata: {f.name}")
            continue

        author_folder = target_root / author
        dest = author_folder / f.name

        if dest.exists():
            print(f"  [EXISTS] {author}/{f.name} - already in library")
            continue

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


def detect_new_books(manifest: dict) -> list[Path]:
    """
    Walk the library and find audiobook files not yet in the upload manifest.
    Returns list of Paths to upload.
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
    max_retries: int = 3,
) -> str | None:
    """
    Upload a file to a specific Google Drive folder using resumable upload.
    Checks for duplicates first — skips if file already exists on Drive.
    Retries on transient failures with exponential backoff.
    Returns the Drive file ID or None on failure.
    """
    if dry_run:
        size_mb = file_path.stat().st_size / (1024 * 1024)
        print(f"  [DRY-RUN] Would upload: {file_path.name} ({size_mb:.1f} MB)")
        return "dry-run-file-id"

    # Check if file already exists on Drive
    existing_id = check_file_exists_on_drive(service, file_path.name, folder_id)
    if existing_id:
        print(f"  [SKIP] Already on Drive: {file_path.name}")
        return existing_id

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

            file_id = response.get("id")
            print(f"\r{label} ... done ({file_id})")
            return file_id

        except Exception as e:
            print(f"\n  [ERROR] Upload failed for {file_path.name}: {e}")
            if attempt < max_retries:
                backoff = 2 ** (attempt - 1)  # 1s, 2s, 4s
                print(f"  [RETRY] Waiting {backoff}s before retry...")
                time.sleep(backoff)
            else:
                print(f"  [FAILED] All {max_retries} attempts exhausted for {file_path.name}")
                return None

    return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    sort_only: bool = False,
    upload_only: bool = False,
    dry_run: bool = False,
) -> None:
    """Run the full audiobook pipeline."""
    print("=" * 60)
    print("  Audiobook Pipeline - Sort & Upload to Google Drive")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if dry_run:
        print("  MODE: DRY RUN (no changes will be made)")
    print("=" * 60)

    # -----------------------------------------------------------------------
    # Step 0: Purchase audit — are recent Audible purchases missing locally?
    # (Book sort breaks OpenAudible's own tracking; this diff is the real
    # signal. Report-only, never blocks the sync.)
    # -----------------------------------------------------------------------
    print("\n[STEP 0] Auditing recent purchases vs catalog...")
    try:
        from app.tools.audit_new_purchases import run_audit
        run_audit()
    except Exception as e:
        print(f"  [WARN] Purchase audit failed: {e}")

    # -----------------------------------------------------------------------
    # Step 1: Sort books from OpenAudible into author folders
    # -----------------------------------------------------------------------
    if not upload_only:
        print("\n[STEP 1] Sorting books from OpenAudible export...")
        moved = sort_books(dry_run=dry_run)
        print(f"  Sorted {len(moved)} file(s).")
        filed = sort_companion_files(dry_run=dry_run)
        if filed:
            print(f"  Filed {len(filed)} orphaned companion file(s).")
    else:
        print("\n[STEP 1] Skipped (--upload-only)")

    # -----------------------------------------------------------------------
    # Step 2: Detect new (un-uploaded) books
    # -----------------------------------------------------------------------
    print("\n[STEP 2] Detecting new books to upload...")
    manifest = load_manifest()
    new_files = detect_new_books(manifest)
    print(f"  Found {len(new_files)} new file(s) to upload.")

    if not new_files:
        print("\n  Nothing to upload. All books are synced!")
        print("=" * 60)
        return

    if sort_only:
        print("\n[STEP 3] Skipped (--sort-only)")
        print("=" * 60)
        return

    # -----------------------------------------------------------------------
    # Step 3: Catalog existing Drive folders
    # -----------------------------------------------------------------------
    print("\n[STEP 3] Reading existing Google Drive folders...")

    if not dry_run:
        from drive_auth import build_drive_service
        service = build_drive_service()
        if not service:
            print("  [ERROR] Failed to authenticate with Google Drive.")
            print("  Run this script interactively first to complete OAuth setup.")
            return

        # Try cache first, refresh if stale
        drive_folders = load_drive_folders_cache()
        if drive_folders is None:
            drive_folders = fetch_all_drive_folders(service)
            save_drive_folders_cache(drive_folders)
        else:
            print(f"  Using cached folder list ({len(drive_folders)} folders)")
    else:
        service = None
        drive_folders = {}
        print("  [DRY-RUN] Skipping Drive catalog")

    # -----------------------------------------------------------------------
    # Step 4: Upload files, resolving authors to Drive folders
    # -----------------------------------------------------------------------
    print(f"\n[STEP 4] Uploading {len(new_files)} file(s) to Google Drive...")

    from app.config import ROOT_DIR

    uploaded_count = 0
    skipped_count = 0
    failed_count = 0
    new_folders_created = []
    aliases = load_author_aliases()
    start_time = time.time()

    for i, file_path in enumerate(new_files, 1):
        rel = file_path.relative_to(ROOT_DIR)
        # Author is the top-level directory name in the library
        author_name = rel.parts[0] if len(rel.parts) > 1 else None

        if not author_name:
            print(f"\n  [{i}/{len(new_files)}] [SKIP] File not in author folder: {rel}")
            failed_count += 1
            continue

        # Resolve through alias map first
        canonical_author, folder_id_override = resolve_alias(author_name, aliases)

        print(f"\n  [{i}/{len(new_files)}] {rel}")

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
                    failed_count += 1
                    continue

        # Upload the file
        drive_file_id = upload_file_to_drive(
            service, file_path, folder_id, dry_run=dry_run
        )

        if drive_file_id:
            # Record in manifest
            rel_path_str = str(rel)
            manifest[rel_path_str] = {
                "uploaded_at": datetime.now().isoformat(),
                "drive_file_id": drive_file_id,
                "drive_folder": folder_name,
                "author": author_name,
            }
            if drive_file_id.startswith("dry-run"):
                uploaded_count += 1
            elif check_file_exists_on_drive and "SKIP" not in str(drive_file_id):
                uploaded_count += 1
            else:
                skipped_count += 1
        else:
            failed_count += 1

    # Save manifest
    if not dry_run:
        save_manifest(manifest)
        # Also update the Drive folders cache
        save_drive_folders_cache(drive_folders)

    # Summary
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print(f"  COMPLETE: {uploaded_count} uploaded, {skipped_count} skipped (already on Drive), {failed_count} failed")
    print(f"  Time: {elapsed:.1f}s")

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

    print("=" * 60)

    # -----------------------------------------------------------------------
    # Step 5: Rebuild catalog (so deploy can detect new books for Discord)
    # -----------------------------------------------------------------------
    if not dry_run and uploaded_count > 0:
        print("\n[STEP 5] Rebuilding catalog...")
        try:
            from app.main import main as catalog_main
            catalog_main()
            print("  Catalog rebuilt.")
        except Exception as e:
            print(f"  [WARN] Catalog rebuild failed: {e}")

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
        if new_books:
            print(f"\n[STEP 5.6] Content warnings for {len(new_books)} new book(s)...")
            try:
                from app.tools.fetch_content_warnings import check_new_books
                check_new_books(new_books)
            except Exception as e:
                print(f"  [WARN] Content-warning fetch failed: {e}")

        # Auto-commit and push if there are changes
        print("\n[STEP 6] Auto-commit & push...")
        _auto_commit_and_push()
    elif uploaded_count > 0:
        print("\n[STEP 5] Skipped catalog rebuild (dry-run)")

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

        # Stage site files
        subprocess.run(
            ["git", "add", "site/catalog.csv", "site/index.html", "site/covers/",
             "site/stats.html", "site/chapters.json", "site/content_warnings.json",
             "author_drive_map.json"],
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

        # Pull with rebase to avoid push failures when remote has diverged
        pull_result = subprocess.run(
            ["git", "pull", "--rebase"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if pull_result.returncode != 0:
            print(f"  [WARN] Pull --rebase failed: {pull_result.stderr.strip()}")
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
    args = parser.parse_args()

    if args.sort_only and args.upload_only:
        print("ERROR: Cannot use --sort-only and --upload-only together.")
        sys.exit(1)

    if args.refresh_cache and DRIVE_FOLDERS_CACHE_PATH.exists():
        DRIVE_FOLDERS_CACHE_PATH.unlink()
        print("Drive folder cache cleared.")

    run_pipeline(
        sort_only=args.sort_only,
        upload_only=args.upload_only,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
