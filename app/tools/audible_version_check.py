"""Is a newer ``audible`` / ``audible-cli`` on PyPI? — report only, never installs.

Why: every Audible download on this machine goes through these two packages,
and nothing checked them for updates. When Audible changes its API the fix
arrives as a new release, and the first symptom here would otherwise be a
stream of failing downloads. ``purchase_audit`` runs ``check`` at most once
per 7 days in a bounded child under the audible-cli interpreter and owns the
state and the "post only a version not already reported" rule.

⚠️ "Installed" means installed in THE INTERPRETER THAT RUNS THIS. audible-cli
lives in the Store Python 3.12 only (see ``purchase_audit.AUDIT_PYTHON``);
run under ``.venv`` it would truthfully say "not installed".

Privacy: one anonymous GET per package to ``pypi.org/pypi/<name>/json`` with a
generic User-Agent; nothing about this machine or household is sent.

Usage:
    python -m app.tools.audible_version_check    # print installed vs latest
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from importlib import metadata

DISTRIBUTIONS = ("audible", "audible-cli")
PYPI_URL = "https://pypi.org/pypi/{name}/json"
TIMEOUT_S = 20
USER_AGENT = "version-check/1.0 (+https://pypi.org)"


def installed_version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def latest_on_pypi(dist: str, timeout: float = TIMEOUT_S) -> str:
    req = urllib.request.Request(PYPI_URL.format(name=dist),
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return str(json.loads(resp.read().decode("utf-8"))["info"]["version"])


def _key(version: str):
    """Comparable form. ``packaging`` when importable (it is, under Store
    3.12, 2026-11-23); otherwise the leading dotted integers, which is enough
    for the plain X.Y.Z releases both projects publish."""
    try:
        from packaging.version import Version
        return (0, Version(version))
    except Exception:
        nums = re.match(r"\d+(?:\.\d+)*", version or "")
        return (1, tuple(int(p) for p in nums.group(0).split("."))) if nums else (2, ())


def is_newer(latest: str | None, installed: str | None) -> bool:
    if not latest or not installed:
        return False
    a, b = _key(latest), _key(installed)
    return a[0] == b[0] and a[1] > b[1]


def check(fetch=latest_on_pypi) -> dict:
    """{dist: {installed, latest, newer, error}} for every distribution.
    JSON-only and never raises: one package's network failure is that
    package's ``error``, not the whole check's."""
    out: dict = {}
    for dist in DISTRIBUTIONS:
        have = installed_version(dist)
        entry = {"installed": have, "latest": None, "newer": False, "error": None}
        try:
            entry["latest"] = fetch(dist)
            entry["newer"] = is_newer(entry["latest"], have)
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"[:200]
        out[dist] = entry
    return out


def describe(result: dict) -> str:
    """One line for a log."""
    parts = []
    for dist, e in result.items():
        if e.get("error"):
            parts.append(f"{dist} {e.get('installed') or 'not installed'} (PyPI unreachable: {e['error']})")
        elif e.get("newer"):
            parts.append(f"{dist} {e.get('installed')} -> {e['latest']} AVAILABLE")
        else:
            parts.append(f"{dist} {e.get('installed') or 'not installed'} (latest {e.get('latest')})")
    return "; ".join(parts)


def main() -> int:
    result = check()
    print(f"interpreter: {sys.executable}")
    for dist, e in result.items():
        status = ("PyPI unreachable: " + e["error"]) if e["error"] else (
            "NEWER VERSION AVAILABLE" if e["newer"] else "up to date")
        print(f"  {dist:12} installed {e['installed'] or 'NOT INSTALLED':10} "
              f"latest {e['latest'] or '?':10} {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
