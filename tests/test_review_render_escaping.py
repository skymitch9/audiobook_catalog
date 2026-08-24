"""
Stored-XSS guard for the inline review renderer.

app/web/templates/index.html::renderReviewSection() builds review HTML as a
template string assigned to reviewsContainer.innerHTML. The review text and
displayName are world-readable, user-submitted fields; if they are interpolated
raw, a review can inject markup/script (stored XSS). The canonical renderer
site/reviews.js already escapes both fields — this test enforces that the inline
divergent copy does too.

The vitest suite (site/__tests__) does not load the inline JS in
templates/index.html, so this pytest test is what makes an escaping regression
fail CI (.github/workflows/tests.yml runs `python -m pytest`).
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_TEMPLATE = REPO_ROOT / "app" / "web" / "templates" / "index.html"


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


class ReviewRenderEscapingTest(unittest.TestCase):
    def setUp(self):
        source = INDEX_TEMPLATE.read_text(encoding="utf-8")
        self.body = _extract_function_body(
            source, r"function\s+renderReviewSection\s*\("
        )

    def test_review_text_is_escaped(self):
        # The raw interpolation `${r.text}` must not appear; it must be wrapped.
        self.assertNotRegex(
            self.body,
            r"\$\{\s*r\.text\s*\}",
            "renderReviewSection interpolates r.text unescaped into innerHTML "
            "(stored XSS). Wrap it in the escHtml() helper.",
        )
        self.assertIn(
            "escHtml(r.text)",
            self.body,
            "renderReviewSection should render review text via escHtml(r.text).",
        )

    def test_review_display_name_is_escaped(self):
        # No bare `${r.displayName}` interpolation into innerHTML.
        self.assertNotRegex(
            self.body,
            r"\$\{\s*r\.displayName\s*\}",
            "renderReviewSection interpolates r.displayName unescaped into "
            "innerHTML (stored XSS). Wrap it in the escHtml() helper.",
        )
        self.assertIn(
            "escHtml(r.displayName)",
            self.body,
            "renderReviewSection should render the reviewer name via "
            "escHtml(r.displayName).",
        )

    def test_escape_helper_is_defined(self):
        self.assertIn(
            "const escHtml",
            self.body,
            "renderReviewSection should define an escHtml() helper that escapes "
            "&, <, >, \", and '.",
        )


if __name__ == "__main__":
    unittest.main()
