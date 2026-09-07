// user-warnings.js — reader-contributed content warnings
// ES module, browser-native (no build step)
//
// Published warnings (site/content_warnings.json) come from Hardcover /
// DoesTheDogDie / verified web sources via the pipeline. This module covers
// the gap: signed-in readers on THIS site can add warnings the published
// sources miss. Stored per book in `user_content_warnings` (dev lane gets
// the usual `_dev` suffix via col()).

import { doc, setDoc, getDoc, deleteDoc, collection, getDocs, query, where, serverTimestamp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { getApp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-app.js';
import { col } from './fb-env.js';
import { bookIdFromTitle } from './reviews.js';
import { describeActionError } from './permission-ux.js';
import { getLiveUser } from './identity.js';
import { reportGate } from './gate-shadow.js';
import { flagEnabled } from './flags.js';
import { seg, workerWrite } from './worker-writes.js';

/* ── Phase 3a: the Worker path, behind AUTH_ROUTES_WARNINGS ───────────────
 *
 * ONE of this file's write functions has mirror routes in the audiobook Worker
 * (catalog-platform/apps/audiobook-worker/src/enforce-routes.ts) — and it has
 * TWO of them, because `deleteUserWarning` has always been one function with
 * two gates (the 2026-08-17 split, owner-approved):
 *
 *   your own note      DELETE /api/warnings/:docId            warning.selfDelete
 *   anyone else's note DELETE /api/warnings/:docId/moderate   warning.modDelete
 *
 * ⚠️ The choice between them is made HERE, by the same `authored` boolean that
 * already chooses which shadow action to report — and the Worker does NOT take
 * that choice on trust. The self route re-checks `authorUid == uid` against the
 * stored document (firestore.rules' `warningAuthorIsRequester`), so a client
 * that named the wrong route gets a worded refusal, not a delete.
 *
 * ⚠️ `addUserWarning` and `requestWarningCheck` do NOT move, and neither has a
 * route. Both are member-open creates the rules keep shape-only; `cw_requests`
 * in particular is money-gated by `request.auth != null` in the rules and by
 * nothing here.
 *
 * ⚠️ No fallback, worded refusals, and no `ab_gate_shadow` report on the
 * Worker path — the Worker writes its own `ab_gate` line and a shadow line
 * beside it would count one action twice. Same three notes as clubs.js.
 */

export const MAX_WARNING_LABEL = 80;

/**
 * The ENFORCED identity's uid, or null — what firestore.rules compares
 * `authorUid` against (see canDeleteUserWarning there).
 *
 * Goes through identity.js getLiveUser(), the one public accessor for the
 * verified identity, on the DEFAULT Firebase app — every page calls
 * initializeApp exactly once, the same assumption gate-shadow.js makes. Doing
 * it here rather than taking a uid parameter is deliberate: two pages call
 * addUserWarning (app/web/templates/index.html and site/club-read.html) and a
 * caller that forgot to pass it would silently write an unprotected note.
 *
 * Never throws. Null means "no live session" — a legacy/mirror-only session
 * or an app that failed to initialise — and null is a note nobody can
 * self-delete, which the UI says out loud rather than discovering as a
 * PERMISSION_DENIED.
 */
async function liveUid() {
  try {
    const user = await getLiveUser(getApp());
    return (user && user.uid) || null;
  } catch (e) {
    return null;
  }
}

/**
 * Add a content warning for a book. One doc per (book, reader, topic) —
 * re-adding the same topic just overwrites it, so no dupes.
 * @returns {Promise<{success: boolean, error?: string}>}
 */
export async function addUserWarning(db, bookTitle, label, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to add a content warning.' };
  }
  const trimmed = (label || '').trim();
  if (!trimmed) return { success: false, error: 'Warning cannot be empty.' };
  if (trimmed.length > MAX_WARNING_LABEL) {
    return { success: false, error: `Warnings must be ${MAX_WARNING_LABEL} characters or fewer.` };
  }
  const bookId = bookIdFromTitle(bookTitle);
  const docId = `${bookId}_${session.displayName.toLowerCase()}_${bookIdFromTitle(trimmed)}`.slice(0, 900);
  // The delete binding (2026-08-17): stamped when a live session can prove a
  // uid, omitted when it cannot — firestore.rules keeps create shape-only, so
  // a legacy session can still add a note, it just cannot remove it later.
  const authorUid = await liveUid();
  try {
    const data = {
      bookId,
      bookTitle,
      label: trimmed,
      displayName: session.displayName,
      createdAt: serverTimestamp(),
    };
    if (authorUid) data.authorUid = authorUid;
    await setDoc(doc(db, col('user_content_warnings'), docId), data);
    return { success: true, id: docId };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/**
 * All reader-added warnings for a book, oldest first.
 * @returns {Promise<Array<{id, label, displayName}>>}
 */
export async function getUserWarnings(db, bookTitle) {
  const q = query(
    collection(db, col('user_content_warnings')),
    where('bookId', '==', bookIdFromTitle(bookTitle)),
  );
  const snap = await getDocs(q);
  return snap.docs
    .map(d => ({ id: d.id, ...d.data() }))
    .sort((a, b) => (a.createdAt?.seconds || 0) - (b.createdAt?.seconds || 0));
}

/**
 * The sentence a signed-out reader sees where the button used to be.
 *
 * ⚠️ Exported so the page and the test say the SAME words — a refusal
 * duplicated in an HTML template is a refusal that drifts.
 */
export const SIGN_IN_TO_REQUEST_WARNING = 'Sign in to request a content warning.';

/**
 * What to render in the AI-check slot: a working button, or a sentence.
 *
 * ⚠️ Keyed on the LIVE uid, never on `getSession()`. A legacy passphrase
 * session has a `displayName` and no Firebase uid at all (KI-4), so it looks
 * signed in to every display-name check in this file and holds no
 * `request.auth` whatsoever — it would get a button that always fails. The
 * gate below and firestore.rules ask the same question of the same thing.
 *
 * The estate rule is that a person never sees a dead control OR a bare
 * status: prefer not rendering a control nobody can use, but never hide so
 * much the page looks broken — so the slot keeps its space and says why.
 */
export async function warningRequestAffordance() {
  const uid = await liveUid();
  return uid
    ? { kind: 'button', label: '🔎 Request AI warning check' }
    : { kind: 'sentence', label: SIGN_IN_TO_REQUEST_WARNING };
}

/**
 * Flag a book for the AI content-warning lookup. One request doc per book —
 * repeat clicks just overwrite. Fulfilled by
 * `python -m app.tools.fetch_content_warnings --requests` (also runs
 * automatically during library sync).
 *
 * 🔴 SIGN-IN REQUIRED SINCE 2026-08-26, and this was a MONEY defect, not a
 * tidiness one. This was *"open to everyone, no sign-in needed"* — a public,
 * anonymous button on a public page that enqueued work the hourly GitHub
 * Action `cw-fulfill.yml` pays Anthropic for. Anyone on the internet could
 * spend the household's LLM budget by clicking it, one book at a time, with
 * no account and no trace beyond a display name they chose themselves.
 * Written up as A3 in `catalog-platform/docs/info/llm-billing-control-design.md`.
 *
 * ⚠️ The gate is `firestore.rules` (`cw_requests` create/update now require
 * `request.auth != null`); everything here is UX, deciding WHICH worded
 * refusal to show. It fails closed either way — a caller that skips this
 * function is refused by the rules with a PERMISSION_DENIED that
 * `describeActionError` turns into words.
 *
 * ⚠️ `allow delete: if true` on that same rules block is LOAD-BEARING and
 * deliberately untouched: the fulfiller clears a finished request over the
 * REST API using the *public web API key*, holding no account at all. Gating
 * delete would strand every request with no error anywhere.
 */
export async function requestWarningCheck(db, bookTitle, session) {
  const bookId = bookIdFromTitle(bookTitle);
  if (!bookId) return { success: false, error: 'Bad book title.' };
  // Asked BEFORE the write, so a signed-out reader gets the sentence rather
  // than a round-trip that comes back PERMISSION_DENIED.
  const uid = await liveUid();
  if (!uid) return { success: false, error: SIGN_IN_TO_REQUEST_WARNING, signedOut: true };
  try {
    await setDoc(doc(db, col('cw_requests'), bookId), {
      bookTitle,
      requestedBy: session && session.displayName ? session.displayName : 'anonymous',
      createdAt: serverTimestamp(),
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/** The pending request for a book, or null. */
export async function getWarningRequest(db, bookTitle) {
  const snap = await getDoc(doc(db, col('cw_requests'), bookIdFromTitle(bookTitle)));
  return snap.exists() ? snap.data() : null;
}

/**
 * Remove a reader-added content note — YOUR OWN, or anyone's as a site
 * moderator+ (the 2026-08-17 delete split, owner-approved).
 *
 * ⚠️ The gate is firestore.rules, not this function: delete is allowed only
 * when `authorUid` on the doc equals the caller's live uid, or the caller's
 * site_roles role is moderator/admin (canDeleteUserWarning there). Everything
 * below is UX — it decides WHICH worded refusal to show and which shadow
 * action to report, and it fails closed if the caller lies about canModerate
 * (the rules deny, and the refusal is worded by permission-ux.js).
 *
 * Reports one shadow action per attempt, split by which floor the write needs:
 *   warning.selfDelete — your own note (member floor)
 *   warning.modDelete  — someone else's (moderator floor)
 * Blind spot #3 of the 2026-08-16 soak audit: this module reported NOTHING
 * before, so the moderator surface measured as unused rather than as absent.
 *
 * ## Two paths, one behaviour, chosen by `AUTH_ROUTES_WARNINGS` (site/flags.js)
 *
 * FLAG OFF (the shipped default): the browser deletes the document directly,
 * rules-enforced by `canDeleteUserWarning()` exactly as it has been since
 * 2026-08-17. Nothing below changes.
 *
 * FLAG ON: the delete goes to whichever of the Worker's two routes matches the
 * arm this attempt is on — the same split the shadow actions above already
 * name. The gain over the rules is the estate check: `firestore.rules` can only
 * ask whether a `site_roles` doc says moderator, so a REVOKED moderator whose
 * doc still stands keeps this power today; the Worker asks the household
 * directory too, and refuses. (The SELF arm is `{kind:'signedIn'}` and reaches
 * no estate check on either path — removing your own note is not a role.)
 *
 * ⚠️ NO FALLBACK. A Worker refusal or outage is returned as a worded error and
 * never retried through Firestore (worker-writes.js rule 1).
 *
 * @param {object} db
 * @param {{id: string, displayName?: string, authorUid?: string}} warning
 * @param {{displayName: string}} session the localStorage mirror (presentation)
 * @param {{canModerate?: boolean}} [opts] canModerate MUST come from the real
 *   role model — resolveSiteAccess()'s 'operateClub' capability, the same
 *   answer club-read.html's canOperate() uses. Never a display-name guess.
 */
export async function deleteUserWarning(db, warning, session, opts) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in first.' };
  }
  const canModerate = !!(opts && opts.canModerate);
  const uid = await liveUid();
  const authored = !!warning.authorUid && warning.authorUid === uid;
  const nameMatches =
    (warning.displayName || '').toLowerCase() === session.displayName.toLowerCase();

  if (!authored && !canModerate) {
    if (!nameMatches) {
      return { success: false, error: 'You can only remove warnings you added.' };
    }
    if (!uid) {
      return {
        success: false,
        error: 'Sign in with Google again to remove this — this session cannot prove '
             + 'the note is yours. A site moderator can take it down for you.',
      };
    }
    // Their name, but no matching authorUid: a note written before the note
    // was tied to an account (or by a different person with the same name).
    return {
      success: false,
      error: 'This note was added before removals were tied to your account, so only a '
           + 'site moderator can take it down. Add it again and it becomes yours to remove.',
    };
  }

  if (flagEnabled('AUTH_ROUTES_WARNINGS')) {
    // ⚠️ The SAME boolean that picks the shadow action picks the route, so the
    // two can never disagree about which floor this attempt was measured
    // against. `authored` here means "the stored authorUid is my live uid" —
    // exactly what the self route re-verifies server-side.
    const result = await workerWrite(
      'DELETE',
      authored ? `/api/warnings/${seg(warning.id)}` : `/api/warnings/${seg(warning.id)}/moderate`,
    );
    return result.success ? { success: true } : { success: false, error: result.error };
  }

  let succeeded = false; // the shadow report's outcome bit — see the finally
  try {
    await deleteDoc(doc(db, col('user_content_warnings'), warning.id));
    succeeded = true;
    return { success: true };
  } catch (e) {
    return {
      success: false,
      error: authored
        ? describeActionError(e)
        : describeActionError(e, { need: 'the site moderator role' }),
    };
  } finally {
    // Phase 1 shadow telemetry (fire-and-forget, cannot affect the delete —
    // see gate-shadow.js). The action names are the worker's ACTION_GATES
    // vocabulary (catalog-platform apps/audiobook-worker/src/gate-shadow.ts).
    reportGate(authored ? 'warning.selfDelete' : 'warning.modDelete', { succeeded });
  }
}
