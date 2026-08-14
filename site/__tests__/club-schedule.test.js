// @vitest-environment jsdom
// Feature: book-clubs backlog #1 — reading schedule (due dates on milestones,
// on-track/behind verdicts) + the club feature-toggle map and the per-club
// Discord webhook setting that gate/serve it.
import { describe, it, expect, beforeEach, vi } from 'vitest';

// --- In-memory Firestore mock (same shape as club-reads.test.js, + arrayUnion) ---
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
      else if (v && typeof v === 'object' && '__arrayUnion' in v) {
        const arr = Array.isArray(current[k]) ? [...current[k]] : [];
        if (!arr.includes(v.__arrayUnion)) arr.push(v.__arrayUnion);
        next[k] = arr;
      } else next[k] = v;
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
    getDoc: async (ref) => makeSnap(ref._path),
    setDoc: async (ref, data) => { mockStore[ref._path] = { ...data }; },
    updateDoc: async (ref, data) => { applyUpdate(ref._path, data); },
    deleteDoc: async (ref) => { delete mockStore[ref._path]; },
    increment: (n) => ({ __inc: n }),
    arrayUnion: (v) => ({ __arrayUnion: v }),
    query: (colRef, ...filters) => ({ _path: colRef._path, _filters: filters }),
    where: (field, op, value) => ({ field, op, value }),
    getDocs: async (q) => {
      const prefix = q._path + '/';
      const filters = q._filters || [];
      const docs = Object.entries(mockStore)
        .filter(([p]) => p.startsWith(prefix) && !p.slice(prefix.length).includes('/'))
        .map(([p, data]) => ({ id: p.split('/').pop(), data: () => data, exists: () => true }))
        .filter((d) =>
          filters.every((f) => {
            const v = d.data()[f.field];
            if (f.op === '==') return v === f.value;
            if (f.op === 'array-contains') return Array.isArray(v) && v.includes(f.value);
            return true;
          })
        );
      return { docs };
    },
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
  dateInputToDueAt, dueAtToDateInput, formatDueDate, spreadScheduleDates,
  hasSchedule, expectedSchedulePosition, memberSchedulePosition,
  nextDueMilestone, scheduleStatus, setReadSchedule,
} = await import('../club-reads.js');

const {
  FEATURE_DEFAULTS, clubFeatureEnabled,
  isValidDiscordWebhook, maskWebhookUrl,
  setClubDiscordWebhook, clearClubDiscordWebhook, updateClubDetails,
} = await import('../clubs.js');

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
// col() resolves to *_dev under jsdom (localhost = dev lane)
const CLUB_PATH = 'clubs_dev/club1';
const READ_PATH = `${CLUB_PATH}/reads/read1`;

// Milestones fixture: positions 0..3; dues[i] (millis) is optional.
// chaptered=true adds part-shaped chapter ranges: part i = chapters 5i..5i+4.
const makeMilestones = (dues = [], chaptered = false) =>
  [0, 1, 2, 3].map(i => ({
    id: `m${i}`, label: `Part ${i + 1}`, position: i,
    ...(chaptered ? { chStart: i * 5, chEnd: i * 5 + 4 } : {}),
    ...(typeof dues[i] === 'number' ? { dueAt: dues[i] } : {}),
  }));

// Local-time anchors so assertions hold in any timezone.
const due = (d) => dateInputToDueAt(`2026-08-${String(d).padStart(2, '0')}`);
const noonOn = (d) => new Date(2026, 7, d, 12).getTime(); // Aug d, local noon

beforeEach(() => {
  mockStore = { [CLUB_PATH]: { name: 'Test Club', activeSlots: [] } };
});

// ==================== Pure date helpers ====================

describe('due-date input helpers', () => {
  it('round-trips a date input through millis in local time', () => {
    const ms = dateInputToDueAt('2026-08-20');
    expect(dueAtToDateInput(ms)).toBe('2026-08-20');
  });

  it('anchors dueAt at local end-of-day (matches the nextMeetingAt millis convention)', () => {
    const d = new Date(dateInputToDueAt('2026-08-20'));
    expect([d.getHours(), d.getMinutes(), d.getSeconds(), d.getMilliseconds()])
      .toEqual([23, 59, 59, 999]);
    expect(d.getDate()).toBe(20);
  });

  it('rejects blank and malformed inputs', () => {
    expect(dateInputToDueAt('')).toBeNull();
    expect(dateInputToDueAt(null)).toBeNull();
    expect(dateInputToDueAt('garbage')).toBeNull();
    expect(dateInputToDueAt('2026-8-2')).toBeNull();
  });

  it('renders empty strings for unset values', () => {
    expect(dueAtToDateInput(null)).toBe('');
    expect(dueAtToDateInput(undefined)).toBe('');
    expect(formatDueDate(null)).toBe('');
  });

  it('formats a short local label', () => {
    expect(formatDueDate(due(20))).toMatch(/Aug/);
    expect(formatDueDate(due(20))).toMatch(/20/);
  });
});

describe('spreadScheduleDates', () => {
  it('spreads evenly and lands the last milestone on the finish date', () => {
    const r = spreadScheduleDates(4, '2026-08-01', '2026-08-09');
    expect(r.error).toBeUndefined();
    expect(r.dates).toEqual(['2026-08-03', '2026-08-05', '2026-08-07', '2026-08-09']);
  });

  it('handles a single milestone (due on the finish date)', () => {
    expect(spreadScheduleDates(1, '2026-08-01', '2026-08-31').dates).toEqual(['2026-08-31']);
  });

  it('never goes backwards even when milestones outnumber days', () => {
    const r = spreadScheduleDates(6, '2026-08-01', '2026-08-03');
    expect(r.error).toBeUndefined();
    for (let i = 1; i < r.dates.length; i++) {
      expect(r.dates[i] >= r.dates[i - 1]).toBe(true);
    }
    expect(r.dates[r.dates.length - 1]).toBe('2026-08-03');
  });

  it('crosses month boundaries correctly', () => {
    const r = spreadScheduleDates(2, '2026-08-28', '2026-09-05');
    expect(r.dates).toEqual(['2026-09-01', '2026-09-05']);
  });

  it('errors on missing or reversed dates', () => {
    expect(spreadScheduleDates(4, '2026-08-01', '').error).toBeTruthy();
    expect(spreadScheduleDates(4, '', '2026-08-09').error).toBeTruthy();
    expect(spreadScheduleDates(4, '2026-08-09', '2026-08-01').error).toBeTruthy();
    expect(spreadScheduleDates(0, '2026-08-01', '2026-08-09').error).toBeTruthy();
  });
});

// ==================== Schedule verdict logic ====================

describe('hasSchedule / expectedSchedulePosition / nextDueMilestone', () => {
  it('detects whether any milestone carries a due date', () => {
    expect(hasSchedule(makeMilestones())).toBe(false);
    expect(hasSchedule(makeMilestones([due(5)]))).toBe(true);
    expect(hasSchedule(null)).toBe(false);
  });

  it('expected position is the last past-due milestone', () => {
    const ms = makeMilestones([due(5), due(10), due(15), due(20)]);
    expect(expectedSchedulePosition(ms, noonOn(1))).toBe(-1);
    expect(expectedSchedulePosition(ms, noonOn(7))).toBe(0);
    expect(expectedSchedulePosition(ms, noonOn(17))).toBe(2);
    expect(expectedSchedulePosition(ms, noonOn(30))).toBe(3);
  });

  it('a milestone is not due until its day fully ends', () => {
    const ms = makeMilestones([due(5)]);
    expect(expectedSchedulePosition(ms, noonOn(5))).toBe(-1); // noon on the due day: still on time
  });

  it('skips undated milestones in a partial schedule', () => {
    const ms = makeMilestones([undefined, due(10), undefined, due(20)]);
    expect(expectedSchedulePosition(ms, noonOn(12))).toBe(1);
    expect(nextDueMilestone(ms, noonOn(12)).position).toBe(3);
  });

  it('nextDueMilestone returns the soonest future due, or null', () => {
    const ms = makeMilestones([due(5), due(10), due(15), due(20)]);
    expect(nextDueMilestone(ms, noonOn(7)).position).toBe(1);
    expect(nextDueMilestone(ms, noonOn(25))).toBeNull();
  });
});

describe('memberSchedulePosition', () => {
  it('is -1 with no progress doc', () => {
    expect(memberSchedulePosition(makeMilestones(), null, false)).toBe(-1);
    expect(memberSchedulePosition(makeMilestones(), undefined, true)).toBe(-1);
  });

  it('uses milestonePosition for milestone-based reads', () => {
    expect(memberSchedulePosition(makeMilestones(), { milestonePosition: 2 }, false)).toBe(2);
    expect(memberSchedulePosition(makeMilestones(), { milestonePosition: -1 }, false)).toBe(-1);
  });

  it('maps chapterIndex onto part-shaped milestones via chEnd', () => {
    const ms = makeMilestones([], true); // part i = ch 5i..5i+4
    expect(memberSchedulePosition(ms, { chapterIndex: -1 }, true)).toBe(-1);
    expect(memberSchedulePosition(ms, { chapterIndex: 3 }, true)).toBe(-1);  // mid part 1
    expect(memberSchedulePosition(ms, { chapterIndex: 4 }, true)).toBe(0);   // end of part 1
    expect(memberSchedulePosition(ms, { chapterIndex: 11 }, true)).toBe(1);  // mid part 3
    expect(memberSchedulePosition(ms, { chapterIndex: 19 }, true)).toBe(3);
  });

  it('finished flag counts as the last position regardless of indices', () => {
    expect(memberSchedulePosition(makeMilestones(), { finished: true }, false)).toBe(3);
    expect(memberSchedulePosition(makeMilestones([], true), { finished: true, chapterIndex: 0 }, true)).toBe(3);
  });
});

describe('scheduleStatus', () => {
  const dated = () => makeMilestones([due(5), due(10), due(15), due(20)]);

  it("is 'none' when no due dates are set", () => {
    expect(scheduleStatus(makeMilestones(), { milestonePosition: 0 }, false, noonOn(12)).status).toBe('none');
  });

  it("is 'done' for finished members", () => {
    expect(scheduleStatus(dated(), { finished: true }, false, noonOn(30)).status).toBe('done');
    expect(scheduleStatus(dated(), { milestonePosition: 3 }, false, noonOn(30)).status).toBe('done');
  });

  it("is 'on-track' before anything is due, even when not started", () => {
    expect(scheduleStatus(dated(), null, false, noonOn(1)).status).toBe('on-track');
    expect(scheduleStatus(dated(), { milestonePosition: -1 }, false, noonOn(1)).status).toBe('on-track');
  });

  it("is 'on-track' when progress meets or beats the schedule", () => {
    expect(scheduleStatus(dated(), { milestonePosition: 1 }, false, noonOn(12)).status).toBe('on-track');
    expect(scheduleStatus(dated(), { milestonePosition: 2 }, false, noonOn(12)).status).toBe('on-track');
  });

  it("is 'behind' with a count of past-due sections not completed", () => {
    const r = scheduleStatus(dated(), { milestonePosition: -1 }, false, noonOn(12));
    expect(r.status).toBe('behind');
    expect(r.behindBy).toBe(2); // parts 1 and 2 were due Aug 5 / Aug 10
    expect(scheduleStatus(dated(), { milestonePosition: 0 }, false, noonOn(12)).behindBy).toBe(1);
  });

  it('judges chaptered reads through chapter progress', () => {
    const ms = makeMilestones([due(5), due(10), due(15), due(20)], true);
    expect(scheduleStatus(ms, { chapterIndex: 9 }, true, noonOn(12)).status).toBe('on-track');
    expect(scheduleStatus(ms, { chapterIndex: 3 }, true, noonOn(12)).status).toBe('behind');
  });

  it('ignores undated milestones when counting behindBy', () => {
    const ms = makeMilestones([undefined, due(10), undefined, due(20)]);
    const r = scheduleStatus(ms, { milestonePosition: -1 }, false, noonOn(12));
    expect(r.status).toBe('behind');
    expect(r.behindBy).toBe(1); // only part 2 has a passed date
  });
});

// ==================== setReadSchedule (Firestore writes) ====================

describe('setReadSchedule', () => {
  beforeEach(() => {
    mockStore[READ_PATH] = {
      bookTitle: 'Dungeon Crawler Carl', status: 'active',
      milestones: makeMilestones(),
    };
  });

  it('writes dueAt per milestone and stamps scheduleUpdatedAt', async () => {
    const r = await setReadSchedule(fakeDb, 'club1', 'read1', [due(5), due(10), null, due(20)]);
    expect(r.success).toBe(true);
    const saved = mockStore[READ_PATH];
    expect(saved.milestones[0].dueAt).toBe(due(5));
    expect(saved.milestones[1].dueAt).toBe(due(10));
    expect('dueAt' in saved.milestones[2]).toBe(false);
    expect(saved.milestones[3].dueAt).toBe(due(20));
    expect(saved.scheduleUpdatedAt).toBe('server-ts');
  });

  it('clears previously set dates when passed nulls', async () => {
    mockStore[READ_PATH].milestones = makeMilestones([due(5), due(10), due(15), due(20)]);
    const r = await setReadSchedule(fakeDb, 'club1', 'read1', [null, null, null, null]);
    expect(r.success).toBe(true);
    expect(hasSchedule(mockStore[READ_PATH].milestones)).toBe(false);
  });

  it('preserves milestone fields (labels, chapter ranges) untouched', async () => {
    mockStore[READ_PATH].milestones = makeMilestones([], true);
    await setReadSchedule(fakeDb, 'club1', 'read1', [due(5), null, null, null]);
    const saved = mockStore[READ_PATH].milestones;
    expect(saved[0]).toMatchObject({ id: 'm0', label: 'Part 1', position: 0, chStart: 0, chEnd: 4 });
    expect(saved[2]).toMatchObject({ id: 'm2', label: 'Part 3', chStart: 10, chEnd: 14 });
    expect(mockStore[READ_PATH].bookTitle).toBe('Dungeon Crawler Carl');
    expect(mockStore[READ_PATH].status).toBe('active');
  });

  it('aligns dates by position even if the stored array is shuffled', async () => {
    mockStore[READ_PATH].milestones = [...makeMilestones()].reverse();
    await setReadSchedule(fakeDb, 'club1', 'read1', [due(5), due(10), due(15), due(20)]);
    const byPos = Object.fromEntries(mockStore[READ_PATH].milestones.map(m => [m.position, m.dueAt]));
    expect(byPos[0]).toBe(due(5));
    expect(byPos[3]).toBe(due(20));
  });

  it('errors on a missing read', async () => {
    const r = await setReadSchedule(fakeDb, 'club1', 'nope', [due(5)]);
    expect(r.success).toBe(false);
    expect(r.error).toMatch(/not found/i);
  });
});

// ==================== Club feature toggles ====================

describe('club feature toggles', () => {
  it('readingSchedule defaults OFF for clubs without a features map', () => {
    expect(FEATURE_DEFAULTS.readingSchedule).toBe(false);
    expect(clubFeatureEnabled({ name: 'Old Club' }, 'readingSchedule')).toBe(false);
    expect(clubFeatureEnabled(null, 'readingSchedule')).toBe(false);
  });

  it('honors explicit per-club settings over the default', () => {
    expect(clubFeatureEnabled({ features: { readingSchedule: true } }, 'readingSchedule')).toBe(true);
    expect(clubFeatureEnabled({ features: { readingSchedule: false } }, 'readingSchedule')).toBe(false);
  });

  it('unknown keys are off', () => {
    expect(clubFeatureEnabled({ features: { readingSchedule: true } }, 'polls')).toBe(false);
  });

  it('discordPollAnnouncements defaults OFF independently of discordAnnouncements', () => {
    expect(FEATURE_DEFAULTS.discordPollAnnouncements).toBe(false);
    expect(clubFeatureEnabled({ name: 'Old Club' }, 'discordPollAnnouncements')).toBe(false);
    // The master toggle being on does not imply the poll sub-toggle is on.
    expect(clubFeatureEnabled(
      { features: { discordAnnouncements: true } }, 'discordPollAnnouncements'
    )).toBe(false);
    expect(clubFeatureEnabled(
      { features: { discordAnnouncements: true, discordPollAnnouncements: true } },
      'discordPollAnnouncements'
    )).toBe(true);
  });

  it('updateClubDetails saves a cleaned features map (unknown keys dropped)', async () => {
    const r = await updateClubDetails(fakeDb, 'club1', {
      features: { readingSchedule: true, evilKey: 'payload' },
    });
    expect(r.success).toBe(true);
    expect(mockStore[CLUB_PATH].features).toEqual({ readingSchedule: true });
  });

  it('updateClubDetails keeps discordPollAnnouncements through the cleaning pass', async () => {
    const r = await updateClubDetails(fakeDb, 'club1', {
      features: { discordAnnouncements: true, discordPollAnnouncements: true, evilKey: 'payload' },
    });
    expect(r.success).toBe(true);
    expect(mockStore[CLUB_PATH].features).toEqual({
      discordAnnouncements: true, discordPollAnnouncements: true,
    });
  });

  it('updateClubDetails rejects a non-object features value', async () => {
    const r = await updateClubDetails(fakeDb, 'club1', { features: 'yes' });
    expect(r.success).toBe(false);
    expect(mockStore[CLUB_PATH].features).toBeUndefined();
  });
});

// ==================== Discord webhook setting ====================

describe('discord webhook setting', () => {
  const GOOD = 'https://discord.com/api/webhooks/123456789012345678/aBcD_eFgH-1234567890';

  it('validates real webhook URLs only', () => {
    expect(isValidDiscordWebhook(GOOD)).toBe(true);
    expect(isValidDiscordWebhook('https://discordapp.com/api/webhooks/1/t-ok_en')).toBe(true);
    expect(isValidDiscordWebhook(`  ${GOOD}  `)).toBe(true); // trimmed
    expect(isValidDiscordWebhook('http://discord.com/api/webhooks/1/token')).toBe(false);
    expect(isValidDiscordWebhook('https://evil.com/api/webhooks/1/token')).toBe(false);
    expect(isValidDiscordWebhook('https://discord.com.evil.com/api/webhooks/1/t')).toBe(false);
    expect(isValidDiscordWebhook('https://discord.com/api/webhooks/notanumber/t')).toBe(false);
    expect(isValidDiscordWebhook('')).toBe(false);
    expect(isValidDiscordWebhook(null)).toBe(false);
  });

  it('masks to the last four characters only', () => {
    expect(maskWebhookUrl(GOOD)).toBe('…7890');
    expect(maskWebhookUrl('')).toBe('');
  });

  it('stores the URL in the write-only settings subdoc and the mask on the club', async () => {
    const r = await setClubDiscordWebhook(fakeDb, 'club1', GOOD, jane);
    expect(r.success).toBe(true);
    const settings = mockStore[`${CLUB_PATH}/settings/discord`];
    expect(settings.webhookUrl).toBe(GOOD);
    expect(settings.updatedBy).toBe('Jane Doe');
    expect(mockStore[CLUB_PATH].discordWebhookMask).toBe('…7890');
    // the capability never lands on the world-readable club doc
    expect(JSON.stringify(mockStore[CLUB_PATH])).not.toContain('webhooks');
  });

  it('rejects invalid URLs without writing anything', async () => {
    const r = await setClubDiscordWebhook(fakeDb, 'club1', 'https://evil.com/hook', jane);
    expect(r.success).toBe(false);
    expect(mockStore[`${CLUB_PATH}/settings/discord`]).toBeUndefined();
    expect(mockStore[CLUB_PATH].discordWebhookMask).toBeUndefined();
  });

  it('clear removes the subdoc and blanks the mask', async () => {
    await setClubDiscordWebhook(fakeDb, 'club1', GOOD, jane);
    const r = await clearClubDiscordWebhook(fakeDb, 'club1');
    expect(r.success).toBe(true);
    expect(mockStore[`${CLUB_PATH}/settings/discord`]).toBeUndefined();
    expect(mockStore[CLUB_PATH].discordWebhookMask).toBe('');
  });
});
