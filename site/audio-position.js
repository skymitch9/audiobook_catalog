// audio-position.js — the audio LOCATOR, and the eviction stamp's throttle
// ES module, browser-native (no build step). PURE: no DOM, no fetch, no clock
// except the one a caller hands in.
//
// AUDIO PLAYER PHASE 3 — "save your spot", 2026-09-02.
// Design of record: catalog-platform/docs/info/audio-player-design.md
//   §7.4 — the position store: one new `kind`, not a new store
//   §8 #1 — write on pause, on chapter change, on pagehide, and every ~15 s
//   §9.2 #2 — per-book speed moves onto the position document
//   §10.1 — the eviction access timestamps, and why `last_position_at` is
//           the half phase 3 owes
//
// ⚠️ THE STORE IS NOT HERE. `site/reading-position.js` is the ONE
// implementation of "save your spot" — the doc id, the two stores, the
// last-write-wins reconcile and the offer-never-apply resume bar are all its,
// and phase 3 reuses it wholesale exactly as §7.4 says to. What lives here is
// the part that is audio-shaped and that the ebook reader has no equivalent
// of: turning a playhead into a locator that survives a re-encode, and back.
//
// ─────────────────────────────────────────────────────────────────────────
// ## 1. 🔴 `{chapter, offsetSec}` — NEVER a single absolute second
//
// Design §7.4, verbatim: *"an absolute offset is a position in the FILE, and a
// re-encode, a re-rip or a chapter-boundary correction moves it silently. A
// chapter index plus an offset within that chapter survives all three, and
// degrades gracefully — a lost chapter costs a chapter, never a book."*
//
// ⚠️ `seconds` IS carried alongside, and it is FOR DISPLAY ONLY. The same
// sentence that asks for it says **"never navigate by it"**, and this module
// enforces that rather than trusting a future caller to remember:
// {@link resolveLocator} never reads `seconds`. If you find yourself wanting
// it to, read §7.4 first — the whole point is that the absolute number is the
// one that goes wrong quietly.
//
// ## 2. ⚠️ THE SAME 6-SECOND TRAP THE CHAPTER MODEL HAS
//
// `offsetSec` is measured from `chapters[i].startSec`, so it inherits phase
// 0a's precision fix. `audio-chapters.chapterStartSec` prefers `start_sec`
// (full float) over `start_min` (rounded to 6 s) for exactly this reason —
// six seconds is invisible in a book-club milestone and audible in a player,
// and a position is a PERSISTED KEY derived from that table. Design §7.4:
// storing positions against rounded boundaries and then correcting the
// boundaries is a migration, not an edit.
//
// ## 3. THE CHAPTERLESS BOOK IS A REAL CASE, NOT AN EDGE ONE
//
// `buildChapters()` answers `[]` for a book with no chapter data and the page
// plays it fine — the bar simply spans the whole recording. Such a book
// records `chapter: 0` with `offsetSec` holding the absolute time, because
// with no chapter table chapter 0 starts at 0 and the two are the same number.
// {@link resolveLocator} accepts that pairing and refuses any other, so a
// position saved WITH chapters can never be silently re-read as an absolute
// time against a book whose chapters failed to load.

/**
 * ⚠️ How often a live playhead is recorded while playing, in ms.
 *
 * Design §8 #1: *"write on pause, on chapter change, on `pagehide` /
 * `visibilitychange`, and every ~15 s while playing (throttled)"*. The remote
 * write is debounced on top of this by `reading-position.js`
 * (`SAVE_DEBOUNCE_MS`), so this number governs how much of a listen is at risk
 * when a tab is killed without an event — 15 seconds of audio, which is one
 * press of the back button.
 */
export const RECORD_INTERVAL_MS = 15000;

/**
 * ⚠️ How often the eviction stamp (§4 below) is written, in ms.
 *
 * MUCH coarser than the position itself, and it can afford to be: the evictor
 * asks "has anybody touched this book in 30 DAYS", so one stamp per listening
 * session is already generous. Ten minutes bounds the writes a 15-hour listen
 * costs at ~90 rather than ~3,600.
 */
export const STAMP_INTERVAL_MS = 600000;

/**
 * The Firestore collection the eviction stamp is written to. Lane-suffixed by
 * `fb-env.col()` at the call site, like every other client-written store.
 *
 * ## 4. 🔴 WHY A SECOND, OPAQUE COLLECTION — AND NOT `readingPositions`
 *
 * `app/tools/fulfill_audio_requests.evict_candidates()` needs ONE fact:
 * *"was this anchor touched recently?"*. It is the MID-BOOK SHIELD, and it is
 * the entire reason 30 days was chosen over the owner's 7 — a 30-hour book
 * over a month of commutes is the normal case, not an abandoned one.
 *
 * It cannot get that fact from `readingPositions`:
 *
 *   * `firestore.rules` says `allow list: if false` on that collection, in
 *     both lanes, deliberately — *"enumerating what a household reads is not
 *     a query any client needs"*. The evictor lists collections with the
 *     PUBLIC WEB API KEY (`app/tools/club_books.fetch`), so it is gated
 *     exactly like a browser and would be refused.
 *   * Even with a service account it would be reading every person's exact
 *     place in every book to answer a yes/no about one opaque anchor. That is
 *     more data than the question needs, and the collection's whole posture is
 *     that nobody enumerates it.
 *
 * So the shape is the one `audio_streams` already established for the other
 * half of the same predicate: **one document per anchor, `{ anchor,
 * lastPositionAt }`, epoch MILLISECONDS, the document id IS the anchor.** What
 * a reader learns is an opaque 12-hex fold of a library path and a timestamp —
 * no title, no path, no uid. `site/audio_manifest.json` is gitignored because
 * it maps 630 GB filename by filename; this does not reopen that surface from
 * the other end, any more than `audio_streams` does.
 *
 * ⚠️ **IT DIFFERS FROM `audio_streams` IN ONE WAY, AND ON PURPOSE:** browsers
 * may WRITE this one. `audio_streams` is `allow write: if false` because
 * *"nobody but the Worker has anything true to say here"* — but a saved
 * position is a fact only the listener's browser holds, and no Worker sees it.
 * The forgery trade therefore runs the other way and is worth stating: a
 * FORGED stamp keeps a cached object on the bill (~$0.009/mo, re-uploadable
 * from the local library either way); a MISSING stamp evicts a book somebody
 * is halfway through. The rules guard the two abuses that are NOT benign — a
 * stamp dragged BACKWARDS (which would cause an early eviction) and a stamp
 * parked far in the FUTURE (which would pin an object for ever).
 */
export const STAMP_COLLECTION = 'audio_positions';

/** Is this a usable finite, non-negative number? */
function num(value) {
  return typeof value === 'number' && isFinite(value) && value >= 0;
}

/**
 * A playhead -> the stored locator.
 *
 * @param {Array} chapters the array `audio-chapters.buildChapters()` produced
 * @param {number} seconds `audio.currentTime`
 * @returns {{chapter: number, offsetSec: number, seconds: number}|null}
 *          null when `seconds` is not a usable number — a caller must never
 *          persist a locator it could not compute, because an unparseable
 *          position is indistinguishable from position zero.
 */
export function toLocator(chapters, seconds) {
  if (!num(seconds)) return null;
  const list = Array.isArray(chapters) ? chapters : [];
  if (!list.length) {
    // §3 — chapterless: chapter 0 starts at 0, so the offset IS the time.
    return { chapter: 0, offsetSec: seconds, seconds };
  }
  let i = list.length - 1;
  while (i > 0 && seconds < list[i].startSec) i -= 1;
  const start = list[i].startSec;
  return {
    chapter: i,
    // ⚠️ Never negative. An m4b routinely opens a few tenths of a second
    // before chapter 0 starts, and a negative offset would resolve to a time
    // before the chapter it names.
    offsetSec: Math.max(0, seconds - start),
    seconds,
  };
}

/**
 * The stored locator -> an absolute time to seek to.
 *
 * ⚠️ **`value.seconds` IS NEVER READ HERE** (§1). Design §7.4 says to carry it
 * and never navigate by it, and the enforcement is that this function does not
 * look at it — not a comment asking the next person to be careful.
 *
 * @param {Array} chapters the CURRENT chapter table
 * @param {object} value the stored `pos.value`
 * @returns {number|null} absolute seconds, or null when the locator cannot be
 *          honoured against this chapter table. **A null is a refusal, not a
 *          zero**: the caller must leave the playhead where it is and say so,
 *          because silently starting a 30-hour book from the beginning is the
 *          failure this whole feature exists to prevent.
 */
export function resolveLocator(chapters, value) {
  if (!value || typeof value !== 'object') return null;
  const chapter = value.chapter;
  if (typeof chapter !== 'number' || !isFinite(chapter) || chapter < 0
      || Math.floor(chapter) !== chapter) return null;
  if (!num(value.offsetSec)) return null;

  const list = Array.isArray(chapters) ? chapters : [];
  if (!list.length) {
    // §3 — the chapterless pairing, and ONLY that pairing. A locator that
    // names chapter 3 cannot be honoured by a book with no chapters: its
    // offset is measured from a boundary we do not have, and treating it as
    // absolute would drop somebody eight hours into a book back near the top
    // with no error anywhere.
    return chapter === 0 ? value.offsetSec : null;
  }
  // ⚠️ "A lost chapter costs a chapter, never a book" (§7.4) — but a chapter
  // that is not there AT ALL costs the position, and that is refused in words
  // rather than guessed at. A book re-cut from 68 chapters to 12 has moved
  // every boundary; landing somebody at the end of the new chapter 11 would be
  // a confident wrong answer.
  if (chapter >= list.length) return null;

  const ch = list[chapter];
  const t = ch.startSec + value.offsetSec;
  // Clamp inside the chapter the locator NAMES. A re-encode that shortened a
  // chapter must not push the playhead into the next one.
  if (ch.endSec !== null && ch.endSec > ch.startSec) {
    return Math.min(t, ch.endSec);
  }
  return t;
}

/**
 * Book-relative progress, 0..1, for the position document's `progress` field.
 *
 * ⚠️ Returns null rather than 0 when the duration is not known yet. The last
 * chapter has no end until `loadedmetadata` fires (audio-chapters §8), and a
 * confident `progress: 0` written during that window is a lie that a
 * "continue listening" shelf would later render as "not started".
 */
export function progressFor(seconds, durationSec) {
  if (!num(seconds) || !num(durationSec) || durationSec <= 0) return null;
  return Math.min(1, Math.max(0, seconds / durationSec));
}

/**
 * The human sentence the resume bar shows: *"Chapter 7 — Vanishing · 13:32 in"*.
 *
 * ⚠️ Handed to `reading-position.makePosition` as `label`, because
 * `describePosition()` prefers a stored label over anything it could invent —
 * *"never invented; it is only ever what the renderer already reported"*. The
 * renderer here is the player, and only the player knows what chapter 7 is
 * called.
 *
 * @param {Array} chapters
 * @param {object} value the locator
 * @param {(n: number) => string} formatTime `audio-chapters.formatTime`
 */
export function positionLabel(chapters, value, formatTime) {
  if (!value || typeof value !== 'object') return '';
  const list = Array.isArray(chapters) ? chapters : [];
  const into = num(value.offsetSec) ? formatTime(value.offsetSec) : '';
  const ch = list[value.chapter];
  if (!ch) {
    // A chapterless book, or a chapter table that is not loaded. Say the book
    // time, which is the only true thing available.
    const abs = num(value.seconds) ? formatTime(value.seconds) : into;
    return abs ? `${abs} in` : '';
  }
  const name = ch.title && ch.title !== 'Untitled' ? ` — ${ch.title}` : '';
  return `Chapter ${value.chapter + 1}${name}${into ? ` · ${into} in` : ''}`;
}

/**
 * Would a stamp be written now? A pure throttle so the interval is testable
 * without a clock or a Firestore.
 *
 * ⚠️ Answers TRUE on the first call for an anchor (`lastMs` null/0), because
 * the first touch of a listening session is the one that matters most: a
 * person who opens a book, listens for two minutes and closes the tab must
 * still have shielded it.
 */
export function shouldStamp(lastMs, nowMs, intervalMs = STAMP_INTERVAL_MS) {
  if (!num(nowMs)) return false;
  if (!num(lastMs) || lastMs === 0) return true;
  return nowMs - lastMs >= intervalMs;
}

/**
 * The stamp document body. One place, so the browser and
 * `app/tools/fulfill_audio_requests.parse_position_doc` cannot drift.
 *
 * 🔴 **`lastPositionAt` IS EPOCH MILLISECONDS.** The reader's `_parse_stamp()`
 * treats a number under 1e11 as SECONDS, so a stamp written in seconds is read
 * as a date in 1970 — which is older than any cutoff and therefore says
 * *"evict this book"* about a book somebody is listening to right now. It is
 * the one unit in this seam that must not be guessed at; `Date.now()` is
 * already milliseconds and nothing here divides it.
 */
export function stampBody(anchor, nowMs) {
  return { anchor: String(anchor || ''), lastPositionAt: Math.floor(nowMs) };
}
