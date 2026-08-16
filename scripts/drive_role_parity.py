"""
Drive <-> Role Parity Auditor — docs/info/ROLES.md §2.

Owner's words: "go into google drive and set everyones rights to match their
role... I want this always to match. We can also do this backwards and make
sure all the people with contribute in drive are contributors in ours."

⚠️ Roles are the source of truth; Drive is downstream (ROLES.md §2). This
script REPORTS drift in both directions. It does not fix anything on its own
run, ever — see the safety model below.

Three sources, three different trust levels:
  (a) Drive permissions on the GABI folder — ground truth for who can open
      Drive today. Read via scripts/drive_auth.py (the shared OAuth helper,
      full auth/drive scope, token at scripts/token.json). REQUIRED; the
      script exits if this can't be read.
  (b) The estate directory — ground truth for who has a household account
      and whether it is approved/pending/revoked. Read via `wrangler d1
      execute estate_auth --remote` from the sibling catalog-platform repo's
      apps/auth-worker (needs `npx wrangler` authenticated there). DEGRADES
      GRACEFULLY: if wrangler/D1 is unreachable, the script says so and
      reports Drive + Firestore only, with estate status marked UNKNOWN
      rather than invented.
  (c) audiobook site roles — Firestore site_roles/{uid}, admin|moderator
      only (the three-tier model). Read via firebase_admin + the service
      account (scripts/firebase_service_account.json or
      $FIREBASE_SERVICE_ACCOUNT), same plumbing as seed_site_admin.py.
      REQUIRED; the script exits if this can't be read.

⚠️ IMPORTANT GAP, not a bug: ROLES.md's role ladder (viewer < reader <
contributor < moderator < admin < owner) is "DESIGN, partially built" — only
admin/moderator are actually stored anywhere (Firestore site_roles). reader
and contributor have NO per-user storage yet. So "approved in the estate
directory, no site_roles doc" cannot be read as "role = reader" or "role =
viewer" with certainty — it is reported as exactly what it is (approved
membership, no elevated role on file) and never silently upgraded to a role
this script invented.

THE EXCEPTION LIST (docs/access/drive-exceptions.json) — owner order
2026-08-16: some Drive-only people are a known, temporary migration queue
(the owner is contacting them to create estate accounts) and must NOT be
revoked or overwritten while that outreach is in flight. This file is the
one MANDATORY input besides the three sources above: if it is missing, this
script FAILS LOUDLY rather than silently treating every Drive-only person as
fair game (a silently-ignored exception list would revoke real people's
access — see the header comment in that file). The goal state is an empty
`pending_outreach` array; `permanent_exceptions` holds carve-outs the owner
has said are permanent (today: empty — see the file's own note on why
Mitchlandtv@gmail.com was REMOVED from it, 2026-08-16: that account is now a
full estate member and reconciles by the normal rules instead).

SAFETY MODEL:
  - Dry-run, report-only, is the ONLY thing that happens without --commit.
  - --commit alone is refused: it must be paired with exactly one direction
    flag, so "which way parity is enforced" is always a conscious choice:
      --apply-to-drive   roles win; Drive permissions get created/changed/
                         removed to match. Still refuses to touch:
                           * the folder OWNER permission (role == 'owner')
                           * any 'anyone' / 'domain' (link-sharing) entry
                           * OWNER_PROTECTED_EMAILS (both of the owner's own
                             accounts — see below)
                           * anyone in drive-exceptions.json
                             (pending_outreach or permanent_exceptions)
                         and SKIPS (never guesses) any row where the
                         estate side is merely "approved, no elevated role"
                         — that tier isn't implemented, so there is no role
                         to enforce yet; forcing a level here would be
                         exactly the naive reconciliation ROLES.md warns
                         against.
      --apply-to-roles   report-only, always. Prints what role each Drive
                         permission level implies (writer -> contributor,
                         reader -> reader) as a suggestion for the owner to
                         act on by hand in the admin UI. NEVER writes
                         Firestore — granting a site role is a human act,
                         full stop.
  - OWNER_PROTECTED_EMAILS below are never demoted or removed, regardless of
    classification or exception-list state — the owner has two accounts
    (nbaslamking@gmail.com, mitchlandtv@gmail.com) and both are owner-level
    (2026-08-16 order: "give it all permissions my main has").

Usage (from the repo root):
    python scripts/drive_role_parity.py                       # report only
    python scripts/drive_role_parity.py --commit --apply-to-drive   # mutate
    python scripts/drive_role_parity.py --commit --apply-to-roles   # report
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# Windows consoles default to a legacy codepage that mangles the em-dashes
# used throughout this report ("—" -> "�"). Force UTF-8 stdout/stderr so the
# report is legible in PowerShell / cmd.exe as well as UTF-8 terminals.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
# Sibling repo — the estate auth Worker owns the D1 database this script reads.
# Layout: .../vs-code-repos/bookbuddy/audiobook_catalog (REPO_ROOT) and
#         .../vs-code-repos/catalog-platform/apps/auth-worker — two levels
# up from REPO_ROOT (out of bookbuddy/), not one.
AUTH_WORKER_DIR = REPO_ROOT.parent.parent / "catalog-platform" / "apps" / "auth-worker"

FOLDER_ID_DEFAULT = "1yZHU_UryCZkuhg9zFzu5uOadx3NI0FJv"
EXCEPTIONS_PATH_DEFAULT = REPO_ROOT / "docs" / "access" / "drive-exceptions.json"

# The owner's own accounts. Never demoted, never removed, in any mutating
# mode, regardless of what classification or the exception list say.
# nbaslamking@gmail.com is also the Drive folder's `role: owner` permission
# (refused separately, unconditionally, below). mitchlandtv@gmail.com is the
# owner's second account (owns the GCP project); owner order 2026-08-16:
# "give it all permissions my main has" — full member, owner-level trust.
OWNER_PROTECTED_EMAILS = {"nbaslamking@gmail.com", "mitchlandtv@gmail.com"}

# ROLES.md §2 — the mapping a reconciler enforces.
#   Drive permission  -> estate role
#   owner              -> owner
#   writer             -> contributor (or above)
#   reader              -> reader (or above)
#   (none)              -> viewer
DRIVE_LEVEL_RANK = {"none": 0, "reader": 1, "writer": 2, "owner": 3}
DRIVE_LEVEL_TO_ESTATE_LABEL = {
    "owner": "owner",
    "writer": "contributor (or above)",
    "reader": "reader (or above)",
    "none": "viewer",
}
DRIVE_LEVEL_TO_IMPLIED_ROLE = {
    "owner": "owner",
    "writer": "contributor",
    "reader": "reader",
    "none": "viewer",
}


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# ---------------------------------------------------------------------------
# Source (a): Drive permissions
# ---------------------------------------------------------------------------

def fetch_drive_permissions(folder_id: str):
    """Return (owner_email, user_perms, non_user_perms).

    user_perms: {email -> {"role": "reader"|"writer"|"owner", "id": ..., "displayName": ...}}
    non_user_perms: list of permission dicts whose type is NOT 'user'
                     (type: anyone / domain / group) — never touched, always
                     reported loudly.

    Fatal on failure: Drive is a required source (the auditor is useless
    without it), so this exits rather than degrading.
    """
    sys.path.insert(0, str(SCRIPT_DIR))
    import drive_auth  # noqa: E402  (path inserted just above)
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds = drive_auth.get_credentials()
    if not creds:
        sys.exit(
            "FATAL: could not obtain Google Drive credentials via "
            "scripts/drive_auth.py. Run `python scripts/drive_auth.py` "
            "once to authorize, then retry."
        )

    service = build("drive", "v3", credentials=creds)
    try:
        resp = (
            service.permissions()
            .list(
                fileId=folder_id,
                fields="permissions(id,emailAddress,role,type,displayName,domain)",
                supportsAllDrives=True,
            )
            .execute()
        )
    except HttpError as e:
        sys.exit(f"FATAL: Drive permissions.list failed for folder {folder_id}: {e}")

    user_perms: dict[str, dict] = {}
    non_user_perms: list[dict] = []
    owner_email = None

    for p in resp.get("permissions", []):
        if p.get("type") != "user":
            non_user_perms.append(p)
            continue
        email = normalize_email(p.get("emailAddress", ""))
        if not email:
            non_user_perms.append(p)
            continue
        user_perms[email] = {
            "role": p.get("role"),
            "id": p.get("id"),
            "displayName": p.get("displayName", ""),
        }
        if p.get("role") == "owner":
            owner_email = email

    return owner_email, user_perms, non_user_perms


# ---------------------------------------------------------------------------
# Source (b): estate directory (D1, via wrangler in the sibling repo)
# ---------------------------------------------------------------------------

def fetch_estate_directory():
    """Return (dict[email -> row] | None, error_message | None).

    Degrades gracefully: any failure (wrangler missing, not authenticated,
    D1 unreachable, unexpected output shape) returns (None, reason) instead
    of raising, so the rest of the report can still run with estate status
    marked UNKNOWN rather than invented.
    """
    if not AUTH_WORKER_DIR.exists():
        return None, f"sibling repo not found at {AUTH_WORKER_DIR}"

    cmd = [
        "npx",
        "wrangler",
        "d1",
        "execute",
        "estate_auth",
        "--remote",
        "--command",
        "SELECT email, status, is_approver, is_devops FROM estate_user;",
        "--json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(AUTH_WORKER_DIR),
            capture_output=True,
            text=True,
            shell=True,
            timeout=60,
        )
    except FileNotFoundError:
        return None, "npx/wrangler not found on PATH"
    except subprocess.TimeoutExpired:
        return None, "wrangler d1 execute timed out after 60s"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = detail[-1] if detail else "no output"
        return None, f"wrangler exited {proc.returncode}: {detail}"

    stdout = proc.stdout.strip()
    start = stdout.find("[")
    if start == -1:
        return None, "wrangler produced no JSON on stdout"
    try:
        parsed = json.loads(stdout[start:])
    except json.JSONDecodeError as e:
        return None, f"could not parse wrangler JSON output: {e}"

    if not parsed or not parsed[0].get("success"):
        return None, f"D1 query did not report success: {parsed}"

    rows = {}
    for row in parsed[0].get("results", []):
        email = normalize_email(row.get("email", ""))
        if email:
            rows[email] = {
                "status": row.get("status"),
                "is_approver": bool(row.get("is_approver")),
                "is_devops": bool(row.get("is_devops")),
            }
    return rows, None


# ---------------------------------------------------------------------------
# Source (c): audiobook site roles (Firestore site_roles/{uid})
# ---------------------------------------------------------------------------

def _firebase_app():
    import os

    key_path = Path(
        os.getenv("FIREBASE_SERVICE_ACCOUNT")
        or (SCRIPT_DIR / "firebase_service_account.json")
    )
    if not key_path.exists():
        sys.exit(
            f"FATAL: no Firebase service account key at {key_path} "
            "(set FIREBASE_SERVICE_ACCOUNT or place the JSON there) — "
            "same requirement as scripts/seed_site_admin.py."
        )
    import firebase_admin
    from firebase_admin import credentials

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(str(key_path)))
    return firebase_admin


def fetch_site_roles():
    """Return dict[email -> {"role": ..., "uid": ..., "displayName": ...}].

    Fatal on failure: this is one of the two role-bearing sources named
    explicitly in the brief, required like Drive.
    """
    _firebase_app()
    from firebase_admin import firestore

    db = firestore.client()
    try:
        docs = list(db.collection("site_roles").stream())
    except Exception as e:  # noqa: BLE001 — surfacing any Firestore failure plainly
        sys.exit(f"FATAL: could not read Firestore site_roles: {e}")

    roles = {}
    for d in docs:
        data = d.to_dict() or {}
        email = normalize_email(data.get("email", ""))
        if email:
            roles[email] = {
                "role": data.get("role"),
                "uid": d.id,
                "displayName": data.get("displayName", ""),
            }
    return roles


# ---------------------------------------------------------------------------
# The exception list — mandatory input, never defaulted to empty
# ---------------------------------------------------------------------------

def load_exceptions(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"FATAL: exception list not found at {path}.\n"
            "This script REFUSES to run without it: a missing exception "
            "list would silently treat every Drive-only person as fair "
            "game for revocation, including people the owner has "
            "explicitly told us to leave alone during outreach. Create "
            "the file (see docs/access/DRIVE_ROLE_PARITY.md for its "
            "shape) or pass --exceptions-file to point at it."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"FATAL: {path} is not valid JSON: {e}")

    pending = {normalize_email(e["email"]): e for e in data.get("pending_outreach", [])}
    permanent = {
        normalize_email(e["email"]): e for e in data.get("permanent_exceptions", [])
    }
    alias_pairs = [
        {
            "site_account": normalize_email(p["site_account"]),
            "drive_account": normalize_email(p["drive_account"]),
            "note": p.get("note", ""),
        }
        for p in data.get("aliases", {}).get("pairs", [])
    ]
    return {
        "raw": data,
        "measured_at": data.get("measured_at"),
        "pending_outreach": pending,
        "permanent_exceptions": permanent,
        "estate_members_without_drive": {
            normalize_email(e)
            for e in data.get("estate_members_without_drive", {}).get("emails", [])
        },
        "alias_pairs": alias_pairs,
    }


# ---------------------------------------------------------------------------
# Aliases: co-emails — one person, two addresses (owner 2026-08-16: "sylvenix
# is the same as sparkling ember"). A Drive permission held under EITHER
# address satisfies the site account held under the other. Folded BEFORE
# classification so an aliased person never appears as both drive-only and
# role-only. Canonical identity = the site_account side of the pair (that's
# the estate directory's join key); the raw drive-side address is kept only
# for display, so the folding stays auditable.
# ---------------------------------------------------------------------------

def apply_aliases(drive_perms, estate_rows, site_roles, alias_pairs):
    alias_map: dict[str, str] = {}
    alias_display: dict[str, dict] = {}
    for pair in alias_pairs:
        site = pair["site_account"]
        drive = pair["drive_account"]
        alias_map[drive] = site
        alias_map[site] = site
        alias_display[site] = {
            "site_account": site,
            "drive_account": drive,
            "note": pair["note"],
        }

    def canon(email: str) -> str:
        return alias_map.get(email, email)

    def fold(mapping):
        if mapping is None:
            return None
        out: dict[str, dict] = {}
        for email, row in mapping.items():
            out[canon(email)] = row
        return out

    return fold(drive_perms), fold(estate_rows), fold(site_roles), alias_display


def days_since(iso_date: str | None) -> str:
    if not iso_date:
        return "unknown"
    try:
        d = date.fromisoformat(iso_date)
    except ValueError:
        return "unknown"
    delta = (date.today() - d).days
    if delta <= 0:
        return f"since {iso_date} (added today or in the future)"
    return f"since {iso_date} ({delta} day{'s' if delta != 1 else ''} ago)"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(
    drive_owner_email: str | None,
    drive_perms: dict[str, dict],
    estate_rows: dict[str, dict] | None,
    site_roles: dict[str, dict],
    exceptions: dict,
    alias_display: dict[str, dict],
):
    """Build the reconciliation report as a dict of buckets."""
    pending_outreach = exceptions["pending_outreach"]
    permanent_exceptions = exceptions["permanent_exceptions"]
    estate_only_gap = exceptions["estate_members_without_drive"]

    all_emails = set(drive_perms) | set(site_roles)
    if estate_rows is not None:
        all_emails |= set(estate_rows)

    def display_email(email: str) -> str:
        alias = alias_display.get(email)
        if not alias:
            return email
        return f"{email} (alias: Drive perm held as {alias['drive_account']})"

    buckets = {
        "owner_protected": [],
        "excepted_pending_outreach": [],
        "excepted_permanent": [],
        "drive_only_untriaged": [],
        "role_only": [],
        "mismatch": [],
        "ok": [],
    }

    for email in sorted(all_emails):
        drive_row = drive_perms.get(email)
        drive_level = drive_row["role"] if drive_row else "none"
        estate_row = estate_rows.get(email) if estate_rows is not None else None
        site_role_row = site_roles.get(email)
        site_role = site_role_row["role"] if site_role_row else None

        is_owner_protected = email in OWNER_PROTECTED_EMAILS

        # ---- Drive folder owner: never a "finding", always its own line ----
        if email == drive_owner_email:
            buckets["owner_protected"].append(
                {
                    "email": display_email(email),
                    "drive": "owner",
                    "estate": _estate_desc(estate_row, estate_rows),
                    "site_role": site_role or "(none)",
                    "note": "Drive folder OWNER — never touched by this script, under any flag.",
                }
            )
            continue

        # ---- the owner's other protected account ----
        if is_owner_protected:
            action = "PROTECTED — never demoted/removed regardless of classification."
        else:
            action = None

        # ---- exception list: pending outreach ----
        if email in pending_outreach and drive_level != "none":
            exc = pending_outreach[email]
            buckets["excepted_pending_outreach"].append(
                {
                    "email": display_email(email),
                    "drive": drive_level,
                    "estate": _estate_desc(estate_row, estate_rows),
                    "implies_role": exc.get("implies_role", DRIVE_LEVEL_TO_IMPLIED_ROLE.get(drive_level)),
                    "on_list": days_since(exceptions["measured_at"]),
                    "action": "NO ACTION — migration queue. Owner is contacting this "
                    "person to create an estate account. Never revoke/demote "
                    "while pending_outreach in drive-exceptions.json.",
                }
            )
            continue

        # ---- exception list: permanent exceptions ----
        if email in permanent_exceptions and drive_level != "none":
            exc = permanent_exceptions[email]
            buckets["excepted_permanent"].append(
                {
                    "email": display_email(email),
                    "drive": drive_level,
                    "estate": _estate_desc(estate_row, estate_rows),
                    "reason": exc.get("reason", "(no reason recorded)"),
                    "on_list": days_since(exceptions["measured_at"]),
                    "action": "NO ACTION — permanent carve-out per drive-exceptions.json.",
                }
            )
            continue

        # ---- estate status unknown (D1 unreadable) ----
        if estate_rows is None:
            if drive_level != "none":
                buckets["mismatch"].append(
                    {
                        "email": display_email(email),
                        "drive": drive_level,
                        "estate": "UNKNOWN — estate directory unreadable this run",
                        "difference": "Cannot classify without estate directory status.",
                        "action": (action or "Re-run once D1 is reachable before drawing conclusions.")
                    }
                )
            continue

        status = estate_row["status"] if estate_row else None
        site_role_implies_writer = site_role in ("admin", "moderator")

        # ---- not in estate directory at all, has Drive access, NOT excepted ----
        if estate_row is None and drive_level != "none":
            buckets["drive_only_untriaged"].append(
                {
                    "email": display_email(email),
                    "drive": drive_level,
                    "estate": "not in estate directory",
                    "implies_role": DRIVE_LEVEL_TO_IMPLIED_ROLE.get(drive_level),
                    "action": (
                        action
                        or "NOT in drive-exceptions.json and NOT in the estate directory. "
                        "Add to drive-exceptions.json (pending_outreach) if outreach is "
                        "wanted, or revoke Drive access directly — decide per person."
                    ),
                }
            )
            continue

        # ---- pending/revoked estate status but still holds Drive access ----
        if estate_row is not None and status in ("pending", "revoked") and drive_level != "none":
            buckets["mismatch"].append(
                {
                    "email": display_email(email),
                    "drive": drive_level,
                    "estate": f"{status} (estate_user.status)",
                    "difference": f"Estate status is '{status}' but Drive still grants "
                    f"{drive_level} access.",
                    "action": action
                    or (
                        f"Revoke Drive access (status={status}), or approve them in the "
                        "estate directory if access should continue."
                    ),
                }
            )
            continue

        # ---- approved + explicit site role (admin/moderator implies contributor+) ----
        if estate_row is not None and status == "approved" and site_role_implies_writer:
            if drive_level in ("writer", "owner"):
                note = "Drive level matches the implied minimum for this site role."
                if is_owner_protected:
                    note += " [OWNER-PROTECTED account]"
                buckets["ok"].append(
                    {
                        "email": display_email(email),
                        "drive": drive_level,
                        "estate": f"approved, site_roles={site_role} (implies contributor+)",
                        "note": note,
                    }
                )
            else:
                buckets["mismatch"].append(
                    {
                        "email": display_email(email),
                        "drive": drive_level,
                        "estate": f"approved, site_roles={site_role} (implies contributor+ -> Drive writer)",
                        "difference": f"Site role implies Drive writer; actual Drive level is "
                        f"'{drive_level}'.",
                        "action": action or f"Upgrade Drive permission to writer to match site role {site_role}.",
                    }
                )
            continue

        # ---- approved, no elevated site role, no Drive access ----
        if estate_row is not None and status == "approved" and drive_level == "none":
            in_gap_list = email in estate_only_gap
            buckets["role_only"].append(
                {
                    "email": display_email(email),
                    "drive": "none",
                    "estate": "approved (no elevated site role on file)",
                    "note": (
                        "Also listed in drive-exceptions.json's estate_members_without_drive."
                        if in_gap_list
                        else "Not in drive-exceptions.json's mirror list — worth adding for visibility."
                    ),
                    "action": action
                    or "No Drive access despite estate membership. reader/contributor "
                    "role storage does not exist yet, so this can't be auto-graded — "
                    "owner decides whether this person should get Drive reader access.",
                }
            )
            continue

        # ---- approved, no elevated role, DOES have Drive access: no positive
        #      conflict, but also no stored role to verify the level against ----
        if estate_row is not None and status == "approved" and drive_level != "none":
            buckets["ok"].append(
                {
                    "email": display_email(email),
                    "drive": drive_level,
                    "estate": "approved (no elevated site role on file)",
                    "note": (
                        f"Consistent, not verified: approved member already has Drive "
                        f"{drive_level}. reader/contributor role storage isn't built yet, "
                        f"so this can't be confirmed against a stored role — Drive is the "
                        f"de facto record for now. Implied role once storage exists: "
                        f"{DRIVE_LEVEL_TO_IMPLIED_ROLE.get(drive_level)}."
                    ),
                }
            )
            continue

        # ---- fallback: nothing matched (should be rare) ----
        buckets["mismatch"].append(
            {
                "email": display_email(email),
                "drive": drive_level,
                "estate": _estate_desc(estate_row, estate_rows),
                "difference": "Unclassified combination — inspect by hand.",
                "action": action or "Manual review needed.",
            }
        )

    return buckets


def _estate_desc(estate_row, estate_rows):
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
# Report printing
# ---------------------------------------------------------------------------

def print_report(
    folder_id: str,
    drive_owner_email: str | None,
    drive_perms: dict,
    non_user_perms: list,
    estate_rows,
    estate_error,
    site_roles: dict,
    exceptions: dict,
    buckets: dict,
    alias_display: dict,
):
    print("=" * 78)
    print("DRIVE <-> ROLE PARITY AUDIT")
    print(f"Folder: {folder_id}   Run at: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 78)

    print(f"\nDrive permissions read: {len(drive_perms) + (1 if drive_owner_email else 0)}"
          f" total ({len(drive_perms)} non-owner + folder owner)")
    print(f"Estate directory (D1 estate_user): "
          f"{'UNREADABLE — ' + estate_error if estate_rows is None else str(len(estate_rows)) + ' rows'}")
    print(f"Firestore site_roles: {len(site_roles)} doc(s)")
    print(f"Exception list measured_at: {exceptions['measured_at']} "
          f"({len(exceptions['pending_outreach'])} pending_outreach, "
          f"{len(exceptions['permanent_exceptions'])} permanent_exceptions)")

    if alias_display:
        print(f"\nALIASES FOLDED — co-emails treated as one identity ({len(alias_display)}):")
        for site, info in alias_display.items():
            print(f"  {site}  <->  {info['drive_account']}   ({info['note'] or 'no note'})")
    else:
        print("\nNo alias pairs configured in drive-exceptions.json.")

    if non_user_perms:
        print("\n" + "!" * 78)
        print("!! NON-USER PERMISSIONS FOUND (type != 'user') — REFUSING TO TOUCH THESE")
        print("!" * 78)
        for p in non_user_perms:
            print(f"  type={p.get('type')} role={p.get('role')} "
                  f"domain={p.get('domain', '')} id={p.get('id')}")
    else:
        print("\nNo 'anyone'/'domain'/'group' permissions found (good — no link sharing).")

    def hdr(title, count):
        print(f"\n{'-' * 78}\n{title}  ({count})\n{'-' * 78}")

    hdr("OWNER — PROTECTED, never touched", len(buckets["owner_protected"]))
    for r in buckets["owner_protected"]:
        print(f"  {r['email']:<32} drive={r['drive']:<8} estate={r['estate']:<20} "
              f"site_role={r['site_role']:<10} {r['note']}")

    hdr("EXCEPTED — pending outreach (migration queue, do not touch)",
        len(buckets["excepted_pending_outreach"]))
    for r in buckets["excepted_pending_outreach"]:
        print(f"  {r['email']:<32} drive={r['drive']:<8} estate={r['estate']:<25} "
              f"implies={r['implies_role']:<12} on list {r['on_list']}")
        print(f"      -> {r['action']}")

    hdr("EXCEPTED — permanent exceptions", len(buckets["excepted_permanent"]))
    if not buckets["excepted_permanent"]:
        print("  (none)")
    for r in buckets["excepted_permanent"]:
        print(f"  {r['email']:<32} drive={r['drive']:<8} estate={r['estate']:<25} "
              f"on list {r['on_list']}")
        print(f"      reason: {r['reason']}")
        print(f"      -> {r['action']}")

    hdr("DRIVE-ONLY, UNTRIAGED — has Drive, no estate account, NOT in exception list",
        len(buckets["drive_only_untriaged"]))
    if not buckets["drive_only_untriaged"]:
        print("  (none — every Drive-only person is accounted for in drive-exceptions.json)")
    for r in buckets["drive_only_untriaged"]:
        print(f"  {r['email']:<32} drive={r['drive']:<8} estate={r['estate']:<25} "
              f"implies={r['implies_role']}")
        print(f"      -> {r['action']}")

    hdr("ROLE-ONLY — estate member, no Drive access at all", len(buckets["role_only"]))
    for r in buckets["role_only"]:
        print(f"  {r['email']:<32} drive={r['drive']:<8} estate={r['estate']}")
        print(f"      note: {r['note']}")
        print(f"      -> {r['action']}")

    hdr("MISMATCH — both sides present, wrong level", len(buckets["mismatch"]))
    if not buckets["mismatch"]:
        print("  (none)")
    for r in buckets["mismatch"]:
        print(f"  {r['email']:<32} drive={r['drive']:<8} estate={r['estate']}")
        print(f"      difference: {r['difference']}")
        print(f"      -> {r['action']}")

    hdr("OK — no actionable drift", len(buckets["ok"]))
    for r in buckets["ok"]:
        print(f"  {r['email']:<32} drive={r['drive']:<8} estate={r['estate']}")
        print(f"      {r['note']}")

    print("\n" + "=" * 78)
    print("COUNTS BY PROBLEM CLASS")
    print("=" * 78)
    print(f"  owner (protected):              {len(buckets['owner_protected'])}")
    print(f"  excepted (pending outreach):     {len(buckets['excepted_pending_outreach'])}")
    print(f"  excepted (permanent):            {len(buckets['excepted_permanent'])}")
    print(f"  drive-only, untriaged:           {len(buckets['drive_only_untriaged'])}")
    print(f"  role-only:                       {len(buckets['role_only'])}")
    print(f"  mismatch:                        {len(buckets['mismatch'])}")
    print(f"  ok:                              {len(buckets['ok'])}")
    total = sum(len(v) for v in buckets.values())
    print(f"  TOTAL rows:                      {total}")


# ---------------------------------------------------------------------------
# --apply-to-drive: mutate Drive to match roles (never run by this task)
# ---------------------------------------------------------------------------

def apply_to_drive(folder_id, drive_owner_email, buckets, dry_run: bool):
    """Roles win; Drive is edited to match. Only acts on rows where a role
    is actually known (site_roles admin/moderator mismatches, and
    pending/revoked-with-Drive-access). Never invents a level for
    'approved, no elevated role' rows — that tier isn't implemented.
    Always skips owner_protected, excepted, and non-user permissions
    (those never even reach `buckets['mismatch']`/`buckets['drive_only_untriaged']`
    in a form this function would act on, by construction of classify()).
    """
    sys.path.insert(0, str(SCRIPT_DIR))
    import drive_auth  # noqa: E402
    from googleapiclient.discovery import build

    creds = drive_auth.get_credentials()
    if not creds:
        sys.exit("FATAL: could not obtain Drive credentials for --apply-to-drive.")
    service = build("drive", "v3", credentials=creds)

    planned = []
    for r in buckets["mismatch"]:
        email = r["email"]
        if email in OWNER_PROTECTED_EMAILS:
            continue
        if "Upgrade Drive permission to writer" in r.get("action", ""):
            planned.append(("update_to_writer", email))
        elif r["estate"].startswith(("pending", "revoked")):
            planned.append(("remove", email))

    if not planned:
        print("apply-to-drive: nothing actionable (no confidently-known-role mismatches).")
        return

    for action, email in planned:
        if dry_run:
            print(f"[DRY RUN] would {action} Drive permission for {email}")
            continue
        # Real mutation path — intentionally never exercised by this task.
        print(f"[COMMIT] {action} Drive permission for {email}")
        # Implementation deliberately left minimal/defensive: fetch current
        # permission id fresh (do not trust a stale in-memory id) before
        # calling permissions().update()/delete() via `service`.
        raise NotImplementedError(
            "Real Drive mutation is intentionally not wired up in this task run."
        )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folder-id", default=FOLDER_ID_DEFAULT)
    parser.add_argument(
        "--exceptions-file",
        default=str(EXCEPTIONS_PATH_DEFAULT),
        help="path to drive-exceptions.json (default: docs/access/drive-exceptions.json)",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually mutate. Requires --apply-to-drive or --apply-to-roles. "
        "Without --commit this script NEVER writes anything, ever.",
    )
    parser.add_argument(
        "--apply-to-drive",
        action="store_true",
        help="Direction: roles win, Drive is edited to match. Only with --commit.",
    )
    parser.add_argument(
        "--apply-to-roles",
        action="store_true",
        help="Direction: report-only. Prints what role each Drive permission "
        "implies; NEVER grants a site role. Granting stays a human act in "
        "the admin UI.",
    )
    args = parser.parse_args()

    if args.commit and not (args.apply_to_drive or args.apply_to_roles):
        sys.exit("FATAL: --commit requires a direction: --apply-to-drive or --apply-to-roles.")
    if args.apply_to_drive and args.apply_to_roles:
        sys.exit("FATAL: choose one direction, not both.")

    exceptions = load_exceptions(Path(args.exceptions_file))

    print("Reading Drive permissions...")
    drive_owner_email, drive_perms, non_user_perms = fetch_drive_permissions(args.folder_id)

    print("Reading estate directory (D1 via wrangler)...")
    estate_rows, estate_error = fetch_estate_directory()
    if estate_rows is None:
        print(f"  WARNING: estate directory unreadable — {estate_error}")
        print("  Degrading gracefully: reporting Drive + Firestore only; "
              "estate status will show as UNKNOWN.")

    print("Reading Firestore site_roles...")
    site_roles = fetch_site_roles()

    drive_perms, estate_rows, site_roles, alias_display = apply_aliases(
        drive_perms, estate_rows, site_roles, exceptions["alias_pairs"]
    )

    buckets = classify(
        drive_owner_email, drive_perms, estate_rows, site_roles, exceptions, alias_display
    )

    print_report(
        args.folder_id,
        drive_owner_email,
        drive_perms,
        non_user_perms,
        estate_rows,
        estate_error,
        site_roles,
        exceptions,
        buckets,
        alias_display,
    )

    if args.apply_to_roles:
        print("\n--apply-to-roles: report-only, as designed. No site roles were "
              "granted or will be — grant them by hand in the admin UI using the "
              "'implies' values above.")
        return 0

    if args.apply_to_drive:
        print(f"\n--apply-to-drive requested (commit={args.commit}).")
        apply_to_drive(args.folder_id, drive_owner_email, buckets, dry_run=not args.commit)

    if not args.commit:
        print("\nDRY RUN — nothing was written. Pass --commit with a direction "
              "flag to mutate (see --help).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
