// @vitest-environment jsdom
// @vitest-environment-options { "url": "https://audiobooks.heygabi.ai/" }
//
// Feature: auth-migration Phase 3a, surface 2 — site/clubs.js on the Worker's
// club routes (catalog-platform/apps/audiobook-worker/src/enforce-routes.ts).
//
// Nine functions move; the properties worth pinning are the same four as for
// reviews, plus the two this surface adds:
//
//   5. ⚠️ THE SPLIT. `updateClubDetails` is the only function whose edit
//      spans both worlds — `joinMode`/`features`/`nextMeetingAt`/
//      `nextMeetingNotes` are gated, `name`/`description`/`emoji`/… are
//      member-editable and stay browser-direct. Send a member field to the
//      PATCH and the whole call is refused; keep a gated one on the direct
//      path and Phase 3b's rules deploy refuses it the day it lands. Both
//      directions are asserted.
//   6. ⚠️ ORDER. The gated half goes FIRST, so a refusal leaves the club
//      entirely unchanged rather than half-saved.
//
// The member-open writes (createClub, joinClub, leaveClub, acceptInvite,
// declineInvite, requestToJoin, dismissRateNudge) have no route by design;
// a test below pins that the flag does not touch them.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

let flagOn = false;
vi.mock('../flags.js', () => ({
  flagEnabled: (name) => (name === 'AUTH_ROUTES_CLUBS' ? flagOn : false),
}));

const fsCalls = [];
const RECORD_UPDATE = async (ref, data) => { fsCalls.push(['updateDoc', ref._path, data]); };
let updateDocImpl = RECORD_UPDATE;
vi.mock('firebase/firestore', () => ({
  doc: (db, ...parts) => ({ _path: parts.join('/') }),
  collection: (db, ...parts) => ({ _path: parts.join('/') }),
  updateDoc: (...args) => updateDocImpl(...args),
  setDoc: async (ref, data) => { fsCalls.push(['setDoc', ref._path, data]); },
  deleteDoc: async (ref) => { fsCalls.push(['deleteDoc', ref._path]); },
  getDoc: async (ref) => { fsCalls.push(['getDoc', ref._path]); return { exists: () => false, data: () => undefined }; },
  getDocs: async (ref) => { fsCalls.push(['getDocs', ref._path]); return { docs: [] }; },
  runTransaction: async (db, fn) => {
    fsCalls.push(['runTransaction']);
    return fn({
      get: async (ref) => ({ exists: () => true, data: () => ({ memberSlugs: [], invitedSlugs: [] }) }),
      update: (ref, data) => fsCalls.push(['tx.update', ref._path, data]),
      set: (ref, data) => fsCalls.push(['tx.set', ref._path, data]),
    });
  },
  query: (ref) => ref,
  where: () => ({}),
  arrayUnion: (...v) => ({ _arrayUnion: v }),
  serverTimestamp: () => ({ seconds: 1 }),
}));

let currentUser = null;
vi.mock('firebase/auth', () => ({
  getAuth: () => ({ currentUser }),
  onAuthStateChanged: (auth, cb) => { cb(currentUser); return () => {}; },
}));

import {
  WORKER_PATCHABLE_CLUB_FIELDS,
  acceptRequest,
  clearClubDiscordWebhook,
  claimManagerRole,
  createClub,
  deleteClub,
  inviteMember,
  joinClub,
  rejectRequest,
  removeMemberBySlug,
  setClubDiscordWebhook,
  setMemberRole,
  splitClubUpdates,
  updateClubDetails,
} from '../clubs.js';

const WEBHOOK = 'https://discord.com/api/webhooks/12345/AbCdEf-1234';

function stubFetch(impl) {
  const mock = vi.fn(
    impl || (async () => ({ ok: true, status: 200, json: async () => ({ success: true }) })),
  );
  vi.stubGlobal('fetch', mock);
  return mock;
}

/** [method, path] of one fetch call, with the origin stripped. */
function call(fetchMock, i = 0) {
  const [url, init] = fetchMock.mock.calls[i];
  return [init.method, url.replace('https://audiobook-api.heygabi.ai', '')];
}

beforeEach(() => {
  flagOn = false;
  currentUser = { getIdToken: async () => 'live-token' };
  fsCalls.length = 0;
  updateDocImpl = RECORD_UPDATE;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('WORKER_PATCHABLE_CLUB_FIELDS — the split contract', () => {
  it('is exactly the STRUCTURAL + OPERATIONAL tiers, and no RESTRICTED field', () => {
    expect([...WORKER_PATCHABLE_CLUB_FIELDS].sort()).toEqual([
      'features', 'joinMode', 'nextMeetingAt', 'nextMeetingNotes',
    ]);
    expect(WORKER_PATCHABLE_CLUB_FIELDS).not.toContain('discordWebhookMask');
    expect(WORKER_PATCHABLE_CLUB_FIELDS).not.toContain('managerUids');
  });

  it('splitClubUpdates puts every member-editable field on the direct side', () => {
    const { gated, direct } = splitClubUpdates({
      name: 'New name',
      description: 'd',
      emoji: '📗',
      avatarReadId: 'r1',
      avatarCoverHref: '/c.jpg',
      promptsEnabled: true,
      joinMode: 'application',
      features: { polls: true },
      nextMeetingAt: 1,
      nextMeetingNotes: 'n',
    });
    expect(Object.keys(gated).sort()).toEqual([
      'features', 'joinMode', 'nextMeetingAt', 'nextMeetingNotes',
    ]);
    expect(Object.keys(direct).sort()).toEqual([
      'avatarCoverHref', 'avatarReadId', 'description', 'emoji', 'name', 'promptsEnabled',
    ]);
  });
});

describe('FLAG OFF — the shipped behaviour, unchanged for every function', () => {
  it('makes no request from any of the nine gated functions', async () => {
    const fetchMock = stubFetch();
    await updateClubDetails({}, 'c1', { joinMode: 'application' });
    await deleteClub({}, 'c1');
    await setClubDiscordWebhook({}, 'c1', WEBHOOK, { displayName: 'Sky' });
    await clearClubDiscordWebhook({}, 'c1');
    await claimManagerRole({}, 'c1', 'uid-1', { displayName: 'Sky' }, 'host');
    await setMemberRole({}, 'c1', 'gabi', 'moderator');
    await removeMemberBySlug({}, 'c1', 'gabi');
    await acceptRequest({}, 'c1', 'gabi');
    await rejectRequest({}, 'c1', 'gabi');
    await inviteMember({}, 'c1', 'Gabi');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(fsCalls.length).toBeGreaterThan(0);
  });

  it('updateClubDetails still writes ONE updateDoc carrying both tiers', async () => {
    stubFetch();
    await updateClubDetails({}, 'c1', { name: 'New', joinMode: 'application' });
    const updates = fsCalls.filter((c) => c[0] === 'updateDoc');
    expect(updates).toHaveLength(1);
    expect(updates[0][2]).toEqual({ name: 'New', joinMode: 'application' });
  });
});

describe('FLAG ON — each function calls its own route', () => {
  beforeEach(() => { flagOn = true; });

  it('routes every gated function to the path enforce-routes.ts mounts', async () => {
    const fetchMock = stubFetch();

    await updateClubDetails({}, 'c1', { joinMode: 'application' });
    expect(call(fetchMock, 0)).toEqual(['PATCH', '/api/clubs/c1']);

    await deleteClub({}, 'c1');
    expect(call(fetchMock, 1)).toEqual(['DELETE', '/api/clubs/c1']);

    await setClubDiscordWebhook({}, 'c1', WEBHOOK, { displayName: 'Sky' });
    expect(call(fetchMock, 2)).toEqual(['PUT', '/api/clubs/c1/webhook']);

    await clearClubDiscordWebhook({}, 'c1');
    expect(call(fetchMock, 3)).toEqual(['DELETE', '/api/clubs/c1/webhook']);

    await claimManagerRole({}, 'c1', 'uid-1', { displayName: 'Sky' }, 'host');
    expect(call(fetchMock, 4)).toEqual(['POST', '/api/clubs/c1/managers/claim']);

    await setMemberRole({}, 'c1', 'gabi', 'moderator');
    expect(call(fetchMock, 5)).toEqual(['PUT', '/api/clubs/c1/members/gabi/role']);

    await removeMemberBySlug({}, 'c1', 'gabi');
    expect(call(fetchMock, 6)).toEqual(['DELETE', '/api/clubs/c1/members/gabi']);

    await acceptRequest({}, 'c1', 'gabi');
    expect(call(fetchMock, 7)).toEqual(['POST', '/api/clubs/c1/requests/gabi/accept']);

    await rejectRequest({}, 'c1', 'gabi');
    expect(call(fetchMock, 8)).toEqual(['DELETE', '/api/clubs/c1/requests/gabi']);

    await inviteMember({}, 'c1', 'Gabi');
    expect(call(fetchMock, 9)).toEqual(['POST', '/api/clubs/c1/invites']);

    // ⚠️ and NOTHING went to Firestore on any of them
    expect(fsCalls).toEqual([]);
  });

  it('percent-encodes the club id and the slug into the path', async () => {
    const fetchMock = stubFetch();
    await removeMemberBySlug({}, 'c/1', 'a b');
    expect(call(fetchMock)).toEqual(['DELETE', '/api/clubs/c%2F1/members/a%20b']);
  });

  it('sends the bodies each route validates', async () => {
    const fetchMock = stubFetch();
    await setClubDiscordWebhook({}, 'c1', WEBHOOK, { displayName: 'Sky' });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      url: WEBHOOK, displayName: 'Sky',
    });

    await claimManagerRole({}, 'c1', 'uid-1', { displayName: 'Sky' }, 'moderator');
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      role: 'moderator', displayName: 'Sky',
    });

    await setMemberRole({}, 'c1', 'gabi', 'member');
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ role: 'member' });

    await inviteMember({}, 'c1', '  Gabi  ');
    expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toEqual({ displayName: 'Gabi' });
  });

  it('keeps every client-side validation refusal BEFORE the network', async () => {
    const fetchMock = stubFetch();
    expect(await setClubDiscordWebhook({}, 'c1', 'not-a-webhook', {})).toEqual({
      success: false,
      error: expect.stringContaining('does not look like a Discord webhook URL'),
    });
    expect(await setMemberRole({}, 'c1', 'gabi', 'emperor')).toEqual({
      success: false, error: 'Invalid role.',
    });
    expect(await inviteMember({}, 'c1', 'G')).toEqual({
      success: false, error: 'Enter a display name.',
    });
    expect((await claimManagerRole({}, 'c1', null, {}, 'host')).error)
      .toMatch(/Sign in with Google/);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('FLAG ON — updateClubDetails splits the edit, gated half first', () => {
  beforeEach(() => { flagOn = true; });

  it('sends ONLY the gated fields to the PATCH, and the rest direct', async () => {
    const fetchMock = stubFetch();

    const result = await updateClubDetails({}, 'c1', {
      name: 'New name',
      joinMode: 'application',
      features: { polls: true },
    });

    expect(result).toEqual({ success: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      joinMode: 'application', features: { polls: true },
    });
    const updates = fsCalls.filter((c) => c[0] === 'updateDoc');
    expect(updates).toHaveLength(1);
    expect(updates[0][2]).toEqual({ name: 'New name' });
  });

  it('a member-only edit makes NO request at all — nothing gated to send', async () => {
    const fetchMock = stubFetch();
    const result = await updateClubDetails({}, 'c1', { name: 'New name', emoji: '📗' });
    expect(result).toEqual({ success: true });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(fsCalls.filter((c) => c[0] === 'updateDoc')).toHaveLength(1);
  });

  it('a gated-only edit makes NO Firestore write', async () => {
    stubFetch();
    await updateClubDetails({}, 'c1', { nextMeetingNotes: 'bring snacks' });
    expect(fsCalls).toEqual([]);
  });

  it('⚠️ a refused PATCH leaves the club ENTIRELY unchanged — the direct half never runs', async () => {
    const detail = 'This action needs the "manageClub" capability, which the admin role holds.';
    stubFetch(async () => ({ ok: false, status: 403, json: async () => ({ detail }) }));

    const result = await updateClubDetails({}, 'c1', { name: 'New name', joinMode: 'open' });

    expect(result).toEqual({ success: false, error: detail });
    expect(fsCalls).toEqual([]);
  });

  it('a direct-half failure after a landed PATCH says which half saved', async () => {
    stubFetch();
    updateDocImpl = async () => { throw new Error('write refused'); };

    const result = await updateClubDetails({}, 'c1', { name: 'New name', joinMode: 'open' });

    expect(result.success).toBe(false);
    // ⚠️ Not a bare failure: the person is told the settings DID save, so
    // they retry the right half instead of the whole modal.
    expect(result.error).toMatch(/club settings were saved/i);
    expect(result.error).toMatch(/name\/description could not be/i);
  });
});

describe('FLAG ON — failures are worded and never fall back to Firestore', () => {
  beforeEach(() => { flagOn = true; });

  it('a refusal is the Worker sentence, and nothing is written', async () => {
    const detail = 'Your household access has been revoked, so this action is refused.';
    stubFetch(async () => ({ ok: false, status: 403, json: async () => ({ detail }) }));
    expect(await deleteClub({}, 'c1')).toEqual({ success: false, error: detail });
    expect(fsCalls).toEqual([]);
  });

  it('an unreachable Worker is worded as an outage, and nothing is written', async () => {
    stubFetch(async () => { throw new TypeError('Failed to fetch'); });
    const result = await removeMemberBySlug({}, 'c1', 'gabi');
    expect(result.success).toBe(false);
    expect(result.error).toMatch(/connection problem, not a permission one/i);
    expect(fsCalls).toEqual([]);
  });

  it('a lost claim race reads as "somebody claimed it first", not as a bare conflict', async () => {
    const detail = 'This club changed while the claim was being written — most likely '
      + 'somebody else claimed it first.';
    stubFetch(async () => ({ ok: false, status: 409, json: async () => ({ detail }) }));
    expect(await claimManagerRole({}, 'c1', 'uid', { displayName: 'S' }, 'host')).toEqual({
      success: false, error: detail,
    });
  });
});

describe('⚠️ the member-open writes are NOT touched by the flag', () => {
  beforeEach(() => { flagOn = true; });

  it('createClub and joinClub still go straight to Firestore', async () => {
    const fetchMock = stubFetch();
    await createClub({}, { name: 'Book Club' }, { displayName: 'Sky' }, 'uid-1');
    await joinClub({}, 'c1', { displayName: 'Sky' });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(fsCalls.some((c) => c[0] === 'setDoc')).toBe(true);
  });
});
