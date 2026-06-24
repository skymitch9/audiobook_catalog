// reviews.js — Book review system for the audiobook catalog
// ES module, browser-native (no build step)

import { doc, setDoc, getDoc, serverTimestamp, collection, getDocs } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';

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
 * @returns {Promise<{success: boolean, error?: string}>}
 */
export async function submitReview(db, bookId, displayName, rating, text) {
  if (typeof rating !== 'number' || rating < 0.5 || rating > 5 || (rating * 2) % 1 !== 0) {
    return { success: false, error: 'Rating must be between 0.5 and 5 in half-star increments.' };
  }
  if (typeof text !== 'string' || text.length < 1 || text.length > 1000) {
    return { success: false, error: 'Review text must be between 1 and 1000 characters.' };
  }

  const docId = `${bookId}_${displayName.toLowerCase()}`;
  const reviewRef = doc(db, 'reviews', docId);

  try {
    const existingDoc = await getDoc(reviewRef);
    const data = {
      bookId,
      displayName,
      rating,
      text,
      updatedAt: serverTimestamp(),
    };
    if (!existingDoc.exists()) {
      data.createdAt = serverTimestamp();
    }
    await setDoc(reviewRef, data, { merge: true });

    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/**
 * Render a star rating as an accessible HTML string.
 * Supports half-star increments using CSS half-star technique.
 * @param {number} rating - 0.5 to 5 in 0.5 increments
 * @returns {string} HTML string with star display and aria-label
 */
export function renderStars(rating) {
  let html = '';
  for (let i = 1; i <= 5; i++) {
    if (rating >= i) {
      html += '<span class="star star-full">★</span>';
    } else if (rating >= i - 0.5) {
      html += '<span class="star star-half">★</span>';
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
    const snapshot = await getDocs(collection(db, 'reviews'));
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
