# app/core/people.py
from typing import Optional, Any
import re

def bytes_to_str(b: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return b.decode(enc).strip()
        except Exception:
            pass
    return b.decode("utf-8", errors="ignore").strip()

def first_str(val: Any) -> Optional[str]:
    v = val[0] if isinstance(val, list) and val else val
    if isinstance(v, bytes): return bytes_to_str(v)
    if isinstance(v, tuple): return str(v[0])
    return (str(v).strip()) if v is not None else None

def normalize_people_field(s: Optional[str]) -> Optional[str]:
    if not s: return None
    parts = re.split(r"[;,/&]| and ", s, flags=re.IGNORECASE)
    cleaned, seen = [], set()
    for p in parts:
        name = re.sub(r"\s+", " ", p).strip()
        if not name: continue
        norm = name if (name.isupper() and len(name) <= 5) else " ".join(w.capitalize() for w in name.split())
        key = norm.lower()
        if key not in seen:
            seen.add(key); cleaned.append(norm)
    return ", ".join(cleaned) if cleaned else None

# Alias for test compatibility
normalize_people_list = normalize_people_field
