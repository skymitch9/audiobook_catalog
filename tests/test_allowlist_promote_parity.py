"""F4 (2026-08-24 sanctity audit): the commit allowlist is the SAME contract at
two altitudes — what the pipeline may COMMIT (``_ALLOWLIST`` in
``scripts/sync_to_drive.py``) and what the auto-promote gate will PROMOTE (the
``allow=`` regex in ``.github/workflows/auto-promote.yml``). They drifted by one
entry on 2026-08-19 (``site/ebooks_status.json`` added to Python and not the
workflow): three days of books reached /dev/ and never prod, silently, because
the runs in between reported "skipped" rather than failing.

A per-side test pinned the Python list, and the workflow carried a prose plea to
"add it in both". Nothing MECHANICALLY cross-checked the two. This test is that
tripwire: it parses both and asserts every path the pipeline may commit is
accepted by the promote gate (Python allowlist ⊆ workflow allow-set). A future
one-sided edit now fails CI instead of shipping a silent three-day gap.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SYNC = _REPO / "scripts" / "sync_to_drive.py"
_WORKFLOW = _REPO / ".github" / "workflows" / "auto-promote.yml"


def _python_allowlist() -> list[str]:
    """Extract the ``_ALLOWLIST = [...]`` string literal from the source via
    AST — no import, no running the pipeline, works on a local variable."""
    tree = ast.parse(_SYNC.read_text(encoding="utf-8"))
    found: list[list[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_ALLOWLIST":
                    if isinstance(node.value, ast.List):
                        found.append([
                            el.value for el in node.value.elts
                            if isinstance(el, ast.Constant)
                            and isinstance(el.value, str)
                        ])
    assert len(found) == 1, (
        f"expected exactly one _ALLOWLIST assignment, found {len(found)}"
    )
    assert found[0], "_ALLOWLIST parsed as empty — the AST extraction broke"
    return found[0]


def _workflow_allow_regex() -> str:
    """Pull the single-quoted ``allow='...'`` regex out of the workflow YAML."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    m = re.search(r"allow='([^']+)'", text)
    assert m, "could not find the allow='...' regex in auto-promote.yml"
    return m.group(1)


def test_python_allowlist_is_subset_of_promote_gate():
    """Every path the pipeline may COMMIT must be PROMOTABLE. If a future edit
    adds a filename to _ALLOWLIST without adding it to the workflow regex, the
    gate would silently refuse the commit — this asserts it cannot."""
    allowlist = _python_allowlist()
    regex = _workflow_allow_regex()
    pattern = re.compile(regex)

    rejected = [p for p in allowlist if not pattern.match(p)]
    assert not rejected, (
        "these committed paths are NOT accepted by the auto-promote allow= "
        f"regex (one-sided allowlist drift — add them to auto-promote.yml in "
        f"the same commit): {rejected}"
    )


def test_promote_gate_is_not_trivially_permissive():
    """Guards the test above: a regex of ``.*`` would make the subset check
    pass vacuously. Assert the gate still REJECTS a path outside the contract
    (e.g. a secret or an arbitrary source file)."""
    pattern = re.compile(_workflow_allow_regex())
    for outside in ("access/CREDENTIALS.md", "scripts/sync_to_drive.py",
                    "site/secrets.json", "README.md"):
        assert not pattern.match(outside), (
            f"the promote gate unexpectedly accepts {outside!r} — the allow= "
            "regex is too permissive to be a meaningful guard"
        )


def test_known_allowlist_members_are_present():
    """Sanity that the AST extraction found the real list, not something else:
    the load-bearing catalog files must be in it."""
    allowlist = set(_python_allowlist())
    for required in ("site/catalog.csv", "site/index.html",
                     "site/ebooks_status.json", "author_drive_map.json"):
        assert required in allowlist, f"{required} missing from _ALLOWLIST"
