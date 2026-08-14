// @vitest-environment jsdom
// Feature: book-clubs backlog #5 — meeting scheduler RSVP + .ics download,
// extending the shipped next-meeting field. See the "Meeting RSVP" section
// of club-reads.js for the full staleness-design rationale: responses are
// stamped with the meetingAt epoch they answered, and every reader filters
// on meetingAt === the club's live nextMeetingAt so a stale response (from
// a meeting that was rescheduled or cleared) can never be miscounted.
import { describe, it, expect, beforeEach, vi } from 'vitest';

// --- In-memory Firestore mock (same shape as club-polls.test.js) ---
let mockStore = {};

vi.mock('firebase/firestore', () => {
  let autoId = 0;

  function makeSnap(path) {
    const d = mockStore[path];
    return { exists: () => !!d, data: () => d, id: path.split('/').pop() };
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
    getDoc: async (ref) => makeSnap(ref._path),
    setDoc: async (ref, data) => { mockStore[ref._path] = { ...data }; },
    updateDoc: async (ref, data) => { mockStore[ref._path] = { ...(mockStore[ref._path] || {}), ...data }; },
    deleteDoc: async (ref) => { delete mockStore[ref._path]; },
    query: (colRef, ...filters) => ({ _path: colRef._path, _filters: filters }),
    where: (field, op, value) => ({ field, op, value }),
    getDocs: async (q) => {
      const prefix = q._path + '/';
      const docs = Object.entries(mockStore)
        .filter(([p]) => p.startsWith(prefix) && !p.slice(prefix.length).includes('/'))
        .map(([p, data]) => ({ id: p.split('/').pop(), data: () => data, exists: () => true }));
      return { docs };
    },
    serverTimestamp: () => 'server-ts',
    runTransaction: async (db, fn) =>
      fn({
        get: async (ref) => makeSnap(ref._path),
        set: (ref, data) => { mockStore[ref._path] = { ...data }; },
        update: (ref, data) => { mockStore[ref._path] = { ...(mockStore[ref._path] || {}), ...data }; },
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
  RSVP_RESPONSES, validateRsvpResponse, isRsvpCurrent, currentRsvps,
  myRsvpResponse, tallyRsvps, castRsvp, getRsvps,
} = await import('../club-reads.js');
import { col } from '../fb-env.js';

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
const bob = { displayName: 'Bob' };
// col() resolves to *_dev under jsdom (localhost = dev lane)
const CLUB_PATH = `${col('clubs')}/club1`;
const MEETING_A = Date.parse('2026-09-01T19:00:00Z');
const MEETING_B = Date.parse('2026-09-08T19:00:00Z'); // rescheduled

beforeEach(() => {
  mockStore = { [CLUB_PATH]: { name: 'Test Club', nextMeetingAt: MEETING_A } };
});

// ==================== Pure validation ====================

describe('RSVP_RESPONSES / validateRsvpResponse', () => {
  it('is exactly the three allowed responses, in Going/Maybe/Can\'t order', () => {
    expect(RSVP_RESPONSES).toEqual(['going', 'maybe', 'cant']);
  });

  it('accepts only the three valid responses', () => {
    expect(validateRsvpResponse('going')).toBe(true);
    expect(validateRsvpResponse('maybe')).toBe(true);
    expect(validateRsvpResponse('cant')).toBe(true);
  });

  it('rejects anything else, including near-misses', () => {
    expect(validateRsvpResponse('yes')).toBe(false);
    expect(validateRsvpResponse('CANT')).toBe(false);
    expect(validateRsvpResponse('')).toBe(false);
    expect(validateRsvpResponse(undefined)).toBe(false);
    expect(validateRsvpResponse(null)).toBe(false);
  });
});

// ==================== Staleness mechanism ====================
// This is the core contract of the feature: a response can only ever be
// counted against the meeting instant it was stamped for.

describe('isRsvpCurrent / currentRsvps — the staleness mechanism', () => {
  it('a response stamped with the live meetingAt is current', () => {
    expect(isRsvpCurrent({ meetingAt: MEETING_A }, MEETING_A)).toBe(true);
  });

  it('a response stamped with a DIFFERENT meetingAt (rescheduled meeting) is stale', () => {
    expect(isRsvpCurrent({ meetingAt: MEETING_A }, MEETING_B)).toBe(false);
  });

  it('a cleared meeting (nextMeetingAt null/undefined) makes every response stale', () => {
    expect(isRsvpCurrent({ meetingAt: MEETING_A }, null)).toBe(false);
    expect(isRsvpCurrent({ meetingAt: MEETING_A }, undefined)).toBe(false);
    expect(isRsvpCurrent({ meetingAt: MEETING_A }, NaN)).toBe(false);
  });

  it('a missing/malformed rsvp doc is never current', () => {
    expect(isRsvpCurrent(null, MEETING_A)).toBe(false);
    expect(isRsvpCurrent({}, MEETING_A)).toBe(false);
    expect(isRsvpCurrent({ meetingAt: 'oops' }, MEETING_A)).toBe(false);
  });

  it('currentRsvps filters a mixed list down to only the live-meeting responses', () => {
    const rsvps = [
      { slug: 'a', meetingAt: MEETING_A, response: 'going' },  // current
      { slug: 'b', meetingAt: MEETING_B, response: 'going' },  // stale (old meeting)
      { slug: 'c', response: 'maybe' },                        // stale (never stamped)
    ];
    expect(currentRsvps(rsvps, MEETING_A)).toEqual([rsvps[0]]);
  });

  it('a rescheduled meeting flips which responses count, with no rewrite of stored docs', () => {
    const rsvps = [
      { slug: 'a', meetingAt: MEETING_A, response: 'going' },
      { slug: 'b', meetingAt: MEETING_B, response: 'cant' },
    ];
    // Before the reschedule, only a's response counts.
    expect(currentRsvps(rsvps, MEETING_A).map(r => r.slug)).toEqual(['a']);
    // After the reschedule (club.nextMeetingAt flips to MEETING_B), only b's
    // does — the exact same stored docs, no write needed to make this true.
    expect(currentRsvps(rsvps, MEETING_B).map(r => r.slug)).toEqual(['b']);
  });
});

describe('myRsvpResponse', () => {
  const current = [
    { slug: 'jane doe', response: 'going' },
    { slug: 'bob', response: 'maybe' },
  ];

  it("finds the caller's own current response by slug", () => {
    expect(myRsvpResponse(current, 'jane doe')).toBe('going');
    expect(myRsvpResponse(current, 'bob')).toBe('maybe');
  });

  it('returns null when the caller has no current response', () => {
    expect(myRsvpResponse(current, 'nobody')).toBeNull();
    expect(myRsvpResponse([], 'jane doe')).toBeNull();
    expect(myRsvpResponse(undefined, 'jane doe')).toBeNull();
  });

  it('ignores a malformed response value defensively', () => {
    expect(myRsvpResponse([{ slug: 'x', response: 'yes-please' }], 'x')).toBeNull();
  });
});

describe('tallyRsvps', () => {
  it('groups and counts by response', () => {
    const rsvps = [
      { slug: 'a', displayName: 'Alice', response: 'going' },
      { slug: 'b', displayName: 'Bob', response: 'going' },
      { slug: 'c', displayName: 'Cara', response: 'maybe' },
      { slug: 'd', displayName: 'Dee', response: 'cant' },
    ];
    const { counts, byResponse, total } = tallyRsvps(rsvps);
    expect(counts).toEqual({ going: 2, maybe: 1, cant: 1 });
    expect(byResponse.going.map(r => r.displayName)).toEqual(['Alice', 'Bob']);
    expect(total).toBe(4);
  });

  it('returns all-zero counts with no responses', () => {
    expect(tallyRsvps([])).toEqual({ counts: { going: 0, maybe: 0, cant: 0 }, byResponse: { going: [], maybe: [], cant: [] }, total: 0 });
    expect(tallyRsvps(undefined).total).toBe(0);
  });

  it('ignores malformed response values (defends against a hand-forged doc)', () => {
    const { counts, total } = tallyRsvps([{ response: 'going' }, { response: 'nope' }, {}]);
    expect(counts).toEqual({ going: 1, maybe: 0, cant: 0 });
    expect(total).toBe(1);
  });
});

// ==================== Firestore-backed CRUD ====================

describe('castRsvp', () => {
  it('requires sign-in', async () => {
    const r = await castRsvp(fakeDb, 'club1', 'going', MEETING_A, null);
    expect(r.success).toBe(false);
  });

  it('rejects an invalid response without writing anything', async () => {
    const r = await castRsvp(fakeDb, 'club1', 'sure', MEETING_A, jane);
    expect(r.success).toBe(false);
    expect(await getRsvps(fakeDb, 'club1')).toEqual([]);
  });

  it('rejects a write with no meeting scheduled', async () => {
    const r = await castRsvp(fakeDb, 'club1', 'going', null, jane);
    expect(r.success).toBe(false);
    expect(r.error).toMatch(/no meeting/i);
  });

  it('writes one doc per member, keyed by slug, stamped with the meeting instant', async () => {
    const r = await castRsvp(fakeDb, 'club1', 'going', MEETING_A, jane);
    expect(r.success).toBe(true);
    const rsvps = await getRsvps(fakeDb, 'club1');
    expect(rsvps).toHaveLength(1);
    expect(rsvps[0]).toMatchObject({
      slug: 'jane doe', displayName: 'Jane Doe', response: 'going', meetingAt: MEETING_A,
    });
  });

  it('is changeable — a second cast by the same member updates the one doc', async () => {
    await castRsvp(fakeDb, 'club1', 'maybe', MEETING_A, jane);
    await castRsvp(fakeDb, 'club1', 'going', MEETING_A, jane); // changed her mind
    const rsvps = await getRsvps(fakeDb, 'club1');
    expect(rsvps).toHaveLength(1);
    expect(rsvps[0].response).toBe('going');
  });

  it('different members get different docs', async () => {
    await castRsvp(fakeDb, 'club1', 'going', MEETING_A, jane);
    await castRsvp(fakeDb, 'club1', 'cant', MEETING_A, bob);
    const rsvps = await getRsvps(fakeDb, 'club1');
    expect(rsvps).toHaveLength(2);
  });

  it('re-casting for a rescheduled meeting supersedes the old meetingAt, not archives it', async () => {
    await castRsvp(fakeDb, 'club1', 'going', MEETING_A, jane);
    await castRsvp(fakeDb, 'club1', 'maybe', MEETING_B, jane); // club rescheduled, jane re-RSVPs
    const rsvps = await getRsvps(fakeDb, 'club1');
    expect(rsvps).toHaveLength(1); // still one doc for jane — the slug-keyed convention
    expect(rsvps[0]).toMatchObject({ response: 'maybe', meetingAt: MEETING_B });
    // And the pre-reschedule response no longer counts toward the new meeting.
    expect(currentRsvps(rsvps, MEETING_A)).toEqual([]);
    expect(currentRsvps(rsvps, MEETING_B)).toHaveLength(1);
  });
});

describe('getRsvps', () => {
  it('returns every rsvp doc for the club, current and stale alike (filtering is the callers job)', async () => {
    await castRsvp(fakeDb, 'club1', 'going', MEETING_A, jane);
    const rsvps = await getRsvps(fakeDb, 'club1');
    expect(rsvps).toHaveLength(1);
    expect(rsvps[0].slug).toBe('jane doe');
  });
});
