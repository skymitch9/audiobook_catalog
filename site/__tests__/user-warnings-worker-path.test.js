// @vitest-environment jsdom
// @vitest-environment-options { "url": "https://audiobooks.heygabi.ai/" }
//
// Feature: auth-migration Phase 3a, surface 4 — site/user-warnings.js on the
// Worker's two content-note routes (added 2026-09-07, agent W15-AB-ENFORCE,
// closing the `warning.selfDelete` / `warning.modDelete` gap Phase 3a had
// recorded as "in ACTION_GATES, no enforce route").
//
// This surface is the odd one of the four: ONE function, TWO routes, because
// `deleteUserWarning` has had two gates since the 2026-08-17 delete split.
// The properties pinned here:
//
//   1. ⚠️ THE FLAG SHIPS OFF, so nothing on /dev/ changes until somebody
//      decides it should. With it off the browser writes Firestore exactly as
//      before, shadow report and all.
//   2. The ARM decides the route — the same `authored` boolean that already
//      chooses the shadow action, so the two can never disagree about which
//      floor an attempt was measured against.
//   3. ⚠️ No `ab_gate_shadow` report on the Worker path: the Worker writes its
//      own `ab_gate` line (mode enforce) and a shadow line beside it would
//      count one action twice in the soak ledger.
//   4. No fallback to Firestore on ANY failure, and an outage is worded
//      differently from a refusal.
//   5. `addUserWarning` / `requestWarningCheck` do NOT move — member-open
//      creates with no route, on either path.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

let mockStore = {};
const fsCalls = [];

vi.mock('firebase/firestore', () => ({
  collection: (db, ...segs) => ({ _type: 'col', _path: segs.join('/') }),
  doc: (dbOrCol, ...segs) => ({ _path: segs.join('/'), id: segs[segs.length - 1] }),
  setDoc: async (ref, data) => { fsCalls.push(['setDoc', ref._path]); mockStore[ref._path] = { ...data }; },
  getDoc: async (ref) => {
    fsCalls.push(['getDoc', ref._path]);
    const data = mockStore[ref._path];
    return { exists: () => !!data, data: () => data };
  },
  deleteDoc: async (ref) => { fsCalls.push(['deleteDoc', ref._path]); delete mockStore[ref._path]; },
  query: (colRef, ...filters) => ({ _path: colRef._path, _filters: filters }),
  where: (field, op, value) => ({ field, op, value }),
  getDocs: async () => ({ docs: [] }),
  serverTimestamp: () => 'server-ts',
}));

vi.mock('firebase/app', () => ({ getApp: () => ({ name: '[DEFAULT]' }) }));

let liveUid = 'uid-jane';
vi.mock('../identity.js', () => ({
  getLiveUser: async () => (liveUid ? { uid: liveUid, email: null, displayName: null } : null),
}));

vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

let flagOn = false;
vi.mock('../flags.js', () => ({
  flagEnabled: (name) => (name === 'AUTH_ROUTES_WARNINGS' ? flagOn : false),
}));

// The bearer worker-writes.js puts on every call. Mocked so this file tests
// THIS surface's wiring; auth-token.js has its own contract elsewhere. `null`
// is a legacy/mirror-only session, which is refused before the network.
let token = 'live-token';
vi.mock('../auth-token.js', () => ({
  idToken: async () => token,
  liveAuthUser: async () => (token ? { uid: 'uid-jane' } : null),
}));

const { addUserWarning, deleteUserWarning, requestWarningCheck } =
  await import('../user-warnings.js');
const { reportGate } = await import('../gate-shadow.js');

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
/** Jane's own note — `authorUid` matches the live uid, so this is the SELF arm. */
const ownNote = { id: 'n1', displayName: 'Jane Doe', authorUid: 'uid-jane' };
/** Somebody else's note — only a moderator may take it down. */
const otherNote = { id: 'n2', displayName: 'Bob Brown', authorUid: 'uid-bob' };

function stubFetch(impl) {
  const mock = vi.fn(
    impl || (async () => ({ ok: true, status: 200, json: async () => ({ success: true }) })),
  );
  vi.stubGlobal('fetch', mock);
  return mock;
}

function call(fetchMock, i = 0) {
  const [url, init] = fetchMock.mock.calls[i];
  return [init.method, url.replace('https://audiobook-api.heygabi.ai', '')];
}

beforeEach(() => {
  mockStore = {};
  fsCalls.length = 0;
  liveUid = 'uid-jane';
  flagOn = false;
  token = 'live-token';
  reportGate.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('FLAG OFF — the shipped behaviour, unchanged', () => {
  it('⚠️ deletes through Firestore and makes no request, on BOTH arms', async () => {
    const fetchMock = stubFetch();
    mockStore['user_content_warnings/n1'] = { label: 'Gore' };
    mockStore['user_content_warnings/n2'] = { label: 'Gore' };

    expect(await deleteUserWarning(fakeDb, ownNote, jane)).toEqual({ success: true });
    expect(await deleteUserWarning(fakeDb, otherNote, jane, { canModerate: true }))
      .toEqual({ success: true });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(fsCalls.filter((c) => c[0] === 'deleteDoc')).toHaveLength(2);
    // …and the Phase 1 shadow report still accompanies the direct write.
    expect(reportGate).toHaveBeenCalledWith('warning.selfDelete', { succeeded: true });
    expect(reportGate).toHaveBeenCalledWith('warning.modDelete', { succeeded: true });
  });
});

describe('FLAG ON — the arm picks the route', () => {
  beforeEach(() => { flagOn = true; });

  it('your OWN note goes to the self route', async () => {
    const fetchMock = stubFetch();
    expect(await deleteUserWarning(fakeDb, ownNote, jane)).toEqual({ success: true });
    expect(call(fetchMock)).toEqual(['DELETE', '/api/warnings/n1']);
    expect(fsCalls.filter((c) => c[0] === 'deleteDoc')).toEqual([]);
  });

  it('somebody ELSE’S note goes to the moderate route', async () => {
    const fetchMock = stubFetch();
    expect(await deleteUserWarning(fakeDb, otherNote, jane, { canModerate: true }))
      .toEqual({ success: true });
    expect(call(fetchMock)).toEqual(['DELETE', '/api/warnings/n2/moderate']);
    expect(fsCalls.filter((c) => c[0] === 'deleteDoc')).toEqual([]);
  });

  it('a doc id with punctuation is percent-encoded into the path', async () => {
    const fetchMock = stubFetch();
    await deleteUserWarning(
      fakeDb,
      { id: 'book id/weird?', displayName: 'Jane Doe', authorUid: 'uid-jane' },
      jane,
    );
    expect(call(fetchMock)).toEqual(['DELETE', '/api/warnings/book%20id%2Fweird%3F']);
  });

  it('carries the live id token as a bearer', async () => {
    const fetchMock = stubFetch();
    await deleteUserWarning(fakeDb, ownNote, jane);
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer live-token');
  });

  it('⚠️ a session with no id token is refused BEFORE the network, in words', async () => {
    token = null;
    const fetchMock = stubFetch();
    const result = await deleteUserWarning(fakeDb, ownNote, jane);
    expect(result.success).toBe(false);
    expect(result.error).toMatch(/not signed in with Google/i);
    expect(fetchMock).not.toHaveBeenCalled();
    // ⚠️ and it does NOT quietly fall back to the Firestore delete.
    expect(fsCalls.filter((c) => c[0] === 'deleteDoc')).toEqual([]);
  });

  it('⚠️ sends NO ab_gate_shadow report — the Worker writes its own line', async () => {
    stubFetch();
    await deleteUserWarning(fakeDb, ownNote, jane);
    await deleteUserWarning(fakeDb, otherNote, jane, { canModerate: true });
    expect(reportGate).not.toHaveBeenCalled();
  });

  it('keeps every client-side refusal BEFORE the network', async () => {
    const fetchMock = stubFetch();
    // Not the author, not a moderator, and not even their name: refused here.
    expect(await deleteUserWarning(fakeDb, otherNote, jane))
      .toEqual({ success: false, error: 'You can only remove warnings you added.' });
    // Signed out entirely.
    expect(await deleteUserWarning(fakeDb, ownNote, null))
      .toEqual({ success: false, error: 'Sign in first.' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('an UNSTAMPED note of their own name is still refused client-side, in words', async () => {
    const fetchMock = stubFetch();
    const unstamped = { id: 'old', displayName: 'Jane Doe' };
    const result = await deleteUserWarning(fakeDb, unstamped, jane);
    expect(result.success).toBe(false);
    expect(result.error).toMatch(/only a\s+site moderator can take it down/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('FLAG ON — failures are worded and never fall back to Firestore', () => {
  beforeEach(() => { flagOn = true; });

  it('a refusal passes the Worker’s own §1e sentence through, and writes nothing', async () => {
    const detail =
      'This content note was added by somebody else, so it cannot be removed from '
      + 'your own account.';
    stubFetch(async () => ({ ok: false, status: 403, json: async () => ({ detail }) }));
    expect(await deleteUserWarning(fakeDb, ownNote, jane))
      .toEqual({ success: false, error: detail });
    expect(fsCalls.filter((c) => c[0] === 'deleteDoc')).toEqual([]);
  });

  it('an unreachable Worker is an OUTAGE, never a permission failure', async () => {
    stubFetch(async () => { throw new TypeError('Failed to fetch'); });
    const result = await deleteUserWarning(fakeDb, otherNote, jane, { canModerate: true });
    expect(result.success).toBe(false);
    expect(result.error).toMatch(/connection problem, not a permission one/i);
    expect(result.error).not.toMatch(/\d{3}/);
    expect(fsCalls.filter((c) => c[0] === 'deleteDoc')).toEqual([]);
  });

  it('a dormant Worker (503) says it is a rollout problem, not the reader’s fault', async () => {
    stubFetch(async () => ({ ok: false, status: 503, json: async () => ({}) }));
    const result = await deleteUserWarning(fakeDb, ownNote, jane);
    expect(result.success).toBe(false);
    expect(result.error).toMatch(/not switched on server-side yet/i);
    expect(result.error).not.toMatch(/\b503\b/);
  });
});

describe('⚠️ the two member-open writes DO NOT move', () => {
  beforeEach(() => { flagOn = true; });

  it('addUserWarning and requestWarningCheck stay browser-direct with the flag ON', async () => {
    const fetchMock = stubFetch();
    expect((await addUserWarning(fakeDb, 'Some Book', 'Gore', jane)).success).toBe(true);
    expect((await requestWarningCheck(fakeDb, 'Some Book', jane)).success).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(fsCalls.filter((c) => c[0] === 'setDoc')).toHaveLength(2);
  });
});
