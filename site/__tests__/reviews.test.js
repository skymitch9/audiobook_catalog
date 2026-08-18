// @vitest-environment jsdom
// Feature: book-reviews-and-user-identity, Property 13: Book ID derivation is deterministic
// Feature: book-reviews-and-user-identity, Property 10: Average rating computation
// Feature: book-reviews-and-user-identity, Property 6: Review submission round-trip
// Feature: book-reviews-and-user-identity, Property 7: Review input validation
// Feature: book-reviews-and-user-identity, Property 8: Review upsert
// Feature: book-reviews-and-user-identity, Property 9: Review fetch returns correct book's reviews in date order
// Feature: book-reviews-and-user-identity, Property 11: Review display contains all required fields
import { describe, it, expect, beforeEach, vi } from 'vitest';
// The Phase 1 shadow reporter (gate-shadow.js) fires fire-and-forget from
// the gated write paths under test; mock it so no test ever touches the
// network. Its own contract is pinned in gate-shadow.test.js.
vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

import * as fc from 'fast-check';

// --- In-memory Firestore mock ---
let mockStore = {};
let timestampCounter = 0;

vi.mock('firebase/firestore', () => {
  return {
    doc: (db, collectionName, id) => ({ _path: `${collectionName}/${id}` }),
    getDoc: async (ref) => {
      const data = mockStore[ref._path];
      return {
        exists: () => !!data,
        data: () => (data ? { ...data } : undefined),
      };
    },
    setDoc: async (ref, data, options) => {
      if (options && options.merge) {
        mockStore[ref._path] = { ...(mockStore[ref._path] || {}), ...data };
      } else {
        mockStore[ref._path] = { ...data };
      }
    },
    // Test seam: a doc seeded with __denyDelete simulates the firestore.rules
    // PERMISSION_DENIED every non-site-admin caller gets on /reviews delete.
    deleteDoc: async (ref) => {
      if (mockStore[ref._path] && mockStore[ref._path].__denyDelete) {
        const e = new Error('Missing or insufficient permissions.');
        e.code = 'permission-denied';
        throw e;
      }
      delete mockStore[ref._path];
    },
    serverTimestamp: () => ({ seconds: ++timestampCounter }),
    collection: (db, name) => ({ _collectionName: name }),
    query: (collectionRef, ...constraints) => ({
      _collectionName: collectionRef._collectionName,
      _constraints: constraints,
    }),
    where: (field, op, value) => ({ _type: 'where', field, op, value }),
    orderBy: (field, direction) => ({ _type: 'orderBy', field, direction }),
    getDocs: async (q) => {
      // Support both collection references (no _constraints) and query objects
      const collectionName = q._collectionName;
      const allDocs = Object.entries(mockStore)
        .filter(([key]) => key.startsWith(`${collectionName}/`))
        .map(([key, data]) => ({ id: key.split('/')[1], data: () => ({ ...data }) }));

      // If called with a query wrapper that has constraints, apply them
      let filtered = allDocs;
      if (q._constraints) {
        for (const c of q._constraints) {
          if (c._type === 'where') {
            filtered = filtered.filter((d) => {
              const val = d.data()[c.field];
              if (c.op === '==') return val === c.value;
              return true;
            });
          }
        }

        for (const c of q._constraints) {
          if (c._type === 'orderBy') {
            filtered.sort((a, b) => {
              const aVal = a.data()[c.field];
              const bVal = b.data()[c.field];
              const aSeconds = aVal && aVal.seconds != null ? aVal.seconds : 0;
              const bSeconds = bVal && bVal.seconds != null ? bVal.seconds : 0;
              return c.direction === 'desc' ? bSeconds - aSeconds : aSeconds - bSeconds;
            });
          }
        }
      }

      return { docs: filtered };
    },
  };
});

import { bookIdFromTitle, computeAverageRating, submitReview, getReviews, renderStars, renderReviewSection, formatDate, deleteReview, clearTbrForRating, readingListDocId, legacyReadingListDocId, isUidKeyedListId, ownsReadingListDoc } from '../reviews.js';
import { col } from '../fb-env.js';

const fakeDb = {};

// --- Generators ---
const alphaLowerChars = 'abcdefghijklmnopqrstuvwxyz0123456789'.split('');
const validBookId = fc
  .array(fc.constantFrom(...alphaLowerChars), { minLength: 1, maxLength: 20 })
  .map((arr) => arr.join(''));
const validDisplayName = fc
  .array(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'.split('')), {
    minLength: 2,
    maxLength: 20,
  })
  .map((arr) => arr.join(''));
const validRating = fc.integer({ min: 1, max: 5 });
const validReviewText = fc
  .array(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz '.split('')), { minLength: 1, maxLength: 50 })
  .map((arr) => arr.join(''));

// ---- Existing Property Tests (pure functions) ----

describe('Property 13: Book ID derivation is deterministic', () => {
  // **Validates: Requirements 4.1, 5.1**

  it('bookIdFromTitle returns the same output for the same input', () => {
    fc.assert(
      fc.property(fc.string(), (title) => {
        expect(bookIdFromTitle(title)).toBe(bookIdFromTitle(title));
      }),
      { numRuns: 100 }
    );
  });

  it('distinct non-trivial titles produce distinct book IDs', () => {
    const alphaNumChars = 'abcdefghijklmnopqrstuvwxyz0123456789'.split('');
    const alphaNum = fc
      .array(fc.constantFrom(...alphaNumChars), { minLength: 1, maxLength: 30 })
      .map((chars) => chars.join(''));

    fc.assert(
      fc.property(alphaNum, alphaNum, (a, b) => {
        fc.pre(a !== b);
        expect(bookIdFromTitle(a)).not.toBe(bookIdFromTitle(b));
      }),
      { numRuns: 100 }
    );
  });

  it('bookIdFromTitle output contains only lowercase alphanumeric and hyphens', () => {
    fc.assert(
      fc.property(fc.string(), (title) => {
        const id = bookIdFromTitle(title);
        expect(id).toMatch(/^[a-z0-9-]*$/);
      }),
      { numRuns: 100 }
    );
  });

  it('bookIdFromTitle output has no leading, trailing, or consecutive hyphens', () => {
    fc.assert(
      fc.property(
        fc.string({ minLength: 1 }).filter((s) => /[a-zA-Z0-9]/.test(s)),
        (title) => {
          const id = bookIdFromTitle(title);
          expect(id).not.toMatch(/^-/);
          expect(id).not.toMatch(/-$/);
          expect(id).not.toMatch(/--/);
        }
      ),
      { numRuns: 100 }
    );
  });
});

describe('Property 10: Average rating computation', () => {
  // **Validates: Requirements 5.3**

  it('returns arithmetic mean rounded to 1 decimal for any non-empty array of ratings 1-5', () => {
    const ratingsArb = fc.array(fc.integer({ min: 1, max: 5 }), { minLength: 1, maxLength: 50 });

    fc.assert(
      fc.property(ratingsArb, (ratings) => {
        const reviews = ratings.map((r) => ({ rating: r }));
        const result = computeAverageRating(reviews);
        const expectedMean = ratings.reduce((a, b) => a + b, 0) / ratings.length;
        const expected = Math.round(expectedMean * 10) / 10;
        expect(result).toBe(expected);
      }),
      { numRuns: 100 }
    );
  });

  it('returns 0 for an empty array', () => {
    expect(computeAverageRating([])).toBe(0);
  });

  it('returns the rating itself for a single-element array', () => {
    fc.assert(
      fc.property(fc.integer({ min: 1, max: 5 }), (rating) => {
        expect(computeAverageRating([{ rating }])).toBe(rating);
      }),
      { numRuns: 100 }
    );
  });
});

// ---- New Property Tests (require Firestore mock) ----

describe('Property 6: Review submission round-trip', () => {
  // **Validates: Requirements 4.1**

  beforeEach(() => {
    mockStore = {};
    timestampCounter = 0;
  });

  it('submit then fetch returns review with matching fields', async () => {
    await fc.assert(
      fc.asyncProperty(validBookId, validDisplayName, validRating, validReviewText, async (bookId, name, rating, text) => {
        mockStore = {};
        timestampCounter = 0;

        const result = await submitReview(fakeDb, bookId, name, rating, text);
        expect(result.success).toBe(true);

        const reviews = await getReviews(fakeDb, bookId);
        expect(reviews.length).toBe(1);
        expect(reviews[0].bookId).toBe(bookId);
        expect(reviews[0].displayName).toBe(name);
        expect(reviews[0].rating).toBe(rating);
        expect(reviews[0].text).toBe(text);
      }),
      { numRuns: 100 }
    );
  });
});

describe('Property 7: Review input validation', () => {
  // **Validates: Requirements 4.2, 4.3**

  beforeEach(() => {
    mockStore = {};
    timestampCounter = 0;
  });

  it('rejects rating outside 1-5', async () => {
    const invalidRating = fc.oneof(
      fc.integer({ max: 0 }),
      fc.integer({ min: 6 }),
      fc.double().filter((n) => !Number.isInteger(n))
    );

    await fc.assert(
      fc.asyncProperty(validBookId, validDisplayName, invalidRating, validReviewText, async (bookId, name, rating, text) => {
        const result = await submitReview(fakeDb, bookId, name, rating, text);
        expect(result.success).toBe(false);
        expect(result.error).toBeDefined();
      }),
      { numRuns: 100 }
    );
  });

  it('rejects text over 1000 chars (empty text is allowed — rating-only reviews)', async () => {
    const tooLongText = fc
      .array(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz'.split('')), { minLength: 1001, maxLength: 1050 })
      .map((arr) => arr.join(''));

    await fc.assert(
      fc.asyncProperty(validBookId, validDisplayName, validRating, tooLongText, async (bookId, name, rating, text) => {
        const result = await submitReview(fakeDb, bookId, name, rating, text);
        expect(result.success).toBe(false);
        expect(result.error).toBeDefined();
      }),
      { numRuns: 100 }
    );
  });
});

describe('Property 8: Review upsert', () => {
  // **Validates: Requirements 4.4**

  beforeEach(() => {
    mockStore = {};
    timestampCounter = 0;
  });

  it('submitting twice for same user+book results in one review with second content', async () => {
    await fc.assert(
      fc.asyncProperty(
        validBookId,
        validDisplayName,
        validRating,
        validReviewText,
        validRating,
        validReviewText,
        async (bookId, name, rating1, text1, rating2, text2) => {
          mockStore = {};
          timestampCounter = 0;

          const first = await submitReview(fakeDb, bookId, name, rating1, text1);
          expect(first.success).toBe(true);

          const second = await submitReview(fakeDb, bookId, name, rating2, text2);
          expect(second.success).toBe(true);

          const reviews = await getReviews(fakeDb, bookId);
          expect(reviews.length).toBe(1);
          expect(reviews[0].rating).toBe(rating2);
          expect(reviews[0].text).toBe(text2);
          expect(reviews[0].displayName).toBe(name);
        }
      ),
      { numRuns: 100 }
    );
  });
});

describe('Property 9: Review fetch returns correct book\'s reviews in date order', () => {
  // **Validates: Requirements 5.1, 5.5**

  beforeEach(() => {
    mockStore = {};
    timestampCounter = 0;
  });

  it('only returns reviews matching the requested bookId, sorted newest first', async () => {
    // Generate two distinct bookIds and a set of reviews spread across them
    const distinctBookIds = fc
      .tuple(validBookId, validBookId)
      .filter(([a, b]) => a !== b);

    const reviewEntry = fc.tuple(validDisplayName, validRating, validReviewText);
    const reviewList = fc.array(reviewEntry, { minLength: 1, maxLength: 5 });

    await fc.assert(
      fc.asyncProperty(distinctBookIds, reviewList, reviewList, async ([bookIdA, bookIdB], reviewsA, reviewsB) => {
        mockStore = {};
        timestampCounter = 0;

        // Submit reviews for bookA — use index to make display names unique per book
        for (let i = 0; i < reviewsA.length; i++) {
          const [, rating, text] = reviewsA[i];
          const uniqueName = `usera${i}`;
          await submitReview(fakeDb, bookIdA, uniqueName, rating, text);
        }

        // Submit reviews for bookB
        for (let i = 0; i < reviewsB.length; i++) {
          const [, rating, text] = reviewsB[i];
          const uniqueName = `userb${i}`;
          await submitReview(fakeDb, bookIdB, uniqueName, rating, text);
        }

        // Fetch reviews for bookA only
        const fetchedA = await getReviews(fakeDb, bookIdA);
        expect(fetchedA.length).toBe(reviewsA.length);
        for (const r of fetchedA) {
          expect(r.bookId).toBe(bookIdA);
        }

        // Verify sorted newest first (descending createdAt.seconds)
        for (let i = 1; i < fetchedA.length; i++) {
          const prevTs = fetchedA[i - 1].createdAt?.seconds ?? 0;
          const currTs = fetchedA[i].createdAt?.seconds ?? 0;
          expect(prevTs).toBeGreaterThanOrEqual(currTs);
        }

        // Fetch reviews for bookB only
        const fetchedB = await getReviews(fakeDb, bookIdB);
        expect(fetchedB.length).toBe(reviewsB.length);
        for (const r of fetchedB) {
          expect(r.bookId).toBe(bookIdB);
        }
      }),
      { numRuns: 100 }
    );
  });
});

describe('Property 12: Star rendering produces correct accessible output', () => {
  // **Validates: Requirements 8.2**

  it('produces exactly `rating` filled stars and `5 - rating` empty stars with correct aria-label', () => {
    fc.assert(
      fc.property(fc.integer({ min: 1, max: 5 }), (rating) => {
        const html = renderStars(rating);

        // Count filled (★ U+2605) and empty (☆ U+2606) stars
        const filledCount = (html.match(/★/g) || []).length;
        const emptyCount = (html.match(/☆/g) || []).length;

        expect(filledCount).toBe(rating);
        expect(emptyCount).toBe(5 - rating);
        expect(filledCount + emptyCount).toBe(5);

        // Verify aria-label contains the correct rating
        expect(html).toContain(`aria-label="Rating: ${rating} out of 5 stars"`);
      }),
      { numRuns: 100 }
    );
  });
});

describe('Property 11: Review display contains all required fields', () => {
  // **Validates: Requirements 5.2**

  beforeEach(() => {
    mockStore = {};
    timestampCounter = 0;
  });

  it('rendered HTML contains star rating, review text, display name, and formatted date', async () => {
    const validTimestampSeconds = fc.integer({ min: 946684800, max: 1893456000 }); // 2000-01-01 to 2030-01-01

    await fc.assert(
      fc.asyncProperty(
        validBookId,
        validDisplayName,
        validRating,
        validReviewText,
        validTimestampSeconds,
        async (bookId, displayName, rating, text, tsSeconds) => {
          mockStore = {};
          timestampCounter = 0;

          // Submit a review so the mock store has data
          const result = await submitReview(fakeDb, bookId, displayName, rating, text);
          expect(result.success).toBe(true);

          // Patch the createdAt timestamp to a known value for date verification.
          // col() resolves to reviews_dev under jsdom (localhost = dev lane).
          const docKey = `${col('reviews')}/${bookId}_${displayName.toLowerCase()}`;
          mockStore[docKey].createdAt = { seconds: tsSeconds };

          // Render the review section into a container
          const container = document.createElement('div');
          await renderReviewSection(container, fakeDb, bookId, null);

          const html = container.innerHTML;

          // Verify star characters are present (★ or ☆)
          expect(html).toMatch(/[★☆]/);

          // Verify the review text is present
          expect(html).toContain(text);

          // Verify the display name is present
          expect(html).toContain(displayName);

          // Verify a formatted date string is present
          const expectedDate = formatDate({ seconds: tsSeconds });
          expect(expectedDate.length).toBeGreaterThan(0);
          expect(html).toContain(expectedDate);
        }
      ),
      { numRuns: 100 }
    );
  });
});

describe('deleteReview — site-ADMIN-only removal (three-tier model)', () => {
  it('deletes the composite-id doc when rules allow (site admin)', async () => {
    await submitReview(fakeDb, 'some-book', 'Jane Doe', 4, 'fine');
    const key = `${col('reviews')}/some-book_jane doe`;
    expect(mockStore[key]).toBeDefined();

    const r = await deleteReview(fakeDb, 'some-book', 'Jane Doe');
    expect(r.success).toBe(true);
    expect(mockStore[key]).toBeUndefined();
  });

  it('maps the rules PERMISSION_DENIED onto an honest admin-only error', async () => {
    // firestore.rules: allow delete: if isSiteAdmin() — everyone else
    // (moderators included; their sweep is clubs-only) is denied. The
    // __denyDelete seam makes the mock behave like Firestore does then.
    const key = `${col('reviews')}/some-book_bob`;
    mockStore[key] = { bookId: 'some-book', displayName: 'Bob', rating: 1, __denyDelete: true };

    const r = await deleteReview(fakeDb, 'some-book', 'Bob');
    expect(r.success).toBe(false);
    expect(r.error).toMatch(/site admin/i);
    expect(mockStore[key]).toBeDefined(); // nothing was removed
  });
});

// ==================== Phase 1 shadow reports (auth migration §4) ====================
//
// reportGate is MOCKED at the top of this file, which is itself half the
// contract: the reviews module only ever calls it fire-and-forget, so a
// mock (or a dead network) changes no outcome asserted above.

describe('Phase 1 shadow reports — review writes', () => {
  let reportGate;
  beforeEach(async () => {
    ({ reportGate } = await import('../gate-shadow.js'));
    reportGate.mockClear();
    mockStore = {};
  });

  it('a NEW review reports review.submit; editing an existing one reports review.update', async () => {
    const r1 = await submitReview(fakeDb, 'dune', 'Jane', 4, 'great');
    expect(r1.success).toBe(true);
    expect(reportGate).toHaveBeenCalledTimes(1);
    expect(reportGate).toHaveBeenCalledWith('review.submit', { succeeded: true });

    reportGate.mockClear();
    const r2 = await submitReview(fakeDb, 'dune', 'Jane', 5, 'even better');
    expect(r2.success).toBe(true);
    expect(reportGate).toHaveBeenCalledTimes(1);
    expect(reportGate).toHaveBeenCalledWith('review.update', { succeeded: true });
  });

  it('a validation reject never reaches Firestore and reports nothing', async () => {
    const r = await submitReview(fakeDb, 'dune', 'Jane', 99, 'bad rating');
    expect(r.success).toBe(false);
    expect(reportGate).not.toHaveBeenCalled();
  });

  it('deleteReview reports review.delete on success', async () => {
    mockStore[`${col('reviews')}/dune_jane`] = { bookId: 'dune', displayName: 'Jane', rating: 4 };
    const r = await deleteReview(fakeDb, 'dune', 'Jane');
    expect(r.success).toBe(true);
    expect(reportGate).toHaveBeenCalledTimes(1);
    expect(reportGate).toHaveBeenCalledWith('review.delete', { succeeded: true });
  });

  it('deleteReview reports review.delete even when rules DENY — the denial is the measurement, and the report changes nothing about the refusal', async () => {
    const key = `${col('reviews')}/dune_jane`;
    mockStore[key] = { bookId: 'dune', displayName: 'Jane', rating: 4, __denyDelete: true };
    const r = await deleteReview(fakeDb, 'dune', 'Jane');
    expect(r.success).toBe(false); // outcome identical to the pre-shadow behaviour
    expect(mockStore[key]).toBeDefined();
    expect(reportGate).toHaveBeenCalledTimes(1);
    // ⚠️ THE CASE SOAK BLOCKER 4 NAMED. review.delete is already rules-enforced
    // (admin-only), so this refusal is the gate AGREEING with today's rules —
    // not a regression. succeeded:false is what lets the next evidence pack
    // tell the two apart; without it this line was byte-identical to the
    // successful delete above, and every legitimate refusal read as a false
    // alarm.
    expect(reportGate).toHaveBeenCalledWith('review.delete', { succeeded: false });
  });
});

// ==================== TBR instant-clear (cross-catalog TBR, tbr.md §6) ====================
//
// Rating a book is evidence you read it, so it settles the intention your TBR
// entry recorded. Before this, the audiobook site's own `✓ To Be Read` button
// went on claiming the book until the library catalog's next sweep deleted the
// entry; now submitReview retires it at the moment of the rating, with the
// SAME delete the button's own toggle performs.
//
// The shared store is `readingLists` (lane-suffixed by col()), and its
// document id is `{displayNameLower}_{bookId}` — the REVERSE of a review's
// `{bookId}_{displayNameLower}`. That reversal is the single most expensive
// thing to get wrong here, so it is asserted directly rather than implied.

const rlKey = (name, bookId) => `${col('readingLists')}/${name.toLowerCase()}_${bookId}`;
const reviewKey = (bookId, name) => `${col('reviews')}/${bookId}_${name.toLowerCase()}`;

describe('TBR instant-clear on rating', () => {
  beforeEach(() => {
    mockStore = {};
    timestampCounter = 0;
  });

  it('a successful rating deletes that person entry from readingLists', async () => {
    mockStore[rlKey('Jane Doe', 'dune')] = {
      displayName: 'Jane Doe', bookId: 'dune', bookTitle: 'Dune', status: 'tbr',
    };

    const r = await submitReview(fakeDb, 'dune', 'Jane Doe', 4, 'excellent');

    expect(r.success).toBe(true);
    expect(mockStore[rlKey('Jane Doe', 'dune')]).toBeUndefined();
    expect(mockStore[reviewKey('dune', 'Jane Doe')]).toBeDefined(); // the review still landed
  });

  it('builds the reading-list id in the REVERSE order to a review id, and never the review order', async () => {
    // tbr.md §2: the two ids are deliberately mirrored. Writing the review
    // order into readingLists would file a second document beside somebody's
    // real entry and their button would disagree with this site forever.
    mockStore[rlKey('Jane', 'dune')] = { displayName: 'Jane', bookId: 'dune', status: 'tbr' };
    const wrongOrder = `${col('readingLists')}/dune_jane`;
    mockStore[wrongOrder] = { __decoy: true };

    await submitReview(fakeDb, 'dune', 'Jane', 5, 'great');

    expect(rlKey('Jane', 'dune')).toBe(`${col('readingLists')}/jane_dune`);
    expect(rlKey('Jane', 'dune')).not.toBe(reviewKey('dune', 'Jane'));
    expect(mockStore[`${col('readingLists')}/jane_dune`]).toBeUndefined(); // the real entry went
    expect(mockStore[wrongOrder]).toBeDefined();                          // the decoy did NOT
  });

  it('clears only the rater own entry — another person TBR for the same book survives', async () => {
    mockStore[rlKey('Jane', 'dune')] = { displayName: 'Jane', bookId: 'dune', status: 'tbr' };
    mockStore[rlKey('Bob', 'dune')] = { displayName: 'Bob', bookId: 'dune', status: 'tbr' };

    await submitReview(fakeDb, 'dune', 'Jane', 3, 'fine');

    expect(mockStore[rlKey('Jane', 'dune')]).toBeUndefined();
    expect(mockStore[rlKey('Bob', 'dune')]).toBeDefined();
  });

  it('clears only the rated book — the same person other TBR entries survive', async () => {
    mockStore[rlKey('Jane', 'dune')] = { displayName: 'Jane', bookId: 'dune', status: 'tbr' };
    mockStore[rlKey('Jane', 'neuromancer')] = { displayName: 'Jane', bookId: 'neuromancer', status: 'tbr' };

    await submitReview(fakeDb, 'dune', 'Jane', 3, 'fine');

    expect(mockStore[rlKey('Jane', 'dune')]).toBeUndefined();
    expect(mockStore[rlKey('Jane', 'neuromancer')]).toBeDefined();
  });

  it('a book that was never on the list is a no-op, and a rating EDIT re-runs harmlessly', async () => {
    const first = await submitReview(fakeDb, 'dune', 'Jane', 4, 'good');
    expect(first.success).toBe(true);

    // Nothing was on the list; the re-rate deletes an absent document again.
    const second = await submitReview(fakeDb, 'dune', 'Jane', 5, 'better on a reread');
    expect(second.success).toBe(true);
    expect(mockStore[rlKey('Jane', 'dune')]).toBeUndefined();
    expect(mockStore[reviewKey('dune', 'Jane')].rating).toBe(5);
  });

  it('a FAILED review write clears nothing — a rejected rating settles no intention', async () => {
    mockStore[rlKey('Jane', 'dune')] = { displayName: 'Jane', bookId: 'dune', status: 'tbr' };

    const r = await submitReview(fakeDb, 'dune', 'Jane', 99, 'out of range');

    expect(r.success).toBe(false);
    expect(mockStore[rlKey('Jane', 'dune')]).toBeDefined(); // still on her list
  });

  it('a REFUSED reading-list delete never turns a saved review into a failure', async () => {
    // The __denyDelete seam makes the mock refuse like Firestore would. The
    // review must still report success: the rating is the thing the person
    // asked for, and a stale button is not worth reporting it as lost.
    mockStore[rlKey('Jane', 'dune')] = {
      displayName: 'Jane', bookId: 'dune', status: 'tbr', __denyDelete: true,
    };

    const r = await submitReview(fakeDb, 'dune', 'Jane', 4, 'good');

    expect(r.success).toBe(true);
    expect(r.error).toBeUndefined();
    expect(mockStore[reviewKey('dune', 'Jane')]).toBeDefined();
  });

  it('clearTbrForRating reports the refusal in words, never a bare code', async () => {
    mockStore[rlKey('Jane', 'dune')] = { displayName: 'Jane', status: 'tbr', __denyDelete: true };

    const r = await clearTbrForRating(fakeDb, 'dune', 'Jane');

    expect(r.cleared).toBe(false);
    expect(typeof r.error).toBe('string');
    expect(r.error.length).toBeGreaterThan(0);
    expect(r.error).not.toMatch(/^permission-denied$/);
  });

  it('is callable on its own and folds the display name case, like the button does', async () => {
    mockStore[rlKey('Jane Doe', 'dune')] = { displayName: 'Jane Doe', status: 'tbr' };

    const r = await clearTbrForRating(fakeDb, 'dune', 'JANE DOE');

    expect(r.cleared).toBe(true);
    expect(mockStore[`${col('readingLists')}/jane doe_dune`]).toBeUndefined();
  });

  it('writes to the lane-suffixed collection, so the dev lane can never clear a prod entry', () => {
    // col() is the one lane switch (fb-env.js); asserting the key is built
    // through it is what keeps this delete on the same lane as the button.
    expect(rlKey('Jane', 'dune').startsWith(`${col('readingLists')}/`)).toBe(true);
  });
});

// ==================== TBR keyed to the ACCOUNT (2026-08-18) ====================
//
// Owner's order, verbatim: "Make tbr keyed to account".
//
// The id was `{displayNameLower}_{bookId}`, so two members who chose the same
// display name shared ONE document per book: each saw the other's intentions on
// their own list and could delete them. A display name identifies nobody, and
// firestore.rules says in its own header that no rule can bind one to a person
// — so the fix had to be the key, and 234 live documents had to move with it
// (scripts/migrate_tbr_to_uid.py).
//
// ⚠️ 53 of them could NOT move: their owner is a retired v1 passphrase account
// with no Firebase uid to key to, and the migration refuses to guess an owner
// for somebody's reading list. So this collection runs TWO models at once, and
// the tests below have to pin both — the account lane AND the legacy lane that
// keeps those 53 reachable.

const uidKey = (uid, bookId) => `${col('readingLists')}/${uid}_${bookId}`;

// A real Firebase uid: 28 characters of [A-Za-z0-9]. The length is load-bearing
// (firestore.rules `tbrIdIsUidKeyed` reads it), so the fixtures use real shapes.
const UID_A = 'tX912OtdBheUhIe4kLDsGuJwE3D2'.slice(0, 28);
const UID_B = 'jjuEFDx0RehdFbvYDe1djZFbU5s2'.slice(0, 28);

describe('readingListDocId — the account key', () => {
  it('is `{uid}_{bookId}`, matching positionDocId exactly', () => {
    expect(readingListDocId(UID_A, 'dune')).toBe(`${UID_A}_dune`);
  });

  it('is still the REVERSE of a review id — that much did not change', () => {
    // tbr.md §2. The left-hand half became an account; the ORDER is untouched,
    // and building a review-ordered id would still file a stray document.
    expect(readingListDocId(UID_A, 'dune')).not.toBe(`dune_${UID_A}`);
  });

  it('does NOT fold case, because a uid is case-sensitive and a name was not', () => {
    // ⚠️ The old key lowercased, because display names had to match loosely.
    // A uid must not: `tX912…` and `tx912…` are different accounts as far as
    // any comparison here is concerned, and folding one would build an id that
    // matches nothing and silently loses the entry.
    expect(readingListDocId('AbC', 'dune')).toBe('AbC_dune');
    expect(readingListDocId('AbC', 'dune')).not.toBe(readingListDocId('abc', 'dune'));
  });

  it('never collides with a legacy id for the same book', () => {
    // The two lanes coexist in one collection; if they could collide, the
    // migration would overwrite the very documents it was preserving.
    expect(readingListDocId(UID_A, 'dune')).not.toBe(legacyReadingListDocId('Skylar', 'dune'));
  });
});

describe('legacyReadingListDocId — read-only, and NOT dead code', () => {
  it('still builds the pre-migration id, case-folded as it always was', () => {
    expect(legacyReadingListDocId('Jane Doe', 'dune')).toBe('jane doe_dune');
    expect(legacyReadingListDocId('JANE DOE', 'dune')).toBe('jane doe_dune');
  });

  it('survives a missing name rather than throwing', () => {
    expect(legacyReadingListDocId(undefined, 'dune')).toBe('_dune');
  });
});

describe('isUidKeyedListId — the predicate firestore.rules mirrors', () => {
  // ⚠️ If this and `tbrIdIsUidKeyed` in firestore.rules ever disagree, the hole
  // re-opens silently: the client would write to one lane and the rules would
  // police the other.
  it('reads a 28-character alphanumeric head as an account', () => {
    expect(isUidKeyedListId(`${UID_A}_dune`)).toBe(true);
  });

  it('reads every real legacy id as legacy', () => {
    // Every display name measured in the live population, 2026-08-18.
    for (const name of ['skylar', 'samantha hardman', 'jamie jeremiah lievertz',
                        'divaelf', 'skylar mitchell (skymitch9)', 'remy']) {
      expect(isUidKeyedListId(`${name}_dune`)).toBe(false);
    }
  });

  it('rejects a head of the right length that is not alphanumeric', () => {
    expect(isUidKeyedListId(`${'a'.repeat(27)}-_dune`)).toBe(false);
  });

  it('rejects heads of the wrong length', () => {
    expect(isUidKeyedListId(`${'a'.repeat(27)}_dune`)).toBe(false);
    expect(isUidKeyedListId(`${'a'.repeat(29)}_dune`)).toBe(false);
  });

  it('is total — a malformed or empty id answers false, never throws', () => {
    expect(isUidKeyedListId('')).toBe(false);
    expect(isUidKeyedListId(undefined)).toBe(false);
    expect(isUidKeyedListId('nounderscore')).toBe(false);
  });
});

describe('ownsReadingListDoc — whose list is this?', () => {
  it('matches an account-keyed document by uid', () => {
    expect(ownsReadingListDoc({ uid: UID_A, displayName: 'Skylar' },
                              { uid: UID_A, displayName: 'Skylar' })).toBe(true);
  });

  it('⚠️ does NOT hand a name-sharer somebody else account-keyed entry', () => {
    // THE BUG THE MIGRATION EXISTS TO FIX, asserted directly. Two members, one
    // display name: before, this was the same document and this predicate did
    // not exist. A regression here is invisible on every screen.
    expect(ownsReadingListDoc({ uid: UID_A, displayName: 'Skylar' },
                              { uid: UID_B, displayName: 'Skylar' })).toBe(false);
  });

  it('⚠️ never falls back to the name for a document that HAS a uid', () => {
    // The fallback is what keeps the legacy 53 reachable. Applying it to an
    // account-keyed document would re-open the hole while every other test
    // still passed — so it is pinned separately from the case above.
    expect(ownsReadingListDoc({ uid: UID_A, displayName: 'Skylar' },
                              { uid: null, displayName: 'Skylar' })).toBe(false);
  });

  it('matches a LEGACY (uid-less) document by display name', () => {
    expect(ownsReadingListDoc({ displayName: 'Divaelf' },
                              { uid: UID_A, displayName: 'Divaelf' })).toBe(true);
  });

  it('folds case and whitespace on the legacy path only', () => {
    expect(ownsReadingListDoc({ displayName: '  DIVAELF ' },
                              { uid: null, displayName: 'divaelf' })).toBe(true);
  });

  it('a document with neither key belongs to nobody', () => {
    expect(ownsReadingListDoc({}, { uid: UID_A, displayName: 'Skylar' })).toBe(false);
    expect(ownsReadingListDoc({ displayName: 'Skylar' }, {})).toBe(false);
  });

  it('is total — undefined arguments answer false rather than throwing', () => {
    expect(ownsReadingListDoc(undefined, undefined)).toBe(false);
  });
});

describe('clearTbrForRating clears BOTH ids', () => {
  beforeEach(() => { mockStore = {}; timestampCounter = 0; });

  it('retires the ACCOUNT-keyed entry when the rater has an account', async () => {
    mockStore[uidKey(UID_A, 'dune')] = { uid: UID_A, displayName: 'Skylar', status: 'tbr' };

    const r = await clearTbrForRating(fakeDb, 'dune', 'Skylar', UID_A);

    expect(r.cleared).toBe(true);
    expect(mockStore[uidKey(UID_A, 'dune')]).toBeUndefined();
  });

  it('⚠️ also retires a LEGACY entry the migration could not move', async () => {
    // A signed-in person may still hold an entry under the old id — the 53, or
    // one written by a still-open legacy tab. Clearing only the account id
    // would leave a rated book on their list, which is the exact "the button
    // lies about a list" failure the instant clear was written to prevent.
    mockStore[rlKey('Skylar', 'dune')] = { displayName: 'Skylar', status: 'tbr' };

    const r = await clearTbrForRating(fakeDb, 'dune', 'Skylar', UID_A);

    expect(r.cleared).toBe(true);
    expect(mockStore[rlKey('Skylar', 'dune')]).toBeUndefined();
  });

  it('clears both at once when both somehow exist', async () => {
    mockStore[uidKey(UID_A, 'dune')] = { uid: UID_A, displayName: 'Skylar', status: 'tbr' };
    mockStore[rlKey('Skylar', 'dune')] = { displayName: 'Skylar', status: 'tbr' };

    await clearTbrForRating(fakeDb, 'dune', 'Skylar', UID_A);

    expect(mockStore[uidKey(UID_A, 'dune')]).toBeUndefined();
    expect(mockStore[rlKey('Skylar', 'dune')]).toBeUndefined();
  });

  it('with NO uid it touches only the legacy id — a legacy session made no other entry', async () => {
    mockStore[uidKey(UID_A, 'dune')] = { uid: UID_A, displayName: 'Skylar', status: 'tbr' };
    mockStore[rlKey('Skylar', 'dune')] = { displayName: 'Skylar', status: 'tbr' };

    await clearTbrForRating(fakeDb, 'dune', 'Skylar');

    expect(mockStore[rlKey('Skylar', 'dune')]).toBeUndefined();
    expect(mockStore[uidKey(UID_A, 'dune')]).toBeDefined(); // not theirs to clear
  });

  it('⚠️ never clears ANOTHER account entry for the same book', async () => {
    mockStore[uidKey(UID_A, 'dune')] = { uid: UID_A, displayName: 'Skylar', status: 'tbr' };
    mockStore[uidKey(UID_B, 'dune')] = { uid: UID_B, displayName: 'Skylar', status: 'tbr' };

    await clearTbrForRating(fakeDb, 'dune', 'Skylar', UID_A);

    expect(mockStore[uidKey(UID_A, 'dune')]).toBeUndefined();
    expect(mockStore[uidKey(UID_B, 'dune')]).toBeDefined();
  });

  it('a refusal on one id does not stop the other delete', async () => {
    // Both are attempted before anything is reported — otherwise a rules
    // refusal on the first would silently leave the second entry standing.
    mockStore[uidKey(UID_A, 'dune')] = { uid: UID_A, status: 'tbr', __denyDelete: true };
    mockStore[rlKey('Skylar', 'dune')] = { displayName: 'Skylar', status: 'tbr' };

    const r = await clearTbrForRating(fakeDb, 'dune', 'Skylar', UID_A);

    expect(r.cleared).toBe(false);
    expect(typeof r.error).toBe('string');
    expect(mockStore[rlKey('Skylar', 'dune')]).toBeUndefined(); // still cleared
  });

  it('a rating through submitReview passes the uid through', async () => {
    mockStore[uidKey(UID_A, 'dune')] = { uid: UID_A, displayName: 'Skylar', status: 'tbr' };

    const r = await submitReview(fakeDb, 'dune', 'Skylar', 4, 'good', UID_A);

    expect(r.success).toBe(true);
    expect(mockStore[uidKey(UID_A, 'dune')]).toBeUndefined();
  });
});
