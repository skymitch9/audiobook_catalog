// gate-shadow.js — Phase 1 shadow reporter (auth-migration design §4)
// ES module, browser-native (no build step)
//
// Fire-and-forget would-deny telemetry: one report per gated user action,
// POSTed to the audiobook-worker's shadow receiver, which runs the FULL
// future gate (verify token -> estate check -> ladder role -> capability +
// club managerUids), logs one JSON line, and acts on nothing. The receiver
// answers 204 always and is inert while ESTATE_CHECK is "off".
//
// ⚠️ THE IRON RULE: the report must never be able to affect the user's
// action. reportGate() is synchronous, returns nothing, and cannot throw;
// every async failure — network down, CORS refusal, CSP block, worker gone,
// auth SDK broken, a bug in this file — is swallowed. The Firestore write it
// accompanies has already run (or already failed) by the time the report is
// even built, and nothing consumes the response. Callers must NEVER await
// this, branch on it, or wrap UI state around it.
//
// The action names are the worker's ACTION_GATES vocabulary
// (catalog-platform/apps/audiobook-worker/src/gate-shadow.ts) — exactly
// those strings; an unknown action still logs server-side, as
// unknown_action, so a typo is visible in the tail rather than silent.
//
// The ID token, when a live Firebase session exists, rides the BODY
// (a beacon-style POST cannot set headers). It is read with getIdToken()
// and NO forceRefresh — the SDK hands back its cached token; a session
// that cannot produce one reports tokenless, which is itself measurement
// #2 of the design (the legacy/v1 population an enforce flip would break).
//
// The request is a "simple" CORS POST on purpose: no headers are set, so
// the body goes as text/plain and no preflight fires. The worker parses
// the body as JSON regardless of content type.
//
// ⚠️ THE OUTCOME BIT — `context.succeeded`, added 2026-08-17 to close the
// soak pack's blocker 4. reportGate() is called from a `finally` block, so
// it fires whether the Firestore write SUCCEEDED or FAILED, and without an
// outcome the two are byte-identical in the log. That made the flip
// criterion unfalsifiable in both directions: it asks for "requests that
// succeeded today but the gate would refuse", so a would_deny line on a
// write firestore.rules already refused is the gate merely AGREEING, while a
// would_deny line on a write that worked is a real regression. Callers know
// which they had; they thread it through. Absent (an older cached build) is
// logged as null server-side — "cannot say", never "failed".
//
// The payload contract this module writes is pinned at BOTH ends:
//   site/__tests__/gate-shadow.test.js  (what is sent)
//   catalog-platform/apps/audiobook-worker/test/gate-shadow.test.ts
//                                       (what is parsed, same literal)

import { getAuth, onAuthStateChanged } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js';
import { IS_DEV_LANE } from './fb-env.js';

export const GATE_SHADOW_URL = 'https://audiobook-api.heygabi.ai/api/gate/shadow';

/** How long to wait for Firebase to publish auth state before reporting
 * tokenless anyway. A page whose auth never settles still measures. */
const AUTH_WAIT_MS = 4000;

/**
 * The live Firebase user, or null — the identity.js liveUser() pattern, but
 * on the DEFAULT app (every page calls initializeApp exactly once) so this
 * module needs no app threaded through reviews.js/clubs.js/club-reads.js.
 * Never rejects; every failure resolves null.
 */
function liveAuthUser() {
  return new Promise((resolve) => {
    let done = false;
    let unsub = null;
    const finish = (u) => {
      if (done) return;
      done = true;
      if (typeof unsub === 'function') { try { unsub(); } catch (e) { /* already detached */ } }
      resolve(u || null);
    };
    try {
      const auth = getAuth();
      if (auth.currentUser) return finish(auth.currentUser);
      unsub = onAuthStateChanged(auth, (u) => finish(u));
      setTimeout(() => finish(null), AUTH_WAIT_MS);
    } catch (e) {
      finish(null);
    }
  });
}

/** The async half — only ever called with every rejection swallowed. */
async function sendReport(action, context) {
  const payload = { action: String(action), lane: IS_DEV_LANE ? 'dev' : 'prod' };
  if (context && context.clubId) payload.clubId = String(context.clubId);
  // The OUTCOME BIT (see the module header). Sent ONLY when the caller
  // actually knows — a strict boolean, never coerced from undefined, because
  // the worker's third state ("this report cannot say") has to stay reachable
  // and must never be silently read as a failure.
  if (context && typeof context.succeeded === 'boolean') payload.succeeded = context.succeeded;
  const user = await liveAuthUser();
  if (user && typeof user.getIdToken === 'function') {
    try {
      // No forceRefresh argument: the cached token, never a refresh forced
      // just to report. An unobtainable token measures as tokenless.
      const token = await user.getIdToken();
      if (token) payload.token = token;
    } catch (e) { /* tokenless report — that IS the measurement */ }
  }
  await fetch(GATE_SHADOW_URL, {
    method: 'POST',
    mode: 'cors',
    keepalive: true, // survives the navigation a click handler may trigger
    body: JSON.stringify(payload),
  });
}

/**
 * Report one gated user action to the shadow receiver. Fire-and-forget:
 * returns nothing, never throws, never blocks, and the outcome is
 * deliberately unobservable to the caller.
 *
 * @param {string} action one of the worker's ACTION_GATES names,
 *   e.g. 'review.delete', 'club.setSchedule'
 * @param {{clubId?: string, succeeded?: boolean}} [context]
 *   `clubId` — the club the action targets, where the gate is club-scoped
 *   (the worker consults that club's managerUids).
 *   `succeeded` — did the Firestore write this report accompanies actually
 *   work? Omit ONLY where the caller genuinely cannot tell; a wrong value is
 *   worse than an absent one, because absent logs honestly as null.
 */
export function reportGate(action, context) {
  try {
    sendReport(action, context).catch(() => { /* iron rule: swallowed */ });
  } catch (e) {
    /* iron rule: telemetry can never touch the action */
  }
}
