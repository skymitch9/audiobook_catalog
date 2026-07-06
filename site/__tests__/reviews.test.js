// @vitest-environment jsdom
// Feature: book-reviews-and-user-identity, Property 13: Book ID derivation is deterministic
// Feature: book-reviews-and-user-identity, Property 10: Average rating computation
// Feature: book-reviews-and-user-identity, Property 6: Review submission round-trip
// Feature: book-reviews-and-user-identity, Property 7: Review input validation
// Feature: book-reviews-and-user-identity, Property 8: Review upsert
// Feature: book-reviews-and-user-identity, Property 9: Review fetch returns correct book's reviews in date order
// Feature: book-reviews-and-user-identity, Property 11: Review display contains all required fields
import { describe, it, expect, beforeEach, vi } from 'vitest';
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

import { bookIdFromTitle, computeAverageRating, submitReview, getReviews, renderStars, renderReviewSection, formatDate } from '../reviews.js';

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

          // Patch the createdAt timestamp to a known value for date verification
          const docKey = `reviews/${bookId}_${displayName.toLowerCase()}`;
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
