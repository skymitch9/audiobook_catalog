"""
Keep the shelf's (Audiobookshelf) COLLECTIONS in step with the catalog's lists.

Owner, 2026-10-16: "can we dynamically create collections for people to match
tbr, already read and reviewed, and books marked in a book club" — and, asked
whether a person's lists should be private: "public is fine for now. It's
public on the audiobook site to anyone with access".

What it builds, per ABS library (Audio and Ebooks are separate libraries, and
an ABS collection belongs to exactly one):

    "<Name> · TBR"               readingLists rows with status 'tbr'
    "<Name> · Read & reviewed"   readingLists 'read' rows UNION that name's reviews
    "Book club · <Club>"         every read of the club, active or finished

⚠️ ABS collections are visible to EVERY shelf user with access to the library.
That is the owner's decision above — the same lists are world-readable on the
catalog site. If that ever changes, these become per-user PLAYLISTS instead,
which needs each person's own ABS account (OIDC accounts store no email, so
the uid ⇄ ABS-user join does not exist yet).

⚠️ IT ONLY EVER TOUCHES COLLECTIONS IT MADE. Ownership is the MARKER line in
the description. A hand-made collection — even one with an identical name — is
never edited or deleted. An owned collection whose list went empty or whose
person/club vanished is deleted, so the shelf does not keep stale lists.

People are grouped by DISPLAY NAME, not uid, because reviews carry no uid. Two
members sharing a display name would share collections; that is the same
residual the catalog's own "somebody else's list" view already has.

Sources are read through the public Firestore REST API (these collections are
world-readable by the rules, which is why no credential is needed). ⚠️ Nothing
here is ever written to Firestore, and the `email` field on readingLists rows
is never read into anything.

Usage (repo root):
    python scripts/sync_shelf_collections.py            # DRY RUN (default)
    python scripts/sync_shelf_collections.py --commit   # write to the shelf
    python scripts/sync_shelf_collections.py --commit --json-summary

Needs the same .env names as build_shelf_map.py (ABS_BASE_URL, ABS_USERNAME,
ABS_PASSWORD, ABS_LIBRARY_ID, ABS_EBOOK_LIBRARY_ID, ABS_CF_CLIENT_ID,
ABS_CF_CLIENT_SECRET).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

import build_shelf_map as shelf  # noqa: E402  (loads .env as a side effect)

FIRESTORE_BASE = (
    "https://firestore.googleapis.com/v1/projects/audiobook-catalog/databases/(default)/documents"
)
MARKER = "Auto-synced from the audiobook catalog. Edits here are overwritten."
SEP = " · "


# ---------------------------------------------------------------------------
# Firestore (read-only, public)
# ---------------------------------------------------------------------------
def _value(v: Dict):
    kind, x = next(iter(v.items()))
    if kind == "mapValue":
        return {k: _value(b) for k, b in (x.get("fields") or {}).items()}
    if kind == "arrayValue":
        return [_value(i) for i in (x.get("values") or [])]
    return x


def fs_list(path: str) -> List[Tuple[str, Dict]]:
    """Every document under `path` as (doc id, fields). Raises on failure:
    a half-read source must never be mistaken for an empty list, or the sync
    would delete collections that are really full."""
    out: List[Tuple[str, Dict]] = []
    token: Optional[str] = None
    while True:
        url = f"{FIRESTORE_BASE}/{path}?pageSize=300" + (f"&pageToken={token}" if token else "")
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.load(r)
        for d in data.get("documents", []):
            fields = {k: _value(v) for k, v in (d.get("fields") or {}).items()}
            out.append((d["name"].rsplit("/", 1)[-1], fields))
        token = data.get("nextPageToken")
        if not token:
            return out


def desired_lists(
    reading_lists: Iterable[Dict],
    reviews: Iterable[Dict],
    clubs: Iterable[Tuple[str, List[Dict]]],
) -> Dict[str, Set[str]]:
    """Collection name -> the set of catalog slugs it should hold. Pure."""
    want: Dict[str, Set[str]] = {}

    def add(name: str, slug: str) -> None:
        if slug:
            want.setdefault(name, set()).add(slug)

    for row in reading_lists:
        who = (row.get("displayName") or "").strip()
        slug = row.get("bookId") or shelf.book_id_from_title(row.get("bookTitle") or "")
        status = row.get("status")
        if not who:
            continue
        if status == "tbr":
            add(f"{who}{SEP}TBR", slug)
        elif status == "read":
            add(f"{who}{SEP}Read & reviewed", slug)
    for rv in reviews:
        who = (rv.get("displayName") or "").strip()
        if who:
            add(f"{who}{SEP}Read & reviewed", rv.get("bookId") or "")
    for club_name, reads in clubs:
        club_name = (club_name or "").strip()
        if not club_name:
            continue
        for rd in reads:
            add(f"Book club{SEP}{club_name}", shelf.book_id_from_title(rd.get("bookTitle") or ""))
    return want


# ---------------------------------------------------------------------------
# Slug -> ABS item ids, per library
# ---------------------------------------------------------------------------
def resolve_ids(
    catalog_rows: List[Dict[str, str]],
    audio_items: List[Dict],
    ebook_items: List[Dict],
) -> Dict[str, Dict[str, str]]:
    """slug -> {library id: item id}. The catalog join (exact + fuzzy) first,
    exactly as the site's shelf buttons use it; then an exact title-slug match
    straight against ABS for slugs the catalog CSV does not hold (an ebook-site
    TBR row, a club read of a book that is only on the shelf)."""
    claimed: Dict[str, List[str]] = {}
    shelf.build_books_block(catalog_rows, audio_items, ebook_items, ids=claimed)
    lib_of: Dict[str, str] = {}
    for it in audio_items:
        lib_of[it["id"]] = shelf.ABS_LIBRARY_ID
    for it in ebook_items:
        lib_of[it["id"]] = shelf.ABS_EBOOK_LIBRARY_ID
    out: Dict[str, Dict[str, str]] = {}
    for slug, item_ids in claimed.items():
        for iid in item_ids:
            lib = lib_of.get(iid)
            if lib:
                out.setdefault(slug, {}).setdefault(lib, iid)
    for items, lib in ((audio_items, shelf.ABS_LIBRARY_ID), (ebook_items, shelf.ABS_EBOOK_LIBRARY_ID)):
        groups: Dict[str, List[Dict]] = {}
        for it in items:
            s = shelf.book_id_from_title(shelf.abs_title_of(it))
            if s:
                groups.setdefault(s, []).append(it)
        for s, group in groups.items():
            if lib in out.get(s, {}):
                continue
            real = [i for i in group if not shelf.is_missing(i)]
            if real:
                out.setdefault(s, {})[lib] = real[0]["id"]
    return out


def plan(
    want: Dict[str, Set[str]],
    ids: Dict[str, Dict[str, str]],
    libraries: List[str],
    existing: Dict[Tuple[str, str], Dict],
) -> List[Dict]:
    """The actions to take. `existing` maps (library id, name) -> an OWNED
    collection {id, books:[item ids]}. Pure, so the tests can drive it."""
    actions: List[Dict] = []
    for lib in libraries:
        for name, slugs in sorted(want.items()):
            items = sorted({ids[s][lib] for s in slugs if lib in ids.get(s, {})})
            have = existing.get((lib, name))
            if not items:
                if have:
                    actions.append({"op": "delete", "lib": lib, "name": name, "id": have["id"]})
                continue
            if not have:
                actions.append({"op": "create", "lib": lib, "name": name, "books": items})
                continue
            cur = set(have.get("books") or [])
            add, rem = sorted(set(items) - cur), sorted(cur - set(items))
            if add or rem:
                actions.append({"op": "update", "lib": lib, "name": name, "id": have["id"],
                                "add": add, "remove": rem})
        for (elib, ename), have in sorted(existing.items()):
            if elib == lib and ename not in want:
                actions.append({"op": "delete", "lib": lib, "name": ename, "id": have["id"]})
    return actions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--commit", action="store_true", help="write to the shelf (default: dry run)")
    ap.add_argument("--json-summary", action="store_true")
    args = ap.parse_args(argv)

    reading = [f for _, f in fs_list("readingLists")]
    reviews = [f for _, f in fs_list("reviews")]
    clubs = [(f.get("name") or "", [r for _, r in fs_list(f"clubs/{cid}/reads")])
             for cid, f in fs_list("clubs")]
    want = desired_lists(reading, reviews, clubs)

    session = shelf.abs_session()
    session.headers["Authorization"] = f"Bearer {shelf.abs_login(session)}"
    base = shelf.ABS_BASE_URL.rstrip("/")
    libraries = [shelf.ABS_LIBRARY_ID] + ([shelf.ABS_EBOOK_LIBRARY_ID] if shelf.ABS_EBOOK_LIBRARY_ID else [])
    audio_items = shelf.abs_get_library_items(session, shelf.ABS_LIBRARY_ID)
    ebook_items = (shelf.abs_get_library_items(session, shelf.ABS_EBOOK_LIBRARY_ID)
                   if shelf.ABS_EBOOK_LIBRARY_ID else [])
    ids = resolve_ids(shelf.load_catalog_rows(), audio_items, ebook_items)

    existing: Dict[Tuple[str, str], Dict] = {}
    for lib in libraries:
        r = session.get(f"{base}/api/libraries/{lib}/collections", timeout=60)
        r.raise_for_status()
        for c in r.json().get("results", []):
            if MARKER in (c.get("description") or ""):
                existing[(lib, c["name"])] = {"id": c["id"],
                                              "books": [b["id"] for b in c.get("books", [])]}

    unresolved = sorted({s for slugs in want.values() for s in slugs if s not in ids})
    actions = plan(want, ids, libraries, existing)
    lib_label = {shelf.ABS_LIBRARY_ID: "Audio", shelf.ABS_EBOOK_LIBRARY_ID: "Ebooks"}
    print(f"{'COMMIT' if args.commit else 'DRY RUN'}: {len(want)} lists, "
          f"{len(existing)} owned collections on the shelf, {len(actions)} change(s), "
          f"{len(unresolved)} book(s) not on the shelf")
    for a in actions:
        detail = (f"{len(a['books'])} books" if a["op"] == "create"
                  else f"+{len(a['add'])} -{len(a['remove'])}" if a["op"] == "update" else "")
        print(f"  {a['op']:6} [{lib_label.get(a['lib'], a['lib'])}] {a['name']} {detail}")

    done = 0
    if args.commit:
        for a in actions:
            if a["op"] == "create":
                r = session.post(f"{base}/api/collections", json={
                    "libraryId": a["lib"], "name": a["name"], "description": MARKER,
                    "books": a["books"]}, timeout=60)
            elif a["op"] == "update":
                r = None
                if a["add"]:
                    r = session.post(f"{base}/api/collections/{a['id']}/batch/add",
                                     json={"books": a["add"]}, timeout=60)
                    r.raise_for_status()
                if a["remove"]:
                    r = session.post(f"{base}/api/collections/{a['id']}/batch/remove",
                                     json={"books": a["remove"]}, timeout=60)
            else:
                r = session.delete(f"{base}/api/collections/{a['id']}", timeout=60)
            if r is not None:
                r.raise_for_status()
            done += 1
        print(f"  applied {done}/{len(actions)}")

    if args.json_summary:
        print("SHELF_COLLECTIONS_JSON " + json.dumps({
            "commit": args.commit, "lists": len(want), "owned": len(existing),
            "changes": len(actions), "applied": done, "unresolved": len(unresolved)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
