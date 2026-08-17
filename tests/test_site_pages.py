"""
The verbatim template pages (guess-game.html, ebooks.html) and their wiring.

These pages are copied AS-IS from app/web/templates/ into site/ by
app/writers.py on every build — no substitution, unlike index.html. The tests
pin the copy step and the contracts the ebooks page (ebook-split design,
phase 1) must keep:

  - display-only: the page renders site/ebooks.json client-side and offers no
    downloads; a manifest refresh must update it with NO html rebuild, which
    is only true while the fetch is the relative same-origin 'ebooks.json'
    (on the dev lane that resolves to /dev/ebooks.json, on prod /ebooks.json)
  - honesty: the manifest's `source` field distinguishes metadata read out of
    the file ('opf') from metadata guessed off the filename ('filename');
    the page must keep the provisional rows visibly provisional
"""

from pathlib import Path

from app.web.html_builder import TEMPLATE_DIR
from app.writers import STATIC_TEMPLATE_PAGES, _copy_template_pages_to_site


def _template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


class TestTemplatePageCopy:
    def test_both_pages_are_registered(self):
        # Order is unimportant; membership is the contract. Dropping either
        # name silently un-ships that page on the next full build.
        assert "guess-game.html" in STATIC_TEMPLATE_PAGES
        assert "ebooks.html" in STATIC_TEMPLATE_PAGES

    def test_registered_templates_exist(self):
        for name in STATIC_TEMPLATE_PAGES:
            assert (TEMPLATE_DIR / name).is_file(), f"missing template: {name}"

    def test_copy_is_verbatim(self, tmp_path):
        _copy_template_pages_to_site(tmp_path)
        for name in STATIC_TEMPLATE_PAGES:
            copied = (tmp_path / name).read_bytes()
            assert copied == (TEMPLATE_DIR / name).read_bytes(), (
                f"{name} must be copied byte-for-byte — these pages take no "
                "substitution; edit the template, never site/"
            )


class TestEbooksPageContracts:
    def test_fetches_manifest_same_origin_relative(self):
        html = _template("ebooks.html")
        assert "fetch('ebooks.json'" in html, (
            "the page must fetch the RELATIVE 'ebooks.json' so the /dev/ and "
            "prod lanes each read their own manifest"
        )
        assert "/ebooks.json'" not in html.replace("'ebooks.json'", ""), (
            "no absolute-path fetch of the manifest — that would cross lanes"
        )

    def test_display_only_no_download_links(self):
        # Phase 1 is display-only: file access tiers belong to the auth
        # migration's file-permissions phase. The page must not link at the
        # ebook files themselves.
        html = _template("ebooks.html")
        assert "download" not in html.lower().replace("downloads", ""), (
            "no download affordance in phase 1"
        )
        for ext in (".epub'", '.epub"', ".pdf'", '.pdf"'):
            assert ext not in html, "no direct file links in phase 1"

    def test_filename_sourced_rows_are_marked_provisional(self):
        html = _template("ebooks.html")
        assert "b.source === 'filename'" in html
        assert "unverified metadata" in html

    def test_nav_links_the_family_of_pages(self):
        html = _template("ebooks.html")
        for target in ("index.html", "community.html", "clubs.html",
                       "stats.html", "guess-game.html"):
            assert f'href="{target}"' in html

    def test_index_template_links_the_ebooks_page(self):
        assert 'href="ebooks.html"' in _template("index.html")
