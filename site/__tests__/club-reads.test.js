// @vitest-environment jsdom
// Feature: book-clubs-phase2 — reads, milestones, comments, progress + spoiler shield
import { describe, it, expect, beforeEach, vi } from 'vitest';

// --- In-memory Firestore mock (superset of clubs.test.js: adds updateDoc/increment) ---
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
      next[k] = v && typeof v === 'object' && '__inc' in v ? (current[k] || 0) + v.__inc : v;
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
  parseHhmm, formatHhmm, splitByDuration, parseManualMilestones,
  isMilestoneLocked, parseCsv,
  milestonesFromChapters, milestonesFromChapterRanges,
  milestonesFromParts, wholeBookMilestones,
  startRead, getReads, getRead,
  addComment, deleteComment, getComments,
  setProgress, getProgressAll,
  MAX_MILESTONES, GENERAL_MILESTONE,
} = await import('../club-reads.js');

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
const bob = { displayName: 'Bob Brown' };
// col() resolves to clubs_dev under jsdom (localhost = dev lane)
const CLUB = 'club1';
const CLUB_PATH = 'clubs_dev/club1';

const bookInput = (over = {}) => ({
  bookTitle: 'Dungeon Crawler Carl',
  bookAuthor: 'Matt Dinniman',
  coverHref: 'covers/Matt Dinniman/Dungeon Crawler Carl.jpg',
  durationMinutes: 800,
  milestones: splitByDuration(800, 4),
  ...over,
});

beforeEach(() => {
  mockStore = { [CLUB_PATH]: { name: 'Test Club', activeSlots: [] } };
});

describe('duration utils', () => {
  it('parses and formats hh:mm', () => {
    expect(parseHhmm('10:07')).toBe(607);
    expect(parseHhmm('0:45')).toBe(45);
    expect(parseHhmm('')).toBe(0);
    expect(parseHhmm('garbage')).toBe(0);
    expect(formatHhmm(607)).toBe('10:07');
    expect(formatHhmm(45)).toBe('0:45');
  });
});

describe('splitByDuration', () => {
  it('splits into n contiguous parts covering the full duration', () => {
    const ms = splitByDuration(600, 4);
    expect(ms).toHaveLength(4);
    expect(ms[0].label).toBe('Part 1 (0:00–2:30)');
    expect(ms[3].label).toBe('Part 4 (7:30–10:00)');
    expect(ms.map((m) => m.position)).toEqual([0, 1, 2, 3]);
  });

  it('clamps n to sane bounds', () => {
    expect(splitByDuration(600, 0)).toHaveLength(1);
    expect(splitByDuration(600, 99)).toHaveLength(MAX_MILESTONES);
  });

  it('omits time ranges when duration is unknown', () => {
    expect(splitByDuration(0, 3)[0].label).toBe('Part 1');
  });
});

describe('parseManualMilestones', () => {
  it('parses one label per line, skipping blanks', () => {
    const { milestones } = parseManualMilestones('Ch 1-5\n\n  Ch 6-10  \nCh 11-end\n');
    expect(milestones.map((m) => m.label)).toEqual(['Ch 1-5', 'Ch 6-10', 'Ch 11-end']);
    expect(milestones[2]).toEqual({ id: 'm2', label: 'Ch 11-end', position: 2 });
  });

  it('rejects empty input and too many lines', () => {
    expect(parseManualMilestones('  \n ').error).toBeDefined();
    expect(parseManualMilestones(Array(MAX_MILESTONES + 1).fill('x').join('\n')).error).toBeDefined();
  });
});

describe('chapter-based milestones', () => {
  const chapters = (n) => Array.from({ length: n }, (_, i) => ({ title: `Chapter ${i + 1}`, start_min: i * 10 }));

  it('one milestone per chapter, labeled by chapter title', () => {
    const { milestones } = milestonesFromChapters(chapters(3));
    expect(milestones.map((m) => m.label)).toEqual(['Chapter 1', 'Chapter 2', 'Chapter 3']);
    expect(milestones[2]).toEqual({ id: 'm2', label: 'Chapter 3', position: 2 });
  });

  it('rejects one-per-chapter beyond MAX_MILESTONES', () => {
    expect(milestonesFromChapters(chapters(MAX_MILESTONES + 1)).error).toBeDefined();
    expect(milestonesFromChapters(chapters(MAX_MILESTONES)).milestones).toHaveLength(MAX_MILESTONES);
  });

  it('groups chapters into contiguous ranges covering all chapters', () => {
    const ms = milestonesFromChapterRanges(chapters(10), 3);
    expect(ms.map((m) => m.label)).toEqual(['Ch 1–3', 'Ch 4–6', 'Ch 7–10']);
    expect(ms.map((m) => m.position)).toEqual([0, 1, 2]);
  });

  it('labels single-chapter ranges with the chapter title', () => {
    const ms = milestonesFromChapterRanges(chapters(2), 2);
    expect(ms[0].label).toBe('Ch 1: Chapter 1');
  });

  it('clamps range count to the chapter count', () => {
    expect(milestonesFromChapterRanges(chapters(3), 12)).toHaveLength(3);
  });

  it('builds milestones from detected parts', () => {
    const ms = milestonesFromParts([
      { label: 'Part One', start_index: 0, end_index: 4 },
      { label: 'Part Two', start_index: 5, end_index: 9 },
    ]);
    expect(ms.map((m) => m.label)).toEqual(['Part One', 'Part Two']);
  });

  it('whole book is a single milestone', () => {
    expect(wholeBookMilestones()).toEqual([{ id: 'm0', label: 'Whole book', position: 0 }]);
  });
});

describe('isMilestoneLocked (spoiler shield)', () => {
  it('general is never locked', () => {
    expect(isMilestoneLocked(-1, -1, GENERAL_MILESTONE)).toBe(false);
  });

  it('locks milestones beyond my progress', () => {
    expect(isMilestoneLocked(0, -1, 'm0')).toBe(true);   // not started: all locked
    expect(isMilestoneLocked(0, 0, 'm0')).toBe(false);   // finished part 1: open
    expect(isMilestoneLocked(1, 0, 'm1')).toBe(true);    // part 2 still locked
    expect(isMilestoneLocked(1, 3, 'm1')).toBe(false);   // ahead: open
  });

  it('treats missing progress as not started', () => {
    expect(isMilestoneLocked(0, undefined, 'm0')).toBe(true);
  });
});

describe('parseCsv', () => {
  it('handles quoted fields with commas, newlines, and escaped quotes', () => {
    const rows = parseCsv('title,author\n"Hello, World","Jane ""JD"" Doe"\nPlain,Bob\n"Multi\nline",X\n');
    expect(rows).toHaveLength(3);
    expect(rows[0].title).toBe('Hello, World');
    expect(rows[0].author).toBe('Jane "JD" Doe');
    expect(rows[1].title).toBe('Plain');
    expect(rows[2].title).toBe('Multi\nline');
  });

  it('returns empty for empty input', () => {
    expect(parseCsv('')).toEqual([]);
  });
});

describe('startRead', () => {
  it('creates an active read in slot 1', async () => {
    const r = await startRead(fakeDb, CLUB, bookInput(), jane);
    expect(r.success).toBe(true);
    const read = await getRead(fakeDb, CLUB, r.readId);
    expect(read.status).toBe('active');
    expect(read.slot).toBe(1);
    expect(read.milestones).toHaveLength(4);
    expect(read.commentCount).toBe(0);
    expect(mockStore[CLUB_PATH].activeSlots).toEqual([1]);
  });

  it('assigns slot 2 to a second book and blocks a third', async () => {
    await startRead(fakeDb, CLUB, bookInput(), jane);
    const second = await startRead(fakeDb, CLUB, bookInput({ bookTitle: 'Side Book' }), jane);
    expect(second.success).toBe(true);
    expect((await getRead(fakeDb, CLUB, second.readId)).slot).toBe(2);

    const third = await startRead(fakeDb, CLUB, bookInput({ bookTitle: 'One Too Many' }), jane);
    expect(third.success).toBe(false);
    expect(third.error).toMatch(/2 active books/);
  });

  it('validates session, book, and milestones', async () => {
    expect((await startRead(fakeDb, CLUB, bookInput(), null)).success).toBe(false);
    expect((await startRead(fakeDb, CLUB, bookInput({ bookTitle: ' ' }), jane)).success).toBe(false);
    expect((await startRead(fakeDb, CLUB, bookInput({ milestones: [] }), jane)).success).toBe(false);
  });
});

describe('comments', () => {
  let readId;
  beforeEach(async () => {
    readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
  });

  it('adds comments and keeps commentCount in sync', async () => {
    const r = await addComment(fakeDb, CLUB, readId, { milestoneId: 'm0', text: 'Loved this part!' }, jane);
    expect(r.success).toBe(true);
    await addComment(fakeDb, CLUB, readId, { milestoneId: GENERAL_MILESTONE, text: 'Great pick.' }, bob);
    expect((await getRead(fakeDb, CLUB, readId)).commentCount).toBe(2);
    const comments = await getComments(fakeDb, CLUB, readId);
    expect(comments).toHaveLength(2);
    expect(comments.find((c) => c.slug === 'jane doe').milestoneId).toBe('m0');
  });

  it('supports one level of replies via parentId', async () => {
    const top = await addComment(fakeDb, CLUB, readId, { milestoneId: 'm0', text: 'Thoughts?' }, jane);
    const reply = await addComment(fakeDb, CLUB, readId,
      { milestoneId: 'm0', parentId: top.commentId, text: 'Agreed!' }, bob);
    expect(reply.success).toBe(true);
    const comments = await getComments(fakeDb, CLUB, readId);
    expect(comments.find((c) => c.id === reply.commentId).parentId).toBe(top.commentId);
  });

  it('rejects empty and over-long comments', async () => {
    expect((await addComment(fakeDb, CLUB, readId, { milestoneId: 'm0', text: '  ' }, jane)).success).toBe(false);
    expect((await addComment(fakeDb, CLUB, readId, { milestoneId: 'm0', text: 'x'.repeat(2001) }, jane)).success).toBe(false);
    expect((await getRead(fakeDb, CLUB, readId)).commentCount).toBe(0);
  });

  it('deleting a comment decrements the count', async () => {
    const r = await addComment(fakeDb, CLUB, readId, { milestoneId: 'm0', text: 'Oops.' }, jane);
    await deleteComment(fakeDb, CLUB, readId, r.commentId);
    expect((await getRead(fakeDb, CLUB, readId)).commentCount).toBe(0);
    expect(await getComments(fakeDb, CLUB, readId)).toHaveLength(0);
  });
});

describe('progress', () => {
  let readId;
  beforeEach(async () => {
    readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
  });

  it('records and lists member progress', async () => {
    await setProgress(fakeDb, CLUB, readId, 2, jane);
    await setProgress(fakeDb, CLUB, readId, -1, bob);
    const all = await getProgressAll(fakeDb, CLUB, readId);
    expect(all).toHaveLength(2);
    expect(all.find((p) => p.slug === 'jane doe').milestonePosition).toBe(2);
    expect(all.find((p) => p.slug === 'bob brown').milestonePosition).toBe(-1);
  });

  it('moving progress backward is allowed (re-locks sections)', async () => {
    await setProgress(fakeDb, CLUB, readId, 3, jane);
    await setProgress(fakeDb, CLUB, readId, 1, jane);
    const all = await getProgressAll(fakeDb, CLUB, readId);
    expect(all.find((p) => p.slug === 'jane doe').milestonePosition).toBe(1);
    expect(isMilestoneLocked(2, 1, 'm2')).toBe(true);
  });

  it('requires a session', async () => {
    expect((await setProgress(fakeDb, CLUB, readId, 1, null)).success).toBe(false);
  });
});
