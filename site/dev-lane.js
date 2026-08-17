/**
 * dev-lane.js — the /dev/ lane's worded curtain for the ebook pages.
 *
 * Owner, 2026-08-17: *"i need a way in the estate to manage dev access for
 * ebook, add a button for give dev access also make devops always able to see
 * dev envs."* The estate half shipped in `catalog-platform` the same day
 * (migration `0011_dev_access.sql`, auth-worker `be6f15c`); this is the half
 * that draws the curtain.
 *
 * ## ⚠️ A CURTAIN, NOT A LOCK. Say it out loud, because the difference is the
 * whole design.
 *
 * Nothing here protects a byte. The books stay locked by the estate's
 * `vis_ebooks` grant, enforced SERVER-SIDE by the audiobook Worker on
 * `/api/ebooks/manifest` and on **every range** of `/api/ebook/:anchor/file`,
 * identically on both lanes and completely untouched by this file. Someone who
 * deletes this curtain with devtools gets the dev lane's markup — and then the
 * same 401/403 for the books that they would have got anyway.
 *
 * What it IS: the /dev/ lane is the in-progress copy, and a household member
 * who lands on it should be told that in words rather than shown a half-built
 * shelf and left to wonder which one is real. The estate's own header says the
 * same thing about the flag (`apps/auth-worker/src/me.ts`): *"a `true` here has
 * never opened a file and must never start."*
 *
 * ## ⚠️ LANE-AWARE, NEVER HOST-AWARE — and the prod path gets no check at all
 *
 * `isDevLane()` reads the PATH. Every caller guards on it FIRST, so a prod page
 * (`ebooks.heygabi.ai/ebooks`, `/read`) never makes the estate call, never
 * awaits anything new, and cannot be curtained by an outage.
 *
 * That is not only tidiness. `ME_ORIGINS` on the auth Worker is
 * `heygabi.ai,audiobooks.heygabi.ai` — **`ebooks.heygabi.ai` is not in it**, so
 * the very call this module makes would be CORS-refused from the prod host.
 * Being lane-aware is what makes that irrelevant instead of a bug: the /dev/
 * lane is a PATH on `audiobooks.heygabi.ai`, which IS an allowed origin.
 *
 * ## ⚠️ "WE COULD NOT TELL" IS NOT "NO"
 *
 * `devAccessFrom()` answers `null` — never `false` — when the estate did not
 * answer, answered something unrecognisable, or answered without the field
 * (an older Worker, or a `sessionStorage` reply cached by identity.js before
 * `dev_access` existed; its TTL is ten minutes). `verdictFor()` turns that into
 * `'unknown'`, and every caller lets an `'unknown'` through.
 *
 * Failing OPEN is the right way round here and it is a deliberate, arguable
 * call:
 *   - a network or server failure is NOT a permission failure, and dressing an
 *     outage up as one sends people asking for access they already hold;
 *   - this is a curtain, so a missed curtain costs a person seeing a page they
 *     had no business being interested in — while a FALSE curtain locks the
 *     household's own devops out of the lane they were told to work in;
 *   - the thing that would actually matter, the books, is gated somewhere else
 *     entirely and fails CLOSED there.
 */

import { getEstateStatus } from './identity.js';

/** Where a curtained reader should go instead. The regular, promoted site. */
export const PROD_SHELF_URL = 'https://ebooks.heygabi.ai';

/**
 * The curtain's words. What happened, what it needs, and how to get it —
 * never a bare status, never a dead page.
 *
 * ⚠️ It names the ADMIN PAGE rather than a person, and it says the devops
 * implication out loud, because "ask for dev access" to somebody who is devops
 * and therefore already has it is the same mislabelling in another costume.
 */
export const DEV_CURTAIN = {
  title: 'The dev lane is for people building the site',
  why:
    'This is /dev/ — the work-in-progress copy of the ebook pages, where changes are ' +
    'tried out before they reach everyone. Seeing it needs dev access, which is switched ' +
    'on per person from the estate admin page at heygabi.ai/admin (anyone with devops or ' +
    'approver already has it). Nothing is wrong with your account, and your books are ' +
    'unaffected — the regular shelf is at ebooks.heygabi.ai.',
  href: PROD_SHELF_URL,
  hrefText: 'Go to the regular shelf',
};

/**
 * Is this page being served under the /dev/ lane?
 *
 * ⚠️ A PATH, NOT A HOST. `/dev/ebooks` and `/dev/read` live on
 * `audiobooks.heygabi.ai`; the promoted copies live at the ROOT of
 * `ebooks.heygabi.ai`. Every other lane-sensitive thing in this repo (the
 * `_headers` CSP rules, every relative asset reference) is written the same
 * way, and a host check would quietly curtain the wrong site.
 */
export function isDevLane(pathname) {
  const p = typeof pathname === 'string'
    ? pathname
    : (typeof window !== 'undefined' && window.location ? window.location.pathname : '');
  return /^\/dev(?:\/|$)/.test(String(p || ''));
}

/**
 * The estate's EFFECTIVE dev-access answer, or `null` for "we could not tell".
 *
 * ⚠️ It is read, never derived. `devAccessAllows()` in the auth Worker is the
 * ONE implementation of the owner's rule (`approved AND (dev_access OR
 * is_devops OR is_approver)`, plus the owner break-glass), and re-deriving
 * *"devops implies dev access"* here from `is_devops` would be a second copy of
 * that rule, free to drift the first time the owner changes it. So: the boolean
 * or nothing.
 *
 * @param {{dev_access?: unknown}|null|undefined} answer  a GET /api/estate/me body
 * @returns {boolean|null}
 */
export function devAccessFrom(answer) {
  if (!answer || typeof answer !== 'object') return null;
  return typeof answer.dev_access === 'boolean' ? answer.dev_access : null;
}

/**
 * 'prod-lane' | 'allowed' | 'curtain' | 'unknown', from a lane and an answer.
 * Pure, so every branch is exercised in site/__tests__/dev-lane.test.js.
 */
export function verdictFor(onDevLane, answer) {
  if (!onDevLane) return 'prod-lane';
  const dev = devAccessFrom(answer);
  if (dev === true) return 'allowed';
  if (dev === false) return 'curtain';
  return 'unknown';
}

/**
 * The same verdict, for a live signed-in session.
 *
 * ⚠️ NO NETWORK ON THE PROD PATH. The lane check comes first and returns
 * before `getEstateStatus()` is ever reached, so promoted pages add no request,
 * no await and no new failure mode.
 *
 * ⚠️ SIGNED-OUT IS NOT THIS FUNCTION'S PROBLEM. `getEstateStatus()` answers
 * null with no live session, which lands on `'unknown'` — and every caller
 * runs its own sign-in gate BEFORE asking, so a signed-out visitor meets the
 * ordinary "sign in" door first and never a curtain that would tell them the
 * wrong thing to do.
 *
 * @param {import('firebase/app').FirebaseApp} app
 * @returns {Promise<'prod-lane'|'allowed'|'curtain'|'unknown'>}
 */
export async function devLaneVerdict(app, pathname) {
  if (!isDevLane(pathname)) return 'prod-lane';
  const answer = await getEstateStatus(app).catch(() => null);
  const verdict = verdictFor(true, answer);
  if (verdict === 'unknown') {
    // Named, not silent: an operator reading a console needs to be able to tell
    // "the estate said no" from "the estate did not say".
    console.warn('[dev-lane] no dev_access in the estate answer — letting the page through (curtain, not lock)');
  }
  return verdict;
}
