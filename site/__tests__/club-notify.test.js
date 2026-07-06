// @vitest-environment jsdom
// Feature: club post notifications (client-side seen-map badges)
import { describe, it, expect, beforeEach, vi } from 'vitest';

let mockStore = {};

vi.mock('firebase/firestore', () => ({
  collection: (db, ...segs) => ({ _type: 'col', _path: segs.join('/') }),
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

const { loadSeen, markReadSeen, newCountForClub, clubActivity } =
  await import('../club-notify.js');

const fakeDb = {};

beforeEach(() => {
  mockStore = {};
  localStorage.clear();
});

describe('seen map', () => {
  it('marking a read seen stores the count and never regresses', () => {
    markReadSeen('c1', 'r1', 5);
    markReadSeen('c1', 'r1', 3); // stale write must not lower it
    expect(loadSeen().c1.r1).toBe(5);
  });

  it('survives garbage in localStorage', () => {
    localStorage.setItem('ab_club_seen', '{not json');
    expect(loadSeen()).toEqual({});
  });
});

describe('newCountForClub', () => {
  it('counts only unseen comments on active reads', () => {
    markReadSeen('c1', 'r1', 2);
    const reads = [
      { id: 'r1', status: 'active', commentCount: 6 },   // 4 new
      { id: 'r2', status: 'active', commentCount: 3 },   // 3 new (never seen)
      { id: 'r3', status: 'finished', commentCount: 9 }, // archived: ignored
    ];
    expect(newCountForClub('c1', reads)).toBe(7);
  });

  it('is zero when fully caught up', () => {
    markReadSeen('c1', 'r1', 6);
    expect(newCountForClub('c1', [{ id: 'r1', status: 'active', commentCount: 6 }])).toBe(0);
  });
});

describe('clubActivity', () => {
  it('reports per-club unread totals for my clubs only, skipping archived', async () => {
    // jsdom = localhost = dev lane collections
    mockStore['clubs_dev/c1'] = { name: 'Side Babes', memberSlugs: ['jane doe'] };
    mockStore['clubs_dev/c1/reads/r1'] = { status: 'active', commentCount: 4 };
    mockStore['clubs_dev/c2'] = { name: 'Other Club', memberSlugs: ['bob brown'] };
    mockStore['clubs_dev/c2/reads/r9'] = { status: 'active', commentCount: 50 };
    mockStore['clubs_dev/c3'] = { name: 'Dead Club', memberSlugs: ['jane doe'], archived: true };

    const activity = await clubActivity(fakeDb, 'jane doe');
    expect(activity).toEqual([{ clubId: 'c1', name: 'Side Babes', newCount: 4 }]);
  });

  it('returns empty for signed-out users', async () => {
    expect(await clubActivity(fakeDb, null)).toEqual([]);
  });
});
