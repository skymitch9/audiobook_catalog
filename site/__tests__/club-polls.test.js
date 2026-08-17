// @vitest-environment jsdom
// Feature: book-clubs backlog #3 — club polls (free-form, chapter-taggable).
// Two creation surfaces (club.html = untagged, club-read.html = tagged to a
// read's section) share this one data layer; see the "Club polls" section
// of club-reads.js for the full data-shape rationale.
import { describe, it, expect, beforeEach, vi } from 'vitest';

// The Phase 1 shadow reporter (gate-shadow.js) fires fire-and-forget from
// the gated write paths under test; mock it so no test ever touches the
// network. Its own contract is pinned in gate-shadow.test.js.
vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

// --- In-memory Firestore mock (same shape as club-schedule.test.js) ---
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
      else next[k] = v;
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
      const docs = Object.entries(mockStore)
        .filter(([p]) => p.startsWith(prefix) && !p.slice(prefix.length).includes('/'))
        .map(([p, data]) => ({ id: p.split('/').pop(), data: () => data, exists: () => true }));
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
  MIN_POLL_OPTIONS, MAX_POLL_OPTIONS, MAX_POLL_QUESTION_LENGTH, MAX_POLL_OPTION_LENGTH,
  MAX_POLL_BOOK_TITLE_LENGTH, MAX_POLL_BOOK_AUTHOR_LENGTH,
  POLL_TYPE_FREEFORM, POLL_TYPE_NEXT_BOOK,
  validatePollQuestion, validatePollOptions, validateNextBookOptions, tallyPollVotes, myPollVote,
  isPollLocked, pollResultsVisible, isNextBookPoll, pollOptionContentHtml, pollWinnerIndex,
  createPoll, getPolls, getPoll, setPollStatus, deletePoll, castVote, getPollVotes,
} = await import('../club-reads.js');

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
const bob = { displayName: 'Bob' };
// col() resolves to *_dev under jsdom (localhost = dev lane)
const CLUB_PATH = 'clubs_dev/club1';

beforeEach(() => {
  mockStore = { [CLUB_PATH]: { name: 'Test Club', activeSlots: [] } };
});

// ==================== Pure validation ====================

describe('validatePollQuestion', () => {
  it('rejects blank', () => {
    expect(validatePollQuestion('').valid).toBe(false);
    expect(validatePollQuestion('   ').valid).toBe(false);
  });

  it('accepts a normal question', () => {
    expect(validatePollQuestion('Favorite POV character?').valid).toBe(true);
  });

  it('rejects over the length cap', () => {
    const r = validatePollQuestion('x'.repeat(MAX_POLL_QUESTION_LENGTH + 1));
    expect(r.valid).toBe(false);
    expect(r.error).toMatch(String(MAX_POLL_QUESTION_LENGTH));
  });

  it('accepts exactly at the cap', () => {
    expect(validatePollQuestion('x'.repeat(MAX_POLL_QUESTION_LENGTH)).valid).toBe(true);
  });
});

describe('validatePollOptions', () => {
  it('drops blank rows before counting', () => {
    const r = validatePollOptions(['Kaladin', '', '  ', 'Shallan']);
    expect(r.valid).toBe(true);
    expect(r.options).toEqual(['Kaladin', 'Shallan']);
  });

  it(`rejects fewer than ${MIN_POLL_OPTIONS} options`, () => {
    expect(validatePollOptions(['Only one']).valid).toBe(false);
    expect(validatePollOptions([]).valid).toBe(false);
  });

  it(`rejects more than ${MAX_POLL_OPTIONS} options`, () => {
    const opts = Array.from({ length: MAX_POLL_OPTIONS + 1 }, (_, i) => `Opt ${i}`);
    expect(validatePollOptions(opts).valid).toBe(false);
  });

  it(`accepts exactly ${MAX_POLL_OPTIONS} options`, () => {
    const opts = Array.from({ length: MAX_POLL_OPTIONS }, (_, i) => `Opt ${i}`);
    expect(validatePollOptions(opts).valid).toBe(true);
  });

  it('rejects an option over the per-option length cap', () => {
    const r = validatePollOptions(['ok', 'x'.repeat(MAX_POLL_OPTION_LENGTH + 1)]);
    expect(r.valid).toBe(false);
  });
});

// ==================== Next-book poll options (backlog #3b) ====================

describe('validateNextBookOptions', () => {
  const book = (title, author = 'An Author', coverHref = 'https://covers.example/x.jpg') =>
    ({ title, author, coverHref });

  it('accepts 2-10 book refs and cleans (trims) each field', () => {
    const r = validateNextBookOptions([
      { title: '  The Way of Kings  ', author: ' Brandon Sanderson ', coverHref: ' https://c/1.jpg ' },
      book('Words of Radiance'),
    ]);
    expect(r.valid).toBe(true);
    expect(r.options[0]).toEqual({
      title: 'The Way of Kings', author: 'Brandon Sanderson', coverHref: 'https://c/1.jpg',
    });
  });

  it('drops entries with a blank title before counting (mirrors free-form blank-row rule)', () => {
    const r = validateNextBookOptions([book('A'), { title: '  ', author: 'Nobody' }, book('B')]);
    expect(r.valid).toBe(true);
    expect(r.options.map(o => o.title)).toEqual(['A', 'B']);
  });

  it(`rejects fewer than ${MIN_POLL_OPTIONS} books`, () => {
    expect(validateNextBookOptions([book('Only one')]).valid).toBe(false);
    expect(validateNextBookOptions([]).valid).toBe(false);
    expect(validateNextBookOptions(undefined).valid).toBe(false);
  });

  it(`rejects more than ${MAX_POLL_OPTIONS} books`, () => {
    const opts = Array.from({ length: MAX_POLL_OPTIONS + 1 }, (_, i) => book(`Book ${i}`));
    expect(validateNextBookOptions(opts).valid).toBe(false);
  });

  it(`accepts exactly ${MAX_POLL_OPTIONS} books`, () => {
    const opts = Array.from({ length: MAX_POLL_OPTIONS }, (_, i) => book(`Book ${i}`));
    expect(validateNextBookOptions(opts).valid).toBe(true);
  });

  it('rejects a title or author over their length caps', () => {
    expect(validateNextBookOptions([book('x'.repeat(MAX_POLL_BOOK_TITLE_LENGTH + 1)), book('ok')]).valid).toBe(false);
    expect(validateNextBookOptions([book('ok', 'x'.repeat(MAX_POLL_BOOK_AUTHOR_LENGTH + 1)), book('ok2')]).valid).toBe(false);
  });

  it('tolerates a missing author/coverHref (catalog entries can lack either)', () => {
    const r = validateNextBookOptions([{ title: 'Solo' }, { title: 'Duo', author: 'Someone' }]);
    expect(r.valid).toBe(true);
    expect(r.options[0]).toEqual({ title: 'Solo', author: '', coverHref: '' });
  });
});

describe('isNextBookPoll', () => {
  it('is true only for type nextBook', () => {
    expect(isNextBookPoll({ type: POLL_TYPE_NEXT_BOOK })).toBe(true);
    expect(isNextBookPoll({ type: POLL_TYPE_FREEFORM })).toBe(false);
  });

  it('treats a missing type as free-form (legacy polls predate the field)', () => {
    expect(isNextBookPoll({})).toBe(false);
    expect(isNextBookPoll(null)).toBe(false);
    expect(isNextBookPoll(undefined)).toBe(false);
  });
});

describe('pollOptionContentHtml', () => {
  it('escapes a free-form (string) option as-is', () => {
    const html = pollOptionContentHtml({ type: POLL_TYPE_FREEFORM }, '<script>Kaladin</script>');
    expect(html).toBe('&lt;script&gt;Kaladin&lt;/script&gt;');
  });

  it('renders a next-book option as a cover + title + author, HTML-escaped', () => {
    const html = pollOptionContentHtml(
      { type: POLL_TYPE_NEXT_BOOK },
      { title: 'A "Great" Book', author: 'Some & Author', coverHref: 'https://c/1.jpg' },
    );
    expect(html).toContain('<img class="poll-opt-cover" src="https://c/1.jpg"');
    expect(html).toContain('A &quot;Great&quot; Book');
    expect(html).toContain('Some &amp; Author');
  });

  it('omits the cover image entirely when coverHref is blank', () => {
    const html = pollOptionContentHtml({ type: POLL_TYPE_NEXT_BOOK }, { title: 'No Cover', author: '' });
    expect(html).not.toContain('<img');
    expect(html).toContain('No Cover');
  });
});

describe('pollWinnerIndex', () => {
  const options = [{ title: 'A' }, { title: 'B' }, { title: 'C' }];

  it('returns null with no votes', () => {
    expect(pollWinnerIndex(options, [])).toBeNull();
  });

  it('returns the single option with the most votes', () => {
    const votes = [{ optionIndex: 1 }, { optionIndex: 1 }, { optionIndex: 0 }];
    expect(pollWinnerIndex(options, votes)).toBe(1);
  });

  it('returns null on a tie rather than an arbitrary pick', () => {
    const votes = [{ optionIndex: 0 }, { optionIndex: 1 }];
    expect(pollWinnerIndex(options, votes)).toBeNull();
  });
});

// ==================== Vote tallying ====================

describe('tallyPollVotes', () => {
  const options = ['A', 'B', 'C'];

  it('counts votes per option and totals them', () => {
    const votes = [{ optionIndex: 0 }, { optionIndex: 0 }, { optionIndex: 2 }];
    expect(tallyPollVotes(options, votes)).toEqual({ counts: [2, 0, 1], total: 3 });
  });

  it('returns all-zero counts with no votes', () => {
    expect(tallyPollVotes(options, [])).toEqual({ counts: [0, 0, 0], total: 0 });
    expect(tallyPollVotes(options, undefined)).toEqual({ counts: [0, 0, 0], total: 0 });
  });

  it('ignores out-of-range or malformed vote docs (defends against stale options edits)', () => {
    const votes = [{ optionIndex: 0 }, { optionIndex: 99 }, { optionIndex: -1 }, {}];
    expect(tallyPollVotes(options, votes)).toEqual({ counts: [1, 0, 0], total: 1 });
  });
});

describe('myPollVote', () => {
  const votes = [{ slug: 'jane-doe', optionIndex: 1 }, { slug: 'bob', optionIndex: 0 }];

  it("finds the caller's own vote by slug", () => {
    expect(myPollVote(votes, 'jane-doe')).toBe(1);
    expect(myPollVote(votes, 'bob')).toBe(0);
  });

  it('returns null when the caller has not voted', () => {
    expect(myPollVote(votes, 'nobody')).toBeNull();
    expect(myPollVote([], 'jane-doe')).toBeNull();
    expect(myPollVote(undefined, 'jane-doe')).toBeNull();
  });
});

// ==================== Spoiler gate ====================

describe('isPollLocked', () => {
  it('never locks an untagged poll', () => {
    expect(isPollLocked({ milestonePosition: null }, -1)).toBe(false);
    expect(isPollLocked({ milestonePosition: undefined }, 5)).toBe(false);
    expect(isPollLocked({}, -1)).toBe(false);
  });

  it('locks a tagged poll while the viewer is behind its section', () => {
    expect(isPollLocked({ milestonePosition: 2 }, 1)).toBe(true);
    expect(isPollLocked({ milestonePosition: 2 }, -1)).toBe(true);
  });

  it('unlocks once the viewer has reached or passed the section', () => {
    expect(isPollLocked({ milestonePosition: 2 }, 2)).toBe(false);
    expect(isPollLocked({ milestonePosition: 2 }, 3)).toBe(false);
  });

  it('treats a non-numeric position as not-started (-1), same as isMilestoneLocked', () => {
    expect(isPollLocked({ milestonePosition: 0 }, undefined)).toBe(true);
    expect(isPollLocked({ milestonePosition: -1 }, undefined)).toBe(false);
  });
});

// ==================== Results visibility ====================

describe('pollResultsVisible', () => {
  it('shows results to everyone once the poll is closed', () => {
    expect(pollResultsVisible({ status: 'closed' }, false, false)).toBe(true);
  });

  it('hides live results from a non-voting, non-manager member on an open poll', () => {
    expect(pollResultsVisible({ status: 'open' }, false, false)).toBe(false);
  });

  it('shows live results after the viewer votes', () => {
    expect(pollResultsVisible({ status: 'open' }, true, false)).toBe(true);
  });

  it('always shows live results to a manager, even without voting', () => {
    expect(pollResultsVisible({ status: 'open' }, false, true)).toBe(true);
  });
});

// ==================== Firestore-backed CRUD ====================

describe('createPoll', () => {
  it('requires sign-in', async () => {
    const r = await createPoll(fakeDb, 'club1', { question: 'Q?', options: ['A', 'B'] }, null);
    expect(r.success).toBe(false);
  });

  it('rejects an invalid question or option count without writing anything', async () => {
    const bad1 = await createPoll(fakeDb, 'club1', { question: '', options: ['A', 'B'] }, jane);
    expect(bad1.success).toBe(false);
    const bad2 = await createPoll(fakeDb, 'club1', { question: 'Q?', options: ['A'] }, jane);
    expect(bad2.success).toBe(false);
    expect(await getPolls(fakeDb, 'club1')).toEqual([]);
  });

  it('creates an untagged (club-wide) poll open by default', async () => {
    const r = await createPoll(fakeDb, 'club1', { question: 'Best arc?', options: ['A', 'B', 'C'] }, jane);
    expect(r.success).toBe(true);
    const poll = await getPoll(fakeDb, 'club1', r.pollId);
    expect(poll).toMatchObject({
      question: 'Best arc?', options: ['A', 'B', 'C'],
      readId: null, milestoneId: null, milestonePosition: null,
      status: 'open', createdBy: 'Jane Doe', createdBySlug: 'jane doe',
    });
  });

  it('creates a section-tagged poll carrying readId/milestoneId/milestonePosition', async () => {
    const r = await createPoll(fakeDb, 'club1', {
      question: 'Who dies first?', options: ['X', 'Y'],
      readId: 'read1', milestoneId: 'm2', milestonePosition: 2,
    }, jane);
    const poll = await getPoll(fakeDb, 'club1', r.pollId);
    expect(poll).toMatchObject({ readId: 'read1', milestoneId: 'm2', milestonePosition: 2 });
  });

  it('trims the question and drops blank option rows', async () => {
    const r = await createPoll(fakeDb, 'club1', {
      question: '  Trimmed?  ', options: ['A', '', 'B', '  '],
    }, jane);
    const poll = await getPoll(fakeDb, 'club1', r.pollId);
    expect(poll.question).toBe('Trimmed?');
    expect(poll.options).toEqual(['A', 'B']);
  });

  it('defaults to type freeform when omitted', async () => {
    const r = await createPoll(fakeDb, 'club1', { question: 'Q?', options: ['A', 'B'] }, jane);
    const poll = await getPoll(fakeDb, 'club1', r.pollId);
    expect(poll.type).toBe('freeform');
  });

  it('creates a nextBook poll storing book-ref options, not strings', async () => {
    const r = await createPoll(fakeDb, 'club1', {
      question: 'What should we read next?',
      type: 'nextBook',
      options: [
        { title: 'The Way of Kings', author: 'Brandon Sanderson', coverHref: 'https://c/1.jpg' },
        { title: 'Mistborn', author: 'Brandon Sanderson', coverHref: 'https://c/2.jpg' },
      ],
    }, jane);
    expect(r.success).toBe(true);
    const poll = await getPoll(fakeDb, 'club1', r.pollId);
    expect(poll.type).toBe('nextBook');
    expect(poll.options).toEqual([
      { title: 'The Way of Kings', author: 'Brandon Sanderson', coverHref: 'https://c/1.jpg' },
      { title: 'Mistborn', author: 'Brandon Sanderson', coverHref: 'https://c/2.jpg' },
    ]);
  });

  it('rejects a nextBook poll with fewer than 2 book refs, or a malformed one (blank title)', async () => {
    const tooFew = await createPoll(fakeDb, 'club1', {
      question: 'Q?', type: 'nextBook', options: [{ title: 'Solo' }],
    }, jane);
    expect(tooFew.success).toBe(false);

    const malformed = await createPoll(fakeDb, 'club1', {
      question: 'Q?', type: 'nextBook', options: [{ title: '' }, { title: 'Fine' }],
    }, jane);
    expect(malformed.success).toBe(false);
  });

  it('an unrecognized type falls back to freeform validation/storage', async () => {
    const r = await createPoll(fakeDb, 'club1', { question: 'Q?', type: 'bogus', options: ['A', 'B'] }, jane);
    expect(r.success).toBe(true);
    const poll = await getPoll(fakeDb, 'club1', r.pollId);
    expect(poll.type).toBe('freeform');
  });
});

describe('setPollStatus / castVote / getPollVotes', () => {
  it('rejects an invalid status', async () => {
    const r = await setPollStatus(fakeDb, 'club1', 'poll1', 'paused');
    expect(r.success).toBe(false);
  });

  it('closing stamps closedAt; reopening clears it', async () => {
    const { pollId } = await createPoll(fakeDb, 'club1', { question: 'Q?', options: ['A', 'B'] }, jane);
    await setPollStatus(fakeDb, 'club1', pollId, 'closed');
    let poll = await getPoll(fakeDb, 'club1', pollId);
    expect(poll.status).toBe('closed');
    expect(poll.closedAt).toBe('server-ts');
    await setPollStatus(fakeDb, 'club1', pollId, 'open');
    poll = await getPoll(fakeDb, 'club1', pollId);
    expect(poll.status).toBe('open');
    expect(poll.closedAt).toBeNull();
  });

  it('records a vote and lets the same member change it (one doc per member)', async () => {
    const { pollId } = await createPoll(fakeDb, 'club1', { question: 'Q?', options: ['A', 'B'] }, jane);
    await castVote(fakeDb, 'club1', pollId, 0, jane);
    let votes = await getPollVotes(fakeDb, 'club1', pollId);
    expect(votes).toHaveLength(1);
    expect(votes[0]).toMatchObject({ slug: 'jane doe', optionIndex: 0 });

    await castVote(fakeDb, 'club1', pollId, 1, jane); // changes her mind
    votes = await getPollVotes(fakeDb, 'club1', pollId);
    expect(votes).toHaveLength(1); // still one doc, updated
    expect(votes[0].optionIndex).toBe(1);

    await castVote(fakeDb, 'club1', pollId, 0, bob);
    votes = await getPollVotes(fakeDb, 'club1', pollId);
    expect(votes).toHaveLength(2);
  });

  it('requires sign-in and a valid option index', async () => {
    const { pollId } = await createPoll(fakeDb, 'club1', { question: 'Q?', options: ['A', 'B'] }, jane);
    expect((await castVote(fakeDb, 'club1', pollId, 0, null)).success).toBe(false);
    expect((await castVote(fakeDb, 'club1', pollId, -1, jane)).success).toBe(false);
    expect((await castVote(fakeDb, 'club1', pollId, 'nope', jane)).success).toBe(false);
  });
});

describe('deletePoll', () => {
  it('removes the poll and every vote doc under it', async () => {
    const { pollId } = await createPoll(fakeDb, 'club1', { question: 'Q?', options: ['A', 'B'] }, jane);
    await castVote(fakeDb, 'club1', pollId, 0, jane);
    await castVote(fakeDb, 'club1', pollId, 1, bob);
    expect(await getPollVotes(fakeDb, 'club1', pollId)).toHaveLength(2);

    const r = await deletePoll(fakeDb, 'club1', pollId);
    expect(r.success).toBe(true);
    expect(await getPoll(fakeDb, 'club1', pollId)).toBeNull();
    expect(await getPollVotes(fakeDb, 'club1', pollId)).toEqual([]);
  });
});

describe('getPolls', () => {
  it('returns every poll for the club, tagged and untagged alike', async () => {
    await createPoll(fakeDb, 'club1', { question: 'Untagged?', options: ['A', 'B'] }, jane);
    await createPoll(fakeDb, 'club1', {
      question: 'Tagged?', options: ['A', 'B'], readId: 'read1', milestoneId: 'm0', milestonePosition: 0,
    }, jane);
    const polls = await getPolls(fakeDb, 'club1');
    expect(polls).toHaveLength(2);
    expect(polls.map(p => p.question).sort()).toEqual(['Tagged?', 'Untagged?']);
  });
});
