#!/usr/bin/env python3
"""
Materialise the estate SEARCH component from catalog-platform into site/estate/.

    python scripts/sync_estate_search.py           # write the vendored copy
    python scripts/sync_estate_search.py --check   # fail if it has drifted

WHY THIS EXISTS — the measurement, stated plainly.
`site/estate/estate-search.js` was the estate's FOURTH copy of one component
and the ONLY one nobody synced. `library_catalog` and `Board_Game_Catalog` each
run `scripts/sync-estate-search.mjs` in a prebuild step; the apex owns the
original; this site had a hand copy, vendored 2026-08-17, whose own banner said
"refresh is manual". Measured 2026-09-05 by
`catalog-platform/docs/info/multi-library-survey-2026-09-05.md` §5: the apex and
library copies were byte-identical (md5 `90840e74…`), the games copy was a
generated artifact one build behind, and this one was **23 lines behind upstream
with four real divergences** — of which two were user-visible behaviour, not
formatting:

  1. its own DO-NOT-EDIT banner (expected, and this script writes one too);
  2. `SOURCE_LABELS` had no `library2` at all and called the main library
     "library", so a second household's shelf could not be named here;
  3. the missing `a.es-hit-cover { cursor: pointer }` + `:focus-visible` rules;
  4. 🔴 **the cover was not a link.** Upstream makes it an `<a>` routed through
     `_openHit` (so the cancelable `estate-search:select` event still fires and
     this site's own hash routing still runs); the hand copy built an inert
     `aria-hidden` `<span>`. Clicking a cover here did nothing.

None of that failed anything. Nothing went red, no page broke, and the drift
was found by an audit rather than by use — which is exactly the shape of the
`hearts` theme incident that produced `sync_estate_theme.py`, and why this
script is that script's twin rather than a new invention.

⚠️ WHY A SCRIPT PLUS A TEST, RATHER THAN A PREBUILD STEP LIKE THE OTHER REPOS.
Copied verbatim from `sync_estate_theme.py`'s reasoning, because it applies
here word for word: the library and games repos run their sync as
`prebuild`/`pretest` and gitignore the result, because those sites are BUILT.
This one is not — `site/` is served straight out of the repo (see .gitignore's
warning about ever ignoring `site/static`; doing it once caused a full site
outage), so the vendored copy must stay TRACKED. A sync that ran on `pretest`
would rewrite tracked files during a test run, which on this repo means
fighting the pipeline's auto-commit and any concurrent agent for the working
tree. So:

    the SCRIPT is how the copy is updated   (run it, commit the result)
    the TEST is how you find out you must   (tests/test_estate_search_vendor.py)

and the test is read-only.

⚠️ AND IT IS DELIBERATELY **NOT** IN THE PIPELINE'S COMMIT ALLOWLIST.
`scripts/sync_to_drive.py`'s `_ALLOWLIST` is data the pipeline itself GENERATES
(catalog.csv, the manifests), mirrored in `.github/workflows/auto-promote.yml`'s
`allow=` regex and pinned by `tests/test_allowlist_promote_parity.py`. This file
is vendored SOURCE that a person or an agent updates and commits — the same
place `site/static/css/estate-theme.css` sits, and for the same reason. Adding
it there would also make every re-vendor look like a book-only auto-commit and
self-promote to prod, which is not a decision a `git add` should make.

  ⚠️ The dispatch brief for this work said to add "a call site in
  `scripts/sync_to_server.py`". Measured 2026-09-06: that script is the
  **shelf-server rclone push** — it uploads the AUDIO LIBRARY to a box that
  `docs/access/SHELF_SERVER.md` still records as NOT YET BUILT, it is
  explicitly "deliberately NOT a pipeline step", and it touches nothing under
  `site/`. A call site there would have been a sync that never ran. The
  call site is the test, per the twin above.

THE ONE DELIBERATE TRANSFORMATION, not a fork: the banner, prepended. Nothing
else is changed — no path re-rooting (unlike the theme's fonts), no minifying,
no edits. Everything below the banner is upstream, byte for byte, and the drift
check is what proves it.

NOT vendored, on purpose — the two modules the component may dynamically
import. Both decisions are older than this script and it must not quietly
reverse them:
  · estate-auth.js — only imported when no `.authAdapter` property is set.
    `site/estate-search-mount.js` always sets one (built over site/identity.js's
    existing Firebase app), so the import never fires. Vendoring it would invite
    a SECOND Firebase app, the exact hazard the adapter exists to avoid.
  · estate-scan.js — only fetched when the `scan` attribute is present. This
    embed does not set it.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ONE implementation of "where is the sibling checkout" — app/core/universes.py
# already owns it, tries the three plausible layouts and honours
# CATALOG_PLATFORM_DIR. A second copy here would be a second thing to fix.
from app.core.universes import ENV_VAR, find_platform_dir  # noqa: E402

CANONICAL_SUBPATH = Path("sites") / "heygabi-home" / "public" / "assets"
CANONICAL_NAME = "estate-search.js"

JS_DEST = Path("site") / "estate" / "estate-search.js"
PROVENANCE_DEST = Path("site") / "estate" / "SOURCE-estate-search.txt"

# ⚠️ The banner is SEVEN lines and the count is load-bearing for one reason
# only: the old hand-refresh recipe in SOURCE-estate-search.txt was
# `sed -n '1,6p'` — keep the first six lines, paste upstream after them. That
# recipe is retired by this script (it is printed into the provenance file as
# history, not as instructions), so nothing now depends on the number; it is
# recorded here so a reader who meets the old recipe knows why it no longer
# matches.
BANNER_LINES = 7


class SyncError(RuntimeError):
    """Something is wrong at the SOURCE. Never write a partial result."""


def _banner() -> str:
    return (
        "// ⚠️ DO NOT EDIT — GENERATED COPY. Upstream owns this file:\n"
        f"//   catalog-platform/{CANONICAL_SUBPATH.as_posix()}/{CANONICAL_NAME}\n"
        "// Refresh it with: python scripts/sync_estate_search.py\n"
        "// A fix or a new feature goes THERE and reaches every estate site; an\n"
        "// edit here dies at this repo and is overwritten on the next sync.\n"
        "// tests/test_estate_search_vendor.py fails while this is stale.\n"
        "// Everything below this banner is upstream, verbatim.\n"
    )


def canonical_file() -> Path:
    """The component in the sibling checkout, or raise saying what to do."""
    platform_dir, tried = find_platform_dir()
    if platform_dir is None:
        raise SyncError(
            "cannot find the catalog-platform checkout, which OWNS the estate search\n"
            "component (<estate-search>).\n\nTried:\n"
            + "\n".join(f"  - {t}" for t in tried)
            + f"\n\nFix: clone catalog-platform beside this repo, or set {ENV_VAR} to its root."
        )
    src = platform_dir / CANONICAL_SUBPATH / CANONICAL_NAME
    if not src.is_file():
        raise SyncError(
            f"catalog-platform found at {platform_dir}, but "
            f"{CANONICAL_SUBPATH.as_posix()}/{CANONICAL_NAME}\n"
            "is not there. An old checkout predates it — `git pull` in that repo."
        )
    return src


def _provenance(upstream: str, sha256: str, when: str) -> str:
    """The committed provenance note. It ships with the site and is harmless
    (it names a path and a hash, never a secret), and it is what a reader meets
    first when they wonder where this file came from."""
    return f"""estate-search.js — provenance and refresh procedure
====================================================

⚠️ DO NOT EDIT site/estate/estate-search.js. It is a GENERATED copy; the one
canonical implementation lives upstream in catalog-platform:

  Upstream:  catalog-platform/{upstream}
  Synced:    {when}
  Upstream sha256 at sync time (the copy = this content with a
  {BANNER_LINES}-line DO-NOT-EDIT banner prepended, nothing else changed):
    {sha256}

  ⚠️ THIS FILE IS GENERATED TOO — by scripts/sync_estate_search.py. Editing it
  by hand records a hash that nothing produced.

Refresh:

  python scripts/sync_estate_search.py           # write the copy
  python scripts/sync_estate_search.py --check   # what the test runs

⚠️ The old procedure — the one this file used to carry — was a hand recipe
(`sed -n '1,6p' … ; cat "$UP"` then `sha256sum` and edit this file). It was
followed once, on 2026-08-17, and never again: measured 2026-09-05, this copy
was 23 lines behind upstream with four divergences, one of which (the cover was
an inert span rather than a link) was a real, user-visible behaviour gap. The
recipe is recorded here as history, not as instructions.

Why a committed copy and not a build-time sync: the library and games repos
copy this file from the sibling checkout in a prebuild step
(scripts/sync-estate-search.mjs there) and gitignore the result, because those
sites are BUILT. This one is not — site/ is served straight out of the repo —
so the copy must stay TRACKED, and a sync running on `pretest` would rewrite
tracked files mid-test and fight the pipeline's auto-commit for the tree. Same
argument, same shape, as scripts/sync_estate_theme.py. Loading it cross-origin
from heygabi.ai was rejected on purpose: it adds module-CORS coupling the other
repos deliberately avoided.

Sibling modules deliberately NOT vendored:
  - estate-auth.js — only dynamically imported when no .authAdapter property
    is set. site/estate-search-mount.js always sets one (built over
    site/identity.js's existing Firebase app), so the import never fires.
    Vendoring it would invite a SECOND Firebase app — the exact hazard the
    adapter exists to avoid.
  - estate-scan.js — only fetched when the `scan` attribute is present. This
    embed does not set it.
  - assets/catalog-registry.js — the apex's registry client. The component
    carries its OWN inline copy of that fetch and those label helpers, on
    purpose (catalog-platform/docs/info/catalog-registry.md §10a): the sync
    copies exactly ONE file, and a sibling import would 404 here and on both
    other consumer sites.

Consumer wiring: site/estate-search-mount.js (hand-authored, tested in
site/__tests__/estate-search-mount.test.js). Embedded on index.html via
app/web/templates/index.html — edit the TEMPLATE, never the generated
site/index.html alone.
"""


def render(src: Path, when: str | None = None) -> Dict[Path, bytes]:
    """
    Everything this sync would write, as {repo-relative path: bytes}.

    Pure apart from the date: reads the source, touches nothing. Both `--check`
    and the write path go through it, so the test can never disagree with the
    writer about what "current" means.

    ⚠️ `when` exists because the provenance file carries a DATE, and a date
    makes `render()` impure — which would make the drift check fail every day
    at midnight for no reason. `drift()` passes the date already ON DISK, so a
    re-run compares content and not the calendar.
    """
    body = src.read_text(encoding="utf-8")
    if not body.strip():
        raise SyncError(f"{CANONICAL_NAME} is empty at the source - refusing to copy nothing.")
    # ⚠️ A cheap sanity check on the SOURCE, not a schema: this is a custom
    # element definition, and a file that no longer defines one is not the
    # component — it is something else that happens to have the right name.
    if "customElements.define" not in body:
        raise SyncError(
            f"{CANONICAL_NAME} at the source does not call customElements.define.\n"
            "That is not the <estate-search> component; refusing to vendor it."
        )

    raw = src.read_bytes()
    sha = hashlib.sha256(_normalise(raw)).hexdigest()
    upstream = f"{CANONICAL_SUBPATH.as_posix()}/{CANONICAL_NAME}"

    return {
        JS_DEST: (_banner() + body).encode("utf-8"),
        PROVENANCE_DEST: _provenance(upstream, sha, when or date.today().isoformat()).encode("utf-8"),
    }


def _normalise(data: bytes) -> bytes:
    """
    Compare CONTENT, not line endings.

    This machine has core.autocrlf on, so a fresh checkout puts CRLF on disk
    while this script produces LF; an exact comparison would rewrite every file
    on every run, dirty the tree for no reason, and trip the deploy's clean-tree
    guard. A guard that always fires is a guard that gets deleted.

    ⚠️ The sha256 in the provenance file is taken over the NORMALISED bytes for
    the same reason — otherwise the recorded hash would depend on which machine
    ran the sync.
    """
    return data.replace(b"\r\n", b"\n")


def _synced_date_on_disk(root: Path) -> str | None:
    """The `Synced:` date already recorded, so a drift check compares content
    rather than the calendar. `None` when the file is missing or unreadable."""
    dest = root / PROVENANCE_DEST
    if not dest.is_file():
        return None
    for line in dest.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("Synced:"):
            return line.split(":", 1)[1].strip() or None
    return None


def drift(root: Path | None = None) -> List[str]:
    """
    The vendored files that differ from canonical. Empty list means in step.

    This is the whole drift guard; the test is a thin wrapper around it, and
    --check prints it.
    """
    root = root or REPO_ROOT
    planned = render(canonical_file(), when=_synced_date_on_disk(root))
    stale: List[str] = []
    for rel, body in planned.items():
        dest = root / rel
        if not dest.is_file():
            stale.append(f"{rel.as_posix()} (missing)")
        elif _normalise(dest.read_bytes()) != _normalise(body):
            stale.append(rel.as_posix())
    return stale


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the vendored copy differs from canonical",
    )
    args = parser.parse_args(argv)

    try:
        src = canonical_file()
        if args.check:
            stale = drift()
            if stale:
                print(
                    "\nsync_estate_search: the vendored estate search component has DRIFTED "
                    "from canonical:\n"
                    + "\n".join(f"  - {s}" for s in stale)
                    + "\n\nFix: python scripts/sync_estate_search.py   (then commit the result)\n",
                    file=sys.stderr,
                )
                return 1
            print(f"sync_estate_search: canonical at {src}")
            print("sync_estate_search: in step with canonical (2 files checked).")
            return 0
        planned = render(src)
    except SyncError as exc:
        print(f"\nsync_estate_search: {exc}\n", file=sys.stderr)
        return 1

    print(f"sync_estate_search: canonical at {src}")

    wrote = 0
    for rel, body in planned.items():
        dest = REPO_ROOT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Byte writes throughout: no newline translation, so a re-run on Windows
        # cannot silently rewrite every line of the file. The skip compares
        # NORMALISED, for the reason _normalise() gives.
        if dest.is_file() and _normalise(dest.read_bytes()) == _normalise(body):
            continue
        dest.write_bytes(body)
        wrote += 1
        print(f"  wrote {rel.as_posix()}")

    print(f"sync_estate_search: {len(planned)} file(s) in step with canonical ({wrote} rewritten).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
