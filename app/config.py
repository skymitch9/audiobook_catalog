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

# ---------------------------------------------------------------------------
# ROOT_DIR — the AUDIO library, and the ONE derivation of it (F6, 2026-09-07)
# ---------------------------------------------------------------------------
# ⚠️ THIS LINE MOVED A DEFAULT. Until 2026-09-07 the unset-`ROOT_DIR` default
# here was `PROJECT_ROOT / "library"`, while `scripts/sync_to_drive.py` (and
# seven other scripts) defaulted to the OpenAudible export path below. So on a
# machine that does not set `ROOT_DIR` the sorter's source directory and the
# catalogue's library root pointed at two ENTIRELY DIFFERENT folders,
# unconditionally — not "identical unless resolve() diverges", which is what
# the 2026-08-24 sanctity audit recorded. That machine is the RECOVERY machine
# (`docs/access/RECOVERY.md` rebuilds from git + blobs, and a fresh clone has
# no `.env`), which is the worst possible day to find out.
#
# The OpenAudible path won because it is what the OTHER EIGHT derivations
# already say, so this makes them agree instead of leaving one odd. Nothing on
# this box moves: `ROOT_DIR` is set here, and the two compared equal (measured
# 2026-08-26, re-measured 2026-09-07). Nothing on any other box moves either —
# `PROJECT_ROOT/"library"` does not exist in this repo, nothing creates it and
# nothing else referenced it, so every consumer already bailed with
# "ROOT_DIR not found" and now bails on a different missing path.
#
# ⚠️ It is one person's user profile as a literal, and that is deliberate
# rather than tidy: the alternative (the repo-local path) is the value that was
# WRONG for eight of nine callers. Any machine that is not this one is expected
# to set `ROOT_DIR`; the default only decides which error it prints.
#
# ⚠️ Do NOT "fix" `filed_author_folder`'s `os.path.relpath` on the strength of
# this merge (`scripts/sync_to_drive.py`). That guards the GENERAL case — a
# free-text `$ROOT_DIR` carrying a `..`, a `~` or a relative segment — and this
# only merges one pair of constants. Removing it re-opens F5: `relative_to`
# raises on every file, every filed book reads as a new arrival, and the sorter
# relocates the whole library.
ROOT_DIR_ENV = os.getenv("ROOT_DIR")
DEFAULT_LIBRARY_DIR = Path(r"C:\Users\nbasl\OpenAudible\books")
ROOT_DIR: Path = Path(ROOT_DIR_ENV if ROOT_DIR_ENV else DEFAULT_LIBRARY_DIR).expanduser().resolve()

# ---------------------------------------------------------------------------
# The SIBLING library catalogue (bookbuddy/library_catalog)
# ---------------------------------------------------------------------------
# ⚠️ NOT the same thing as ROOT_DIR above. ROOT_DIR is the AUDIO library — the
# folder of .m4b files this machine sorts and uploads. This is the checkout of
# the *print/ebook* catalogue repo that lives beside this one, and the only
# reason this repo needs to know where it is: sync_to_drive.py's STEP 11 shells
# out to that repo's scripts/backfill-audiobook-holdings.mjs to link the two
# catalogues (see that step's header for why it is a script and not a route).
#
# There is no discovery here on purpose. A machine that does not have the
# sibling checked out must be TOLD apart from one that ran the sweep and found
# nothing, so STEP 11 turns a missing directory into a named skip rather than
# hunting for a plausible path.
LIBRARY_CATALOG_DIR_ENV = os.getenv("LIBRARY_CATALOG_DIR")
DEFAULT_LIBRARY_CATALOG_DIR = PROJECT_ROOT.parent / "library_catalog"
LIBRARY_CATALOG_DIR: Path = Path(
    LIBRARY_CATALOG_DIR_ENV if LIBRARY_CATALOG_DIR_ENV else DEFAULT_LIBRARY_CATALOG_DIR
).expanduser()

# ⚠️ THERE ARE TWO LIBRARY INSTANCES, AND STEP 11 MUST SWEEP BOTH.
#
# `library_catalog` deploys twice: MAIN (library.heygabi.ai) and FRIEND —
# padhard.heygabi.ai, on its own D1 (`library-catalog-2nd`). They are separate
# databases, so the sibling-link sweep run against one leaves the other exactly
# as stale as it was.
#
# The owner and padhard SHARE ONE AUDIO POOL (owner decision 2026-08-25: *"her
# and I share audio and ebooks … they're already pre-mixed with mine; they
# should count as she owns them too"*), which is what makes sweeping her
# instance correct rather than presumptuous — the same audiobooks really are
# hers. Her machine-route tokens were set by hand the same day; before that her
# routes were off and there was nothing here to target.
#
# Measured 2026-08-25: her 101 audio links existed only because somebody ran the
# sweep with `--friend` BY HAND. That is the same "freshness depends on somebody
# remembering" failure STEP 11 was built to end, one instance over.
#
# The switch exists so this can be turned off WITHOUT A CODE CHANGE — a machine
# that should only ever touch the main catalogue (a fresh checkout, a test box,
# or the day padhard is retired) sets `SIBLING_LINK_FRIEND=0`. Default ON.
# A friend failure never fails the cycle: STEP 11 reports it as a named partial
# and main's result still stands.
SIBLING_LINK_FRIEND: bool = (os.getenv("SIBLING_LINK_FRIEND") or "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

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
# Switched off the r2.dev URL on 2026-08-10. That was always the interim, and
# Cloudflare's own notice on it is the reason: the public development URL is
# **rate-limited, and Cache and Access are unavailable on it**. A custom domain
# puts covers inside the heygabi.ai zone, so they are cached at the edge and
# honour the `public, max-age=604800` that upload_covers_r2.py already sets —
# the zone's Browser Cache TTL is "Respect Existing Headers" (see
# docs/info/caching.md), so that header is obeyed rather than overridden.
#
# The r2.dev URL still works and is the fallback: set COVERS_BASE_URL in the
# environment to move everything back without touching code.
COVERS_BASE_URL: str = (
    os.getenv("COVERS_BASE_URL")
    or "https://covers.heygabi.ai/"
).strip()

COVERS_R2_BUCKET: str = os.getenv("COVERS_R2_BUCKET") or "audiobook-covers"
COVERS_MANIFEST_NAME: str = "covers_manifest.json"
