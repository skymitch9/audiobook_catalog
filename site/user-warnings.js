// user-warnings.js — reader-contributed content warnings
// ES module, browser-native (no build step)
//
// Published warnings (site/content_warnings.json) come from Hardcover /
// DoesTheDogDie / verified web sources via the pipeline. This module covers
// the gap: signed-in readers on THIS site can add warnings the published
// sources miss. Stored per book in `user_content_warnings` (dev lane gets
// the usual `_dev` suffix via col()).

import { doc, setDoc, getDoc, deleteDoc, collection, getDocs, query, where, serverTimestamp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';
import { bookIdFromTitle } from './reviews.js';

export const MAX_WARNING_LABEL = 80;

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
  try {
    await setDoc(doc(db, col('user_content_warnings'), docId), {
      bookId,
      bookTitle,
      label: trimmed,
      displayName: session.displayName,
      createdAt: serverTimestamp(),
    });
    return { success: true, id: docId };
  } catch (e) {
    return { success: false, error: e.message };
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
    return { success: false, error: e.message };
  }
}

/** The pending request for a book, or null. */
export async function getWarningRequest(db, bookTitle) {
  const snap = await getDoc(doc(db, col('cw_requests'), bookIdFromTitle(bookTitle)));
  return snap.exists() ? snap.data() : null;
}

/**
 * Remove a warning you added (client-side author check — rules are the
 * site's usual trust model).
 */
export async function deleteUserWarning(db, warning, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in first.' };
  }
  if ((warning.displayName || '').toLowerCase() !== session.displayName.toLowerCase()) {
    return { success: false, error: 'You can only remove warnings you added.' };
  }
  try {
    await deleteDoc(doc(db, col('user_content_warnings'), warning.id));
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
}
