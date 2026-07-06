// @vitest-environment jsdom
// Feature: club post notifications (Firestore-backed seen state + badges)
import { describe, it, expect, beforeEach, vi } from 'vitest';

let mockStore = {};

function deepMerge(target, src) {
  for (const [k, v] of Object.entries(src)) {
    if (v && typeof v === 'object' && !Array.isArray(v)
        && target[k] && typeof target[k] === 'object') {
      deepMerge(target[k], v);
    } else {
      target[k] = v;
    }
  }
}

vi.mock('firebase/firestore', () => ({
  collection: (db, ...segs) => ({ _type: 'col', _path: segs.join('/') }),
  doc: (dbOrCol, ...segs) => ({ _path: segs.join('/'), id: segs[segs.length - 1] }),
  getDoc: async (ref) => {
    const d = mockStore[ref._path];
    return { exists: () => !!d, data: () => d, id: ref.id };
  },
  setDoc: async (ref, data, opts) => {
    if (opts && opts.merge && mockStore[ref._path]) {
      const cur = JSON.parse(JSON.stringify(mockStore[ref._path]));
      deepMerge(cur, data);
      mockStore[ref._path] = cur;
    } else {
      mockStore[ref._path] = JSON.parse(JSON.stringify(data));
    }
  },
  query: (colRef, ...filters) => ({ _path: colRef._path, _filters: filters }),
  where: (field, op, value) => ({ field, op, value }),
  getDocs: async (q) => {
    const prefix = q._path + '/';
    const filters = q._filters || [];
    const docs = Object.entries(mockStore)
      .filter(([p]) => p.startsWith(prefix) && !p.slice(prefix.length).includes('/'))
      .map(([p, data]) => ({ id: p.split('/').pop(), data: () => data, exists: () => true }))
      .filter((d) => filters.every((f) => {
        const v = d.data()[f.field];
        if (f.op === '==') return v === f.value;
        if (f.op === 'array-contains') return Array.isArray(v) && v.includes(f.value);
        return true;
      }));
    return { docs };
  },
}));

const { loadSeen, syncSeen, markReadSeen, newCountForClub, clubActivity } =
  await import('../club-notify.js');

const fakeDb = {};
const JANE = 'jane doe';
// jsdom = localhost = dev lane collections
const SEEN_DOC = `club_seen_dev/${JANE}`;

beforeEach(() => {
  mockStore = {};
  localStorage.clear();
});

describe('markReadSeen', () => {
  it('writes local cache AND deep-merges the Firestore doc', async () => {
    await markReadSeen(fakeDb, JANE, 'c1', 'r1', 5);
    await markReadSeen(fakeDb, JANE, 'c1', 'r2', 2);
    expect(loadSeen().c1).toEqual({ r1: 5, r2: 2 });
    expect(mockStore[SEEN_DOC].seen.c1).toEqual({ r1: 5, r2: 2 });
  });

  it('never regresses on stale counts', async () => {
    await markReadSeen(fakeDb, JANE, 'c1', 'r1', 5);
    await markReadSeen(fakeDb, JANE, 'c1', 'r1', 3);
    expect(loadSeen().c1.r1).toBe(5);
  });

  it('signed out: local only, no Firestore write', async () => {
    await markReadSeen(fakeDb, null, 'c1', 'r1', 4);
    expect(loadSeen().c1.r1).toBe(4);
    expect(Object.keys(mockStore)).toHaveLength(0);
  });
});

describe('syncSeen', () => {
  it('merges remote and local taking the max per read', async () => {
    localStorage.setItem('ab_club_seen', JSON.stringify({ c1: { r1: 7, r2: 1 } }));
    mockStore[SEEN_DOC] = { seen: { c1: { r1: 3, r2: 9 }, c2: { r5: 4 } } };
    const merged = await syncSeen(fakeDb, JANE);
    expect(merged).toEqual({ c1: { r1: 7, r2: 9 }, c2: { r5: 4 } });
    expect(loadSeen()).toEqual(merged); // cached locally
  });

  it('falls back to local cache when signed out', async () => {
    localStorage.setItem('ab_club_seen', JSON.stringify({ c1: { r1: 2 } }));
    expect(await syncSeen(fakeDb, null)).toEqual({ c1: { r1: 2 } });
  });

  it('survives garbage in localStorage', async () => {
    localStorage.setItem('ab_club_seen', '{not json');
    expect(loadSeen()).toEqual({});
  });
});

describe('newCountForClub', () => {
  it('counts only unseen comments on active reads', async () => {
    await markReadSeen(fakeDb, JANE, 'c1', 'r1', 2);
    const reads = [
      { id: 'r1', status: 'active', commentCount: 6 },   // 4 new
      { id: 'r2', status: 'active', commentCount: 3 },   // 3 new (never seen)
      { id: 'r3', status: 'finished', commentCount: 9 }, // archived: ignored
    ];
    expect(newCountForClub('c1', reads)).toBe(7);
  });
});

describe('clubActivity', () => {
  it('uses the REMOTE seen state (fresh browser, synced account)', async () => {
    mockStore['clubs_dev/c1'] = { name: 'Side Babes', memberSlugs: [JANE] };
    mockStore['clubs_dev/c1/reads/r1'] = { status: 'active', commentCount: 6 };
    mockStore[SEEN_DOC] = { seen: { c1: { r1: 4 } } }; // seen on another device
    const activity = await clubActivity(fakeDb, JANE);
    expect(activity).toEqual([{ clubId: 'c1', name: 'Side Babes', newCount: 2 }]);
  });

  it('skips archived clubs and other people\'s clubs', async () => {
    mockStore['clubs_dev/c2'] = { name: 'Other', memberSlugs: ['bob brown'] };
    mockStore['clubs_dev/c3'] = { name: 'Dead', memberSlugs: [JANE], archived: true };
    expect(await clubActivity(fakeDb, JANE)).toEqual([]);
  });

  it('returns empty for signed-out users', async () => {
    expect(await clubActivity(fakeDb, null)).toEqual([]);
  });
});
