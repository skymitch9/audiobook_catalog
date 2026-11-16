"""
Remember purchases Audible will no longer license, so they are not re-downloaded.

Some titles stay in an account's library list after Audible stops granting a
download licence for them (withdrawn titles, expired plan catalogue items). A
download then fails every time with "License not granted to customer", and
because the title is still "purchased but not in the catalogue" the purchase
audit retried it on every tick, forever, counting an error each time.

The auto-downloader records such a title here the first time it is refused;
the purchase audit then leaves it out of the missing list. Nothing is
hard-coded: the store is filled by the refusal itself.

Usage:
    python -m app.tools.license_refused --list           # what is being skipped
    python -m app.tools.license_refused --forget <ASIN>  # try that title again
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import OUTPUT_DIR

STORE: Path = OUTPUT_DIR / "license_refused.json"

# audible-cli's wording when the licence request is denied.
REFUSAL_MARKER = "License not granted to customer"


def is_license_refusal(error) -> bool:
    return REFUSAL_MARKER.lower() in str(error or "").lower()


def load(path=None) -> dict:
    """{asin: {title, profile, first_refused, error}}; {} when absent/unreadable."""
    try:
        data = json.loads(Path(path or STORE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data, path=None):
    p = Path(path or STORE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def record(asin, title, profile=None, error="", path=None) -> bool:
    """Remember a refused title. Returns True when it was newly recorded."""
    if not asin:
        return False
    data = load(path)
    if asin in data:
        return False
    data[asin] = {
        "title": title or "",
        "profile": profile or "",
        "first_refused": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": str(error or "")[-300:],
    }
    _save(data, path)
    return True


def forget(asin, path=None) -> bool:
    """Drop a title so the next audit tries it again. True when it was there."""
    data = load(path)
    if asin not in data:
        return False
    del data[asin]
    _save(data, path)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--list", action="store_true", help="show the titles being skipped")
    parser.add_argument("--forget", metavar="ASIN", help="remove a title so it is retried")
    args = parser.parse_args()
    if args.forget:
        ok = forget(args.forget)
        print(f"{args.forget}: {'removed - it will be retried' if ok else 'was not in the list'}")
        return 0 if ok else 1
    data = load()
    print(f"{len(data)} title(s) Audible refuses to license (not retried): {STORE}")
    for asin, e in sorted(data.items(), key=lambda kv: kv[1].get("first_refused", "")):
        print(f"  {asin} | {e.get('profile', '')} | {e.get('first_refused', '')[:10]} | {e.get('title', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
