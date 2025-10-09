# app/extractors/covers.py
from typing import Optional
from pathlib import Path
from mutagen.mp4 import MP4, MP4Cover
from app.config import ROOT_DIR, OUTPUT_DIR

def save_cover_for_file(path: Path) -> Optional[str]:
    """
    Extract first cover from 'covr' atom and write it under:
      OUTPUT_DIR / "covers" / <relative-to-ROOT_DIR parent> / <stem>.<ext>
    Returns a site-relative href like:
      "covers/<relative-path>/<filename>.jpg"
    or None if no cover found.
    """
    try:
        audio = MP4(str(path))
        tags = audio.tags or {}
        covrs = tags.get("covr")
        if not covrs:
            return None

        cover = covrs[0]
        ext = ".jpg"
        try:
            if isinstance(cover, MP4Cover):
                if cover.imageformat == MP4Cover.FORMAT_PNG:   ext = ".png"
                elif cover.imageformat == MP4Cover.FORMAT_JPEG: ext = ".jpg"
        except Exception:
            pass

        rel = path.relative_to(ROOT_DIR)
        out_dir = OUTPUT_DIR / "covers" / rel.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / (path.stem + ext)

        data = bytes(cover) if isinstance(cover, (MP4Cover, bytes, bytearray)) else bytes(cover)
        out_path.write_bytes(data)

        return str(Path("covers") / rel.parent / (path.stem + ext)).replace("\\", "/")
    except Exception:
        return None
