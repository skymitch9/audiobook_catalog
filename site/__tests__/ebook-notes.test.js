// @vitest-environment jsdom
// Feature: the gated ebook shelf's "Content notes" block (site/ebook-notes.js)
//
// ⚠️ WHAT THESE TESTS ARE FOR. The store, the doc id, the delete rules and the
// 80-char bound are user-warnings.js's and are pinned by user-warnings.test.js.
// This suite pins the two things the SHELF answers for itself, and the first
// of them is the one that fails SILENTLY in production if it is wrong:
//
//   1. WHICH TITLE a note is keyed by. An ebook's epub-metadata title is a
//      different spelling from the audiobook catalog's, so keying on it files
//      notes where nobody reads AND finds none written elsewhere — both
//      looking exactly like "nobody has added one yet". No error, no symptom.
//   2. Published sources CHECKED-AND-CLEAN vs NEVER-CHECKED vs UNREACHABLE:
//      three different facts that must not collapse into one sentence.
import { describe, it, expect, beforeEach, vi } from 'vitest';

let mockStore = {};

vi.mock('firebase/firestore', () => ({
  collection: (db, ...segs) => ({ _type: 'col', _path: segs.join('/') }),
  doc: (dbOrCol, ...segs) => ({ _path: segs.join('/'), id: segs[segs.length - 1] }),
  setDoc: async (ref, data) => { mockStore[ref._path] = { ...data }; },
  getDoc: async (ref) => ({
    exists: () => Object.prototype.hasOwnProperty.call(mockStore, ref._path),
    data: () => mockStore[ref._path],
  }),
  deleteDoc: async (ref) => { delete mockStore[ref._path]; },
  query: (colRef, ...filters) => ({ _path: colRef._path, _filters: filters }),
  where: (field, op, value) => ({ field, op, value }),
  getDocs: async (q) => {
    const prefix = q._path + '/';
    const filters = q._filters || [];
    const docs = Object.entries(mockStore)
      .filter(([p]) => p.startsWith(prefix) && !p.slice(prefix.length).includes('/'))
      .map(([p, data]) => ({ id: p.split('/').pop(), data: () => data, exists: () => true }))
      .filter((d) => filters.every((f) => (f.op === '==' ? d.data()[f.field] === f.value : true)));
    return { docs };
  },
  serverTimestamp: () => 'server-ts',
}));
vi.mock('firebase/app', () => ({ getApp: () => ({ name: '[DEFAULT]' }) }));

let liveUid = 'uid-jane';
vi.mock('../identity.js', () => ({
  getLiveUser: async () => (liveUid ? { uid: liveUid, email: null, displayName: null } : null),
}));
vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

const {
  warningTitleFor,
  publishedWarningsFor,
  fetchPublishedWarnings,
  resetPublishedWarnings,
  noteDeleteAffordance,
  renderEbookNotes,
  PUBLISHED_WARNINGS_URLS,
} = await import('../ebook-notes.js');
const { addUserWarning, getUserWarnings } = await import('../user-warnings.js');

const fakeDb = {};
const jane = { displayName: 'Jane Doe' };
const bob = { displayName: 'Bob Brown' };

// The real divergence this feature exists for: the audiobook catalog spells
// the book one way, the epub another.
const AUDIO_TITLE = 'Moonfall - Beneath the Dragoneye Moons, Book 13';
const bookWithSibling = {
  title: 'Moonfall',
  audiobook_title: AUDIO_TITLE,
  beside_audiobook: 'Selkie Myrtle',
};
const bookBesideButUnmatched = {
  title: 'Tamer: King of Dinosaurs Book 10',
  audiobook_title: null,
  beside_audiobook: 'Michael-Scott Earle',
};
const bookEbookOnly = { title: 'Dragonsteel Prime', audiobook_title: null, beside_audiobook: null };

beforeEach(() => {
  mockStore = {};
  liveUid = 'uid-jane';
  resetPublishedWarnings();
});

// --------------------------------------------------------------------------
describe('warningTitleFor — the three keying classes', () => {
  it('keys a sibling-matched ebook by the AUDIOBOOK catalog title, not its own', () => {
    const k = warningTitleFor(bookWithSibling);
    expect(k.title).toBe(AUDIO_TITLE);
    expect(k.keying).toBe('audiobook');
    // ⚠️ The assertion that matters: the ebook's own spelling would produce a
    // DIFFERENT key, and a note filed under it is invisible to every other
    // surface in the estate.
    expect(k.bookId).toBe('moonfall-beneath-the-dragoneye-moons-book-13');
    expect(k.bookId).not.toBe('moonfall');
  });

  it('keys a beside-but-unmatched ebook by its own title, and says which class', () => {
    const k = warningTitleFor(bookBesideButUnmatched);
    expect(k.title).toBe('Tamer: King of Dinosaurs Book 10');
    expect(k.keying).toBe('beside');
    expect(k.audiobookTitle).toBeNull();
  });

  it('keys an ebook with no audiobook sibling by its own title — this catalog’s spelling', () => {
    const k = warningTitleFor(bookEbookOnly);
    expect(k.title).toBe('Dragonsteel Prime');
    expect(k.keying).toBe('ebook-only');
    expect(k.bookId).toBe('dragonsteel-prime');
  });

  it('treats a blank or whitespace audiobook_title as absent, never as the key', () => {
    expect(warningTitleFor({ title: 'A Book', audiobook_title: '   ' }).title).toBe('A Book');
    expect(warningTitleFor({ title: 'A Book', audiobook_title: '' }).keying).toBe('ebook-only');
  });

  it('survives a row with nothing usable rather than keying on "undefined"', () => {
    const k = warningTitleFor({});
    expect(k.title).toBe('');
    expect(k.bookId).toBe('');   // never the slug of an empty/undefined title
    expect(() => warningTitleFor(null)).not.toThrow();
  });
});

// --------------------------------------------------------------------------
describe('publishedWarningsFor — checked-and-clean is not never-checked', () => {
  const map = {
    'defiant': { warnings: [{ label: 'War', source_url: 'https://x/1' }] },
    'all the skills': { warnings: [] },
  };

  it('lists what the sources found', () => {
    const r = publishedWarningsFor(map, 'Defiant');
    expect(r.state).toBe('listed');
    expect(r.warnings).toHaveLength(1);
  });

  it('distinguishes an EMPTY array (checked, none listed) from a MISSING entry', () => {
    expect(publishedWarningsFor(map, 'All The Skills').state).toBe('clean');
    expect(publishedWarningsFor(map, 'Never Looked At').state).toBe('unknown');
  });

  it('a null map (the file could not be read) is unknown, never clean', () => {
    expect(publishedWarningsFor(null, 'Defiant').state).toBe('unknown');
  });
});

// --------------------------------------------------------------------------
describe('fetchPublishedWarnings', () => {
  it('tries the relative copy first, then the public audiobook-site copy', async () => {
    const seen = [];
    const f = async (url) => {
      seen.push(url);
      if (url === PUBLISHED_WARNINGS_URLS[0]) return { ok: false, status: 404 };
      return { ok: true, json: async () => ({ Defiant: { warnings: [] } }) };
    };
    const map = await fetchPublishedWarnings(f);
    expect(seen).toEqual(PUBLISHED_WARNINGS_URLS);
    expect(map).toHaveProperty('defiant');   // indexed lower-cased
  });

  it('a 200 that is not JSON (a door serving its shell) falls through, not through as empty', async () => {
    const f = async (url) => (url === PUBLISHED_WARNINGS_URLS[0]
      ? { ok: true, json: async () => { throw new SyntaxError('<!DOCTYPE html>'); } }
      : { ok: true, json: async () => ({ Defiant: { warnings: [{ label: 'War' }] } }) });
    const map = await fetchPublishedWarnings(f);
    expect(publishedWarningsFor(map, 'Defiant').state).toBe('listed');
  });

  it('answers null when every URL fails — losing the extra must not throw', async () => {
    const f = async () => { throw new TypeError('network'); };
    await expect(fetchPublishedWarnings(f)).resolves.toBeNull();
  });

  it('fetches at most once per page — the promise is cached', async () => {
    const f = vi.fn(async () => ({ ok: true, json: async () => ({}) }));
    await Promise.all([fetchPublishedWarnings(f), fetchPublishedWarnings(f)]);
    await fetchPublishedWarnings(f);
    expect(f).toHaveBeenCalledTimes(1);
  });
});

// --------------------------------------------------------------------------
describe('noteDeleteAffordance — offered matches what the rules allow', () => {
  it('offers your own note, always', () => {
    const v = noteDeleteAffordance({ displayName: 'jane doe' }, jane, false);
    expect(v.show).toBe(true);
    expect(v.asModerator).toBe(false);
  });

  it('offers somebody else’s note only to a moderator, and marks it as such', () => {
    expect(noteDeleteAffordance({ displayName: 'Bob Brown' }, jane, false).show).toBe(false);
    const asMod = noteDeleteAffordance({ displayName: 'Bob Brown' }, jane, true);
    expect(asMod.show).toBe(true);
    expect(asMod.asModerator).toBe(true);
  });

  it('offers nothing when there is no session at all', () => {
    expect(noteDeleteAffordance({ displayName: 'Bob Brown' }, null, false).show).toBe(false);
  });
});

// --------------------------------------------------------------------------
describe('renderEbookNotes', () => {
  function slot() {
    const el = document.createElement('div');
    document.body.appendChild(el);
    return el;
  }
  const noPublished = async () => { throw new TypeError('offline'); };

  it('reads and writes under the AUDIOBOOK title for a sibling-matched ebook', async () => {
    // Seeded the way the book modal on audiobooks.heygabi.ai would have.
    await addUserWarning(fakeDb, AUDIO_TITLE, 'Animal death', bob);
    const el = slot();
    await renderEbookNotes(fakeDb, el, bookWithSibling, { session: jane });
    await new Promise((r) => setTimeout(r, 0));

    expect(el.textContent).toContain('Animal death');
    expect(el.textContent).toContain('added by Bob Brown');
    // ...and the provenance line names the title it looked under.
    expect(el.querySelector('.eb-notes-prov').textContent).toContain(AUDIO_TITLE);
  });

  it('does NOT find a note filed under the ebook’s own title — the silent-silo case', async () => {
    await addUserWarning(fakeDb, 'Moonfall', 'Wrong key', bob);
    const el = slot();
    await renderEbookNotes(fakeDb, el, bookWithSibling, { session: jane });
    await new Promise((r) => setTimeout(r, 0));
    expect(el.textContent).not.toContain('Wrong key');
  });

  it('adding a note from the shelf files it where the audiobook site reads', async () => {
    const el = slot();
    vi.spyOn(window, 'prompt').mockReturnValue('Gore');
    await renderEbookNotes(fakeDb, el, bookWithSibling, { session: jane });
    await new Promise((r) => setTimeout(r, 0));
    el.querySelector('.eb-notes-add').click();
    await new Promise((r) => setTimeout(r, 0));

    const fromTheAudiobookSide = await getUserWarnings(fakeDb, AUDIO_TITLE);
    expect(fromTheAudiobookSide.map((w) => w.label)).toContain('Gore');
    window.prompt.mockRestore();
  });

  it('is idempotent — re-rendering never double-posts', async () => {
    await addUserWarning(fakeDb, AUDIO_TITLE, 'Gore', bob);
    const el = slot();
    await renderEbookNotes(fakeDb, el, bookWithSibling, { session: jane });
    await renderEbookNotes(fakeDb, el, bookWithSibling, { session: jane });
    await new Promise((r) => setTimeout(r, 0));
    expect(el.querySelectorAll('.eb-notes-list li').length).toBe(1);
  });

  it('draws the ✕ on your own note and not on somebody else’s', async () => {
    await addUserWarning(fakeDb, AUDIO_TITLE, 'Mine', jane);
    await addUserWarning(fakeDb, AUDIO_TITLE, 'Theirs', bob);
    const el = slot();
    await renderEbookNotes(fakeDb, el, bookWithSibling, { session: jane });
    await new Promise((r) => setTimeout(r, 0));
    const rows = [...el.querySelectorAll('.eb-notes-list li')];
    const mine = rows.find((li) => li.textContent.includes('Mine'));
    const theirs = rows.find((li) => li.textContent.includes('Theirs'));
    expect(mine.querySelector('.eb-notes-x')).not.toBeNull();
    expect(theirs.querySelector('.eb-notes-x')).toBeNull();
  });

  it('draws it on everyone’s note for a moderator, asked at CLICK time', async () => {
    await addUserWarning(fakeDb, AUDIO_TITLE, 'Theirs', bob);
    const el = slot();
    let moderator = false;
    // The role answer settles AFTER the card opened — the real ordering.
    const done = renderEbookNotes(fakeDb, el, bookWithSibling, {
      session: jane, canModerate: () => moderator,
    });
    moderator = true;
    await done;
    await new Promise((r) => setTimeout(r, 0));
    expect(el.querySelector('.eb-notes-x')).not.toBeNull();
  });

  it('says a file with no title cannot be filed, rather than showing an empty block', async () => {
    const el = slot();
    await renderEbookNotes(fakeDb, el, { title: '' }, { session: jane });
    expect(el.querySelector('.eb-notes-empty')).not.toBeNull();
    expect(el.querySelector('.eb-notes-add')).toBeNull();
  });

  it('names the beside-but-unmatched class in words rather than implying a match', async () => {
    const el = slot();
    await renderEbookNotes(fakeDb, el, bookBesideButUnmatched, { session: jane });
    await new Promise((r) => setTimeout(r, 0));
    expect(el.querySelector('.eb-notes-prov').textContent).toContain('not matched');
  });

  it('an unreachable published file leaves the reader notes standing', async () => {
    await addUserWarning(fakeDb, 'Dragonsteel Prime', 'Spiders', bob);
    const el = slot();
    await fetchPublishedWarnings(noPublished);  // prime the cache with a failure
    await renderEbookNotes(fakeDb, el, bookEbookOnly, { session: jane });
    await new Promise((r) => setTimeout(r, 0));
    expect(el.textContent).toContain('Spiders');
    expect(el.textContent).toContain('could not be reached');
  });
});
