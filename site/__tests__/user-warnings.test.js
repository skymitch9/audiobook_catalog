// @vitest-environment jsdom
// Feature: reader-added content warnings (user_content_warnings collection)
import { describe, it, expect, beforeEach, vi } from 'vitest';

let mockStore = {};

vi.mock('firebase/firestore', () => ({
  collection: (db, ...segs) => ({ _type: 'col', _path: segs.join('/') }),
  doc: (dbOrCol, ...segs) => ({ _path: segs.join('/'), id: segs[segs.length - 1] }),
  setDoc: async (ref, data) => { mockStore[ref._path] = { ...data }; },
  deleteDoc: async (ref) => { delete mockStore[ref._path]; },
  query: (colRef, ...filters) => ({ _path: colRef._path, _filters: filters }),
  where: (field, op, value) => ({ field, op, value }),
  getDocs: async (q) => {
    const prefix = q._path + '/';
    const filters = q._filters || [];
    const docs = Object.entries(mockStore)
      .filter(([p]) => p.startsWith(prefix) && !p.slice(prefix.length).includes('/'))
      .map(([p, data]) => ({ id: p.split('/').pop(), data: () => data, exists: () => true }))
      .filter((d) => filters.every((f) => (f.op === '==' ? d.data()[f.field] === f.value : true)));
    return { docs };
  },
  serverTimestamp: () => 'server-ts',
}));

// The default Firebase app the module reads the live identity off (see
// liveUid() in user-warnings.js — the gate-shadow.js default-app pattern).
vi.mock('firebase/app', () => ({ getApp: () => ({ name: '[DEFAULT]' }) }));

// The ENFORCED identity. `liveUid` below is what the tests steer: null is a
// legacy/mirror-only session (no uid to stamp), a string is a live one.
let liveUid = 'uid-jane';
vi.mock('../identity.js', () => ({
  getLiveUser: async () => (liveUid ? { uid: liveUid, email: null, displayName: null } : null),
}));

// Phase 1 shadow reporter — mocked so the action split is assertable and no
// fetch is attempted. Its own contract is pinned in gate-shadow.test.js.
vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

const { addUserWarning, getUserWarnings, deleteUserWarning, MAX_WARNING_LABEL } =
  await import('../user-warnings.js');
const { reportGate } = await import('../gate-shadow.js');

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
const bob = { displayName: 'Bob Brown' };
const TITLE = 'Dungeon Crawler Carl - Book 1';

beforeEach(() => { mockStore = {}; liveUid = 'uid-jane'; reportGate.mockClear(); });

describe('addUserWarning', () => {
  it('stores a warning keyed by book/reader/topic in the dev-lane collection', async () => {
    const r = await addUserWarning(fakeDb, TITLE, 'Animal death', jane);
    expect(r.success).toBe(true);
    // jsdom = localhost = dev lane
    expect(r.id.startsWith('dungeon-crawler-carl-book-1_jane doe_')).toBe(true);
    expect(mockStore[`user_content_warnings_dev/${r.id}`].label).toBe('Animal death');
  });

  it('re-adding the same topic overwrites instead of duplicating', async () => {
    await addUserWarning(fakeDb, TITLE, 'Animal death', jane);
    await addUserWarning(fakeDb, TITLE, 'Animal Death', jane); // same topic, different case
    const all = await getUserWarnings(fakeDb, TITLE);
    expect(all).toHaveLength(1);
    expect(all[0].label).toBe('Animal Death');
  });

  it('rejects signed-out, empty, and over-long labels', async () => {
    expect((await addUserWarning(fakeDb, TITLE, 'Gore', null)).success).toBe(false);
    expect((await addUserWarning(fakeDb, TITLE, '   ', jane)).success).toBe(false);
    expect((await addUserWarning(fakeDb, TITLE, 'x'.repeat(MAX_WARNING_LABEL + 1), jane)).success).toBe(false);
    expect(Object.keys(mockStore)).toHaveLength(0);
  });
});

describe('getUserWarnings', () => {
  it('returns only the asked-for book, oldest first', async () => {
    await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    await addUserWarning(fakeDb, 'Other Book', 'Spiders', bob);
    await addUserWarning(fakeDb, TITLE, 'Animal death', bob);
    const all = await getUserWarnings(fakeDb, TITLE);
    expect(all).toHaveLength(2);
    expect(all.every((w) => w.bookId === 'dungeon-crawler-carl-book-1')).toBe(true);
  });
});

describe('addUserWarning — the delete binding (2026-08-17)', () => {
  it('stamps the live uid as authorUid', async () => {
    const r = await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    expect(mockStore[`user_content_warnings_dev/${r.id}`].authorUid).toBe('uid-jane');
  });

  it('omits authorUid entirely when no live session can prove a uid', async () => {
    liveUid = null; // legacy / mirror-only session
    const r = await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    expect(r.success).toBe(true); // create stays open — rules are shape-only
    expect('authorUid' in mockStore[`user_content_warnings_dev/${r.id}`]).toBe(false);
  });
});

describe('deleteUserWarning', () => {
  it('lets the author remove their warning but nobody else', async () => {
    const r = await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    const w = (await getUserWarnings(fakeDb, TITLE))[0];
    liveUid = 'uid-bob';
    expect((await deleteUserWarning(fakeDb, w, bob)).success).toBe(false);
    liveUid = 'uid-jane';
    expect((await deleteUserWarning(fakeDb, w, jane)).success).toBe(true);
    expect(await getUserWarnings(fakeDb, TITLE)).toHaveLength(0);
    expect(r.success).toBe(true);
  });

  it('lets a site moderator remove SOMEONE ELSE’S warning', async () => {
    await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    const w = (await getUserWarnings(fakeDb, TITLE))[0];
    liveUid = 'uid-bob';
    // Without the capability: refused before the write is even attempted.
    expect((await deleteUserWarning(fakeDb, w, bob)).success).toBe(false);
    expect(await getUserWarnings(fakeDb, TITLE)).toHaveLength(1);
    // With it: allowed.
    expect((await deleteUserWarning(fakeDb, w, bob, { canModerate: true })).success).toBe(true);
    expect(await getUserWarnings(fakeDb, TITLE)).toHaveLength(0);
  });

  it('refuses a same-name caller whose uid does not match the doc, in words', async () => {
    liveUid = null;
    await addUserWarning(fakeDb, TITLE, 'Gore', jane); // no authorUid stamped
    const w = (await getUserWarnings(fakeDb, TITLE))[0];
    liveUid = 'uid-jane';
    const r = await deleteUserWarning(fakeDb, w, jane);
    expect(r.success).toBe(false);
    expect(r.error).toMatch(/site moderator/i);   // says what it needs
    expect(r.error).not.toMatch(/^\d+$/);         // never a bare status
    expect(await getUserWarnings(fakeDb, TITLE)).toHaveLength(1);
  });

  it('tells a session that cannot prove a uid to sign in again', async () => {
    await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    const w = (await getUserWarnings(fakeDb, TITLE))[0];
    liveUid = null;
    const r = await deleteUserWarning(fakeDb, w, jane);
    expect(r.success).toBe(false);
    expect(r.error).toMatch(/sign in with google again/i);
  });

  it('refuses a signed-out caller', async () => {
    await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    const w = (await getUserWarnings(fakeDb, TITLE))[0];
    expect((await deleteUserWarning(fakeDb, w, null)).success).toBe(false);
  });
});

// The soak audit's blind spot #3: this module imported gate-shadow.js not at
// all, so warning.modDelete measured as "nobody used it" rather than "nothing
// reports it". reportGate is MOCKED at the top of this file.
describe('shadow instrumentation — the action split', () => {
  it('reports warning.selfDelete for your own note', async () => {
    await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    const w = (await getUserWarnings(fakeDb, TITLE))[0];
    reportGate.mockClear();
    await deleteUserWarning(fakeDb, w, jane);
    expect(reportGate).toHaveBeenCalledTimes(1);
    expect(reportGate).toHaveBeenCalledWith('warning.selfDelete');
  });

  it('reports warning.modDelete for someone else’s note', async () => {
    await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    const w = (await getUserWarnings(fakeDb, TITLE))[0];
    liveUid = 'uid-bob';
    reportGate.mockClear();
    await deleteUserWarning(fakeDb, w, bob, { canModerate: true });
    expect(reportGate).toHaveBeenCalledTimes(1);
    expect(reportGate).toHaveBeenCalledWith('warning.modDelete');
  });

  it('reports nothing when the attempt is refused before the write', async () => {
    await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    const w = (await getUserWarnings(fakeDb, TITLE))[0];
    liveUid = 'uid-bob';
    reportGate.mockClear();
    await deleteUserWarning(fakeDb, w, bob);
    expect(reportGate).not.toHaveBeenCalled();
  });
});
