// auth-token.js — the live Firebase session's ID token, in one place
// ES module, browser-native (no build step)
//
// Extracted from gate-shadow.js on 2026-09-06 when Phase 3a gave a SECOND
// caller the same need (site/worker-writes.js, which puts the token in an
// Authorization header on every gated write). One fact, one home: two copies
// of "how this site obtains an ID token" would drift the moment one of them
// learned something — a refresh policy, a wait, a fallback — and the other
// did not.
//
// The behaviour is gate-shadow.js's, unchanged and moved verbatim:
//
//  - the DEFAULT Firebase app. Every page calls `initializeApp` exactly once,
//    so nothing has to be threaded through reviews.js/clubs.js/club-reads.js.
//  - `liveAuthUser()` NEVER rejects. Every failure — the SDK missing, no app
//    initialised, auth state that never settles — resolves `null`.
//  - a 4-second ceiling on waiting for auth state, so a page whose auth never
//    publishes still gets an answer instead of hanging a click handler.
//  - `getIdToken()` with NO forceRefresh: the SDK's cached token. A token that
//    cannot be produced is reported as "none", never as an error.
//
// ⚠️ The two callers treat "no token" very differently, and that is correct:
// the shadow reporter logs it as measurement #2 (the tokenless population an
// enforce flip would break), while a gated WRITE must refuse in words and
// write nothing. This module answers the question; it does not decide.

import { getAuth, onAuthStateChanged } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js';

/** How long to wait for Firebase to publish auth state before giving up. */
export const AUTH_WAIT_MS = 4000;

/**
 * The live Firebase user, or null. Never rejects.
 * @returns {Promise<object|null>}
 */
export function liveAuthUser() {
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

/**
 * The live session's cached Firebase ID token, or null. Never rejects.
 *
 * ⚠️ No forced refresh — the SDK hands back what it already holds, and it
 * refreshes on its own schedule. Forcing one here would add a network
 * round-trip to every gated click for a token that is almost always fresh,
 * and an expired token is answered by the Worker in words (401, "sign in with
 * Google …") rather than silently.
 *
 * @returns {Promise<string|null>}
 */
export async function idToken() {
  const user = await liveAuthUser();
  if (!user || typeof user.getIdToken !== 'function') return null;
  try {
    const token = await user.getIdToken();
    return token || null;
  } catch (e) {
    return null;
  }
}
