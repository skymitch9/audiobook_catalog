#!/usr/bin/env python3
"""
Materialise the estate THEME asset from catalog-platform into site/static/.

    python scripts/sync_estate_theme.py           # write the vendored copy
    python scripts/sync_estate_theme.py --check   # fail if it has drifted

WHY THIS EXISTS — the incident, stated plainly.
`hearts` (the fifth theme) shipped into catalog-platform on 2026-08-16 and
reached NOTHING here. The appearance controls in the account modal build their
list from `window.estateTheme.themes`, exactly as the estate guide says to, so
the only thing standing between this site and a new theme was the fact that
somebody had to remember to re-copy two files by hand. Nobody did, and nothing
failed. Owner order 2026-08-17, verbatim: "Add the pink theme as an option for
every site, when a theme is added all sites get it some may just default right
away."

⚠️ WHY A SCRIPT PLUS A TEST, RATHER THAN A PREBUILD STEP LIKE THE OTHER REPOS.
The library and games repos run their sync as `prebuild`/`pretest` and
gitignore the result, because those sites are BUILT. This one is not: site/ is
served straight out of the repo (see .gitignore's warning about ever ignoring
site/static — doing it once caused a full site outage), so the vendored copy
must stay TRACKED. A sync that ran on `pretest` would therefore rewrite tracked
files during a test run, which on this repo means fighting the pipeline's
auto-commit and any concurrent agent for the working tree. So:

    the SCRIPT is how the copy is updated   (run it, commit the result)
    the TEST is how you find out you must   (tests/test_estate_theme_vendor.py)

and the test is read-only. Acceptance, in the owner's terms: theme #6 added to
canonical tomorrow either lands here by running this script, or the suite fails
loudly until it does.

THE ONE DELIBERATE TRANSFORMATION, not a fork: canonical's @font-face rules
point at `/assets/fonts/…` (an absolute path that suits the apex). This site's
pages live at both `/` and `/dev/`, so an absolute font path would 404 on the
dev lane; the URLs are re-rooted to `../fonts/`, relative to
site/static/css/. The rewrite is pattern-checked — zero replacements is a
failure, because that means upstream moved its fonts and this script is now
lying about what it produced.

NOT vendored, on purpose:
  · motion.js — marketing choreography for the apex; no page here has a hero.
  · ab-bridge.css — this site's OWN file, aliasing its legacy `--neon-*`
    vocabulary onto the `--et-*` tokens. It is not canonical's and must not be
    overwritten. Do not add it to any list in here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ONE implementation of "where is the sibling checkout" — app/core/universes.py
# already owns it, tries the three plausible layouts and honours
# CATALOG_PLATFORM_DIR. A second copy here would be a second thing to fix.
from app.core.universes import ENV_VAR, find_platform_dir  # noqa: E402

CANONICAL_SUBPATH = Path("sites") / "heygabi-home" / "public" / "assets"

CSS_DEST = Path("site") / "static" / "css" / "estate-theme.css"
JS_DEST = Path("site") / "static" / "js" / "theme.js"
FONT_DEST_DIR = Path("site") / "static" / "fonts"

# Named explicitly rather than globbed, so a file appearing or vanishing
# upstream is a loud diff here instead of a silent one. The licence text
# travels with the faces: self-hosting under the SIL OFL requires it, and the
# estate's no-third-party-requests rule is why they are self-hosted at all.
FONT_FILES: Tuple[str, ...] = (
    "rajdhani-400.woff2",
    "rajdhani-600.woff2",
    "rajdhani-700.woff2",
    "share-tech-mono-400.woff2",
    "bangers.woff2",
    "luckiest-guy.woff2",
    "OFL-bangers-luckiestguy.txt",
    "OFL-rajdhani-sharetechmono.txt",
)

FONT_URL_FROM = "url('/assets/fonts/"
FONT_URL_TO = "url('../fonts/"


class SyncError(RuntimeError):
    """Something is wrong at the SOURCE. Never write a partial result."""


def _banner(name: str, open_tok: str, close_tok: str) -> str:
    return (
        f"{open_tok} GENERATED COPY - DO NOT EDIT. Source of truth:\n"
        f"{open_tok} catalog-platform/sites/heygabi-home/public/assets/{name}\n"
        f"{open_tok} Refresh it with: python scripts/sync_estate_theme.py\n"
        f"{open_tok} A theme fix or a NEW THEME goes THERE and reaches every estate\n"
        f"{open_tok} site; an edit here dies at this repo and is overwritten.\n"
        f"{open_tok} tests/test_estate_theme_vendor.py fails while this is stale. {close_tok}\n\n"
    )


def canonical_dir() -> Path:
    """The assets directory in the sibling checkout, or raise saying what to do."""
    platform_dir, tried = find_platform_dir()
    if platform_dir is None:
        raise SyncError(
            "cannot find the catalog-platform checkout, which OWNS the estate theme\n"
            "(estate-theme.css + theme.js + the self-hosted faces).\n\nTried:\n"
            + "\n".join(f"  - {t}" for t in tried)
            + f"\n\nFix: clone catalog-platform beside this repo, or set {ENV_VAR} to its root."
        )
    src = platform_dir / CANONICAL_SUBPATH
    if not (src / "estate-theme.css").is_file():
        raise SyncError(
            f"catalog-platform found at {platform_dir}, but {CANONICAL_SUBPATH}/estate-theme.css\n"
            "is not there. The theme asset shipped 2026-08-13 (that repo's\n"
            "docs/info/estate-themes.md) - an old checkout predates it. `git pull` there."
        )
    return src


def render(src: Path) -> Dict[Path, bytes]:
    """
    Everything this sync would write, as {repo-relative path: bytes}.

    Pure: reads the source, touches nothing. Both `--check` and the write path
    go through it, so the test can never disagree with the writer about what
    "current" means.
    """
    out: Dict[Path, bytes] = {}

    css = (src / "estate-theme.css").read_text(encoding="utf-8")
    if not css.strip():
        raise SyncError("estate-theme.css is empty at the source - refusing to copy nothing.")
    hits = css.count(FONT_URL_FROM)
    if hits == 0:
        raise SyncError(
            f"estate-theme.css no longer contains {FONT_URL_FROM}... - upstream moved its\n"
            "fonts, so the re-rooting this script advertises would silently do nothing and\n"
            "every face would 404. Read the new @font-face block, then update FONT_URL_FROM."
        )
    css = css.replace(FONT_URL_FROM, FONT_URL_TO)
    out[CSS_DEST] = (_banner("estate-theme.css", "/*", "*/") + css).encode("utf-8")

    js = (src / "theme.js").read_text(encoding="utf-8")
    if not js.strip():
        raise SyncError("theme.js is empty at the source - refusing to copy nothing.")
    out[JS_DEST] = (_banner("theme.js", "//", "") + js).encode("utf-8")

    for name in FONT_FILES:
        face = src / "fonts" / name
        if not face.is_file():
            raise SyncError(
                f"fonts/{name} is missing at the source.\n"
                "The self-hosted faces are part of the contract (no Google Fonts, ever), and a\n"
                "theme without its faces renders in fallbacks and lies about itself. If the file\n"
                "genuinely moved upstream, update FONT_FILES - do not drop the licence text."
            )
        out[FONT_DEST_DIR / name] = face.read_bytes()

    return out


def _normalise(data: bytes) -> bytes:
    """
    Compare CONTENT, not line endings.

    Nothing in this repo sets .gitattributes, and both sides are LF today, but a
    checkout on a machine configured for CRLF would otherwise report every file
    as drifted forever - a guard that always fires is a guard that gets deleted.
    """
    return data.replace(b"\r\n", b"\n")


def drift(root: Path | None = None) -> List[str]:
    """
    The vendored files that differ from canonical. Empty list means in step.

    This is the whole drift guard; the test is a thin wrapper around it, and
    --check prints it.
    """
    root = root or REPO_ROOT
    stale: List[str] = []
    for rel, body in render(canonical_dir()).items():
        dest = root / rel
        if not dest.is_file():
            stale.append(f"{rel.as_posix()} (missing)")
        elif _normalise(dest.read_bytes()) != _normalise(body):
            stale.append(rel.as_posix())
    return stale


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the vendored copy differs from canonical",
    )
    args = parser.parse_args(argv)

    try:
        src = canonical_dir()
        planned = render(src)
    except SyncError as exc:
        print(f"\nsync_estate_theme: {exc}\n", file=sys.stderr)
        return 1

    print(f"sync_estate_theme: canonical at {src}")

    if args.check:
        stale = drift()
        if stale:
            print(
                "\nsync_estate_theme: the vendored estate theme has DRIFTED from canonical:\n"
                + "\n".join(f"  - {s}" for s in stale)
                + "\n\nFix: python scripts/sync_estate_theme.py   (then commit the result)\n",
                file=sys.stderr,
            )
            return 1
        print(f"sync_estate_theme: in step with canonical ({len(planned)} files checked).")
        return 0

    for rel, body in planned.items():
        dest = REPO_ROOT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Byte writes throughout: no newline translation, so a re-run of this
        # script on Windows cannot silently rewrite every line of every file.
        if dest.is_file() and dest.read_bytes() == body:
            continue
        dest.write_bytes(body)
        print(f"  wrote {rel.as_posix()}")

    print(f"sync_estate_theme: {len(planned)} file(s) in step with canonical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
