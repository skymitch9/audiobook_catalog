"""
Find ALREADY-PUBLISHED content warnings for books and write them to
site/content_warnings.json (shown on club read pages and the catalog modal).

Source chain per book (first source with results wins, recorded in "source"):
  1. Hardcover   — community content-warning tags via their GraphQL API
                   (free; needs HARDCOVER_TOKEN)
  2. DoesTheDogDie — community yes/no topic votes via their REST API (free;
                   needs DOESTHEDOGDIE_API_KEY from your profile page)
  3. Claude web  — searches StoryGraph, trigger-warning databases, reviews;
                   may only report warnings actually found in a source, every
                   warning carries its URL (needs 'Claude-llm' key + the
                   'anthropic' package). This is the paid backfill.

Scope:
  default      club books (current reads + TBRs)
  --title      one exact catalog title
  --all        every book in site/catalog.csv (the big backfill; pair with
               --no-llm first for a free Hardcover-only pass)
  new imports  scripts/sync_to_drive.py Step 5.6 calls check_new_books()
               with the titles that just arrived
  --requests   fulfill flagged books with the FULL chain including Claude:
               the site's "Request AI check" button (cw_requests docs, both
               lanes) and lines in cw_requests.txt at the repo root. Sync
               Step 5.6 also fulfills these automatically.

Books already checked are skipped unless --force; "checked, none found" is
recorded so we don't re-search every run. With --no-llm, empty results are
NOT recorded, so a later run with the LLM enabled can still backfill them.

Usage:
    python -m app.tools.fetch_content_warnings
    python -m app.tools.fetch_content_warnings --title "Exact Catalog Title"
    python -m app.tools.fetch_content_warnings --all --no-llm   # free pass
    python -m app.tools.fetch_content_warnings --all            # + Claude backfill
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from app.config import SITE_DIR
from app.tools.club_books import club_book_titles, fetch as fs_fetch, gv, API_KEY as FS_KEY
from app.tools.extract_chapters import save_json_with_retry, load_json

WARNINGS_PATH = SITE_DIR / "content_warnings.json"
CATALOG_PATH = SITE_DIR / "catalog.csv"
REQUESTS_FILE = SITE_DIR.parent / "cw_requests.txt"  # local flag list, one title per line
CLAUDE_API_KEY = os.getenv("Claude-llm")
HARDCOVER_TOKEN = os.getenv("HARDCOVER_TOKEN")
HARDCOVER_API = "https://api.hardcover.app/v1/graphql"
DTDD_API_KEY = os.getenv("DOESTHEDOGDIE_API_KEY")
DTDD_BASE = "https://www.doesthedogdie.com"


SEVERITY_RE = re.compile(r"^(graphic|moderate|minor)\s*:\s*", re.IGNORECASE)
SEVERITY_RANK = {"graphic": 3, "moderate": 2, "minor": 1}


def filter_warnings(raw):
    """Keep only warnings with a label and an http(s) source URL, deduped by
    topic. StoryGraph lists the same topic at several severities (Graphic:
    Death / Moderate: Death) — the highest severity wins."""
    by_topic = {}
    order = []
    for w in raw or []:
        label = (w.get("label") or "").strip()
        url = (w.get("source_url") or "").strip()
        if not label or not url.lower().startswith(("http://", "https://")):
            continue
        m = SEVERITY_RE.match(label)
        rank = SEVERITY_RANK[m.group(1).lower()] if m else 0
        topic = SEVERITY_RE.sub("", label).strip().lower()
        if topic not in by_topic:
            order.append(topic)
        if topic not in by_topic or rank > by_topic[topic][0]:
            by_topic[topic] = (rank, {"label": label, "source_url": url})
    return [by_topic[t][1] for t in order][:40]


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


# ---------------------------------------------------------------------------
# Source 1: Hardcover community tags (free)
# ---------------------------------------------------------------------------

def _hardcover_gql(query, variables=None):
    token = HARDCOVER_TOKEN
    if not token.startswith("Bearer "):
        token = f"Bearer {token}"
    req = urllib.request.Request(
        HARDCOVER_API,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": token,
            "User-Agent": "bookbuddy-catalog/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if resp.get("errors"):
        raise RuntimeError(resp["errors"][0].get("message", "GraphQL error"))
    return resp["data"]


def main_title(catalog_title):
    """Catalog titles are 'Main Title - Subtitle'; Hardcover wants the main."""
    return catalog_title.split(" - ")[0].strip()


def warnings_from_hardcover(title, author):
    """Community content-warning tags from Hardcover.

    Returns a warnings list ([] = matched but no tags, or no match) or None
    on request failure. NOTE: their API 403s on the _ilike operator — search
    goes through the typesense `search` query instead.
    """
    if not HARDCOVER_TOKEN:
        return None
    want = main_title(title)
    try:
        hits = _hardcover_gql(
            'query ($q: String!) { search(query: $q, query_type: "Book", per_page: 5) { results } }',
            {"q": f"{want} {author}".strip()},
        )["search"]["results"]["hits"]
        if not hits:
            return []
        # Best hit: exact main-title match beats popularity; popularity breaks
        # ties (keeps 'Summary of <book>' knockoffs from winning).
        docs = [h["document"] for h in hits]
        docs.sort(key=lambda d: (
            (d.get("title") or "").strip().lower() != want.lower(),
            -(d.get("users_count") or d.get("activities_count") or 0),
        ))
        book = _hardcover_gql(
            "query ($id: Int!) { books_by_pk(id: $id) { title slug cached_tags } }",
            {"id": int(docs[0]["id"])},
        )["books_by_pk"]
        if not book:
            return []
        tags = book.get("cached_tags") or {}
        if isinstance(tags, str):
            tags = json.loads(tags)
        cw = tags.get("Content Warning") or []
        url = f"https://hardcover.app/books/{book['slug']}"
        return filter_warnings([{"label": t.get("tag"), "source_url": url} for t in cw])
    except Exception as e:
        print(f"  [hardcover] lookup failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Source 2: DoesTheDogDie community votes (free)
# ---------------------------------------------------------------------------

def _dtdd_get(path):
    req = urllib.request.Request(
        f"{DTDD_BASE}{path}",
        headers={
            "Accept": "application/json",
            "X-API-KEY": DTDD_API_KEY,
            "User-Agent": "bookbuddy-catalog/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def warnings_from_dtdd(title, author):
    """Topics the DoesTheDogDie community voted YES on for this book.

    Returns a warnings list ([] = no match / no yes-votes) or None on
    request failure or missing DOESTHEDOGDIE_API_KEY.
    """
    if not DTDD_API_KEY:
        return None
    want = main_title(title)
    try:
        items = _dtdd_get("/dddsearch?q=" + urllib.parse.quote(want)).get("items") or []
        # Books only (DTDD is mostly movies/TV), best name match first
        books = [i for i in items
                 if "book" in ((i.get("itemType") or {}).get("name") or "").lower()]
        if not books:
            return []
        books.sort(key=lambda i: (i.get("name") or "").strip().lower() != want.lower())
        item = books[0]
        stats = _dtdd_get(f"/media/{item['id']}").get("topicItemStats") or []
        url = f"{DTDD_BASE}/media/{item['id']}"
        out = []
        for s in stats:
            yes, no = s.get("yesSum") or 0, s.get("noSum") or 0
            if yes > no and yes > 0:
                topic = s.get("topic") or {}
                label = topic.get("doesName") or topic.get("name")
                if label:
                    out.append({"label": label, "source_url": url})
        return filter_warnings(out)
    except Exception as e:
        print(f"  [dtdd] lookup failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Source 3: Claude + web search (paid backfill)
# ---------------------------------------------------------------------------

def warnings_from_web(title, author):
    """Ask Claude with web search. Returns a list (possibly empty) or None on failure."""
    if not CLAUDE_API_KEY:
        print("  [LLM] Claude-llm not set — skipping")
        return None
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


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------

def check_book(title, author, use_llm=True):
    """Run the source chain. Returns (warnings, source); warnings None means
    every source failed (or was skipped) — don't record, retry next run."""
    hc = warnings_from_hardcover(title, author)
    if hc:
        return hc, "hardcover"
    dtdd = warnings_from_dtdd(title, author)
    if dtdd:
        return dtdd, "dtdd"
    if not use_llm:
        # Free pass: only record real finds so the LLM can backfill later.
        return None, None
    web = warnings_from_web(title, author)
    if web is None:
        return None, None
    return web, ("web" if web else "none")


def check_new_books(books, use_llm=True):
    """Check (title, author) pairs not yet recorded — called by sync Step 5.6."""
    data = load_json(WARNINGS_PATH, {})
    found = 0
    for title, author in books:
        if title in data:
            continue
        print(f"[cw] {title}")
        warnings, source = check_book(title, author, use_llm=use_llm)
        if warnings is None:
            print("  -> lookup failed; will retry next run")
            continue
        data[title] = {"warnings": warnings, "source": source, "checked_at": int(time.time())}
        save_json_with_retry(data, WARNINGS_PATH)
        if warnings:
            found += 1
            print(f"  -> {len(warnings)} warnings via {source}")
        else:
            print("  -> none published (recorded)")
    return found


def _fs_delete(doc_name):
    req = urllib.request.Request(
        f"https://firestore.googleapis.com/v1/{doc_name}?key={FS_KEY}", method="DELETE")
    with urllib.request.urlopen(req, timeout=30):
        pass


def pending_requests():
    """Flagged books: cw_requests docs from both lanes (the site's 'Request
    AI check' button) + lines in cw_requests.txt. [(title, docName|None)]."""
    out = []
    for coll in ("cw_requests", "cw_requests_dev"):
        try:
            for d in fs_fetch(coll):
                title = gv(d["fields"], "bookTitle")
                if title:
                    out.append((title, d["name"]))
        except Exception as e:
            print(f"[WARN] listing {coll} failed: {e}")
    if REQUESTS_FILE.exists():
        for line in REQUESTS_FILE.read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if t and not t.startswith("#"):
                out.append((t, None))
    return out


def fulfill_requests(use_llm=True):
    """Run the full chain (incl. the paid Claude backfill) for every flagged
    book, then clear the fulfilled requests. Books that already have
    warnings are just cleared; 'checked, none' entries get a fresh look."""
    reqs = pending_requests()
    if not reqs:
        print("no pending warning requests")
        return 0
    data = load_json(WARNINGS_PATH, {})
    authors = dict(catalog_books())
    done = set()
    for title, doc_name in reqs:
        entry = data.get(title)
        if title in done or (entry and entry.get("warnings")):
            done.add(title)
            if doc_name:
                try:
                    _fs_delete(doc_name)
                except Exception as e:
                    print(f"  [WARN] request cleanup failed: {e}")
            continue
        print(f"[request] {title}")
        warnings, source = check_book(title, authors.get(title, ""), use_llm=use_llm)
        if warnings is None:
            print("  -> lookup failed; request kept for next run")
            continue
        data[title] = {"warnings": warnings, "source": source, "checked_at": int(time.time())}
        save_json_with_retry(data, WARNINGS_PATH)
        done.add(title)
        print(f"  -> {len(warnings)} warnings via {source}" if warnings
              else "  -> none published (recorded)")
        if doc_name:
            try:
                _fs_delete(doc_name)
            except Exception as e:
                print(f"  [WARN] request cleanup failed: {e}")
    if REQUESTS_FILE.exists():
        keep = [line for line in REQUESTS_FILE.read_text(encoding="utf-8").splitlines()
                if not line.strip() or line.strip().startswith("#")
                or line.strip() not in done]
        REQUESTS_FILE.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return len(done)


def catalog_books():
    """(title, author) for every catalog row."""
    books = set()
    with open(CATALOG_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            title = (row.get("title") or "").strip()
            if title:
                books.add((title, (row.get("author") or "").strip()))
    return books


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--title", help="check one exact catalog title only")
    parser.add_argument("--all", action="store_true",
                        help="every catalog book (backfill; consider --no-llm first)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Hardcover only — free; empty results not recorded")
    parser.add_argument("--force", action="store_true", help="re-check books already recorded")
    parser.add_argument("--dedup", action="store_true",
                        help="re-filter recorded entries in place (no lookups)")
    parser.add_argument("--requests", action="store_true",
                        help="fulfill flagged books (site button + cw_requests.txt), Claude included")
    args = parser.parse_args()

    if args.requests:
        n = fulfill_requests(use_llm=not args.no_llm)
        print(f"fulfilled {n} request(s); wrote {WARNINGS_PATH}")
        return 0

    if args.dedup:
        data = load_json(WARNINGS_PATH, {})
        changed = 0
        for entry in data.values():
            cleaned = filter_warnings(entry.get("warnings"))
            if cleaned != entry.get("warnings"):
                entry["warnings"] = cleaned
                changed += 1
        save_json_with_retry(data, WARNINGS_PATH)
        print(f"deduped {changed} of {len(data)} entries")
        return 0

    if args.title:
        books = {(args.title, "")}
    elif args.all:
        books = catalog_books()
        print(f"catalog books: {len(books)}" + (" (hardcover-only free pass)" if args.no_llm else ""))
    else:
        books = club_book_titles(include_tbr=True)
        print(f"club books (reads + TBR): {len(books)}")

    data = load_json(WARNINGS_PATH, {})
    found = none_found = skipped = failed = 0
    for title, author in sorted(books):
        if title in data and not args.force:
            skipped += 1
            continue
        print(f"[check] {title}")
        warnings, source = check_book(title, author, use_llm=not args.no_llm)
        if warnings is None:
            failed += 1
            if args.no_llm:
                print("  -> nothing on hardcover (not recorded; LLM can backfill)")
            else:
                print("  -> lookup failed; will retry next run")
            continue
        data[title] = {"warnings": warnings, "source": source, "checked_at": int(time.time())}
        if warnings:
            print(f"  -> {len(warnings)} published warnings via {source}")
            found += 1
        else:
            print("  -> no published warnings found (recorded)")
            none_found += 1
        save_json_with_retry(data, WARNINGS_PATH)
        if args.all:
            time.sleep(1)  # Hardcover rate limit is 60 req/min; we make 2/book

    print(f"\nDone. with warnings: {found}, none published: {none_found}, "
          f"already checked: {skipped}, not recorded/failed: {failed}")
    print(f"Wrote {WARNINGS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
