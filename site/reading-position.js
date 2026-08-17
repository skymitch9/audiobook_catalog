/**
 * reading-position.js — "save your spot", the ONE implementation.
 *
 * Owner's ask, 2026-08-17: *"for reading ebooks we also need to have it save
 * your spot. this will be so important for pwa."* Both readers, both lanes,
 * per person per book, and CROSS-DEVICE — which is the whole reason this is a
 * Firestore document and not a localStorage key. (localStorage is here too,
 * but as a first-paint cache, not as the store; see §3.)
 *
 * Loaded by `site/reader.js` only. It is a separate module for the same reason
 * `epub-loader.js` is: `/read`'s CSP forbids inline script, reader.js is
 * already the reader's whole brain, and a persisted-key implementation wants
 * its own tests (`site/__tests__/reading-position.test.js`).
 *
 * ─────────────────────────────────────────────────────────────────────────
 * ## 1. ⚠️ THE KEY IS THE BOOK'S TITLE-ID, **NOT** THE ANCHOR
 *
 * The obvious key is the `anchor` — the reader already has one, the stream
 * endpoint is keyed on it, and it is right there in the URL. It is the wrong
 * key, and the failure is silent.
 *
 * `anchor` is `"b-" + sha256(RELATIVE PATH)[:12]`
 * (`scripts/build_ebook_manifest.ebook_anchor`). **Re-filing or renaming a
 * file changes it.** A deep link that dies is a page that does not scroll; a
 * position that dies is a person's place in a book, gone, with no error
 * anywhere and nothing to notice. The viewer design flagged exactly this in
 * advance — `library_catalog/docs/info/ebook-viewer-design.md` §7.1, "Do not
 * key a stored position on the anchor".
 *
 * So the key is the estate's own book identity: `bookIdFromTitle(title)`,
 * where *title* is the AUDIOBOOK catalog's spelling when this ebook has a
 * sibling and the ebook's own when it does not. ⚠️ That choice is **not
 * re-derived here** — `ebook-notes.warningTitleFor(book)` already answers it,
 * for content notes, and its header explains at length why keying on the
 * epub's own title fails silently in both directions. One implementation.
 *
 * The `anchor` IS stored, as a **hint field**: it makes "open the book this
 * position belongs to" a one-hop lookup for as long as the file keeps its
 * path, and costs nothing when it does not.
 *
 * ## 2. ⚠️ `pos.kind` TRAVELS WITH `pos.value`, ATOMICALLY
 *
 * `{ kind: 'page', value: 137 }` or `{ kind: 'cfi', value: 'epubcfi(…)' }` —
 * one map, one write. A CFI read as a page number is a silent jump to the
 * wrong place, so the pair must never be able to disagree; firestore.rules
 * refuses a document whose `pos` has no `kind`.
 *
 * ⚠️ **A CFI is a persisted key produced by a SPECIFIC renderer.** These come
 * from foliate-js `view.getCFI()` (vendored at a pinned commit — see
 * `site/static/foliate/VENDORED.md`). Swapping the renderer is therefore a
 * MIGRATION, not an edit: epub.js's `EpubCFI` and foliate's `epubcfi.js` are
 * both "EPUB CFI" and are not guaranteed to agree on the strings they emit.
 *
 * ## 3. THE TWO STORES, AND WHY BOTH
 *
 *   Firestore  — the store. Cross-device is the ask; a PWA on a phone that
 *                cannot see where the desktop got to is the thing being fixed.
 *   localStorage — a first-paint CACHE, per device. The network must never be
 *                on the critical path of "show me my book": the local row is
 *                read synchronously and applied immediately, and Firestore's
 *                answer reconciles afterwards.
 *
 * ⚠️ `updatedAt` is a **client-clock epoch number**, not `serverTimestamp()`,
 * and that is deliberate. Last-write-wins has to compare a row written offline
 * in localStorage against a row written by another device, and a server
 * sentinel is unreadable until it round-trips. The cost is honest and small:
 * a device with a badly wrong clock can win or lose a race it should not.
 * `updatedAtServer` rides along as a server-stamped audit value that nothing
 * compares.
 *
 * ## 4. ⚠️ NEVER JUMP SOMEBODY SILENTLY
 *
 * When the remote row is NEWER than the local one and points somewhere else,
 * the reader offers *"You were at 63% on iPhone — Jump / Stay"*. It does not
 * move the page. Cross-device sync that relocates a reader without asking is
 * the single most common complaint about every reader that has ever shipped
 * one. With no local row at all there is nothing to conflict with, so the
 * remote position is simply restored.
 */

import { doc, getDoc, setDoc, serverTimestamp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';

/** The collection, lane-suffixed like every other client-written store. */
export const POSITION_COLLECTION = 'readingPositions';

/** localStorage prefix. Namespaced by lane so /dev/ never resumes prod's spot. */
export const LOCAL_PREFIX = 'ab_reading_pos_';

/** The save debounce, in ms. A page turn is a keypress; a write is not. */
export const SAVE_DEBOUNCE_MS = 3000;

/**
 * The document id: `${uid}_${bookId}`.
 *
 * ⚠️ The `_` is load-bearing — firestore.rules reads the uid back out with
 * `docId.split('_')[0]`, which is what makes "your own document" enforceable
 * rather than advisory. A Firebase uid is alphanumeric and a `bookIdFromTitle`
 * slug is `[a-z0-9-]`, so neither half can contain the separator. Changing
 * this shape is a rules change AND a migration, never an edit.
 */
export function positionDocId(uid, bookId) {
  return `${uid}_${bookId}`;
}

/** The per-device cache key. Lane-suffixed via col(), same as the collection. */
export function localKey(bookId) {
  return LOCAL_PREFIX + col(POSITION_COLLECTION) + '_' + bookId;
}

/**
 * A coarse device label, for the §4 prompt's wording and nothing else.
 *
 * Deliberately vague — "iPhone · Safari", not a fingerprint. It exists so
 * "jump there?" can say WHERE the other place came from, because "somewhere
 * else" is not a sentence anyone can act on.
 */
export function describeDevice(ua, platformHint) {
  const s = String(ua || '');
  const os = /iPhone/i.test(s) ? 'iPhone'
    : /iPad/i.test(s) ? 'iPad'
      : /Android/i.test(s) ? 'Android'
        : /Macintosh|Mac OS X/i.test(s) ? 'Mac'
          : /Windows/i.test(s) ? 'Windows'
            : /Linux/i.test(s) ? 'Linux'
              : (platformHint || 'This device');
  // ⚠️ Order matters: every Chrome UA claims Safari, and Edge claims both.
  const browser = /Edg\//.test(s) ? 'Edge'
    : /OPR\//.test(s) ? 'Opera'
      : /Firefox\//.test(s) ? 'Firefox'
        : /Chrome\//.test(s) ? 'Chrome'
          : /Safari\//.test(s) ? 'Safari'
            : '';
  return browser ? `${os} · ${browser}` : os;
}

/**
 * The stored document, built in one place so the local row and the remote row
 * can never drift into different shapes (they are compared against each other).
 *
 * @param {{uid: string, bookId: string, anchor: string, format: string,
 *          pos: {kind: string, value: any}, progress?: number|null,
 *          label?: string, title?: string, device?: string, at?: number}} p
 */
export function makePosition(p) {
  const at = typeof p.at === 'number' ? p.at : Date.now();
  const row = {
    uid: String(p.uid || ''),
    bookId: String(p.bookId || ''),
    anchor: String(p.anchor || ''),      // §1 — a hint, never the key
    format: String(p.format || ''),
    pos: { kind: String(p.pos.kind), value: p.pos.value },   // §2 — atomic
    updatedAt: at,
    device: String(p.device || ''),
  };
  if (typeof p.progress === 'number' && isFinite(p.progress)) {
    row.progress = Math.min(1, Math.max(0, p.progress));
  }
  if (p.label) row.label = String(p.label);
  if (p.title) row.title = String(p.title);
  return row;
}

/**
 * Whichever of two rows was written last. Null-safe both ways.
 * ⚠️ Last-write-wins, NEVER furthest-progress-wins: somebody re-reading, or
 * who flipped back to check a map, would have their real place overwritten by
 * a stale high-water mark.
 */
export function newerOf(a, b) {
  if (!a) return b || null;
  if (!b) return a;
  const at = typeof a.updatedAt === 'number' ? a.updatedAt : -1;
  const bt = typeof b.updatedAt === 'number' ? b.updatedAt : -1;
  return bt > at ? b : a;
}

/** Do two rows point at the same place? Used to decide whether to ASK (§4). */
export function samePlace(a, b) {
  if (!a || !b || !a.pos || !b.pos) return false;
  return a.pos.kind === b.pos.kind && String(a.pos.value) === String(b.pos.value);
}

/**
 * The human-readable hint: "p. 214 of 392", "63%", or a chapter label.
 * ⚠️ Never invented — it is only ever what the renderer already reported.
 */
export function describePosition(row) {
  if (!row) return '';
  if (row.label) return row.label;
  if (row.pos && row.pos.kind === 'page') return `p. ${row.pos.value}`;
  if (typeof row.progress === 'number') return `${Math.round(row.progress * 100)}%`;
  return 'where you left off';
}

/* ── the per-device cache ────────────────────────────────────────────────── */

/**
 * The cached row for a book, or null. Synchronous by design: this is what the
 * first paint uses, and a first paint that waits for a network is the thing
 * this whole file exists to avoid.
 *
 * ⚠️ Never throws. Storage can be unavailable (private mode, a wiped quota, a
 * hostile embedder) and a reader whose BOOK will not open because its
 * BOOKMARK could not be read is a strictly worse product than one that opens
 * at page 1.
 */
export function loadLocal(bookId, storage) {
  try {
    const store = storage || (typeof localStorage !== 'undefined' ? localStorage : null);
    if (!store || !bookId) return null;
    const raw = store.getItem(localKey(bookId));
    if (!raw) return null;
    const row = JSON.parse(raw);
    return row && row.pos && row.pos.kind ? row : null;
  } catch (e) {
    return null;
  }
}

/** Cache a row for this device. Never throws — see loadLocal. */
export function saveLocal(bookId, row, storage) {
  try {
    const store = storage || (typeof localStorage !== 'undefined' ? localStorage : null);
    if (!store || !bookId || !row) return false;
    store.setItem(localKey(bookId), JSON.stringify(row));
    return true;
  } catch (e) {
    return false;
  }
}

/* ── the store ───────────────────────────────────────────────────────────── */

/**
 * This person's stored position for this book, or null.
 *
 * ⚠️ Never throws and never rejects. A rules refusal, an offline device or a
 * Firestore outage all answer "no saved position", which is a state the reader
 * already handles (it opens the book at the start). Reporting it as an error
 * would put an outage sentence in front of somebody whose book opened fine.
 */
export async function loadRemote(db, uid, bookId) {
  try {
    if (!db || !uid || !bookId) return null;
    const snap = await getDoc(doc(db, col(POSITION_COLLECTION), positionDocId(uid, bookId)));
    if (!snap.exists()) return null;
    const row = snap.data();
    return row && row.pos && row.pos.kind ? row : null;
  } catch (e) {
    return null;
  }
}

/**
 * Write this person's position. Answers a boolean rather than throwing — see
 * loadRemote. `updatedAtServer` is stamped for audit and is NOT what
 * last-write-wins compares (§3).
 */
export async function saveRemote(db, row) {
  try {
    if (!db || !row || !row.uid || !row.bookId) return false;
    await setDoc(
      doc(db, col(POSITION_COLLECTION), positionDocId(row.uid, row.bookId)),
      { ...row, updatedAtServer: serverTimestamp() },
    );
    return true;
  } catch (e) {
    console.warn('[reading-position] could not save your spot:', e);
    return false;
  }
}

/* ── the keeper ──────────────────────────────────────────────────────────── */

/**
 * The thing reader.js actually holds: `record()` on every page turn,
 * `flush()` when the page is going away.
 *
 * ⚠️ THE LOCAL WRITE IS IMMEDIATE AND THE REMOTE ONE IS DEBOUNCED, and that
 * asymmetry is the whole design. A page turn is a keypress: at one Firestore
 * write per turn a reader flipping through an index would cost dozens of
 * writes a minute for a fact only the last of which matters. But the local
 * write costs nothing and is what makes a crash, a swipe-away or a killed tab
 * lose nothing — so it happens every time.
 *
 * ⚠️ `flush()` is bound to `pagehide` AND `visibilitychange`, not to
 * `beforeunload`: mobile browsers routinely kill a backgrounded tab without
 * ever firing an unload event, which is precisely the case a PWA reader lives
 * in. Both handlers are `void`-called and cannot block the page going away.
 *
 * @param {{db: any, uid: string, bookId: string, anchor: string,
 *          format: string, title?: string, device?: string,
 *          delay?: number, now?: () => number, storage?: any}} cfg
 */
export function createPositionKeeper(cfg) {
  const delay = typeof cfg.delay === 'number' ? cfg.delay : SAVE_DEBOUNCE_MS;
  const now = cfg.now || (() => Date.now());
  let timer = null;
  let pending = null;
  let armed = false;

  function build(pos, extra) {
    return makePosition({
      uid: cfg.uid,
      bookId: cfg.bookId,
      anchor: cfg.anchor,
      format: cfg.format,
      title: cfg.title,
      device: cfg.device,
      pos,
      progress: extra && extra.progress,
      label: extra && extra.label,
      at: now(),
    });
  }

  return {
    /**
     * ⚠️ ARMING IS EXPLICIT, and it is what stops a failed open from
     * overwriting a good position. reader.js arms the keeper only after the
     * book has actually rendered; a book that would not open records nothing,
     * so "the file broke" never costs somebody the page they were on.
     */
    arm() { armed = true; },

    /** One observed position. Local now, remote on the debounce. */
    record(pos, extra) {
      if (!armed || !pos || pos.value == null) return;
      pending = build(pos, extra);
      saveLocal(cfg.bookId, pending, cfg.storage);
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => { timer = null; void this.flush(); }, delay);
    },

    /** Write whatever is pending, now. Safe to call when nothing is. */
    flush() {
      if (timer) { clearTimeout(timer); timer = null; }
      const row = pending;
      pending = null;
      if (!row) return Promise.resolve(false);
      return saveRemote(cfg.db, row);
    },

    /** Tests only. */
    _pending() { return pending; },
  };
}
