// @vitest-environment jsdom
// @vitest-environment-options { "url": "https://audiobooks.heygabi.ai/" }
//
// Feature: auth-migration Phase 3a, surface 1 — site/reviews.js on the
// Worker's `DELETE /api/reviews/:docId`.
//
// Four properties, and the first two are the ones that make the flag a real
// rollout control rather than decoration:
//
//   1. FLAG OFF (the shipped default) → the Firestore delete still happens
//      and NO request is made. Nothing changes for a visitor.
//   2. FLAG ON → the Worker is called and `deleteDoc` is NEVER called.
//   3. ⚠️ A Worker failure DOES NOT fall back to Firestore. This is the
//      property worth a test of its own: a silent retry would make the flag
//      meaningless, and it is exactly the shape of "fix" a later session
//      would add to make an error go away.
//   4. The document id is the same composite on both paths — the id is now
//      part of a URL as well as of a Firestore path.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

let flagOn = false;
vi.mock('../flags.js', () => ({
  flagEnabled: (name) => (name === 'AUTH_ROUTES_REVIEWS' ? flagOn : false),
}));

const deleteDocMock = vi.fn(async () => {});
vi.mock('firebase/firestore', () => ({
  doc: (db, collectionName, id) => ({ _path: `${collectionName}/${id}` }),
  deleteDoc: (...args) => deleteDocMock(...args),
  setDoc: vi.fn(async () => {}),
  getDoc: vi.fn(async () => ({ exists: () => false, data: () => undefined })),
  getDocs: vi.fn(async () => ({ docs: [] })),
  collection: (db, name) => ({ _collectionName: name }),
  query: (ref) => ref,
  where: () => ({}),
  serverTimestamp: () => ({ seconds: 1 }),
}));

let currentUser = null;
vi.mock('firebase/auth', () => ({
  getAuth: () => ({ currentUser }),
  onAuthStateChanged: (auth, cb) => { cb(currentUser); return () => {}; },
}));

import { reportGate } from '../gate-shadow.js';
import { deleteReview, reviewDocId } from '../reviews.js';

function stubFetch(impl) {
  const mock = vi.fn(impl || (async () => ({ ok: true, status: 200, json: async () => ({ success: true }) })));
  vi.stubGlobal('fetch', mock);
  return mock;
}

beforeEach(() => {
  flagOn = false;
  currentUser = { getIdToken: async () => 'live-token' };
  deleteDocMock.mockClear();
  reportGate.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('reviewDocId — one id, two readers', () => {
  it('is the composite submitReview writes, lowercased', () => {
    expect(reviewDocId('the-way-of-kings', 'Skylar')).toBe('the-way-of-kings_skylar');
    expect(reviewDocId('b', undefined)).toBe('b_');
  });
});

describe('deleteReview — FLAG OFF is the shipped behaviour, unchanged', () => {
  it('deletes through Firestore and makes no request at all', async () => {
    const fetchMock = stubFetch();

    const result = await deleteReview({}, 'the-way-of-kings', 'Skylar');

    expect(result).toEqual({ success: true });
    expect(deleteDocMock).toHaveBeenCalledTimes(1);
    expect(deleteDocMock.mock.calls[0][0]._path).toBe('reviews/the-way-of-kings_skylar');
    expect(fetchMock).not.toHaveBeenCalled();
    // the Phase 1 shadow report still accompanies the direct write
    expect(reportGate).toHaveBeenCalledWith('review.delete', { succeeded: true });
  });

  it('a rules refusal is still a worded sentence, not a code', async () => {
    stubFetch();
    deleteDocMock.mockImplementationOnce(async () => {
      const e = new Error('Missing or insufficient permissions.');
      e.code = 'permission-denied';
      throw e;
    });

    const result = await deleteReview({}, 'b', 'Skylar');

    expect(result.success).toBe(false);
    expect(result.error).toMatch(/don't have permission/i);
    expect(result.error).toMatch(/site admin role/i);
    expect(reportGate).toHaveBeenCalledWith('review.delete', { succeeded: false });
  });
});

describe('deleteReview — FLAG ON routes the delete to the Worker', () => {
  beforeEach(() => { flagOn = true; });

  it('calls DELETE /api/reviews/:docId with the bearer, and never touches Firestore', async () => {
    const fetchMock = stubFetch();

    const result = await deleteReview({}, 'the-way-of-kings', 'Skylar');

    expect(result).toEqual({ success: true });
    expect(deleteDocMock).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://audiobook-api.heygabi.ai/api/reviews/the-way-of-kings_skylar');
    expect(init.method).toBe('DELETE');
    expect(init.headers.Authorization).toBe('Bearer live-token');
  });

  it('⚠️ a Worker REFUSAL is surfaced in words and does NOT retry through Firestore', async () => {
    const detail = 'This action needs the "removeAnyReview" capability, which the admin role holds.';
    stubFetch(async () => ({
      ok: false,
      status: 403,
      json: async () => ({ error: 'insufficient_role', detail }),
    }));

    const result = await deleteReview({}, 'b', 'Skylar');

    expect(result).toEqual({ success: false, error: detail });
    expect(deleteDocMock).not.toHaveBeenCalled();
  });

  it('⚠️ a Worker OUTAGE is surfaced as an outage and does NOT retry through Firestore', async () => {
    stubFetch(async () => { throw new TypeError('Failed to fetch'); });

    const result = await deleteReview({}, 'b', 'Skylar');

    expect(result.success).toBe(false);
    expect(result.error).toMatch(/connection problem, not a permission one/i);
    expect(deleteDocMock).not.toHaveBeenCalled();
  });

  it('a signed-out caller is told to sign in, and nothing is written anywhere', async () => {
    currentUser = null;
    const fetchMock = stubFetch();

    const result = await deleteReview({}, 'b', 'Skylar');

    expect(result.success).toBe(false);
    expect(result.error).toMatch(/not signed in with Google/i);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(deleteDocMock).not.toHaveBeenCalled();
  });

  it('sends no ab_gate_shadow report — the Worker writes its own ab_gate line', async () => {
    stubFetch();
    await deleteReview({}, 'b', 'Skylar');
    expect(reportGate).not.toHaveBeenCalled();
  });
});
