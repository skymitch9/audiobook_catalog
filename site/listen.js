/**
 * listen.js — the audiobook player's whole brain (audio player PHASE 2).
 *
 * Loaded as a module by `listen.html`. ⚠️ IT IS AN EXTERNAL FILE ON PURPOSE:
 * /listen's Content-Security-Policy (site/_headers) is `script-src 'self'`
 * plus gstatic for the Firebase SDK, with no `'unsafe-inline'`. Moving this
 * inline breaks the page silently, only in production, where the header is
 * applied. reader.js sits beside this for the same reason.
 *
 * Design of record: catalog-platform/docs/info/audio-player-design.md
 *   §2.3  — a thin custom UI over a bare <audio>, no player library
 *   §3.2  — the service-worker bearer seam AND its mandatory HEAD probe
 *   §4.4  — Media Session
 *   §6    — speed to 3×, per book
 *   §8    — the seven requirements, and the two cross-cutting notes
 *
 * ## What it does, in order
 *
 *   1. reads `?b=<anchor>` — the manifest's own anchor, never recomputed here;
 *   2. brings up a live Firebase session (identity.js keeps one);
 *   3. asks the gated `/api/audio/status` projection what this anchor names,
 *      so the page and the Worker can never disagree about which book it is;
 *   4. registers the bearer-injecting service worker and PROVES the seam with
 *      a HEAD probe before an <audio> element is ever asked to use it;
 *   5. plays it, with a CHAPTER-relative scrub bar.
 *
 * ## ⚠️ The five things that will bite whoever edits this next
 *
 * **1. THE FAILURE MODE OF THIS WHOLE FEATURE IS A SILENTLY DEAD PLAY BUTTON.**
 * `<audio src>` makes the BROWSER issue the range requests, and the page
 * cannot put an `Authorization` header on requests it does not make (§3.1).
 * A service worker can — but if none controls the page (a first load before
 * activation, a hard reload, private browsing, a failed registration) the
 * request goes out bare, the Worker answers a correct worded 401, and the
 * media element reports it to us as a bare `error` event **with no status
 * code**. The person sees a button that does nothing. The estate's rule is
 * that a person must never see a bare HTTP status; a silently dead control is
 * worse than one. So §3.2 item 5's mitigation is MANDATORY and it is `probe()`
 * below: the page issues its OWN request, which it CAN read, and either shows
 * the refusal in words or proves the seam works. ⚠️ Do not "simplify" the play
 * button into existence before that probe answers.
 *
 * **2. EVERY SEEK GOES THROUGH `seekTo()`. There are seven ways to move.**
 * The bar, ±skip, chapter prev/next, the chapter list, the lock screen's
 * `seekto`, and the lock screen's own skip actions. Design §8 records this
 * exact defect shipping in the ebook reader (`reader-page.md` §7.6): a second
 * page-turn path bypassed the position keeper and stopped saving the spot,
 * silently. Phase 3 hangs position writes off `seekTo`; a path that bypasses
 * it will look exactly like that bug.
 *
 * **3. `playbackRate` RESETS TO 1.0 WHEN `src` CHANGES** (§6). Anything that
 * reloads the element must re-apply it. `applyRate()` exists so there is one
 * place that does, and it is called after every load and every recovery.
 *
 * **4. THE LAST CHAPTER HAS NO END until `loadedmetadata` fires** (§8). Until
 * then the chapter bar has no domain and must render as indeterminate rather
 * than as NaN or as a confident zero. `audio-chapters.js` answers `null` for
 * exactly this and every reader of it here handles the null.
 *
 * **5. THE SERVICE WORKER'S SCOPE IS THE LANE, NOT THE ORIGIN.** `/dev/` is a
 * PATH on audiobooks.heygabi.ai, not a host. Registering `/audio-sw.js` at
 * scope `/` from the dev lane would install the PROD copy of the worker and
 * give it control of the promoted site — a dev-lane page silently changing
 * production behaviour. `audio-seam.js`'s `swPaths()` derives both the script
 * URL and the scope from this page's own directory, so each lane gets its own
 * worker — and this file must always pass it `location.pathname`.
 */

import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-app.js';
import { getFirestore } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { FIREBASE_CONFIG } from './fb-env.js';
import { mountAccountModal } from './account-modal.js';
// ⚠️ `identity.getIdToken(app)`, NEVER `user.getIdToken()`. getLiveUser()
// answers a flat SNAPSHOT with no such method, and the ebook reader shipped
// exactly that TypeError for every signed-in person (reader.js §5). Same rule.
import { getLiveUser, getIdToken, signInWithGoogle, handleRedirectResult } from './identity.js';
import { getAudioStatus } from './audio-request.js';
import {
  buildChapters, withDuration, chapterIndexAt, chapterFraction,
  timeAtChapterFraction, chapterTimes, previousChapterTarget, nextChapterTarget,
  msUntilChapterEnd, formatTime,
} from './audio-chapters.js';
import {
  SPEEDS, SKIP_INTERVALS, SLEEP_PRESETS_MIN,
  getBookRate, setBookRate, getSkipSec, setSkipSec,
} from './audio-prefs.js';
import * as ms from './media-session.js';
// ⚠️ THE AUTH SEAM LIVES IN ITS OWN MODULE and its header explains why at
// length: it is the one genuine unknown the feasibility study named, its
// failure mode is a silently dead play button, and a seam that cannot be
// tested without booting Firebase is a seam nobody tests. This file boots
// Firebase; that one does not.
import {
  audioFileUrl, ensureController, idbPutToken, probe, fallbackDetail,
} from './audio-seam.js';

/** Where the public chapter data lives (owner decision 4: it stays public). */
const CHAPTERS_URL = 'chapters.json';

/** How often the lock-screen scrubber is refreshed while playing. */
const POSITION_PUBLISH_MS = 1000;

/** The sleep timer's fade, in ms. Design §8 #8: "fade out over ~10 s". */
const SLEEP_FADE_MS = 10000;

const el = (id) => document.getElementById(id);

const state = {
  anchor: '',
  bookId: '',
  title: '',
  author: '',
  coverUrl: '',
  chapters: [],
  chapterIndex: -1,
  skipSec: 15,
  rate: 1,
  sleepAt: 0,          // epoch ms the timer fires, 0 = off
  sleepMode: 'off',    // 'off' | 'minutes' | 'chapter'
  sleepTimer: null,
  fadeTimer: null,
  positionTimer: null,
};

let app = null;
let db = null;
let audio = null;

/* ── refusals and faults, always in words ────────────────────────────────── */

/**
 * Close the page with a worded explanation.
 *
 * ⚠️ Three things, every time, per the estate's refusal rule: what happened,
 * what it needs, and how to get it. The four causes are kept apart because the
 * fixes differ — not signed in / no grant / an outage / a bad link — and
 * collapsing them sends people asking for access they already hold.
 */
function closed(title, why, opts = {}) {
  el('ls-shell').hidden = true;
  const gate = el('ls-gate');
  gate.hidden = false;
  el('ls-gate-title').textContent = title;
  el('ls-gate-why').textContent = why;
  el('ls-gate-signin').hidden = !opts.signIn;
  el('ls-gate-back').hidden = !opts.back;
}

/** A non-fatal problem, shown beside a player that is otherwise usable. */
function showError(message) {
  const box = el('ls-error');
  box.textContent = message;
  box.style.display = 'block';
}

function clearError() {
  const box = el('ls-error');
  box.textContent = '';
  box.style.display = 'none';
}

/* ── §1 — the service-worker seam (site/audio-seam.js owns it) ──────────── */

/**
 * Hand the worker a fresh token.
 *
 * ⚠️ The store is IndexedDB and the reason is in audio-seam.js's header: a
 * service worker is TERMINATED WHEN IDLE and restarts with no memory, so the
 * token has to survive in something the fetch handler can read. The
 * `postMessage` is only an optimisation for the instance running right now.
 */
async function pushToken() {
  const token = await getIdToken(app).catch(() => null);
  try {
    await idbPutToken(token);
  } catch { /* a refused IndexedDB degrades to "no auth", which the probe reports */ }
  try {
    navigator.serviceWorker?.controller?.postMessage({ type: 'SET_TOKEN', token: token || null });
  } catch { /* best effort */ }
  return token;
}

/* ── §2 — one seek, seven callers ────────────────────────────────────────── */

/**
 * ⚠️ THE ONLY WAY THE PLAYER MOVES. See header note 2.
 *
 * `reason` is carried for the same purpose it serves in `reading-position.js`:
 * it is what a future phase hangs behaviour off (position writes in phase 3,
 * the sleep timer's smart-resume in phase 5) without any caller having to
 * learn about it.
 */
function seekTo(seconds, reason) {
  if (!audio) return;
  const d = isFinite(audio.duration) && audio.duration > 0 ? audio.duration : null;
  let t = typeof seconds === 'number' && isFinite(seconds) ? seconds : 0;
  if (t < 0) t = 0;
  if (d !== null && t > d) t = d;
  try {
    audio.currentTime = t;
  } catch (e) {
    // Seeking before the element is seekable throws in some browsers.
    console.warn('[listen] seek refused', reason, e);
    return;
  }
  render();
}

/* ── §3 — rendering ──────────────────────────────────────────────────────── */

/** Re-apply the remembered rate. ⚠️ `src` changes reset it to 1.0 (§6). */
function applyRate() {
  if (!audio) return;
  try {
    audio.playbackRate = state.rate;
    // Baseline since Dec 2023 and true by default; set explicitly so a browser
    // that ever flips the default does not make 2× sound like a chipmunk.
    if ('preservesPitch' in audio) audio.preservesPitch = true;
  } catch { /* an unsupported rate is not worth taking the page down for */ }
}

function render() {
  if (!audio) return;
  const t = audio.currentTime || 0;
  const d = isFinite(audio.duration) && audio.duration > 0 ? audio.duration : null;

  // 🔴 THE CHAPTER-RELATIVE BAR.
  const bar = el('ls-chapter-bar');
  const frac = chapterFraction(state.chapters, t);
  if (frac === null) {
    // The last chapter before loadedmetadata: no domain, so no confident
    // position. Indeterminate, never a NaN and never a confident zero.
    bar.classList.add('ls-unknown');
    bar.value = '0';
  } else {
    bar.classList.remove('ls-unknown');
    if (!bar.dataset.dragging) bar.value = String(Math.round(frac * 1000));
  }

  const { index, elapsed, remaining, chapter } = chapterTimes(state.chapters, t);
  if (index !== state.chapterIndex) {
    state.chapterIndex = index;
    highlightChapter(index);
    publishMeta();
  }
  el('ls-chapter-title').textContent = chapter ? chapter.title : 'Playing';
  el('ls-chapter-where').textContent = state.chapters.length
    ? `· ${index + 1} of ${state.chapters.length}${chapter && chapter.partLabel ? ` · ${chapter.partLabel}` : ''}`
    : '';
  // ⚠️ CHAPTER times beside a CHAPTER bar. Book times live on their own line.
  el('ls-chapter-elapsed').textContent = state.chapters.length ? `${formatTime(elapsed)} in chapter` : formatTime(t);
  el('ls-chapter-remaining').textContent = remaining === null
    ? '--:-- left'
    : `${formatTime(remaining)} left`;

  el('ls-book-fill').style.width = d ? `${Math.min(100, (t / d) * 100)}%` : '0';
  el('ls-book-times').textContent = `${formatTime(t)} / ${d === null ? '--:--' : formatTime(d)}`;

  el('ls-prev-ch').disabled = previousChapterTarget(state.chapters, t) === null;
  el('ls-next-ch').disabled = nextChapterTarget(state.chapters, t) === null;

  renderSleepCountdown();
}

/** The chapter list, with `parts` as group headers (§8 #6). */
function renderChapterList() {
  const list = el('ls-chapter-list');
  list.innerHTML = '';
  el('ls-chapters-summary').textContent = state.chapters.length
    ? `Chapters (${state.chapters.length})`
    : 'Chapters';
  if (!state.chapters.length) {
    const li = document.createElement('li');
    li.className = 'ls-part-head';
    // ⚠️ Said out loud rather than rendered as an empty box. A book with no
    // chapter data still plays perfectly; the bar simply spans the whole book.
    li.textContent = 'No chapter list for this book — the bar covers the whole recording.';
    list.appendChild(li);
    return;
  }
  let lastPart = null;
  state.chapters.forEach((ch) => {
    if (ch.partLabel && ch.partLabel !== lastPart) {
      const head = document.createElement('li');
      head.className = 'ls-part-head';
      head.textContent = ch.partLabel;
      list.appendChild(head);
      lastPart = ch.partLabel;
    }
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.dataset.index = String(ch.index);
    const name = document.createElement('span');
    name.textContent = ch.title;
    const len = document.createElement('span');
    len.className = 'ls-ch-len';
    len.textContent = ch.endSec === null ? '' : formatTime(ch.endSec - ch.startSec);
    btn.append(name, len);
    btn.addEventListener('click', () => seekTo(ch.startSec, 'chapter-list'));
    li.appendChild(btn);
    list.appendChild(li);
  });
  highlightChapter(state.chapterIndex);
}

/**
 * Mark the current chapter and keep it in view.
 * ⚠️ §8 #6: a 255-chapter book needs the list scroll-anchored to the current
 * chapter, or opening it shows chapter 1 of a book you are eight hours into.
 */
function highlightChapter(index) {
  const list = el('ls-chapter-list');
  let current = null;
  list.querySelectorAll('button').forEach((b) => {
    const isNow = Number(b.dataset.index) === index;
    b.setAttribute('aria-current', isNow ? 'true' : 'false');
    if (isNow) current = b;
  });
  if (current && el('ls-chapters').open) {
    try { current.scrollIntoView({ block: 'nearest' }); } catch { /* older browsers */ }
  }
}

/* ── §4 — Media Session ──────────────────────────────────────────────────── */

function publishMeta() {
  const ch = state.chapters[state.chapterIndex];
  ms.publishMetadata({
    title: state.title,
    author: state.author,
    chapterTitle: ch ? ch.title : '',
    coverUrl: state.coverUrl,
  });
}

function publishPos() {
  if (!audio) return;
  ms.publishPosition(audio.duration, audio.currentTime, audio.playbackRate || 1);
}

function wireMediaSession() {
  const accepted = ms.wireHandlers({
    play: () => audio.play().catch(() => {}),
    pause: () => audio.pause(),
    // ⚠️ `details.seekOffset` is the OS's own requested jump and is HONOURED
    // when present (§4.4); our configured interval is the fallback.
    seekbackward: (d) => seekTo(audio.currentTime - ((d && d.seekOffset) || state.skipSec), 'ms-seekbackward'),
    seekforward: (d) => seekTo(audio.currentTime + ((d && d.seekOffset) || state.skipSec), 'ms-seekforward'),
    // ⚠️ Track buttons are CHAPTER buttons. That is what a listener expects
    // from a car stereo on an audiobook (§4.4).
    previoustrack: () => {
      const target = previousChapterTarget(state.chapters, audio.currentTime);
      if (target !== null) seekTo(target, 'ms-previoustrack');
    },
    nexttrack: () => {
      const target = nextChapterTarget(state.chapters, audio.currentTime);
      if (target !== null) seekTo(target, 'ms-nexttrack');
    },
    seekto: (d) => {
      if (d && typeof d.seekTime === 'number') seekTo(d.seekTime, 'ms-seekto');
    },
  }, state.skipSec);
  publishMeta();
  return accepted;
}

/* ── §5 — the sleep timer (§8 #8) ────────────────────────────────────────── */

function clearSleep() {
  if (state.sleepTimer) clearTimeout(state.sleepTimer);
  if (state.fadeTimer) clearInterval(state.fadeTimer);
  state.sleepTimer = null;
  state.fadeTimer = null;
  state.sleepAt = 0;
  state.sleepMode = 'off';
  if (audio) audio.volume = 1;
  el('ls-sleep-more').hidden = true;
  el('ls-sleep-left').textContent = '';
}

/**
 * Arm the timer.
 *
 * ⚠️ "End of chapter" is the option audiobook listeners actually use (§8 #8),
 * and it is the one that must be recomputed: the remaining audio divided by
 * the playback rate. `msUntilChapterEnd` owns that arithmetic and is tested.
 */
function armSleep(mode, minutes) {
  clearSleep();
  let ms_ = null;
  if (mode === 'minutes') ms_ = minutes * 60 * 1000;
  else if (mode === 'chapter') ms_ = msUntilChapterEnd(state.chapters, audio.currentTime, audio.playbackRate || 1);
  if (ms_ === null || !isFinite(ms_) || ms_ <= 0) {
    // ⚠️ Refused in words rather than armed at a nonsense time. "End of
    // chapter" in the last chapter of a book whose duration has not loaded has
    // no answer yet, and a timer that fires immediately reads as a bug.
    el('ls-sleep').value = 'off';
    showError('The sleep timer could not work out when this chapter ends yet. '
      + 'Give the book a moment to load, or choose a number of minutes instead.');
    return;
  }
  state.sleepMode = mode;
  state.sleepAt = Date.now() + ms_;
  state.sleepTimer = setTimeout(fadeAndPause, Math.max(0, ms_ - SLEEP_FADE_MS));
  el('ls-sleep-more').hidden = false;
  renderSleepCountdown();
}

function renderSleepCountdown() {
  if (!state.sleepAt) { el('ls-sleep-left').textContent = ''; return; }
  const left = Math.max(0, state.sleepAt - Date.now());
  el('ls-sleep-left').textContent = `sleeping in ${formatTime(left / 1000)}`;
}

/** Fade over ~10 s, then pause. A hard cut mid-sentence wakes people up. */
function fadeAndPause() {
  if (!audio) return;
  const steps = 20;
  const stepMs = SLEEP_FADE_MS / steps;
  let i = 0;
  state.fadeTimer = setInterval(() => {
    i += 1;
    audio.volume = Math.max(0, 1 - i / steps);
    if (i >= steps) {
      clearInterval(state.fadeTimer);
      state.fadeTimer = null;
      audio.pause();
      audio.volume = 1;
      clearSleep();
      el('ls-sleep').value = 'off';
    }
  }, stepMs);
}

/* ── §6 — controls ───────────────────────────────────────────────────────── */

function buildSelects() {
  const speed = el('ls-speed');
  speed.innerHTML = '';
  SPEEDS.forEach((s) => {
    const o = document.createElement('option');
    o.value = String(s);
    o.textContent = `${s}×`;
    speed.appendChild(o);
  });
  speed.value = String(state.rate);

  const skip = el('ls-skip');
  skip.innerHTML = '';
  SKIP_INTERVALS.forEach((s) => {
    const o = document.createElement('option');
    o.value = String(s);
    o.textContent = `${s}s`;
    skip.appendChild(o);
  });
  skip.value = String(state.skipSec);

  const sleep = el('ls-sleep');
  sleep.innerHTML = '';
  const off = document.createElement('option');
  off.value = 'off';
  off.textContent = 'Off';
  sleep.appendChild(off);
  SLEEP_PRESETS_MIN.forEach((m) => {
    const o = document.createElement('option');
    o.value = String(m);
    o.textContent = `${m} min`;
    sleep.appendChild(o);
  });
  const chap = document.createElement('option');
  chap.value = 'chapter';
  chap.textContent = 'End of chapter';
  sleep.appendChild(chap);
}

function labelSkipButtons() {
  el('ls-back').textContent = `−${state.skipSec}s`;
  el('ls-fwd').textContent = `+${state.skipSec}s`;
}

function wireControls() {
  el('ls-play').addEventListener('click', () => {
    if (audio.paused) audio.play().catch((e) => {
      // ⚠️ A play() rejection is the browser refusing (autoplay policy, a
      // decode failure). It has no HTTP status either, so it is worded.
      showError('The browser would not start playback. Press play again — if it keeps '
        + `refusing, reload the page and tell Mitch what it says: ${e && e.name ? e.name : 'unknown'}.`);
    });
    else audio.pause();
  });

  el('ls-back').addEventListener('click', () => seekTo(audio.currentTime - state.skipSec, 'skip-back'));
  el('ls-fwd').addEventListener('click', () => seekTo(audio.currentTime + state.skipSec, 'skip-forward'));

  el('ls-prev-ch').addEventListener('click', () => {
    const target = previousChapterTarget(state.chapters, audio.currentTime);
    if (target !== null) seekTo(target, 'chapter-prev');
  });
  el('ls-next-ch').addEventListener('click', () => {
    const target = nextChapterTarget(state.chapters, audio.currentTime);
    if (target !== null) seekTo(target, 'chapter-next');
  });

  // 🔴 The chapter bar. A drag maps back through the SAME chapter domain it
  // was drawn from — `timeAtChapterFraction` is `chapterFraction`'s inverse
  // and the pair is round-trip tested.
  const bar = el('ls-chapter-bar');
  bar.addEventListener('input', () => {
    bar.dataset.dragging = '1';
    const t = timeAtChapterFraction(state.chapters, state.chapterIndex, Number(bar.value) / 1000);
    if (t !== null) {
      const { elapsed, remaining } = chapterTimes(state.chapters, t);
      el('ls-chapter-elapsed').textContent = `${formatTime(elapsed)} in chapter`;
      el('ls-chapter-remaining').textContent = remaining === null ? '--:-- left' : `${formatTime(remaining)} left`;
    }
  });
  bar.addEventListener('change', () => {
    const t = timeAtChapterFraction(state.chapters, state.chapterIndex, Number(bar.value) / 1000);
    delete bar.dataset.dragging;
    if (t !== null) seekTo(t, 'chapter-bar');
  });

  el('ls-speed').addEventListener('change', () => {
    state.rate = parseFloat(el('ls-speed').value) || 1;
    applyRate();
    setBookRate(state.bookId, state.rate);
    // ⚠️ An armed "end of chapter" timer is now wrong: the same audio at a new
    // rate is a different amount of wall-clock time. Re-arm rather than let it
    // fire at the old moment.
    if (state.sleepMode === 'chapter') armSleep('chapter');
    publishPos();
  });

  el('ls-skip').addEventListener('change', () => {
    const n = parseInt(el('ls-skip').value, 10);
    if (setSkipSec(n)) {
      state.skipSec = n;
      labelSkipButtons();
      // The lock screen advertises the interval through the handler closure,
      // so it is re-wired to pick up the new one.
      wireMediaSession();
    }
  });

  el('ls-sleep').addEventListener('change', () => {
    const v = el('ls-sleep').value;
    if (v === 'off') clearSleep();
    else if (v === 'chapter') armSleep('chapter');
    else armSleep('minutes', parseInt(v, 10));
  });

  // ⚠️ "+5 minutes" is design §8 #8's "tap to add five minutes". People wake
  // up, want a little more, and should not have to re-choose a preset.
  el('ls-sleep-more').addEventListener('click', () => {
    if (!state.sleepAt) return;
    const left = state.sleepAt - Date.now() + 5 * 60 * 1000;
    clearTimeout(state.sleepTimer);
    if (state.fadeTimer) { clearInterval(state.fadeTimer); state.fadeTimer = null; audio.volume = 1; }
    state.sleepAt = Date.now() + left;
    state.sleepTimer = setTimeout(fadeAndPause, Math.max(0, left - SLEEP_FADE_MS));
    renderSleepCountdown();
  });

  // Keyboard, design §9.2 #4 — free, and this estate's readers are desktop-first.
  // ⚠️ Shift+arrow is CHAPTER, plain arrow is the skip interval; the two are
  // mutually exclusive branches so a shifted press never does both.
  document.addEventListener('keydown', (e) => {
    if (e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (e.key === ' ') {
      e.preventDefault();
      el('ls-play').click();
    } else if (e.key === 'ArrowLeft') {
      const t = e.shiftKey
        ? previousChapterTarget(state.chapters, audio.currentTime)
        : audio.currentTime - state.skipSec;
      if (t !== null) seekTo(t, e.shiftKey ? 'key-chapter-prev' : 'key-back');
    } else if (e.key === 'ArrowRight') {
      const t = e.shiftKey
        ? nextChapterTarget(state.chapters, audio.currentTime)
        : audio.currentTime + state.skipSec;
      if (t !== null) seekTo(t, e.shiftKey ? 'key-chapter-next' : 'key-forward');
    }
  });
}

/* ── §7 — opening a book ─────────────────────────────────────────────────── */

async function loadChapters(title) {
  try {
    const res = await fetch(CHAPTERS_URL);
    if (!res.ok) return [];
    return buildChapters(await res.json(), title);
  } catch {
    // ⚠️ NOT FATAL. A book with no chapter data still plays; the bar simply
    // spans the whole recording and the list says so.
    return [];
  }
}

async function openBook(anchor) {
  // The gated projection: five fields, and deliberately NOT `path`.
  const status = await getAudioStatus(app);
  if (!status.ok) {
    if (status.reason === 'signed_out') {
      closed('This player is for the household',
        'These are the household’s own audiobook files, so the player is not public. '
        + 'Sign in with Google and, if you have been given the book-files grant, the book will play.',
        { signIn: true, back: true });
    } else if (status.reason === 'no_grant') {
      closed('Your account cannot play the household’s book files',
        status.detail || fallbackDetail(403), { back: true });
    } else {
      closed('We could not check what is playable',
        status.detail
          ? `${status.detail} This is an outage, not a decision about your account.`
          : 'The audio service did not answer, so we could not look this book up. '
            + 'This is an outage on our side, not a decision about your account — try again shortly.',
        { back: true });
    }
    return;
  }

  // anchor -> bookId, from the Worker's own answer. ⚠️ Never recomputed here:
  // the anchor fold lives in the pipeline and one implementation is the rule.
  let bookId = '';
  for (const [id, a] of (status.anchors || new Map()).entries()) {
    if (a === anchor) { bookId = id; break; }
  }
  if (!bookId) {
    closed('That book is not streamable',
      'This link names a book that is not in the streaming set — either it was never '
      + 'requested, or it has been removed because nobody played it for a month. Open it in '
      + 'the catalogue and press request; it will be ready after the next library run.',
      { back: true });
    return;
  }

  state.anchor = anchor;
  state.bookId = bookId;
  // ⚠️ TITLE, AUTHOR AND COVER ARE DISPLAY-ONLY and may arrive as query
  // parameters from the catalogue modal, which already holds all three. The
  // AUTHORITY for "which book is this" is the anchor and the gated projection
  // above — never the query string. A wrong `t=` mislabels a card; it cannot
  // play a different book or reach a byte the grant does not allow.
  const params = new URLSearchParams(location.search);
  state.title = params.get('t') || bookId;
  state.author = params.get('a') || '';
  state.coverUrl = params.get('c') || '';

  state.rate = getBookRate(state.bookId);
  state.skipSec = getSkipSec();

  // The seam, proven BEFORE a play button exists. See header note 1.
  // ⚠️ The page's OWN pathname, so the /dev/ lane registers the /dev/ worker
  // at the /dev/ scope. See audio-seam.js's swPaths() for what a hard-coded
  // '/' would do to production.
  const controlled = await ensureController(navigator, location.pathname);
  const token = await pushToken();

  if (!controlled) {
    closed('The player could not get ready',
      'This player needs a small background helper to prove to the library that you are '
      + 'allowed to listen, and the browser did not start it. That usually means private '
      + 'browsing, or a browser setting that blocks site storage. Try a normal window and a '
      + 'reload; if it keeps happening, tell Mitch.',
      { back: true });
    return;
  }

  const answer = await probe(anchor, token);
  if (!answer.ok) {
    closed(answer.status === 401 ? 'Sign in to listen'
      : answer.status === 403 ? 'Your account cannot play the household’s book files'
        : answer.status === 0 ? 'We could not reach the audio service'
          : 'This book would not play',
    answer.detail, { signIn: answer.status === 401, back: true });
    return;
  }

  state.chapters = await loadChapters(state.title);

  el('ls-gate').hidden = true;
  el('ls-shell').hidden = false;
  el('ls-title').textContent = state.title;
  el('ls-author').textContent = state.author ? `· ${state.author}` : '';
  el('ls-note').textContent =
    'This player does not save your spot yet, and does not work offline — both are the next '
    + 'two pieces of work. Speed and skip settings are remembered on this device.';

  buildSelects();
  labelSkipButtons();
  renderChapterList();

  audio = document.createElement('audio');
  audio.preload = 'metadata';
  // ⚠️ NO `crossorigin` attribute. The service worker carries the credential
  // as a header; `crossorigin="anonymous"` would forbid credentials outright
  // and `use-credentials` belongs to the cookie fallback (design §3.3), which
  // is a different seam and not this one.
  audio.src = audioFileUrl(anchor);
  applyRate();

  audio.addEventListener('loadedmetadata', () => {
    // ⚠️ THE ONLY MOMENT THE LAST CHAPTER GETS AN END (§8).
    state.chapters = withDuration(state.chapters, audio.duration);
    renderChapterList();
    applyRate();          // header note 3: a load resets the rate
    render();
    publishPos();
  });

  audio.addEventListener('timeupdate', render);
  audio.addEventListener('seeked', render);
  audio.addEventListener('ratechange', publishPos);

  audio.addEventListener('play', () => {
    clearError();
    el('ls-play').innerHTML = '&#x23F8; Pause';
    ms.publishPlaybackState('playing');
    if (!state.positionTimer) state.positionTimer = setInterval(publishPos, POSITION_PUBLISH_MS);
  });

  audio.addEventListener('pause', () => {
    el('ls-play').innerHTML = '&#x25B6; Play';
    ms.publishPlaybackState('paused');
    if (state.positionTimer) { clearInterval(state.positionTimer); state.positionTimer = null; }
    // ⚠️ Design §8 #8: the sleep timer "must survive a chapter change but must
    // be cancelled on manual pause". `fadeAndPause` clears it itself, so a
    // timer still armed here is a person pressing pause — and an alarm that
    // outlives the listening it was set for is a book that stops tomorrow.
    if (state.sleepAt && !state.fadeTimer) {
      clearSleep();
      el('ls-sleep').value = 'off';
    }
  });

  audio.addEventListener('ended', () => {
    el('ls-play').innerHTML = '&#x25B6; Play';
    ms.publishPlaybackState('paused');
    if (state.positionTimer) { clearInterval(state.positionTimer); state.positionTimer = null; }
  });

  audio.addEventListener('error', () => {
    // ⚠️ THE BARE ERROR EVENT — header note 1. There is no HTTP status here,
    // only a MediaError code, so this must never guess at a permission
    // meaning. The probe above already proved access at open time; a failure
    // now is a transport or decode problem, and it is worded as one.
    const code = audio.error ? audio.error.code : 0;
    const words = code === 1 ? 'Playback was stopped before it started.'
      : code === 2 ? 'The connection dropped while streaming. Press play to pick it up again.'
        : code === 3 ? 'The audio could not be decoded — the file may be damaged. Tell Mitch which book this was.'
          : code === 4 ? 'This browser cannot play this audio format. Safari, Chrome, Edge and Firefox all can — if you are on one of those, tell Mitch.'
            : 'Playback stopped for a reason the browser did not name.';
    showError(`${words} Your access is fine — this is not a permission problem.`);
    ms.publishPlaybackState('paused');
  });

  wireControls();
  wireMediaSession();
  render();

  // ⚠️ Firebase ID tokens last an hour and a book is 13.7 hours on average
  // (MEASURED, design §1.3). Without this the stream 401s partway through the
  // second hour, mid-sentence, and every range after it fails.
  setInterval(() => { pushToken(); }, 45 * 60 * 1000);

  // ⚠️ `pagehide`, not `beforeunload` — the pattern reading-position.js
  // already uses, and the one iOS actually delivers.
  window.addEventListener('pagehide', () => {
    try { audio.pause(); } catch { /* leaving anyway */ }
    ms.teardown();
  });
}

/* ── boot ────────────────────────────────────────────────────────────────── */

function anchorFromLocation() {
  return new URLSearchParams(location.search).get('b') || '';
}

try {
  app = initializeApp(FIREBASE_CONFIG);
  db = getFirestore(app);
  mountAccountModal(db, app, el('identity-bar'));
} catch (e) {
  console.warn('[listen] identity failed to initialise:', e);
  closed('The player could not check your sign-in',
    'Sign-in did not load, so the player cannot tell who you are. This is a loading problem '
    + 'on our side, not a decision about you — try a refresh, and if it keeps happening tell Mitch.');
}

// A blocked or very slow gstatic must not leave "Checking your sign-in…" on
// screen for ever. The shelf's backstop, ported.
let resolved = false;
const backstop = setTimeout(() => {
  if (resolved) return;
  resolved = true;
  closed('The player could not check your sign-in',
    'Sign-in is taking too long to load. Try a refresh; if it keeps happening tell Mitch.');
}, 8000);

el('ls-gate-signin').addEventListener('click', async () => {
  const btn = el('ls-gate-signin');
  btn.disabled = true;
  try {
    await signInWithGoogle(app);
    await boot();
  } catch {
    el('ls-gate-why').textContent = 'Sign-in did not complete. Try again, or tell Mitch if it keeps failing.';
  }
  btn.disabled = false;
});

async function boot() {
  if (!app) return;
  const anchor = anchorFromLocation();
  const user = await getLiveUser(app).catch(() => null);
  resolved = true;
  clearTimeout(backstop);

  if (!user || !user.uid) {
    closed('This player is for the household',
      'These are the household’s own audiobook files, so the player is not public. Sign in '
      + 'with Google and, if you have been given the book-files grant, the book will play.',
      { signIn: true, back: true });
    return;
  }

  if (!anchor) {
    closed('No book was named',
      'This page plays one book at a time and this link did not say which. Open a book in the '
      + 'catalogue and press Listen.',
      { back: true });
    return;
  }

  await openBook(anchor);
}

handleRedirectResult(app).catch(() => {}).then(boot);
