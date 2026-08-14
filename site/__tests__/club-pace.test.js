// @vitest-environment jsdom
// Feature: book-clubs backlog #6 — buddy-read pace graph (progress-over-time
// lines per member). History rides the EXISTING progress doc (see the
// "Buddy-read pace graph" section of club-reads.js) — no new subcollection,
// no rules change. These tests cover the append/thin mechanics, the pure
// pace/expected-line derivation, and that setProgress/setChapterProgress
// actually grow the history field on write.
import { describe, it, expect, beforeEach, vi } from 'vitest';

// --- In-memory Firestore mock (same shape as club-schedule.test.js) ---
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
    increment: (n) => ({ __inc: n }),
    query: (colRef) => ({ _path: colRef._path }),
    where: () => ({}),
    getDocs: async (q) => {
      const prefix = q._path + '/';
      const docs = Object.entries(mockStore)
        .filter(([p]) => p.startsWith(prefix) && !p.slice(prefix.length).includes('/'))
        .map(([p, data]) => ({ id: p.split('/').pop(), data: () => data, exists: () => true }));
      return { docs };
    },
    serverTimestamp: () => 'server-ts',
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
  MAX_PACE_HISTORY_POINTS, buildHistoryPoint, appendPaceHistory, thinPaceHistory,
  pacePosition, paceSeriesForMember, expectedPaceLine, paceAxisMax, paceAxisLabel,
  hasPaceData, paceAxisEndMs, assignPaceColors, computePaceGraphModel,
  paceScaleX, paceScaleY, PACE_PALETTE,
  setProgress, setChapterProgress, getProgressAll, getMyProgress,
} = await import('../club-reads.js');

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
const bob = { displayName: 'Bob Brown' };
const CLUB = 'club1';
const CLUB_PATH = 'clubs_dev/club1';

// Milestones fixture: positions 0..3; dues[i] optional (millis).
const makeMilestones = (dues = [], chaptered = false) =>
  [0, 1, 2, 3].map(i => ({
    id: `m${i}`, label: `Part ${i + 1}`, position: i,
    ...(chaptered ? { chStart: i * 5, chEnd: i * 5 + 4 } : {}),
    ...(typeof dues[i] === 'number' ? { dueAt: dues[i] } : {}),
  }));

beforeEach(() => {
  mockStore = { [CLUB_PATH]: { name: 'Test Club', activeSlots: [] } };
});

// ==================== buildHistoryPoint ====================

describe('buildHistoryPoint', () => {
  it('builds a point with an explicit timestamp', () => {
    expect(buildHistoryPoint(2, false, 1000)).toEqual({ position: 2, finished: false, at: 1000 });
  });

  it('coerces finished to a real boolean', () => {
    expect(buildHistoryPoint(1, undefined, 5)).toEqual({ position: 1, finished: false, at: 5 });
    expect(buildHistoryPoint(1, 1, 5).finished).toBe(true);
  });

  it('defaults `at` to Date.now() when omitted', () => {
    const before = Date.now();
    const pt = buildHistoryPoint(0, false);
    expect(pt.at).toBeGreaterThanOrEqual(before);
  });
});

// ==================== appendPaceHistory / thinPaceHistory ====================

describe('appendPaceHistory', () => {
  it('appends to an empty/missing history', () => {
    expect(appendPaceHistory(null, buildHistoryPoint(0, false, 1))).toEqual([{ position: 0, finished: false, at: 1 }]);
    expect(appendPaceHistory(undefined, buildHistoryPoint(0, false, 1))).toHaveLength(1);
  });

  it('appends a new point when position changes', () => {
    const h1 = appendPaceHistory([], buildHistoryPoint(0, false, 1));
    const h2 = appendPaceHistory(h1, buildHistoryPoint(1, false, 2));
    expect(h2).toEqual([{ position: 0, finished: false, at: 1 }, { position: 1, finished: false, at: 2 }]);
  });

  it('is a no-op when the position and finished state repeat the last point', () => {
    const h1 = appendPaceHistory([], buildHistoryPoint(2, false, 1));
    const h2 = appendPaceHistory(h1, buildHistoryPoint(2, false, 99));
    expect(h2).toBe(h1); // same reference: nothing appended
    expect(h2).toHaveLength(1);
  });

  it('appends when only the finished flag changes at the same position', () => {
    const h1 = appendPaceHistory([], buildHistoryPoint(3, false, 1));
    const h2 = appendPaceHistory(h1, buildHistoryPoint(3, true, 2));
    expect(h2).toHaveLength(2);
    expect(h2[1]).toEqual({ position: 3, finished: true, at: 2 });
  });

  it('thins once the cap is exceeded', () => {
    let history = [];
    for (let i = 0; i < MAX_PACE_HISTORY_POINTS + 10; i++) {
      history = appendPaceHistory(history, buildHistoryPoint(i, false, i));
    }
    expect(history.length).toBe(MAX_PACE_HISTORY_POINTS);
  });
});

describe('thinPaceHistory', () => {
  it('leaves a short history untouched', () => {
    const h = [buildHistoryPoint(0, false, 1), buildHistoryPoint(1, false, 2)];
    expect(thinPaceHistory(h, 100)).toBe(h);
  });

  it('keeps the very first point as a start-of-history anchor', () => {
    const h = Array.from({ length: 20 }, (_, i) => buildHistoryPoint(i, false, i));
    const thinned = thinPaceHistory(h, 10);
    expect(thinned).toHaveLength(10);
    expect(thinned[0]).toEqual(h[0]);
  });

  it('keeps the most recent points dense (the tail is untouched)', () => {
    const h = Array.from({ length: 20 }, (_, i) => buildHistoryPoint(i, false, i));
    const thinned = thinPaceHistory(h, 10);
    expect(thinned[thinned.length - 1]).toEqual(h[h.length - 1]);
    // Last 9 original points all survive alongside the anchor.
    expect(thinned.slice(1)).toEqual(h.slice(-9));
  });

  it('treats a non-array as empty', () => {
    expect(thinPaceHistory(null, 5)).toEqual([]);
    expect(thinPaceHistory(undefined, 5)).toEqual([]);
  });
});

// ==================== pacePosition / paceSeriesForMember ====================

describe('pacePosition', () => {
  it('reads milestonePosition for non-chaptered reads', () => {
    const ms = makeMilestones();
    expect(pacePosition({ position: 2, finished: false }, ms, false)).toBe(2);
  });

  it('maps a chapter index onto part-shaped milestones for chaptered reads', () => {
    const ms = makeMilestones([], true); // part i = ch 5i..5i+4
    expect(pacePosition({ position: 4, finished: false }, ms, true)).toBe(0);
    expect(pacePosition({ position: 11, finished: false }, ms, true)).toBe(1);
  });

  it('a finished point always lands on the last position', () => {
    const ms = makeMilestones();
    expect(pacePosition({ position: 0, finished: true }, ms, false)).toBe(3);
  });
});

describe('paceSeriesForMember', () => {
  it('sorts by time and normalizes position', () => {
    const ms = makeMilestones();
    const history = [
      buildHistoryPoint(2, false, 300),
      buildHistoryPoint(0, false, 100),
      buildHistoryPoint(1, false, 200),
    ];
    expect(paceSeriesForMember(history, ms, false)).toEqual([
      { atMs: 100, position: 0 },
      { atMs: 200, position: 1 },
      { atMs: 300, position: 2 },
    ]);
  });

  it('drops "not started" (-1) points', () => {
    const ms = makeMilestones();
    const history = [buildHistoryPoint(-1, false, 100), buildHistoryPoint(1, false, 200)];
    expect(paceSeriesForMember(history, ms, false)).toEqual([{ atMs: 200, position: 1 }]);
  });

  it('collapses consecutive duplicate positions', () => {
    const ms = makeMilestones();
    const history = [
      buildHistoryPoint(1, false, 100),
      buildHistoryPoint(1, false, 150), // e.g. a finished-flag-only re-save
      buildHistoryPoint(2, false, 200),
    ];
    expect(paceSeriesForMember(history, ms, false)).toEqual([
      { atMs: 100, position: 1 },
      { atMs: 200, position: 2 },
    ]);
  });

  it('degrades gracefully for a progress doc with no history field (pre-feature read)', () => {
    expect(paceSeriesForMember(undefined, makeMilestones(), false)).toEqual([]);
    expect(paceSeriesForMember(null, makeMilestones(), false)).toEqual([]);
  });
});

// ==================== expectedPaceLine / paceAxisMax ====================

describe('expectedPaceLine', () => {
  it('builds one point per dated milestone, in position order', () => {
    const ms = makeMilestones([500, 100, undefined, 900]); // positions 0,1,3 dated
    expect(expectedPaceLine(ms)).toEqual([
      { atMs: 500, position: 0 },
      { atMs: 100, position: 1 },
      { atMs: 900, position: 3 },
    ]);
  });

  it('is empty when nothing is dated', () => {
    expect(expectedPaceLine(makeMilestones())).toEqual([]);
    expect(expectedPaceLine(null)).toEqual([]);
  });
});

describe('paceAxisMax', () => {
  it('is the highest milestone position', () => {
    expect(paceAxisMax(makeMilestones())).toBe(3);
  });

  it('is 0 for no milestones', () => {
    expect(paceAxisMax([])).toBe(0);
    expect(paceAxisMax(null)).toBe(0);
  });
});

// ==================== paceAxisLabel (spoiler care: numeric/percent only) ====================

describe('paceAxisLabel', () => {
  it('renders a percent string', () => {
    expect(paceAxisLabel(2, 4)).toBe('50%');
    expect(paceAxisLabel(4, 4)).toBe('100%');
    expect(paceAxisLabel(0, 4)).toBe('0%');
  });

  it('never contains a milestone title — numbers and a percent sign only', () => {
    for (const [pos, max] of [[0, 0], [-1, 4], [3, 4], [4, 4]]) {
      expect(paceAxisLabel(pos, max)).toMatch(/^\d+%$/);
    }
  });

  it('guards against a zero/negative axis max', () => {
    expect(paceAxisLabel(5, 0)).toBe('0%');
    expect(paceAxisLabel(5, -1)).toBe('0%');
  });
});

// ==================== hasPaceData ====================

describe('hasPaceData', () => {
  it('is false when every member series is empty', () => {
    expect(hasPaceData({ jane: [], bob: [] })).toBe(false);
    expect(hasPaceData({})).toBe(false);
    expect(hasPaceData(null)).toBe(false);
  });

  it('is true once any member has a point', () => {
    expect(hasPaceData({ jane: [], bob: [{ atMs: 1, position: 0 }] })).toBe(true);
  });
});

// ==================== paceAxisEndMs (frozen graph for finished reads) ====================

describe('paceAxisEndMs', () => {
  it('is "now" for an active read', () => {
    expect(paceAxisEndMs(true, null, [], 5000)).toBe(5000);
  });

  it('is the finish time for a finished read', () => {
    expect(paceAxisEndMs(false, 4000, [1000, 2000], 9999)).toBe(4000);
  });

  it('falls back to the latest data point when no finish time is known', () => {
    expect(paceAxisEndMs(false, null, [1000, 3000, 2000], 9999)).toBe(3000);
  });

  it('falls back to now when there is no finish time AND no data', () => {
    expect(paceAxisEndMs(false, null, [], 9999)).toBe(9999);
  });
});

// ==================== assignPaceColors ====================

describe('assignPaceColors', () => {
  it('assigns one palette slot per member, in the given order', () => {
    const colors = assignPaceColors(['a', 'b', 'c']);
    expect(colors.a.color).toBe(PACE_PALETTE[0]);
    expect(colors.b.color).toBe(PACE_PALETTE[1]);
    expect(colors.c.color).toBe(PACE_PALETTE[2]);
    expect(colors.a.dashed).toBe(false);
  });

  it('is deterministic for the same input order', () => {
    const slugs = ['jane', 'bob', 'ann'];
    expect(assignPaceColors(slugs)).toEqual(assignPaceColors([...slugs]));
  });

  it('recycles hues with a dashed flag past the 8-color palette (legible up to ~12)', () => {
    const slugs = Array.from({ length: 12 }, (_, i) => `m${i}`);
    const colors = assignPaceColors(slugs);
    expect(colors.m0.dashed).toBe(false);
    expect(colors.m7.dashed).toBe(false);
    expect(colors.m8.dashed).toBe(true);
    expect(colors.m8.color).toBe(PACE_PALETTE[0]); // recycled hue...
    expect(colors.m0.color).toBe(colors.m8.color);  // ...same hue as slot 0...
    // ...but the dashed flag tells them apart even at identical hue.
    expect(colors.m0.dashed).not.toBe(colors.m8.dashed);
    expect(colors.m11.dashed).toBe(true);
  });

  it('handles an empty/missing member list', () => {
    expect(assignPaceColors([])).toEqual({});
    expect(assignPaceColors(null)).toEqual({});
  });
});

// ==================== paceScaleX / paceScaleY ====================

describe('paceScaleX / paceScaleY', () => {
  it('scales time linearly across the plot width', () => {
    expect(paceScaleX(0, 0, 100, 200)).toBe(0);
    expect(paceScaleX(50, 0, 100, 200)).toBe(100);
    expect(paceScaleX(100, 0, 100, 200)).toBe(200);
  });

  it('guards a degenerate/zero time domain', () => {
    expect(paceScaleX(5, 10, 10, 200)).toBe(0);
    expect(paceScaleX(5, 20, 10, 200)).toBe(0);
  });

  it('scales position with 0 at the bottom (SVG y grows downward)', () => {
    expect(paceScaleY(0, 4, 100)).toBe(100);
    expect(paceScaleY(4, 4, 100)).toBe(0);
    expect(paceScaleY(2, 4, 100)).toBe(50);
  });

  it('guards a degenerate/zero position domain', () => {
    expect(paceScaleY(2, 0, 100)).toBe(100);
    expect(paceScaleY(2, -1, 100)).toBe(100);
  });

  it('clamps a negative position to the baseline', () => {
    expect(paceScaleY(-1, 4, 100)).toBe(100);
  });
});

// ==================== computePaceGraphModel (integration) ====================

describe('computePaceGraphModel', () => {
  it('reports empty when no progress doc has any history', () => {
    const model = computePaceGraphModel(
      [{ slug: 'jane doe' }, { slug: 'bob brown' }], makeMilestones(), false,
      { status: 'active', finishedAtMs: null }, 1000);
    expect(model.empty).toBe(true);
    expect(model.frozen).toBe(false);
    expect(model.todayMs).toBe(1000);
  });

  it('builds series + expected line + a shared domain for an active read', () => {
    const ms = makeMilestones([500, 1500, undefined, 3000]);
    const progressAll = [
      { slug: 'jane doe', history: [buildHistoryPoint(0, false, 100), buildHistoryPoint(2, false, 900)] },
      { slug: 'bob brown', history: [buildHistoryPoint(1, false, 400)] },
    ];
    const model = computePaceGraphModel(progressAll, ms, false, { status: 'active', finishedAtMs: null }, 2000);
    expect(model.empty).toBe(false);
    expect(model.maxPosition).toBe(3);
    expect(model.seriesByMember['jane doe']).toHaveLength(2);
    expect(model.seriesByMember['bob brown']).toHaveLength(1);
    expect(model.expected).toEqual([
      { atMs: 500, position: 0 }, { atMs: 1500, position: 1 }, { atMs: 3000, position: 3 },
    ]);
    expect(model.frozen).toBe(false);
    expect(model.todayMs).toBe(2000);
    expect(model.endMs).toBe(2000); // active: always "now", even though data + expected points are earlier
    expect(model.startMs).toBe(100); // earliest of all series + expected points
  });

  it('has no expected line when the read has no schedule set, even with milestones', () => {
    const model = computePaceGraphModel(
      [{ slug: 'jane doe', history: [buildHistoryPoint(1, false, 100)] }],
      makeMilestones(), false, { status: 'active', finishedAtMs: null }, 2000);
    expect(model.expected).toEqual([]);
  });

  it('freezes the graph for a finished read (no live "today" marker)', () => {
    const progressAll = [{ slug: 'jane doe', history: [buildHistoryPoint(3, true, 500)] }];
    const model = computePaceGraphModel(progressAll, makeMilestones(), false,
      { status: 'finished', finishedAtMs: 800 }, 999999);
    expect(model.frozen).toBe(true);
    expect(model.todayMs).toBeNull();
    expect(model.endMs).toBe(800); // the read's own finish time, not "now"
  });

  it('falls back to the last data point when a finished read has no finishedAtMs', () => {
    const progressAll = [{ slug: 'jane doe', history: [buildHistoryPoint(3, true, 750)] }];
    const model = computePaceGraphModel(progressAll, makeMilestones(), false,
      { status: 'abandoned', finishedAtMs: null }, 999999);
    expect(model.endMs).toBe(750);
  });
});

// ==================== Firestore integration: setProgress/setChapterProgress ====================

describe('progress history writes', () => {
  it('setProgress grows history across successive calls', async () => {
    await setProgress(fakeDb, CLUB, 'read1', 0, jane);
    await setProgress(fakeDb, CLUB, 'read1', 2, jane);
    const doc = await getMyProgress(fakeDb, CLUB, 'read1', 'jane doe');
    expect(doc.history).toEqual([
      { position: 0, finished: false, at: expect.any(Number) },
      { position: 2, finished: false, at: expect.any(Number) },
    ]);
    expect(doc.milestonePosition).toBe(2); // current-position field is unaffected
  });

  it('setChapterProgress grows its own history the same way', async () => {
    await setChapterProgress(fakeDb, CLUB, 'read1', 5, bob);
    await setChapterProgress(fakeDb, CLUB, 'read1', 9, bob);
    const doc = await getMyProgress(fakeDb, CLUB, 'read1', 'bob brown');
    expect(doc.history.map(p => p.position)).toEqual([5, 9]);
  });

  it('does not grow history on a true repeat write (same position + finished)', async () => {
    await setProgress(fakeDb, CLUB, 'read1', 1, jane);
    await setProgress(fakeDb, CLUB, 'read1', 1, jane);
    const doc = await getMyProgress(fakeDb, CLUB, 'read1', 'jane doe');
    expect(doc.history).toHaveLength(1);
  });

  it('caps growth at MAX_PACE_HISTORY_POINTS across many real writes', async () => {
    for (let i = 0; i < MAX_PACE_HISTORY_POINTS + 15; i++) {
      await setProgress(fakeDb, CLUB, 'read1', i, jane);
    }
    const doc = await getMyProgress(fakeDb, CLUB, 'read1', 'jane doe');
    expect(doc.history.length).toBe(MAX_PACE_HISTORY_POINTS);
    expect(doc.history[0].position).toBe(0); // anchor survives
    expect(doc.history[doc.history.length - 1].position).toBe(MAX_PACE_HISTORY_POINTS + 14); // latest survives
  });

  it('a progress doc written before this feature has no history field, and reads back as empty', async () => {
    // Simulates a pre-feature doc: no `history` key at all.
    mockStore[`${CLUB_PATH}/reads/read1/progress/ann appleseed`] = {
      displayName: 'Ann Appleseed', milestonePosition: 2, finished: false, updatedAt: 'server-ts',
    };
    const all = await getProgressAll(fakeDb, CLUB, 'read1');
    const ann = all.find(p => p.slug === 'ann appleseed');
    expect(ann.history).toBeUndefined();
    expect(paceSeriesForMember(ann.history, makeMilestones(), false)).toEqual([]);
  });

  it('requires a session for both progress setters', async () => {
    expect((await setProgress(fakeDb, CLUB, 'read1', 1, null)).success).toBe(false);
    expect((await setChapterProgress(fakeDb, CLUB, 'read1', 1, null)).success).toBe(false);
  });
});
