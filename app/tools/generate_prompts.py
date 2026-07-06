"""
Generate discussion prompts for books that clubs are actively reading, and
write them to site/discussion_prompts.json for the club read page.

Cost control: only books on club Current Reads (queried live from Firestore)
are generated, not the whole catalog. Books already in the output file are
skipped unless --force. Prompts are anchored to chapter indexes (from
site/chapters.json) so the site can surface each question in the right
milestone section with an automatic spoiler tag.

Clubs can turn the feature off per-club ('Starter questions' toggle in
Edit Club); this tool just supplies the data.

Needs: the 'anthropic' package and the 'Claude-llm' (or ANTHROPIC_API_KEY)
env var. Run on any machine; no library access required.

Usage:
    python -m app.tools.generate_prompts             # active club reads
    python -m app.tools.generate_prompts --title "Exact Catalog Title"
    python -m app.tools.generate_prompts --force
"""

import argparse
import json
import os
import sys

from app.config import SITE_DIR
from app.tools.club_books import club_book_titles
from app.tools.extract_chapters import save_json_with_retry, load_json

PROMPTS_PATH = SITE_DIR / "discussion_prompts.json"
CHAPTERS_PATH = SITE_DIR / "chapters.json"
CLAUDE_API_KEY = os.getenv("Claude-llm") or os.getenv("ANTHROPIC_API_KEY")

PROMPTS_SCHEMA = {
    "type": "object",
    "properties": {
        "known": {
            "type": "boolean",
            "description": "True only if you actually know this specific book well enough to write grounded questions.",
        },
        "prompts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chapter_index": {
                        "type": "integer",
                        "description": "0-based index into the provided chapter list of the LATEST chapter the question references.",
                    },
                    "question": {"type": "string"},
                },
                "required": ["chapter_index", "question"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["known", "prompts"],
    "additionalProperties": False,
}


def validate_prompts(raw_prompts, chapter_count):
    """Keep only well-formed prompts with in-range chapter anchors."""
    out = []
    for p in raw_prompts or []:
        q = (p.get("question") or "").strip()
        idx = p.get("chapter_index")
        if q and isinstance(idx, int) and 0 <= idx < chapter_count:
            out.append({"chapter_index": idx, "question": q})
    return out[:15]


def prompts_from_llm(title, author, chapter_titles):
    try:
        import anthropic
    except ImportError:
        print("  [LLM] 'anthropic' package not installed — skipping")
        return None
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    chapter_list = "\n".join(f"{i}: {t}" for i, t in enumerate(chapter_titles))
    try:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=16000,
            output_config={"format": {"type": "json_schema", "schema": PROMPTS_SCHEMA}},
            messages=[{
                "role": "user",
                "content": (
                    f'Write 6-10 book-club discussion questions for "{title}" by {author}.\n\n'
                    "The audiobook's chapters, as `index: title`:\n"
                    f"{chapter_list}\n\n"
                    "Rules:\n"
                    "- Spread questions across the whole book (early, middle, late).\n"
                    "- Anchor each question with chapter_index = the LATEST chapter it "
                    "references or spoils, so readers who haven't reached that chapter "
                    "won't be shown it. Never anchor earlier than the events discussed.\n"
                    "- Questions should provoke discussion (motives, choices, themes, "
                    "predictions), not reading-comprehension quizzes.\n"
                    "- Only set known=true if you genuinely know this book. If you don't, "
                    "set known=false and prompts=[] — do not write generic questions."
                ),
            }],
        )
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text)
    except Exception as e:
        print(f"  [LLM] request failed: {e}")
        return None
    if not data.get("known"):
        return None
    return validate_prompts(data.get("prompts"), len(chapter_titles)) or None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--title", help="generate for one exact catalog title only")
    parser.add_argument("--force", action="store_true", help="regenerate existing entries")
    args = parser.parse_args()

    if not CLAUDE_API_KEY:
        print("Claude-llm / ANTHROPIC_API_KEY not set — cannot generate prompts.")
        return 1

    chapters_data = load_json(CHAPTERS_PATH, {})
    data = load_json(PROMPTS_PATH, {})

    if args.title:
        books = {(args.title, "")}
    else:
        books = club_book_titles(include_tbr=False)
        print(f"active club books: {len(books)}")

    done = skipped = failed = 0
    for title, author in sorted(books):
        if title in data and not args.force:
            skipped += 1
            continue
        entry = chapters_data.get(title)
        if not entry or not entry.get("chapters"):
            print(f"[skip] {title!r}: no chapter data (run extract_chapters first)")
            continue
        titles = [c["title"] for c in entry["chapters"]]
        print(f"[gen] {title}")
        prompts = prompts_from_llm(title, author, titles)
        if prompts:
            data[title] = {"prompts": prompts}
            print(f"  -> {len(prompts)} questions")
            done += 1
        else:
            print("  -> model doesn't know this book well enough; nothing written")
            failed += 1
        save_json_with_retry(data, PROMPTS_PATH)

    print(f"\nDone. generated: {done}, unknown-book: {failed}, already had: {skipped}")
    print(f"Wrote {PROMPTS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
