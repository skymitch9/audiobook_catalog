// flags.js — per-surface rollout flags
// ES module, browser-native (no build step)
//
// Phase 3a of the auth migration (catalog-platform/docs/info/
// audiobook-auth-migration.md §5) switches four write surfaces from
// browser-direct Firestore writes onto the audiobook Worker's 22 enforce
// routes. (It was three surfaces and 18 routes when this file was written on
// 2026-09-06; the fourth surface and the last four routes landed 2026-09-07,
// closing the `comment.modDelete` / `quote.modDelete` / `warning.*Delete` gap
// Phase 3a had recorded.) §5's own rule for that phase is:
//
//     "3a. Worker endpoints live, client switched behind a per-surface flag
//      (old direct-write path kept in code one release)."
//
// This file is that flag mechanism. There was none before — grepped
// 2026-09-06 across `site/*.js`; the only "flag"-shaped things in the tree
// were `ratingsRevealed` (a Firestore field) and the reader's fetch
// switches, neither of which is a rollout control.
//
// ## ⚠️ EVERY FLAG DEFAULTS OFF, AND FLIPPING ONE IS THE OWNER'S ACT
//
// A `false` here means the surface behaves EXACTLY as it did before Phase
// 3a: the browser writes Firestore directly, under today's `firestore.rules`.
// Nothing a visitor sees changes until a flag is flipped, and flipping one is
// a deliberate, reviewed edit to this file — never a side effect of shipping
// something else. The old path stays in the code beside the new one for one
// release, so the reverse lever is this file and nothing more (design §5
// Phase 3: "flip the client flag back", one of the two independent levers).
//
// ## The local-check override — per-browser, and it can only make you WEAKER
//
// A reviewer needs to exercise the Worker path before the owner flips
// anything, so a flag may also be turned on FOR ONE BROWSER from the
// devtools console:
//
//     localStorage.setItem('ab_flags', JSON.stringify({ AUTH_ROUTES_REVIEWS: true }))
//     // then reload — the override is read ONCE, at module load
//     localStorage.removeItem('ab_flags')   // and back to the shipped default
//
// ⚠️ This is NOT a security hole, and the direction is why: the Worker gate
// (`enforce-gate.ts` → `gateDecision`) is at least as strict as
// `firestore.rules` on every action it mirrors. Switching yourself onto it
// can only refuse you things you can do today; it can never grant you
// anything. The one thing it changes for the person who set it is that their
// writes are now server-enforced.
//
// ⚠️ Set it on `https://audiobooks.heygabi.ai/dev/`, NOT on localhost: the
// Worker's `SITE_ORIGINS` allows `audiobooks.heygabi.ai` and
// `ebooks.heygabi.ai` and nothing else, so a localhost page is refused by
// CORS before the gate is ever reached — which looks exactly like "the Worker
// is down".

/**
 * The shipped value of every per-surface flag. ⚠️ ALL FALSE. Adding a
 * surface means adding a key here; a name absent from this map is not a flag
 * and `flagEnabled()` answers false for it, so a typo fails closed (the
 * browser-direct path) rather than silently enabling something.
 */
export const FLAG_DEFAULTS = {
  /** site/reviews.js  — deleteReview → DELETE /api/reviews/:docId */
  AUTH_ROUTES_REVIEWS: false,
  /** site/clubs.js    — the club doc, webhook, claim and member ops */
  AUTH_ROUTES_CLUBS: false,
  /**
   * site/club-reads.js — read lifecycle, schedule, the three poll ops, and
   * (since 2026-09-07) the two MODERATION deletes: someone else's comment and
   * someone else's quote. ⚠️ Nine functions on one flag now, not seven —
   * because the convention here is ONE FLAG PER FILE, not per route. A
   * per-route flag would multiply the rollout surface without giving anyone a
   * finer decision than "is this module's writes enforced yet".
   */
  AUTH_ROUTES_CLUB_READS: false,
  /**
   * site/user-warnings.js — deleteUserWarning, whose two arms take two routes
   * (added 2026-09-07). The FOURTH surface, and it needed its own flag rather
   * than a share of another for the same reason the first three are separate:
   * it is its own file, it can be switched on and reviewed alone, and the
   * `user_content_warnings` collection has nothing to do with clubs.
   *
   *   your own note      → DELETE /api/warnings/:docId
   *   anyone else's note → DELETE /api/warnings/:docId/moderate
   */
  AUTH_ROUTES_WARNINGS: false,
};

/** The localStorage key holding the per-browser override map. */
export const FLAG_OVERRIDE_KEY = 'ab_flags';

/**
 * Parse an override blob into a map of `{name: true|false}`.
 *
 * Pure, and deliberately strict: only a JSON OBJECT whose values are real
 * booleans contributes anything. A string `"true"`, a `1`, an array, a
 * malformed blob — each yields no override at all, so the shipped default
 * stands. A half-understood override is the one outcome worth refusing: it
 * would put a browser on a path nobody could name from reading this file.
 *
 * @param {string|null} raw
 * @returns {Object<string, boolean>}
 */
export function parseFlagOverrides(raw) {
  if (typeof raw !== 'string' || !raw) return {};
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return {};
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
  const out = {};
  for (const key of Object.keys(FLAG_DEFAULTS)) {
    if (typeof parsed[key] === 'boolean') out[key] = parsed[key];
  }
  return out;
}

/**
 * Read the override map once, at module load — never per call.
 *
 * ⚠️ Read-once is deliberate. A flag that could change between two writes in
 * one page life would let a surface start an action on one path and finish it
 * on the other, which is the one failure mode a rollout flag must not have.
 * Reload to change it.
 */
const OVERRIDES = (() => {
  try {
    return parseFlagOverrides(
      typeof localStorage !== 'undefined' ? localStorage.getItem(FLAG_OVERRIDE_KEY) : null,
    );
  } catch (e) {
    // Private mode, a blocked-storage setting, or a non-browser host: the
    // shipped defaults are the answer, and that is never an error.
    return {};
  }
})();

/**
 * Is this surface on the Worker path?
 *
 * @param {string} name one of FLAG_DEFAULTS' keys
 * @returns {boolean} false for anything unknown — fail closed.
 */
export function flagEnabled(name) {
  if (Object.prototype.hasOwnProperty.call(OVERRIDES, name)) return OVERRIDES[name] === true;
  return FLAG_DEFAULTS[name] === true;
}
