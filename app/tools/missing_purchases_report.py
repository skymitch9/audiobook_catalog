"""Daily FULL-library "missing purchases" report — report only, never downloads.

Why it exists (owner-approved 2026-11-23): the 15-minute purchase audit
(``purchase_audit`` -> ``auto_acquire``, default ``--top 50``) only looks at
the newest 50 purchases across all three Audible accounts. A burst of 50+ new
adds pushes every older un-downloaded title out of that window for good — two
titles went unfetched for weeks exactly that way, and nothing said so.

This module is the wide, slow, once-a-day look that the fast tick cannot
afford: the SAME matcher (``audit_new_purchases.missing_purchase_rows``) over
the whole library (``FULL_TOP``), the same licence-refused exclusions, and no
download path at all. ``purchase_audit`` runs ``collect`` in a bounded child
under the audible-cli interpreter once per calendar day and owns the state,
the report file and the "post only when the set changed" rule.

⚠️ A profile with NO rows is a failed report, not an empty account. If one
export fails, its missing titles would vanish from the set and come back the
next day — two false "the list changed" posts for one Audible hiccup. There is
deliberately no books.json fallback here either: a stale list is exactly what
this report exists to see past.

OWNED UNDER ANOTHER TITLE (owner order 2026-11-24: "do 1 but add checking the
drive manifest to verify we have it"). The title matcher cannot see that the
Audible purchase "Percy Jackson: The Demigod Files" IS the catalogue's "Percy
Jackson & the Olympians - The Demigod Files", so such titles sat on this list
for good. A title-unmatched purchase is now dropped ONLY when we can VERIFY we
hold it, and both routes end at the same proof — the file is on Drive (its
library-relative path is an audiobook entry with a Drive id in
``upload_manifest.json``, the record ``scripts/sync_to_drive.py`` keeps) AND it
is on local disk under ``ROOT_DIR``:

  1. ``scripts/owned_as.json`` — a hand-kept ASIN -> file map, each entry with
     its evidence. Checked FIRST. A mapping that fails the proof never
     suppresses the title: it stays listed with a ``note`` naming why.
  2. the ASIN tag — a library file whose own ``CDEK`` atom equals the
     purchase's ASIN. Only files the manifest names are read (tags only, about
     1 s for ~1,100 files warm, measured 2026-11-24), never written.

Every dropped title is kept in the report's ``verified_owned`` list with how it
was verified, so a suppression is always visible. ⚠️ An unreadable manifest or
a missing library root FAILS the report rather than listing everything: listing
four titles one day and dropping them the next is two false "the list changed"
posts — the same flap the profile rule above exists to prevent.

Usage:
    python -m app.tools.missing_purchases_report   # print the full list (contacts Audible)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.tools import audit_new_purchases as anp

# Larger than any household library will plausibly be (1,077 purchases across
# the three profiles, measured 2026-10-20). `collect` says when it was not.
FULL_TOP = 5000

SCRIPTS_DIR = anp.PROJECT_ROOT / "scripts"
OWNED_AS_PATH = SCRIPTS_DIR / "owned_as.json"
# Same set as scripts/sync_to_drive.py AUDIOBOOK_EXTS. The manifest also holds
# companion PDFs/EPUBs (the Dragoneye books have both), and a companion on
# Drive is NOT the audiobook — it must never count as owning one.
AUDIOBOOK_EXTS = (".m4b", ".m4a", ".mp4")


def manifest_path() -> Path:
    """Where ``scripts/sync_to_drive.py`` keeps ``upload_manifest.json``,
    derived the way it and ``scripts/health_check.py`` derive it. Not imported
    from there: that module loads .env, the status shim and more on import."""
    return Path(os.getenv("SYNC_DATA_DIR", str(SCRIPTS_DIR))) / "upload_manifest.json"


def path_key(rel: str) -> str:
    """Manifest keys are ``str(Path.relative_to(root))`` — backslashes on this
    box. Compare separator- and case-blind (Windows paths are)."""
    return str(rel).replace("\\", "/").strip("/").casefold()


def manifest_audiobooks(manifest: dict) -> dict[str, str]:
    """{path_key: manifest key} for every AUDIOBOOK entry that records a Drive
    file id — the manifest only records an upload that returned one."""
    out = {}
    for key, rec in manifest.items():
        if (str(key).lower().endswith(AUDIOBOOK_EXTS) and isinstance(rec, dict)
                and rec.get("drive_file_id")):
            out[path_key(key)] = str(key)
    return out


@dataclass
class Ownership:
    """What the verification may look at. Pure data, so ``summarise`` stays
    pure and tests never touch the real library.

    on_drive  — manifest_audiobooks() of the upload manifest
    owned_as  — scripts/owned_as.json minus its ``_`` keys: {asin: {file, why}}
    asin_index— {asin: manifest key} from each on-Drive file's own CDEK tag
    on_disk   — is this manifest key a file under ROOT_DIR right now?
    """
    on_drive: dict[str, str]
    owned_as: dict[str, dict] = field(default_factory=dict)
    asin_index: dict[str, str] = field(default_factory=dict)
    on_disk: Callable[[str], bool] = field(default=lambda key: False)
    index_note: str = ""

    def verify(self, row: dict) -> tuple[dict | None, str | None]:
        """(proof, None) when we verifiably hold this purchase, else
        (None, note). ``note`` is None for a plain gap and a reason when a
        mapping exists but failed its proof."""
        asin = row.get("asin") or ""
        if not asin:
            return None, None
        tagged = self.asin_index.get(asin)
        entry = self.owned_as.get(asin)
        if entry is not None:
            rel = str(entry.get("file") or "")
            key = self.on_drive.get(path_key(rel)) if rel else None
            if not rel:
                note = "owned_as entry has no file"
            elif key is None:
                note = f"mapped to {rel} but it is not on Drive (not an audiobook in the upload manifest)"
            elif not self.on_disk(key):
                note = f"mapped to {rel} but it is not on local disk"
            else:
                return {"how": "owned_as", "file": key, "why": entry.get("why", "")}, None
            # ⚠️ A failed mapping is never rescued silently by the tag index:
            # it says the mapping is stale, so the owner fixes the map.
            if tagged:
                note += f"; the ASIN tag points at {tagged} — fix scripts/owned_as.json"
            return None, note
        if tagged:
            # asin_index is built only from on-Drive files; disk is rechecked.
            if self.on_disk(tagged):
                return {"how": "asin_tag", "file": tagged}, None
        return None, None


def summarise(books: list[dict], catalog_titles, skip_asins, top: int = FULL_TOP,
              profiles=anp.AUDIBLE_PROFILES, ownership: Ownership | None = None) -> dict:
    """Pure: the report for these rows. JSON-only, so it crosses the child
    boundary unchanged. ``ownership=None`` skips verification (title match
    only — the behaviour before 2026-11-24)."""
    seen = {b.get("profile") for b in books}
    absent = [p for p in profiles if p not in seen]
    if absent:
        return {"ok": False,
                "error": f"no library rows exported for profile(s) {', '.join(absent)} — "
                         "not reporting a partial list"}
    rows = anp.missing_purchase_rows(books, catalog_titles, top, skip_asins=skip_asins)
    checked = len(anp.newest_purchases(books, top))
    purchases = len(anp.newest_purchases(books, len(books)))

    def _item(b):
        return {"date": anp.purchase_date(b), "title": b.get("title_short") or b.get("title") or "",
                "asin": b.get("asin") or "", "profile": b.get("profile") or ""}

    items, verified = [], []
    for b in rows:
        proof, note = ownership.verify(b) if ownership is not None else (None, None)
        if proof is not None:
            verified.append({**_item(b), **proof})
            continue
        it = _item(b)
        if note:
            it["note"] = note
        items.append(it)
    order = lambda i: (i["date"], i["title"])  # noqa: E731
    report = {"ok": True, "checked": checked, "covered_all": purchases <= top,
              "items": sorted(items, key=order, reverse=True)}
    if ownership is not None:
        report["verified_owned"] = sorted(verified, key=order, reverse=True)
        if ownership.index_note:
            report["asin_index"] = ownership.index_note
    return report


def load_owned_as(path: Path = OWNED_AS_PATH) -> dict[str, dict]:
    """scripts/owned_as.json without its ``_`` documentation keys. A missing
    file is an empty map; a malformed one raises (the report fails, it does
    not quietly stop verifying)."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def build_asin_index(root: Path, on_drive: dict[str, str]) -> tuple[dict[str, str], str]:
    """{asin: manifest key} from the CDEK tag of each on-Drive audiobook that
    is on disk. Read-only (mutagen opens for reading). Returns (index, note);
    without mutagen the index is empty and the note says so — the owned_as
    route still works, it needs only the manifest and the disk."""
    try:
        from mutagen.mp4 import MP4
        from app.metadata import K_ASIN, get_tag_any
    except Exception as e:
        return {}, f"ASIN tag index unavailable ({type(e).__name__}: {e}); owned_as only"
    index: dict[str, str] = {}
    read = unreadable = 0
    for key in sorted(on_drive.values()):
        p = root / key.replace("\\", os.sep).replace("/", os.sep)
        if not p.is_file():
            continue
        try:
            asin = get_tag_any(MP4(str(p)).tags or {}, [K_ASIN])
        except Exception:
            unreadable += 1
            continue
        read += 1
        if asin:
            index.setdefault(asin.strip(), key)
    return index, (f"ASIN tag index: {len(index)} ASINs from {read} files"
                   + (f", {unreadable} unreadable" if unreadable else ""))


def load_ownership(root: Path, manifest: Path | None = None,
                   owned_as: Path = OWNED_AS_PATH) -> Ownership:
    """Everything ``Ownership.verify`` needs, from disk. Raises when the proof
    itself is unavailable (no manifest, unreadable manifest, no library root)
    — the caller fails the report rather than listing every owned title."""
    manifest = manifest or manifest_path()
    if not root.is_dir():
        raise FileNotFoundError(f"library root {root} not found — cannot verify ownership")
    if not manifest.exists():
        raise FileNotFoundError(f"upload manifest {manifest} not found — cannot verify ownership")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"upload manifest {manifest} is not a JSON object")
    on_drive = manifest_audiobooks(data)
    index, note = build_asin_index(root, on_drive)

    def on_disk(key: str) -> bool:
        return (root / key.replace("\\", os.sep).replace("/", os.sep)).is_file()

    return Ownership(on_drive=on_drive, owned_as=load_owned_as(owned_as),
                     asin_index=index, on_disk=on_disk, index_note=note)


def collect(top: int = FULL_TOP) -> dict:
    """Fetch fresh exports for every profile and build the report. Contacts
    Audible (read-only library export); downloads nothing; reads the upload
    manifest and the library's tags read-only. Never raises."""
    try:
        from app.config import ROOT_DIR
        from app.tools.license_refused import load as load_refused
        books = anp.audible_cli_books()
        try:
            ownership = load_ownership(Path(ROOT_DIR))
        except Exception as e:
            return {"ok": False, "error": f"ownership check unavailable: {type(e).__name__}: {e}"}
        return summarise(books, anp.load_catalog_titles(), load_refused(), top,
                         ownership=ownership)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def asin_key(item: dict) -> str:
    """The identity used for "did the set change". A row with no ASIN (never
    seen in practice from audible-cli) falls back to its title."""
    return item.get("asin") or f"title:{item.get('title', '')}"


def discord_lines(items: list[dict], new_keys: set[str], limit_chars: int = 1700) -> list[str]:
    """One line per title, newest first, NEW ones marked, capped well under
    Discord's 2,000-character message limit."""
    lines: list[str] = []
    used = 0
    for i, it in enumerate(items):
        mark = "🆕 " if asin_key(it) in new_keys else ""
        line = f"- {mark}{it['date']} | {it['title']} ({it['profile']}, {it['asin']})"
        if it.get("note"):
            line += f" — {it['note']}"
        if used + len(line) > limit_chars:
            lines.append(f"- … +{len(items) - i} more in output_files/missing_purchases_report.json")
            break
        lines.append(line)
        used += len(line) + 1
    return lines


def main() -> int:
    report = collect()
    if not report.get("ok"):
        print(f"FAILED — {report.get('error')}")
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["items"] else 0


if __name__ == "__main__":
    sys.exit(main())
