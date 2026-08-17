// @vitest-environment jsdom
// Feature: club permissions upgrade (2026-08-14) — manager uids bound to
// Firebase Auth identity + the site admin break-glass. Extended 2026-08-16
// with a third club-doc field tier (RESTRICTED) that locks the Discord
// webhook and managerUids itself to the site admin — no club mod, not even
// a bound host/mod. Everything else a club mod could do stays exactly as
// it was: joinMode, features, next meeting (club-doc), and the read-doc
// fields (schedule/milestones, finish/abandon, the ratings reveal flip).
//
// These tests pin three things:
//   1. the GATE LOGIC the UI runs (isClubClaimed / isManagerUid /
//      canManageClub / canOperateClub / canAdministerClub) — the client-side
//      mirror of the firestore.rules gates;
//   2. the RULES CONTRACT lists (MANAGED_CLUB_FIELDS / MANAGED_READ_FIELDS,
//      and their STRUCTURAL/OPERATIONAL/RESTRICTED tier breakdowns) that
//      must match clubStructuralFieldsChanged() / clubOperationalFieldsChanged()
//      / clubRestrictedFieldsChanged() / readStructuralFieldsChanged() /
//      readOperationalFieldsChanged() in firestore.rules;
//   3. the CLUB-MOD BOUNDARY itself: a claimed club's own roster member can
//      still do everything in STRUCTURAL/OPERATIONAL, but canAdministerClub
//      refuses them — only the site admin passes.
// If a test here fails after an edit, the rules and the UI have drifted
// apart — fix BOTH sides, not the test.
import { describe, it, expect, beforeEach, vi } from 'vitest';

// The Phase 1 shadow reporter (gate-shadow.js) fires fire-and-forget from
// the gated write paths under test; mock it so no test ever touches the
// network. Its own contract is pinned in gate-shadow.test.js.
vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

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
  STRUCTURAL_CLUB_FIELDS, OPERATIONAL_CLUB_FIELDS, RESTRICTED_CLUB_FIELDS,
  STRUCTURAL_READ_FIELDS, OPERATIONAL_READ_FIELDS,
  isClubClaimed, isManagerUid, canManageClub, canOperateClub, canAdministerClub,
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
  // (clubStructuralFieldsChanged / clubOperationalFieldsChanged /
  // clubRestrictedFieldsChanged / readStructuralFieldsChanged /
  // readOperationalFieldsChanged) because rules cannot import JS. These
  // tests are the tripwire that keeps them in step — including the
  // three-tier split (2026-08-14) and the RESTRICTED tier (2026-08-16).
  it('STRUCTURAL_CLUB_FIELDS pins the club-mod-reachable "club island" fields', () => {
    // joinMode + features stay club-mod territory — the owner explicitly
    // preserved these ("let a club mod change the books and stuff like they
    // can now"). discordWebhookMask and managerUids moved OUT to RESTRICTED.
    expect([...STRUCTURAL_CLUB_FIELDS].sort()).toEqual(['features', 'joinMode']);
  });

  it('OPERATIONAL_CLUB_FIELDS pins the moderator-reachable club-doc fields', () => {
    expect([...OPERATIONAL_CLUB_FIELDS].sort()).toEqual([
      'nextMeetingAt', 'nextMeetingNotes',
    ]);
  });

  it('RESTRICTED_CLUB_FIELDS pins the site-admin-only club-doc fields (2026-08-16)', () => {
    // The Discord webhook (outbound capability) and managerUids itself
    // (peer-escalation) — locked away from the club's own manager roster.
    expect([...RESTRICTED_CLUB_FIELDS].sort()).toEqual(['discordWebhookMask', 'managerUids']);
  });

  it('MANAGED_CLUB_FIELDS is exactly the union of all three tiers', () => {
    expect([...MANAGED_CLUB_FIELDS].sort()).toEqual([
      'discordWebhookMask', 'features', 'joinMode', 'managerUids',
      'nextMeetingAt', 'nextMeetingNotes',
    ]);
    expect([...MANAGED_CLUB_FIELDS].sort()).toEqual(
      [...STRUCTURAL_CLUB_FIELDS, ...OPERATIONAL_CLUB_FIELDS, ...RESTRICTED_CLUB_FIELDS].sort());
  });

  it('no club-doc field sits in more than one tier', () => {
    for (const f of STRUCTURAL_CLUB_FIELDS) {
      expect(OPERATIONAL_CLUB_FIELDS).not.toContain(f);
      expect(RESTRICTED_CLUB_FIELDS).not.toContain(f);
    }
    for (const f of OPERATIONAL_CLUB_FIELDS) expect(RESTRICTED_CLUB_FIELDS).not.toContain(f);
  });

  it('STRUCTURAL_READ_FIELDS pins lifecycle + the reveal flip as manager/admin-only', () => {
    expect([...STRUCTURAL_READ_FIELDS].sort()).toEqual([
      'finishedAt', 'ratingsRevealed', 'revealedAt', 'slot', 'status',
    ]);
  });

  it('OPERATIONAL_READ_FIELDS pins the reading schedule as moderator-reachable', () => {
    expect([...OPERATIONAL_READ_FIELDS].sort()).toEqual([
      'milestones', 'scheduleUpdatedAt',
    ]);
  });

  it('MANAGED_READ_FIELDS is exactly the union of the two tiers', () => {
    expect([...MANAGED_READ_FIELDS].sort()).toEqual([
      'finishedAt', 'milestones', 'ratingsRevealed', 'revealedAt',
      'scheduleUpdatedAt', 'slot', 'status',
    ]);
    expect([...MANAGED_READ_FIELDS].sort()).toEqual(
      [...STRUCTURAL_READ_FIELDS, ...OPERATIONAL_READ_FIELDS].sort());
  });

  it('no field sits in both tiers — a field has exactly one gate', () => {
    for (const f of STRUCTURAL_CLUB_FIELDS) expect(OPERATIONAL_CLUB_FIELDS).not.toContain(f);
    for (const f of STRUCTURAL_READ_FIELDS) expect(OPERATIONAL_READ_FIELDS).not.toContain(f);
  });

  it('member-action fields are NOT manager-gated, including RESTRICTED (joins/leaves/comments must keep working)', () => {
    // Every field a member (or legacy/anonymous) write path touches. If one
    // of these lands in a MANAGED list, joining or commenting breaks.
    const memberClubFields = [
      'memberSlugs', 'invitedSlugs', 'memberCount', 'hostSlug',
      'hostDisplayName', 'archived', 'archivedAt', 'activeSlots',
      'avatarReadId', 'avatarCoverHref', 'name', 'description', 'emoji',
      'promptsEnabled',
    ];
    for (const f of memberClubFields) expect(MANAGED_CLUB_FIELDS).not.toContain(f);
    // ratingCount stays open too — same "bumped by every writer" pattern as
    // commentCount (see the blind-ratings section of club-reads.js).
    const memberReadFields = ['commentCount', 'slotLabel', 'ratingCount'];
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

describe('canOperateClub — the OPERATIONAL gate (three-tier model)', () => {
  const claimed = { managerUids: { [UID]: { role: 'host' } } };

  it('admits everyone canManageClub admits (roster uid, site admin, unclaimed)', () => {
    expect(canOperateClub(claimed, UID)).toBe(true);              // roster uid
    expect(canOperateClub(claimed, OTHER, 'admin')).toBe(true);   // site admin
    expect(canOperateClub({}, null)).toBe(true);                  // unclaimed (migration)
  });

  it('admits the site MODERATOR on a claimed club they do not manage', () => {
    expect(canOperateClub(claimed, OTHER, 'moderator')).toBe(true);
    expect(canOperateClub(claimed, null, 'moderator')).toBe(true);
  });

  it('refuses a plain signed-in user (and legacy sessions) on a claimed club', () => {
    expect(canOperateClub(claimed, OTHER)).toBe(false);
    expect(canOperateClub(claimed, OTHER, null)).toBe(false);
    expect(canOperateClub(claimed, null)).toBe(false);
  });

  it('an unknown role string grants nothing', () => {
    expect(canOperateClub(claimed, OTHER, 'overlord')).toBe(false);
  });

  it('the moderator does NOT pass the STRUCTURAL gate (canManageClub)', () => {
    // The distinction that IS the three-tier model: moderator operates,
    // never manages — features/webhook/identity/roster/deletes stay closed.
    expect(canManageClub(claimed, OTHER, false)).toBe(false);
  });
});

describe('canAdministerClub — the RESTRICTED gate (2026-08-16 tightening)', () => {
  // This is the test the owner asked for by name: a club's own manager
  // roster (a bound host/mod) must NOT be able to touch the Discord webhook
  // or managerUids, only the site admin. Everything else that roster
  // member can do (STRUCTURAL/OPERATIONAL) must keep working — the "club
  // island" is preserved, only these two fields moved out.
  const claimed = { managerUids: { [UID]: { role: 'host', displayName: 'Skylar' } } };

  it('a bound club-roster manager (host) CANNOT administer a claimed club', () => {
    expect(canAdministerClub(claimed, false)).toBe(false);
    // ...even though that same uid fully passes the STRUCTURAL/OPERATIONAL
    // gates that still govern the club island (features, joinMode, next
    // meeting) — the roster uid is not even accepted as an argument here,
    // by design (see the function's own comment in clubs.js).
    expect(canManageClub(claimed, UID)).toBe(true);
    expect(canOperateClub(claimed, UID)).toBe(true);
  });

  it('the site moderator also does NOT administer a claimed club', () => {
    expect(canOperateClub(claimed, OTHER, 'moderator')).toBe(true);   // operates
    expect(canAdministerClub(claimed, false)).toBe(false);            // never administers
  });

  it('the site admin administers any claimed club', () => {
    expect(canAdministerClub(claimed, true)).toBe(true);
  });

  it('an unclaimed club stays open (transition safety, same migration path as canManageClub)', () => {
    for (const club of [{}, { managerUids: {} }, null]) {
      expect(canAdministerClub(club, false)).toBe(true);
      expect(canAdministerClub(club, true)).toBe(true);
    }
  });

  it('a malformed managerUids counts as unclaimed here too, not a lockout', () => {
    expect(canAdministerClub({ managerUids: 'oops' }, false)).toBe(true);
  });
});

describe('club-mod boundary — what a claimed club\'s own roster CAN and CANNOT still do', () => {
  // One place that states the preserved set plainly, so it is auditable
  // rather than implied by what other tests don't cover. A club mod here
  // is a roster uid on a claimed club, not the site admin.
  const claimed = { managerUids: { [UID]: { role: 'moderator', displayName: 'Rowan' } } };

  it('CAN: club-doc STRUCTURAL fields (joinMode, features)', () => {
    expect(canManageClub(claimed, UID)).toBe(true);
  });

  it('CAN: club-doc OPERATIONAL fields (next meeting)', () => {
    expect(canOperateClub(claimed, UID)).toBe(true);
  });

  it('CAN: read-doc STRUCTURAL fields (status/finishedAt/slot/ratingsRevealed/revealedAt — finish, abandon, blind-ratings reveal)', () => {
    // Read-doc gating uses the same canManageClub/canOperateClub mirrors
    // against the parent club; STRUCTURAL_READ_FIELDS/OPERATIONAL_READ_FIELDS
    // were NOT touched by this change (see the field-tier pinning tests).
    expect(STRUCTURAL_READ_FIELDS).toEqual(
      expect.arrayContaining(['status', 'finishedAt', 'slot', 'ratingsRevealed', 'revealedAt']));
    expect(canManageClub(claimed, UID)).toBe(true);
  });

  it('CAN: read-doc OPERATIONAL fields (milestones/scheduleUpdatedAt — the reading schedule)', () => {
    expect(OPERATIONAL_READ_FIELDS).toEqual(expect.arrayContaining(['milestones', 'scheduleUpdatedAt']));
    expect(canOperateClub(claimed, UID)).toBe(true);
  });

  it('CANNOT: the Discord webhook or managerUids (RESTRICTED — site admin only)', () => {
    expect(canAdministerClub(claimed, false)).toBe(false);
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

  it('maps a rules permission-denied onto ask-the-site-admin guidance (RESTRICTED, 2026-08-16)', async () => {
    // A claimed club whose roster does not include the caller: rules deny
    // the write. The __denyWrites seam makes the mock behave like Firestore.
    // Since managerUids moved to RESTRICTED, this is now the ONLY path —
    // even an existing bound manager could not add this uid for them, so
    // the guidance names the site admin only (no more "ask a bound host/mod").
    mockStore[CLUBS + '/c3'] = {
      name: 'Secured Club',
      managerUids: { [OTHER]: { role: 'host' } },
      __denyWrites: true,
    };
    const res = await claimManagerRole(db, 'c3', UID, { displayName: 'S' }, 'moderator');
    expect(res.success).toBe(false);
    expect(res.error).toMatch(/already secured/i);
    expect(res.error).toMatch(/site admin/i);
    expect(res.error).not.toMatch(/bound host\/mod/i);
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
