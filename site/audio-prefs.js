// audio-prefs.js — what the player remembers between sessions
// ES module, browser-native. PURE apart from an injectable storage object.
//
// AUDIO PLAYER PHASE 2, 2026-09-02.
// Design: catalog-platform/docs/info/audio-player-design.md §6, §8 #4, §9.2 #2
//
// ✅ PHASE 3 (2026-09-02) MOVED `rate` ONTO THE POSITION DOCUMENT, and this
// file's copy of it is now a CACHE rather than the store. Design §9.2 #2
// wanted the playback rate to follow a person between devices; the position
// document is what does that, and `site/listen.js` writes both.
//
// ⚠️ THE LOCAL COPY IS NOT VESTIGIAL AND MUST NOT BE DELETED. It is read
// SYNCHRONOUSLY when a book opens, which is what makes the first chapter start
// at the right speed — exactly the asymmetry `reading-position.js` §3 draws
// for the position itself: *"the network must never be on the critical path"*.
// The document reconciles afterwards, and only when the LISTENER agrees to
// take another device's place (listen.js `applyStoredRate`), because changing
// somebody's speed unasked reads as "the narrator sounds wrong".
//
// ⚠️ It was local-ONLY through phase 2, on purpose: the sequencing rule the
// whole plan is built on is *"every persisted-key decision lands before
// anything writes against it"*, and the position document was gated on a
// `firestore.rules` deploy plus a live smoke test (§1.4, §7.4). That was a
// phase boundary, not an oversight, and the migration was one key.
//
// ⚠️ SPEED IS REMEMBERED PER BOOK, NEVER GLOBALLY (§6). Narrator pace varies
// enormously, so one global rate means every new book opens at the speed that
// suited a different narrator. And a book left at 2x and resumed a week later
// reads as *"the narrator sounds wrong"* — which is why the rate is also shown
// on the control at all times (that half is in listen.js).
//
// ⚠️ THE SKIP INTERVAL IS PER DEVICE, NOT PER BOOK (§8 #4). It is a thumb
// habit, not a property of a narrator, and "people are religious about it".

/** Speeds offered as taps. Design §6, verbatim — and it goes to 3x. */
export const SPEEDS = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0];

/** Skip intervals offered. Design §8 #4: "make the interval configurable". */
export const SKIP_INTERVALS = [10, 15, 30];

/** The default skip, and the one Media Session advertises to the lock screen. */
export const DEFAULT_SKIP_SEC = 15;

/** Sleep-timer presets in minutes. Design §8 #8. `null` = "end of chapter". */
export const SLEEP_PRESETS_MIN = [5, 10, 15, 30, 45, 60];

const RATE_KEY_PREFIX = 'ab:audio:rate:';
const SKIP_KEY = 'ab:audio:skip';

/**
 * A storage that cannot throw.
 *
 * ⚠️ `localStorage` THROWS, it does not merely fail — Safari in private
 * browsing, a blocked-cookies setting, and a full quota all raise on read AND
 * on write. `reading-position.js` degrades the same way for the same reason:
 * losing a remembered speed must never be able to take the player down with
 * it. Every accessor here is wrapped, and every failure answers "no preference"
 * rather than propagating.
 */
function safeStore(store) {
  return store || (typeof localStorage !== 'undefined' ? localStorage : null);
}

function readKey(store, key) {
  try {
    const s = safeStore(store);
    return s ? s.getItem(key) : null;
  } catch {
    return null;
  }
}

function writeKey(store, key, value) {
  try {
    const s = safeStore(store);
    if (s) s.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

/**
 * Snap an arbitrary number to the nearest offered speed.
 *
 * ⚠️ Values are SNAPPED rather than rejected, because the stored value is the
 * one thing here that outlives a change to {@link SPEEDS}. If the ladder is
 * ever re-cut, a book remembered at a retired speed must still open at
 * something sensible — not at 1.0, which would silently discard the memory of
 * every book on the old ladder.
 */
export function nearestSpeed(value) {
  const n = typeof value === 'number' && isFinite(value) ? value : NaN;
  if (!isFinite(n)) return 1.0;
  return SPEEDS.reduce((best, s) => (
    Math.abs(s - n) < Math.abs(best - n) ? s : best
  ), SPEEDS[0]);
}

/**
 * The remembered speed for one book. 1.0 when nothing is remembered, when the
 * store refuses, or when the stored value is unusable.
 *
 * @param {string} bookId the estate's book identity fold (bookIdFromTitle)
 */
export function getBookRate(bookId, store) {
  if (!bookId) return 1.0;
  const raw = readKey(store, RATE_KEY_PREFIX + bookId);
  if (raw === null) return 1.0;
  const n = parseFloat(raw);
  if (!isFinite(n) || n <= 0) return 1.0;
  return nearestSpeed(n);
}

/** Remember the speed for one book. Answers whether it was actually stored. */
export function setBookRate(bookId, rate, store) {
  if (!bookId) return false;
  const snapped = nearestSpeed(rate);
  return writeKey(store, RATE_KEY_PREFIX + bookId, String(snapped));
}

/** The device's skip interval, defaulting to 15 s. */
export function getSkipSec(store) {
  const raw = readKey(store, SKIP_KEY);
  const n = raw === null ? NaN : parseInt(raw, 10);
  return SKIP_INTERVALS.includes(n) ? n : DEFAULT_SKIP_SEC;
}

/**
 * Set the device's skip interval.
 * ⚠️ An unoffered value is REFUSED, not stored — unlike the speed, which is
 * snapped. The difference is deliberate: the speed ladder may be re-cut and
 * old values must survive it, whereas a skip interval comes from a fixed
 * three-way control and anything else is a bug in the caller.
 */
export function setSkipSec(seconds, store) {
  if (!SKIP_INTERVALS.includes(seconds)) return false;
  return writeKey(store, SKIP_KEY, String(seconds));
}
