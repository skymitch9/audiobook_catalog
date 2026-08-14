// @vitest-environment jsdom
// Feature: club permissions upgrade (2026-08-14) — manager uids bound to
// Firebase Auth identity + the site admin break-glass.
//
// These tests pin two things:
//   1. the GATE LOGIC the UI runs (isClubClaimed / isManagerUid /
//      canManageClub) — the client-side mirror of the firestore.rules gate;
//   2. the RULES CONTRACT lists (MANAGED_CLUB_FIELDS / MANAGED_READ_FIELDS)
//      that must match clubManagedFieldsChanged() / readManagedFieldsChanged()
//      in firestore.rules. If a test here fails after an edit, the rules and
//      the UI have drifted apart — fix BOTH sides, not the test.
import { describe, it, expect, beforeEach, vi } from 'vitest';

// --- In-memory Firestore mock (same shape as clubs.test.js) ---
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
    updateDoc: async (ref, data) => {
      // Test seam: a doc seeded with __denyWrites simulates a firestore.rules
      // PERMISSION_DENIED (e.g. claiming on an already-claimed club).
      if (mockStore[ref._path] && mockStore[ref._path].__denyWrites) {
        const e = new Error('Missing or insufficient permissions.');
        e.code = 'permission-denied';
        throw e;
      }
      mockStore[ref._path] = { ...(mockStore[ref._path] || {}), ...data };
    },
    deleteDoc: async (ref) => { delete mockStore[ref._path]; },
    query: (colRef) => ({ _path: colRef._path }),
    where: () => ({}),
    getDocs: async (q) => ({ docs: [] }),
    serverTimestamp: () => 'server-ts',
    arrayUnion: (...vals) => ({ _arrayUnion: vals }),
    increment: (n) => ({ _increment: n }),
    runTransaction: async (db, fn) =>
      fn({
        get: async (ref) => ({ exists: () => !!mockStore[ref._path], data: () => mockStore[ref._path] }),
        set: (ref, data) => { mockStore[ref._path] = { ...data }; },
        update: (ref, data) => { mockStore[ref._path] = { ...mockStore[ref._path], ...data }; },
        delete: (ref) => { delete mockStore[ref._path]; },
      }),
  };
});

// firebase/auth is pulled in transitively via identity.js; not exercised here.
vi.mock('firebase/auth', () => ({
  getAuth: vi.fn(() => ({ currentUser: null })),
  onAuthStateChanged: vi.fn(() => () => {}),
  signInWithPopup: vi.fn(),
  signInWithRedirect: vi.fn(),
  getRedirectResult: vi.fn(async () => null),
  GoogleAuthProvider: class GoogleAuthProvider {},
  signOut: vi.fn(),
}));

import {
  MANAGED_CLUB_FIELDS, MANAGED_READ_FIELDS,
  isClubClaimed, isManagerUid, canManageClub,
  claimManagerRole, createClub,
} from '../clubs.js';
import { col } from '../fb-env.js';

// jsdom's default URL is localhost, so fb-env resolves the DEV lane here
// (col('clubs') === 'clubs_dev') — build paths through col(), never literals.
const CLUBS = col('clubs');
const topLevelClub = () => {
  const hit = Object.entries(mockStore).find(([p]) =>
    p.startsWith(CLUBS + '/') && !p.slice(CLUBS.length + 1).includes('/'));
  return hit ? hit[1] : undefined;
};

const db = {};
const UID = 'tUidAAA111';
const OTHER = 'tUidBBB222';

beforeEach(() => { mockStore = {}; });

describe('rules contract — the field lists rules gate behind the roster', () => {
  // ⚠️ These arrays are duplicated in firestore.rules
  // (clubManagedFieldsChanged / readManagedFieldsChanged) because rules
  // cannot import JS. This test is the tripwire that keeps them in step.
  it('MANAGED_CLUB_FIELDS pins the manager-only club-doc fields', () => {
    expect([...MANAGED_CLUB_FIELDS].sort()).toEqual([
      'discordWebhookMask', 'features', 'joinMode', 'managerUids',
      'nextMeetingAt', 'nextMeetingNotes',
    ]);
  });

  it('MANAGED_READ_FIELDS pins the manager-only read-doc fields', () => {
    expect([...MANAGED_READ_FIELDS].sort()).toEqual([
      'finishedAt', 'milestones', 'scheduleUpdatedAt', 'slot', 'status',
    ]);
  });

  it('member-action fields are NOT manager-gated (joins/leaves/comments must keep working)', () => {
    // Every field a member (or legacy/anonymous) write path touches. If one
    // of these lands in a MANAGED list, joining or commenting breaks.
    const memberClubFields = [
      'memberSlugs', 'invitedSlugs', 'memberCount', 'hostSlug',
      'hostDisplayName', 'archived', 'archivedAt', 'activeSlots',
      'avatarReadId', 'avatarCoverHref', 'name', 'description', 'emoji',
      'promptsEnabled',
    ];
    for (const f of memberClubFields) expect(MANAGED_CLUB_FIELDS).not.toContain(f);
    const memberReadFields = ['commentCount', 'slotLabel'];
    for (const f of memberReadFields) expect(MANAGED_READ_FIELDS).not.toContain(f);
  });
});

describe('gate logic — the client mirror of the rules gate', () => {
  it('a club with no managerUids is unclaimed and open to anyone (migration path)', () => {
    for (const club of [{}, { managerUids: undefined }, { managerUids: {} }, null]) {
      expect(isClubClaimed(club)).toBe(false);
      expect(canManageClub(club, null)).toBe(true);       // legacy session
      expect(canManageClub(club, OTHER)).toBe(true);      // any signed-in user
    }
  });

  it('a claimed club only accepts roster uids', () => {
    const club = { managerUids: { [UID]: { role: 'host', displayName: 'Skylar' } } };
    expect(isClubClaimed(club)).toBe(true);
    expect(isManagerUid(club, UID)).toBe(true);
    expect(isManagerUid(club, OTHER)).toBe(false);
    expect(canManageClub(club, UID)).toBe(true);
    expect(canManageClub(club, OTHER)).toBe(false);
    expect(canManageClub(club, null)).toBe(false);        // legacy session locked out
  });

  it('the site admin passes the gate on any claimed club without being in the roster', () => {
    const club = { managerUids: { [UID]: { role: 'host' } } };
    expect(canManageClub(club, OTHER, true)).toBe(true);
    expect(canManageClub(club, null, true)).toBe(true);
  });

  it('a malformed managerUids (not a map) counts as unclaimed, not as a lockout', () => {
    expect(isClubClaimed({ managerUids: 'oops' })).toBe(false);
    expect(canManageClub({ managerUids: 42 }, OTHER)).toBe(true);
  });
});

describe('claimManagerRole — trust-on-first-use uid stamping', () => {
  it('writes the caller uid under managerUids.<uid> with role + displayName', async () => {
    mockStore[CLUBS + '/c1'] = { name: 'Reading Rats', hostDisplayName: 'Skylar' };
    const r = await claimManagerRole(db, 'c1', UID, { displayName: 'Skylar' }, 'host');
    expect(r.success).toBe(true);
    // The mock keeps the dotted field path literal — asserting on it pins
    // the write SHAPE: a nested-field update of managerUids.<uid>, never a
    // whole-map replace (which would evict other managers' claims).
    const written = mockStore[CLUBS + '/c1']['managerUids.' + UID];
    expect(written.role).toBe('host');
    expect(written.displayName).toBe('Skylar');
    expect(typeof written.claimedAt).toBe('number');
  });

  it('normalises unknown roles to host and refuses a missing uid', async () => {
    mockStore[CLUBS + '/c2'] = { name: 'x' };
    const r = await claimManagerRole(db, 'c2', UID, { displayName: 'S' }, 'overlord');
    expect(mockStore[CLUBS + '/c2']['managerUids.' + UID].role).toBe('host');
    const denied = await claimManagerRole(db, 'c2', null, { displayName: 'S' }, 'host');
    expect(denied.success).toBe(false);
    expect(denied.error).toMatch(/sign in/i);
  });

  it('maps a rules permission-denied onto the ask-a-bound-manager guidance', async () => {
    // A claimed club whose roster does not include the caller: rules deny
    // the write. The __denyWrites seam makes the mock behave like Firestore.
    mockStore[CLUBS + '/c3'] = {
      name: 'Secured Club',
      managerUids: { [OTHER]: { role: 'host' } },
      __denyWrites: true,
    };
    const res = await claimManagerRole(db, 'c3', UID, { displayName: 'S' }, 'moderator');
    expect(res.success).toBe(false);
    expect(res.error).toMatch(/already secured/i);
    expect(res.error).toMatch(/site admin/i);
    // Nothing was written.
    expect('managerUids.' + UID in mockStore[CLUBS + '/c3']).toBe(false);
  });
});

describe('createClub — clubs are born claimed when a live uid exists', () => {
  const session = { displayName: 'Skylar' };

  it('stamps managerUids with the creator uid (role host)', async () => {
    const r = await createClub(db, { name: 'Night Owls' }, session, UID);
    expect(r.success).toBe(true);
    const club = topLevelClub();
    expect(club.managerUids[UID].role).toBe('host');
    expect(club.managerUids[UID].displayName).toBe('Skylar');
    expect(club.hostDisplayName).toBe('Skylar');   // display layer untouched
    expect(club.hostSlug).toBe('skylar');
  });

  it('creates an UNCLAIMED club when no uid is passed (legacy session path)', async () => {
    const r = await createClub(db, { name: 'Legacy Club' }, session);
    expect(r.success).toBe(true);
    const club = topLevelClub();
    expect('managerUids' in club).toBe(false);
    expect(isClubClaimed(club)).toBe(false);
  });
});
