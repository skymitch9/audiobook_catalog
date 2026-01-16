# app/extractors/tags.py
from typing import Dict, List, Optional

from mutagen.mp4 import MP4FreeForm

from app.core.people import bytes_to_str, first_str


def get_tag_any(tags: Dict, keys: List[str]) -> Optional[str]:
    for k in keys:
        if k in tags and tags[k]:
            s = first_str(tags[k])
            if s:
                return s
    return None


def get_freeform_by_suffix(tags: Dict, suffixes: List[str]) -> Optional[str]:
    for key, val in (tags or {}).items():
        if not isinstance(key, str) or not key.startswith("----"):
            continue
        tail = key.split(":")[-1].lower()
        if any(tail.endswith(sfx.lower()) for sfx in suffixes):
            if isinstance(val, list) and val:
                parts = []
                for piece in val:
                    if isinstance(piece, MP4FreeForm):
                        parts.append(bytes_to_str(bytes(piece)))
                    elif isinstance(piece, (bytes, bytearray)):
                        parts.append(bytes_to_str(piece))
                    else:
                        parts.append(str(piece))
                joined = ", ".join(p for p in parts if p)
                if joined.strip():
                    return joined.strip()
    return None
