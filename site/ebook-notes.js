// ebook-notes.js — the gated ebook shelf's "Content notes" block
// ES module, browser-native (no build step)
//
// The ebooks half of the owner's 2026-08-17 ask, *"port content warning
// feature over to all physical book and the ebook site"*. The library half
// shipped that day (`library_catalog/docs/info/content-warnings.md`); this is
// the same feature on `ebooks.heygabi.ai`.
//
// ⚠️ NOTHING HERE IS A SECOND IMPLEMENTATION. The store, the document id, the
// 80-character bound, the authorUid stamp and the author-or-moderator delete
// all belong to `user-warnings.js`, which this module CALLS. `bookIdFromTitle`
// belongs to `reviews.js`. This file is the two things the shelf genuinely has
// to answer for itself:
//
//   1. WHICH TITLE this book's notes are keyed by (`warningTitleFor`), and
//   2. what the block looks like in the shelf's paper-and-ink idiom.
//
// ============================ THE IDENTITY QUESTION ========================
//
// ⚠️ The estate keys every content note by `bookIdFromTitle(title)` where
// *title* is **the audiobook catalog's own spelling of the book**. An ebook's
// title comes from its epub metadata and is a DIFFERENT spelling of the same
// book. Key on it and the note is filed where nobody reads AND finds none of
// the notes written elsewhere — both silently, both looking exactly like
// "nobody has added a warning yet". There is no error to notice, which is why
// this is the first thing in the file rather than a detail in a function.
//
// `library_catalog/docs/info/content-warnings.md` §2 measured the divergence
// on the shared books: 33 of 92 spelled differently, 27 of them producing a
// different key. The library solves it from its cached `audiobook_holding.title`.
//
// THIS REPO **IS** THE AUDIOBOOK CATALOG, so the answer is nearer: the ebook
// manifest's sibling join (`scripts/build_ebook_manifest.sibling_catalog_match`
// — the same conservative join that resolves the sibling's cover) already
// knows which audiobook a file sits beside, and now publishes that audiobook's
// raw catalog title as `audiobook_title`. Three keying classes result:
//
//   'audiobook'  — a sibling matched; keyed by the AUDIOBOOK catalog's title,
//                  which is byte-for-byte what the book modal on
//                  audiobooks.heygabi.ai keys on. Notes cross both ways.
//   'beside'     — the file sits in an audiobook folder but the join refused
//                  (ambiguous, or a different volume). Keyed by its own title,
//                  and the block SAYS SO — a refused join is not a match.
//   'ebook-only' — no audiobook sibling at all. Keyed by its own title, which
//                  IS this catalog's spelling for such a file. Not a fallback
//                  so much as the correct answer.
//
// ⚠️ Never invent a normaliser here. `bookIdFromTitle` is imported, exactly as
// user-warnings.js imports it, and the raw title is what the manifest travels.

import { bookIdFromTitle } from './reviews.js';
import {
  addUserWarning,
  getUserWarnings,
  deleteUserWarning,
  MAX_WARNING_LABEL,
} from './user-warnings.js';

/**
 * The published pipeline warnings file, in the order it is tried.
 *
 * ⚠️ TWO URLs because this page is served from TWO origins — its own
 * `ebooks.heygabi.ai` (through the ebooks-door Worker) and
 * `audiobooks.heygabi.ai` (the Pages deploy, including the `/dev/` lane). The
 * relative form is right on the audiobook host and on any lane prefix; the
 * absolute one is the public copy, measured 2026-08-17 by the library build to
 * answer `Access-Control-Allow-Origin: *`, and covers the ebooks host whether
 * or not its door serves the audiobook site's JSON files.
 *
 * ⚠️ Relative FIRST, deliberately: it keeps `/dev/` reading `/dev/`'s own file
 * rather than prod's, so a lane's published notes are the lane's own.
 */
export const PUBLISHED_WARNINGS_URLS = [
  'content_warnings.json',
  'https://audiobooks.heygabi.ai/content_warnings.json',
];

/** ~200 KB, so it is fetched at most once per page and only when a card is
 *  opened — never on shelf load. The PROMISE is cached, not the result, so
 *  two cards opened quickly share one request. */
let _publishedPromise = null;

/** Drop the cached fetch. Tests only; a page never needs it. */
export function resetPublishedWarnings() {
  _publishedPromise = null;
}

/**
 * The published warnings map, lower-cased by title, or null.
 *
 * ⚠️ A failure answers `null`, never throws and never rejects. Losing the
 * published extra must not take the reader notes down with it — they are the
 * half a person can actually act on.
 *
 * @param {Function} [fetchImpl] injectable for tests; defaults to window.fetch
 * @returns {Promise<Object|null>}
 */
export function fetchPublishedWarnings(fetchImpl) {
  if (_publishedPromise) return _publishedPromise;
  const f = fetchImpl || (typeof fetch === 'function' ? fetch.bind(globalThis) : null);
  if (!f) return Promise.resolve(null);
  _publishedPromise = (async () => {
    for (const url of PUBLISHED_WARNINGS_URLS) {
      try {
        const res = await f(url);
        if (!res || !res.ok) continue;
        // A 200 that is actually the SPA shell (a door serving index.html for
        // an unknown path) parses as a JSON error, not as an empty map — so
        // this must be a `continue`, never a silent "checked, none published".
        const data = await res.json();
        if (data && typeof data === 'object') {
          const map = {};
          Object.keys(data).forEach((k) => { map[k.toLowerCase()] = data[k]; });
          return map;
        }
      } catch (e) { /* try the next URL */ }
    }
    return null;
  })();
  return _publishedPromise;
}

/**
 * Which title this ebook's content notes are keyed by, and why.
 *
 * ⚠️ The whole point of the module — see the header. Returns the raw title
 * (the caller slugs it, or hands it straight to user-warnings.js, which slugs
 * it with the one `bookIdFromTitle`).
 *
 * @param {{title?: string, audiobook_title?: string, beside_audiobook?: string}} book
 *   one manifest row
 * @returns {{title: string, bookId: string, keying: string, ownTitle: string,
 *            audiobookTitle: string|null}}
 */
export function warningTitleFor(book) {
  const row = book || {};
  const ownTitle = String(row.title == null ? '' : row.title).trim();
  const audiobookTitle =
    typeof row.audiobook_title === 'string' && row.audiobook_title.trim()
      ? row.audiobook_title.trim()
      : null;

  const title = audiobookTitle || ownTitle;
  return {
    title,
    bookId: title ? bookIdFromTitle(title) : '',
    keying: audiobookTitle
      ? 'audiobook'
      : (row.beside_audiobook ? 'beside' : 'ebook-only'),
    ownTitle,
    audiobookTitle,
  };
}

/**
 * What the published sources say about a title.
 *
 * ⚠️ THREE STATES, not two. "Published sources have been checked and listed
 * none" and "nobody has looked" are different facts and the block says which —
 * an empty `warnings` array is NOT a missing entry. (The same distinction the
 * book modal on the main site draws, and the library's `publishedWarningsFor`.)
 *
 * @returns {{state: 'unknown'|'clean'|'listed', warnings: Array}}
 */
export function publishedWarningsFor(map, title) {
  const entry = map && title ? map[String(title).toLowerCase()] : null;
  if (!entry) return { state: 'unknown', warnings: [] };
  const warnings = Array.isArray(entry.warnings) ? entry.warnings : [];
  return { state: warnings.length ? 'listed' : 'clean', warnings };
}

// --------------------------------------------------------------------------
// The block itself
// --------------------------------------------------------------------------
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/**
 * Whether the ✕ should be drawn on somebody's note, and what it says.
 *
 * ⚠️ Kept as a named function so what is OFFERED and what firestore.rules
 * ALLOWS are read off the same sentence: your own note always, anyone's note
 * for a site moderator+ (`canDeleteUserWarning` in firestore.rules, the
 * 2026-08-17 delete split). A name match is not authorship — that is the
 * rules' job via authorUid — but it is the right affordance question, and
 * `deleteUserWarning` refuses in words if the two disagree.
 */
export function noteDeleteAffordance(warning, session, canModerate) {
  const mine = !!session
    && String(warning.displayName || '').toLowerCase()
       === String(session.displayName || '').toLowerCase();
  if (mine) return { show: true, asModerator: false, title: 'Remove your note' };
  if (canModerate) return { show: true, asModerator: true, title: 'Remove this note (site moderator)' };
  return { show: false, asModerator: false, title: '' };
}

/**
 * Draw (and re-draw) the Content notes block for one book.
 *
 * ⚠️ IDEMPOTENT: it empties `slotEl` first, so re-rendering after an add or a
 * delete can never double-post, however many times it runs.
 *
 * The shim in ebooks.html has already required a signed-in, ebook-granted
 * reader before a single tile renders, so there is no signed-out state to
 * degrade to here — the one honest exception is a session the localStorage
 * mirror cannot name, which is said in words rather than shown as a dead
 * button.
 *
 * @param {object} db Firestore
 * @param {HTMLElement} slotEl the container the reading card provides
 * @param {object} book one manifest row
 * @param {{session: object|null, canModerate?: () => boolean}} opts
 *   `canModerate` is a FUNCTION, called at click time: resolveSiteAccess()
 *   settles asynchronously and a card may open before it does. Its answer must
 *   come from the real role model ('operateClub'), never a display-name guess.
 */
export async function renderEbookNotes(db, slotEl, book, opts) {
  if (!slotEl) return;
  const options = opts || {};
  const session = options.session || null;
  const canModerate = typeof options.canModerate === 'function' ? options.canModerate : () => false;
  const key = warningTitleFor(book);

  slotEl.textContent = '';
  if (!db || !key.title) {
    // No title to key on is not an empty shelf of notes — it is a book this
    // feature cannot address, and saying nothing would look like "no notes".
    slotEl.appendChild(el('div', 'eb-notes-empty', db
      ? 'This file has no title to file content notes under.'
      : 'Content notes could not load — sign-in did not initialise.'));
    return;
  }

  slotEl.appendChild(el('h3', 'eb-notes-h', 'Content notes'));

  const publishedBox = el('div', 'eb-notes-pub');
  publishedBox.appendChild(el('div', 'eb-notes-status', 'Checking published sources…'));
  slotEl.appendChild(publishedBox);

  const readerBox = el('div', 'eb-notes-reader');
  slotEl.appendChild(readerBox);

  // ⚠️ The panel NAMES THE TITLE IT LOOKED UNDER whenever that is not the
  // title on the card. Some joins match by subtitle extension, so the notes
  // may belong to the audiobook edition rather than this exact file, and
  // provenance-in-words costs no branch. (Same rule the library's panel and
  // OtherVersions follow.)
  if (key.keying === 'audiobook' && key.audiobookTitle !== key.ownTitle) {
    const prov = el('div', 'eb-notes-prov');
    prov.appendChild(document.createTextNode('Shared with the audiobook catalog’s '));
    prov.appendChild(el('em', null, '“' + key.audiobookTitle + '”'));
    prov.appendChild(document.createTextNode('.'));
    slotEl.appendChild(prov);
  } else if (key.keying === 'beside') {
    slotEl.appendChild(el(
      'div',
      'eb-notes-prov',
      'Filed beside an audiobook, but not matched to one — these notes are this '
      + 'file’s own.',
    ));
  }

  renderReaderNotes();
  renderPublished();

  async function renderPublished() {
    const map = await fetchPublishedWarnings();
    publishedBox.textContent = '';
    const found = publishedWarningsFor(map, key.title);
    if (map === null) {
      // An outage is not an answer about the book. Say which it is.
      publishedBox.appendChild(el('div', 'eb-notes-status',
        'Published sources could not be reached just now.'));
      return;
    }
    if (found.state === 'unknown') {
      publishedBox.appendChild(el('div', 'eb-notes-status',
        'Published sources have not been checked for this one yet.'));
      return;
    }
    if (found.state === 'clean') {
      publishedBox.appendChild(el('div', 'eb-notes-status',
        'Published sources checked — none listed.'));
      return;
    }
    publishedBox.appendChild(el('div', 'eb-notes-status',
      'From published sources (' + found.warnings.length + '):'));
    const ul = el('ul', 'eb-notes-list');
    found.warnings.forEach((w) => {
      const li = el('li', null);
      li.appendChild(document.createTextNode(String(w.label == null ? '' : w.label) + ' '));
      if (w.source_url) {
        const a = el('a', 'eb-notes-src', 'source');
        a.href = w.source_url;
        a.target = '_blank';
        a.rel = 'noopener';
        li.appendChild(a);
      }
      ul.appendChild(li);
    });
    publishedBox.appendChild(ul);
  }

  async function renderReaderNotes() {
    let notes = [];
    try {
      notes = await getUserWarnings(db, key.title);
    } catch (e) {
      // Offline, rules refusal, anything: the reader half simply says so.
      readerBox.textContent = '';
      readerBox.appendChild(el('div', 'eb-notes-status',
        'Readers’ notes could not be loaded just now.'));
      return;
    }
    readerBox.textContent = '';

    if (notes.length) {
      readerBox.appendChild(el('div', 'eb-notes-status',
        'From readers here (' + notes.length + '):'));
      const ul = el('ul', 'eb-notes-list');
      notes.forEach((w) => {
        const li = el('li', null);
        li.appendChild(document.createTextNode(String(w.label == null ? '' : w.label) + ' '));
        li.appendChild(el('em', 'eb-notes-by', '— added by ' + (w.displayName || 'someone')));
        const verdict = noteDeleteAffordance(w, session, canModerate());
        if (verdict.show) {
          const del = el('button', 'eb-notes-x', '✕');
          del.type = 'button';
          del.title = verdict.title;
          del.setAttribute('aria-label', verdict.title);
          del.addEventListener('click', async () => {
            if (verdict.asModerator
                && !window.confirm('Remove “' + w.label + '” added by ' + w.displayName + '?')) {
              return;
            }
            const r = await deleteUserWarning(db, w, session, { canModerate: canModerate() });
            // ⚠️ A refused delete used to be discarded silently on the main
            // site, which looked exactly like a dead button. Nobody sees a
            // bare status, and nobody sees nothing at all.
            if (!r.success) { window.alert(r.error); return; }
            renderReaderNotes();
          });
          li.appendChild(del);
        }
        ul.appendChild(li);
      });
      readerBox.appendChild(ul);
    }

    if (!session || !session.displayName) {
      readerBox.appendChild(el('div', 'eb-notes-status',
        'Sign in again to add a note — this session cannot say who you are.'));
      return;
    }

    const add = el('button', 'eb-notes-add', '+ Add a content note');
    add.type = 'button';
    add.addEventListener('click', async () => {
      const label = window.prompt(
        'What should readers know going in? (e.g. "Animal death", "Gore")',
      );
      if (label === null) return;
      const r = await addUserWarning(db, key.title, label, session);
      if (!r.success) { window.alert(r.error); return; }
      renderReaderNotes();
    });
    readerBox.appendChild(add);
  }
}

export { MAX_WARNING_LABEL };
