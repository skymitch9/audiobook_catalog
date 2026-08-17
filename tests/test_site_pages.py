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
  - own identity (owner, 2026-08-17): the ebooks page is ITS OWN THING —
    its parent is the estate front door (heygabi.ai), not the audiobook
    catalog. It stays out of the audiobook nav family in both directions,
    carries its own title/favicon, and wears its own paper-and-ink theme
    (light and dark both defined) instead of the estate theme stylesheets.
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
    def test_fetches_the_GATED_manifest_with_a_bearer(self):
        """⚠️ SUPERSEDES test_fetches_manifest_same_origin_relative (2026-08-17).

        The page used to fetch the RELATIVE 'ebooks.json' so each lane read
        its own manifest. That file left the deployment AND left git with the
        permission gate (owner directive: "ebooks should be like the other
        site where we grant permission to view it. I don't want people
        scraping my books"), so a relative fetch would now 404 on both lanes.

        What replaced it, and what this pins: the absolute gated endpoint,
        called with a bearer. A same-origin relative fetch reappearing here
        would mean someone put the manifest back in the deployment.
        """
        html = _template("ebooks.html")
        assert "https://audiobook-api.heygabi.ai/api/ebooks/manifest" in html
        assert "Authorization" in html and "Bearer" in html, (
            "the manifest is bearer-gated — a fetch without one is a 401"
        )
        assert "fetch('ebooks.json'" not in html, (
            "the relative manifest fetch is gone; it would 404 and, if it did "
            "not, it would mean the shelf is public again"
        )

    def test_the_gate_has_a_distinct_sentence_for_each_cause(self):
        # §1e: not signed in ≠ awaiting approval ≠ no grant ≠ an outage.
        # One message for four causes sends people to ask for access they
        # already hold. The Worker writes the specific sentences; the page
        # must carry the signed-out one and must tell 401 from 403 from the
        # rest rather than collapsing them.
        html = _template("ebooks.html")
        assert "res.status === 401" in html
        assert "res.status === 403" in html
        assert "outage, not a permission decision" in html
        assert "Sign in with Google" in html

    def test_no_book_data_and_no_direct_file_links_in_the_page_source(self):
        # The page is a SHIM: every book arrives at runtime, behind the gate.
        # A view-source of the signed-out page must be a shelf with no books.
        html = _template("ebooks.html")
        for ext in (".epub'", '.epub"', ".pdf'", '.pdf"'):
            assert ext not in html, "no direct file links — and no filenames at all"

    def test_display_only_no_download_affordance(self):
        # Still display-only. `can_download` is READ from the gated answer so
        # a future reader knows what to draw, but this page draws nothing:
        # there must be no anchor, no `download` attribute, no button.
        html = _template("ebooks.html")
        assert "download=" not in html.lower(), "no download attribute"
        assert "<a download" not in html.lower()
        assert "downloadbtn" not in html.lower().replace(" ", "")

    def test_filename_sourced_rows_are_marked_provisional(self):
        html = _template("ebooks.html")
        assert "b.source === 'filename'" in html
        assert "unverified metadata" in html

    def test_page_is_outside_the_audiobook_nav_family(self):
        # Own identity: the page's parent is the estate front door, so it
        # must NOT link into the audiobook site's page family. The single
        # allowed cross-reference is the quiet absolute "also in the pool"
        # link to the audiobooks hostname.
        html = _template("ebooks.html")
        for target in ("index.html", "community.html", "clubs.html",
                       "stats.html", "guess-game.html"):
            assert f'href="{target}"' not in html, (
                f"ebooks.html must not link the audiobook nav family ({target})"
            )
        assert 'href="https://heygabi.ai"' in html, (
            "the masthead/footer must point at the estate front door"
        )
        assert 'href="https://audiobooks.heygabi.ai"' in html, (
            "the one quiet cross-link to the audiobook pool"
        )

    def test_own_title_and_favicon(self):
        html = _template("ebooks.html")
        title = html.split("<title>")[1].split("</title>")[0]
        assert "Audiobook Catalog" not in title, (
            "the page's <title> must stand alone, no audiobook-site suffix"
        )
        assert "Ebooks" in title
        assert 'href="data:image/svg+xml,' in html, (
            "own inline-SVG favicon, not the audiobook site's favicon.png"
        )
        assert "favicon.png" not in html

    def test_own_theme_not_the_estate_stylesheets(self):
        # The look is self-contained: no estate-theme.css / ab-bridge.css.
        # theme.js alone is kept so the shared account modal's Appearance
        # controls stay live (the page honours data-mode, ignores data-theme).
        # Match the <link> hrefs, not bare names — the template's own comments
        # are allowed to TALK about the stylesheets it refuses to load.
        html = _template("ebooks.html")
        assert 'href="static/css/estate-theme.css"' not in html
        assert 'href="static/css/ab-bridge.css"' not in html
        assert '<link rel="stylesheet"' not in html, (
            "the whole look is inline — no external stylesheets at all"
        )
        assert 'src="static/js/theme.js"' in html
        # Dark mode must be complete both ways: the estate mode stamp and the
        # scriptless OS-preference fallback.
        assert 'html[data-mode="dark"]' in html
        assert "prefers-color-scheme: dark" in html

    def test_index_template_does_not_link_the_ebooks_page(self):
        # The 📖 Ebooks nav button was removed on the owner's word
        # (2026-08-17): the ebooks page is not part of the audiobook nav.
        assert 'href="ebooks.html"' not in _template("index.html")


class TestBookshelfContracts:
    """The cover-first redesign (owner, 2026-08-17: "it can't be a raw list").

    Grid of covers -> reading card -> chips, wearing the page's own
    paper-and-ink identity. String-pins in the file's idiom: each assert names
    a behaviour the page must keep, not an implementation detail for its own
    sake.
    """

    def test_shelf_is_a_cover_grid_not_a_list(self):
        html = _template("ebooks.html")
        assert 'class="eb-shelf"' in html
        assert ".eb-shelf{" in html and "display:grid" in html
        assert 'class="eb-tile"' in html.replace("'eb-tile'", "")  # rendered tiles

    def test_cover_images_lazy_load(self):
        html = _template("ebooks.html")
        assert 'loading="lazy"' in html
        assert 'decoding="async"' in html

    def test_placeholder_spine_is_the_designed_default_not_an_error(self):
        # The spine is ALWAYS rendered under the (optional) image, so zero
        # resolved covers — or a failed image load — still looks like a
        # deliberate bookcase. cover_url is optional per row, never required.
        html = _template("ebooks.html")
        assert 'class="eb-spine"' in html
        assert "eb-spine-title" in html and "eb-spine-author" in html
        assert "b.cover_url" in html  # image only when the manifest has one
        assert "addEventListener('error'" in html  # broken art -> spine, not glyph
        assert "classList.remove('has-img')" in html

    def test_cover_url_is_escaped_into_the_img_src(self):
        assert "esc(b.cover_url)" in _template("ebooks.html")

    def test_format_badge_rides_the_cover(self):
        html = _template("ebooks.html")
        assert 'class="eb-fmt"' in html
        assert ".eb-fmt{" in html and "position:absolute" in html

    def test_reading_card_is_a_dialog_in_the_pages_idiom(self):
        html = _template("ebooks.html")
        assert 'role="dialog"' in html and 'aria-modal="true"' in html
        assert 'aria-labelledby="eb-card-title"' in html
        assert "'Escape'" in html  # Esc closes
        assert 'aria-label="Close"' in html

    def test_reading_card_keeps_the_provisional_pill(self):
        # The honesty contract follows the row into the card.
        html = _template("ebooks.html")
        assert "b.source === 'filename'" in html
        assert "unverified metadata" in html

    def test_also_on_audio_links_the_audiobook_hash_search(self):
        # beside_audiobook rows link the audiobook catalog's own #q= search —
        # the catalog's only book anchor (same contract as index_push's
        # detail_url_for), URLSearchParams-encoded.
        html = _template("ebooks.html")
        assert "Also on audio" in html
        assert "https://audiobooks.heygabi.ai/#" in html
        assert "URLSearchParams" in html
        assert "b.beside_audiobook" in html  # gated, never unconditional

    def test_spine_cloth_tones_exist_in_both_schemes(self):
        # Placeholders must look right in light AND dark — no colour may live
        # in only one scheme (the file's own rule).
        html = _template("ebooks.html")
        for var in ("--cloth-1", "--cloth-2", "--cloth-3", "--cloth-4", "--cloth-ink"):
            assert html.count(var + ":") >= 3, (
                f"{var} must be defined on :root, the data-mode stamp, and the "
                "prefers-color-scheme fallback"
            )

    def test_sort_offers_title_author_newest_size(self):
        html = _template("ebooks.html")
        for value in ("title", "author", "modified", "size"):
            assert f'<option value="{value}">' in html

    def test_search_and_format_chips_survive_the_redesign(self):
        html = _template("ebooks.html")
        assert 'id="eb-search"' in html
        assert "format-chip" in html
        assert "activeFormats" in html


class TestShowPdfsCheckbox:
    """PDFs are hidden by default behind a checkbox (owner, 2026-08-17).

    The owner's decision instead of a cover hunt for them: they are game
    handbooks and household documents, and they are the one format with no
    embedded art to extract. The contract that matters is that HIDDEN MEANS
    HIDDEN — the grid, this page's search, and the format chips must all agree,
    because a "168 ebooks" count that includes rows you cannot reach is the
    kind of quiet lie that costs an afternoon.
    """

    def test_the_checkbox_exists_and_is_unticked_in_the_markup(self):
        html = _template("ebooks.html")
        assert 'id="eb-show-pdfs"' in html
        assert "Show PDFs" in html
        # DEFAULT OFF: no `checked` attribute anywhere on the input.
        checkbox = html.split('id="eb-show-pdfs"')[0].rsplit("<input", 1)[1] + html.split(
            'id="eb-show-pdfs"'
        )[1].split(">")[0]
        assert "checked" not in checkbox, "the PDF checkbox must ship unticked"

    def test_the_preference_persists_in_localstorage(self):
        html = _template("ebooks.html")
        assert "eb:showPdfs" in html
        assert "localStorage.getItem" in html and "localStorage.setItem" in html

    def test_storage_failure_cannot_take_the_shelf_down(self):
        # Private-mode Safari and locked-down profiles throw on localStorage.
        html = _template("ebooks.html")
        prefs = html.split("function readPdfPref")[1].split("function isHiddenFormat")[0]
        assert "try {" in prefs and "catch" in prefs

    def test_hidden_means_hidden_in_the_search_and_the_chips_too(self):
        html = _template("ebooks.html")
        assert "function eligibleBooks" in html
        # The search filter and the chip census both read the eligible pool,
        # never allBooks — that is what makes "hidden" mean hidden.
        search_fn = html.split("function visibleBooks")[1].split("function render(")[0]
        assert "eligibleBooks()" in search_fn and "allBooks.filter" not in search_fn
        chips_fn = html.split("function renderChips")[1].split("function renderPdfToggle")[0]
        assert "eligibleBooks()" in chips_fn

    def test_the_count_describes_the_pool_the_reader_can_actually_reach(self):
        render_fn = _template("ebooks.html").split("function render(")[1].split("function renderChips")[0]
        assert "eligibleBooks()" in render_fn
        assert "allBooks.length" not in render_fn


class TestDeepLinkAnchors:
    """Per-book anchors, so estate search lands on the book (2026-08-17).

    ⚠️ ONE implementation of the anchor id, in
    scripts/build_ebook_manifest.ebook_anchor(); the page READS the manifest's
    `anchor` field and app/index_push.py builds detail_url from the same value.
    A recomputation here would break every deep link SILENTLY — the page would
    simply not scroll, with no error anywhere — which is exactly why this is
    pinned by a test rather than left to a comment.
    """

    def test_tiles_carry_the_manifests_anchor_as_their_element_id(self):
        html = _template("ebooks.html")
        assert "b.anchor" in html
        assert "esc(b.anchor)" in html, "the id must be escaped like every other field"

    def test_the_page_never_recomputes_the_anchor(self):
        html = _template("ebooks.html")
        for hint in ("sha256", "createHash", "crypto.subtle"):
            assert hint not in html, (
                f"'{hint}' suggests the page is deriving the anchor itself — it must "
                "read the manifest's value (build_ebook_manifest.ebook_anchor is the "
                "one implementation)"
            )

    def test_a_hash_on_load_scrolls_to_the_book_and_opens_its_card(self):
        html = _template("ebooks.html")
        assert "function goToAnchor" in html
        assert "scrollIntoView" in html
        assert "goToAnchor();" in html.split("renderChips();")[-1], (
            "deep links must resolve AFTER the manifest loads — the tiles do not "
            "exist before that, which is why the browser's own fragment scroll cannot do it"
        )
        assert "addEventListener('hashchange'" in html

    def test_a_deep_link_to_a_pdf_is_not_a_dead_link(self):
        # Arriving at a hidden PDF reveals it rather than silently doing
        # nothing...
        goto = _template("ebooks.html").split("function goToAnchor")[1].split("function openCard")[0]
        assert "isHiddenFormat(b)" in goto
        assert "showPdfs = true" in goto

    def test_a_deep_link_does_not_rewrite_the_stored_preference(self):
        # ...but FOR THIS VISIT ONLY. The owner asked for default-off, and
        # following one search result is not the same as ticking the box;
        # silently flipping a stored default is a surprise nobody asked for.
        goto = _template("ebooks.html").split("function goToAnchor")[1].split("function openCard")[0]
        assert "writePdfPref" not in goto, (
            "a deep link must reveal PDFs without persisting the preference"
        )

    def test_opening_a_card_makes_the_url_copyable_without_re_scrolling(self):
        html = _template("ebooks.html")
        assert "history.replaceState" in html
        assert "location.hash = " not in html, (
            "assigning location.hash re-scrolls the page mid-dialog; replaceState does not"
        )
