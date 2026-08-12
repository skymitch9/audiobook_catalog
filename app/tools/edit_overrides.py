# app/tools/edit_overrides.py
# EDIT THE LISTINGS. A local CLI over the catalog corrections layer.
#
#   python -m app.tools.edit_overrides find "thunderplump"
#   python -m app.tools.edit_overrides show "thunderplump"
#   python -m app.tools.edit_overrides edit "thunderplump"            # interactive
#   python -m app.tools.edit_overrides edit "thunderplump" \
#          --set series_index=11 --why series_index="trkn says 11" --yes
#   python -m app.tools.edit_overrides rm "thunderplump"
#   python -m app.tools.edit_overrides list [query]
#   python -m app.tools.edit_overrides canon "lions quest" "Lion's Quest"
#   python -m app.tools.edit_overrides unresolved --item X --question Y
#   python -m app.tools.edit_overrides validate
#
# Why a CLI and not a web page: the thing being edited is a file in the repo
# that only takes effect on the next build, on this machine, from the library
# on this machine. A page on the static site could not read the m4b tags, could
# not write the repo, and would need a backend the architecture deliberately
# does not have (docs/info/ARCHITECTURE.md). Same reasoning as the sibling
# universe editor.
#
# What it does that hand-editing the JSON does not:
#   * keys the entry on what the BUILD will match - the pre-correction tag
#     values and the CDEK asin - not on the published title, which may itself
#     already be a correction;
#   * fills evidence.tags_read from the real file and refuses a correction with
#     no stated reason, so the entry stays the kind of record the tests demand;
#   * proves the entry fires, by running the real layer over the proposed file
#     before writing anything;
#   * validates and writes atomically, so a bad edit is a refusal rather than a
#     corrections file the next build silently ignores in full.
#
# It never writes an m4b. Tags are opened read-only. Fixing tags at source is
# app/tools/audit_series_tags.py's job, and its uncurated repair path is
# DISARMED - see docs/info/catalog-corrections.md §8.2.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from app.core import book_lookup as bl
from app.core import overrides_store as store
from app.core.catalog_overrides import CORRECTABLE_FIELDS, OVERRIDES_PATH, canonicalize_series

BLANK = "-"  # what to type to force a field blank rather than leave it alone


# --------------------------------------------------------------------------- #
# Small output helpers
# --------------------------------------------------------------------------- #


def _out(text: str = "") -> None:
    # The library is full of curly quotes and accented names; a Windows console
    # in cp1252 would raise UnicodeEncodeError halfway through a listing.
    sys.stdout.write(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8") + "\n")


def _rule(char: str = "-", width: int = 78) -> None:
    _out(char * width)


def _fmt(value: Optional[str], width: int = 34) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ")
    if not text:
        return "(blank)".ljust(width)
    return (text if len(text) <= width else text[: width - 3] + "...").ljust(width)


def _ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(prompt).strip()
    except EOFError:
        return default
    return answer or default


def _confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    return _ask(f"{prompt} [y/N] ").lower() in ("y", "yes")


# --------------------------------------------------------------------------- #
# Resolving "which book"
# --------------------------------------------------------------------------- #


def _resolve(query: str, file: Optional[str], interactive: bool) -> Optional[bl.Book]:
    if file:
        path = Path(file).expanduser()
        if not path.exists():
            _out(f"No such file: {path}")
            return None
        return bl.load_book_from_file(path)

    rows = bl.load_catalog()
    if not rows:
        _out(f"No catalog at {bl.CATALOG_CSV}. Build it with `python -m app.main`, or pass --file <m4b>.")
        return None
    hits = bl.search(rows, query)
    if not hits:
        _out(f"Nothing in the catalog matches {query!r}. Try fewer words, or pass --file <m4b>.")
        return None
    if len(hits) == 1:
        return bl.load_book(hits[0])

    _out(f"{len(hits)} matches for {query!r}:")
    for i, row in enumerate(hits, 1):
        _out(f"  {i:>2}. {_fmt(row.get('title'), 46)} {_fmt(row.get('author'), 22)} {row.get('series') or ''} {row.get('series_index_display') or ''}")
    if not interactive:
        _out("Narrow the query, or pass --file <m4b>. Refusing to guess.")
        return None
    choice = _ask("Which one? (number, blank to cancel) ")
    if not choice.isdigit() or not (1 <= int(choice) <= len(hits)):
        return None
    return bl.load_book(hits[int(choice) - 1])


def _duplicate_titles(book: bl.Book) -> int:
    """How many catalog rows share this title+author. >1 means the key is ambiguous."""
    rows = bl.load_catalog()
    t = (book.title or "").strip().lower()
    a = (book.author or "").strip().lower()
    return sum(1 for r in rows if (r.get("title") or "").strip().lower() == t and (r.get("author") or "").strip().lower() == a)


# --------------------------------------------------------------------------- #
# Rendering a book
# --------------------------------------------------------------------------- #


def _show_book(book: bl.Book, data: Dict) -> List[int]:
    """Print the three views of a book. Returns the indices of entries that match it."""
    published = book.published()
    tagged = book.uncorrected or {}
    existing = store.entries_for(
        data,
        asin=book.asin,
        title=tagged.get("title") or book.title,
        author=tagged.get("author") or book.author,
        filename=book.filename,
    )
    current_set = (data["overrides"][existing[0]].get("set") if existing else {}) or {}

    _rule("=")
    _out(f"{book.title}  -  {book.author}")
    _out(f"file : {book.path if book.path else 'NOT FOUND under ROOT_DIR'}")
    _out(f"asin : {book.asin or '(none - this book will be keyed on title + author)'}")
    _rule("=")
    _out(f"{'field':<14}{'published (site)':<36}{'from the tags':<36}override")
    _rule()
    for f in CORRECTABLE_FIELDS:
        _out(f"{f:<14}{_fmt(published.get(f))}  {_fmt(tagged.get(f) if book.uncorrected else '?')}  {current_set.get(f, '')}")
    _rule()
    if book.uncorrected is None:
        _out("! The m4b was not found, so 'from the tags' is unknown and this entry would be")
        _out("  keyed on the PUBLISHED title - which is wrong if the title is itself corrected.")
    if len(existing) > 1:
        _out(f"! {len(existing)} existing entries match this book; only the first ever fires.")
    return existing


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_find(args) -> int:
    rows = bl.load_catalog()
    hits = bl.search(rows, args.query, limit=args.limit)
    data = store.load(args.overrides)
    if not hits:
        _out(f"Nothing matches {args.query!r}.")
        return 1
    for row in hits:
        marked = store.entries_for(data, title=row.get("title"), author=row.get("author"))
        flag = "*" if marked else " "
        _out(f"{flag} {_fmt(row.get('title'), 46)} {_fmt(row.get('author'), 22)} {_fmt(row.get('series'), 28)} {row.get('series_index_display') or ''}")
    _out()
    _out(f"{len(hits)} shown. '*' already has a correction (matched on the published title).")
    return 0


def cmd_show(args) -> int:
    data = store.load(args.overrides)
    book = _resolve(args.query, args.file, interactive=sys.stdin.isatty())
    if book is None:
        return 1
    existing = _show_book(book, data)
    if book.tags_read:
        _out()
        _out("tags on disk:")
        for atom, value in book.tags_read.items():
            _out(f"  {atom:<8} {value}")
    for i in existing:
        _out()
        _out(f"existing entry #{i}:")
        _out(json.dumps(data["overrides"][i], indent=2, ensure_ascii=False))
    return 0


def _parse_pairs(pairs: Sequence[str], what: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise SystemExit(f"--{what} expects field=value, got {raw!r}")
        field, value = raw.split("=", 1)
        out[field.strip()] = value
    return out


def _collect_interactive(book: bl.Book, current_set: Dict[str, str]) -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    published = book.published()
    _out()
    _out(f"New value per field. Enter = leave alone, {BLANK!r} = force blank (an unknown volume is")
    _out("recorded blank, never guessed). Every change then needs a one-line reason.")
    sets: Dict[str, Optional[str]] = {}
    why: Dict[str, str] = {}
    for f in CORRECTABLE_FIELDS:
        shown = current_set.get(f, published.get(f, ""))
        answer = _ask(f"  {f:<13} [{shown or 'blank'}] > ")
        if not answer:
            continue
        value = "" if answer == BLANK else answer
        if value == (current_set.get(f) if f in current_set else published.get(f, "")):
            _out(f"    (unchanged, skipping {f})")
            continue
        sets[f] = value
    if not sets:
        return {}, {}
    _out()
    for f in sets:
        while True:
            reason = _ask(f"  why {f} = {sets[f]!r}? > ")
            if reason:
                why[f] = reason
                break
            _out("    Required. An override with no stated reason is indistinguishable from a typo.")
    return sets, why


def _key_file_for(args, book: bl.Book) -> Optional[str]:
    """
    match.file, but only where it helps.

    With an ASIN there is nothing to disambiguate and a file key can only make
    the entry stop firing. Without one, two catalog rows sharing a title and an
    author genuinely need a tiebreaker.
    """
    if book.asin:
        if args.key_file:
            _out("! --key-file ignored: this book has an ASIN, and narrowing an ASIN key can only make it stop firing.")
        return None
    duplicates = _duplicate_titles(book)
    if args.key_file:
        return book.filename
    if duplicates > 1:
        _out(f"! {duplicates} catalog rows share this title and author - adding match.file as a tiebreaker.")
        return book.filename
    return None


def _build_entry(args, book: bl.Book, sets: Dict[str, Optional[str]], why: Dict[str, str]) -> Dict:
    """Key it (ASIN first - it survives a rename AND a retag) and attach the evidence."""
    tagged = book.uncorrected or {}
    title = tagged.get("title") or book.title
    author = tagged.get("author") or book.author
    return store.build_entry(
        match=store.build_match(asin=book.asin, title=title, author=author, file=_key_file_for(args, book)),
        sets=sets,
        why=why,
        tags_read=book.tags_read or {"_note": "not read - no m4b found for this row under ROOT_DIR"},
        filename_said=book.filename_said(),
        sources=args.source,
        note=args.note,
        book=f"{title} - {author}",
    )


def cmd_edit(args) -> int:
    interactive = not args.yes and sys.stdin.isatty()
    data = store.load(args.overrides)
    book = _resolve(args.query, args.file, interactive)
    if book is None:
        return 1
    existing = _show_book(book, data)
    current_set = (data["overrides"][existing[0]].get("set") if existing else {}) or {}

    if args.set:
        sets = {k: ("" if v == BLANK else v) for k, v in _parse_pairs(args.set, "set").items()}
        why = _parse_pairs(args.why, "why")
    elif interactive:
        sets, why = _collect_interactive(book, current_set)
    else:
        _out("Nothing to change: pass --set field=value (and --why field=reason), or run interactively.")
        return 1

    if not sets:
        _out("No changes.")
        return 0

    try:
        entry = _build_entry(args, book, sets, why)
    except store.OverridesError as exc:
        _out(f"Refused: {exc}")
        return 2
    match = entry["match"]

    proposed = json.loads(store.dumps(data))  # deep copy, same formatting rules
    action = store.upsert(proposed, entry, merge=not args.replace)
    idx = store.find_entry(proposed, match)
    final_entry = proposed["overrides"][idx]

    _out()
    _out(f"Entry to be {action}:")
    _out(json.dumps(final_entry, indent=2, ensure_ascii=False))

    ok, notes = _verify(proposed, book, final_entry["set"])
    _out()
    for note in notes:
        _out(note)
    if not ok:
        _out("Refusing to write: the entry does not actually change the catalog. Check the match block.")
        return 3

    problems = store.validate(proposed)
    for p in problems:
        if p.startswith("ERROR"):
            _out(p)
    if any(p.startswith("ERROR") for p in problems):
        return 2

    if args.dry_run:
        _out("--dry-run: nothing written.")
        return 0
    if not _confirm("Write this to scripts/catalog_overrides.json?", assume_yes=args.yes or not interactive):
        _out("Cancelled.")
        return 0

    store.save(proposed, args.overrides)
    _out(f"{action.capitalize()} in {args.overrides}.")
    _out("Rebuild to see it in the catalog:  python -m app.main   (a metadata-only fix has no upload, so nothing rebuilds on its own)")
    return 0


def _verify(proposed: Dict, book: bl.Book, sets: Dict[str, str]) -> Tuple[bool, List[str]]:
    """
    Run the real corrections layer over the proposed file and check the book
    actually comes out corrected. This is the check that catches an entry keyed
    on a value the build never sees.
    """
    row = dict(book.uncorrected) if book.uncorrected else {
        k: v for k, v in book.published().items()
    }
    result = store.simulate(proposed, row, path=book.path, asin=book.asin)
    notes: List[str] = []
    ok = True
    for field, wanted in sets.items():
        got = result.get(field) or ""
        if got == (wanted or ""):
            continue
        if field == "series" and got == (canonicalize_series(wanted) or ""):
            notes.append(f"note: series {wanted!r} folds to the canonical {got!r} (canonical_series).")
            continue
        notes.append(f"FAILED: {field} would still read {got!r}, not {wanted!r}.")
        ok = False
    if ok:
        notes.append("verified: the real corrections layer applies this entry to this book.")
    return ok, notes


def cmd_rm(args) -> int:
    data = store.load(args.overrides)
    book = _resolve(args.query, args.file, interactive=sys.stdin.isatty())
    if book is None:
        return 1
    existing = _show_book(book, data)
    if not existing:
        _out("No entry corrects this book.")
        return 1
    for i in reversed(existing):
        _out()
        _out(json.dumps(data["overrides"][i], indent=2, ensure_ascii=False))
        if _confirm("Delete this entry?", assume_yes=args.yes):
            store.remove(data, i)
    store.save(data, args.overrides)
    _out("Saved. Rebuild with `python -m app.main`.")
    return 0


def cmd_list(args) -> int:
    data = store.load(args.overrides)
    query = (args.query or "").lower()
    shown = 0
    for i, entry in enumerate(data["overrides"]):
        label = store.describe(entry)
        changes = ", ".join(f"{k}={v!r}" for k, v in entry["set"].items())
        if query and query not in f"{label} {changes}".lower():
            continue
        _out(f"{i:>3}. {_fmt(label, 44)} {changes}")
        shown += 1
    _out()
    _out(f"{shown} of {len(data['overrides'])} entries | {len(data.get('canonical_series') or {})} canonical_series folds")
    return 0


def cmd_canon(args) -> int:
    data = store.load(args.overrides)
    store.set_canonical_series(data, args.variant, args.canonical)
    store.save(data, args.overrides)
    _out(f"{args.variant.lower()!r} -> {args.canonical!r}. Applies to every book, matched or not.")
    return 0


def cmd_unresolved(args) -> int:
    data = store.load(args.overrides)
    store.add_unresolved(data, item=args.item, question=args.question, where=args.where or "", status=args.status or "")
    store.save(data, args.overrides)
    _out(f"Recorded as unresolved: {args.item}")
    return 0


def cmd_validate(args) -> int:
    try:
        data = store.load(args.overrides)
    except store.OverridesError as exc:
        _out(str(exc))
        return 2
    problems = store.validate(data)
    for p in problems:
        _out(p)
    errors = sum(1 for p in problems if p.startswith("ERROR"))
    _out()
    _out(f"{len(data['overrides'])} entries | {errors} errors | {len(problems) - errors} warnings")
    return 2 if errors else 0


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.tools.edit_overrides",
        description="Edit the catalog listings: add and amend entries in scripts/catalog_overrides.json.",
    )
    p.add_argument("--overrides", type=Path, default=OVERRIDES_PATH, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("find", help="search the published catalog")
    f.add_argument("query")
    f.add_argument("--limit", type=int, default=25)
    f.set_defaults(func=cmd_find)

    s = sub.add_parser("show", help="published values, tag values and any existing correction")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--file", help="an .m4b path, instead of searching the catalog")
    s.set_defaults(func=cmd_show)

    e = sub.add_parser("edit", help="add or amend a correction")
    e.add_argument("query", nargs="?", default="")
    e.add_argument("--file", help="an .m4b path, instead of searching the catalog")
    e.add_argument("--set", action="append", metavar="FIELD=VALUE", help=f"repeatable; VALUE {BLANK!r} forces blank")
    e.add_argument("--why", action="append", metavar="FIELD=REASON", help="repeatable; required for every --set")
    e.add_argument("--source", action="append", metavar="URL", help="repeatable; citable source for the correction")
    e.add_argument("--note", help="free-text note on the entry")
    e.add_argument("--key-file", action="store_true", help="add match.file as a tiebreaker (never the only key)")
    e.add_argument("--replace", action="store_true", help="replace an existing entry instead of merging into it")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("-y", "--yes", action="store_true", help="no prompts")
    e.set_defaults(func=cmd_edit)

    r = sub.add_parser("rm", help="delete the correction(s) for a book")
    r.add_argument("query", nargs="?", default="")
    r.add_argument("--file")
    r.add_argument("-y", "--yes", action="store_true")
    r.set_defaults(func=cmd_rm)

    ls = sub.add_parser("list", help="list existing corrections")
    ls.add_argument("query", nargs="?", default="")
    ls.set_defaults(func=cmd_list)

    c = sub.add_parser("canon", help="fold a variant series spelling onto the canonical one")
    c.add_argument("variant")
    c.add_argument("canonical")
    c.set_defaults(func=cmd_canon)

    u = sub.add_parser("unresolved", help="record something deliberately NOT corrected")
    u.add_argument("--item", required=True)
    u.add_argument("--question", required=True)
    u.add_argument("--where", default="")
    u.add_argument("--status", default="")
    u.set_defaults(func=cmd_unresolved)

    v = sub.add_parser("validate", help="check the corrections file against every rule")
    v.set_defaults(func=cmd_validate)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except store.OverridesError as exc:
        _out(f"Refused: {exc}")
        return 2
    except KeyboardInterrupt:
        _out("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
