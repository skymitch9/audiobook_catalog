"""
Find ALREADY-PUBLISHED content warnings for club books and write them to
site/content_warnings.json for the club read page.

Verified-only policy: Claude searches the web (StoryGraph, trigger-warning
databases, reviews) and may only report warnings it actually found in a
source — every warning must carry the source URL, and anything without one
is discarded. It is explicitly instructed never to infer warnings from its
own knowledge of the plot.

Scope: books on club Current Reads AND club TBRs (warnings matter most
before you start a book). Books already checked are skipped unless --force;
"checked, none found" is recorded so we don't re-search every run.

Needs: the 'anthropic' package and the 'Claude-llm' (or ANTHROPIC_API_KEY)
env var.

Usage:
    python -m app.tools.fetch_content_warnings
    python -m app.tools.fetch_content_warnings --title "Exact Catalog Title"
    python -m app.tools.fetch_content_warnings --force
"""

import argparse
import json
import os
import re
import sys
import time

from app.config import SITE_DIR
from app.tools.club_books import club_book_titles
from app.tools.extract_chapters import save_json_with_retry, load_json

WARNINGS_PATH = SITE_DIR / "content_warnings.json"
CLAUDE_API_KEY = os.getenv("Claude-llm") or os.getenv("ANTHROPIC_API_KEY")


def filter_warnings(raw):
    """Keep only warnings with a label and an http(s) source URL."""
    out = []
    seen = set()
    for w in raw or []:
        label = (w.get("label") or "").strip()
        url = (w.get("source_url") or "").strip()
        if not label or not url.lower().startswith(("http://", "https://")):
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "source_url": url})
    return out[:20]


def extract_json(text):
    """Parse the model's reply as JSON, tolerating surrounding prose."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


def warnings_from_web(title, author):
    """Ask Claude with web search. Returns a list (possibly empty) or None on failure."""
    try:
        import anthropic
    except ImportError:
        print("  [LLM] 'anthropic' package not installed — skipping")
        return None
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    try:
        # Note: no structured-output format here — web search results carry
        # citations, which are incompatible with output_config.format.
        with client.messages.stream(
            model="claude-opus-4-8",
            max_tokens=16000,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
            messages=[{
                "role": "user",
                "content": (
                    f'Find published content warnings / trigger warnings for the book "{title}"'
                    f" by {author}. Check sources like TheStoryGraph's content-warning section, "
                    "trigger-warning databases, and detailed reviews.\n\n"
                    "STRICT RULES:\n"
                    "- Report ONLY warnings you actually found stated in a source you visited. "
                    "Do NOT add warnings from your own knowledge of the plot, however confident.\n"
                    "- Each warning needs the URL of the page where you found it.\n"
                    "- If you find no published warnings, say so.\n\n"
                    "Reply with ONLY this JSON (no other text after it):\n"
                    '{"found": true/false, "warnings": [{"label": "...", "source_url": "https://..."}]}'
                ),
            }],
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in response.content if b.type == "text")
        data = extract_json(text)
    except Exception as e:
        print(f"  [LLM] request failed: {e}")
        return None
    if data is None:
        print("  [LLM] unparseable reply")
        return None
    return filter_warnings(data.get("warnings"))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--title", help="check one exact catalog title only")
    parser.add_argument("--force", action="store_true", help="re-check books already recorded")
    args = parser.parse_args()

    if not CLAUDE_API_KEY:
        print("Claude-llm / ANTHROPIC_API_KEY not set — cannot fetch warnings.")
        return 1

    data = load_json(WARNINGS_PATH, {})
    if args.title:
        books = {(args.title, "")}
    else:
        books = club_book_titles(include_tbr=True)
        print(f"club books (reads + TBR): {len(books)}")

    found = none_found = skipped = 0
    for title, author in sorted(books):
        if title in data and not args.force:
            skipped += 1
            continue
        print(f"[check] {title}")
        warnings = warnings_from_web(title, author)
        if warnings is None:
            print("  -> lookup failed; will retry next run")
            continue
        data[title] = {"warnings": warnings, "checked_at": int(time.time())}
        if warnings:
            print(f"  -> {len(warnings)} published warnings found")
            found += 1
        else:
            print("  -> no published warnings found (recorded)")
            none_found += 1
        save_json_with_retry(data, WARNINGS_PATH)

    print(f"\nDone. with warnings: {found}, none published: {none_found}, already checked: {skipped}")
    print(f"Wrote {WARNINGS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
