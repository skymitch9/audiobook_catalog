#!/usr/bin/env python3
"""
Promote-and-verify: the whole "Promote to Prod" ceremony as one command.

Normalizes what used to be ~8 manual steps per promotion:

    1. Check origin/main is at a clean, green boundary (latest Tests + Lint
       runs on main's tip both succeeded).
    2. Dispatch promote.yml and watch it to completion.
    3. Find the deploy.yml run promote.yml triggers (matched by branch +
       time, same as done by hand all day) and watch that too.
    4. Fetch the live site and confirm it actually served the promoted
       content (status 200, and any versioned ("cache-busting") JS module
       tokens on the page match what main's tree carries).
    5. Print a compact PASS/FAIL summary suitable for pasting.

This script never pushes to `prod` itself — promote.yml (the only writer of
`prod`, per its own header comment) still does that. This script only
drives the ceremony around it.

Usage:
    python scripts/promote_and_verify.py              # do the whole thing
    python scripts/promote_and_verify.py --dry-run     # pre-flight only,
                                                        # stop before dispatch
    python scripts/promote_and_verify.py --ref main --force

Exit codes: 0 on a clean PASS, 1 on any refusal or FAIL.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent

SITE_URL = "https://audiobooks.heygabi.ai/"
CLUB_PATH = "club"  # https://audiobooks.heygabi.ai/club

REQUIRED_GREEN_WORKFLOWS = ("Tests", "Lint")
PROD_TAG_PREFIX = "prod-"

CACHE_TOKEN_RE = re.compile(r"([A-Za-z0-9_\-./]+\.js)\?v=(\d{8}[a-z]?)")

# Cheap heuristic for "does this promoted range touch the club pages" —
# substring match on path, not a real dependency graph. Good enough to
# decide whether the extra /club fetch is worth doing.
CLUB_PATH_HINT = re.compile(r"club", re.IGNORECASE)


# --------------------------------------------------------------------------
# Pure helpers (unit-tested directly, no subprocess/network involved)
# --------------------------------------------------------------------------


def extract_cache_tokens(html_text: str) -> dict:
    """Return {module filename: version token} for every '<name>.js?v=<token>'
    reference found in a page of HTML/JS. Order-preserving; last occurrence
    of a given filename wins (module import lines repeat rarely, but if they
    ever disagree we want the diagnostic to be visible, not hidden)."""
    tokens: dict = {}
    for filename, token in CACHE_TOKEN_RE.findall(html_text):
        # Keep just the basename — local template paths and the site's own
        # relative "./foo.js" both resolve to the same served file.
        name = filename.rsplit("/", 1)[-1]
        tokens[name] = token
    return tokens


def compare_cache_tokens(local: dict, remote: dict) -> tuple:
    """Compare the cache-bust tokens main's tree carries (`local`) against
    what a fetched page actually served (`remote`). Returns (ok, problems).

    A page with no versioned modules at all (empty `local`) is a trivial
    pass — not every page uses this convention (only the club pages do, as
    of 2026-08; the main catalog page does not)."""
    problems = []
    for name, expected in sorted(local.items()):
        actual = remote.get(name)
        if actual is None:
            problems.append(f"{name}: expected ?v={expected}, not referenced on live page")
        elif actual != expected:
            problems.append(f"{name}: expected ?v={expected}, live page serves ?v={actual}")
    return (len(problems) == 0, problems)


def club_files_changed(changed_files: list) -> bool:
    """Cheap heuristic: did anything in the promoted range plausibly touch
    the book-club pages? Substring match on path, not a dependency graph —
    intentionally over-inclusive rather than under-inclusive."""
    return any(CLUB_PATH_HINT.search(f) for f in changed_files)


def summarize_promoted_range(last_tag: Optional[str], head_sha: str, commits: list, changed_files: list) -> str:
    """Compact human-readable summary of what a promote would ship."""
    lines = []
    base = last_tag if last_tag else "(no prior prod-* tag found — first promotion)"
    lines.append(f"Range: {base} .. {head_sha[:12]} (origin/main)")
    lines.append(f"Commits ahead of last promote: {len(commits)}")
    for c in commits[:10]:
        lines.append(f"  {c['sha'][:9]}  {c['subject']}")
    if len(commits) > 10:
        lines.append(f"  ... and {len(commits) - 10} more")
    lines.append(f"Files changed: {len(changed_files)}")
    for f in changed_files[:15]:
        lines.append(f"  {f}")
    if len(changed_files) > 15:
        lines.append(f"  ... and {len(changed_files) - 15} more")
    return "\n".join(lines)


def parse_commit_log(log_text: str) -> list:
    """Parse `git log --format=%H%x1f%s` output into [{'sha', 'subject'}]."""
    commits = []
    for line in log_text.splitlines():
        line = line.strip("\n")
        if not line:
            continue
        parts = line.split("\x1f", 1)
        sha = parts[0]
        subject = parts[1] if len(parts) > 1 else ""
        commits.append({"sha": sha, "subject": subject})
    return commits


def latest_prod_tag(tag_lines: list) -> Optional[str]:
    """Pick the latest prod-* tag. Tag names are `prod-YYYYMMDD-HHMMSS`, so
    a plain reverse-lexical sort is also a reverse-chronological sort —
    no date parsing needed."""
    tags = [t.strip() for t in tag_lines if t.strip().startswith(PROD_TAG_PREFIX)]
    if not tags:
        return None
    return sorted(tags, reverse=True)[0]


def ci_green_at_sha(runs: list, sha: str, required: tuple = REQUIRED_GREEN_WORKFLOWS) -> tuple:
    """Given `gh run list --json workflowName,headSha,conclusion,status,url`
    rows, decide whether every workflow in `required` has a successful
    completed run at exactly `sha`. Returns (ok, message)."""
    by_workflow = {}
    for run in runs:
        if run.get("headSha") != sha:
            continue
        name = run.get("workflowName")
        if name not in required:
            continue
        # Keep the most recently created run we've seen per workflow name —
        # `runs` is expected newest-first from `gh run list`, so first-seen
        # wins; only overwrite if we haven't recorded one yet.
        by_workflow.setdefault(name, run)

    missing = [w for w in required if w not in by_workflow]
    if missing:
        return (
            False,
            f"No run found at {sha[:12]} for: {', '.join(missing)} "
            "(CI may still be queued/running — wait and retry)",
        )

    bad = []
    for name, run in by_workflow.items():
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            bad.append(f"{name} is still {status} ({run.get('url')})")
        elif conclusion != "success":
            bad.append(f"{name} concluded '{conclusion}', not success ({run.get('url')})")

    if bad:
        return (False, "; ".join(bad))
    return (True, f"{', '.join(required)} all green at {sha[:12]}")


def find_deploy_run(deploy_runs: list, not_before_iso: str) -> Optional[dict]:
    """Among `deploy.yml` runs, find the one promote.yml's own dispatch
    triggered: it is a workflow_dispatch-triggered run created at or after
    the promote run started. Ties broken by earliest such run (the first
    workflow_dispatch run after our dispatch is ours; concurrency group
    "pages" on deploy.yml serializes them anyway)."""
    candidates = [
        r for r in deploy_runs
        if r.get("event") == "workflow_dispatch" and r.get("createdAt", "") >= not_before_iso
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda r: r.get("createdAt", ""))[0]


# --------------------------------------------------------------------------
# Process / network helpers (thin, deliberately not unit-tested)
# --------------------------------------------------------------------------


def run_cmd(args: list, cwd: Path = PROJECT_ROOT, check: bool = True) -> str:
    result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout


def gh_json(args: list):
    out = run_cmd(["gh"] + args)
    return json.loads(out) if out.strip() else None


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


class Refused(Exception):
    """Pre-flight refused to proceed. Message is safe to print as-is."""


def preflight(ref: str) -> dict:
    """Fetch, compute the promoted range, and require CI green at the tip.
    Raises Refused with a clear message on any failure. Returns a dict with
    everything the rest of the ceremony needs."""
    print("Fetching origin...")
    run_cmd(["git", "fetch", "origin", "--tags", "--quiet"])

    head_sha = run_cmd(["git", "rev-parse", f"origin/{ref}"]).strip()

    tag_lines = run_cmd(["git", "tag", "-l", f"{PROD_TAG_PREFIX}*"]).splitlines()
    last_tag = latest_prod_tag(tag_lines)

    range_spec = f"{last_tag}..origin/{ref}" if last_tag else f"origin/{ref}"

    log_out = run_cmd(["git", "log", "--format=%H%x1f%s", range_spec])
    commits = parse_commit_log(log_out)

    diff_args = (
        ["git", "diff", "--name-only", range_spec] if last_tag
        else ["git", "ls-tree", "-r", "--name-only", f"origin/{ref}"]
    )
    diff_out = run_cmd(diff_args)
    changed_files = [f for f in diff_out.splitlines() if f.strip()]

    if last_tag and not commits:
        raise Refused(
            f"origin/{ref} is already at the last promoted tag ({last_tag}) — "
            "nothing to promote."
        )

    summary = summarize_promoted_range(last_tag, head_sha, commits, changed_files)
    print(summary)

    print(f"\nChecking CI at {head_sha[:12]}...")
    runs = gh_json([
        "run", "list", "--branch", ref, "--limit", "50",
        "--json", "workflowName,headSha,conclusion,status,url,createdAt",
    ]) or []
    ok, msg = ci_green_at_sha(runs, head_sha)
    print(msg)
    if not ok:
        raise Refused(
            f"Refusing to promote: {msg}\n"
            "(No --force here by design — if you want to ship red deliberately, "
            "use the GitHub UI to run promote.yml directly.)"
        )

    return {
        "ref": ref,
        "head_sha": head_sha,
        "last_tag": last_tag,
        "commits": commits,
        "changed_files": changed_files,
        "summary": summary,
        "club_changed": club_files_changed(changed_files),
    }


def dispatch_and_watch_promote(ref: str, force: bool) -> dict:
    args = ["workflow", "run", "promote.yml", "--ref", ref, "-f", f"ref={ref}"]
    if force:
        args += ["-f", "force=true"]
    # Use wall-clock, not a git timestamp, as the "not before" bound for
    # locating the run — dispatch happens now, not at HEAD's commit time.
    not_before = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    print(f"\nDispatching promote.yml (ref={ref}, force={force})...")
    run_cmd(["gh"] + args)

    run_id = _poll_for_new_run("promote.yml", not_before)
    run_info = gh_json(["run", "view", str(run_id), "--json", "url,htmlUrl"]) or {}
    url = run_info.get("url") or f"(run id {run_id})"
    print(f"Watching promote run: {url}")
    watch_result = subprocess.run(["gh", "run", "watch", str(run_id), "--exit-status"], cwd=str(PROJECT_ROOT))
    if watch_result.returncode != 0:
        raise Refused(f"promote.yml run failed — see {url}")

    return {"run_id": run_id, "url": url, "not_before": not_before}


def _poll_for_new_run(workflow: str, not_before_iso: str, attempts: int = 15, delay_s: float = 2.0) -> int:
    """`gh workflow run` doesn't hand back a run id, so poll `gh run list`
    until a run created at/after our dispatch shows up."""
    for _ in range(attempts):
        runs = gh_json([
            "run", "list", "--workflow", workflow, "--limit", "5",
            "--json", "databaseId,createdAt,event",
        ]) or []
        candidates = [r for r in runs if r.get("createdAt", "") >= not_before_iso]
        if candidates:
            newest = sorted(candidates, key=lambda r: r.get("createdAt", ""))[0]
            return newest["databaseId"]
        time.sleep(delay_s)
    raise Refused(
        f"Dispatched {workflow} but no matching run appeared within "
        f"{attempts * delay_s:.0f}s — check `gh run list` by hand."
    )


def find_and_watch_deploy(not_before_iso: str) -> dict:
    print("\nLooking for the deploy.yml run promote.yml triggered...")
    run_info = None
    for _ in range(15):
        deploy_runs = gh_json([
            "run", "list", "--workflow", "deploy.yml", "--branch", "main", "--limit", "10",
            "--json", "databaseId,createdAt,event,url",
        ]) or []
        run_info = find_deploy_run(deploy_runs, not_before_iso)
        if run_info:
            break
        time.sleep(2.0)
    if not run_info:
        raise Refused(
            "promote.yml finished but no matching deploy.yml workflow_dispatch run "
            "was found — check `gh run list --workflow=deploy.yml` by hand."
        )

    url = run_info.get("url") or f"(run id {run_info['databaseId']})"
    print(f"Watching deploy run: {url}")
    watch_result = subprocess.run(
        ["gh", "run", "watch", str(run_info["databaseId"]), "--exit-status"], cwd=str(PROJECT_ROOT)
    )
    if watch_result.returncode != 0:
        raise Refused(f"deploy.yml run failed — see {url}")

    return {"run_id": run_info["databaseId"], "url": url}


def verify_live_site(club_changed: bool) -> dict:
    import requests

    results = []
    overall_ok = True

    def check_page(label: str, url: str, local_html_path: Path):
        nonlocal overall_ok
        try:
            resp = requests.get(url, timeout=20)
        except requests.RequestException as exc:
            overall_ok = False
            results.append((label, False, f"fetch failed: {exc}"))
            return
        if resp.status_code != 200:
            overall_ok = False
            results.append((label, False, f"HTTP {resp.status_code} from {url}"))
            return

        local_html = local_html_path.read_text(encoding="utf-8") if local_html_path.exists() else ""
        local_tokens = extract_cache_tokens(local_html)
        remote_tokens = extract_cache_tokens(resp.text)
        ok, problems = compare_cache_tokens(local_tokens, remote_tokens)
        if ok:
            note = "200; cache tokens match" if local_tokens else "200; no versioned modules on this page (nothing to compare)"
            results.append((label, True, note))
        else:
            overall_ok = False
            results.append((label, False, "; ".join(problems)))

    check_page("root (/)", SITE_URL, PROJECT_ROOT / "site" / "index.html")
    if club_changed:
        check_page(f"/{CLUB_PATH}", SITE_URL.rstrip("/") + "/" + CLUB_PATH, PROJECT_ROOT / "site" / "club.html")
    else:
        results.append((f"/{CLUB_PATH}", None, "skipped — no club-related files in the promoted range"))

    return {"ok": overall_ok, "results": results}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", default="main", help="branch to promote (default: main)")
    parser.add_argument(
        "--force", action="store_true",
        help="forward force=true to promote.yml (skips its catalog-shrink guard)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="run pre-flight (fetch, range summary, CI-green check) and stop "
             "before dispatching promote.yml",
    )
    args = parser.parse_args(argv)

    try:
        info = preflight(args.ref)
    except Refused as exc:
        print(f"\nREFUSED: {exc}")
        return 1

    if args.dry_run:
        print("\n--dry-run: pre-flight passed. Stopping before dispatch.")
        print(f"Would run: gh workflow run promote.yml --ref {args.ref} -f ref={args.ref}"
              + (" -f force=true" if args.force else ""))
        return 0

    try:
        promote_info = dispatch_and_watch_promote(args.ref, args.force)
        deploy_info = find_and_watch_deploy(promote_info["not_before"])
        verification = verify_live_site(info["club_changed"])
    except Refused as exc:
        print(f"\nREFUSED / FAILED: {exc}")
        return 1

    print("\n" + "=" * 70)
    print("PROMOTE + VERIFY SUMMARY")
    print("=" * 70)
    print(info["summary"])
    print(f"\npromote.yml run: {promote_info['url']}")
    print(f"deploy.yml run:  {deploy_info['url']}")
    print("\nVerification:")
    for label, ok, note in verification["results"]:
        status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        print(f"  [{status}] {label} — {note}")

    overall = verification["ok"]
    print(f"\nOVERALL: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
