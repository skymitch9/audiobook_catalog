// permission-ux.js — turn a failed action into a human sentence
// ES module, browser-native (no build step)
//
// Owner requirement 2026-08-16 (docs/info/ROLES.md §1e): "make sure if any
// one gets permission blocked they get a warning message and not a https
// only error. make it a good ux." Nobody sees a bare HTTP status, a raw
// Firestore SDK message ("Missing or insufficient permissions."), or a raw
// JSON body. A refusal says three things: what happened, what it needs, and
// how to get it. A network/server failure is NOT a permission failure (§1e
// point 5) — mislabelling an outage sends people to ask for access they
// already have, so the two are told apart here in one place rather than in
// every catch block that surfaces an error to the UI.
//
// This is presentation only. It does not change who can do what — the gate
// is still firestore.rules (server-enforced) or the UI checks that decide
// whether to render a control at all. This module only decides the sentence
// shown when a gate that was already going to refuse, refuses.

/** True when an error looks like a Firestore/HTTP permission refusal. */
export function isPermissionError(e) {
  if (!e) return false;
  if (e.code === 'permission-denied') return true;
  if (typeof e.status === 'number' && (e.status === 401 || e.status === 403)) return true;
  return /permission/i.test(e.message || '');
}

/**
 * True when an error looks like a network/server failure rather than a
 * refusal. Firestore's SDK surfaces these with codes like 'unavailable' when
 * offline or the backend cannot be reached; text-matching covers fetch()'s
 * plain "Failed to fetch" / "NetworkError".
 */
export function isNetworkError(e) {
  if (!e) return false;
  const code = e.code || '';
  if (['unavailable', 'deadline-exceeded', 'internal', 'cancelled'].includes(code)) return true;
  return /network|offline|failed to fetch/i.test(e.message || '');
}

/**
 * Turn a caught error into a sentence safe to show a person.
 *
 * @param {any} e the caught error
 * @param {{ need?: string, fallback?: string }} [opts]
 *   need: what the action requires, named in the sentence — e.g. "the host
 *     or moderator role". Omit when the exact role isn't known here; the
 *     generic "ask an admin" still satisfies the standard.
 *   fallback: message for errors that are neither a permission refusal nor a
 *     network failure. Defaults to e.message, because most thrown errors in
 *     this codebase are already hand-authored human sentences ("Club not
 *     found.", "The host cannot be removed.") rather than SDK internals.
 * @returns {string}
 */
export function describeActionError(e, opts) {
  const o = opts || {};
  if (isPermissionError(e)) {
    const need = o.need ? ` That needs ${o.need}.` : '';
    return `You don't have permission to do that.${need} Ask an admin.`;
  }
  if (isNetworkError(e)) {
    return "Couldn't reach the server. Check your connection and try again.";
  }
  return o.fallback || (e && e.message) || 'Something went wrong. Try again.';
}
