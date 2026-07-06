// @vitest-environment jsdom
// Feature: book-clubs-phase1 — club create/browse/join/leave + members
import { describe, it, expect, beforeEach, vi } from 'vitest';

// --- In-memory Firestore mock ---
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
    deleteDoc: async (ref) => { delete mockStore[ref._path]; },
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
        update: (ref, data) => { mockStore[ref._path] = { ...mockStore[ref._path], ...data }; },
        delete: (ref) => { delete mockStore[ref._path]; },
      }),
  };
});

// firebase/auth is pulled in transitively via identity.js; not exercised here.
vi.mock('firebase/auth', () => ({
  getAuth: vi.fn(),
  signInWithPopup: vi.fn(),
  GoogleAuthProvider: vi.fn(),
  onAuthStateChanged: vi.fn(),
  signOut: vi.fn(),
}));

const {
  validateClubName, validateClubDescription,
  createClub, getAllClubs, getMyClubs, getClub, getMembers,
  joinClub, leaveClub, removeMemberBySlug,
  setMemberRole, deleteClub,
} = await import('../clubs.js');

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
const bob = { displayName: 'Bob Brown' };

async function makeClub(overrides = {}) {
  const result = await createClub(fakeDb, { name: 'LitRPG Legends', ...overrides }, jane);
  expect(result.success).toBe(true);
  return result.clubId;
}

beforeEach(() => {
  mockStore = {};
});

describe('validation', () => {
  it('rejects club names under 3 or over 40 chars', () => {
    expect(validateClubName('ab').valid).toBe(false);
    expect(validateClubName('  a  ').valid).toBe(false);
    expect(validateClubName('x'.repeat(41)).valid).toBe(false);
    expect(validateClubName('Sci-Fi Squad').valid).toBe(true);
  });

  it('rejects descriptions over 300 chars, allows empty', () => {
    expect(validateClubDescription('').valid).toBe(true);
    expect(validateClubDescription('x'.repeat(301)).valid).toBe(false);
  });
});

describe('createClub', () => {
  it('requires a session', async () => {
    const r = await createClub(fakeDb, { name: 'No Auth Club' }, null);
    expect(r.success).toBe(false);
  });

  it('creates the club with the creator as host and sole member', async () => {
    const clubId = await makeClub({ description: 'We read LitRPG.' });
    const club = await getClub(fakeDb, clubId);
    expect(club.name).toBe('LitRPG Legends');
    expect(club.hostSlug).toBe('jane doe');
    expect(club.memberCount).toBe(1);
    expect(club.memberSlugs).toEqual(['jane doe']);

    const members = await getMembers(fakeDb, clubId);
    expect(members).toHaveLength(1);
    expect(members[0].role).toBe('host');
    expect(members[0].displayName).toBe('Jane Doe');
  });

  it('rejects invalid names', async () => {
    const r = await createClub(fakeDb, { name: 'xy' }, jane);
    expect(r.success).toBe(false);
  });
});

describe('browse queries', () => {
  it('getAllClubs returns every club', async () => {
    await makeClub({ name: 'First Club' });
    await makeClub({ name: 'Second Club' });
    const all = await getAllClubs(fakeDb);
    expect(all.map((c) => c.name).sort()).toEqual(['First Club', 'Second Club']);
  });

  it('getMyClubs returns clubs containing the member', async () => {
    const clubId = await makeClub({ name: 'Janes Club' });
    await makeClub({ name: 'Also Janes' });
    await joinClub(fakeDb, clubId, bob);
    expect((await getMyClubs(fakeDb, 'Jane Doe')).length).toBe(2);
    expect((await getMyClubs(fakeDb, 'Bob Brown')).map((c) => c.name)).toEqual(['Janes Club']);
  });
});

describe('joinClub / leaveClub', () => {
  it('adds a member and keeps memberCount in sync', async () => {
    const clubId = await makeClub();
    const r = await joinClub(fakeDb, clubId, bob);
    expect(r.success).toBe(true);
    const club = await getClub(fakeDb, clubId);
    expect(club.memberCount).toBe(2);
    expect(club.memberSlugs).toContain('bob brown');
    const members = await getMembers(fakeDb, clubId);
    expect(members.find((m) => m.slug === 'bob brown').role).toBe('member');
  });

  it('is idempotent — rejoining changes nothing', async () => {
    const clubId = await makeClub();
    await joinClub(fakeDb, clubId, bob);
    await joinClub(fakeDb, clubId, bob);
    expect((await getClub(fakeDb, clubId)).memberCount).toBe(2);
    expect(await getMembers(fakeDb, clubId)).toHaveLength(2);
  });

  it('fails for a nonexistent club', async () => {
    const r = await joinClub(fakeDb, 'nope', bob);
    expect(r.success).toBe(false);
  });

  it('a member can leave; count and docs update', async () => {
    const clubId = await makeClub();
    await joinClub(fakeDb, clubId, bob);
    const r = await leaveClub(fakeDb, clubId, bob);
    expect(r.success).toBe(true);
    expect((await getClub(fakeDb, clubId)).memberCount).toBe(1);
    expect(await getMembers(fakeDb, clubId)).toHaveLength(1);
  });

  it('the host cannot leave', async () => {
    const clubId = await makeClub();
    const r = await leaveClub(fakeDb, clubId, jane);
    expect(r.success).toBe(false);
    expect((await getClub(fakeDb, clubId)).memberCount).toBe(1);
  });
});

describe('setMemberRole', () => {
  it('promotes and demotes members', async () => {
    const clubId = await makeClub();
    await joinClub(fakeDb, clubId, bob);
    expect((await setMemberRole(fakeDb, clubId, 'bob brown', 'moderator')).success).toBe(true);
    expect((await getMembers(fakeDb, clubId)).find((m) => m.slug === 'bob brown').role).toBe('moderator');
    expect((await setMemberRole(fakeDb, clubId, 'bob brown', 'member')).success).toBe(true);
  });

  it("refuses to change the host's role or use invalid roles", async () => {
    const clubId = await makeClub();
    expect((await setMemberRole(fakeDb, clubId, 'jane doe', 'member')).success).toBe(false);
    expect((await setMemberRole(fakeDb, clubId, 'jane doe', 'admin')).success).toBe(false);
  });
});

describe('deleteClub', () => {
  it('removes the club and all member docs', async () => {
    const clubId = await makeClub();
    await joinClub(fakeDb, clubId, bob);
    const r = await deleteClub(fakeDb, clubId);
    expect(r.success).toBe(true);
    expect(await getClub(fakeDb, clubId)).toBeNull();
    expect(Object.keys(mockStore)).toHaveLength(0);
  });
});
