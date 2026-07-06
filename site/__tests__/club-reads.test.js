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
  startRead, getReads, getRead, finishRead, refreshClubAvatar, groupChapters, updateReadLabel,
  addComment, deleteComment, getComments,
  setProgress, setChapterProgress, getProgressAll, isCommentSpoiler,
  getTbr, addTbrItem, removeTbrItem, toggleTbrVote,
  toggleReaction, togglePin, REACTION_EMOJI,
  addQuote, getQuotes, deleteQuote, MAX_QUOTE_LENGTH,
  highlightMentions, mentionsUser,
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
    expect(splitByDuration(600, MAX_MILESTONES + 1)).toHaveLength(MAX_MILESTONES);
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
    expect(milestones[2]).toEqual({ id: 'm2', label: 'Chapter 3', position: 2, chStart: 2, chEnd: 2 });
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

describe('club avatar (cover of the current book)', () => {
  it('starting a read sets the club avatar to its cover', async () => {
    await startRead(fakeDb, CLUB, bookInput(), jane);
    expect(mockStore[CLUB_PATH].avatarCoverHref).toBe('covers/Matt Dinniman/Dungeon Crawler Carl.jpg');
  });

  it('first book (slot 1) stays the default avatar when a second starts', async () => {
    await startRead(fakeDb, CLUB, bookInput(), jane);
    await startRead(fakeDb, CLUB, bookInput({ bookTitle: 'Side Book', coverHref: 'covers/side.jpg' }), jane);
    expect(mockStore[CLUB_PATH].avatarCoverHref).toBe('covers/Matt Dinniman/Dungeon Crawler Carl.jpg');
  });

  it('an explicit avatarReadId choice is honored', async () => {
    await startRead(fakeDb, CLUB, bookInput(), jane);
    const second = await startRead(fakeDb, CLUB, bookInput({ bookTitle: 'Side Book', coverHref: 'covers/side.jpg' }), jane);
    mockStore[CLUB_PATH].avatarReadId = second.readId;
    await refreshClubAvatar(fakeDb, CLUB);
    expect(mockStore[CLUB_PATH].avatarCoverHref).toBe('covers/side.jpg');
  });

  it('finishing the avatar book falls back to the next active one, then clears', async () => {
    const first = await startRead(fakeDb, CLUB, bookInput(), jane);
    const second = await startRead(fakeDb, CLUB, bookInput({ bookTitle: 'Side Book', coverHref: 'covers/side.jpg' }), jane);
    await finishRead(fakeDb, CLUB, first.readId, 'finished');
    expect(mockStore[CLUB_PATH].avatarCoverHref).toBe('covers/side.jpg');
    await finishRead(fakeDb, CLUB, second.readId, 'finished');
    expect(mockStore[CLUB_PATH].avatarCoverHref).toBe('');
    expect(mockStore[CLUB_PATH].avatarReadId).toBeNull();
  });
});

describe('updateReadLabel', () => {
  it('sets a free-form label on a read', async () => {
    const readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
    expect((await updateReadLabel(fakeDb, CLUB, readId, '  Spicy pick  ')).success).toBe(true);
    expect((await getRead(fakeDb, CLUB, readId)).slotLabel).toBe('Spicy pick');
    expect((await updateReadLabel(fakeDb, CLUB, readId, 'x'.repeat(41))).success).toBe(false);
  });
});

describe('finishRead', () => {
  it('archives the read and frees its slot', async () => {
    const readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
    const r = await finishRead(fakeDb, CLUB, readId, 'finished');
    expect(r.success).toBe(true);
    const read = await getRead(fakeDb, CLUB, readId);
    expect(read.status).toBe('finished');
    expect(read.finishedAt).toBeTruthy();
    expect(mockStore[CLUB_PATH].activeSlots).toEqual([]);
    // the freed slot is reusable
    const again = await startRead(fakeDb, CLUB, bookInput({ bookTitle: 'Next Book' }), jane);
    expect(again.success).toBe(true);
    expect((await getRead(fakeDb, CLUB, again.readId)).slot).toBe(1);
  });

  it('abandon works and archived reads cannot be re-finished', async () => {
    const readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
    expect((await finishRead(fakeDb, CLUB, readId, 'abandoned')).success).toBe(true);
    expect((await finishRead(fakeDb, CLUB, readId, 'finished')).success).toBe(false);
  });

  it('rejects invalid statuses', async () => {
    const readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
    expect((await finishRead(fakeDb, CLUB, readId, 'paused')).success).toBe(false);
  });
});

describe('club TBR', () => {
  const book = { title: 'Dungeon Crawler Carl', author: 'Matt Dinniman', coverHref: 'covers/x.jpg', durationMinutes: 800, durationHhmm: '13:20' };

  it('suggesting adds an item with the suggester as first voter', async () => {
    const r = await addTbrItem(fakeDb, CLUB, book, jane);
    expect(r.success).toBe(true);
    const items = await getTbr(fakeDb, CLUB);
    expect(items).toHaveLength(1);
    expect(items[0].bookTitle).toBe('Dungeon Crawler Carl');
    expect(items[0].suggestedBy).toBe('Jane Doe');
    expect(items[0].voterSlugs).toEqual(['jane doe']);
  });

  it('rejects duplicate titles and missing session', async () => {
    await addTbrItem(fakeDb, CLUB, book, jane);
    expect((await addTbrItem(fakeDb, CLUB, book, bob)).success).toBe(false);
    expect((await addTbrItem(fakeDb, CLUB, book, null)).success).toBe(false);
    expect(await getTbr(fakeDb, CLUB)).toHaveLength(1);
  });

  it('vote toggles on and off, and the list sorts by votes', async () => {
    const a = await addTbrItem(fakeDb, CLUB, book, jane);
    const b = await addTbrItem(fakeDb, CLUB, { ...book, title: 'Other Book' }, jane);
    await toggleTbrVote(fakeDb, CLUB, b.itemId, bob); // Other Book: 2 votes
    let items = await getTbr(fakeDb, CLUB);
    expect(items[0].bookTitle).toBe('Other Book');
    expect(items[0].voterSlugs).toContain('bob brown');

    await toggleTbrVote(fakeDb, CLUB, b.itemId, bob); // un-vote
    items = await getTbr(fakeDb, CLUB);
    expect(items.find(i => i.id === b.itemId).voterSlugs).toEqual(['jane doe']);
    expect((await toggleTbrVote(fakeDb, CLUB, 'nope', bob)).success).toBe(false);
  });

  it('remove deletes the suggestion', async () => {
    const r = await addTbrItem(fakeDb, CLUB, book, jane);
    await removeTbrItem(fakeDb, CLUB, r.itemId);
    expect(await getTbr(fakeDb, CLUB)).toHaveLength(0);
  });
});

describe('groupChapters (dropdown grouping)', () => {
  it('groups by real Part headings, with a Beginning group for leading chapters', () => {
    const titles = ['Prologue', 'Part One', 'Ch 1', 'Ch 2', 'Part Two', 'Ch 3'];
    expect(groupChapters(titles)).toEqual([
      { label: 'Beginning', start: 0, end: 0 },
      { label: 'Part One', start: 1, end: 3 },
      { label: 'Part Two', start: 4, end: 5 },
    ]);
  });

  it('recognizes Book N headings too', () => {
    const groups = groupChapters(['Book 1', 'a', 'Book 2', 'b']);
    expect(groups.map(g => g.label)).toEqual(['Book 1', 'Book 2']);
  });

  it('synthesizes chunks of 25 when no headings exist', () => {
    const titles = Array.from({ length: 60 }, (_, i) => `Chapter ${i + 1}`);
    const groups = groupChapters(titles);
    expect(groups).toEqual([
      { label: 'Ch 1–25', start: 0, end: 24 },
      { label: 'Ch 26–50', start: 25, end: 49 },
      { label: 'Ch 51–60', start: 50, end: 59 },
    ]);
  });

  it('short books get a single group; a lone heading is not a split', () => {
    expect(groupChapters(['a', 'b', 'c'])).toEqual([{ label: 'Chapters', start: 0, end: 2 }]);
    expect(groupChapters(['Part One', 'a', 'b']).length).toBe(1);
    expect(groupChapters([])).toEqual([]);
  });

  it('does not treat mid-word matches as headings', () => {
    const titles = Array.from({ length: 30 }, () => 'The Party Continues');
    expect(groupChapters(titles)[0].label).toBe('Ch 1–25');
  });
});

describe('chapter tags and comment spoilers', () => {
  it('isCommentSpoiler: untagged comments are never spoilers', () => {
    expect(isCommentSpoiler(null, -1)).toBe(false);
    expect(isCommentSpoiler(undefined, 5)).toBe(false);
  });

  it('isCommentSpoiler: tagged comments hide from readers who are behind', () => {
    expect(isCommentSpoiler(52, -1)).toBe(true);       // not started
    expect(isCommentSpoiler(52, 30)).toBe(true);       // behind
    expect(isCommentSpoiler(52, 52)).toBe(false);      // at that chapter
    expect(isCommentSpoiler(52, 100)).toBe(false);     // past it
    expect(isCommentSpoiler(0, undefined)).toBe(true); // no progress doc
  });

  it('startRead snapshots chapter titles onto the read', async () => {
    const chapters = [{ title: 'Prologue' }, { title: 'Chapter 1' }, { title: 'Chapter 2' }];
    const r = await startRead(fakeDb, CLUB, bookInput({ chapters }), jane);
    const read = await getRead(fakeDb, CLUB, r.readId);
    expect(read.chapterTitles).toEqual(['Prologue', 'Chapter 1', 'Chapter 2']);
  });

  it('chapter-derived milestones carry chStart/chEnd for section locking', () => {
    const chapters = Array.from({ length: 10 }, (_, i) => ({ title: `Ch ${i + 1}` }));
    expect(milestonesFromChapters(chapters).milestones[3]).toMatchObject({ chStart: 3, chEnd: 3 });
    expect(milestonesFromChapterRanges(chapters, 2)[1]).toMatchObject({ chStart: 5, chEnd: 9 });
    expect(milestonesFromParts([{ label: 'Part One', start_index: 0, end_index: 4 }])[0])
      .toMatchObject({ chStart: 0, chEnd: 4 });
    expect(wholeBookMilestones()[0].chStart).toBeUndefined();
  });

  it('comments store an optional part tag (audiobook-style coarse tagging)', async () => {
    const readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
    const r = await addComment(fakeDb, CLUB, readId,
      { milestoneId: 'general', text: 'Somewhere in part two...', partIndex: 1 }, jane);
    expect(r.success).toBe(true);
    const c = (await getComments(fakeDb, CLUB, readId)).find(x => x.id === r.commentId);
    expect(c.partIndex).toBe(1);
    expect(c.chapterIndex).toBeNull();
  });

  it('comments store an optional chapter tag', async () => {
    const readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
    const tagged = await addComment(fakeDb, CLUB, readId,
      { milestoneId: 'm0', text: 'At the twist!', chapterIndex: 52 }, jane);
    const plain = await addComment(fakeDb, CLUB, readId,
      { milestoneId: 'm0', text: 'No spoilers here.' }, bob);
    const comments = await getComments(fakeDb, CLUB, readId);
    expect(comments.find(c => c.id === tagged.commentId).chapterIndex).toBe(52);
    expect(comments.find(c => c.id === plain.commentId).chapterIndex).toBeNull();
  });

  it('multiple comments per user in the same section are allowed', async () => {
    const readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
    await addComment(fakeDb, CLUB, readId, { milestoneId: 'm0', text: 'First!' }, jane);
    await addComment(fakeDb, CLUB, readId, { milestoneId: 'm0', text: 'Second thought.' }, jane);
    await addComment(fakeDb, CLUB, readId, { milestoneId: 'm0', text: 'Third.' }, jane);
    expect(await getComments(fakeDb, CLUB, readId)).toHaveLength(3);
  });

  it('setChapterProgress records chapter-level progress', async () => {
    const readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
    await setChapterProgress(fakeDb, CLUB, readId, 51, jane);
    const all = await getProgressAll(fakeDb, CLUB, readId);
    expect(all.find(p => p.slug === 'jane doe').chapterIndex).toBe(51);
    expect((await setChapterProgress(fakeDb, CLUB, readId, 1, null)).success).toBe(false);
  });
});

describe('quotes', () => {
  let readId;
  beforeEach(async () => {
    readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
  });

  it('saves, lists, and deletes quotes with optional chapter tags', async () => {
    const r = await addQuote(fakeDb, CLUB, readId, { text: 'A reader lives a thousand lives.', chapterIndex: 4 }, jane);
    expect(r.success).toBe(true);
    await addQuote(fakeDb, CLUB, readId, { text: 'Untagged wisdom.' }, bob);
    const quotes = await getQuotes(fakeDb, CLUB, readId);
    expect(quotes).toHaveLength(2);
    expect(quotes.find(q => q.slug === 'jane doe').chapterIndex).toBe(4);
    expect(quotes.find(q => q.slug === 'bob brown').chapterIndex).toBeNull();

    await deleteQuote(fakeDb, CLUB, readId, r.quoteId);
    expect(await getQuotes(fakeDb, CLUB, readId)).toHaveLength(1);
  });

  it('validates text and session', async () => {
    expect((await addQuote(fakeDb, CLUB, readId, { text: '  ' }, jane)).success).toBe(false);
    expect((await addQuote(fakeDb, CLUB, readId, { text: 'x'.repeat(MAX_QUOTE_LENGTH + 1) }, jane)).success).toBe(false);
    expect((await addQuote(fakeDb, CLUB, readId, { text: 'ok' }, null)).success).toBe(false);
  });
});

describe('mentions', () => {
  it('highlights member mentions, longest name wins, no nesting', () => {
    const html = highlightMentions('@Jane Doe and @Jane disagree', ['Jane Doe', 'Jane'], null);
    expect(html).toBe('<span class="mention">@Jane Doe</span> and <span class="mention">@Jane</span> disagree');
  });

  it('marks mentions of the viewer and matches case-insensitively', () => {
    const html = highlightMentions('ping @skylar', ['Skylar'], 'Skylar');
    expect(html).toContain('mention-me');
  });

  it('escapes HTML and skips partial-word matches', () => {
    expect(highlightMentions('<b>@Jane</b>', ['Jane'], null))
      .toBe('&lt;b&gt;<span class="mention">@Jane</span>&lt;/b&gt;');
    expect(highlightMentions('email @Janet', ['Jane'], null)).toBe('email @Janet');
  });

  it('mentionsUser detects mentions of a specific person', () => {
    expect(mentionsUser('hey @Skylar!', 'Skylar')).toBe(true);
    expect(mentionsUser('hey @Skylarlar', 'Skylar')).toBe(false);
    expect(mentionsUser('no one', 'Skylar')).toBe(false);
  });
});

describe('reactions and pinning', () => {
  let readId, commentId;
  beforeEach(async () => {
    readId = (await startRead(fakeDb, CLUB, bookInput(), jane)).readId;
    commentId = (await addComment(fakeDb, CLUB, readId, { milestoneId: 'm0', text: 'Hot take!' }, jane)).commentId;
  });

  const getComment = async () => (await getComments(fakeDb, CLUB, readId)).find(c => c.id === commentId);

  it('reactions toggle per user and disappear at zero', async () => {
    const emoji = REACTION_EMOJI[0];
    await toggleReaction(fakeDb, CLUB, readId, commentId, emoji, jane);
    await toggleReaction(fakeDb, CLUB, readId, commentId, emoji, bob);
    expect((await getComment()).reactions[emoji]).toEqual(['jane doe', 'bob brown']);

    await toggleReaction(fakeDb, CLUB, readId, commentId, emoji, jane);
    expect((await getComment()).reactions[emoji]).toEqual(['bob brown']);

    await toggleReaction(fakeDb, CLUB, readId, commentId, emoji, bob);
    expect((await getComment()).reactions[emoji]).toBeUndefined();
  });

  it('rejects unknown emoji and missing session', async () => {
    expect((await toggleReaction(fakeDb, CLUB, readId, commentId, '🦖', jane)).success).toBe(false);
    expect((await toggleReaction(fakeDb, CLUB, readId, commentId, REACTION_EMOJI[0], null)).success).toBe(false);
  });

  it('pin toggles on and off', async () => {
    expect((await getComment()).isPinned).toBe(false);
    await togglePin(fakeDb, CLUB, readId, commentId);
    expect((await getComment()).isPinned).toBe(true);
    await togglePin(fakeDb, CLUB, readId, commentId);
    expect((await getComment()).isPinned).toBe(false);
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
