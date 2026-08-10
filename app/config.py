from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
OUTPUT_DIR: Path = PROJECT_ROOT / "output_files"
SITE_DIR: Path = PROJECT_ROOT / "site"

ROOT_DIR_ENV = os.getenv("ROOT_DIR")
DEFAULT_LIBRARY_DIR = PROJECT_ROOT / "library"
ROOT_DIR: Path = Path(ROOT_DIR_ENV if ROOT_DIR_ENV else DEFAULT_LIBRARY_DIR).expanduser().resolve()

DRIVE_FOLDER_URL: str | None = os.getenv("DRIVE_FOLDER_URL") or None

EXTS: set[str] = {".m4b", ".m4a", ".mp4"}

SITE_INDEX_NAME: str = "index.html"
SITE_CSV_NAME: str = "catalog.csv"

# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------
# Covers are NOT committed to git. They are uploaded to Cloudflare R2 by
# scripts/upload_covers_r2.py and served from there; site/covers/ is a local
# build product (gitignored) and site/covers_manifest.json is the committed
# record of what is in the bucket.
#
# THE ONE KNOB. Everything that emits a cover URL resolves it through this:
#   - app/web/html_builder.py       (site/index.html)
#   - app/writers.py                (generates site/covers-base.js for the
#                                    browser-side consumers)
#   - app/tools/send_discord_notification.py  (embed thumbnails)
#
# catalog.csv's `cover_href` stays RELATIVE ("covers/<author>/<title>.jpg") —
# it is the canonical key, and the R2 object key is that path minus the
# leading "covers/". So swapping the r2.dev URL below for a custom domain
# (https://covers.heygabi.ai/) is a one-line change and nothing else moves.
#
# Set COVERS_BASE_URL="covers/" to restore the old fully-relative behaviour
# (useful for a purely local preview with site/covers present).
COVERS_BASE_URL: str = (
    os.getenv("COVERS_BASE_URL")
    or "https://pub-7ab0a1938250448aa329ca218db15a68.r2.dev/"
).strip()

COVERS_R2_BUCKET: str = os.getenv("COVERS_R2_BUCKET") or "audiobook-covers"
COVERS_MANIFEST_NAME: str = "covers_manifest.json"
