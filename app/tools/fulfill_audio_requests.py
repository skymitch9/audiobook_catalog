"""Fulfil ON-DEMAND audiobook ingest requests, and evict what nobody uses.

AUDIO PLAYER PHASE 0b — the pipeline half of the queue. The other half (the
site's "request this book" button) is a later phase; this ships the collection
contract, the fulfiller and the eviction machinery so the local side is
complete and exercised before anything can press a button.

WHY A QUEUE AT ALL  (audio-player-design.md §12 decision 3)
------------------------------------------------------------
Owner: *"upon clicking the download button it adds it to a queue to be
downloaded for everyone. so each book is on request then ready for everyone.
that should spread it out a lot if there are plenty of books we've already
read that most likely wont get read again for a while."*

The library is 630 GB / 1,073 files. Uploading it whole is days of uplink and
~$9.45/mo for books nobody asked for. On demand, the bill starts near zero and
grows one requested book (~$0.009/mo) at a time.

⚠️ THE SPLIT THAT SHAPES THIS FILE: requests are MADE on the site (a browser,
then a Worker) and can only be FULFILLED here, because this machine is the
only one holding the 630 GB. So the queue must live somewhere both halves can
reach. It is Firestore — exactly the seam `cw_requests` already uses for the
site's "Request AI check" button, read by `fetch_content_warnings.py
--requests`. That flow is the model this one copies, gotchas included.

THE COLLECTION CONTRACT
-----------------------
    audio_requests/{bookId}          (prod)      ⚠️ one doc per BOOK
    audio_requests_dev/{bookId}      (/dev/ lane)

    bookId       string  == the document id == book_id_from_title(title),
                          the estate's existing book identity fold (the one
                          site/reviews.js and readingPositions already use).
                          ⚠️ NOT the anchor: the site knows a book's TITLE,
                          not its path on this machine, so it cannot compute
                          an anchor. The anchor is derived here at upload.
    bookTitle    string  the catalog/m4b title, for humans and for resolution
    requesters   array<string>  requester uids, ARRAY-UNIONED
    requestedBy  string  the first requester's uid (mirrors validCwRequest)
    status       string  'pending'
    createdAt    number  ms
    updatedAt    number  ms

⚠️ **THE DUPLICATE CLAUSE IS THE DOC ID.** Owner: *"my book club reads a lot
of the same books so 3 of us might request the same book … add a duplicate
clause."* Keying the document on the book makes a second request an UPDATE
that array-unions one uid, not a second document — so a book-club pile can
never upload the same 600 MB twice, and "it's ready" has one place to look for
everyone who asked. The dedupe is therefore structural (a document id) rather
than a check somebody has to remember to write. `dedupe_requests()` below
enforces it a second time at the read side, because the two lanes are two
collections and a book can legitimately be pending in both.

EVICTION — ⚠️ AND WHY IT DELETES NOTHING TODAY
-----------------------------------------------
Owner's clause: *"if a book isnt downloaded for 7 days its removed … it can
always be rerequested"*, accepted with two build-time tunings (decision 3,
ratified in decision 5):

  (i)  R2 bills storage as a prorated monthly average, so waiting for a cycle
       boundary saves nothing — a stale book is deleted the day it goes stale.
  (ii) 7 days is too short: a 30-hour book over a month of commutes is paused
       for a week routinely. The predicate is **no stream AND no in-progress
       reading position for 30 days**, so a club-mate's half-finished book is
       shielded.

✅ **AS OF PHASE 3 (2026-09-02) BOTH HALVES ARE BUILT.** Streams are stamped
by the Worker's byte route into `audio_streams/{anchor}` (design §10.1);
POSITIONS are stamped by the player into `audio_positions/{anchor}` (phase 3),
and both are merged into the manifest here — `stream_stamps()` +
`merge_stream_stamps()`, `position_stamps()` + `merge_position_stamps()`.

⚠️ **BUILT IS NOT THE SAME AS MEASURING, AND THE DIFFERENCE IS THE WHOLE
POINT OF THE GUARD BELOW.** Two things must be true before either collection
can hold anything:

  1. `firebase deploy --only firestore:rules` — both the `audio_streams` and
     `audio_positions` clauses. Until then this tool's listings answer 403,
     which it reports as a WARN and an empty dict.
  2. The Worker half of the stream stamp is a HANDOFF, not built here —
     `audiobook-worker` lives in `catalog-platform` and this repo does not
     touch it.

🔴 **`last_position_at` IS THE MID-BOOK SHIELD**, and it is the whole reason
30 days was chosen over the owner's 7. A book with a stream stamp and NO
position data goes idle 30 days after the last listen — which is precisely
the 30-hour-book-over-a-month-of-commutes case the shield exists for. So
`--evict --commit` now **REFUSES** when the position lane has produced nothing
at all, because "the shield is empty" and "the shield was never deployed" look
identical from here and only one of them is safe. The escape hatch is explicit
(`--evict-without-position-shield`) and it is deliberately ugly to type.

`evict_candidates()` still refuses on a pair of nulls: no access data, no
deletions, stated out loud rather than silently no-op'd. R2 is a cache and the
local library deletes nothing, so an over-eager eviction costs an upload
rather than a book — that is why this can be tuned later without fear, and it
is not a reason to guess now.

R2 is a CACHE here, never the archive — the local library deletes nothing, so
an over-eager eviction costs an upload, not a book. That is the reason this
can be tuned later without fear; it is not a reason to guess now.

USAGE
-----
    python -m app.tools.fulfill_audio_requests              # dry run
    python -m app.tools.fulfill_audio_requests --commit     # upload + clear
    python -m app.tools.fulfill_audio_requests --evict      # report evictions
    python -m app.tools.fulfill_audio_requests --status     # what is streamable

Called by sync_to_drive.py as STEP 5.9 (soft: a failure warns, never stops).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.core.review_join import book_id_from_title
from app.tools.club_books import API_KEY as FS_KEY
from app.tools.club_books import fetch as fs_fetch
from app.tools.club_books import gv
from scripts import upload_audio_r2 as up

# 🔴 Imported, never re-typed. One canonical answer to "which keys are the
# backup" — a second copy of the string "archive/" in this file is exactly how
# a rename of the prefix would silently disarm the eviction guard below.
from scripts.archive_audio_r2 import ARCHIVE_PREFIX

# Both lanes, exactly as fetch_content_warnings.py reads cw_requests +
# cw_requests_dev: /dev/ and prod share one Firestore project, so a request
# made while testing must still be fulfillable.
REQUEST_COLLECTIONS = ("audio_requests", "audio_requests_dev")

# The stream stamps the Worker's byte route writes — audio-player PHASE 2,
# design §10.1. Both lanes, for the reason REQUEST_COLLECTIONS gives: a book
# listened to on /dev/ has been listened to.
STREAM_COLLECTIONS = ("audio_streams", "audio_streams_dev")

# The POSITION stamps the player writes — audio-player PHASE 3, the MID-BOOK
# SHIELD. Both lanes, and it matters more here than for the streams: the
# player has only ever run on the /dev/ lane, so today every position anybody
# saves lands in the `_dev` collection and nowhere else.
#
# ⚠️ THIS IS NOT `readingPositions`, AND THAT IS DELIBERATE. The real
# positions live there and it is `allow list: if false` in both lanes — "what
# a household reads is not a queryable set" — and this tool lists with the
# PUBLIC WEB API KEY, so it is gated exactly like a browser. Even holding a
# service account, reading every person's exact place in every book to answer
# a yes/no about one opaque anchor is more data than the question needs. This
# collection carries only `{anchor, lastPositionAt}`: no title, no path, no
# uid. Full reasoning: site/audio-position.js §4 and the firestore.rules block.
POSITION_COLLECTIONS = ("audio_positions", "audio_positions_dev")

# ⚠️ The eviction tuning, owner-ratified 2026-08-17. 30 days, not 7 — see the
# module docstring. Changing this number changes what gets deleted; it is a
# policy value, not a magic number, and it is named here once.
EVICT_IDLE_DAYS = 30


# ---------------------------------------------------------------------------
# reading the queue
# ---------------------------------------------------------------------------
def _request_rows(collection: str) -> List[dict]:
    """Raw Firestore docs for one lane. A listing failure is a WARN, not a
    stop: the other lane, and the rest of the pipeline, must still run."""
    try:
        return fs_fetch(collection)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] listing {collection} failed: {exc}")
        return []


def parse_request(doc: dict) -> Optional[dict]:
    """One Firestore doc -> a request dict, or None if it is unusable.

    Tolerant on purpose: a doc missing `bookId` (an older client, a hand-made
    row) is still fulfillable from its title, because the id fold is ours and
    deterministic. A doc missing BOTH is not a request, it is noise.
    """
    fields = doc.get("fields") or {}
    title = gv(fields, "bookTitle")
    book_id = gv(fields, "bookId") or (book_id_from_title(title) if title else "")
    if not (title or book_id):
        return None
    requesters = [
        v.get("stringValue", "")
        for v in ((fields.get("requesters") or {}).get("arrayValue") or {}).get("values", [])
        if v.get("stringValue")
    ]
    if not requesters and gv(fields, "requestedBy"):
        requesters = [gv(fields, "requestedBy")]
    return {
        "bookId": book_id,
        "bookTitle": title,
        "requesters": requesters,
        "docName": doc.get("name"),
    }


def dedupe_requests(requests: List[dict]) -> List[dict]:
    """One entry per BOOK, requester lists merged. ⚠️ THE DUPLICATE CLAUSE.

    The document id already makes a second press an update rather than a
    second row — but two LANES are two collections, so the same book can be
    legitimately pending in `audio_requests` and `audio_requests_dev` at once.
    Merging here means the upload happens once and BOTH docs are cleared,
    which is also why `docNames` is a list.
    """
    merged: Dict[str, dict] = {}
    for req in requests:
        if not req:
            continue
        key = req.get("bookId") or book_id_from_title(req.get("bookTitle") or "")
        if not key:
            continue
        entry = merged.setdefault(key, {
            "bookId": key,
            "bookTitle": req.get("bookTitle") or "",
            "requesters": [],
            "docNames": [],
        })
        if not entry["bookTitle"] and req.get("bookTitle"):
            entry["bookTitle"] = req["bookTitle"]
        for uid in req.get("requesters") or []:
            if uid not in entry["requesters"]:
                entry["requesters"].append(uid)
        if req.get("docName"):
            entry["docNames"].append(req["docName"])
    return [merged[k] for k in sorted(merged)]


def pending_requests() -> List[dict]:
    """Every pending request, both lanes, deduped to one entry per book."""
    rows: List[dict] = []
    for collection in REQUEST_COLLECTIONS:
        for doc in _request_rows(collection):
            parsed = parse_request(doc)
            if parsed:
                rows.append(parsed)
    return dedupe_requests(rows)


def _clear_request(doc_name: str) -> None:
    """Best-effort request cleanup — a failure is a warning, never a stopper
    (the request simply re-enters the next run).

    ⚠️ Authenticated by the PUBLIC WEB API KEY, exactly like
    `fetch_content_warnings._fs_delete`, so it is gated by firestore.rules the
    same way a browser is. That is why `allow delete: if true` on
    audio_requests is LOAD-BEARING: closing it would strand every fulfilled
    request and the book would re-upload every run.
    """
    if not doc_name:
        return
    try:
        req = urllib.request.Request(
            f"https://firestore.googleapis.com/v1/{doc_name}?key={FS_KEY}", method="DELETE")
        with urllib.request.urlopen(req, timeout=30):
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] request cleanup failed: {exc}")


# ---------------------------------------------------------------------------
# fulfilling
# ---------------------------------------------------------------------------
def already_streamable(book_id: str, record: Dict[str, dict]) -> Optional[str]:
    """The object key already serving this book, or None.

    Requesting a book that is already up is a NO-OP with a worded answer
    ("already streamable"), per decision 3 — not a re-upload.
    """
    for key, entry in record.items():
        if entry.get("streamable") and entry.get("bookId") == book_id:
            return key
    return None


def fulfill_requests(commit: bool = False) -> dict:
    """Upload every requested book that is not already up, then clear the docs.

    Returns a stats dict. Never raises for one bad book: the pipeline step that
    calls this is soft, and a single unresolvable title must not strand the
    rest of the queue.
    """
    stats = {"requested": 0, "uploaded": 0, "already": 0, "unresolved": 0, "failed": 0}
    requests = pending_requests()
    stats["requested"] = len(requests)
    if not requests:
        print("no pending audio requests")
        return stats

    record = up.load_record()
    print(f"{len(requests)} book(s) requested:")
    for req in requests:
        label = req["bookTitle"] or req["bookId"]
        have = already_streamable(req["bookId"], record)
        if have:
            print(f"  [have] {label} — already streamable ({have})")
            stats["already"] += 1
            if commit:
                for doc in req["docNames"]:
                    _clear_request(doc)
            continue

        targets, unresolved = up.resolve_targets([], [], [req["bookTitle"]] if req["bookTitle"] else [])
        if not targets:
            # ⚠️ NOT cleared. A title this machine cannot resolve is a real
            # problem someone must see (a re-filed book, a spelling drift) —
            # deleting the request would make it vanish silently instead.
            print(f"  [??  ] {label} — no file in the local library for this title; request KEPT")
            stats["unresolved"] += 1
            continue

        size = sum(p.stat().st_size for p in targets.values())
        print(f"  [want] {label} — {len(targets)} file(s), {size / 1e6:.0f} MB, "
              f"{len(req['requesters'])} requester(s)")
        if not commit:
            continue

        titles = {k: req["bookTitle"] for k in targets}
        uploaded, failed = up.upload_keys(targets, titles=titles)
        stats["uploaded"] += len(uploaded)
        stats["failed"] += len(failed)
        if failed:
            print(f"  [FAIL] {label} — request KEPT for the next run")
            continue
        for doc in req["docNames"]:
            _clear_request(doc)
        record = up.load_record()

    if not commit:
        print("\nDRY RUN (default) — nothing uploaded, nothing cleared. Re-run with --commit.")
        return stats

    # ⚠️ PUBLISH, or the upload was for nothing. The Worker resolves
    # `anchor -> path` out of the manifest in a PRIVATE R2 bucket, not out of
    # site/audio_manifest.json (which is gitignored and never leaves this
    # machine). An uploaded object nobody published is 600 MB in R2 that the
    # player answers `not_streamable` for — the worst of both, billed and
    # unusable. Soft: publish_if_changed never raises, and its own no-op branch
    # makes this free when nothing changed.
    _publish_manifest()
    return stats


def _publish_manifest() -> None:
    """Push the record to the gated bucket. Soft — STEP 5.9 must never stop."""
    try:
        from scripts.publish_audio_manifest import publish_if_changed
        publish_if_changed()
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] audio manifest publish failed: {exc}")


# ---------------------------------------------------------------------------
# eviction — built, tested, and refusing
# ---------------------------------------------------------------------------
def _parse_stamp(value) -> Optional[float]:
    """An ISO-Z string or epoch number -> epoch seconds. None if unusable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Milliseconds if it is implausibly large as seconds (year > 5000).
        return float(value) / 1000.0 if value > 1e11 else float(value)
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _parse_stamp_doc(doc: dict, field: str) -> Optional[Tuple[str, str]]:
    """One access-stamp document -> `(anchor, iso_stamp)`, or None.

    ⚠️ THE WIRE FORMAT IS DELIBERATELY PERMISSIVE ON THE READ SIDE, and this
    is the seam where codebases meet: the audiobook Worker (TypeScript, in
    catalog-platform) writes the stream stamps and a BROWSER writes the
    position stamps; this file reads both. Firestore's REST encoding gives a
    different key depending on what the writer used — `integerValue` for a JS
    `Date.now()`, `doubleValue` if it ever arrives as a float,
    `timestampValue` for a real server timestamp, `stringValue` for an ISO
    string. All four are accepted, because the alternative is a stamp silently
    ignored, which is indistinguishable from "nobody listened" and would let
    the evictor delete a book somebody is halfway through.

    ⚠️ ONE PARSER, TWO COLLECTIONS. The shapes are identical by design (the
    position collection was modelled on the stream one), and two copies of a
    permissive parser is two places for the units to drift.
    """
    fields = doc.get("fields") or {}
    anchor = gv(fields, "anchor") or (doc.get("name") or "").split("/")[-1]
    if not anchor:
        return None
    raw = fields.get(field) or {}
    value = (
        raw.get("integerValue")
        or raw.get("doubleValue")
        or raw.get("timestampValue")
        or raw.get("stringValue")
    )
    if value is None:
        return None
    numeric = str(value).lstrip("-").replace(".", "", 1).isdigit()
    seconds = _parse_stamp(float(value) if numeric else value)
    if seconds is None:
        return None
    return anchor, datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stream_doc(doc: dict) -> Optional[Tuple[str, str]]:
    """One `audio_streams` document -> `(anchor, iso_stamp)`, or None.

    🔴 **THE CANONICAL CONTRACT, for whoever builds the Worker half:**
    `audio_streams/{anchor}` = `{ anchor: string, lastStreamAt: number }`,
    `lastStreamAt` being **epoch MILLISECONDS**. The document id IS the
    anchor; `anchor` is carried in the body too so a doc read on its own is
    self-describing.
    """
    return _parse_stamp_doc(doc, "lastStreamAt")


def parse_position_doc(doc: dict) -> Optional[Tuple[str, str]]:
    """One `audio_positions` document -> `(anchor, iso_stamp)`, or None.

    🔴 **THE CANONICAL CONTRACT, honoured by `site/audio-position.js`
    `stampBody()`:** `audio_positions/{anchor}` = `{ anchor: string,
    lastPositionAt: number }`, `lastPositionAt` being **epoch MILLISECONDS**.

    ⚠️ THE UNIT IS THE TRAP AND IT IS ASYMMETRIC. `_parse_stamp` reads
    anything under 1e11 as SECONDS, so a stamp written in seconds decodes to a
    date in 1970 — older than every cutoff, i.e. "evict this book" about a
    book somebody is listening to right now. `Date.now()` is already
    milliseconds and nothing on the writing side divides it.
    """
    return _parse_stamp_doc(doc, "lastPositionAt")


def _collect_stamps(collections, field: str) -> Tuple[Dict[str, str], List[str]]:
    """`({anchor: newest ISO-Z stamp}, [lanes that could not be listed])`.

    ⚠️ A listing failure is a WARN and an EMPTY dict, never an exception —
    exactly as `_request_rows` does it. But note what that costs and why it is
    still right: an empty answer means `evict_candidates()` sees no access
    data and therefore REFUSES to delete anything. Failing this way round is
    safe (nothing is lost); failing the other way round deletes books.

    🔴 THE FAILED LANES ARE RETURNED, NOT JUST WARNED ABOUT, and that is the
    phase-3 addition. "Listed cleanly and found nothing" and "could not list
    it at all" produce the same dict and mean completely different things —
    the second is what a missing `firebase deploy --only firestore:rules`
    looks like from here, and it is exactly the silent-staleness trap. The
    caller words them differently.

    ⚠️ `club_books.fetch` asks for `pageSize=300` and does not paginate. That
    is fine while the streaming set is smaller than 300 books — it is 1 today
    and its ceiling is the 1,073-file library — but a household that streams
    more than 300 books would silently see only the first page, and the
    symptom would be old books never being evicted. Noted here rather than
    solved, because paginating an unpaginated helper used by five other tools
    is a change with its own blast radius.
    """
    out: Dict[str, str] = {}
    unreadable: List[str] = []
    for collection in collections:
        try:
            docs = fs_fetch(collection)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] listing {collection} failed: {exc}")
            unreadable.append(collection)
            continue
        for doc in docs:
            parsed = _parse_stamp_doc(doc, field)
            if not parsed:
                continue
            anchor, stamp = parsed
            # Two lanes can both hold a stamp for one book. Newest wins.
            if anchor not in out or stamp > out[anchor]:
                out[anchor] = stamp
    return out, unreadable


def stream_stamps() -> Dict[str, str]:
    """`{anchor: newest ISO-Z stamp}` for listening, across both lanes."""
    return _collect_stamps(STREAM_COLLECTIONS, "lastStreamAt")[0]


def position_stamps() -> Dict[str, str]:
    """`{anchor: newest ISO-Z stamp}` for saved positions, across both lanes.

    🔴 THE MID-BOOK SHIELD's data source. See POSITION_COLLECTIONS for why it
    is not `readingPositions`.
    """
    return _collect_stamps(POSITION_COLLECTIONS, "lastPositionAt")[0]


def _merge_stamps(files: Dict[str, dict], stamps: Dict[str, str], field: str) -> int:
    """Write one access-timestamp field onto every recorded object with a stamp.

    Joined on the **anchor**, not the key: the record is keyed on the
    library-relative path, and a path is exactly what the writers do not have
    (and must never be sent — the projection withholds it on purpose, and the
    stamp collections are world-readable). The anchor is the shared identity,
    which is what an anchor is for.

    Mutates `files` in place and answers how many entries gained a stamp.

    ⚠️ NEWEST WINS, never blind overwrite. A record may already carry a stamp
    from an earlier pass; taking the max means a Firestore listing that loses
    a page, or a lane that has not been written recently, can never move a
    book's last-touched time BACKWARDS — and moving it backwards is what would
    evict a book somebody is currently listening to.
    """
    merged = 0
    for entry in files.values():
        anchor = (entry or {}).get("anchor")
        if not anchor or anchor not in stamps:
            continue
        incoming = stamps[anchor]
        existing = entry.get(field)
        if existing and str(existing) >= incoming:
            continue
        entry[field] = incoming
        merged += 1
    return merged


def merge_stream_stamps(files: Dict[str, dict], stamps: Dict[str, str]) -> int:
    """Write `last_stream_at` onto every recorded object with a stamp."""
    return _merge_stamps(files, stamps, "last_stream_at")


def merge_position_stamps(files: Dict[str, dict], stamps: Dict[str, str]) -> int:
    """Write `last_position_at` — the MID-BOOK SHIELD — onto every recorded
    object with a stamp."""
    return _merge_stamps(files, stamps, "last_position_at")


def evict_candidates(files: Dict[str, dict], now: Optional[float] = None,
                     idle_days: int = EVICT_IDLE_DAYS) -> Tuple[List[str], List[str]]:
    """Which objects may be deleted -> `(candidates, refusals)`.

    ⚠️ **THE GUARD IS THE POINT OF THIS FUNCTION.** An object is a candidate
    only when there is POSITIVE evidence it is idle — and never, under any
    circumstance, if its key is under ``archive/``:

      * 🔴 **``archive/`` IS THE BACKUP, NOT A CACHE.**
        `scripts/archive_audio_r2.py` mirrors the entire ~685 GB library into
        this same bucket under that prefix as the household's only off-site
        copy (owner, 2026-08-18: *"we lose this data we lose it all and the
        server isnt ready yet"*). Everything else in this bucket is a streaming
        cache whose deletion costs a re-upload from disk; an `archive/` object
        may be the LAST COPY. It is refused here unconditionally, with no flag
        to override it. The archive manifest is a separate file
        (`output_files/audio_archive_manifest.json`) so these keys should never
        reach this function at all — this is the belt to that braces, because
        the cost of being wrong is asymmetric past all reasoning.

      * it must carry at least one access timestamp (`last_stream_at` or
        `last_position_at`). A pair of nulls means "nothing has ever measured
        this", which is NOT the same as "nobody has listened" — and treating
        it as such would evict every book 30 days after upload, mid-listen
        included. Phase 2 writes the first field, phase 3 the second; while
        neither is arriving this branch returns a refusal, in words, for every
        object.
      * the newest of those timestamps must be older than `idle_days`.
        ⚠️ `last_position_at` is the MID-BOOK SHIELD: a paused 30-hour book is
        the normal case, not an abandoned one.

    Pure and side-effect free — it decides, it never deletes. `--evict` prints
    the answer; deletion is deliberately not wired until phase 2 supplies the
    data, so this cannot become a blind date-based purge by accident.
    """
    now = now if now is not None else time.time()
    cutoff = now - idle_days * 86400
    candidates: List[str] = []
    refusals: List[str] = []
    for key in sorted(files):
        entry = files[key] or {}
        # 🔴 THE ARCHIVE GUARD — first, before anything else is even considered.
        # See the docstring: an object under this prefix is the off-site backup
        # of the master, and deleting it can lose the only copy.
        if str(key).startswith(ARCHIVE_PREFIX):
            refusals.append(
                f"{key}: under the '{ARCHIVE_PREFIX}' prefix — this is the DISASTER-RECOVERY "
                "ARCHIVE (scripts/archive_audio_r2.py), not a streaming cache. Never evicted, "
                "no flag overrides this."
            )
            continue
        if not entry.get("streamable"):
            continue
        stamps = [s for s in (_parse_stamp(entry.get("last_stream_at")),
                              _parse_stamp(entry.get("last_position_at"))) if s is not None]
        if not stamps:
            refusals.append(
                f"{key}: nothing has ever MEASURED this object — last_stream_at and "
                "last_position_at are both null, which is not the same as 'nobody "
                "listened'. Deploy the firestore.rules stamp lanes and check that the "
                "Worker's byte route is stamping (design §10.1)")
            continue
        newest = max(stamps)
        if newest < cutoff:
            candidates.append(key)
        else:
            refusals.append(
                f"{key}: last touched {(now - newest) / 86400:.1f}d ago "
                f"(< {idle_days}d) — kept")
    return candidates, refusals


def run_eviction(commit: bool = False, idle_days: int = EVICT_IDLE_DAYS,
                 without_position_shield: bool = False) -> dict:
    """Report (and, when both access fields are arriving, delete) idle objects."""
    record = up.load_record()

    # ⚠️ THE TWO HALVES OF THE ACCESS DATA, and they answer different
    # questions. Design §10.1: the Worker's byte route stamps
    # `audio_streams/{anchor}` — "bytes were served". Phase 3: the player
    # stamps `audio_positions/{anchor}` — "somebody has a place in this book".
    # This is where both meet the manifest the evictor actually reads.
    streams, streams_unreadable = _collect_stamps(STREAM_COLLECTIONS, "lastStreamAt")
    positions, positions_unreadable = _collect_stamps(POSITION_COLLECTIONS, "lastPositionAt")
    merged = merge_stream_stamps(record, streams)
    shielded = merge_position_stamps(record, positions)

    # ⚠️ "Could not list it" and "listed it and found nothing" are DIFFERENT
    # FACTS that produce the same empty dict, and only one of them is a
    # problem to fix. Saying which is the difference between "nobody has
    # listened yet" and "the rules were never deployed".
    if merged:
        print(f"Merged {merged} stream stamp(s) from {'/'.join(STREAM_COLLECTIONS)}.")
    elif streams_unreadable:
        print(f"Could NOT list {', '.join(streams_unreadable)} - the stream stamps are "
              "unreadable, not absent. Deploy firestore.rules and re-run.")
    else:
        print(f"No stream stamps found in {'/'.join(STREAM_COLLECTIONS)} - either nobody "
              "has listened yet, or the Worker's byte route is not stamping (design 10.1).")
    if shielded:
        print(f"Merged {shielded} position stamp(s) from {'/'.join(POSITION_COLLECTIONS)} "
              "- the mid-book shield.")
    elif positions_unreadable:
        print(f"Could NOT list {', '.join(positions_unreadable)} - the MID-BOOK SHIELD is "
              "unreadable, not absent. Deploy firestore.rules and re-run.")
    else:
        print(f"No position stamps found in {'/'.join(POSITION_COLLECTIONS)} - either "
              "nobody has saved a spot yet, or the player is not stamping.")
    # ⚠️ Persisted only on --commit. A dry run that rewrites a state file is a
    # surprise, and this one sits in a tree a scheduled pipeline is also
    # writing to.
    if commit and (merged or shielded):
        up.write_record(record)

    candidates, refusals = evict_candidates(record, idle_days=idle_days)
    print(f"Eviction pass over {len(record)} recorded object(s), "
          f"idle threshold {idle_days} days:")
    for line in refusals:
        print(f"  [keep] {line}")
    for key in candidates:
        print(f"  [EVICT] {key}")
    if not candidates:
        print("  nothing is evictable. R2 is a cache, not the archive — a book "
              "removed here is always re-requestable from the local library.")
    elif not commit:
        print("\n  DRY RUN — re-run with --evict --commit to delete these.")
    elif not positions and not without_position_shield:
        # 🔴 THE MID-BOOK SHIELD REFUSAL, and it is the reason `--evict
        # --commit` was unsafe for the whole of phases 0b-2.
        #
        # An empty position lane has TWO causes that look identical from here:
        # nobody has saved a spot yet, or the shield was never deployed / is
        # not being written. Only the first is safe to act on, and this tool
        # cannot tell them apart — so it refuses, in words, rather than
        # deleting a 30-hour book out from under somebody who has been
        # listening to it on the commute for a fortnight. That case is the
        # ENTIRE reason the threshold is 30 days rather than the owner's 7.
        #
        # ⚠️ The escape hatch is explicit and deliberately ugly to type, per
        # the estate's "mechanical guards beat written advice" rule. Nothing
        # is lost by waiting: R2 is a cache, the local library deletes
        # nothing, and every evicted book costs an upload rather than a book.
        if positions_unreadable:
            why = (f"the lane could not be listed at all ({', '.join(positions_unreadable)}) "
                   "- that is what a missing `firebase deploy --only firestore:rules` "
                   "looks like from here")
        else:
            why = ("the lane listed cleanly and is empty - either nobody has saved a spot "
                   "yet, or the player is not stamping")
        # ⚠️ PLAIN ASCII IN EVERY print() HERE. A hand run on this box encodes
        # stdout as cp1252 and a non-cp1252 character raises mid-run — which
        # in a REFUSAL path would turn "nothing was deleted, and here is why"
        # into a traceback. Emoji in comments and docstrings are fine.
        print(f"\n  REFUSED: {len(candidates)} object(s) look idle, but the MID-BOOK "
              f"SHIELD has no data at all - {why}.")
        print("  Nothing was deleted. A book with a stream stamp and no position data goes "
              f"idle {idle_days} days after the last listen, which is exactly the "
              "paused-30-hour-book case the shield exists for.")
        print("  Verify the shield first (scripts/smoke_audio_position_rules.py, then "
              "--status), or override deliberately with "
              "`--evict --commit --evict-without-position-shield`.")
        return {"candidates": len(candidates), "kept": len(refusals), "refused_no_shield": True}
    else:
        client = up.s3_client()
        for key in candidates:
            try:
                client.delete_object(Bucket=up.BUCKET, Key=key)
                entry = record.get(key) or {}
                entry["streamable"] = False
                entry["evicted_at"] = up.now_iso()
                record[key] = entry
                print(f"  deleted {key}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [WARN] could not delete {key}: {exc}")
        up.write_record(record)
        # An eviction that is not published leaves the player offering a play
        # button for a book we just deleted — a 404 where a worded "request it"
        # belongs. Same soft contract as the fulfil path.
        _publish_manifest()
    return {"candidates": len(candidates), "kept": len(refusals)}


# ---------------------------------------------------------------------------
def print_status() -> None:
    record = up.load_record()
    # ⚠️ IN MEMORY ONLY — `--status` must not write files. The merge is shown
    # so a person can see whether the Worker is stamping at all, which is the
    # single question "is eviction ever going to work" reduces to.
    merged = merge_stream_stamps(record, stream_stamps())
    shielded = merge_position_stamps(record, position_stamps())
    streamable = {k: v for k, v in record.items() if v.get("streamable")}
    print(f"Bucket     : {up.BUCKET}")
    print(f"Record     : {up.RECORD_PATH}")
    print(f"Streamable : {len(streamable)} of {len(record)} recorded "
          f"({sum(int(v.get('size') or 0) for v in streamable.values()) / 1e9:.3f} GB)")
    print(f"Streams    : {merged} object(s) carry a stream stamp "
          f"(from {'/'.join(STREAM_COLLECTIONS)})")
    # 🔴 The MID-BOOK SHIELD's coverage. This line is the single honest answer
    # to "is --evict --commit safe yet", and a zero here is why it refuses.
    print(f"Positions  : {shielded} object(s) carry a saved-position stamp "
          f"(from {'/'.join(POSITION_COLLECTIONS)})")
    for key, entry in sorted(streamable.items()):
        # ⚠️ "never measured" is the honest phrase, and it is NOT the same as
        # "nobody has listened": it means nothing has ever MEASURED a listen.
        # Until the Worker and the player stamp, every book reads `never` no
        # matter how much it is played — which is exactly why
        # evict_candidates() refuses to delete on a pair of nulls.
        last = entry.get("last_stream_at") or "never measured"
        spot = entry.get("last_position_at") or "never measured"
        print(f"  {key}  (since {entry.get('since')}, last stream {last}, "
              f"last saved spot {spot})")
    pending = pending_requests()
    print(f"Pending    : {len(pending)} request(s)")
    for req in pending:
        print(f"  {req['bookTitle'] or req['bookId']}  "
              f"({len(req['requesters'])} requester(s))")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", action="store_true", help="actually upload / delete (default is a dry run)")
    ap.add_argument("--evict", action="store_true", help="run the eviction pass instead of fulfilling")
    ap.add_argument("--status", action="store_true", help="what is streamable and what is queued")
    ap.add_argument("--idle-days", type=int, default=EVICT_IDLE_DAYS,
                    help=f"eviction idle threshold in days (default {EVICT_IDLE_DAYS})")
    # ⚠️ DELIBERATELY UGLY TO TYPE, per the estate's "mechanical guards beat
    # written advice" rule: the guard it lifts is the MID-BOOK SHIELD, and the
    # thing on the other side of it is somebody's half-finished 30-hour book.
    ap.add_argument("--evict-without-position-shield", action="store_true",
                    help="delete idle objects even though NO saved-position stamps exist "
                         "at all (the mid-book shield is the reason the threshold is 30 "
                         "days, not 7 - see the module docstring before using this)")
    ap.add_argument("--json", action="store_true", help="print the stats dict as JSON")
    args = ap.parse_args(argv)

    if args.status:
        print_status()
        return 0
    if args.evict:
        stats = run_eviction(commit=args.commit, idle_days=args.idle_days,
                             without_position_shield=args.evict_without_position_shield)
    else:
        stats = fulfill_requests(commit=args.commit)
    if args.json:
        print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
