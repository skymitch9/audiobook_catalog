"""
uid-binding audit — Phase 5's MEASUREMENT. READ-ONLY, in every sense.

docs/TODO.md, Phase 5: *"uid-binding on member self-writes. Partially true
already by accident: readingPositions, readingLists and user_content_warnings
are uid-bound; reviews, comments, RSVPs and progress are not."* The survey's
recommendation was to build the MEASUREMENT before the enforcement, because
the phase's own exit condition is a number — *"the phase does not ship until
the tokenless-writes measurement reads zero"* — and nothing in this estate
could produce that number.

This script answers three questions and changes nothing:

  1. WHICH WRITE PATHS EXIST, and how many of them could currently accept a
     write with no uid at all. Measured by parsing `firestore.rules`: every
     `allow create|update|write` whose condition — resolved THROUGH the
     validator functions it calls — never mentions `request.auth`.
  2. HOW MANY EXISTING RECORDS CARRY A UID, per collection, both lanes.
  3. HOW MANY DO NOT.

⚠️ IT DEPLOYS NO RULES, FLIPS NO FLAG AND WRITES NO DOCUMENT. Phase 5's
enforcement is an owner decision (a rules deploy plus a two-repo change whose
second repo — `bookbuddy/library_catalog`'s Worker — writes to this same
`reviews` collection). A measurement tool that is one flag away from being the
enforcement is how an unreviewed tightening ships as a side effect of an
audit. tests/test_uid_binding_audit.py asserts the absence, with teeth.

⚠️ WHAT "CARRIES A UID" MEANS HERE, because the honest answer has two
strengths and they must not be blended:

  * `field:<name>` — the document holds a uid FIELD (`authorUid`, `uid`).
    Proof: something stamped it deliberately.
  * `docid:known-uid` — the doc id (or its first `_` segment) is an id this
    run independently observed as a real Firebase uid, harvested from
    `site_roles` doc ids and every club's `managerUids` keys.
  * `docid:uid-shaped` — the id merely LOOKS like a uid (28ish alphanumerics,
    no separators). A heuristic, reported under its own label so a reader can
    discount it. It is never folded into the strong counts.

  A doc-id-keyed collection whose key is `slugifyName(displayName)` is marked
  `docid: slug` and can never report a uid from its id, however uid-shaped a
  slug happens to look — nothing compares that id to `request.auth`, so it
  binds nothing. That is the difference the whole phase is about.

Usage (from the repo root):
    python scripts/uid_binding_audit.py                # rules + live Firestore
    python scripts/uid_binding_audit.py --rules-only   # no Firestore at all
    python scripts/uid_binding_audit.py --json-summary

⚠️ On Windows set PYTHONIOENCODING=utf-8 when capturing this script's output —
the report prints em-dashes and ⚠️, and a cp1252 pipe raises UnicodeEncodeError
mid-report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
RULES_PATH = REPO_ROOT / "firestore.rules"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# A Firebase Auth uid is 28 URL-safe alphanumerics in practice. Bounded loosely
# because the length is a convention rather than a promise — but NO separators,
# which is what distinguishes it from `slugifyName()` output (lowercase words
# joined by '-').
UID_SHAPE = re.compile(r"^[A-Za-z0-9]{20,40}$")

WRITE_VERBS = ("write", "create", "update")


# ---------------------------------------------------------------------------
# PURE: the firestore.rules parser
# ---------------------------------------------------------------------------


def strip_comments(text: str) -> str:
    """Remove `//` and `/* */` comments, QUOTE-AWARE.

    ⚠️ A naive `line.split('//')` is wrong on this file and wrong in the one
    place it matters most: validClubSettings() matches
    `'https://(discord|discordapp)\\.com/...'`, so a naive strip truncates the
    rule mid-expression and the parser then reads a condition that does not
    exist — silently, and in the direction of "looks simpler than it is".
    """
    out = []
    i, n = 0, len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_FUNC_RE = re.compile(r"function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*\{")
_MATCH_RE = re.compile(r"match\s+(\S+)\s*\{")
_ALLOW_RE = re.compile(r"allow\s+([a-z,\s]+):\s*if\b", re.S)
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# The `match /databases/{database}/documents` wrapper is scaffolding, not a
# collection; dropping it is what makes a path read `/reviews/{reviewId}`.
_ROOT_SEGMENTS = ("/databases/{database}/documents",)


def _function_bodies(src: str) -> dict[str, str]:
    fns: dict[str, str] = {}
    for m in _FUNC_RE.finditer(src):
        name = m.group(1)
        depth, i, n = 1, m.end(), len(src)
        while i < n and depth:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        body = src[m.end(): i - 1]
        ret = body.split("return", 1)[-1]
        fns[name] = ret.strip().rstrip(";").strip()
    return fns


def resolve_condition(cond: str, functions: dict[str, str], depth: int = 0) -> str:
    """Inline every function call one level at a time, up to a bounded depth.

    Bounded rather than exhaustive on purpose: the rules file has mutually
    reachable helpers, and the question being asked ("does `request.auth`
    appear anywhere in what this allow really evaluates") does not need a
    perfect expansion — it needs one that cannot hang.
    """
    if depth >= 6:
        return cond
    out = cond
    for name in set(_CALL_RE.findall(cond)):
        body = functions.get(name)
        if body is None:
            continue
        out = out.replace(f"{name}(", f"( {body} ) IGNORED(")
    if out == cond:
        return out
    return resolve_condition(out, functions, depth + 1)


def parse_rules(text: str) -> dict:
    """Every `allow` in the file, with its path, verbs and resolved condition."""
    src = strip_comments(text)
    functions = _function_bodies(src)

    paths: list[dict] = []
    stack: list[str] = []
    i, n = 0, len(src)
    # Depth of the brace at which each match block was opened, so the path
    # stack pops in step with the braces.
    open_depths: list[int] = []
    depth = 0
    while i < n:
        ch = src[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if open_depths and open_depths[-1] == depth:
                open_depths.pop()
                stack.pop()
            i += 1
            continue
        m = _MATCH_RE.match(src, i)
        if m:
            seg = m.group(1)
            if seg not in _ROOT_SEGMENTS:
                stack.append(seg)
                open_depths.append(depth)
            else:
                stack.append("")
                open_depths.append(depth)
            depth += 1
            i = m.end()
            continue
        m = _ALLOW_RE.match(src, i)
        if m:
            end = src.find(";", m.end())
            cond = src[m.end(): end].strip()
            verbs = [v.strip() for v in m.group(1).split(",") if v.strip()]
            resolved = resolve_condition(cond, functions)
            client_writable = (
                any(v in WRITE_VERBS for v in verbs) and cond.strip() != "false"
            )
            paths.append(
                {
                    "path": "".join(stack) or "/",
                    "verbs": verbs,
                    "condition": cond,
                    "resolved": resolved,
                    "requires_auth": "request.auth" in resolved,
                    "client_writable": client_writable,
                }
            )
            i = end + 1 if end != -1 else m.end()
            continue
        i += 1
    return {"functions": functions, "paths": paths}


def summarize_paths(paths: list[dict]) -> dict:
    writable = [p for p in paths if p["client_writable"]]
    unbound = [p for p in writable if not p["requires_auth"]]
    return {
        "allow_statements": len(paths),
        "client_write_paths": len(writable),
        "write_paths_with_uid": len(writable) - len(unbound),
        "write_paths_without_uid": len(unbound),
    }


# ---------------------------------------------------------------------------
# PURE: the per-document uid probe
# ---------------------------------------------------------------------------


def _field_evidence(data: dict, probe: dict) -> str | None:
    for field in probe.get("uid_fields", ()):
        value = (data or {}).get(field)
        if isinstance(value, str) and value.strip():
            return f"field:{field}"
    return None


def _docid_evidence(doc_id: str, probe: dict, known_uids: set[str]) -> str | None:
    form = probe.get("docid", "none")
    if form in ("none", "slug", "auto"):
        return None
    candidate = doc_id.split("_", 1)[0] if form == "uid_prefix" else doc_id
    if candidate in known_uids:
        return "docid:known-uid"
    if UID_SHAPE.match(candidate):
        return "docid:uid-shaped"
    return None


def classify_uid(doc_id: str, data: dict, probe: dict, known_uids: set[str]) -> str | None:
    """The evidence that THIS document is bound to an account, or None.

    ⚠️ THE ORDER IS THE PROBE'S TO CHOOSE, and it must match what the RULE
    actually compares — verify with the right instrument. `readingLists` and
    `readingPositions` are bound by `canWriteReadingList(docId)` /
    `ownsPositionDoc(docId)`, which compare `request.auth.uid` against the DOC
    ID; those documents also happen to carry a `uid` FIELD, and a probe that
    read the field first would report a legacy display-name-keyed document as
    bound because it carries a field no rule looks at. `user_content_warnings`
    is the mirror case — its binding IS the `authorUid` field.

    `known-uid` beats `uid-shaped`, and the caller counts the labels
    separately, so a heuristic is never added to a proof.
    """
    if probe.get("prefer") == "docid":
        if probe.get("docid") in ("uid", "uid_prefix"):
            # ⚠️ NO FALLBACK. The doc id is the whole binding here, so a `uid`
            # field on a document whose id is a display name is corroboration
            # of nothing — counting it would report the exact population
            # (KI-4's legacy display-name-keyed docs) that the binding misses.
            return _docid_evidence(doc_id, probe, known_uids)
        return _field_evidence(data, probe)
    return _field_evidence(data, probe) or _docid_evidence(doc_id, probe, known_uids)


# ---------------------------------------------------------------------------
# The probe table — WHAT gets counted, and why each is in or out.
#
# ⚠️ A probe pointed at a collection the rules do not have measures 0 and looks
# like good news, so a test asserts every `rules_path` below exists.
# ---------------------------------------------------------------------------

SELF_WRITE_PROBES: list[dict] = [
    # --- the four the TODO names as UNBOUND ---------------------------------
    {
        "collection": "reviews",
        "kind": "root",
        "rules_path": "/reviews/{reviewId}",
        "docid": "none",
        "uid_fields": ("authorUid", "uid"),
        "why": "doc id is `{bookId}_{displayNameLower}` (site/reviews.js submitReview) "
               "— a display name, which no rule can bind to a person",
    },
    {
        "collection": "comments",
        "kind": "group",
        "rules_path": "/clubs/{clubId}/reads/{readId}/comments/{commentId}",
        "docid": "auto",
        "uid_fields": ("authorUid", "uid"),
        "why": "auto id; the doc carries displayName + slug and no account",
    },
    {
        "collection": "rsvps",
        "kind": "group",
        "rules_path": "/clubs/{clubId}/rsvps/{userId}",
        "docid": "slug",
        "uid_fields": ("authorUid", "uid"),
        "why": "the rules call the wildcard {userId}; the client writes "
               "slugifyName(displayName) (site/club-reads.js setRsvp)",
    },
    {
        "collection": "progress",
        "kind": "group",
        "rules_path": "/clubs/{clubId}/reads/{readId}/progress/{userId}",
        "docid": "slug",
        "uid_fields": ("authorUid", "uid"),
        "why": "same slug key as rsvps (setProgress/setChapterProgress)",
    },
    # --- the neighbours with the same shape, in for completeness ------------
    {
        "collection": "ratings",
        "kind": "group",
        "rules_path": "/clubs/{clubId}/reads/{readId}/ratings/{userId}",
        "docid": "slug",
        "uid_fields": ("authorUid", "uid"),
        "why": "slug-keyed like progress; blind until reveal, but not bound",
    },
    {
        "collection": "quotes",
        "kind": "group",
        "rules_path": "/clubs/{clubId}/reads/{readId}/quotes/{quoteId}",
        "docid": "auto",
        "uid_fields": ("authorUid", "uid"),
        "why": "auto id, displayName only",
    },
    {
        "collection": "votes",
        "kind": "group",
        "rules_path": "/clubs/{clubId}/polls/{pollId}/votes/{userId}",
        "docid": "slug",
        "uid_fields": ("authorUid", "uid"),
        "why": "slug-keyed (castVote)",
    },
    {
        "collection": "members",
        "kind": "group",
        "rules_path": "/clubs/{clubId}/members/{memberId}",
        "docid": "slug",
        "uid_fields": ("uid", "authorUid"),
        "why": "slug-keyed; the club's uid layer lives in the club doc's "
               "managerUids, not here",
    },
    {
        "collection": "profiles",
        "kind": "root",
        "rules_path": "/profiles/{userId}",
        "docid": "slug",
        "uid_fields": ("uid", "authorUid"),
        "why": "doc id is slugifyName(displayName) (site/identity.js)",
    },
    {
        "collection": "club_seen",
        "kind": "root",
        "rules_path": "/club_seen/{userId}",
        "docid": "slug",
        "uid_fields": ("uid",),
        "why": "slug-keyed (site/club-notify.js)",
    },
    # --- the CONTROL: the three that ARE bound ------------------------------
    {
        "collection": "readingLists",
        "kind": "root",
        "rules_path": "/readingLists/{docId}",
        "docid": "uid_prefix",
        "prefer": "docid",
        "uid_fields": ("uid",),
        "why": "CONTROL — `{uid}_{bookId}` since 2026-08-18 (readingListDocId). "
               "⚠️ prefer=docid: canWriteReadingList() compares request.auth.uid "
               "to the DOC ID, so the id is the binding and the `uid` field is "
               "only corroboration",
    },
    {
        "collection": "readingPositions",
        "kind": "root",
        "rules_path": "/readingPositions/{docId}",
        "docid": "uid_prefix",
        "prefer": "docid",
        "uid_fields": ("uid",),
        "why": "CONTROL — `{uid}_{bookId}` (positionDocId); ownsPositionDoc() "
               "compares against the DOC ID, so prefer=docid",
    },
    {
        "collection": "user_content_warnings",
        "kind": "root",
        "rules_path": "/user_content_warnings/{docId}",
        "docid": "none",
        "uid_fields": ("authorUid",),
        "why": "CONTROL, and a PARTIAL one — the authorUid field is stamped "
               "when a live session can prove one and omitted when it cannot; "
               "only DELETE is bound, create/update stay shape-only",
    },
]


# ---------------------------------------------------------------------------
# IMPURE: the live read. Nothing here writes.
# ---------------------------------------------------------------------------


def _firestore_client():
    import os

    key_path = Path(
        os.getenv("FIREBASE_SERVICE_ACCOUNT") or (SCRIPT_DIR / "firebase_service_account.json")
    )
    if not key_path.exists():
        sys.exit(
            f"FATAL: no Firebase service account key at {key_path} "
            "(set FIREBASE_SERVICE_ACCOUNT or place the JSON there) — same "
            "requirement as scripts/drive_role_parity.py."
        )
    import firebase_admin
    from firebase_admin import credentials, firestore

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(str(key_path)))
    return firestore.client()


def harvest_known_uids(db) -> set[str]:
    """Real uids, observed rather than assumed: every `site_roles` doc id and
    every key of every club's `managerUids` map."""
    uids: set[str] = set()
    for d in db.collection("site_roles").stream():
        uids.add(d.id)
    for col in ("clubs", "clubs_dev"):
        for club in db.collection(col).stream():
            roster = (club.to_dict() or {}).get("managerUids") or {}
            if isinstance(roster, dict):
                uids.update(k for k in roster if isinstance(k, str))
    return uids


def _lane_of(ref) -> str:
    """`clubs/...` vs `clubs_dev/...` — a collection-group query returns both
    lanes, because a collection GROUP is keyed on the last segment only."""
    parts = ref.path.split("/")
    root = parts[0]
    return "dev" if root.endswith("_dev") else "prod"


def measure(db, probes: list[dict], known_uids: set[str]) -> list[dict]:
    rows = []
    for probe in probes:
        counts = {
            "prod": {"total": 0, "field": 0, "known_uid": 0, "uid_shaped": 0, "none": 0},
            "dev": {"total": 0, "field": 0, "known_uid": 0, "uid_shaped": 0, "none": 0},
        }
        if probe["kind"] == "group":
            docs = db.collection_group(probe["collection"]).stream()
        else:
            docs = []
            for name, lane in ((probe["collection"], "prod"), (probe["collection"] + "_dev", "dev")):
                docs = list(docs) + [(d, lane) for d in db.collection(name).stream()]
        for item in docs:
            if isinstance(item, tuple):
                doc, lane = item
            else:
                doc, lane = item, _lane_of(item.reference)
            data = doc.to_dict() or {}
            verdict = classify_uid(doc.id, data, probe, known_uids)
            bucket = counts[lane]
            bucket["total"] += 1
            if verdict is None:
                bucket["none"] += 1
            elif verdict.startswith("field:"):
                bucket["field"] += 1
            elif verdict == "docid:known-uid":
                bucket["known_uid"] += 1
            else:
                bucket["uid_shaped"] += 1
        rows.append({"probe": probe, "counts": counts})
    return rows


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_rules_report(parsed: dict) -> None:
    print("=" * 78)
    print("① WRITE PATHS — which could accept a write with NO uid")
    print("=" * 78)
    s = summarize_paths(parsed["paths"])
    print(f"  allow statements parsed:        {s['allow_statements']}")
    print(f"  client-writable paths:          {s['client_write_paths']}")
    print(f"  ... requiring request.auth:     {s['write_paths_with_uid']}")
    print(f"  ... NOT requiring request.auth: {s['write_paths_without_uid']}")
    print("\n  Unbound client write paths (create/update/write, no request.auth):")
    for p in parsed["paths"]:
        if p["client_writable"] and not p["requires_auth"]:
            print(f"    {p['path']:<62} allow {', '.join(p['verbs'])}")


def print_data_report(rows: list[dict], known_uids: int) -> None:
    print("\n" + "=" * 78)
    print("② EXISTING RECORDS — how many carry a uid")
    print("=" * 78)
    print(f"  known uids harvested (site_roles ids + club managerUids): {known_uids}")
    print(
        f"\n  {'collection':<24}{'lane':<6}{'total':>7}{'uid(field)':>12}"
        f"{'uid(docid)':>12}{'shaped':>8}{'NO uid':>8}"
    )
    for row in rows:
        for lane in ("prod", "dev"):
            c = row["counts"][lane]
            if not c["total"]:
                continue
            print(
                f"  {row['probe']['collection']:<24}{lane:<6}{c['total']:>7}"
                f"{c['field']:>12}{c['known_uid']:>12}{c['uid_shaped']:>8}{c['none']:>8}"
            )


def totals(rows: list[dict]) -> dict:
    out = {"total": 0, "with_uid": 0, "without_uid": 0, "uid_shaped_only": 0}
    for row in rows:
        for lane in ("prod", "dev"):
            c = row["counts"][lane]
            out["total"] += c["total"]
            out["with_uid"] += c["field"] + c["known_uid"]
            out["uid_shaped_only"] += c["uid_shaped"]
            out["without_uid"] += c["none"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="Parse firestore.rules and stop. Touches no Firestore at all.",
    )
    parser.add_argument("--json-summary", action="store_true", help="One machine-readable line, last. Counts only.")
    args = parser.parse_args()

    parsed = parse_rules(RULES_PATH.read_text(encoding="utf-8"))
    print_rules_report(parsed)

    payload = {"rules": summarize_paths(parsed["paths"])}

    if not args.rules_only:
        db = _firestore_client()
        known = harvest_known_uids(db)
        rows = measure(db, SELF_WRITE_PROBES, known)
        print_data_report(rows, len(known))
        t = totals(rows)
        print("\n" + "=" * 78)
        print("③ THE PHASE-5 NUMBER")
        print("=" * 78)
        print(f"  self-write records measured:    {t['total']}")
        print(f"  carrying a uid (field or id):   {t['with_uid']}")
        print(f"  carrying NO uid:                {t['without_uid']}")
        print(f"  uid-SHAPED id only (heuristic): {t['uid_shaped_only']}")
        payload["records"] = t
        payload["known_uids"] = len(known)
        payload["by_collection"] = [
            {"collection": r["probe"]["collection"], "counts": r["counts"]} for r in rows
        ]

    print(
        "\nREAD-ONLY. No document was written, no rules were deployed, and no "
        "shadow/enforce flag was touched — Phase 5's enforcement is an owner "
        "decision."
    )
    if args.json_summary:
        payload["at"] = datetime.now().isoformat(timespec="seconds")
        print("UID_BINDING_JSON " + json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
