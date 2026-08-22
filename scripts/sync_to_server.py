"""
Force a full reconciliation upload from the local library to the shelf
server (owner ask 2026-08-16, catalog-platform /status Operations section:
"we probably should add a button to force a full upload to the server that
we can run to make sure we can move google drive to server without the full
pipeline").

⚠️ THE SHELF SERVER DOES NOT EXIST YET — docs/access/SHELF_SERVER.md is
still "NOT YET RUN" as of this writing; Justin has not built the box. This
script is real, working code for when it does, and is deliberately honest
about that gap: unset or unreachable config is reported PLAINLY ("not
configured" / "unreachable"), never silently swallowed and never dressed up
as success. Safe to run repeatedly by design — the transfer itself
(`rclone sync`) is idempotent, same property the existing Drive upload step
already relies on.

Deliberately NOT a pipeline step (see scripts/sync_to_drive.py's STEP_INFO —
this has no entry there) — it is a standalone reconciliation that pushes
whatever is on local disk (the same ROOT_DIR the Drive step uploads FROM,
already the pipeline's canonical library tree) straight to the shelf server,
bypassing the Drive round-trip entirely. It does NOT touch
scripts/sync_to_drive.py, Google Drive, or git — Drive stays the permanent,
canonical destination (owner order 2026-08-15: "no matter what do not kill
any part of my pipeline") and this is an ADDITIONAL, independent push
target, matching Phase 2 of the shelf-server build (SHELF_SERVER.md §8:
"scripts/sync_to_server.py... rclone over SFTP to the tailnet address, same
source-of-truth walk as the Drive step"). Built now, ahead of Phase 2's
original "not before the box exists" trigger, because the owner explicitly
asked for a manual override control tonight — nothing here commits the
project to Phase 2's full cutover design; it is a narrow, self-contained
addition.

Config (all unset by default — see .env.example):
    SHELF_SERVER_HOST      tailnet IP or hostname of the shelf server
    SHELF_SERVER_PATH      remote library path, e.g. /srv/shelf/library
    SHELF_SERVER_USER      SSH/SFTP user (default: the local username)
    SHELF_SERVER_SSH_PORT  SSH port (default: 22)

Usage:
    python scripts/sync_to_server.py            # real run
    python scripts/sync_to_server.py --dry-run  # rclone --dry-run — still
                                                  # requires a reachable,
                                                  # configured target; this
                                                  # is NOT a config-only check
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from app.core import pipeline_lock  # noqa: E402

try:
    from app import pipeline_status as pstatus  # noqa: E402
except Exception:  # pragma: no cover - defensive
    class _NoStatus:
        def __getattr__(self, _name):
            return lambda *a, **k: ""

    pstatus = _NoStatus()


@dataclass(frozen=True)
class ShelfUploadResult:
    ok: bool
    configured: bool
    reachable: bool | None  # None when configured is False (never checked)
    message: str
    returncode: int | None = None


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


ESTATE_CONFIG_URL = "https://heygabi.ai/api/machine/shelf-config"


def _config_from_estate() -> dict[str, str]:
    """Fetch the four connection values from the estate, or {} if unavailable.

    ⚠️ NEVER RAISES, and never blocks the caller for long. This is a
    convenience so Justin can type the values into a form instead of them
    being relayed through a chat message and hand-copied into `.env` (owner
    ask, 2026-08-22). If it is unreachable, unauthenticated, or nobody has
    filled the form in yet, the answer is "no config" and `get_config()`
    falls through to its existing honest "not configured" — which is the best
    behaviour this script has and must not be traded for a guess.

    ⚠️ `.env` WINS. A value set locally is a deliberate override by someone at
    the keyboard; a value from the form is a default. Never the other way
    round, or a stale KV record silently beats the person debugging.

    Auth is `SHELF_CONFIG_TOKEN`, minted at https://heygabi.ai/status/api.
    That token is the secret here; the four values it fetches are not (a
    hostname, a username, a path and a port open nothing without the SSH key,
    which never leaves this machine).
    """
    token = _env("SHELF_CONFIG_TOKEN")
    if not token:
        return {}
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            ESTATE_CONFIG_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any failure is "no config"
        print(f"[shelf-config] estate lookup skipped: {type(exc).__name__}: {exc}")
        return {}

    if not payload.get("configured"):
        print(f"[shelf-config] estate has no connection details yet: {payload.get('fix', '')}")
        return {}
    if payload.get("warning"):
        print(f"[shelf-config] ⚠️ {payload['warning']}")
    cfg = payload.get("config") or {}
    return {k: str(cfg.get(k, "")) for k in ("host", "path", "user", "ssh_port")}


def get_config() -> tuple[bool, str, str, str, int]:
    """(is_configured, host, remote_path, user, port). is_configured requires
    BOTH host and path — a half-set config is still "not configured", never
    a guess at the missing half.

    Reads `.env` first, then falls back to the estate form (see
    `_config_from_estate`). ⚠️ The both-not-blank rule is stated in two places
    — here and in the Worker's /api/machine/shelf-config — and they must agree;
    THIS one is canonical, because this is what actually refuses to run.
    """
    host = _env("SHELF_SERVER_HOST")
    path = _env("SHELF_SERVER_PATH")
    if not (host and path):
        remote = _config_from_estate()
        host = host or remote.get("host", "")
        path = path or remote.get("path", "")
        if remote.get("user") and not _env("SHELF_SERVER_USER"):
            os.environ["SHELF_SERVER_USER"] = remote["user"]
        if remote.get("ssh_port") and not _env("SHELF_SERVER_SSH_PORT"):
            os.environ["SHELF_SERVER_SSH_PORT"] = remote["ssh_port"]
    user = _env("SHELF_SERVER_USER") or getpass.getuser()
    port_raw = _env("SHELF_SERVER_SSH_PORT")
    try:
        port = int(port_raw) if port_raw else 22
    except ValueError:
        port = 22
    return (bool(host and path), host, path, user, port)


def reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    """A raw TCP reachability probe — no rclone/ssh binary required for this
    check, so "unreachable" is reported fast even on a machine with no
    rclone installed yet."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run(dry_run: bool = False) -> ShelfUploadResult:
    """Never raises — degrades honestly at every stage, same 'never break
    the caller' contract as app/pipeline_status.py's own functions."""
    from app.config import ROOT_DIR

    is_configured, host, remote_path, user, port = get_config()
    if not is_configured:
        return ShelfUploadResult(
            ok=False, configured=False, reachable=None,
            message=(
                "SHELF_SERVER_HOST / SHELF_SERVER_PATH are not set — the shelf "
                "server has not been built yet (see docs/access/SHELF_SERVER.md). "
                "Nothing was attempted; this is expected until Justin's box exists."
            ),
        )

    if not reachable(host, port):
        return ShelfUploadResult(
            ok=False, configured=True, reachable=False,
            message=(
                f"Configured ({user}@{host}:{port} -> {remote_path}) but not "
                "reachable right now — the box may be off, asleep, or the "
                "network path (e.g. Tailscale) is down. Nothing was transferred."
            ),
        )

    if shutil.which("rclone") is None:
        return ShelfUploadResult(
            ok=False, configured=True, reachable=True,
            message="rclone is not installed on this machine — cannot push. Install it and retry.",
        )

    remote_spec = f":sftp,host={host},user={user},port={port}:{remote_path}"
    cmd = [
        "rclone", "sync", str(ROOT_DIR), remote_spec,
        "--fast-list", "--transfers", "4", "--checkers", "8",
    ]
    if dry_run:
        cmd.append("--dry-run")

    print(f"  [FORCE-UPLOAD] {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return ShelfUploadResult(
            ok=False, configured=True, reachable=True, returncode=proc.returncode,
            message=f"rclone exited {proc.returncode}: {detail}",
        )

    return ShelfUploadResult(
        ok=True, configured=True, reachable=True, returncode=0,
        message=f"Pushed {ROOT_DIR} -> {user}@{host}:{remote_path} (rclone sync, rc=0).",
    )


def _publish(result: ShelfUploadResult) -> None:
    """Report the result to shelf_upload_status/current — its OWN doc, never
    pipeline_status/current (see pstatus.force_upload_result's docstring for
    why: this is not a pipeline run and must not masquerade as one on the
    status page's primary pipeline row)."""
    try:
        pstatus.force_upload_result(
            ok=result.ok, configured=result.configured, reachable=result.reachable,
            message=result.message,
        )
    except Exception:
        pass


def run_locked(dry_run: bool = False, trigger: str = "manual-force-upload") -> ShelfUploadResult:
    """Public entry point: takes the SAME single-flight lock every pipeline
    step takes (app/core/pipeline_lock.py) — defense in depth, even though
    this is deliberately NOT one of the 7 numbered pipeline steps: reading
    ROOT_DIR mid-sort could otherwise walk a half-moved library. Fails
    loudly+immediately when the lock is held; never defers (same stance as
    every non-scheduled trigger)."""
    try:
        lock = pipeline_lock.acquire(trigger)
    except pipeline_lock.PipelineLockHeld as held:
        print(f"\n[LOCK] BLOCKED: pipeline lock held by {held.holder.describe()}")
        print("[LOCK] Refusing to start the shelf-server force-upload — another run is already in flight.")
        pstatus.blocked_run(trigger, held.holder.describe())
        raise

    try:
        result = run(dry_run=dry_run)
        _publish(result)
        return result
    finally:
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Force a reconciliation push from the local library to the shelf server"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Pass --dry-run to rclone (still requires a reachable, configured target)",
    )
    args = parser.parse_args()

    try:
        result = run_locked(dry_run=args.dry_run)
    except pipeline_lock.PipelineLockHeld:
        return 1

    print(f"  {'[OK]' if result.ok else '[NOT OK]'} {result.message}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
