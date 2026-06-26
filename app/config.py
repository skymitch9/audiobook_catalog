from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def _project_path_from_env(name: str, default: str) -> Path:
    """Resolve a path env var relative to the project root when it is not absolute."""
    raw = os.getenv(name, default)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _bool_from_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float_from_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _extensions_from_env() -> set[str]:
    raw = os.getenv("EXTS_CSV")
    if not raw:
        return {".m4b", ".m4a", ".mp4"}
    exts: set[str] = set()
    for part in raw.split(","):
        ext = part.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        exts.add(ext)
    return exts or {".m4b", ".m4a", ".mp4"}


OUTPUT_DIR: Path = _project_path_from_env("OUTPUT_DIR", "output_files")
SITE_DIR: Path = _project_path_from_env("SITE_DIR", "site")

ROOT_DIR_ENV = os.getenv("ROOT_DIR")
DEFAULT_LIBRARY_DIR = PROJECT_ROOT / "library"
ROOT_DIR: Path = Path(ROOT_DIR_ENV if ROOT_DIR_ENV else DEFAULT_LIBRARY_DIR).expanduser().resolve()

# Accept both names because .env.example historically used DRIVE_LINK.
DRIVE_FOLDER_URL: str | None = os.getenv("DRIVE_FOLDER_URL") or os.getenv("DRIVE_LINK") or None

EXTS: set[str] = _extensions_from_env()

SITE_INDEX_NAME: str = os.getenv("SITE_INDEX_NAME", "index.html")
SITE_CSV_NAME: str = os.getenv("SITE_CSV_NAME", "catalog.csv")

# Optional Hardcover.app enrichment. This runs at build time only, never in the browser.
HARDCOVER_ENABLED: bool = _bool_from_env("HARDCOVER_ENABLED", False)
HARDCOVER_TOKEN: str | None = os.getenv("HARDCOVER_TOKEN") or os.getenv("HARDCOVER_API_TOKEN") or None
HARDCOVER_API_URL: str = os.getenv("HARDCOVER_API_URL", "https://api.hardcover.app/v1/graphql")
HARDCOVER_CACHE_PATH: Path = _project_path_from_env("HARDCOVER_CACHE", ".cache/hardcover.json")
HARDCOVER_MIN_CONFIDENCE: float = _float_from_env("HARDCOVER_MIN_CONFIDENCE", 0.86)
HARDCOVER_TIMEOUT_SECONDS: float = _float_from_env("HARDCOVER_TIMEOUT_SECONDS", 30.0)
HARDCOVER_MAX_RESULTS: int = _int_from_env("HARDCOVER_MAX_RESULTS", 8)
