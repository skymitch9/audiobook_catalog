/**
 * library-link.js — THE ONE canonical `catalog row → library_catalog` join.
 *
 * ⚠️ Same rule as `shelf-link.js` beside it, for the same reason: if you are
 * about to write `library.heygabi.ai` into another file, import from here
 * instead. This join used to be twelve inline lines in `index.html`'s
 * non-module script; the ebook site (`ebooks.html`) already carries a SECOND,
 * different sibling-catalogue link, and two implementations of "which library
 * record is this book" will eventually disagree about which book is which.
 *
 * ── THE FOUR RECORDED CONSTRAINTS THIS FILE EXISTS TO HONOUR ────────────────
 *
 * 1. 🔴 **A join is never guessed at render time.** Every link this module
 *    emits comes from one of exactly two sources, both decided long before the
 *    browser runs:
 *      - `library_work_id` / `library_formats` on the catalog row, stamped at
 *        BUILD time by `app/library_link.py` from the library's own machine
 *        mapping route; or
 *      - a hand-reviewed row in `site/cross-catalog-overrides.json`.
 *    There is no title matching, no folding and no similarity test in this
 *    file. `app/library_link.py`'s header explains what those cost when they
 *    are wrong (the Space Knight wrong link, 2026-08-14), and this module is
 *    deliberately downstream of every one of those decisions.
 *
 * 2. ⚠️ **A row with no library counterpart gets NO SECTION.** `libraryLinksFor`
 *    returns an EMPTY ARRAY, never a link into a search or a `/work/` with a
 *    guessed id. Measured 2026-09-02: 128 of 1,088 `catalog.csv` rows carry a
 *    `library_work_id`, so 960 books render nothing here, which is the honest
 *    answer — most of the audiobooks are not in the physical library.
 *
 * 3. ⚠️ **Everyone who clicks meets a sign-in.** Measured live 2026-09-02:
 *    `GET https://library.heygabi.ai/work/229` answers **200** (the Worker
 *    serves the SPA shell for any non-`/api` path) but `GET /api/works/229`
 *    answers **401**, so a stranger who follows one of these links gets the
 *    page furniture and no book. That is a bare refusal in all but name, so
 *    every label this module produces SAYS the sign-in is coming
 *    (`ACCESS_NOTE`) — the same rule, and the same wording shape, as
 *    `shelf-link.js` constraint 2.
 *
 * 4. ⚠️ **Curated overrides exist because one id cannot express two books.**
 *    *The Wandering Inn* Book 1 is ONE audiobook and TWO printed works (229
 *    *The Wandering Inn* and 230 *No Killing Goblins*); Book 2 is 231 + 232.
 *    Both catalogues' automatic matchers refuse that shape and are RIGHT to —
 *    `library_work_id` is a single column and picking one of the two halves
 *    would be a coin flip. So the pairs are written down by hand, reviewed,
 *    and read from here. See `site/cross-catalog-overrides.json`'s `_README`
 *    for what is checked and where.
 */

/** The library's public origin. One definition; do not inline it elsewhere. */
export const LIBRARY_ORIGIN = 'https://library.heygabi.ai';

/**
 * ⚠️ Constraint 3, in one string. Every label and tooltip carries it.
 * Reused by tests, so change it here and nowhere else.
 */
export const ACCESS_NOTE = 'needs the family sign-in';

/**
 * The library's per-book page. ⚠️ `/work/<id>` is SINGULAR — `/works/<id>` is
 * the API and answers "Not a page" in the browser (`apps/web/src/router.tsx`
 * `workPath`, read 2026-09-02). The plural form appears in that repo's own
 * `docs/TODO.md`, which is why this is stated here rather than remembered.
 *
 * @param {string|number} id a library_catalog work id
 * @returns {string}
 */
export function libraryWorkUrl(id) {
  return LIBRARY_ORIGIN + '/work/' + encodeURIComponent(String(id));
}

/**
 * Read `site/cross-catalog-overrides.json` into a lookup keyed by the VERBATIM
 * `catalog.csv` title.
 *
 * ⚠️ Verbatim, not folded. A curated row is a claim about ONE catalogue row;
 * folding the key here would quietly let it claim a family of them, which is
 * the failure the whole overrides mechanism exists to avoid rather than
 * reproduce. `app/library_link.py`'s folding is for DISCOVERING a join; this
 * is for HONOURING one already decided.
 *
 * A malformed or missing file yields an empty lookup, and an empty lookup
 * renders no curated links — never a broken one. Individual rows that fail
 * validation are dropped rather than taking the whole file with them: one bad
 * hand-edit should cost one link, not all of them.
 *
 * @param {any} raw parsed JSON, or null/undefined
 * @returns {Map<string, Array<{libraryWorkId: string, libraryTitle: string,
 *                              libraryLabel: string|null, format: string}>>}
 */
export function normalizeOverrides(raw) {
  const out = new Map();
  if (!raw || typeof raw !== 'object') return out;
  const rows = Array.isArray(raw.overrides) ? raw.overrides : [];

  for (const row of rows) {
    if (!row || typeof row !== 'object') continue;
    const title = typeof row.audiobookTitle === 'string' ? row.audiobookTitle.trim() : '';
    // ⚠️ A work id must be a positive integer. `0`, a negative, a float and a
    // non-numeric string all become NO LINK rather than `/work/NaN`, which is
    // a dead link with a plausible shape.
    const id = Number(row.libraryWorkId);
    const format = typeof row.format === 'string' ? row.format.trim() : '';
    if (!title || !format) continue;
    if (!Number.isInteger(id) || id <= 0) continue;

    const entry = {
      libraryWorkId: String(id),
      libraryTitle:
        typeof row.libraryTitle === 'string' && row.libraryTitle.trim()
          ? row.libraryTitle.trim()
          : 'View record',
      libraryLabel:
        typeof row.libraryLabel === 'string' && row.libraryLabel.trim()
          ? row.libraryLabel.trim()
          : null,
      format,
    };

    const list = out.get(title);
    if (list) list.push(entry);
    else out.set(title, [entry]);
  }
  return out;
}

/**
 * The join itself: one catalogue row → the library links to show for it.
 *
 * @param {{title?: string, libraryWorkId?: string, libraryFormats?: string}} row
 *   the modal payload — `title` is the verbatim `catalog.csv` title,
 *   `libraryWorkId`/`libraryFormats` are the build-time stamp.
 * @param {Map} [overrides] the value returned by {@link normalizeOverrides}
 * @returns {Array<{key: string, formatLabel: string, href: string,
 *                  linkText: string, title: string, curated: boolean}>}
 *   EMPTY when this book has no library counterpart — constraint 2. Callers
 *   MUST treat `[]` as "render no section", never as a reason to link
 *   anywhere.
 */
export function libraryLinksFor(row, overrides) {
  const title = String((row && row.title) || '').trim();

  // ── curated first, and curated REPLACES rather than joins ────────────────
  // ⚠️ A reviewed row is the more considered claim about the same question, so
  // letting an auto stamp render beside it would show one book twice and let
  // the two disagree in public. Measured 2026-09-02: no `catalog.csv` row is
  // both curated and auto-stamped, so this branch is a rule about the future,
  // not a description of today's data.
  const curated = title && overrides ? overrides.get(title) : null;
  if (curated && curated.length) {
    return curated.map((e) => ({
      key: 'curated:' + e.libraryWorkId,
      formatLabel: e.format,
      href: libraryWorkUrl(e.libraryWorkId),
      linkText: e.libraryLabel ? e.libraryTitle + ' (' + e.libraryLabel + ')' : e.libraryTitle,
      title: 'Opens the physical library in a new tab — ' + ACCESS_NOTE,
      curated: true,
    }));
  }

  // ── the automatic stamp ──────────────────────────────────────────────────
  const workId = String((row && row.libraryWorkId) || '').trim();
  const formats = String((row && row.libraryFormats) || '')
    .split('|')
    .map((s) => s.trim())
    .filter(Boolean);
  // ⚠️ BOTH halves are required. A work id with no format word would render an
  // entry that does not say what form the media is in, which is the one thing
  // the owner's cross-catalog spec (2026-08-14) says every entry must do; a
  // format with no id has nowhere to point.
  if (!workId || formats.length === 0) return [];

  const href = libraryWorkUrl(workId);
  return formats.map((fmt) => ({
    key: 'auto:' + workId + ':' + fmt,
    formatLabel: fmt,
    href,
    linkText: 'View record',
    title: 'Opens the physical library in a new tab — ' + ACCESS_NOTE,
    curated: false,
  }));
}
