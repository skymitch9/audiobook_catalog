/**
 * audio-seam.js — the AUTH SEAM, and nothing else.
 *
 * AUDIO PLAYER PHASE 2, 2026-09-02.
 * Design: catalog-platform/docs/info/audio-player-design.md §3 — "the hard
 * problem", and the only genuine unknown the feasibility study named.
 *
 * ## 🔴 WHY THIS IS A MODULE OF ITS OWN
 *
 * `<audio src="…">` makes the BROWSER — not the page — issue the HTTP
 * requests, and it will issue many: Safari opens with `Range: bytes=0-1`, then
 * a metadata fetch, then rolling ranges for hours. **The page cannot attach an
 * `Authorization` header to any of them.** There is no API for it; it is by
 * design in HTML (§3.1). So a service worker answers them instead.
 *
 * That seam has ONE failure mode and it is the worst kind: **a silently dead
 * play button.** No controlling worker ⇒ the request goes out bare ⇒ the
 * Worker answers a correct, worded 401 ⇒ the media element reports it to the
 * page as a bare `error` event **with no status code**. The estate's standing
 * rule is that a person must never see a bare HTTP status, and a control that
 * does nothing at all is worse than one.
 *
 * Everything in this file exists to make that failure IMPOSSIBLE TO SHIP
 * SILENTLY, which is why it is separated from the player's UI: `listen.js` has
 * top-level side effects (it boots Firebase), and a seam that cannot be tested
 * without booting Firebase is a seam nobody tests. Every function here is
 * pure, or takes its `fetch`/`navigator` by injection.
 *
 * ## ⚠️ The two constants that are duplicated ON PURPOSE
 *
 * The IndexedDB name/store/key are the wire format between this file and
 * `site/audio-sw.js`, which **cannot import from it** — a service worker has
 * its own module graph and its own global scope. So they are written twice,
 * and `tests/test_listen_page.py` reads BOTH FILES and fails if they drift.
 * That is the only honest way to keep two copies of a wire format in step.
 */

/** The one host audio bytes come from. ⚠️ Also hard-coded in audio-sw.js. */
export const AUDIO_API_ORIGIN = 'https://audiobook-api.heygabi.ai';

export const audioFileUrl = (anchor) => `${AUDIO_API_ORIGIN}/api/audio/${encodeURIComponent(anchor)}/file`;

/** ⚠️ MUST MATCH site/audio-sw.js. See the header. */
export const DB_NAME = 'audio-auth';
export const DB_VERSION = 1;
export const STORE_NAME = 'tokens';
export const TOKEN_KEY = 'firebase-id-token';

/** How long to wait for a newly registered worker to take control. */
export const SW_CONTROL_TIMEOUT_MS = 5000;

/**
 * The worker's script URL and scope, derived from THIS PAGE's directory.
 *
 * 🔴 THE BUG THIS PREVENTS IS INVISIBLE ON ONE LANE AND REAL ON THE OTHER.
 * `/dev/` is a PATH on audiobooks.heygabi.ai, not a host. Registering
 * `/audio-sw.js` at scope `/` from `/dev/listen` would install the PROMOTED
 * copy of the worker and give it control of the PROMOTED site — a dev-lane
 * page silently changing production behaviour for every visitor, with no
 * error anywhere. Deriving both from the page's own directory gives each lane
 * its own worker and its own scope.
 *
 * @param {string} pathname `location.pathname`
 * @returns {{script: string, scope: string}}
 */
export function swPaths(pathname) {
  const p = typeof pathname === 'string' && pathname ? pathname : '/';
  const dir = p.slice(0, p.lastIndexOf('/') + 1) || '/';
  return { script: `${dir}audio-sw.js`, scope: dir };
}

/**
 * Register the bearer injector and wait for it to CONTROL this page.
 *
 * ⚠️ REGISTRATION IS NOT CONTROL, and conflating the two is how the dead play
 * button ships. A newly installed worker does not control the page that
 * installed it until it has activated AND claimed the client. `audio-sw.js`
 * calls `skipWaiting()` and `clients.claim()` so this is usually immediate —
 * but "usually" is not a thing to hang a play button on, so this waits for
 * `controllerchange` and answers honestly when it never comes.
 *
 * @returns {Promise<boolean>} whether a controller is now in place
 */
export async function ensureController(nav, pathname, timeoutMs = SW_CONTROL_TIMEOUT_MS) {
  const n = nav || (typeof navigator !== 'undefined' ? navigator : null);
  if (!n || !n.serviceWorker) return false;
  const { script, scope } = swPaths(pathname);
  try {
    await n.serviceWorker.register(script, { scope });
  } catch (e) {
    console.warn('[audio-seam] service worker registration failed:', e);
    return false;
  }
  if (n.serviceWorker.controller) return true;
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    try {
      n.serviceWorker.addEventListener('controllerchange', () => done(true));
    } catch { /* an environment without listeners falls through to the timeout */ }
    setTimeout(() => done(!!n.serviceWorker.controller), timeoutMs);
  });
}

/**
 * Put a token where the worker can find it.
 *
 * ⚠️ INDEXEDDB, NOT `localStorage` AND NOT A MESSAGE (§3.2 item 3). A service
 * worker has no `localStorage`, and — the half that actually bites — **it is
 * TERMINATED WHEN IDLE**: during a paused book, or between buffering bursts,
 * it is killed and restarted on the next fetch event with NO MEMORY. Anything
 * cached in a module variable is gone by the next range request, so the read
 * has to be on the request path, out of a store that survives. The
 * `postMessage` beside it is only an optimisation for the instance running now.
 */
export function idbPutToken(token, idbFactory) {
  const factory = idbFactory || (typeof indexedDB !== 'undefined' ? indexedDB : null);
  if (!factory) return Promise.resolve(false);
  return new Promise((resolve, reject) => {
    const req = factory.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const idb = req.result;
      if (!idb.objectStoreNames.contains(STORE_NAME)) idb.createObjectStore(STORE_NAME);
    };
    req.onsuccess = () => {
      try {
        const idb = req.result;
        const tx = idb.transaction(STORE_NAME, 'readwrite');
        const store = tx.objectStore(STORE_NAME);
        if (token) store.put(token, TOKEN_KEY); else store.delete(TOKEN_KEY);
        tx.oncomplete = () => resolve(true);
        tx.onerror = () => reject(tx.error);
      } catch (e) {
        reject(e);
      }
    };
    req.onerror = () => reject(req.error);
  });
}

/**
 * 🔴 THE MANDATORY HEAD PROBE — design §3.2 item 5, "not advisory".
 *
 * Ask the byte route the question whose answer the `<audio>` element cannot
 * report, using a request the PAGE makes and can therefore read. It does two
 * jobs at once: it renders a refusal IN WORDS, and it proves the seam works
 * before the media element is ever asked to use it.
 *
 * ⚠️ A HEAD CARRIES NO BODY, so the Worker's worded `detail` is not in it.
 * When the HEAD refuses, this follows up with a ONE-BYTE ranged GET to read
 * that sentence — the sentence that names the grant an approver actually
 * toggles. The follow-up costs one request and happens only on the failure
 * path. ⚠️ It is a RANGED get on purpose: `range.ts` answers a malformed or
 * absent Range with a 200, and a 200 here would be a 601 MB download to read
 * an error message.
 *
 * @param {string} anchor
 * @param {string|null} token
 * @param {Function} [fetchImpl] injected for tests
 * @returns {Promise<{ok: boolean, status: number, detail: string}>}
 */
export async function probe(anchor, token, fetchImpl) {
  const doFetch = fetchImpl || (typeof fetch !== 'undefined' ? fetch : null);
  if (!doFetch) return { ok: false, status: 0, detail: fallbackDetail(0) };
  const url = audioFileUrl(anchor);
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  let res;
  try {
    res = await doFetch(url, { method: 'HEAD', headers, mode: 'cors' });
  } catch {
    // ⚠️ AN OUTAGE IS NOT A REFUSAL. A CORS failure, dead DNS and an offline
    // laptop all land here and none of them is a fact about this person's
    // access. Saying "you do not have access" here is exactly the
    // mislabelling that sends people asking for access they already hold.
    return { ok: false, status: 0, detail: fallbackDetail(0) };
  }
  if (res && (res.status === 200 || res.status === 206)) {
    return { ok: true, status: res.status, detail: '' };
  }
  const status = res ? res.status : 0;

  let detail = '';
  try {
    const body = await doFetch(url, {
      method: 'GET',
      headers: { ...headers, Range: 'bytes=0-0' },
      mode: 'cors',
    }).then((r) => (r && typeof r.json === 'function' ? r.json() : null)).catch(() => null);
    if (body && typeof body.detail === 'string' && body.detail.trim()) detail = body.detail.trim();
  } catch { /* fall through to our own words */ }

  return { ok: false, status, detail: detail || fallbackDetail(status) };
}

/**
 * OUR words for a refusal — used ONLY when the Worker's own sentence could not
 * be read.
 *
 * ⚠️ THE WORKER'S WORDING WINS WHENEVER IT IS AVAILABLE. One decision, one
 * answer: `ebook-gate.ts` is a single gate serving both ebooks and audio
 * (owner decision 1) and its sentences are pinned by its own tests. Forking
 * the copy forks the gate. These are a fallback for the case where the body
 * could not be fetched at all.
 *
 * ⚠️ EVERY ONE SAYS THREE THINGS, per the estate's refusal rule: what
 * happened, what it needs, and how to get it. And the four causes stay
 * distinct — not signed in / no grant / not uploaded / an outage — because
 * the fixes differ and collapsing them sends people to the wrong person.
 */
export function fallbackDetail(status) {
  switch (status) {
    case 0:
      return 'The audio service could not be reached, so we could not check this book. This is '
        + 'a connection or outage problem, not a decision about your account — try again in a '
        + 'moment, and tell Mitch if it keeps happening.';
    case 401:
      return 'These are the household’s own audiobook files, so the player is not public. '
        + 'Sign in with Google and, if you have been given the book-files grant, it will play.';
    case 403:
      return 'Your account is signed in but does not hold the grant that lets you play the '
        + 'household’s book files. It is the same grant that opens the ebooks — ask '
        + 'Mitch to tick the Ebooks box for your account.';
    case 404:
      return 'This book is not in the streaming bucket. Books are uploaded on request, so open '
        + 'it in the catalogue and press request — it will be ready after the next library '
        + 'run.';
    case 429:
      return 'Too many requests from this account in the last few minutes. Wait a moment and '
        + 'press play again; nothing is wrong with the book or with your access.';
    case 503:
      return 'The streaming list has not been published yet, so nothing can be played at the '
        + 'moment. This is an outage on our side, not a decision about your account.';
    default:
      return 'The audio service refused this book and did not say why. This is a problem on our '
        + `side, not a decision about your account — tell Mitch, and mention the code ${status}.`;
  }
}
