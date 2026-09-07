// worker-writes.js — the client for the audiobook Worker's gated write routes
// ES module, browser-native (no build step)
//
// Phase 3a of the auth migration (catalog-platform/docs/info/
// audiobook-auth-migration.md §5). The Worker half has been built and live
// since 2026-08-17 — 18 routes in `catalog-platform/apps/audiobook-worker/
// src/enforce-routes.ts`, each mirroring exactly the Firestore mutation the
// site's own JS performs today. Until now NOTHING called them. This module is
// the one door the site uses to reach them; `site/flags.js` decides, per
// surface, whether that door is used at all.
//
// ## What this module is for, and what it deliberately is not
//
// It is a transport + a REFUSAL TRANSLATOR. It holds no capability logic:
// which role may do what is decided by `enforce-gate.ts` → `gateDecision()`,
// the same function the shadow soak logged, and re-deriving any of it here
// would create the second copy that eventually disagrees.
//
// ## ⚠️ THE THREE RULES, in order of importance
//
// 1. ⚠️ **A failed Worker call NEVER falls back to Firestore.** Not on a
//    refusal, not on a network error, not on a 503. A silent retry through
//    the browser-direct path would make the flag meaningless — the surface
//    would be enforced only when the enforcement happened to be reachable,
//    which is the worst of both designs and impossible to reason about. The
//    caller gets a worded failure and the person sees it.
// 2. ⚠️ **Nobody ever sees a bare HTTP status or a raw JSON body** (the
//    estate's §1e rule, `site/permission-ux.js` module header). Every exit
//    from here is an English sentence saying what happened, what it needs and
//    how to get it. The Worker's own `detail` strings are already written to
//    that standard, so they are passed through verbatim where present; where
//    they are absent, `describeWorkerRefusal()` supplies one.
// 3. ⚠️ **A network or server failure is worded as an OUTAGE, never as a
//    permission failure.** Mislabelling an outage sends people asking for
//    access they already hold — the exact failure permission-ux.js exists to
//    prevent, reproduced here because a `fetch()` rejection never reaches
//    `describeActionError`.
//
// ## The lane
//
// Every enforce route is `?lane=dev`-aware and defaults to prod, matching
// `col()` in fb-env.js: a page under `/dev/` writes `clubs_dev`/`reviews_dev`
// whichever path it takes. The lane rides the query string because that is
// what `laneFrom(c.req.query('lane'))` reads.

import { IS_DEV_LANE } from './fb-env.js';
import { idToken } from './auth-token.js';

/** The Worker's origin — the same host `gate-shadow.js` reports to. */
export const WORKER_BASE = 'https://audiobook-api.heygabi.ai';

/**
 * Build the full URL for a route path, carrying the data lane.
 * @param {string} path e.g. '/api/clubs/abc/webhook'
 */
export function workerUrl(path) {
  return WORKER_BASE + path + (IS_DEV_LANE ? '?lane=dev' : '');
}

/** Percent-encode one path segment (club ids, slugs, review doc ids). */
export function seg(value) {
  return encodeURIComponent(String(value == null ? '' : value));
}

/**
 * The sentence shown when the Worker refuses, and the reason this module
 * exists as more than a `fetch` wrapper.
 *
 * The Worker's `detail` is preferred whenever it sent one: every refusal in
 * `enforce-gate.ts` and `enforce-routes.ts` is already written to the §1e
 * standard, and they know things this file cannot — which capability was
 * missing, which floor holds it, whether the club was already claimed. Only
 * when no `detail` arrived (a proxy error page, a body that would not parse,
 * a status from something in front of the Worker) does this table answer, and
 * it answers in words on every branch.
 *
 * ⚠️ The status NUMBER never reaches the sentence. A person seeing "403" has
 * been told nothing and been made to feel it is their fault.
 *
 * @param {number} status
 * @param {any} body the parsed JSON body, or null when there was none
 * @returns {string}
 */
export function describeWorkerRefusal(status, body) {
  const detail = body && typeof body.detail === 'string' ? body.detail.trim() : '';
  if (detail) return detail;

  if (status === 401) {
    return 'You are not signed in, so this action was refused. Sign in with Google '
         + 'on this site and try again.';
  }
  if (status === 403) {
    return "You don't have permission to do that. Ask the site owner to grant your "
         + 'account the role this action needs from the estate admin page.';
  }
  if (status === 404) {
    return 'That is no longer there — it may have just been deleted by somebody '
         + 'else. Reload the page to see how it stands now.';
  }
  if (status === 409) {
    return 'Somebody else changed this a moment ago, so the change was not applied. '
         + 'Reload the page and try again.';
  }
  if (status === 400) {
    return 'The server would not accept that — something about the values sent was '
         + 'not valid. Check what you entered and try again.';
  }
  if (status === 429) {
    return 'That was too many requests in a row. Wait a moment and try again.';
  }
  if (status === 503) {
    return 'This action is not switched on server-side yet, so nothing was changed. '
         + 'This is a rollout problem rather than anything you did — tell the owner.';
  }
  if (status >= 500) {
    return 'The server could not complete that just now. This is a fault on our side, '
         + 'not a permission problem — nothing may have been saved, so reload and '
         + 'check before trying again.';
  }
  return 'That action did not complete and the server gave no reason. Reload the page '
       + 'to see how things stand; if it keeps happening, tell the owner.';
}

/** The worded refusal when there is no live session to authorise with. */
export const NO_SESSION_MESSAGE =
  'You are not signed in with Google, so this action cannot be authorised. '
  + 'Sign in on this site (a legacy passphrase session is not enough) and try again.';

/** The worded failure when the Worker could not be reached at all. */
export const UNREACHABLE_MESSAGE =
  "Couldn't reach the server, so nothing was changed. This is a connection problem, "
  + 'not a permission one — check your connection and try again.';

/**
 * Call one gated Worker route.
 *
 * Answers the `{ success, error }` shape every caller in reviews.js /
 * clubs.js / club-reads.js already returns, so switching a function onto this
 * path changes no caller above it.
 *
 * @param {'POST'|'PUT'|'PATCH'|'DELETE'} method
 * @param {string} path already-encoded route path
 * @param {object} [body] JSON body; omitted entirely when absent
 * @returns {Promise<{success: boolean, data?: object, error?: string}>}
 */
export async function workerWrite(method, path, body) {
  const token = await idToken();
  // ⚠️ Refused HERE, before any request: a tokenless call would be answered
  // 401 by the Worker anyway, and asking it costs a round-trip to learn what
  // this page already knows.
  if (!token) return { success: false, error: NO_SESSION_MESSAGE };

  const init = { method, headers: { Authorization: `Bearer ${token}` } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(workerUrl(path), init);
  } catch (e) {
    // Rule 3: an unreachable Worker is an OUTAGE, and `fetch` rejections
    // never carry a status to misread as a refusal.
    return { success: false, error: UNREACHABLE_MESSAGE };
  }

  let parsed = null;
  try {
    parsed = await response.json();
  } catch (e) {
    parsed = null; // a body that will not parse is "no detail", not an error
  }

  if (!response.ok) {
    return { success: false, error: describeWorkerRefusal(response.status, parsed) };
  }
  return { success: true, data: parsed || {} };
}
