// @vitest-environment jsdom
// @vitest-environment-options { "url": "https://audiobooks.heygabi.ai/" }
//
// Feature: auth-migration Phase 3a, surface 3 — site/club-reads.js on the
// Worker's read-lifecycle, schedule and poll routes.
//
// Seven functions move. Beyond the four properties surfaces 1 and 2 pin, this
// file carries the two that are specific to reads:
//
//   7. ⚠️ `updateReadLabel` DOES NOT MOVE. `slotLabel` is member-editable by
//      design, `read.setSlot` is `{kind:'signedIn'}`, and enforce-routes.ts
//      says outright there is deliberately no route to mirror it. A test
//      asserts the flag leaves it on Firestore — this is the one place where
//      a later session would "finish the job" and take the pencil away from
//      every member.
//   8. ⚠️ `refreshClubAvatar` still runs after a Worker finish/remove, and a
//      failure to repaint the club card must NOT report the action as
//      failed. It is a member-open presentation write on both paths.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

let flagOn = false;
vi.mock('../flags.js', () => ({
  flagEnabled: (name) => (name === 'AUTH_ROUTES_CLUB_READS' ? flagOn : false),
}));

const fsCalls = [];
let getDocImpl = async (ref) => ({ exists: () => true, data: () => ({ milestones: [] }) });
vi.mock('firebase/firestore', () => ({
  doc: (db, ...parts) => ({ _path: parts.join('/'), id: 'generated-id' }),
  collection: (db, ...parts) => ({ _path: parts.join('/') }),
  updateDoc: async (ref, data) => { fsCalls.push(['updateDoc', ref._path, data]); },
  setDoc: async (ref, data) => { fsCalls.push(['setDoc', ref._path, data]); },
  deleteDoc: async (ref) => { fsCalls.push(['deleteDoc', ref._path]); },
  getDoc: async (ref) => { fsCalls.push(['getDoc', ref._path]); return getDocImpl(ref); },
  getDocs: async (ref) => { fsCalls.push(['getDocs', ref._path]); return { docs: [] }; },
  runTransaction: async (db, fn) => {
    fsCalls.push(['runTransaction']);
    return fn({
      get: async () => ({ exists: () => true, data: () => ({ status: 'active', slot: 0, activeSlots: [] }) }),
      update: (ref, data) => fsCalls.push(['tx.update', ref._path, data]),
      set: (ref, data) => fsCalls.push(['tx.set', ref._path, data]),
    });
  },
  query: (ref) => ref,
  where: () => ({}),
  increment: (n) => ({ _increment: n }),
  serverTimestamp: () => ({ seconds: 1 }),
}));

let currentUser = null;
vi.mock('firebase/auth', () => ({
  getAuth: () => ({ currentUser }),
  onAuthStateChanged: (auth, cb) => { cb(currentUser); return () => {}; },
}));

import {
  createPoll,
  deletePoll,
  finishRead,
  removeRead,
  revealRatings,
  setPollStatus,
  setReadSchedule,
  updateReadLabel,
} from '../club-reads.js';

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

/** Firestore calls that WROTE something (reads are allowed on either path). */
function writes() {
  return fsCalls.filter((c) => ['updateDoc', 'setDoc', 'deleteDoc', 'tx.update', 'tx.set'].includes(c[0]));
}

const SESSION = { displayName: 'Skylar' };

beforeEach(() => {
  flagOn = false;
  currentUser = { getIdToken: async () => 'live-token' };
  fsCalls.length = 0;
  getDocImpl = async () => ({ exists: () => true, data: () => ({ milestones: [] }) });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('FLAG OFF — the shipped behaviour, unchanged', () => {
  it('makes no request from any of the seven gated functions', async () => {
    const fetchMock = stubFetch();
    await setReadSchedule({}, 'c1', 'r1', [1, null]);
    await finishRead({}, 'c1', 'r1', 'finished');
    await removeRead({}, 'c1', 'r1');
    await revealRatings({}, 'c1', 'r1');
    await createPoll({}, 'c1', { question: 'Which?', options: ['a', 'b'] }, SESSION);
    await setPollStatus({}, 'c1', 'p1', 'closed');
    await deletePoll({}, 'c1', 'p1');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(writes().length).toBeGreaterThan(0);
  });
});

describe('FLAG ON — each function calls its own route', () => {
  beforeEach(() => { flagOn = true; });

  it('routes every gated function to the path enforce-routes.ts mounts', async () => {
    const fetchMock = stubFetch();

    await setReadSchedule({}, 'c1', 'r1', [1, null]);
    expect(call(fetchMock, 0)).toEqual(['PUT', '/api/clubs/c1/reads/r1/schedule']);

    await finishRead({}, 'c1', 'r1', 'finished');
    expect(call(fetchMock, 1)).toEqual(['POST', '/api/clubs/c1/reads/r1/finish']);

    await removeRead({}, 'c1', 'r1');
    expect(call(fetchMock, 2)).toEqual(['DELETE', '/api/clubs/c1/reads/r1']);

    await revealRatings({}, 'c1', 'r1');
    expect(call(fetchMock, 3)).toEqual(['POST', '/api/clubs/c1/reads/r1/reveal-ratings']);

    await createPoll({}, 'c1', { question: 'Which?', options: ['a', 'b'] }, SESSION);
    expect(call(fetchMock, 4)).toEqual(['POST', '/api/clubs/c1/polls']);

    await setPollStatus({}, 'c1', 'p1', 'closed');
    expect(call(fetchMock, 5)).toEqual(['PUT', '/api/clubs/c1/polls/p1/status']);

    await deletePoll({}, 'c1', 'p1');
    expect(call(fetchMock, 6)).toEqual(['DELETE', '/api/clubs/c1/polls/p1']);
  });

  it('sends the bodies each route validates', async () => {
    const fetchMock = stubFetch();

    await setReadSchedule({}, 'c1', 'r1', [1000, null, 3000]);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ dueAts: [1000, null, 3000] });

    await finishRead({}, 'c1', 'r1', 'abandoned');
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ status: 'abandoned' });

    await setPollStatus({}, 'c1', 'p1', 'open');
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ status: 'open' });
  });

  it('createPoll sends the CLEANED options and the tagging fields, and returns the new id', async () => {
    const fetchMock = stubFetch(async () => ({
      ok: true, status: 200, json: async () => ({ success: true, pollId: 'poll-9' }),
    }));

    const result = await createPoll({}, 'c1', {
      question: '  Which next?  ',
      options: ['  a  ', 'b', '   '],
      readId: 'r1',
      milestoneId: 'm1',
      milestonePosition: 3,
    }, SESSION);

    expect(result).toEqual({ success: true, pollId: 'poll-9' });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      type: 'freeform',
      question: 'Which next?',
      options: ['a', 'b'], // the blank row is dropped by validatePollOptions
      readId: 'r1',
      milestoneId: 'm1',
      milestonePosition: 3,
      displayName: 'Skylar',
    });
  });

  it('createPoll carries next-book refs unchanged', async () => {
    const fetchMock = stubFetch();
    await createPoll({}, 'c1', {
      type: 'nextBook',
      question: 'Next?',
      options: [
        { title: ' Elantris ', author: 'Sanderson', coverHref: '/e.jpg' },
        { title: 'Warbreaker' },
      ],
    }, SESSION);
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.type).toBe('nextBook');
    expect(body.options).toEqual([
      { title: 'Elantris', author: 'Sanderson', coverHref: '/e.jpg' },
      { title: 'Warbreaker', author: '', coverHref: '' },
    ]);
  });

  it('keeps every client-side validation refusal BEFORE the network', async () => {
    const fetchMock = stubFetch();
    expect(await finishRead({}, 'c1', 'r1', 'nope')).toEqual({
      success: false, error: 'Invalid status.',
    });
    expect(await setPollStatus({}, 'c1', 'p1', 'nope')).toEqual({
      success: false, error: 'Invalid status.',
    });
    expect((await createPoll({}, 'c1', { question: '', options: ['a', 'b'] }, SESSION)).error)
      .toBe('Add a poll question.');
    expect((await createPoll({}, 'c1', { question: 'q', options: ['a'] }, SESSION)).error)
      .toMatch(/at least/i);
    expect((await createPoll({}, 'c1', { question: 'q', options: ['a', 'b'] }, null)).error)
      .toBe('Sign in to create a poll.');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('FLAG ON — refreshClubAvatar still runs, and cannot fail the action', () => {
  beforeEach(() => { flagOn = true; });

  it('finishRead repaints the club card after the Worker succeeds', async () => {
    stubFetch();
    const result = await finishRead({}, 'c1', 'r1', 'finished');
    expect(result).toEqual({ success: true });
    // the avatar refresh is the only Firestore write left on this path
    expect(writes().map((c) => c[0])).toEqual(['updateDoc']);
    expect(writes()[0][1]).toBe('clubs/c1');
    expect(Object.keys(writes()[0][2]).sort()).toEqual(['avatarCoverHref', 'avatarReadId']);
  });

  it('removeRead repaints too, and a repaint failure is NOT a removal failure', async () => {
    stubFetch();
    getDocImpl = async () => { throw new Error('offline'); };
    const result = await removeRead({}, 'c1', 'r1');
    expect(result).toEqual({ success: true });
  });

  it('a REFUSED finish never touches Firestore at all — not even the repaint', async () => {
    const detail = 'This action needs the "operateClub" capability.';
    stubFetch(async () => ({ ok: false, status: 403, json: async () => ({ detail }) }));
    expect(await finishRead({}, 'c1', 'r1', 'finished')).toEqual({ success: false, error: detail });
    expect(fsCalls).toEqual([]);
  });
});

describe('FLAG ON — failures are worded and never fall back to Firestore', () => {
  beforeEach(() => { flagOn = true; });

  it('an unreachable Worker is worded as an outage on every function', async () => {
    stubFetch(async () => { throw new TypeError('Failed to fetch'); });
    for (const run of [
      () => setReadSchedule({}, 'c1', 'r1', []),
      () => removeRead({}, 'c1', 'r1'),
      () => revealRatings({}, 'c1', 'r1'),
      () => deletePoll({}, 'c1', 'p1'),
    ]) {
      fsCalls.length = 0;
      const result = await run();
      expect(result.success).toBe(false);
      expect(result.error).toMatch(/connection problem, not a permission one/i);
      expect(fsCalls).toEqual([]);
    }
  });

  it('an already-archived read reads as a worded conflict, not a bare code', async () => {
    const detail = 'This read is already archived.';
    stubFetch(async () => ({ ok: false, status: 409, json: async () => ({ detail }) }));
    expect(await finishRead({}, 'c1', 'r1', 'finished')).toEqual({ success: false, error: detail });
  });
});

describe('⚠️ updateReadLabel STAYS browser-direct — the settled decision', () => {
  it('writes slotLabel to Firestore even with the flag ON, and makes no request', async () => {
    flagOn = true;
    const fetchMock = stubFetch();

    const result = await updateReadLabel({}, 'c1', 'r1', 'Side read');

    expect(result).toEqual({ success: true });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(writes()).toEqual([['updateDoc', 'clubs/c1/reads/r1', { slotLabel: 'Side read' }]]);
  });
});
