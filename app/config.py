import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# Library root (where your audiobooks live)
ROOT_DIR = Path(os.getenv("ROOT_DIR", "")).expanduser()

# Allowed file extensions
EXTS = {".m4b", ".m4a", ".mp4"}
extra_exts = os.getenv("EXTS_CSV", "")
if extra_exts:
    EXTS |= {ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
             for ext in extra_exts.split(",") if ext.strip()}

# GitHub Pages output directory & filenames
SITE_DIR = Path(os.getenv("SITE_DIR", "site"))
SITE_INDEX_NAME = os.getenv("SITE_INDEX_NAME", "index.html")
SITE_CSV_NAME = os.getenv("SITE_CSV_NAME", "catalog.csv")

# NEW: local folder for timestamped CSVs
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output_files"))

# Safety check
if not ROOT_DIR:
    raise RuntimeError("ROOT_DIR is not set. Create a .env file (see .env.example).")
