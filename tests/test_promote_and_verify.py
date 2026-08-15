"""
Unit tests for scripts/promote_and_verify.py — the pure parts only.

The dispatch/watch/fetch orchestration in that script shells out to `gh`,
`git`, and the live network; per repo policy this test module does NOT run
a real promote. It exercises the pure helpers: cache-token extraction and
comparison, the club-touched heuristic, promoted-range summarizing, commit
log parsing, prod-tag selection, CI-green evaluation, and deploy-run
matching.
"""

import unittest

from scripts.promote_and_verify import (
    ci_green_at_sha,
    club_files_changed,
    compare_cache_tokens,
    extract_cache_tokens,
    find_deploy_run,
    latest_prod_tag,
    parse_commit_log,
    summarize_promoted_range,
)


class ExtractCacheTokensTestCase(unittest.TestCase):
    def test_finds_versioned_js_imports(self):
        html = (
            "import { getClub } from './clubs.js?v=20260814j';\n"
            "import { buildMeetingIcs } from './ics.js?v=20260814g';\n"
        )
        self.assertEqual(
            extract_cache_tokens(html),
            {"clubs.js": "20260814j", "ics.js": "20260814g"},
        )

    def test_ignores_unversioned_script_tags(self):
        html = '<script src="app.js"></script><script src="static/js/theme.js"></script>'
        self.assertEqual(extract_cache_tokens(html), {})

    def test_no_matches_on_empty_page(self):
        self.assertEqual(extract_cache_tokens(""), {})

    def test_strips_leading_path_to_basename(self):
        html = "import x from '../shared/clubs.js?v=20260101a';"
        self.assertEqual(extract_cache_tokens(html), {"clubs.js": "20260101a"})

    def test_last_occurrence_wins_on_duplicate_filenames(self):
        html = "a.js?v=20260101a ... a.js?v=20260101b"
        self.assertEqual(extract_cache_tokens(html), {"a.js": "20260101b"})


class CompareCacheTokensTestCase(unittest.TestCase):
    def test_matching_tokens_pass(self):
        ok, problems = compare_cache_tokens({"clubs.js": "20260814j"}, {"clubs.js": "20260814j"})
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_empty_local_is_a_trivial_pass(self):
        # Pages with no versioned modules (e.g. the main catalog page) have
        # nothing to compare — that's success, not a skip.
        ok, problems = compare_cache_tokens({}, {"unrelated.js": "20260101a"})
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_mismatched_token_fails(self):
        ok, problems = compare_cache_tokens({"clubs.js": "20260814j"}, {"clubs.js": "20260813a"})
        self.assertFalse(ok)
        self.assertIn("clubs.js", problems[0])
        self.assertIn("20260814j", problems[0])
        self.assertIn("20260813a", problems[0])

    def test_missing_on_live_page_fails(self):
        ok, problems = compare_cache_tokens({"clubs.js": "20260814j"}, {})
        self.assertFalse(ok)
        self.assertIn("not referenced on live page", problems[0])

    def test_extra_remote_tokens_do_not_fail(self):
        # The live page is allowed to reference more than main's tree does
        # (e.g. a third-party import) — we only check what main claims.
        ok, problems = compare_cache_tokens(
            {"clubs.js": "20260814j"},
            {"clubs.js": "20260814j", "firebase-app.js": "10.8.0"},
        )
        self.assertTrue(ok)
        self.assertEqual(problems, [])


class ClubFilesChangedTestCase(unittest.TestCase):
    def test_true_for_club_html(self):
        self.assertTrue(club_files_changed(["site/club.html", "site/index.html"]))

    def test_true_for_clubs_js(self):
        self.assertTrue(club_files_changed(["site/clubs.js"]))

    def test_false_when_nothing_club_related(self):
        self.assertFalse(club_files_changed(["site/index.html", "app/main.py"]))

    def test_empty_list_is_false(self):
        self.assertFalse(club_files_changed([]))

    def test_case_insensitive(self):
        self.assertTrue(club_files_changed(["site/CLUB-read.html"]))


class ParseCommitLogTestCase(unittest.TestCase):
    def test_parses_sha_and_subject(self):
        log = "abc123\x1fFirst commit\ndef456\x1fSecond commit\n"
        commits = parse_commit_log(log)
        self.assertEqual(commits, [
            {"sha": "abc123", "subject": "First commit"},
            {"sha": "def456", "subject": "Second commit"},
        ])

    def test_empty_log_gives_empty_list(self):
        self.assertEqual(parse_commit_log(""), [])

    def test_skips_blank_lines(self):
        log = "abc123\x1fFirst\n\ndef456\x1fSecond\n"
        commits = parse_commit_log(log)
        self.assertEqual(len(commits), 2)

    def test_subject_missing_is_empty_string(self):
        commits = parse_commit_log("abc123\n")
        self.assertEqual(commits, [{"sha": "abc123", "subject": ""}])


class LatestProdTagTestCase(unittest.TestCase):
    def test_picks_most_recent_by_name_sort(self):
        tags = ["prod-20260801-120000", "prod-20260810-090000", "prod-20260805-000000"]
        self.assertEqual(latest_prod_tag(tags), "prod-20260810-090000")

    def test_none_when_no_prod_tags(self):
        self.assertIsNone(latest_prod_tag([]))
        self.assertIsNone(latest_prod_tag(["v1.0", "some-other-tag"]))

    def test_ignores_blank_lines(self):
        tags = ["", "prod-20260801-120000", "  "]
        self.assertEqual(latest_prod_tag(tags), "prod-20260801-120000")


class SummarizePromotedRangeTestCase(unittest.TestCase):
    def test_includes_range_and_counts(self):
        commits = [{"sha": "a" * 40, "subject": "fix: thing"}]
        files = ["site/index.html"]
        summary = summarize_promoted_range("prod-20260810-090000", "b" * 40, commits, files)
        self.assertIn("prod-20260810-090000", summary)
        self.assertIn("Commits ahead of last promote: 1", summary)
        self.assertIn("fix: thing", summary)
        self.assertIn("site/index.html", summary)

    def test_first_promotion_has_no_prior_tag(self):
        summary = summarize_promoted_range(None, "b" * 40, [], [])
        self.assertIn("first promotion", summary)

    def test_truncates_long_lists(self):
        commits = [{"sha": str(i) * 7, "subject": f"commit {i}"} for i in range(20)]
        files = [f"site/file{i}.html" for i in range(20)]
        summary = summarize_promoted_range("prod-1", "sha", commits, files)
        self.assertIn("and 10 more", summary)


class CiGreenAtShaTestCase(unittest.TestCase):
    def make_run(self, workflow, sha, status="completed", conclusion="success", url="u"):
        return {"workflowName": workflow, "headSha": sha, "status": status, "conclusion": conclusion, "url": url}

    def test_all_green_passes(self):
        runs = [self.make_run("Tests", "abc"), self.make_run("Lint", "abc")]
        ok, msg = ci_green_at_sha(runs, "abc")
        self.assertTrue(ok)
        self.assertIn("all green", msg)

    def test_missing_workflow_fails(self):
        runs = [self.make_run("Tests", "abc")]
        ok, msg = ci_green_at_sha(runs, "abc")
        self.assertFalse(ok)
        self.assertIn("Lint", msg)

    def test_wrong_sha_counts_as_missing(self):
        runs = [self.make_run("Tests", "old"), self.make_run("Lint", "old")]
        ok, msg = ci_green_at_sha(runs, "abc")
        self.assertFalse(ok)

    def test_failed_conclusion_fails(self):
        runs = [self.make_run("Tests", "abc", conclusion="failure"), self.make_run("Lint", "abc")]
        ok, msg = ci_green_at_sha(runs, "abc")
        self.assertFalse(ok)
        self.assertIn("failure", msg)

    def test_still_in_progress_fails(self):
        runs = [
            self.make_run("Tests", "abc", status="in_progress", conclusion=None),
            self.make_run("Lint", "abc"),
        ]
        ok, msg = ci_green_at_sha(runs, "abc")
        self.assertFalse(ok)
        self.assertIn("in_progress", msg)


class FindDeployRunTestCase(unittest.TestCase):
    def test_finds_workflow_dispatch_after_bound(self):
        runs = [
            {"databaseId": 1, "event": "push", "createdAt": "2026-08-14T10:00:05Z"},
            {"databaseId": 2, "event": "workflow_dispatch", "createdAt": "2026-08-14T10:00:10Z"},
        ]
        found = find_deploy_run(runs, "2026-08-14T10:00:00Z")
        self.assertEqual(found["databaseId"], 2)

    def test_ignores_dispatch_before_bound(self):
        runs = [{"databaseId": 1, "event": "workflow_dispatch", "createdAt": "2026-08-14T09:00:00Z"}]
        found = find_deploy_run(runs, "2026-08-14T10:00:00Z")
        self.assertIsNone(found)

    def test_picks_earliest_of_multiple_candidates(self):
        runs = [
            {"databaseId": 1, "event": "workflow_dispatch", "createdAt": "2026-08-14T10:00:20Z"},
            {"databaseId": 2, "event": "workflow_dispatch", "createdAt": "2026-08-14T10:00:05Z"},
        ]
        found = find_deploy_run(runs, "2026-08-14T10:00:00Z")
        self.assertEqual(found["databaseId"], 2)

    def test_no_candidates_returns_none(self):
        self.assertIsNone(find_deploy_run([], "2026-08-14T10:00:00Z"))


if __name__ == "__main__":
    unittest.main()
