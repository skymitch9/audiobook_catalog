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

const { addUserWarning, getUserWarnings, deleteUserWarning, MAX_WARNING_LABEL } =
  await import('../user-warnings.js');

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
const bob = { displayName: 'Bob Brown' };
const TITLE = 'Dungeon Crawler Carl - Book 1';

beforeEach(() => { mockStore = {}; });

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

describe('deleteUserWarning', () => {
  it('lets the author remove their warning but nobody else', async () => {
    const r = await addUserWarning(fakeDb, TITLE, 'Gore', jane);
    const w = (await getUserWarnings(fakeDb, TITLE))[0];
    expect((await deleteUserWarning(fakeDb, w, bob)).success).toBe(false);
    expect((await deleteUserWarning(fakeDb, w, jane)).success).toBe(true);
    expect(await getUserWarnings(fakeDb, TITLE)).toHaveLength(0);
    expect(r.success).toBe(true);
  });
});
