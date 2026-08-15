# app/tools/sync_series_canon.py
#
# Merge the estate series canon (catalog-platform/data/series-canon.json) into
# this catalog's OWN corrections layer (scripts/catalog_overrides.json
# canonical_series), so a cross-catalog series-spelling fold decided once in
# the shared canon reaches this catalog too. Normalization item 4.
#
#   python -m app.tools.sync_series_canon              # dry run, prints the diff
#   python -m app.tools.sync_series_canon --commit      # writes the corrections file
#
# ## Why a sync tool and not a live read
#
# app/core/universes.py reads catalog-platform LIVE, at every build, because a
# universe classification has no other home - the pipeline consults it and
# nothing downstream needs to know it happened. Series spelling is different:
# this catalog already owns a canonical_series layer
# (app/core/catalog_overrides.py, scripts/catalog_overrides.json) that every
# build already reads, and a second live cross-repo dependency doing the same
# job would just be two paths to one answer. So this tool runs at a
# well-defined moment, by hand - the same way `python -m app.tools.edit_overrides
# canon` is run by hand - and writes its answer into the file the build already
# trusts. After a run, this repo needs nothing from catalog-platform until the
# canon changes and the tool is re-run.
#
# ## What it does, and does not, touch
#
# Additive only, and idempotent. For every {canonical, variants} entry in
# series-canon.json, every variant spelling (including the canonical one, so a
# later re-spelling of the canonical form cannot orphan books already spelled
# correctly - the same self-mapping rule overrides_store.set_canonical_series
# already applies by hand) is folded onto the canonical spelling via
# app.core.overrides_store.set_canonical_series - the EXACT function
# `app.tools.edit_overrides canon` calls. It never deletes an existing
# canonical_series entry, even one the estate canon does not know about:
# set_canonical_series only ever sets the keys it is given, so a local-only
# fold (Lion's Quest, Otherlife, the four Completionist Chronicles filename
# spellings) survives a sync untouched. Running it twice in a row reports
# nothing new the second time.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core import overrides_store as store
from app.core.overrides_store import OverridesError
from app.core.universes import find_platform_dir


def _norm(s: Optional[str]) -> str:
    """Same fold app.core.catalog_overrides / overrides_store use for canonical_series keys."""
    import re

    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def load_canon(platform_dir: Path) -> Dict[str, Any]:
    path = platform_dir / "data" / "series-canon.json"
    if not path.is_file():
        raise OverridesError(f"{path} does not exist. Has catalog-platform's series canon been built yet?")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def plan(canon_doc: Dict[str, Any], data: Dict[str, Any]) -> List[Tuple[str, str, bool]]:
    """
    Every (variant, canonical, is_new) triple the estate canon wants folded.

    `is_new` is False when scripts/catalog_overrides.json already maps that
    variant to that exact canonical spelling - the idempotency check.
    """
    existing = data.get("canonical_series", {})
    out: List[Tuple[str, str, bool]] = []
    seen_keys: set[str] = set()
    for entry in canon_doc.get("entries", []):
        canonical = entry.get("canonical")
        if not canonical or not isinstance(canonical, str):
            continue
        variants = entry.get("variants") or []
        for variant in [*variants, canonical]:
            if not isinstance(variant, str) or not variant.strip():
                continue
            key = _norm(variant)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            is_new = existing.get(key) != canonical
            out.append((variant, canonical, is_new))
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true", help="write scripts/catalog_overrides.json (default: dry run)")
    p.add_argument("--overrides", type=Path, default=store.OVERRIDES_PATH, help=argparse.SUPPRESS)
    p.add_argument("--platform-dir", type=Path, default=None, help=argparse.SUPPRESS)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.platform_dir is not None:
        platform_dir = args.platform_dir
    else:
        platform_dir, tried = find_platform_dir()
        if platform_dir is None:
            print(
                "Cannot find the catalog-platform checkout, so there is nothing to sync.\n"
                f"Tried: {'; '.join(tried)}\n"
                "Fix: clone catalog-platform beside this repo, or set CATALOG_PLATFORM_DIR.",
                file=sys.stderr,
            )
            return 1

    try:
        canon_doc = load_canon(platform_dir)
    except OverridesError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        data = store.load(args.overrides)
    except OverridesError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    changes = plan(canon_doc, data)
    new = [c for c in changes if c[2]]

    canon_path = platform_dir / "data" / "series-canon.json"
    if not new:
        print(f"canonical_series already reflects all {len(changes)} estate-canon fold(s) from {canon_path}. Nothing to do.")
        return 0

    print(f"{len(new)} new fold(s) of {len(changes)} total, from the estate canon ({canon_path}):")
    for variant, canonical, _ in new:
        print(f"  {variant!r} -> {canonical!r}")

    if not args.commit:
        print("\nDRY RUN. Nothing written. Re-run with --commit.")
        return 0

    for variant, canonical, _ in changes:
        store.set_canonical_series(data, variant, canonical)
    store.save(data, args.overrides)
    print(f"\nWrote {args.overrides}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
