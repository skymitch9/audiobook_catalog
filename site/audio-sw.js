// audio-sw.js — the auth seam service worker for audiobook streaming
// Audio Player Phase 2, 2026-08-19.
//
// Design: catalog-platform/docs/info/audio-player-design.md §3.2 — service-
// worker bearer injection. The `<audio src>` element issues its own range
// requests and CANNOT carry an Authorization header, so this worker intercepts
// them and adds one.
//
// ⚠️ THE FIVE THINGS THAT WILL GO WRONG (design §3.2 items 1–5):
//
// 1. The Range header must be re-applied by hand — constructing a new Request
//    historically dropped it. We read it explicitly and set it explicitly.
// 2. The 206 must be returned VERBATIM — WebKit rejects a 200 answering a
//    range request, and re-wrapping the body or stripping Content-Range would
//    silently break Safari. We pass the response through unchanged.
// 3. The token lives in IndexedDB, NOT localStorage (service workers have no
//    localStorage). The worker is terminated when idle and restarts with NO
//    memory, so the IndexedDB read is on the request path every time.
// 4. Cross-origin + Authorization forces a CORS preflight — the Worker
//    already allows it (SITE_ORIGINS + allowHeaders includes Authorization).
// 5. THE FAILURE MODE IS A SILENT DEAD BUTTON — mitigated by the page-level
//    HEAD probe (audio-player.js) which checks auth BEFORE setting src.

const DB_NAME = 'audio-auth';
const DB_VERSION = 1;
const STORE_NAME = 'tokens';
const TOKEN_KEY = 'firebase-id-token';

// The pattern that identifies audio byte requests.
// Matches: /api/audio/<anchor>/file
const AUDIO_PATH_RE = /^\/api\/audio\/[^/]+\/file$/;

/**
 * Open (or create) the IndexedDB store used to pass the token from the page.
 * @returns {Promise<IDBDatabase>}
 */
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

/**
 * Read the current Firebase ID token from IndexedDB.
 * Returns null if absent or if the read fails (degraded: no auth).
 * @returns {Promise<string|null>}
 */
async function getToken() {
  try {
    const db = await openDB();
    return new Promise((resolve) => {
      const tx = db.transaction(STORE_NAME, 'readonly');
      const store = tx.objectStore(STORE_NAME);
      const req = store.get(TOKEN_KEY);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => resolve(null);
    });
  } catch {
    return null;
  }
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────

self.addEventListener('install', (event) => {
  // Activate immediately — don't wait for existing tabs to close.
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  // Claim all open clients so the worker starts intercepting immediately
  // after registration, without requiring a page reload.
  event.waitUntil(self.clients.claim());
});

// ─── Token updates via message ───────────────────────────────────────────────

// The page posts the fresh token here on sign-in and on refresh (every ~55 min
// before Firebase's 1-hour expiry). The worker writes it to IndexedDB so it
// survives termination.
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SET_TOKEN') {
    const token = event.data.token;
    openDB().then((db) => {
      const tx = db.transaction(STORE_NAME, 'readwrite');
      const store = tx.objectStore(STORE_NAME);
      if (token) {
        store.put(token, TOKEN_KEY);
      } else {
        store.delete(TOKEN_KEY);
      }
    }).catch(() => {/* best effort */});
  }
});

// ─── Fetch interception ──────────────────────────────────────────────────────

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Only intercept audio byte requests — leave everything else alone.
  if (!AUDIO_PATH_RE.test(url.pathname)) return;

  event.respondWith(handleAudioFetch(event.request));
});

/**
 * Intercept an audio byte request and inject the bearer token.
 *
 * ⚠️ Design §3.2 item 1: the Range header is read EXPLICITLY and set
 * EXPLICITLY on the outgoing request. Relying on it surviving a Request
 * clone is the bug Chrome fixed in v87 and Safari fixed later.
 *
 * ⚠️ Design §3.2 item 2: the response is returned VERBATIM — status, headers,
 * body. Never rewrite the status, never strip Content-Range, never re-wrap
 * the body. WebKit's media loader rejects a 200 answering a range request.
 *
 * @param {Request} original
 * @returns {Promise<Response>}
 */
async function handleAudioFetch(original) {
  const token = await getToken();

  // No token — pass through unmodified. The Worker will 401 and the page's
  // HEAD probe has already warned the user (design §3.2 item 5 mitigation).
  if (!token) {
    return fetch(original);
  }

  // Build a new headers map with Authorization added.
  const headers = new Headers(original.headers);
  headers.set('Authorization', `Bearer ${token}`);

  // ⚠️ Explicitly preserve the Range header (item 1).
  const range = original.headers.get('Range');
  if (range) {
    headers.set('Range', range);
  }

  const authed = new Request(original.url, {
    method: original.method,
    headers,
    // ⚠️ mode/credentials/cache must match the original so CORS behaves.
    mode: 'cors',
    credentials: 'omit',
    cache: original.cache,
    redirect: original.redirect,
    referrer: original.referrer,
    referrerPolicy: original.referrerPolicy,
  });

  // ⚠️ Return the response verbatim (item 2). Never rewrite.
  return fetch(authed);
}
