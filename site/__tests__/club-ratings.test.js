// @vitest-environment jsdom
// Feature: book-clubs backlog #4 — blind ratings reveal (rate privately,
// reveal together). See the "Blind ratings reveal" section of
// site/club-reads.js for the full trust-model rationale: ratings are
// unreadable to EVERYONE (including their own author) until reveal, because
// this site's member identity has no auth binding a rule could check
// ("read your own doc" is not enforceable when the doc id is just whatever
// path the client asks for). The caller's own rating is mirrored into
// localStorage instead — these tests cover that mirror plus the tally /
// reveal-badge / reveal-ordering pure logic and the Firestore writers.
import { describe, it, expect, beforeEach, vi } from 'vitest';

// The Phase 1 shadow reporter (gate-shadow.js) fires fire-and-forget from
// the gated write paths under test; mock it so no test ever touches the
// network. Its own contract is pinned in gate-shadow.test.js.
vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

// --- In-memory Firestore mock (same shape as club-polls.test.js) ---
let mockStore = {};

vi.mock('firebase/firestore', () => {
  let autoId = 0;

  function makeSnap(path) {
    const d = mockStore[path];
    return { exists: () => !!d, data: () => d, id: path.split('/').pop() };
  }

  function applyUpdate(path, data) {
    const current = mockStore[path] || {};
    const next = { ...current };
    for (const [k, v] of Object.entries(data)) {
      if (v && typeof v === 'object' && '__inc' in v) next[k] = (current[k] || 0) + v.__inc;
      else next[k] = v;
    }
    mockStore[path] = next;
  }

  return {
    collection: (db, ...segs) => ({ _type: 'col', _path: segs.join('/') }),
    doc: (dbOrCol, ...segs) => {
      if (dbOrCol && dbOrCol._type === 'col') {
        autoId += 1;
        return { _path: `${dbOrCol._path}/auto${autoId}`, id: `auto${autoId}` };
      }
      return { _path: segs.join('/'), id: segs[segs.length - 1] };
    },
    // getDoc/getDocs are vi.fn()-wrapped (not plain functions) so tests can
    // assert rateBook() never reads the ratings subcollection before
    // writing — that get() would 403 while blind in the real deployed
    // rules (see the ⚠️ gotcha note in club-reads.js).
    getDoc: vi.fn(async (ref) => makeSnap(ref._path)),
    setDoc: async (ref, data) => { mockStore[ref._path] = { ...data }; },
    updateDoc: async (ref, data) => { applyUpdate(ref._path, data); },
    deleteDoc: async (ref) => { delete mockStore[ref._path]; },
    increment: (n) => ({ __inc: n }),
    query: (colRef, ...filters) => ({ _path: colRef._path, _filters: filters }),
    where: (field, op, value) => ({ field, op, value }),
    getDocs: vi.fn(async (q) => {
      const prefix = q._path + '/';
      const docs = Object.entries(mockStore)
        .filter(([p]) => p.startsWith(prefix) && !p.slice(prefix.length).includes('/'))
        .map(([p, data]) => ({ id: p.split('/').pop(), data: () => data, exists: () => true }));
      return { docs };
    }),
    serverTimestamp: () => 'server-ts',
    runTransaction: async (db, fn) =>
      fn({
        get: async (ref) => makeSnap(ref._path),
        set: (ref, data) => { mockStore[ref._path] = { ...data }; },
        update: (ref, data) => { applyUpdate(ref._path, data); },
        delete: (ref) => { delete mockStore[ref._path]; },
      }),
  };
});

vi.mock('firebase/auth', () => ({
  getAuth: vi.fn(),
  signInWithPopup: vi.fn(),
  GoogleAuthProvider: vi.fn(),
  onAuthStateChanged: vi.fn(),
  signOut: vi.fn(),
}));

const {
  MIN_RATING, MAX_RATING, MAX_RATING_COMMENT_LENGTH,
  validateRatingValue, validateRatingComment, ratingsAreRevealed,
  tallyRatings, isRatingAfterReveal, sortRatingsForReveal,
  myRatingStorageKey, storeMyRatingLocally, getMyStoredRating,
  rateBook, revealRatings, getRatings, deleteRating,
} = await import('../club-reads.js');
const { getDoc: mockedGetDoc, getDocs: mockedGetDocs } = await import('firebase/firestore');

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
const bob = { displayName: 'Bob' };
// col() resolves to *_dev under jsdom (localhost = dev lane)
const CLUB_PATH = 'clubs_dev/club1';
const READ_PATH = `${CLUB_PATH}/reads/read1`;

beforeEach(() => {
  mockStore = {
    [CLUB_PATH]: { name: 'Test Club', activeSlots: [1] },
    [READ_PATH]: { bookTitle: 'A Book', status: 'active' },
  };
  localStorage.clear();
  mockedGetDoc.mockClear();
  mockedGetDocs.mockClear();
});

// ==================== Pure validation ====================

describe('validateRatingValue', () => {
  it('rejects non-numeric input', () => {
    expect(validateRatingValue(undefined).valid).toBe(false);
    expect(validateRatingValue('4').valid).toBe(false);
    expect(validateRatingValue(NaN).valid).toBe(false);
  });

  it('rejects out-of-range values', () => {
    expect(validateRatingValue(0).valid).toBe(false);
    expect(validateRatingValue(5.5).valid).toBe(false);
    expect(validateRatingValue(-1).valid).toBe(false);
  });

  it('rejects values off the half-star grid', () => {
    expect(validateRatingValue(3.25).valid).toBe(false);
    expect(validateRatingValue(2.1).valid).toBe(false);
  });

  it(`accepts the full ${MIN_RATING}-${MAX_RATING} half-star scale`, () => {
    for (let r = MIN_RATING; r <= MAX_RATING; r += 0.5) {
      expect(validateRatingValue(r).valid).toBe(true);
    }
  });
});

describe('validateRatingComment', () => {
  it('accepts a blank comment and trims whitespace', () => {
    expect(validateRatingComment('').valid).toBe(true);
    const r = validateRatingComment('  nice pacing  ');
    expect(r.valid).toBe(true);
    expect(r.comment).toBe('nice pacing');
  });

  it('rejects over the length cap', () => {
    const r = validateRatingComment('x'.repeat(MAX_RATING_COMMENT_LENGTH + 1));
    expect(r.valid).toBe(false);
  });

  it('accepts exactly at the cap', () => {
    expect(validateRatingComment('x'.repeat(MAX_RATING_COMMENT_LENGTH)).valid).toBe(true);
  });
});

// ==================== Reveal predicate ====================

describe('ratingsAreRevealed', () => {
  it('is false for a read with no flag, or a falsy/missing read', () => {
    expect(ratingsAreRevealed({})).toBe(false);
    expect(ratingsAreRevealed(null)).toBe(false);
    expect(ratingsAreRevealed(undefined)).toBe(false);
  });

  it('is true only when the flag is exactly true', () => {
    expect(ratingsAreRevealed({ ratingsRevealed: true })).toBe(true);
    expect(ratingsAreRevealed({ ratingsRevealed: 'true' })).toBe(false);
    expect(ratingsAreRevealed({ ratingsRevealed: false })).toBe(false);
  });
});

// ==================== Tally / average ====================

describe('tallyRatings', () => {
  it('averages ratings and rounds to 1 decimal', () => {
    expect(tallyRatings([{ rating: 5 }, { rating: 4 }, { rating: 3 }])).toEqual({ average: 4, count: 3 });
    expect(tallyRatings([{ rating: 5 }, { rating: 4.5 }])).toEqual({ average: 4.8, count: 2 });
  });

  it('returns 0/0 for an empty or missing list', () => {
    expect(tallyRatings([])).toEqual({ average: 0, count: 0 });
    expect(tallyRatings(undefined)).toEqual({ average: 0, count: 0 });
  });

  it('ignores malformed docs without a numeric rating', () => {
    const ratings = [{ rating: 4 }, { comment: 'no rating field' }, { rating: 'five' }];
    expect(tallyRatings(ratings)).toEqual({ average: 4, count: 1 });
  });
});

// ==================== After-reveal badge ====================

describe('isRatingAfterReveal', () => {
  it('is false when the rating predates the reveal', () => {
    expect(isRatingAfterReveal(100, 200)).toBe(false);
  });

  it('is true when the rating was created after the reveal instant', () => {
    expect(isRatingAfterReveal(300, 200)).toBe(true);
  });

  it('is false when either timestamp is missing/non-numeric (never mislabel blind ratings)', () => {
    expect(isRatingAfterReveal(undefined, 200)).toBe(false);
    expect(isRatingAfterReveal(300, undefined)).toBe(false);
    expect(isRatingAfterReveal(null, null)).toBe(false);
  });
});

// ==================== Reveal-moment ordering ====================

describe('sortRatingsForReveal', () => {
  it('sorts highest stars first', () => {
    const ratings = [
      { displayName: 'Bob', rating: 3 },
      { displayName: 'Ann', rating: 5 },
      { displayName: 'Cid', rating: 4 },
    ];
    expect(sortRatingsForReveal(ratings).map(r => r.displayName)).toEqual(['Ann', 'Cid', 'Bob']);
  });

  it('breaks a tie in star rating alphabetically by name', () => {
    const ratings = [
      { displayName: 'Zed', rating: 4 },
      { displayName: 'Ann', rating: 4 },
    ];
    expect(sortRatingsForReveal(ratings).map(r => r.displayName)).toEqual(['Ann', 'Zed']);
  });

  it('does not mutate the input array', () => {
    const ratings = [{ displayName: 'B', rating: 1 }, { displayName: 'A', rating: 5 }];
    const copy = [...ratings];
    sortRatingsForReveal(ratings);
    expect(ratings).toEqual(copy);
  });
});

// ==================== localStorage mirror (the blind-window design) ====================

describe('localStorage rating mirror', () => {
  it('round-trips a stored rating', () => {
    storeMyRatingLocally('club1', 'read1', 'jane-doe', 4.5, 'loved it');
    const mine = getMyStoredRating('club1', 'read1', 'jane-doe');
    expect(mine).toMatchObject({ rating: 4.5, comment: 'loved it' });
    expect(typeof mine.savedAt).toBe('number');
  });

  it('returns null when nothing is stored', () => {
    expect(getMyStoredRating('club1', 'read1', 'nobody')).toBeNull();
  });

  it('keys are scoped per club/read/member so ratings on different reads never collide', () => {
    storeMyRatingLocally('club1', 'read1', 'jane-doe', 5, '');
    storeMyRatingLocally('club1', 'read2', 'jane-doe', 2, '');
    expect(getMyStoredRating('club1', 'read1', 'jane-doe').rating).toBe(5);
    expect(getMyStoredRating('club1', 'read2', 'jane-doe').rating).toBe(2);
    expect(myRatingStorageKey('club1', 'read1', 'jane-doe'))
      .not.toBe(myRatingStorageKey('club1', 'read2', 'jane-doe'));
  });
});

// ==================== Firestore-backed CRUD ====================

describe('rateBook', () => {
  it('requires sign-in', async () => {
    const r = await rateBook(fakeDb, 'club1', 'read1', 4, '', null);
    expect(r.success).toBe(false);
  });

  it('rejects an invalid rating or over-length comment without writing anything', async () => {
    const bad1 = await rateBook(fakeDb, 'club1', 'read1', 6, '', jane);
    expect(bad1.success).toBe(false);
    const bad2 = await rateBook(fakeDb, 'club1', 'read1', 4, 'x'.repeat(200), jane);
    expect(bad2.success).toBe(false);
    expect(mockStore[READ_PATH].ratingCount).toBeUndefined();
  });

  it('creates a rating, bumps ratingCount once, and mirrors it to localStorage', async () => {
    const r = await rateBook(fakeDb, 'club1', 'read1', 4.5, 'great narration', jane);
    expect(r.success).toBe(true);
    expect(mockStore[`${READ_PATH}/ratings/jane doe`]).toMatchObject({
      displayName: 'Jane Doe', rating: 4.5, comment: 'great narration',
    });
    expect(mockStore[READ_PATH].ratingCount).toBe(1);
    expect(getMyStoredRating('club1', 'read1', 'jane doe')).toMatchObject({ rating: 4.5 });
  });

  it('editing an existing rating does not double-count ratingCount (tracked via the localStorage mirror, not a read)', async () => {
    await rateBook(fakeDb, 'club1', 'read1', 3, 'first pass', jane);
    await rateBook(fakeDb, 'club1', 'read1', 5, 'changed my mind', jane);
    expect(mockStore[READ_PATH].ratingCount).toBe(1);
    expect(mockStore[`${READ_PATH}/ratings/jane doe`]).toMatchObject({ rating: 5, comment: 'changed my mind' });
  });

  it('never reads the ratings subcollection first — would 403 while blind, per firestore.rules (regression: an earlier version wrapped this in a transaction that opened with a get() and broke every blind-window rating)', async () => {
    await rateBook(fakeDb, 'club1', 'read1', 4, '', jane);
    await rateBook(fakeDb, 'club1', 'read1', 3, 'edit', jane); // and again on edit
    expect(mockedGetDoc).not.toHaveBeenCalled();
    expect(mockedGetDocs).not.toHaveBeenCalled();
  });

  it('ratingCount increments independently per distinct member', async () => {
    await rateBook(fakeDb, 'club1', 'read1', 5, '', jane);
    await rateBook(fakeDb, 'club1', 'read1', 3, '', bob);
    expect(mockStore[READ_PATH].ratingCount).toBe(2);
  });
});

describe('revealRatings', () => {
  it('flips ratingsRevealed and stamps revealedAt', async () => {
    const r = await revealRatings(fakeDb, 'club1', 'read1');
    expect(r.success).toBe(true);
    expect(mockStore[READ_PATH].ratingsRevealed).toBe(true);
    expect(mockStore[READ_PATH].revealedAt).toBe('server-ts');
  });
});

describe('getRatings', () => {
  it('returns every rating doc for a read, keyed by slug', async () => {
    await rateBook(fakeDb, 'club1', 'read1', 5, '', jane);
    await rateBook(fakeDb, 'club1', 'read1', 2.5, 'meh', bob);
    const ratings = await getRatings(fakeDb, 'club1', 'read1');
    expect(ratings).toHaveLength(2);
    expect(ratings.map(r => r.slug).sort()).toEqual(['bob', 'jane doe']);
  });

  it('returns an empty list for a read nobody has rated', async () => {
    expect(await getRatings(fakeDb, 'club1', 'read1')).toEqual([]);
  });
});

describe('deleteRating — moderation removal (three-tier model)', () => {
  it('removes the target slug doc and decrements ratingCount', async () => {
    await rateBook(fakeDb, 'club1', 'read1', 5, '', jane);
    await rateBook(fakeDb, 'club1', 'read1', 2.5, 'meh', bob);
    expect(mockStore[READ_PATH].ratingCount).toBe(2);

    const r = await deleteRating(fakeDb, 'club1', 'read1', 'bob');
    expect(r.success).toBe(true);
    expect(mockStore[`${READ_PATH}/ratings/bob`]).toBeUndefined();
    // Jane's rating is untouched; the open counter stays honest.
    expect(mockStore[`${READ_PATH}/ratings/jane doe`]).toBeDefined();
    expect(mockStore[READ_PATH].ratingCount).toBe(1);
  });
});
