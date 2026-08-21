// audio-player.js — the in-browser audiobook player
// ES module, browser-native (no build step)
// Audio Player Phase 2, 2026-08-19.
//
// Design: catalog-platform/docs/info/audio-player-design.md
//   §2.3 — thin custom UI over <audio> (recommended)
//   §3.2 — service-worker bearer injection + HEAD probe mitigation
//   §8   — player features: chapter-relative scrub, speed, skip
//   §10.1 — eviction access timestamps
//
// ⚠️ THIS MODULE REPLACES THE "Ready to stream — player coming" PLACEHOLDER
// that `renderAudioRow()` currently shows. It is loaded by the same page and
// integrates into the same modal lifecycle (fire-and-forget from the
// MutationObserver that drives renderAudioRow).
//
// Dependencies (same page, same import map):
//   - identity.js: getIdToken(app)
//   - audio-request.js: getAudioStatus(app), isStreamable(status, title)
//   - fb-env.js: col(), COLLECTION_SUFFIX
//   - reviews.js: bookIdFromTitle(title)

import { getIdToken } from './identity.js';
import { bookIdFromTitle } from './reviews.js';

// ═══════════════════════════════════════════════════════════════════════════════
// §1 — Constants
// ═══════════════════════════════════════════════════════════════════════════════

const AUDIO_API_BASE = 'https://audiobook-api.heygabi.ai';
const AUDIO_FILE_PATH = '/api/audio/{anchor}/file';
const AUDIO_STREAM_PING_PATH = '/api/audio/{anchor}/stream-ping';

/** How often we report access to the eviction timestamp endpoint. */
const STREAM_PING_INTERVAL_MS = 10 * 60 * 1000;

/** Playback speeds offered. */
const SPEEDS = [0.5, 1, 1.25, 1.5, 2];

/** IndexedDB constants matching audio-sw.js */
const DB_NAME = 'audio-auth';
const DB_VERSION = 1;
const STORE_NAME = 'tokens';
const TOKEN_KEY = 'firebase-id-token';

// ═══════════════════════════════════════════════════════════════════════════════
// §2 — Service Worker Registration & Token Sync
// ═══════════════════════════════════════════════════════════════════════════════

let _swRegistered = false;
let _swControllerReady = false;

/**
 * Register the audio service worker and wait for it to control this page.
 * Returns true if the controller is ready, false if it could not be set up.
 */
export async function ensureServiceWorker() {
  if (_swControllerReady) return true;
  if (!('serviceWorker' in navigator)) return false;

  try {
    if (!_swRegistered) {
      // The sw file is at the site root so its scope covers the whole origin.
      await navigator.serviceWorker.register('/audio-sw.js', { scope: '/' });
      _swRegistered = true;
    }

    // Wait for the controller to be non-null (may be immediate if already
    // activated from a prior visit).
    if (navigator.serviceWorker.controller) {
      _swControllerReady = true;
      return true;
    }

    // First load — the worker is installing/activating. Wait up to 3 seconds.
    await new Promise((resolve) => {
      const check = () => {
        if (navigator.serviceWorker.controller) {
          _swControllerReady = true;
          resolve(true);
        }
      };
      navigator.serviceWorker.addEventListener('controllerchange', check);
      setTimeout(() => resolve(false), 3000);
    });

    return _swControllerReady;
  } catch (e) {
    console.warn('[audio-player] SW registration failed:', e);
    return false;
  }
}

/**
 * Write the current Firebase ID token to IndexedDB so the service worker
 * can read it, AND post it to the worker via message for immediate use.
 */
export async function syncToken(app) {
  const token = await getIdToken(app);
  // Write to IndexedDB (survives worker termination)
  await writeTokenToIDB(token);
  // Also post to the active worker for immediate availability
  if (navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({
      type: 'SET_TOKEN',
      token: token || null,
    });
  }
  return token;
}

function writeTokenToIDB(token) {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME);
      }
    };
    req.onsuccess = () => {
      const db = req.result;
      const tx = db.transaction(STORE_NAME, 'readwrite');
      const store = tx.objectStore(STORE_NAME);
      if (token) {
        store.put(token, TOKEN_KEY);
      } else {
        store.delete(TOKEN_KEY);
      }
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    };
    req.onerror = () => reject(req.error);
  });
}

// ═══════════════════════════════════════════════════════════════════════════════
// §3 — HEAD Probe (design §3.2 item 5 mitigation)
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * Probe the audio byte route with the caller's own bearer token.
 * This is the MANDATORY mitigation for the "no controlling worker" failure:
 * <audio> reports a 401 as a bare error event with no status, so the page
 * must ask the question itself and read the worded answer.
 *
 * @param {string} anchor
 * @param {string|null} token
 * @returns {Promise<{ok: boolean, status: number, detail?: string}>}
 */
export async function probeAudioAccess(anchor, token) {
  const url = `${AUDIO_API_BASE}${AUDIO_FILE_PATH.replace('{anchor}', anchor)}`;
  try {
    const headers = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(url, { method: 'HEAD', headers, mode: 'cors' });
    if (res.ok || res.status === 206) {
      return { ok: true, status: res.status };
    }
    // Try to read a worded detail from the response (JSON body on errors).
    // HEAD has no body, so we just report the status.
    return { ok: false, status: res.status, detail: statusToMessage(res.status) };
  } catch (e) {
    return { ok: false, status: 0, detail: 'Network error — check your connection.' };
  }
}

function statusToMessage(status) {
  switch (status) {
    case 401: return 'Sign in to listen to this audiobook.';
    case 403: return 'Your account does not have listening access — ask the library owner.';
    case 404: return 'This audiobook is not available for streaming right now.';
    case 429: return 'Too many requests — wait a moment and try again.';
    default: return `The audio service returned an error (${status}).`;
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// §4 — Chapters
// ═══════════════════════════════════════════════════════════════════════════════

let _chaptersData = null;
let _chaptersLoaded = false;

/**
 * Load chapters.json (published, public, in the site root).
 * Cached for the page lifetime.
 */
async function loadChapters() {
  if (_chaptersLoaded) return _chaptersData;
  try {
    const res = await fetch('/chapters.json');
    if (res.ok) {
      _chaptersData = await res.json();
    }
  } catch { /* not fatal — player works without chapters */ }
  _chaptersLoaded = true;
  return _chaptersData;
}

/**
 * Get chapters for a book title.
 * Returns an array of {title, startSec} sorted by start time.
 * Falls back to start_min * 60 if start_sec is not present.
 */
export function getBookChapters(chaptersJson, bookTitle) {
  if (!chaptersJson || !bookTitle) return [];
  const entry = chaptersJson[bookTitle];
  if (!entry || !Array.isArray(entry.chapters)) return [];
  return entry.chapters.map((ch) => ({
    title: ch.title || 'Untitled',
    startSec: typeof ch.start_sec === 'number' ? ch.start_sec : (ch.start_min || 0) * 60,
  }));
}

/**
 * Find the current chapter index for a given time.
 */
function currentChapterIndex(chapters, timeSec) {
  if (!chapters.length) return -1;
  for (let i = chapters.length - 1; i >= 0; i--) {
    if (timeSec >= chapters[i].startSec) return i;
  }
  return 0;
}

// ═══════════════════════════════════════════════════════════════════════════════
// §5 — Eviction Access Timestamp (design §10.1)
// ═══════════════════════════════════════════════════════════════════════════════

let _lastPingAt = 0;
let _pingInterval = null;

/**
 * Report a stream access to the eviction timestamp endpoint.
 * Throttled: at most once per 10 minutes per anchor.
 */
async function sendStreamPing(anchor, token) {
  if (!token || !anchor) return;
  const url = `${AUDIO_API_BASE}${AUDIO_STREAM_PING_PATH.replace('{anchor}', anchor)}`;
  try {
    await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ts: Date.now() }),
      mode: 'cors',
    });
  } catch { /* best effort — a missed ping just means the evictor has an older stamp */ }
}

function startStreamPings(anchor, app) {
  stopStreamPings();
  // Immediate first ping on playback start
  const doPing = async () => {
    const now = Date.now();
    if (now - _lastPingAt < STREAM_PING_INTERVAL_MS) return;
    _lastPingAt = now;
    const token = await getIdToken(app);
    sendStreamPing(anchor, token);
  };
  doPing();
  _pingInterval = setInterval(doPing, STREAM_PING_INTERVAL_MS);
}

function stopStreamPings() {
  if (_pingInterval) {
    clearInterval(_pingInterval);
    _pingInterval = null;
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// §6 — Player UI
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * Render the audio player into the given container.
 *
 * @param {HTMLElement} container - the DOM element to render into
 * @param {object} opts
 * @param {string} opts.anchor - the audiobook anchor
 * @param {string} opts.bookTitle - the book title for chapter lookup
 * @param {object} opts.app - the Firebase app instance
 * @returns {Promise<void>}
 */
export async function renderAudioPlayer(container, { anchor, bookTitle, app }) {
  container.innerHTML = '';

  // ── Step 1: Register the service worker ────────────────────────────────
  const swReady = await ensureServiceWorker();

  // ── Step 2: Sync token to IndexedDB / service worker ───────────────────
  const token = await syncToken(app);

  // ── Step 3: HEAD probe — the mandatory "sign in to listen" check ───────
  // ⚠️ Design §3.2 item 5: if no SW controller, or if the probe fails,
  // show a worded message rather than a silently dead play button.
  if (!swReady) {
    // Degrade gracefully: try the probe anyway. If 401, show sign-in message.
    const probe = await probeAudioAccess(anchor, token);
    if (!probe.ok) {
      renderAuthMessage(container, probe.detail || 'Sign in to listen.');
      return;
    }
    // If probe succeeded but no SW, the audio element will fail because it
    // can't carry the bearer. Show a "getting ready" message.
    renderAuthMessage(container,
      'The audio player is setting up — reload the page in a moment.');
    return;
  }

  const probe = await probeAudioAccess(anchor, token);
  if (!probe.ok) {
    renderAuthMessage(container, probe.detail || 'Sign in to listen to this audiobook.');
    return;
  }

  // ── Step 4: Load chapters ──────────────────────────────────────────────
  const chaptersJson = await loadChapters();
  const chapters = getBookChapters(chaptersJson, bookTitle);

  // ── Step 5: Build the player DOM ───────────────────────────────────────
  const audioUrl = `${AUDIO_API_BASE}${AUDIO_FILE_PATH.replace('{anchor}', anchor)}`;
  buildPlayerUI(container, { audioUrl, chapters, anchor, bookTitle, app });
}

function renderAuthMessage(container, message) {
  const div = document.createElement('div');
  div.className = 'audio-player-auth';
  div.style.cssText = 'padding:12px;color:var(--muted,#999);font-size:.9em;text-align:center;';
  div.textContent = message;
  container.appendChild(div);
}

/**
 * Build the full player UI.
 */
function buildPlayerUI(container, { audioUrl, chapters, anchor, bookTitle, app }) {
  // Wrapper
  const wrapper = document.createElement('div');
  wrapper.className = 'audio-player';
  wrapper.style.cssText = `
    padding: 12px; background: var(--bg-2, #1a1a2e); border: 1px solid var(--border, #333);
    border-radius: 8px; font-size: .9em;
  `.trim();

  // Audio element — the browser's native decoder does the work.
  const audio = document.createElement('audio');
  audio.preload = 'metadata';
  audio.src = audioUrl;
  // ⚠️ DO NOT set crossorigin="anonymous" — the service worker handles auth,
  // and anonymous would suppress cookies if we ever fall back to option B.

  // ── Controls row ───────────────────────────────────────────────────────
  const controls = document.createElement('div');
  controls.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;';

  // Play/Pause
  const playBtn = document.createElement('button');
  playBtn.className = 'audio-play-btn';
  playBtn.textContent = '▶';
  playBtn.title = 'Play';
  playBtn.style.cssText = btnStyle();
  playBtn.addEventListener('click', () => {
    if (audio.paused) {
      audio.play();
    } else {
      audio.pause();
    }
  });

  // Skip back 15s
  const skipBack = document.createElement('button');
  skipBack.textContent = '−15s';
  skipBack.title = 'Back 15 seconds';
  skipBack.style.cssText = btnStyle('small');
  skipBack.addEventListener('click', () => {
    audio.currentTime = Math.max(0, audio.currentTime - 15);
  });

  // Skip forward 15s
  const skipFwd = document.createElement('button');
  skipFwd.textContent = '+15s';
  skipFwd.title = 'Forward 15 seconds';
  skipFwd.style.cssText = btnStyle('small');
  skipFwd.addEventListener('click', () => {
    audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 15);
  });

  // Time display
  const timeDisplay = document.createElement('span');
  timeDisplay.className = 'audio-time';
  timeDisplay.style.cssText = 'font-family:monospace;font-size:.85em;color:var(--muted,#aaa);min-width:110px;';
  timeDisplay.textContent = '0:00 / 0:00';

  // Speed selector
  const speedSelect = document.createElement('select');
  speedSelect.title = 'Playback speed';
  speedSelect.style.cssText = 'background:var(--bg,#111);color:var(--text,#eee);border:1px solid var(--border,#555);border-radius:4px;padding:2px 4px;font-size:.85em;';
  SPEEDS.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = String(s);
    opt.textContent = `${s}x`;
    if (s === 1) opt.selected = true;
    speedSelect.appendChild(opt);
  });
  speedSelect.addEventListener('change', () => {
    audio.playbackRate = parseFloat(speedSelect.value);
  });

  controls.appendChild(playBtn);
  controls.appendChild(skipBack);
  controls.appendChild(skipFwd);
  controls.appendChild(timeDisplay);
  controls.appendChild(speedSelect);

  // ── Progress bar (seekable) ────────────────────────────────────────────
  const progressRow = document.createElement('div');
  progressRow.style.cssText = 'margin-bottom:8px;';

  const progressBar = document.createElement('input');
  progressBar.type = 'range';
  progressBar.min = '0';
  progressBar.max = '1000';
  progressBar.value = '0';
  progressBar.style.cssText = 'width:100%;cursor:pointer;accent-color:var(--neon-cyan,#0cc);';
  progressBar.title = 'Seek';

  let isSeeking = false;
  progressBar.addEventListener('input', () => {
    isSeeking = true;
    if (audio.duration) {
      const pct = parseInt(progressBar.value, 10) / 1000;
      timeDisplay.textContent = `${formatTime(pct * audio.duration)} / ${formatTime(audio.duration)}`;
    }
  });
  progressBar.addEventListener('change', () => {
    if (audio.duration) {
      audio.currentTime = (parseInt(progressBar.value, 10) / 1000) * audio.duration;
    }
    isSeeking = false;
  });

  progressRow.appendChild(progressBar);

  // ── Chapter info / navigation ──────────────────────────────────────────
  let chapterRow = null;
  let chapterSelect = null;

  if (chapters.length > 1) {
    chapterRow = document.createElement('div');
    chapterRow.style.cssText = 'display:flex;align-items:center;gap:6px;margin-bottom:4px;flex-wrap:wrap;';

    const prevChBtn = document.createElement('button');
    prevChBtn.textContent = '⏮';
    prevChBtn.title = 'Previous chapter';
    prevChBtn.style.cssText = btnStyle('small');
    prevChBtn.addEventListener('click', () => {
      const idx = currentChapterIndex(chapters, audio.currentTime);
      if (idx > 0) audio.currentTime = chapters[idx - 1].startSec;
    });

    const nextChBtn = document.createElement('button');
    nextChBtn.textContent = '⏭';
    nextChBtn.title = 'Next chapter';
    nextChBtn.style.cssText = btnStyle('small');
    nextChBtn.addEventListener('click', () => {
      const idx = currentChapterIndex(chapters, audio.currentTime);
      if (idx < chapters.length - 1) audio.currentTime = chapters[idx + 1].startSec;
    });

    chapterSelect = document.createElement('select');
    chapterSelect.title = 'Jump to chapter';
    chapterSelect.style.cssText = 'flex:1;min-width:120px;max-width:300px;background:var(--bg,#111);color:var(--text,#eee);border:1px solid var(--border,#555);border-radius:4px;padding:2px 4px;font-size:.85em;overflow:hidden;text-overflow:ellipsis;';
    chapters.forEach((ch, i) => {
      const opt = document.createElement('option');
      opt.value = String(i);
      opt.textContent = ch.title;
      chapterSelect.appendChild(opt);
    });
    chapterSelect.addEventListener('change', () => {
      const idx = parseInt(chapterSelect.value, 10);
      if (chapters[idx]) audio.currentTime = chapters[idx].startSec;
    });

    chapterRow.appendChild(prevChBtn);
    chapterRow.appendChild(nextChBtn);
    chapterRow.appendChild(chapterSelect);
  }

  // ── Error display ──────────────────────────────────────────────────────
  const errorDiv = document.createElement('div');
  errorDiv.className = 'audio-player-error';
  errorDiv.style.cssText = 'display:none;color:var(--neon-magenta,#f0a);font-size:.85em;padding:4px 0;';

  // ── Assemble ───────────────────────────────────────────────────────────
  wrapper.appendChild(controls);
  wrapper.appendChild(progressRow);
  if (chapterRow) wrapper.appendChild(chapterRow);
  wrapper.appendChild(errorDiv);
  wrapper.appendChild(audio);
  container.appendChild(wrapper);

  // ── Audio event wiring ─────────────────────────────────────────────────

  audio.addEventListener('play', () => {
    playBtn.textContent = '⏸';
    playBtn.title = 'Pause';
    startStreamPings(anchor, app);
  });

  audio.addEventListener('pause', () => {
    playBtn.textContent = '▶';
    playBtn.title = 'Play';
    stopStreamPings();
  });

  audio.addEventListener('ended', () => {
    playBtn.textContent = '▶';
    playBtn.title = 'Play';
    stopStreamPings();
  });

  audio.addEventListener('timeupdate', () => {
    if (isSeeking) return;
    if (audio.duration) {
      progressBar.value = String(Math.round((audio.currentTime / audio.duration) * 1000));
      timeDisplay.textContent = `${formatTime(audio.currentTime)} / ${formatTime(audio.duration)}`;
    }
    // Update chapter select
    if (chapterSelect && chapters.length > 1) {
      const idx = currentChapterIndex(chapters, audio.currentTime);
      if (idx >= 0 && chapterSelect.selectedIndex !== idx) {
        chapterSelect.selectedIndex = idx;
      }
    }
  });

  audio.addEventListener('loadedmetadata', () => {
    timeDisplay.textContent = `0:00 / ${formatTime(audio.duration)}`;
  });

  audio.addEventListener('error', () => {
    const code = audio.error ? audio.error.code : 0;
    let msg = 'Playback error.';
    // MediaError codes: 1=ABORTED, 2=NETWORK, 3=DECODE, 4=SRC_NOT_SUPPORTED
    if (code === 2) msg = 'Network error — check your connection and try again.';
    if (code === 4) msg = 'This audio format is not supported by your browser.';
    errorDiv.textContent = msg;
    errorDiv.style.display = 'block';
    stopStreamPings();
  });

  // ── Token refresh ──────────────────────────────────────────────────────
  // Firebase ID tokens expire in 1 hour. Refresh every 55 minutes so the
  // service worker always has a fresh one during a long listen.
  const tokenRefreshInterval = setInterval(async () => {
    await syncToken(app);
  }, 55 * 60 * 1000);

  // Clean up on audio element removal (modal close)
  const cleanupObserver = new MutationObserver(() => {
    if (!document.body.contains(wrapper)) {
      clearInterval(tokenRefreshInterval);
      stopStreamPings();
      cleanupObserver.disconnect();
    }
  });
  cleanupObserver.observe(document.body, { childList: true, subtree: true });
}

// ═══════════════════════════════════════════════════════════════════════════════
// §7 — Helpers
// ═══════════════════════════════════════════════════════════════════════════════

function formatTime(seconds) {
  if (!seconds || !isFinite(seconds)) return '0:00';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) {
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }
  return `${m}:${String(s).padStart(2, '0')}`;
}

function btnStyle(size) {
  const base = 'border:1px solid var(--border,#555);background:var(--bg,#111);color:var(--text,#eee);border-radius:4px;cursor:pointer;';
  if (size === 'small') return base + 'padding:4px 6px;font-size:.8em;';
  return base + 'padding:6px 12px;font-size:1.1em;min-width:36px;';
}
