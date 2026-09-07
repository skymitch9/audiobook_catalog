"""
Drive <-> RUNG parity — Phase 4d. REPORT-ONLY, in both directions, forever.

docs/info/ROLES.md §2 fixed the mapping between a Google Drive permission and
a rung on the estate ladder. `scripts/drive_role_parity.py` enforces the half
of it that existed when it was written: `admin` and `moderator` were the only
roles anything stored, so those are the only rungs it can reconcile. Since
then the ladder gained two STORED rungs — `member` and `contributor`
(catalog-platform `apps/auth-worker/src/role-ladder.ts`, `SITE_ROLES = ['member',
'contributor', 'moderator', 'admin']`) — and the reconciler never learned them.

⚠️ UPDATE 2026-09-07 — THE SIBLING SCRIPT HAS NOW LEARNED THEM, so the
paragraph below is history rather than a live finding. drive_role_parity.py
routes every rung through the same tables this module introduced (they now
LIVE there and are re-exported here — see the import block), and it splits
them on one axis: `contributor` joined `moderator`/`admin` as an ENFORCED
rung applied live behind MASS_DRIFT_CAP, while `member` and `guest` are
computed as SHADOW and applied to nothing. That leaves this module as the
per-rung REPORT — the same numbers, partitioned by rung rather than by
problem class, and still writing nothing in either direction.

⚠️ WHAT THAT COST WHILE IT LASTED, and it was not cosmetic. In the sibling
script a person whose `site_roles` doc said `member` or `contributor` matched
no branch and landed in `ok` with the note *"approved (no elevated site role
on file)"* — a sentence that had become FALSE. Two real drifts hid behind it:

  * a `contributor` holding only Drive `reader` — the ladder granted upload,
    Drive never did (reported here as ROLE-ONLY at `contributor`); and
  * a `member` holding Drive `writer` — Drive grants more than any rung the
    ladder handed out (reported here as DRIVE-ONLY at `contributor`).

⚠️ THIS SCRIPT APPLIES NOTHING, EVER, IN EITHER DIRECTION — and there is no
flag that changes that. Reasons, in order:

  1. Drive -> role is report-only FOREVER by owner decision (ROLES.md §2):
     granting a site role is a human act in the admin UI.
  2. Role -> Drive on the NEW rungs is mostly ACCESS-INCREASING (a `member`
     with no Drive permission wants a `reader` grant), and the global rule is
     to act on access-reducing orders and CONFIRM access-increasing ones.
  3. The one access-reducing case (`member` holding `writer`) is a demotion
     of a real person's working access on evidence this reconciler has never
     been run against before. The sibling script's own history is the
     argument: its first live run found a bug the tests had not, and that was
     a run whose plan a human read first.

  A write path here is therefore out of scope by design, not by omission. If
  one is ever wanted it is a separate, owner-approved change, and it inherits
  every rail the sibling script already carries (MASS_DRIFT_CAP, the exception
  list, OWNER_PROTECTED_EMAILS) rather than reinventing them.

SHARED LOADERS, NOT COPIED ONES. Drive, the estate D1 directory and Firestore
`site_roles` are all read through `drive_role_parity`'s existing functions. A
second copy of three readers is a second thing to keep in step with three live
systems, and the failure mode is silent disagreement between two reports.

Usage (from the repo root):
    python scripts/drive_rung_parity.py
    python scripts/drive_rung_parity.py --json-summary

⚠️ On Windows set PYTHONIOENCODING=utf-8 when capturing this script's output
from another process — the report prints em-dashes and ⚠️, and a cp1252 pipe
raises UnicodeEncodeError mid-report. Same trap as the sibling script.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

sys.path.insert(0, str(SCRIPT_DIR))

# ---------------------------------------------------------------------------
# ⚠️ THE LADDER MOVED, 2026-09-07 — it is no longer defined in this file.
#
# The tables and helpers below (ROLE_LADDER, the two mapping tables,
# STORED_SITE_ROLES, rung_rank, rung_from_drive_level, rung_from_site_role)
# were DEFINED here when this module was written, and now live in
# drive_role_parity.py, which is imported below. They are re-exported by this
# import so every existing caller and test keeps working unchanged.
#
# Why they moved: the sibling script learned the same rungs when the ladder
# gained storage, and two copies of a cumulative comparison is exactly the
# near-duplicate rank check the ladder exists to prevent — one call site
# eventually disagrees with the other about who may open a book. This module
# already imported its three loaders from there, so that side is the end of
# the dependency that can hold the tables without a cycle.
#
# The mirror is still checked against catalog-platform's role-ladder.ts by
# tests/test_drive_rung_parity.py, which parses the .ts file; the test imports
# these names from HERE, so it now transitively guards the one real copy.
# ---------------------------------------------------------------------------
from drive_role_parity import (  # noqa: E402,F401  (path inserted just above)
    DRIVE_LEVEL_MIN_RUNG,
    DRIVE_LEVEL_RANK,
    EXCEPTIONS_PATH_DEFAULT,
    FOLDER_ID_DEFAULT,
    OWNER_PROTECTED_EMAILS,
    ROLE_LADDER,
    RUNG_MIN_DRIVE_LEVEL,
    STORED_SITE_ROLES,
    apply_aliases,
    fetch_drive_permissions,
    fetch_estate_directory,
    fetch_site_roles,
    load_exceptions,
    rung_from_drive_level,
    rung_from_site_role,
    rung_rank,
)


def _estate_desc(estate_row, estate_rows) -> str:
    if estate_rows is None:
        return "UNKNOWN (estate directory unreadable this run)"
    if estate_row is None:
        return "not in estate directory"
    flags = []
    if estate_row.get("is_approver"):
        flags.append("approver")
    if estate_row.get("is_devops"):
        flags.append("devops")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    return f"{estate_row['status']}{flag_str}"


# ---------------------------------------------------------------------------
# reconcile() — PURE. Dicts in, dicts out. No Drive, no D1, no Firestore, no
# clock. Same split as the sibling script's plan/fuse pair and for the same
# reason: the judgement is what has to be trustworthy, so the judgement is
# what gets unit-tested.
# ---------------------------------------------------------------------------


def reconcile(
    drive_owner_email: str | None,
    drive_perms: dict[str, dict],
    estate_rows: dict[str, dict] | None,
    site_roles: dict[str, dict],
    exceptions: dict,
    alias_display: dict[str, dict],
) -> dict:
    """Partition every identity into ONE per-rung bucket.

    For each rung R:
      matched     — the ladder says R and Drive grants exactly R's level
      drive_only  — Drive grants R's level and the ladder justifies LESS
                    (access nobody granted on the ladder)
      role_only   — the ladder says R and Drive grants LESS than R needs
                    (a rung Drive never learned about)

    ⚠️ THE COMPARISON IS ON DRIVE LEVELS, NOT ON RUNG RANKS, and getting that
    backwards is the first bug this function had. Drive has three levels to
    spend on six rungs, so `moderator` and `admin` both map to `writer`: an
    admin holding Drive `writer` is CORRECT, while a rank comparison
    (admin=4 > contributor=2) calls it drift and would report every admin in
    the estate as under-granted forever. What the mapping actually asserts is
    a FLOOR — "this rung needs at least this level" — so the comparison is
    `RUNG_MIN_DRIVE_LEVEL[role_rung]` against the level Drive really grants.

    Every identity lands in exactly one bucket of exactly one rung, so the
    counts add up to the population. A person counted twice inflates drift; a
    person counted zero times hides it, which is worse.
    """
    pending = exceptions.get("pending_outreach") or {}
    permanent = exceptions.get("permanent_exceptions") or {}

    by_rung: dict[str, dict[str, list]] = {
        rung: {"matched": [], "drive_only": [], "role_only": []} for rung in ROLE_LADDER
    }
    unrecognised: list[dict] = []
    non_approved: list[dict] = []

    all_emails = set(drive_perms) | set(site_roles)
    if estate_rows is not None:
        all_emails |= set(estate_rows)

    for email in sorted(all_emails):
        drive_row = drive_perms.get(email)
        drive_level = (drive_row or {}).get("role") or "none"
        estate_row = estate_rows.get(email) if estate_rows is not None else None
        site_role_row = site_roles.get(email)
        site_role = (site_role_row or {}).get("role")

        alias = alias_display.get(email)
        row = {
            "raw_email": email,
            "drive_account": alias["drive_account"] if alias else email,
            "email": (
                f"{email} (alias: Drive perm held as {alias['drive_account']})"
                if alias
                else email
            ),
            "drive_level": drive_level,
            "site_role": site_role or "(none)",
            "estate": _estate_desc(estate_row, estate_rows),
            "excepted": None,
        }

        # ---- the rails, in the order the sibling script applies them --------
        # These do NOT remove a person from the counts: the partition has to
        # hold or the totals stop meaning anything. They mark the row so the
        # ACTIONABLE number never includes somebody the owner already decided
        # about.
        if drive_owner_email and email == drive_owner_email and drive_level == "owner":
            row["excepted"] = "drive_folder_owner"
        elif email in OWNER_PROTECTED_EMAILS:
            row["excepted"] = "owner_protected"
        elif email in pending:
            row["excepted"] = "pending_outreach"
        elif email in permanent:
            row["excepted"] = "permanent"

        role_rung = rung_from_site_role(site_role)
        if role_rung is None:
            # ⚠️ Reported, not bucketed. A value this module does not
            # recognise cannot be reconciled against anything, and putting it
            # at `guest` would report a stale-vocabulary row as ordinary
            # drift and invite somebody to "fix" it by changing Drive.
            unrecognised.append({**row, "reason": f"unrecognised stored role {site_role!r}"})
            continue

        drive_rung = rung_from_drive_level(drive_level)
        row["role_rung"] = role_rung
        row["drive_rung"] = drive_rung

        if estate_rows is not None and role_rung != "guest":
            status = (estate_row or {}).get("status")
            if status != "approved":
                non_approved.append({**row, "estate_status": status})

        needed = RUNG_MIN_DRIVE_LEVEL[role_rung]
        have_rank, need_rank = DRIVE_LEVEL_RANK[drive_level], DRIVE_LEVEL_RANK[needed]
        row["drive_level_needed"] = needed
        if have_rank == need_rank:
            by_rung[role_rung]["matched"].append(row)
        elif have_rank > need_rank:
            row["difference"] = (
                f"Drive grants {drive_level} (implies {drive_rung} or above); "
                f"the ladder says {role_rung}, which needs only {needed}."
            )
            by_rung[drive_rung]["drive_only"].append(row)
        else:
            row["difference"] = (
                f"Ladder says {role_rung}, which needs Drive {needed}; "
                f"Drive grants {drive_level}."
            )
            by_rung[role_rung]["role_only"].append(row)

    return {
        "by_rung": by_rung,
        "unrecognised_site_roles": unrecognised,
        "role_rungs_without_approved_estate": non_approved,
        "estate_unreadable": estate_rows is None,
    }


def summarize(result: dict) -> dict:
    """PURE. Counts only — no emails. ⚠️ Keep it that way: the sibling
    script's `--json-summary` carries emails because the pipeline log is
    local, and the /status dashboard is fed only counts. Same split."""
    by_rung = {
        rung: {bucket: len(rows) for bucket, rows in buckets.items()}
        for rung, buckets in result["by_rung"].items()
    }
    total = 0
    actionable = 0
    drive_only = 0
    role_only = 0
    for buckets in result["by_rung"].values():
        for bucket, rows in buckets.items():
            total += len(rows)
            if bucket == "matched":
                continue
            if bucket == "drive_only":
                drive_only += len(rows)
            else:
                role_only += len(rows)
            actionable += sum(1 for r in rows if not r["excepted"])
    return {
        "by_rung": by_rung,
        "total_rows": total + len(result["unrecognised_site_roles"]),
        "bucketed_rows": total,
        "drive_only": drive_only,
        "role_only": role_only,
        "actionable": actionable,
        "unrecognised_site_roles": len(result["unrecognised_site_roles"]),
        "role_rungs_without_approved_estate": len(
            result["role_rungs_without_approved_estate"]
        ),
        "estate_unreadable": result["estate_unreadable"],
    }


# ---------------------------------------------------------------------------
# Report printing
# ---------------------------------------------------------------------------


def print_report(folder_id, drive_owner_email, drive_perms, estate_rows, estate_error,
                 site_roles, exceptions, result, alias_display) -> None:
    print("=" * 78)
    print("DRIVE <-> RUNG PARITY — REPORT-ONLY (Phase 4d, the new rungs)")
    print(f"Folder: {folder_id}   Run at: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)
    print(
        f"\nDrive permissions read: {len(drive_perms)} non-owner"
        f"{' + folder owner' if drive_owner_email else ''}"
    )
    print(
        "Estate directory (D1 estate_user): "
        + (
            f"UNREADABLE — {estate_error}"
            if estate_rows is None
            else f"{len(estate_rows)} rows"
        )
    )
    print(f"Firestore site_roles: {len(site_roles)} doc(s)")
    print(
        f"Exception list measured_at: {exceptions['measured_at']} "
        f"({len(exceptions['pending_outreach'])} pending_outreach, "
        f"{len(exceptions['permanent_exceptions'])} permanent_exceptions)"
    )
    if alias_display:
        print(f"\nALIASES FOLDED ({len(alias_display)}):")
        for site, info in alias_display.items():
            print(f"  {site}  <->  {info['drive_account']}   ({info['note'] or 'no note'})")

    print("\n" + "-" * 78)
    print("PER-RUNG PARITY")
    print("-" * 78)
    print(f"  {'rung':<13} {'needs Drive':<12} {'matched':>8} {'Drive-only':>11} {'role-only':>10}")
    for rung in ROLE_LADDER:
        b = result["by_rung"][rung]
        print(
            f"  {rung:<13} {RUNG_MIN_DRIVE_LEVEL[rung]:<12} "
            f"{len(b['matched']):>8} {len(b['drive_only']):>11} {len(b['role_only']):>10}"
        )

    for rung in ROLE_LADDER:
        for bucket, title in (
            ("drive_only", "DRIVE-ONLY — Drive grants this rung, the ladder does not"),
            ("role_only", "ROLE-ONLY — the ladder grants this rung, Drive does not"),
        ):
            rows = result["by_rung"][rung][bucket]
            if not rows:
                continue
            print(f"\n{rung.upper()} · {title}  ({len(rows)})")
            for r in rows:
                mark = f"  [EXCEPTED: {r['excepted']}]" if r["excepted"] else ""
                print(
                    f"  {r['email']:<40} drive={r['drive_level']:<7} "
                    f"role={r['site_role']:<12} estate={r['estate']}{mark}"
                )
                print(f"      {r['difference']}")

    if result["unrecognised_site_roles"]:
        print("\n" + "!" * 78)
        print("!! UNRECOGNISED STORED ROLES — reported, never coerced onto a rung")
        print("!" * 78)
        for r in result["unrecognised_site_roles"]:
            print(f"  {r['email']:<40} role={r['site_role']}   {r['reason']}")

    if result["role_rungs_without_approved_estate"]:
        print(
            f"\nLADDER RUNG HELD WITHOUT AN APPROVED ESTATE ROW "
            f"({len(result['role_rungs_without_approved_estate'])}) — the estate "
            "directory is the gate that admits people; D1 wins on a disagreement "
            "(ROLES.md §1f):"
        )
        for r in result["role_rungs_without_approved_estate"]:
            print(f"  {r['email']:<40} role={r['site_role']:<12} estate={r['estate']}")

    s = summarize(result)
    print("\n" + "=" * 78)
    print("COUNTS")
    print("=" * 78)
    print(f"  rows bucketed:                   {s['bucketed_rows']}")
    print(f"  Drive-only (all rungs):          {s['drive_only']}")
    print(f"  role-only  (all rungs):          {s['role_only']}")
    print(f"  ACTIONABLE (drift, not excepted):{s['actionable']:>4}")
    print(f"  unrecognised stored roles:       {s['unrecognised_site_roles']}")
    print(f"  rung held, estate not approved:  {s['role_rungs_without_approved_estate']}")
    print("\nREPORT-ONLY. Nothing was written to Drive, Firestore or D1, and this "
          "script has no flag that would.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folder-id", default=FOLDER_ID_DEFAULT)
    parser.add_argument("--exceptions-file", default=str(EXCEPTIONS_PATH_DEFAULT))
    parser.add_argument(
        "--json-summary",
        action="store_true",
        help="Print one machine-readable line, 'RUNG_PARITY_JSON {...}', as the "
        "LAST line of output. COUNTS ONLY — no emails (see summarize()).",
    )
    args = parser.parse_args()

    exceptions = load_exceptions(Path(args.exceptions_file))

    print("Reading Drive permissions...")
    drive_owner_email, drive_perms, _non_user = fetch_drive_permissions(args.folder_id)

    print("Reading estate directory (D1 via wrangler)...")
    estate_rows, estate_error = fetch_estate_directory()
    if estate_rows is None:
        print(f"  WARNING: estate directory unreadable — {estate_error}")
        print("  Degrading gracefully: estate status will show as UNKNOWN.")

    print("Reading Firestore site_roles...")
    site_roles = fetch_site_roles()

    drive_perms, estate_rows, site_roles, alias_display = apply_aliases(
        drive_perms, estate_rows, site_roles, exceptions["alias_pairs"]
    )

    result = reconcile(
        drive_owner_email, drive_perms, estate_rows, site_roles, exceptions, alias_display
    )
    print_report(
        args.folder_id, drive_owner_email, drive_perms, estate_rows, estate_error,
        site_roles, exceptions, result, alias_display,
    )

    if args.json_summary:
        payload = summarize(result)
        payload["at"] = datetime.now().isoformat(timespec="seconds")
        print("RUNG_PARITY_JSON " + json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
