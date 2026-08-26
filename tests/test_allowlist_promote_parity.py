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

⚠️ **Both directions, 2026-08-26.** The first version of this file checked ONE
direction (Python ⊆ workflow) and so caught only half the drift class. The
other half is just as silent and one shade worse: a filename added to the
WORKFLOW and not to ``_ALLOWLIST`` makes the gate advertise a path the pipeline
can never produce — the promote succeeds, the file is simply never there, and
nothing anywhere says so. ``test_promote_gate_carries_nothing_the_pipeline_cannot_commit``
below closes it.

⚠️ **The two lists are NOT identical, and the difference is DELIBERATE.** It is
enumerated in ``INTENDED_WORKFLOW_ONLY`` with its reason, so an intended
difference is a declaration in this file rather than a silently-tolerated
mismatch. Adding a member there is the explicit act of saying "these two may
differ here, because…"; anything not listed is drift and fails.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SYNC = _REPO / "scripts" / "sync_to_drive.py"
_WORKFLOW = _REPO / ".github" / "workflows" / "auto-promote.yml"

# ---------------------------------------------------------------------------
# The INTENDED difference between the two lists — the only difference allowed.
#
# Everything else must appear on both sides. Each key is an alternative from
# the workflow's allow= regex that has NO counterpart in _ALLOWLIST, and the
# value is why that is correct. A future entry here is a deliberate decision
# somebody wrote down; drift is everything that is not here.
# ---------------------------------------------------------------------------
INTENDED_WORKFLOW_ONLY: dict[str, str] = {
    "site/covers/.*": (
        "HISTORICAL, not current. Cover blobs left git on 2026-08-10 for "
        "Cloudflare R2 and site/covers/ is now gitignored, so the pipeline "
        "CANNOT commit one — naming it in _ALLOWLIST would make `git add` "
        "exit 1 on an ignored path. The gate keeps matching it so that "
        "promoting an OLD commit (from before the move) still passes. "
        "site/covers_manifest.json is its live replacement and IS on both "
        "sides. See the comment above _ALLOWLIST in scripts/sync_to_drive.py."
    ),
}

# Paths the pipeline may commit that the gate is NOT meant to promote. Empty on
# purpose: a commit the pipeline makes and the gate refuses is exactly the
# 2026-08-19 three-day silent gap. It exists so the direction is stated rather
# than assumed, and so adding one is a visible decision.
INTENDED_PIPELINE_ONLY: dict[str, str] = {}


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


def _split_top_level(body: str) -> list[str]:
    """Split ``a|b(c|d)|e`` on the top-level ``|`` only, so a nested group's
    own alternation stays with it."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    escaped = False
    for ch in body:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p]


def _expand(alt: str) -> list[str]:
    """Expand ONE regex alternative into the concrete strings it accepts.

    Handles exactly the shape the workflow uses — a literal prefix, one
    optional nested alternation group, a literal suffix — because that is the
    contract this test pins. ⚠️ A future regex the parser cannot expand must
    FAIL rather than silently expand to nothing: an empty expansion would make
    every assertion below pass vacuously, which is the failure mode this whole
    file exists to prevent. `_workflow_alternatives` asserts non-emptiness.
    """
    m = re.search(r"\(([^()]*)\)", alt)
    if not m:
        return [alt]
    prefix, suffix = alt[: m.start()], alt[m.end():]
    out: list[str] = []
    for inner in _split_top_level(m.group(1)):
        out.extend(_expand(prefix + inner + suffix))
    return out


_META = re.compile(r"[*+?\[\]{}^$]|\.\*")


def _workflow_alternatives() -> tuple[set[str], set[str]]:
    """``(literal_paths, pattern_alternatives)`` accepted by the allow= regex.

    A *literal* is a concrete filename the gate promotes and the pipeline is
    therefore expected to be able to commit. A *pattern* (anything still
    carrying a regex metacharacter after unescaping, e.g. ``site/covers/.*``)
    matches a whole class of paths and can only be reconciled by declaring it
    in ``INTENDED_WORKFLOW_ONLY``.
    """
    regex = _workflow_allow_regex()
    body = regex
    assert body.startswith("^(") and body.endswith(")$"), (
        "the allow= regex no longer has the anchored ^(...)$ shape this parser "
        f"understands — update the parser, do not delete the test: {regex!r}"
    )
    body = body[2:-2]

    literals: set[str] = set()
    patterns: set[str] = set()
    for alt in _split_top_level(body):
        for expanded in _expand(alt):
            if _META.search(expanded):
                patterns.add(expanded)
            else:
                literals.add(expanded.replace("\\.", "."))

    assert literals, (
        "parsed ZERO literal paths out of the allow= regex — the parser is "
        "broken and every parity assertion below would pass vacuously"
    )
    return literals, patterns


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


def test_promote_gate_carries_nothing_the_pipeline_cannot_commit():
    """THE OTHER DIRECTION (F4, 2026-08-26): workflow ⊆ _ALLOWLIST + the
    declared intended difference.

    A filename added to the gate and not to ``_ALLOWLIST`` is drift too, and
    it is quieter than the 2026-08-19 direction: the promote SUCCEEDS, the
    file is simply never in the commit, and nothing reports a thing. This
    asserts every concrete path the gate advertises is one the pipeline can
    actually produce — unless it is enumerated in ``INTENDED_WORKFLOW_ONLY``
    with a written reason.
    """
    allowlist = set(_python_allowlist())
    literals, patterns = _workflow_alternatives()

    undeclared = sorted(
        (literals | patterns) - allowlist - set(INTENDED_WORKFLOW_ONLY)
    )
    assert not undeclared, (
        "the auto-promote allow= regex accepts paths that scripts/"
        "sync_to_drive.py's _ALLOWLIST cannot commit, and they are NOT "
        "declared as an intended difference (one-sided allowlist drift — the "
        "gate would promote a file the pipeline never writes). Add them to "
        "_ALLOWLIST in the same commit, or, if the difference is deliberate, "
        f"add them to INTENDED_WORKFLOW_ONLY in this file with the reason: {undeclared}"
    )


def test_every_pattern_alternative_is_a_declared_difference():
    """A wildcard alternative (``site/covers/.*``) can never equal a literal
    in ``_ALLOWLIST``, so it MUST be declared. This stops a future ``site/.*``
    quietly widening the gate to the whole site tree while the parity tests
    above still read green."""
    _literals, patterns = _workflow_alternatives()
    undeclared = sorted(patterns - set(INTENDED_WORKFLOW_ONLY))
    assert not undeclared, (
        "the allow= regex has WILDCARD alternatives that are not declared in "
        "INTENDED_WORKFLOW_ONLY. A wildcard widens what prod will accept "
        "without any matching change to what the pipeline commits — declare "
        f"it with its reason, or remove it: {undeclared}"
    )


def test_intended_differences_are_still_real():
    """Guards the escape hatch itself. A declaration that no longer
    corresponds to anything in either list is stale, and a stale exemption is
    how a real drift later gets waved through."""
    allowlist = set(_python_allowlist())
    literals, patterns = _workflow_alternatives()

    stale_workflow = sorted(set(INTENDED_WORKFLOW_ONLY) - (literals | patterns))
    assert not stale_workflow, (
        "INTENDED_WORKFLOW_ONLY declares differences that are no longer in "
        f"the auto-promote allow= regex — delete the stale entries: {stale_workflow}"
    )

    contradictory = sorted(set(INTENDED_WORKFLOW_ONLY) & allowlist)
    assert not contradictory, (
        "these paths are declared 'workflow only' but ARE in _ALLOWLIST — the "
        f"declaration contradicts the code, remove it: {contradictory}"
    )

    stale_pipeline = sorted(set(INTENDED_PIPELINE_ONLY) - allowlist)
    assert not stale_pipeline, (
        "INTENDED_PIPELINE_ONLY declares paths that are no longer in "
        f"_ALLOWLIST — delete the stale entries: {stale_pipeline}"
    )

    for path, reason in INTENDED_WORKFLOW_ONLY.items():
        assert reason.strip(), f"{path} is declared an intended difference with no reason"


def test_known_allowlist_members_are_present():
    """Sanity that the AST extraction found the real list, not something else:
    the load-bearing catalog files must be in it."""
    allowlist = set(_python_allowlist())
    for required in ("site/catalog.csv", "site/index.html",
                     "site/ebooks_status.json", "author_drive_map.json"):
        assert required in allowlist, f"{required} missing from _ALLOWLIST"
