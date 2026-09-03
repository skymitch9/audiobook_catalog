/**
 * shelf-link.js — THE ONE canonical `catalog row → Audiobookshelf` join.
 *
 * ⚠️ EVERY consumer of the shelf uses this module. `docs/TODO.md` ("Shelf link
 * on every book") says it in as many words: *"Do not duplicate the join. The
 * reader-port item needs the same mapping. One canonical implementation, used
 * by both, or they will drift and disagree about which book is which."*
 * Today that is the book modal on `index.html` and the card on `ebooks.html`;
 * next it is the EPUB/PDF reader port. If you are about to write
 * `shelf.heygabi.ai` into another file, import from here instead.
 *
 * ── THE THREE RECORDED CONSTRAINTS THIS FILE EXISTS TO HONOUR ──────────────
 *
 * 1. 🔴 **ABS item ids are NOT stable, so this module NEVER emits `/item/<id>`.**
 *    Measured 2026-08-21: every id from the 2026-08-20 flat layout returned 404
 *    after the hardlink reshape. A deep link therefore rots the next time the
 *    library is rebuilt, and a dead link is worse than an absent one.
 *
 *    ⚠️ The ids in the shipped map happened to be LIVE when re-measured
 *    2026-09-02 (0 of 1,077 stale — the map had been regenerated after the
 *    reshape). That is not a reprieve: it measures that the map was rebuilt
 *    recently, not that ids became stable. The rot is one `02-abs-hardlinks.sh`
 *    re-run away, and nothing in the pipeline regenerates the map.
 *
 *    So the link is an ABS **SEARCH**, which cannot 404. The map's value is no
 *    longer an id to put in a URL — it is the ABS-side TITLE to search for,
 *    which is both stable across a rebuild and a better query than the
 *    catalogue's own title. Measured 2026-09-02 over a random 60-item sample:
 *    the intended item came back FIRST for 57 and inside the top 10 for the
 *    other 3. **Nothing was not found.**
 *
 * 2. ⚠️ **Everyone who clicks meets Cloudflare Access.** Confirmed by a real
 *    person 2026-08-21 — the owner: *"shelf cloudflare needs the sign in too,
 *    it made Justin sign in."* So every label this module produces SAYS the
 *    sign-in is coming (`ACCESS_NOTE`). A person must never meet a bare
 *    refusal, and a button that silently bounces you to Google is exactly that.
 *
 * 3. ⚠️ **A row with no shelf counterpart gets NO BUTTON.** `shelfLinkFor()`
 *    returns `null` rather than a link into an empty search. Callers render
 *    nothing, or an explicit label — never a dead link. Measured 2026-09-02:
 *    1,220 ABS items, of which 1,086 are audio and 132 are ebook-only.
 */

import { bookIdFromTitle } from './reviews.js';

/** The shelf's public origin. One definition; do not inline it elsewhere. */
export const SHELF_ORIGIN = 'https://shelf.heygabi.ai';

/** Audiobookshelf is served under a path prefix on that origin. */
export const SHELF_APP = SHELF_ORIGIN + '/audiobookshelf';

/**
 * The `Audio` library. Measured live 2026-09-02: it is the ONLY library on the
 * box (`GET /api/libraries` → 1 result, `mediaType: book`, folder
 * `/audiobooks`).
 */
export const AUDIO_LIBRARY_ID = '69bed7b1-89b1-422d-92de-c27d7c9087db';

/**
 * ⚠️ The `Ebooks` library does NOT exist yet. Option A (owner's choice,
 * 2026-09-02) creates a second ABS library over an ebooks-only hardlink shadow
 * tree; the runbook is `docs/access/SHELF_EBOOKS_LIBRARY.md`.
 *
 * Until it is created this stays null, and every ebook link falls back to the
 * audio library's search — which still finds the 132 ebook-only items, because
 * they live in that same library today. When the owner creates it, the map
 * generator stamps the real id into `shelf_book_map.json` (`ebookLibraryId`)
 * and NO CODE CHANGE IS NEEDED here.
 */
export const EBOOK_LIBRARY_ID = null;

/**
 * ⚠️ Constraint 2, in one string. Every label and tooltip carries it.
 * Reused by tests, so change it here and nowhere else.
 */
export const ACCESS_NOTE = 'needs the family sign-in';

/**
 * Slugify a title the way the whole estate does.
 *
 * ⚠️ Re-exported, NOT reimplemented. Before this module there were four copies
 * of these five lines (`reviews.js`, an inline `_moduleBookIdFromTitle` in
 * index.html, an inline `_bookIdFromTitle` in ebooks.html, and
 * `book_id_from_title` in `scripts/build_shelf_map.py`). Three of them are now
 * gone; the Python one is the deliberate cross-language twin and
 * `site/__tests__/shelf-link.test.js` pins the two together.
 */
export { bookIdFromTitle };

/**
 * Build an Audiobookshelf search URL. This is the ONLY URL shape this module
 * produces, and the reason is constraint 1: a search cannot 404.
 *
 * @param {string} query       what to search for — normally the ABS-side title
 * @param {string} [libraryId] which library to search; defaults to Audio
 * @returns {string}
 */
export function shelfSearchUrl(query, libraryId) {
  const lib = libraryId || AUDIO_LIBRARY_ID;
  return SHELF_APP + '/library/' + encodeURIComponent(lib) +
    '/search?q=' + encodeURIComponent(query || '');
}

/**
 * Read whichever shape of `shelf_book_map.json` is deployed.
 *
 * ⚠️ Two shapes exist on purpose, and this must keep reading BOTH:
 *
 *   legacy (shipped through 2026-09-02):  { "<slug>": "<abs-uuid>" }
 *   current:  { generatedAt, libraryId, ebookLibraryId, books: {
 *                "<slug>": { "t": "<ABS title>", "m": "audio"|"ebook"|"both" } } }
 *
 * A browser holding a cached copy of the legacy file must not lose its shelf
 * buttons the moment this module ships — the two deploy independently. From a
 * legacy entry we know only that a counterpart EXISTS; the search then falls
 * back to the catalogue's own title, which is a slightly worse query and still
 * cannot 404. The uuid is deliberately DISCARDED rather than used.
 *
 * @param {any} raw parsed JSON, or null/undefined
 * @returns {{generatedAt: string|null, libraryId: string, ebookLibraryId: string|null,
 *            books: Object<string, {t: string|null, m: string}>}}
 */
export function normalizeShelfMap(raw) {
  const empty = {
    generatedAt: null,
    libraryId: AUDIO_LIBRARY_ID,
    ebookLibraryId: EBOOK_LIBRARY_ID,
    books: {},
  };
  if (!raw || typeof raw !== 'object') return empty;

  // --- current shape -------------------------------------------------------
  if (raw.books && typeof raw.books === 'object') {
    const books = {};
    for (const slug of Object.keys(raw.books)) {
      const v = raw.books[slug];
      if (!v) continue;
      if (typeof v === 'string') {
        books[slug] = { t: v, m: 'audio' };
      } else {
        books[slug] = {
          t: typeof v.t === 'string' && v.t ? v.t : null,
          m: v.m === 'ebook' || v.m === 'both' ? v.m : 'audio',
        };
      }
    }
    return {
      generatedAt: typeof raw.generatedAt === 'string' ? raw.generatedAt : null,
      libraryId: typeof raw.libraryId === 'string' && raw.libraryId
        ? raw.libraryId : AUDIO_LIBRARY_ID,
      ebookLibraryId: typeof raw.ebookLibraryId === 'string' && raw.ebookLibraryId
        ? raw.ebookLibraryId : EBOOK_LIBRARY_ID,
      books,
    };
  }

  // --- legacy flat shape ---------------------------------------------------
  // ⚠️ The uuid is thrown away on purpose (constraint 1). Presence is the only
  // fact a legacy entry is trusted for.
  const books = {};
  for (const slug of Object.keys(raw)) {
    if (typeof raw[slug] === 'string' && raw[slug]) {
      books[slug] = { t: null, m: 'audio' };
    }
  }
  return { ...empty, books };
}

/**
 * One button, three labels — the verb says what the shelf can DO with the
 * book. Owner call 2026-09-02: "Play", not "Listen", and a book held as both
 * audio and ebook says so rather than hiding one behind the other.
 */
export const SHELF_LABELS = Object.freeze({
  audio: '🎧 Play on the shelf',
  ebook: '📖 Read on the shelf',
  both: '🎧📖 Play or read on the shelf',
});

export function shelfLabelFor(media) {
  return SHELF_LABELS[media] || SHELF_LABELS.audio;
}

/**
 * The join itself: a catalogue title → the link that opens it on the shelf.
 *
 * @param {string} title  the catalogue's title for the book
 * @param {object} map    the value returned by {@link normalizeShelfMap}
 * @returns {null | {href: string, label: string, note: string, title: string,
 *                   media: string, query: string}}
 *   `null` when there is no counterpart on the shelf — constraint 3. Callers
 *   MUST treat null as "render nothing / render an explicit label", never as a
 *   reason to link into an empty search.
 */
export function shelfLinkFor(title, map) {
  const slug = bookIdFromTitle(title || '');
  if (!slug || !map || !map.books) return null;

  const entry = map.books[slug];
  if (!entry) return null;

  // The ABS-side title is the better query (constraint 1's measurement); the
  // catalogue title is the fallback a legacy map leaves us with.
  const query = entry.t || title;
  const isEbook = entry.m === 'ebook';
  const lib = isEbook && map.ebookLibraryId ? map.ebookLibraryId : map.libraryId;

  const label = shelfLabelFor(entry.m);

  return {
    href: shelfSearchUrl(query, lib),
    label,
    note: ACCESS_NOTE,
    // ⚠️ Constraint 2 — the sign-in is named in the hover text as well as the
    // caller's visible note, so it survives a caller that renders only a link.
    title: 'Opens Audiobookshelf in a new tab — ' + ACCESS_NOTE,
    media: entry.m,
    query,
  };
}
