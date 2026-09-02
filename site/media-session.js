// media-session.js — the lock screen, the car stereo, the headphone buttons
// ES module, browser-native.
//
// AUDIO PLAYER PHASE 2, 2026-09-02.
// Design: catalog-platform/docs/info/audio-player-design.md §4.4
//
// ⚠️ THIS IS ~30 LINES OF WIRING THAT DECIDES WHETHER THE FEATURE IS USABLE.
// Design §4.4 and §9.2 #1: it is what puts the cover, the chapter title and
// the skip buttons on a locked phone, and it is what makes the Safari-tab path
// (the ONLY path that works on iPhone — §4.1, owner decision 6) pleasant
// rather than merely functional. MEASURED support (§4.4, against MDN's
// browser-compat-data): everything used here is available in Chrome 73+,
// Chrome Android 57+, Firefox 82+ and Safari 15+.
//
// 🔴 THE ONE TRAP, AND IT IS WHY EVERY CALL IS WRAPPED SEPARATELY:
// `setActionHandler` **THROWS** for an action the browser does not support,
// and one throw aborts the rest of the wiring. Wrapping the whole block in a
// single try/catch would mean an unsupported `seekto` on some browser silently
// costs you `play`, `pause` and both skips — the handlers that matter most —
// with no error anywhere. So each handler gets its own try/catch. This is the
// design's own instruction (§4.4, verbatim) and it is not defensive padding.
//
// ⚠️ `previoustrack`/`nexttrack` are mapped to previous/next CHAPTER, not to a
// track in the file. Design §4.4: that is exactly what a listener expects a
// car stereo's track buttons to do in an audiobook.

/**
 * Is a Media Session available at all?
 * @param {object} [nav] injectable for tests; defaults to the real navigator.
 */
export function hasMediaSession(nav) {
  const n = nav || (typeof navigator !== 'undefined' ? navigator : null);
  return !!(n && n.mediaSession);
}

/**
 * Wire the transport controls.
 *
 * Each handler is registered independently, so an unsupported action costs
 * only itself. Returns the list of actions that were ACCEPTED — the caller can
 * report honestly what the lock screen will and will not offer, and the tests
 * assert on it.
 *
 * @param {object} handlers  {play, pause, seekbackward, seekforward,
 *                            previoustrack, nexttrack, seekto}
 * @param {number} skipSec   the interval `seekbackward`/`seekforward` advertise
 * @param {object} [nav]
 * @returns {string[]} the actions successfully registered
 */
export function wireHandlers(handlers, skipSec, nav) {
  const n = nav || (typeof navigator !== 'undefined' ? navigator : null);
  if (!n || !n.mediaSession) return [];
  const ms = n.mediaSession;
  const accepted = [];
  for (const [action, fn] of Object.entries(handlers || {})) {
    if (typeof fn !== 'function') continue;
    try {
      ms.setActionHandler(action, fn);
      accepted.push(action);
    } catch {
      // ⚠️ Swallowed ON PURPOSE and per action. An unsupported action is not a
      // failure of the player; it is a browser that does not offer that
      // button. The next handler must still be registered.
    }
  }
  // ⚠️ The skip INTERVAL is not set here, and there is no API to set it.
  // `seekbackward`/`seekforward` handlers receive `details.seekOffset` — the
  // OS's requested jump — and the design's instruction (§4.4, CITED as working
  // on iOS) is to HONOUR it when present and fall back to our own interval
  // when it is not. That decision belongs in the handler, which is why
  // `skipSec` is passed to the caller's closure rather than to the browser.
  void skipSec;
  return accepted;
}

/**
 * Publish what is playing to the OS.
 *
 * ⚠️ THE CHAPTER TITLE IS THE POINT. Design §4.4: the lock screen must answer
 * *"what chapter am I in"* without unlocking, so the chapter goes in `album`.
 * A lock screen that shows only the book title tells a listener nothing they
 * did not already know.
 *
 * ⚠️ ARTWORK SHIPS A 96×96 AS WELL AS A 512×512 (§4.4, CITED): older iOS
 * pixellated an upscaled small icon, iOS 18 fixed it, and the extra entry
 * costs nothing but one array element. Both point at the same R2 object —
 * the size hint is advice to the OS, not a promise of distinct files.
 *
 * @param {object} meta {title, author, chapterTitle, coverUrl}
 * @param {object} [nav]
 * @param {Function} [MetadataCtor] injectable MediaMetadata for tests
 * @returns {boolean} whether metadata was actually published
 */
export function publishMetadata(meta, nav, MetadataCtor) {
  const n = nav || (typeof navigator !== 'undefined' ? navigator : null);
  if (!n || !n.mediaSession) return false;
  const Ctor = MetadataCtor
    || (typeof globalThis !== 'undefined' ? globalThis.MediaMetadata : undefined);
  if (typeof Ctor !== 'function') return false;

  const m = meta || {};
  const artwork = m.coverUrl
    ? [
      { src: m.coverUrl, sizes: '96x96', type: 'image/jpeg' },
      { src: m.coverUrl, sizes: '512x512', type: 'image/jpeg' },
    ]
    : [];
  try {
    n.mediaSession.metadata = new Ctor({
      title: m.title || 'Audiobook',
      artist: m.author || '',
      // ⚠️ The chapter, not the series. See the note above.
      album: m.chapterTitle || '',
      artwork,
    });
    return true;
  } catch {
    return false;
  }
}

/**
 * Keep the lock screen's own scrubber live.
 *
 * ⚠️ THE POSITION REPORTED IS BOOK-RELATIVE, AND THAT IS DELIBERATE — it is
 * the one place in this player that is. The OS scrubber's domain is the media
 * element's duration; it has no concept of a chapter, and lying to it about
 * the domain would make the lock screen disagree with the phone's own idea of
 * the file. The CHAPTER-relative bar (requirement 7) is ours and lives on the
 * page; the chapter is communicated to the lock screen through `album`
 * instead. Two audiences, two honest answers.
 *
 * ⚠️ `setPositionState` THROWS on nonsense input (a position past the
 * duration, a NaN, a non-positive rate) and browsers differ on what counts as
 * nonsense — so it is guarded AND validated before the call.
 *
 * @returns {boolean} whether a state was published
 */
export function publishPosition(durationSec, positionSec, playbackRate, nav) {
  const n = nav || (typeof navigator !== 'undefined' ? navigator : null);
  if (!n || !n.mediaSession || typeof n.mediaSession.setPositionState !== 'function') return false;
  const d = Number(durationSec);
  const p = Number(positionSec);
  const r = Number(playbackRate);
  if (!isFinite(d) || d <= 0) return false;
  if (!isFinite(p) || p < 0 || p > d) return false;
  if (!isFinite(r) || r <= 0) return false;
  try {
    n.mediaSession.setPositionState({ duration: d, position: p, playbackRate: r });
    return true;
  } catch {
    return false;
  }
}

/**
 * Tell the OS whether sound is coming out, so the lock screen shows the right
 * button. `state` is 'playing' | 'paused' | 'none'.
 */
export function publishPlaybackState(state, nav) {
  const n = nav || (typeof navigator !== 'undefined' ? navigator : null);
  if (!n || !n.mediaSession) return false;
  try {
    n.mediaSession.playbackState = state;
    return true;
  } catch {
    return false;
  }
}

/**
 * Drop everything on leaving the page.
 *
 * ⚠️ Clearing the handlers matters more than clearing the metadata: a stale
 * handler holding a closure over a dead `<audio>` element is a lock-screen
 * play button that does nothing — the exact silent-dead-control the estate's
 * refusal rule forbids, in the one place a person cannot see a page to explain
 * it on.
 */
export function teardown(nav) {
  const n = nav || (typeof navigator !== 'undefined' ? navigator : null);
  if (!n || !n.mediaSession) return;
  for (const action of ['play', 'pause', 'seekbackward', 'seekforward',
    'previoustrack', 'nexttrack', 'seekto', 'stop']) {
    try { n.mediaSession.setActionHandler(action, null); } catch { /* unsupported */ }
  }
  try { n.mediaSession.metadata = null; } catch { /* ignore */ }
  try { n.mediaSession.playbackState = 'none'; } catch { /* ignore */ }
}
