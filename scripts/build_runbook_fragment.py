"""Render a LOCAL-ONLY runbook markdown file into an estate doc fragment.

The `/runbooks/<slug>/` pages on heygabi.ai are content-free shims: they sign
the caller in, then fetch `GET /api/estate/docs/<slug>` from the auth Worker,
which returns one `{ html }` blob out of the `estate_docs` KV namespace. That
blob is a *fragment* — a `<style>` block plus a `<main>` — never a whole page.
See `catalog-platform/apps/auth-worker/src/docs.ts` for why it is KV and not a
bundled module (that repo is PUBLIC; these runbooks are exactly what the devops
gate exists to fence).

⚠️ WHY THIS SCRIPT EXISTS. The first fragments were hand-authored HTML, and
they rotted the moment the markdown moved on: on 2026-08-20 the published page
still read "Status: NOT YET RUN" and still told Justin to run cloudflared with
`--network host` — an instruction that, by then, would have BROKEN the working
tunnel. Hand-maintaining a second copy of a 700-line document is not a thing
anyone does twice, so the copy silently became a liability. A generator makes
the regen a single command, which is the only version of this that stays true.

Usage (from the audiobook_catalog repo root):

    python -m scripts.build_runbook_fragment docs/access/SHELF_SERVER.md \
        --slug shelf-server --out docs/access/SHELF_SERVER.fragment.html

Then publish it, from `catalog-platform/apps/auth-worker/`:

    npx wrangler kv key put --binding estate_docs "doc:shelf-server" \
        --path <fragment.html> --remote

The style block is taken from the existing fragment when one is present, so the
page keeps the design it was given; `--style-from` overrides the source. That
is deliberate — this script owns the *content*, not the look.
"""

from __future__ import annotations

import argparse
import html as html_mod
import re
import sys
from pathlib import Path

try:
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover - environment guard
    sys.exit(
        "markdown-it-py is required: pip install markdown-it-py\n"
        "(it is already present in this repo's .venv as of 2026-08-20)"
    )

HEADER_COMMENT = """<!-- Estate doc fragment doc:{slug} — served by auth-worker
     GET /api/estate/docs/{slug} to devops/approver callers only.
     Source of truth: audiobook_catalog {source} (LOCAL ONLY).
     ⚠️ GENERATED — do not hand-edit. Regenerate with:
       python -m scripts.build_runbook_fragment {source} --slug {slug} --out <this file>
     then publish with the wrangler kv command in SHELF_SERVER.md §12. -->
"""

# Fallback only — normally lifted from the fragment being replaced.
DEFAULT_STYLE = """<style>
  :root {
    --bg: #faf8f4; --surface: #ffffff; --ink: #26221c; --muted: #6d675d;
    --accent: #7a5c2e; --accent-soft: #f0e8d8; --hairline: #e3ddd1;
    --code-bg: #f2efe8; --warn-bg: #fdf3e2; --warn-edge: #d9a03f; --owner: #8c3b2e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1c1a16; --surface: #24211c; --ink: #e9e4da; --muted: #a49c8e;
      --accent: #d3b071; --accent-soft: #35301f; --hairline: #3a362e;
      --code-bg: #2a2721; --warn-bg: #33290f; --warn-edge: #d9a03f; --owner: #e08a76;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  main { max-width: 52rem; margin: 0 auto; padding: 2.5rem 1.25rem 5rem; }
  table { border-collapse: collapse; width: 100%; font-size: .9rem; margin: .9rem 0; }
  .tw { overflow-x: auto; }
  th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--hairline); vertical-align: top; }
  code { font-family: ui-monospace, SFMono-Regular, Consolas, Menlo, monospace;
         font-size: .88em; background: var(--code-bg); padding: .1em .35em; border-radius: 4px; }
  pre { background: var(--code-bg); border: 1px solid var(--hairline); border-radius: 8px;
        padding: .9rem 1rem; overflow-x: auto; font-size: .84rem; line-height: 1.55; }
  pre code { background: none; padding: 0; }
</style>"""


def extract_style(path: Path | None) -> str:
    """Lift the <style>…</style> block out of an existing fragment."""
    if path is None or not path.exists():
        return DEFAULT_STYLE
    text = path.read_text(encoding="utf-8")
    m = re.search(r"<style>.*?</style>", text, re.S)
    return m.group(0) if m else DEFAULT_STYLE


INCLUDE_RE = re.compile(r"^[ \t]*<!--\s*include:\s*(\S+?)\s*-->[ \t]*$", re.M)

# Narrow on purpose. A broad "looks like a secret" heuristic would false-positive
# on the placeholder lines these scripts legitimately carry
# (`SHELF_PARITY_TOKEN=<the value the page gave you>`). These two prefixes are
# the estate's actual minted-key shapes, and a real one reaching a published
# page is the failure worth refusing outright.
SECRET_PREFIXES = ("shelfpar_", "sk-ant-")


def expand_includes(md_text: str, base_dir: Path) -> str:
    """`<!--include: justin/02-abs-hardlinks.sh -->` -> a fenced, labelled block.

    ⚠️ WHY THIS EXISTS, and it is the same reason as the generator itself.
    Justin cannot be handed a file — the runbook is the delivery mechanism — so
    the script text has to appear ON the page. Pasting it into the markdown
    would create a THIRD copy of a script that already exists twice (the file,
    and whatever he saved on his box), and the copy nobody runs is the copy
    that silently goes stale. The published block is therefore READ FROM THE
    SCRIPT FILE at build time: regenerating the page cannot produce a version
    that disagrees with what is in the repo.

    The `data-filename` attribute is what the shim's doc.js hangs the Copy and
    Download buttons off — see runbooks/shelf-justin/doc.js.
    """

    def sub(m: re.Match[str]) -> str:
        rel = m.group(1)
        target = (base_dir / rel).resolve()
        # Contain it: an include is for files beside the runbook, never a walk
        # up into the repo (or out of it) via `../../.env`.
        try:
            target.relative_to(base_dir.resolve())
        except ValueError:
            sys.exit(f"ERROR: include escapes {base_dir}: {rel}")
        if not target.exists():
            # Loudly, not silently. A missing script must fail the build rather
            # than publish a page with a gap where the instructions were.
            sys.exit(f"ERROR: include not found: {target}")

        text = target.read_text(encoding="utf-8")
        for prefix in SECRET_PREFIXES:
            if prefix in text:
                sys.exit(
                    f"ERROR: {target.name} contains a value starting '{prefix}' — "
                    "that looks like a minted key. Refusing to publish it."
                )
        # ``` inside the file would break out of the fence; use a longer one.
        fence = "`" * max(3, max((len(r) for r in re.findall(r"`+", text)), default=0) + 1)
        return (
            f'<div class="script" data-filename="{html_mod.escape(target.name, quote=True)}">\n\n'
            f"{fence}bash\n{text.rstrip()}\n{fence}\n\n"
            "</div>"
        )

    return INCLUDE_RE.sub(sub, md_text)


def render_markdown(md_text: str) -> str:
    md = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    return md.render(md_text)


def number_headings(body: str) -> str:
    """`<h2>1. Status board</h2>` -> `<h2><span class="n">1</span> Status board</h2>`.

    The stylesheet colours `h2 .n` as a section number; without this the
    numbers render as ordinary text and the page loses its spine.
    """
    return re.sub(
        r"<h2([^>]*)>(\d+)\.\s*",
        r'<h2\1><span class="n">\2</span> ',
        body,
    )


def anchor_headings(body: str) -> str:
    """Give every heading the id GitHub would, so the markdown's own
    `[§4C](#c-standing-access--so-this-is-the-last-message)` links work on the
    published page too.

    ⚠️ Measured 2026-09-02: the fragments had NO heading ids at all, so every
    in-page link Justin's page has carried since 2026-08-20 (Option E, §4C) was
    dead on heygabi.ai while working fine in a markdown preview. The rule is
    GitHub's: lowercase, drop everything but letters/digits/spaces/hyphens,
    spaces to hyphens — which is what the links in the docs were written for.
    """
    seen: dict[str, int] = {}

    def _slug(text: str) -> str:
        plain = html_mod.unescape(re.sub(r"<[^>]+>", "", text)).lower()
        plain = re.sub(r"[^\w\- ]", "", plain).replace(" ", "-")
        n = seen.get(plain, 0)
        seen[plain] = n + 1
        return plain if n == 0 else f"{plain}-{n}"

    def _tag(m: re.Match) -> str:
        return f'<{m.group(1)} id="{_slug(m.group(2))}">{m.group(2)}</{m.group(1)}>'

    return re.sub(r"<(h[1-6])>(.*?)</\1>", _tag, body, flags=re.S)


def wrap_tables(body: str) -> str:
    """Every table gets a horizontal-scroll parent.

    These runbooks are read on phones. A 5-column status table with no scroll
    container is what forces the whole page to scroll sideways.
    """
    return re.sub(r"<table>", '<div class="tw"><table>', body).replace(
        "</table>", "</table></div>"
    )


def lift_lead_blockquote(body: str) -> str:
    """The markdown's opening `> Audience / Status / Last verified` block is the
    status banner, and it is the single most important thing on the page — it is
    what tells a reader whether to trust the rest. Promote it out of a generic
    blockquote into the callout the stylesheet already has."""
    m = re.search(r"<blockquote>\s*(.*?)\s*</blockquote>", body, re.S)
    if not m:
        return body
    inner = m.group(1)
    inner = re.sub(r"</?p>", "", inner).strip()
    return body[: m.start()] + f'<div class="status">{inner}</div>' + body[m.end():]


DEFAULT_KICKER = (
    "heygabi.ai estate &middot; server migration &middot; reference runbook "
    '&middot; step-by-step: <a href="/runbooks/shelf-justin/">Justin\'s steps</a>'
)


def build(md_path: Path, slug: str, style_src: Path | None,
          kicker_html: str | None = None) -> str:
    md_text = md_path.read_text(encoding="utf-8")
    md_text = expand_includes(md_text, md_path.parent)

    body = render_markdown(md_text)
    body = lift_lead_blockquote(body)
    body = anchor_headings(body)
    body = number_headings(body)
    body = wrap_tables(body)

    # The shim supplies no chrome of its own, so the fragment carries its own
    # breadcrumb. Parameterised because this generator now serves more than one
    # page and a hard-coded pointer to a sibling is how the FIRST fragments
    # started lying about where things live.
    kicker = f'<p class="kicker">{kicker_html or DEFAULT_KICKER}</p>\n'

    parts = [
        HEADER_COMMENT.format(slug=slug, source=md_path.as_posix()),
        extract_style(style_src),
        "\n<main>\n",
        kicker,
        body,
        "\n</main>\n",
    ]
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("markdown", type=Path, help="source markdown file")
    ap.add_argument("--slug", required=True, help="estate_docs slug, e.g. shelf-server")
    ap.add_argument("--out", type=Path, required=True, help="fragment path to write")
    ap.add_argument(
        "--style-from",
        type=Path,
        default=None,
        help="fragment to lift the <style> block from (default: --out, if it exists)",
    )
    ap.add_argument(
        "--kicker",
        default=None,
        help="HTML for the breadcrumb line above the title (default: the runbook's)",
    )
    args = ap.parse_args(argv)

    if not args.markdown.exists():
        print(f"ERROR: no such markdown file: {args.markdown}", file=sys.stderr)
        return 1

    style_src = args.style_from or args.out
    # Sample this BEFORE writing: --style-from defaults to --out, so once the
    # file is written it always "exists" and the report would claim a reuse
    # that never happened on a first run.
    reused = style_src.exists()

    out = build(args.markdown, args.slug, style_src, args.kicker)
    args.out.write_text(out, encoding="utf-8")

    print(f"wrote {args.out} ({len(out):,} bytes) from {args.markdown}")
    print(f"  style block: {'reused from ' + str(style_src) if reused else 'DEFAULT (no existing fragment)'}")
    print(f"  publish: npx wrangler kv key put --binding estate_docs \"doc:{args.slug}\" --path {args.out} --remote")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
