"""
Drive <-> Role Parity Auditor — docs/info/ROLES.md §2.

Owner's words: "go into google drive and set everyones rights to match their
role... I want this always to match. We can also do this backwards and make
sure all the people with contribute in drive are contributors in ours."

⚠️ Roles are the source of truth; Drive is downstream (ROLES.md §2). This
script REPORTS drift in both directions, and — since 2026-08-17 — can APPLY
the role→Drive direction. Drive→role stays report-only forever.

⚠️ IT NOW RUNS UNATTENDED. Owner order 2026-08-17 ("Wire it… with auto
apply"): scripts/sync_to_drive.py STEP 8 runs this every pipeline cycle with
`--commit --apply-to-drive --json-summary`. Two things follow, and neither is
optional:
  * every rail in the SAFETY MODEL below is now load-bearing without a human
    in the loop — nobody reads the report before the change lands; and
  * a rail on HOW MANY people change in one tick was added, because the
    existing rails only govern WHO. See MASS_DRIFT_CAP.
The decision half (plan_drive_changes + fuse_check) is pure and unit-tested;
the Drive-mutating half executes a plan and decides nothing.

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
                         against,
                         and refuses the WHOLE plan if it would change more
                         than MASS_DRIFT_CAP people in one run (the fuse).
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
    python scripts/drive_role_parity.py --commit --apply-to-drive --json-summary
                                        # what STEP 8 runs every cycle
    DRIVE_PARITY_FUSE_OVERRIDE=1 python scripts/drive_role_parity.py \
        --commit --apply-to-drive       # a human overriding a tripped fuse

⚠️ On Windows, set PYTHONIOENCODING=utf-8 when capturing this script's output
from another process: the report prints em-dashes and ⚠️, and a cp1252 pipe
raises UnicodeEncodeError mid-report. STEP 8 sets it explicitly for exactly
this reason — the console reconfigure() at the top of this file fixes the
terminal case, not a captured pipe's encoding.
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

# ---------------------------------------------------------------------------
# THE FUSE — blast-radius protection for the AUTO-APPLY (owner order
# 2026-08-17: "Wire it… with auto apply", fulfilling ROLES.md §2's "I want
# this always to match"). Wired at scripts/sync_to_drive.py STEP 8.
#
# Wiring this script into the 8-hourly pipeline turned a reviewed, human-run
# reconciliation into an unattended one. Every rail in the SAFETY MODEL above
# still holds — the owner's accounts, the exception list, the unknown-tier
# skip, Drive→role forever report-only — but every one of them is a rail on
# WHO gets changed. None of them is a rail on HOW MANY.
#
# That is the gap this constant closes. The apply set is derived from THREE
# systems (Drive, D1, Firestore). If any one of them answers
# wrongly-but-parseably — a truncated D1 result, a Firestore read taken
# mid-migration, an OAuth token that silently re-authorised as a different
# account and so returns a different folder's permissions — the plan does not
# come out empty, it comes out BIG. Genuine drift is the opposite shape: one
# person at a time, because one person at a time gets demoted or signs up. A
# single tick that wants to change four people is not four coincidences; it
# is one bad read.
#
# So: if a tick's would-apply set exceeds this cap, apply NOTHING that tick
# and say so loudly. Deliberate single-person drift (a demotion) still flows
# within the tick it happens in — which is the entire point of auto-apply — and
# a mass change stops for a human.
#
# ⚠️ 3, not 1 and not 10. 1 trips on the ordinary case of one person demoted
# in the same 8h window another is granted, and a fuse that trips on normal
# traffic gets overridden by reflex until it means nothing. 10 is larger than
# the whole non-owner Drive population has ever drifted in a day (15 non-owner
# permissions total, measured 2026-08-16). Same style as SWEEP_LIMIT: a small
# named number carrying its reasoning, never a bare literal at the call site.
#
# ⚠️ The override is an ENVIRONMENT VARIABLE, not a flag, on purpose (global
# rule: an escape hatch is deliberately awkward, never an easy flag). A flag
# would eventually be pasted into the scheduled command line and the fuse
# would be off forever with nobody noticing.
# ---------------------------------------------------------------------------
MASS_DRIFT_CAP = 3
FUSE_OVERRIDE_ENV = "DRIVE_PARITY_FUSE_OVERRIDE"


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

    def drive_account_of(email: str) -> str:
        """The address the Drive PERMISSION is actually held under.

        ⚠️ Not the same as the canonical identity when an alias is folded:
        the row is keyed by the site account, but Drive knows the person by
        the other address. Anything that mutates Drive must look the
        permission up by THIS one or it will find nothing and (worse) could
        be tempted to create a permission for an address Drive has never
        seen. See apply_to_drive().
        """
        alias = alias_display.get(email)
        return alias["drive_account"] if alias else email

    def row(email: str) -> dict:
        """Every bucket row carries the machine-readable identity fields
        alongside the human `email` string. `email` is for the REPORT (it
        embeds the alias note); `raw_email` is what code compares against
        OWNER_PROTECTED_EMAILS and the exception list — string-matching a
        display label is how a protected account leaks past a rail."""
        return {
            "raw_email": email,
            "drive_account": drive_account_of(email),
            "email": display_email(email),
        }

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
                    **row(email),
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
                    **row(email),
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
                    **row(email),
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
                        **row(email),
                        "drive": drive_level,
                        "estate": "UNKNOWN — estate directory unreadable this run",
                        "difference": "Cannot classify without estate directory status.",
                        # ⚠️ drive_fix stays None on this path, always. The
                        # estate directory is one of the two inputs that
                        # decides the correct level; with it unreadable the
                        # only honest plan is no plan. An auto-apply that
                        # "reconciled" against a half-read world is exactly
                        # the mass-drift shape MASS_DRIFT_CAP exists for, and
                        # this is the cheaper place to stop it.
                        "drive_fix": None,
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
                    **row(email),
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
                    **row(email),
                    "drive": drive_level,
                    "estate": f"{status} (estate_user.status)",
                    "difference": f"Estate status is '{status}' but Drive still grants "
                    f"{drive_level} access.",
                    # A KNOWN role decision: the estate directory says this
                    # person is pending or revoked, and roles are the source
                    # of truth, so Drive comes off. Owner-protected accounts
                    # are excluded here AND again in plan_drive_changes() —
                    # the rail that matters most gets two independent checks.
                    "drive_fix": None if is_owner_protected else "remove",
                    "action": action
                    or (
                        f"Revoke Drive access (status={status}), or approve them in the "
                        "estate directory if access should continue."
                    ),
                }
            )
            continue

        # ---- pending/revoked AND no Drive access: the correct END STATE ----
        # ⚠️ Found 2026-08-17 by running the reconciler for real immediately
        # after it applied its first change. Removing a revoked person's Drive
        # permission moved them from `mismatch` (revoked-but-has-access) to...
        # `mismatch` again, via the unclassified fallback — because no branch
        # described "revoked, and correctly holds nothing". So the very act of
        # fixing the drift left a permanent phantom finding, which the /status
        # row would have rendered as drift that never clears and no amount of
        # reconciling could ever remove. A row that cannot go green trains
        # everyone to ignore the colour.
        if estate_row is not None and status in ("pending", "revoked") and drive_level == "none":
            buckets["ok"].append(
                {
                    **row(email),
                    "drive": "none",
                    "estate": f"{status} (estate_user.status)",
                    "note": f"Correct: estate status is '{status}' and Drive grants nothing. "
                    "Nothing to reconcile — this is what a completed revocation "
                    "looks like.",
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
                        **row(email),
                        "drive": drive_level,
                        "estate": f"approved, site_roles={site_role} (implies contributor+)",
                        "note": note,
                    }
                )
            else:
                buckets["mismatch"].append(
                    {
                        **row(email),
                        "drive": drive_level,
                        "estate": f"approved, site_roles={site_role} (implies contributor+ -> Drive writer)",
                        "difference": f"Site role implies Drive writer; actual Drive level is "
                        f"'{drive_level}'.",
                        # The other KNOWN role decision: an explicit
                        # admin/moderator doc in Firestore is a stored role,
                        # so the Drive level it implies is enforceable. (The
                        # unimplemented reader/contributor tiers never reach
                        # here — they have no storage to disagree with.)
                        "drive_fix": None if is_owner_protected else "writer",
                        "action": action or f"Upgrade Drive permission to writer to match site role {site_role}.",
                    }
                )
            continue

        # ---- approved, no elevated site role, no Drive access ----
        if estate_row is not None and status == "approved" and drive_level == "none":
            in_gap_list = email in estate_only_gap
            buckets["role_only"].append(
                {
                    **row(email),
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
                    **row(email),
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
                **row(email),
                "drive": drive_level,
                "estate": _estate_desc(estate_row, estate_rows),
                "difference": "Unclassified combination — inspect by hand.",
                # No fix: "we don't know what this is" can never be a reason
                # to change someone's access unattended.
                "drive_fix": None,
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
# --apply-to-drive: the PURE decision half
#
# plan_drive_changes() and fuse_check() take dicts in and return dicts out.
# No Drive, no D1, no Firestore, no clock. That is deliberate and it is the
# whole reason this can be trusted unattended: the two questions that decide
# whether a real person keeps access — "who is in the apply set" and "is this
# set too big to be real drift" — are answerable in a unit test, and they are
# (tests/test_drive_role_parity.py). The Drive-mutating half below does no
# deciding; it executes a plan it was handed.
# ---------------------------------------------------------------------------


def plan_drive_changes(buckets: dict, exceptions: dict) -> list[dict]:
    """PURE. Buckets + the exception list in, the apply set out.

    Reads ONLY `buckets['mismatch']`, and only rows carrying an explicit
    `drive_fix` — the two cases where a role is actually STORED somewhere and
    can therefore be enforced (an admin/moderator site_roles doc; an estate
    status of pending/revoked). Every other bucket is unreachable from here
    by design, and each for its own reason:

      * `role_only` / the "approved, no elevated role" rows — the
        reader/contributor tiers have no per-user storage yet (ROLES.md §2),
        so there is no role to enforce. Forcing a level here would be the
        naive reconciliation ROLES.md warns against, and it would GRANT
        access (see the global rule: act on access-reducing orders, confirm
        access-increasing ones).
      * `drive_only_untriaged` — a person nobody has triaged. Revoking them
        unattended is precisely what the exception list exists to prevent
        happening by accident.
      * `owner_protected` / `excepted_*` — filtered again below even though
        classify() already routed them away from `mismatch`. The rails that
        protect real people's access get two independent checks, so that a
        future refactor of classify() cannot silently disarm one.

    Returns a list of {"action", "email", "drive_account", "from", "reason"}.
    """
    excepted = set(exceptions.get("pending_outreach") or {}) | set(
        exceptions.get("permanent_exceptions") or {}
    )

    planned: list[dict] = []
    for r in buckets.get("mismatch", []):
        email = r.get("raw_email") or r.get("email")
        fix = r.get("drive_fix")
        if not fix:
            continue  # unknown tier, unreadable estate, unclassified — no plan
        if email in OWNER_PROTECTED_EMAILS:
            continue  # rail: the owner's own accounts, never, under any flag
        if email in excepted:
            continue  # rail: the migration queue and permanent carve-outs
        planned.append(
            {
                "action": "remove" if fix == "remove" else "update_to_writer",
                "email": email,
                "drive_account": r.get("drive_account") or email,
                "from": r.get("drive"),
                "reason": r.get("difference", ""),
            }
        )
    return planned


def fuse_check(planned: list[dict], cap: int = MASS_DRIFT_CAP, override: bool = False):
    """PURE. -> (allowed: bool, reason: str). See MASS_DRIFT_CAP above.

    All-or-nothing on purpose: a set that is too big to trust is not made
    trustworthy by applying the first three of it. Half of a bad plan is
    still a bad plan, and it is a harder one to undo because nobody can tell
    from the outside which half ran.
    """
    n = len(planned)
    if n <= cap:
        return True, f"{n} change(s), within the cap of {cap}"
    if override:
        return True, (
            f"{n} change(s) EXCEEDS the cap of {cap}, but {FUSE_OVERRIDE_ENV}=1 "
            "was set — a human deliberately overrode the fuse."
        )
    return False, (
        f"parity wants to change {n} people — that smells like a data problem, "
        f"not drift; run manually to review (cap is {cap}). NOTHING was applied "
        f"this tick. If the number is genuinely correct, re-run with "
        f"{FUSE_OVERRIDE_ENV}=1 set."
    )


# ---------------------------------------------------------------------------
# --apply-to-drive: the IMPURE half — executes a plan, decides nothing
# ---------------------------------------------------------------------------


def _live_permission(service, folder_id: str, drive_account: str):
    """Fetch the CURRENT permission for one address. Never trust an id
    captured earlier in the run: between the report and the apply, a human
    can have changed the very permission we are about to change."""
    from googleapiclient.errors import HttpError

    try:
        resp = (
            service.permissions()
            .list(
                fileId=folder_id,
                fields="permissions(id,emailAddress,role,type)",
                supportsAllDrives=True,
            )
            .execute()
        )
    except HttpError as e:
        print(f"  [WARN] could not re-read Drive permissions before applying: {e}")
        return None
    for p in resp.get("permissions", []):
        if p.get("type") != "user":
            continue
        if normalize_email(p.get("emailAddress", "")) == normalize_email(drive_account):
            return p
    return None


def apply_to_drive(folder_id, drive_owner_email, buckets, dry_run: bool, exceptions: dict,
                   fuse_override: bool = False) -> dict:
    """Roles win; Drive is edited to match. Returns a result dict for the
    caller's summary (the pipeline's STEP 8 reads it out of --json-summary).

    Never raises for an individual failure: one permission that will not
    update must not abandon the rest of the plan or the run around it.
    """
    planned = plan_drive_changes(buckets, exceptions)
    allowed, fuse_reason = fuse_check(planned, override=fuse_override)

    result = {
        "planned": len(planned),
        "applied": [],
        "failed": [],
        "fuse_tripped": not allowed,
        "fuse_reason": fuse_reason,
        "cap": MASS_DRIFT_CAP,
    }

    if not planned:
        print("apply-to-drive: nothing actionable (no confidently-known-role mismatches).")
        return result

    print(f"\napply-to-drive: {len(planned)} change(s) planned — {fuse_reason}")
    for p in planned:
        where = "" if p["email"] == p["drive_account"] else f" (Drive perm held as {p['drive_account']})"
        print(f"  - {p['action']}: {p['email']}{where}  [currently {p['from']}]  {p['reason']}")

    if not allowed:
        print("\n" + "!" * 78)
        print(f"!! FUSE TRIPPED — {fuse_reason}")
        print("!" * 78)
        return result

    if dry_run:
        for p in planned:
            print(f"[DRY RUN] would {p['action']} Drive permission for {p['email']}")
        return result

    sys.path.insert(0, str(SCRIPT_DIR))
    import drive_auth  # noqa: E402
    from googleapiclient.discovery import build

    creds = drive_auth.get_credentials()
    if not creds:
        sys.exit("FATAL: could not obtain Drive credentials for --apply-to-drive.")
    service = build("drive", "v3", credentials=creds)

    for p in planned:
        email, act = p["email"], p["action"]
        perm = _live_permission(service, folder_id, p["drive_account"])
        if perm is None:
            # Loud skip, never a guess. The commonest cause is an alias whose
            # Drive side moved; creating a permission for an address Drive
            # has never seen would GRANT access, which this direction is not
            # allowed to do unattended.
            msg = f"no live Drive permission found for {p['drive_account']} — skipped, not guessed"
            print(f"  [WARN] {msg}")
            result["failed"].append({"email": email, "action": act, "error": msg})
            continue
        if perm.get("role") == "owner":
            msg = f"{email} holds the folder OWNER permission — refused, unconditionally"
            print(f"  [WARN] {msg}")
            result["failed"].append({"email": email, "action": act, "error": msg})
            continue
        try:
            if act == "remove":
                service.permissions().delete(
                    fileId=folder_id, permissionId=perm["id"], supportsAllDrives=True
                ).execute()
            else:
                service.permissions().update(
                    fileId=folder_id,
                    permissionId=perm["id"],
                    body={"role": "writer"},
                    supportsAllDrives=True,
                ).execute()
        except Exception as e:  # noqa: BLE001 — one failure must not abandon the plan
            print(f"  [WARN] {act} FAILED for {email}: {e}")
            result["failed"].append({"email": email, "action": act, "error": str(e)})
            continue
        print(f"  [APPLIED] {act}: {email} (was {perm.get('role')})")
        result["applied"].append({"email": email, "action": act, "from": perm.get("role")})

    return result


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
    parser.add_argument(
        "--json-summary",
        action="store_true",
        help="Print one machine-readable line, 'PARITY_JSON {...}', as the "
        "LAST line of output: counts by direction, the fuse verdict, and the "
        "emails actually changed. Used by scripts/sync_to_drive.py STEP 8 so "
        "the pipeline can report parity without re-implementing any of this "
        "script's judgement. Adds output; changes no behaviour.",
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

    counts = {name: len(rows) for name, rows in buckets.items()}

    if args.apply_to_roles:
        print("\n--apply-to-roles: report-only, as designed. No site roles were "
              "granted or will be — grant them by hand in the admin UI using the "
              "'implies' values above.")
        _emit_json_summary(args, "report-only-roles", counts, None,
                           estate_error if estate_rows is None else None)
        return 0

    apply_result = None
    if args.apply_to_drive:
        import os

        fuse_override = os.getenv(FUSE_OVERRIDE_ENV, "") == "1"
        print(f"\n--apply-to-drive requested (commit={args.commit}, "
              f"fuse cap={MASS_DRIFT_CAP}, override={fuse_override}).")
        apply_result = apply_to_drive(
            args.folder_id, drive_owner_email, buckets,
            dry_run=not args.commit, exceptions=exceptions,
            fuse_override=fuse_override,
        )

    if not args.commit:
        print("\nDRY RUN — nothing was written. Pass --commit with a direction "
              "flag to mutate (see --help).")

    _emit_json_summary(
        args,
        _summary_state(args, apply_result),
        counts,
        apply_result,
        estate_error if estate_rows is None else None,
    )
    return 0


def _summary_state(args, apply_result) -> str:
    """The one word STEP 8 and the /status row key off. Deliberately small —
    a status vocabulary that grows a case per edge case stops being readable
    on a dashboard."""
    if not args.apply_to_drive:
        return "report-only"
    if apply_result is None:
        return "report-only"
    if apply_result["fuse_tripped"]:
        return "fuse-tripped"
    if apply_result["applied"]:
        return "applied"
    if apply_result["failed"]:
        return "failed"
    if apply_result["planned"] and not args.commit:
        return "drift-pending"  # a dry run that found real, appliable drift
    return "in-sync"


def _emit_json_summary(args, state: str, counts: dict, apply_result, estate_error) -> None:
    """One line, last, machine-readable — see --json-summary.

    ⚠️ It carries EMAILS (the pipeline log is local). The pipeline puts only
    COUNTS into pipeline_status, because that doc is world-readable and the
    /status page renders it: real people's addresses do not belong on a
    public dashboard. Keep that split.
    """
    if not args.json_summary:
        return
    payload = {
        "state": state,
        "counts": counts,
        "estateUnreadable": estate_error,
        "cap": MASS_DRIFT_CAP,
        "planned": (apply_result or {}).get("planned", 0),
        "applied": [a["email"] for a in (apply_result or {}).get("applied", [])],
        "appliedDetail": (apply_result or {}).get("applied", []),
        "failed": (apply_result or {}).get("failed", []),
        "fuseTripped": (apply_result or {}).get("fuse_tripped", False),
        "fuseReason": (apply_result or {}).get("fuse_reason", ""),
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    print("PARITY_JSON " + json.dumps(payload))


if __name__ == "__main__":
    sys.exit(main())
