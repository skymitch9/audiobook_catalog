"""
Stored-XSS guard for the community-stats block emitted by generate_stats.py.

generate_stats_html() emits an inline <script> whose loadCommunityStats() builds
a "Top Reviewers" list from Firestore review docs and assigns it to
innerHTML. The reviewer displayName is user-controlled; interpolating it raw is a
stored XSS. The shipped site/stats.html is GENERATED from this file, so the fix
(and this guard) belong at the generator — patching the generated file would be
reverted by the next stats rebuild.

Source-contract test: the escaped value (displayName) is Firestore-runtime data,
not present in the Python-side stats dict, so asserting on the emitted JS source
is as strong as rendering. The doubled braces are the Python f-string escaping.
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GEN = REPO_ROOT / "app" / "tools" / "generate_stats.py"


class GenerateStatsEscapingTest(unittest.TestCase):
    def setUp(self):
        self.source = GEN.read_text(encoding="utf-8")

    def test_escape_helper_is_emitted(self):
        self.assertIn(
            "const escapeHtml",
            self.source,
            "generate_stats.py should emit an escapeHtml() helper into the "
            "community-stats <script>.",
        )

    def test_top_reviewer_name_is_escaped(self):
        # The reviewer name must not be interpolated raw into innerHTML.
        self.assertNotRegex(
            self.source,
            r'top-name">\$\{\{name\}\}',
            "generate_stats.py interpolates the reviewer displayName unescaped "
            "into the Top Reviewers innerHTML (stored XSS). Wrap it with "
            "escapeHtml(name).",
        )
        self.assertIn(
            "${{escapeHtml(name)}}",
            self.source,
            "The Top Reviewers name should be emitted via escapeHtml(name).",
        )


if __name__ == "__main__":
    unittest.main()
