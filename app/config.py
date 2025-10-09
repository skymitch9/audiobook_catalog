# app/config.py

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(os.getenv("ROOT_DIR", "")).expanduser()

EXTS = {".m4b", ".m4a", ".mp4"}
extra_exts = os.getenv("EXTS_CSV", "")
if extra_exts:
    EXTS |= {ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
             for ext in extra_exts.split(",") if ext.strip()}

SITE_DIR = Path(os.getenv("SITE_DIR", "site"))
SITE_INDEX_NAME = os.getenv("SITE_INDEX_NAME", "index.html")
SITE_CSV_NAME = os.getenv("SITE_CSV_NAME", "catalog.csv")

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output_files"))

# NEW: optional Google Drive link for the header
DRIVE_LINK = os.getenv("DRIVE_LINK", "").strip() or None

if not ROOT_DIR:
    raise RuntimeError("ROOT_DIR is not set. Create a .env file (see .env.example).")
