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
 * Flag a book for the AI content-warning lookup (open to everyone, no
 * sign-in needed). One request doc per book — repeat clicks just overwrite.
 * Fulfilled by `python -m app.tools.fetch_content_warnings --requests`
 * (also runs automatically during library sync).
 */
export async function requestWarningCheck(db, bookTitle, session) {
  const bookId = bookIdFromTitle(bookTitle);
  if (!bookId) return { success: false, error: 'Bad book title.' };
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

  try {
    await deleteDoc(doc(db, col('user_content_warnings'), warning.id));
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
    reportGate(authored ? 'warning.selfDelete' : 'warning.modDelete');
  }
}
