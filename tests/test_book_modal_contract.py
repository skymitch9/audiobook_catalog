"""
Drift guard for the book-modal data contract.

The contract has an emit half and a read half:
  - emit: app/web/html_builder.py::_book_data_attrs()  (Python, data-* attrs)
  - read: app/web/templates/index.html::bookPayloadFromEl()  (JS, dataset ->
    openModal payload)

Both are documented as "add a field HERE and there, nowhere else." This test
enforces it mechanically: it parses the attribute names out of each function's
source and asserts the sets match (after accounting for the one documented
back-compat alias, duration_hhmm, which the JS reader also checks because
older cards used that attribute name).

The vitest suite (site/__tests__) only covers site/*.js modules — it does not
load or exercise the inline JS in templates/index.html, so it cannot catch
this class of drift. This pytest test is what makes drift fail CI instead
(.github/workflows/tests.yml runs `python -m pytest`; vitest is not wired
into any workflow).
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HTML_BUILDER = REPO_ROOT / "app" / "web" / "html_builder.py"
INDEX_TEMPLATE = REPO_ROOT / "app" / "web" / "templates" / "index.html"

# The one documented JS-side alias that isn't a distinct contract field —
# see the comment above `duration:d.duration||d.duration_hhmm||""` in
# bookPayloadFromEl().
KNOWN_JS_ALIASES = {"duration_hhmm"}


def _kebab_to_camel(name: str) -> str:
    """Mirror the browser's HTMLElement.dataset conversion: data-foo-bar ->
    el.dataset.fooBar."""
    parts = name.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _extract_function_body(source: str, func_pattern: str) -> str:
    """Return the source text of the first function matching func_pattern,
    from its opening brace to the matching closing brace."""
    m = re.search(func_pattern, source)
    if not m:
        raise AssertionError(f"Could not find function matching {func_pattern!r}")
    start = source.index("{", m.end() - 1) if source[m.end() - 1] == "{" else source.index("{", m.end())
    depth = 0
    i = start
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError("Unbalanced braces while scanning function body")


class TestBookModalContractDrift(unittest.TestCase):
    def test_python_emit_and_js_read_use_the_same_field_set(self):
        py_source = HTML_BUILDER.read_text(encoding="utf-8")
        # Isolate _book_data_attrs()'s body so we only see the contract's
        # own data-* emissions, not attributes belonging to other markup
        # (e.g. _card_html's card-only data-series_index_sort).
        py_func_match = re.search(
            r"def _book_data_attrs\([^)]*\)[^:]*:\n(.*?)\n\ndef ", py_source, re.S
        )
        self.assertIsNotNone(py_func_match, "_book_data_attrs() not found in html_builder.py")
        py_body = py_func_match.group(1)

        emitted_attrs = set(re.findall(r"data-([a-zA-Z-]+)=", py_body))
        self.assertTrue(emitted_attrs, "No data-* attributes found in _book_data_attrs()")
        emitted_fields = {_kebab_to_camel(a) for a in emitted_attrs}

        html_source = INDEX_TEMPLATE.read_text(encoding="utf-8")
        js_body = _extract_function_body(html_source, r"function bookPayloadFromEl\(el\)")

        read_fields = set(re.findall(r"\bd\.([a-zA-Z_]+)\b", js_body))
        self.assertTrue(read_fields, "No d.<field> reads found in bookPayloadFromEl()")
        read_fields -= KNOWN_JS_ALIASES

        self.assertEqual(
            emitted_fields,
            read_fields,
            "Book-modal contract drift: _book_data_attrs() in html_builder.py "
            "and bookPayloadFromEl() in templates/index.html no longer emit/"
            "read the same field set. Add new modal fields in BOTH places.\n"
            f"Emitted only by Python: {sorted(emitted_fields - read_fields)}\n"
            f"Read only by JS: {sorted(read_fields - emitted_fields)}",
        )

    def test_every_openmodal_call_site_uses_the_shared_reader_or_payload(self):
        """Every openModal(...) *call* (not its own function definition)
        must pass bookPayloadFromEl(...) or a bare payload-shaped variable
        (the Book of the Day path: getAllBooks() -> bookPayloadFromEl) —
        never a hand-rolled object literal restating the field list."""
        html_source = INDEX_TEMPLATE.read_text(encoding="utf-8")
        call_sites = [
            m for m in re.finditer(r"openModal\(", html_source)
            if not html_source[max(0, m.start() - 9):m.start()].endswith("function ")
        ]
        self.assertGreaterEqual(len(call_sites), 5, "Expected at least 5 openModal() call sites")

        bad_sites = []
        for m in call_sites:
            arg_start = m.end()
            if html_source[arg_start] == "{":
                bad_sites.append(html_source[m.start():arg_start + 20])

        self.assertEqual(
            bad_sites,
            [],
            "Found openModal(...) called with a hand-built object literal "
            "instead of bookPayloadFromEl(el) / a payload-shaped variable — "
            "this reintroduces the per-call-site duplication the contract "
            f"removed: {bad_sites}",
        )


if __name__ == "__main__":
    unittest.main()
