// reviews.js — Book review system for the audiobook catalog
// ES module, browser-native (no build step)

import { doc, setDoc, getDoc, deleteDoc, serverTimestamp, collection, getDocs } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';
import { describeActionError } from './permission-ux.js';
import { reportGate } from './gate-shadow.js';

/**
 * Derive a book identifier by slugifying the title.
 * Lowercase, replace non-alphanumeric characters with hyphens,
 * collapse multiple consecutive hyphens, trim leading/trailing hyphens.
 * @param {string} title
 * @returns {string}
 */
export function bookIdFromTitle(title) {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^-|-$/g, '');
}

/* ── the reading list's document id: ACCOUNT-keyed since 2026-08-18 ──────── */

/**
 * The owner's order, 2026-08-18, verbatim:
 *
 *     "Make tbr keyed to account"
 *
 * given in answer to the measured finding that `readingLists/{displayNameLower}
 * _{bookId}` files everybody's to-read list under a string anyone can choose.
 * Two members who pick the same display name share one document per book: each
 * sees the other's intentions on their own list, each can delete the other's,
 * and nothing anywhere can tell them apart, because a display name identifies
 * nobody. Firestore rules could not close it either — `firestore.rules` says so
 * in its own header ("no rule can bind a display name to a person").
 *
 * ⚠️ THIS SUPERSEDES THE "MAY NOT BE HARMONISED" NOTE that stood here and in
 * `library_catalog/docs/info/tbr.md` §2. That note was right about the risk —
 * changing a persisted key does not migrate the documents, it orphans them —
 * and wrong only about the conclusion: the answer is to MIGRATE them, which is
 * what `scripts/migrate_tbr_to_uid.py` did. It is a migration, never an edit.
 *
 * The new id matches `positionDocId` in `site/reading-position.js` exactly —
 * this estate's uid-keyed precedent — and the `_` is load-bearing for the same
 * reason: firestore.rules reads the account back out with `docId.split('_')[0]`,
 * which is what makes "your own list" enforceable rather than advisory.
 *
 * @param {string} uid the live Firebase uid — NOT a display name
 * @param {string} bookId
 */
export function readingListDocId(uid, bookId) {
  return `${uid}_${bookId}`;
}

/* ── the legacy display-name lane: REMOVED 2026-08-18 ─────────────────────
 *
 * For one day this collection ran two id shapes at once — `{uid}_{bookId}` and
 * the old `{displayNameLower}_{bookId}` — because 53 of 234 documents belonged
 * to a retired v1 passphrase account with no Firebase uid to key to, and
 * guessing an owner for somebody's reading list was refused. Three things
 * lived here to serve them: `legacyReadingListDocId`, `isUidKeyedListId` (the
 * lane discriminator, mirrored in firestore.rules as `tbrIdIsUidKeyed`), and a
 * display-name branch inside `ownsReadingListDoc`.
 *
 * ⚠️ THE REMOVAL CONDITION WAS A MEASUREMENT, AND IT WAS MET. The owner
 * decided those 53 ("Reassign to Samantha but skip duplicates"),
 * `scripts/reassign_tbr_owner.py` carried them over, and the condition the
 * migration script prints for itself:
 *
 *     python scripts/migrate_tbr_to_uid.py --report
 *     -> uid-less documents remaining: 0
 *
 * read **0** on 2026-08-18 across both lanes — 234 documents, 234 already
 * account-keyed. Re-run that command before reintroducing anything here.
 *
 * ⚠️ AND IT IS WHY THE DISCRIMINATOR WENT TOO. `isUidKeyedListId` existed
 * only to tell the two lanes apart. Leaving a lane discriminator behind with
 * one lane left is an invitation to re-add the other — and the other lane's
 * rule was, necessarily, a SIGNED-OUT write allowance, since a legacy session
 * has no `request.auth` to check.
 *
 * The as-built record is `docs/info/tbr-account-migration.md`.
 * ──────────────────────────────────────────────────────────────────────── */

/**
 * Is this reading-list document MINE? The one implementation, used by every
 * surface that scans the collection (the modal button, the "Reading lists"
 * filter) rather than fetching one id.
 *
 * An ACCOUNT match, and nothing else. Before 2026-08-18 this fell back to
 * comparing display names for a document carrying no `uid`; that branch is
 * gone with the legacy lane, and it must not come back. A name match is not an
 * identity — reinstating it would hand a name-sharer somebody else's list
 * again, which is the exact bug the migration was ordered to fix, and it would
 * do so while every other test still passed.
 *
 * A document with no `uid` is now unowned by anybody rather than owned by
 * whoever shares its name. That fails CLOSED: it drops off one person's list
 * (visible, reportable) instead of appearing on a stranger's (silent).
 *
 * @param {{uid?: string, displayName?: string}} data the document's fields
 * @param {{uid?: string|null, displayName?: string|null}} me
 */
export function ownsReadingListDoc(data, me) {
  const docUid = (data && data.uid) || '';
  const myUid = (me && me.uid) || '';
  return !!docUid && !!myUid && docUid === myUid;
}

/**
 * Compute the arithmetic mean of review ratings, rounded to 1 decimal place.
 * Returns 0 for an empty array.
 * @param {Array<{rating: number}>} reviews
 * @returns {number}
 */
export function computeAverageRating(reviews) {
  if (!reviews || reviews.length === 0) {
    return 0;
  }
  const sum = reviews.reduce((acc, review) => acc + review.rating, 0);
  return Math.round((sum / reviews.length) * 10) / 10;
}

/**
 * Submit (or update) a review for a book.
 * Uses composite document ID `{bookId}_{displayNameLower}` for upsert.
 * @param {import('firebase/firestore').Firestore} db
 * @param {string} bookId
 * @param {string} displayName
 * @param {number} rating - Integer 1-5
 * @param {string} text - 1-1000 characters
 * @param {string} [uid] the live Firebase uid, when the caller has one. Used
 *   ONLY to retire the account-keyed TBR entry (see clearTbrForRating). The
 *   review document itself is still display-name keyed — that is a separate
 *   store with its own 884-document population, and moving it was not ordered.
 *   Optional so every existing caller (club.html, club-read.html) keeps
 *   working unchanged while it clears only the legacy entry.
 * @returns {Promise<{success: boolean, error?: string}>}
 */
export async function submitReview(db, bookId, displayName, rating, text, uid) {
  if (typeof rating !== 'number' || rating < 0.5 || rating > 5 || (rating * 2) % 1 !== 0) {
    return { success: false, error: 'Rating must be between 0.5 and 5 in half-star increments.' };
  }
  if (text && text.length > 1000) {
    return { success: false, error: 'Review text must be 1000 characters or fewer.' };
  }

  const docId = `${bookId}_${displayName.toLowerCase()}`;
  const reviewRef = doc(db, col('reviews'), docId);

  let existed = false;
  let succeeded = false; // the shadow report's outcome bit — see the finally
  try {
    const existingDoc = await getDoc(reviewRef);
    existed = existingDoc.exists();
    const data = {
      bookId,
      displayName,
      rating,
      text,
      updatedAt: serverTimestamp(),
    };
    if (!existed) {
      data.createdAt = serverTimestamp();
    }
    await setDoc(reviewRef, data, { merge: true });

    // A rating is evidence the book was read, so it settles the intention the
    // person's TBR entry recorded — retire it NOW instead of waiting for the
    // library catalog's next sweep (cross-catalog TBR, tbr.md §5–§6). Only
    // reachable when the write above succeeded; it can never fail a saved
    // review, because clearTbrForRating swallows its own errors.
    await clearTbrForRating(db, bookId, displayName, uid);

    succeeded = true;
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  } finally {
    // Phase 1 shadow telemetry (fire-and-forget, cannot affect the write —
    // see gate-shadow.js). submit/update are wired to measure Phase 5's
    // tokenless population, not a role gate.
    reportGate(existed ? 'review.update' : 'review.submit', { succeeded });
  }
}

/**
 * Retire the to-be-read intention a rating settles: delete this person's
 * entry for the book from the shared `readingLists` store — the SAME delete
 * the modal's `✓ To Be Read` button performs when it is toggled off, so the
 * button falls back to `📋 Add to TBR` on its next render.
 *
 * ⚠️ ONE ID SINCE 2026-08-18. This used to clear the legacy
 * `{displayNameLower}_{bookId}` as well, because 53 documents were still filed
 * that way. They were reassigned, the collection measured 0 uid-less, and both
 * the legacy id and its Firestore rule were removed the same day — so a second
 * delete would now only ever be refused. See the removal note above
 * `ownsReadingListDoc`.
 *
 * ⚠️ The reading-list id is the REVERSE of a review's `{bookId}_{name}` — that
 * much is unchanged and still deliberate (tbr.md §2). What changed on
 * 2026-08-18 is the left-hand half: an account, not a name. See
 * `readingListDocId`.
 *
 * ⚠️ NO uid MEANS NO CLEAR, and that is a real behaviour, not an oversight.
 * A caller with no live session cannot own a reading-list entry any more, so
 * there is nothing of theirs to retire. Silently deleting by name instead
 * would reach into whoever shares that name — the exact bug the account
 * migration was ordered to fix.
 *
 * Non-fatal by design, on both counts that matter:
 *  - it never throws, so a rating that saved is never reported as failed
 *    because a to-read entry could not be deleted;
 *  - deleting an absent document is a no-op in Firestore, so a rating EDIT,
 *    a re-submit, or a book that was never on the list all run harmlessly.
 *
 * A rules refusal is reported in words, never as a bare code.
 *
 * @param {import('firebase/firestore').Firestore} db
 * @param {string} bookId
 * @param {string} displayName kept for the signature every caller already
 *   passes; the id no longer derives from it.
 * @param {string} [uid] the live Firebase uid. Without one there is nothing
 *   this caller could own, and nothing is deleted.
 * @returns {Promise<{cleared: boolean, error?: string}>}
 */
export async function clearTbrForRating(db, bookId, displayName, uid) {
  if (!uid) return { cleared: true };
  try {
    await deleteDoc(doc(db, col('readingLists'), readingListDocId(uid, bookId)));
  } catch (e) {
    return { cleared: false, error: describeActionError(e) };
  }
  return { cleared: true };
}

/**
 * Remove a review — SITE ADMIN ONLY, and rules-enforced (three-tier model,
 * 2026-08-14): firestore.rules allows a /reviews delete only when the
 * caller's live Firebase uid holds site_roles role 'admin'. Everyone else
 * (moderators included — the owner scoped their sweep to clubs) gets
 * PERMISSION_DENIED, so this function is safe to ship in a public module:
 * the rule, not the UI, is the control. Doc id is the same composite
 * submitReview writes: `{bookId}_{displayNameLower}`.
 */
export async function deleteReview(db, bookId, displayName) {
  const docId = `${bookId}_${(displayName || '').toLowerCase()}`;
  let succeeded = false; // the shadow report's outcome bit — see the finally
  try {
    await deleteDoc(doc(db, col('reviews'), docId));
    succeeded = true;
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the site admin role' }) };
  } finally {
    // ⚠️ This surface is ALREADY rules-enforced (admin-only), so a non-admin
    // reaching here fails with PERMISSION_DENIED — succeeded:false, and the
    // gate would_deny is the gate AGREEING with today's rules, not a
    // regression. That distinction is the whole point of the outcome bit
    // (soak pack §6 names review.delete as the exact case).
    reportGate('review.delete', { succeeded });
  }
}

/**
 * Render a star rating as an accessible HTML string.
 * Supports half-star increments using CSS clip for half-filled stars.
 * @param {number} rating - 0.5 to 5 in 0.5 increments
 * @returns {string} HTML string with star display and aria-label
 */
export function renderStars(rating) {
  let html = '';
  for (let i = 1; i <= 5; i++) {
    if (rating >= i) {
      html += '<span class="star star-full">★</span>';
    } else if (rating >= i - 0.5) {
      html += '<span class="star star-half"><span class="star-half-fill">★</span>☆</span>';
    } else {
      html += '<span class="star star-empty">☆</span>';
    }
  }
  return `<span class="stars" aria-label="Rating: ${rating} out of 5 stars">${html}</span>`;
}

/**
 * Fetch all reviews for a given book, sorted newest first.
 * Returns an empty array on error (graceful degradation).
 * @param {import('firebase/firestore').Firestore} db
 * @param {string} bookId
 * @returns {Promise<Array<{bookId: string, displayName: string, rating: number, text: string, createdAt: any, updatedAt: any}>>}
 */
export async function getReviews(db, bookId) {
  try {
    // Fetch all reviews and filter client-side to avoid Firestore index requirements
    const snapshot = await getDocs(collection(db, col('reviews')));
    const reviews = [];
    snapshot.docs.forEach(d => {
      const data = d.data();
      if (data.bookId === bookId) {
        reviews.push(data);
      }
    });
    // Sort by createdAt descending
    reviews.sort((a, b) => {
      const aTime = a.createdAt?.seconds || 0;
      const bTime = b.createdAt?.seconds || 0;
      return bTime - aTime;
    });
    return reviews;
  } catch (e) {
    console.error('[getReviews] Error fetching reviews for bookId:', bookId, e);
    return [];
  }
}

/**
 * Format a Firestore timestamp into a readable date string.
 * Handles Firestore Timestamp objects (with .toDate()), plain objects with .seconds, and Date instances.
 * @param {any} timestamp
 * @returns {string}
 */
export function formatDate(timestamp) {
  if (!timestamp) return '';
  if (typeof timestamp.toDate === 'function') {
    return timestamp.toDate().toLocaleDateString();
  }
  if (timestamp.seconds != null) {
    return new Date(timestamp.seconds * 1000).toLocaleDateString();
  }
  if (timestamp instanceof Date) {
    return timestamp.toLocaleDateString();
  }
  return '';
}

/**
 * Render the full review section into the given container element.
 * Displays average rating, review list, and review form (if logged in) or sign-in prompt.
 * @param {HTMLElement} containerEl
 * @param {import('firebase/firestore').Firestore} db
 * @param {string} bookId
 * @param {{ displayName: string } | null} session
 */
export async function renderReviewSection(containerEl, db, bookId, session) {
  containerEl.innerHTML = '';

  const wrapper = document.createElement('div');
  wrapper.className = 'review-section';

  // Loading state
  wrapper.innerHTML = '<p class="review-section__loading">Loading reviews…</p>';
  containerEl.appendChild(wrapper);

  let reviews;
  try {
    reviews = await getReviews(db, bookId);
  } catch (e) {
    wrapper.innerHTML = '<p class="review-section__error">Unable to load reviews.</p>';
    return;
  }

  wrapper.innerHTML = '';

  // Average rating
  const avgContainer = document.createElement('div');
  avgContainer.className = 'review-section__average';
  if (reviews.length > 0) {
    const avg = computeAverageRating(reviews);
    avgContainer.innerHTML = `${renderStars(Math.round(avg))} <span class="review-section__avg-text">${avg} out of 5 (${reviews.length} review${reviews.length !== 1 ? 's' : ''})</span>`;
  } else {
    avgContainer.innerHTML = '<p class="review-section__empty">No reviews yet.</p>';
  }
  wrapper.appendChild(avgContainer);

  // Review form or sign-in prompt
  if (session) {
    _renderReviewForm(wrapper, db, bookId, session.displayName, reviews);
  } else {
    const prompt = document.createElement('p');
    prompt.className = 'review-section__signin-prompt';
    prompt.textContent = 'Register or sign in to leave a review.';
    wrapper.appendChild(prompt);
  }

  // Review list
  const listEl = document.createElement('div');
  listEl.className = 'review-section__list';
  _renderReviewList(listEl, reviews);
  wrapper.appendChild(listEl);
}

/**
 * Render the list of reviews into the given element.
 * @param {HTMLElement} listEl
 * @param {Array} reviews
 */
function _renderReviewList(listEl, reviews) {
  listEl.innerHTML = '';
  for (const review of reviews) {
    const item = document.createElement('div');
    item.className = 'review-item';

    const header = document.createElement('div');
    header.className = 'review-item__header';
    header.innerHTML = `${renderStars(review.rating)} <span class="review-item__author">${_escapeHtml(review.displayName)}</span> <span class="review-item__date">${formatDate(review.createdAt)}</span>`;
    item.appendChild(header);

    const body = document.createElement('p');
    body.className = 'review-item__text';
    body.textContent = review.text;
    item.appendChild(body);

    listEl.appendChild(item);
  }
}

/**
 * Render the review submission form.
 * @param {HTMLElement} parentEl
 * @param {import('firebase/firestore').Firestore} db
 * @param {string} bookId
 * @param {string} displayName
 * @param {Array} reviews - current reviews list for live update
 */
function _renderReviewForm(parentEl, db, bookId, displayName, reviews) {
  // Check if user already has a review for this book
  const existingReview = reviews.find(
    (r) => r.displayName.toLowerCase() === displayName.toLowerCase()
  );

  // If editing, show a pencil icon that expands the form
  if (existingReview) {
    const editWrapper = document.createElement('div');
    editWrapper.className = 'review-form__edit-wrapper';

    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'review-form__edit-btn';
    editBtn.innerHTML = '✏️';
    editBtn.setAttribute('aria-label', 'Edit your review');
    editBtn.title = 'Edit your review';

    const formContainer = document.createElement('div');
    formContainer.style.display = 'none';
    formContainer.style.width = '100%';

    editBtn.addEventListener('click', () => {
      const isVisible = formContainer.style.display !== 'none';
      formContainer.style.display = isVisible ? 'none' : '';
    });

    editWrapper.appendChild(editBtn);
    editWrapper.appendChild(formContainer);
    parentEl.appendChild(editWrapper);

    _buildReviewForm(formContainer, db, bookId, displayName, reviews, existingReview, parentEl);
  } else {
    _buildReviewForm(parentEl, db, bookId, displayName, reviews, null, parentEl);
  }
}

function _buildReviewForm(containerEl, db, bookId, displayName, reviews, existingReview, rootEl) {
  const form = document.createElement('form');
  form.className = 'review-form';
  form.addEventListener('submit', (e) => e.preventDefault());

  // Star selector
  const starSelector = document.createElement('div');
  starSelector.className = 'review-form__stars';
  starSelector.setAttribute('role', 'radiogroup');
  starSelector.setAttribute('aria-label', 'Select a rating');
  let selectedRating = existingReview ? existingReview.rating : 0;

  for (let i = 1; i <= 5; i++) {
    const star = document.createElement('span');
    star.className = 'review-form__star';
    star.textContent = '☆';
    star.setAttribute('role', 'radio');
    star.setAttribute('aria-checked', 'false');
    star.setAttribute('aria-label', `${i} star${i !== 1 ? 's' : ''}`);
    star.setAttribute('tabindex', '0');
    star.dataset.value = i;

    star.addEventListener('click', () => {
      selectedRating = i;
      _updateStarSelection(starSelector, i);
    });
    star.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        selectedRating = i;
        _updateStarSelection(starSelector, i);
      }
    });

    starSelector.appendChild(star);
  }
  form.appendChild(starSelector);

  if (existingReview) {
    _updateStarSelection(starSelector, existingReview.rating);
  }

  // Text input
  const textArea = document.createElement('textarea');
  textArea.className = 'review-form__text';
  textArea.setAttribute('aria-label', 'Review text');
  textArea.setAttribute('maxlength', '1000');
  textArea.setAttribute('placeholder', 'Write your review…');
  textArea.rows = 4;
  if (existingReview) {
    textArea.value = existingReview.text;
  }
  form.appendChild(textArea);

  // Character count
  const charCount = document.createElement('span');
  charCount.className = 'review-form__char-count';
  charCount.textContent = `${textArea.value.length}/1000`;
  textArea.addEventListener('input', () => {
    charCount.textContent = `${textArea.value.length}/1000`;
  });
  form.appendChild(charCount);

  // Error area
  const errorEl = document.createElement('span');
  errorEl.className = 'review-form__error';
  errorEl.setAttribute('role', 'alert');
  errorEl.style.display = 'none';
  form.appendChild(errorEl);

  // Submit button
  const submitBtn = document.createElement('button');
  submitBtn.type = 'button';
  submitBtn.className = existingReview ? 'review-form__edit-btn' : 'review-form__submit';
  if (existingReview) {
    submitBtn.textContent = '💾';
    submitBtn.title = 'Save changes';
    submitBtn.setAttribute('aria-label', 'Save changes');
  } else {
    submitBtn.textContent = 'Submit Review';
  }

  submitBtn.addEventListener('click', async () => {
    errorEl.style.display = 'none';
    errorEl.textContent = '';

    if (selectedRating < 1) {
      errorEl.textContent = 'Please select a star rating.';
      errorEl.style.display = '';
      return;
    }
    if (textArea.value.length < 1 || textArea.value.length > 1000) {
      errorEl.textContent = 'Review text must be between 1 and 1000 characters.';
      errorEl.style.display = '';
      return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = existingReview ? '⏳' : 'Submitting…';

    const result = await submitReview(db, bookId, displayName, selectedRating, textArea.value);

    if (result.success) {
      const newReview = {
        bookId,
        displayName,
        rating: selectedRating,
        text: textArea.value,
        createdAt: { seconds: Math.floor(Date.now() / 1000) },
        updatedAt: { seconds: Math.floor(Date.now() / 1000) },
      };

      const existingIdx = reviews.findIndex(
        (r) => r.displayName.toLowerCase() === displayName.toLowerCase()
      );
      if (existingIdx !== -1) {
        reviews[existingIdx] = newReview;
      } else {
        reviews.unshift(newReview);
      }

      // Re-render average
      const avgContainer = rootEl.querySelector('.review-section__average');
      if (avgContainer) {
        const avg = computeAverageRating(reviews);
        avgContainer.innerHTML = `${renderStars(Math.round(avg))} <span class="review-section__avg-text">${avg} out of 5 (${reviews.length} review${reviews.length !== 1 ? 's' : ''})</span>`;
      }

      // Re-render list
      const listEl = rootEl.querySelector('.review-section__list');
      if (listEl) {
        _renderReviewList(listEl, reviews);
      }

      submitBtn.disabled = false;
      if (existingReview) {
        submitBtn.textContent = '💾';
      } else {
        submitBtn.textContent = 'Submit Review';
      }

      // Collapse the edit form after successful update
      const editWrapper = containerEl.closest('.review-form__edit-wrapper');
      if (editWrapper) {
        containerEl.style.display = 'none';
      }
      return;
    } else {
      errorEl.textContent = result.error || 'Unable to submit review. Please try again.';
      errorEl.style.display = '';
    }

    submitBtn.disabled = false;
    submitBtn.textContent = existingReview ? '💾' : 'Submit Review';
  });

  form.appendChild(submitBtn);
  containerEl.appendChild(form);
}


/**
 * Update the visual state of the star selector.
 * @param {HTMLElement} starSelector
 * @param {number} rating
 */
function _updateStarSelection(starSelector, rating) {
  const stars = starSelector.querySelectorAll('.review-form__star');
  stars.forEach((star) => {
    const val = parseInt(star.dataset.value, 10);
    star.textContent = val <= rating ? '★' : '☆';
    star.setAttribute('aria-checked', val === rating ? 'true' : 'false');
  });
}

/**
 * Escape HTML special characters to prevent XSS.
 * @param {string} str
 * @returns {string}
 */
function _escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
