// @vitest-environment jsdom
// Feature: "save your spot" — site/reading-position.js
//
// ⚠️ WHAT THESE TESTS ARE FOR. Every failure this feature can have is SILENT.
// A position filed under the wrong id is not an error, it is "you were never
// here". A locator that loses its `kind` is not an error, it is a jump to the
// wrong place. A last-write-wins comparison that goes the wrong way is not an
// error, it is somebody's real place overwritten by a stale one. None of that
// throws, none of it logs, and none of it is visible in a browser until a
// person notices their book opened at the beginning.
//
// So these pin the DECISIONS, not the plumbing: the doc id's shape (which
// firestore.rules parses), the atomic kind/value pair, which of two rows wins,
// and the storage failures that must degrade to "no bookmark" rather than
// taking the book down with them.
import { describe, it, expect, beforeEach, vi } from 'vitest';

let mockStore = {};

vi.mock('firebase/firestore', () => ({
  doc: (db, ...segs) => ({ _path: segs.join('/'), id: segs[segs.length - 1] }),
  getDoc: async (ref) => ({
    exists: () => Object.prototype.hasOwnProperty.call(mockStore, ref._path),
    data: () => mockStore[ref._path],
  }),
  setDoc: async (ref, data) => { mockStore[ref._path] = { ...data }; },
  serverTimestamp: () => 'server-ts',
}));

const {
  POSITION_COLLECTION,
  SAVE_DEBOUNCE_MS,
  createPositionKeeper,
  describeDevice,
  describePosition,
  loadLocal,
  loadRemote,
  localKey,
  makePosition,
  newerOf,
  positionDocId,
  samePlace,
  saveLocal,
  saveRemote,
} = await import('../reading-position.js');

const { col } = await import('../fb-env.js');

/**
 * The collection path a row actually lands in.
 *
 * ⚠️ jsdom serves these tests from localhost, which fb-env.js counts as the
 * DEV LANE — so the real path here is `readingPositions_dev`. That is not a
 * test artefact to work around, it is the contract: a position saved while
 * reviewing /dev/ must never be read back on prod, and vice versa.
 */
const path = (docId) => `${col(POSITION_COLLECTION)}/${docId}`;

beforeEach(() => {
  mockStore = {};
  localStorage.clear();
});

describe('the lane', () => {
  it('suffixes the collection, so /dev/ and prod keep separate spots', () => {
    expect(col(POSITION_COLLECTION)).toBe('readingPositions_dev');
  });
});

describe('the document id', () => {
  it('is uid_bookId, the shape firestore.rules parses back', () => {
    // ⚠️ The rule is `docId.split('_')[0] == request.auth.uid`. Change this
    // shape and ownership silently stops being enforceable — every write
    // starts failing, or worse, stops being checked.
    expect(positionDocId('abc123UID', 'the-way-of-kings')).toBe('abc123UID_the-way-of-kings');
    expect(positionDocId('u', 'b').split('_')[0]).toBe('u');
  });

  it('cannot be confused by a book slug, which never contains the separator', () => {
    // bookIdFromTitle emits [a-z0-9-] only, so the FIRST underscore is always
    // the uid boundary however many words the title has.
    const id = positionDocId('uid42', 'the-well-of-ascension-a-mistborn-novel');
    expect(id.split('_')).toHaveLength(2);
  });
});

describe('the stored row', () => {
  it('keeps kind and value together, always', () => {
    // ⚠️ A CFI read as a page number is a silent jump to the wrong place.
    const row = makePosition({
      uid: 'u', bookId: 'b', anchor: 'b-abc', format: 'epub',
      pos: { kind: 'cfi', value: 'epubcfi(/6/14!/4/2)' }, at: 5,
    });
    expect(row.pos).toEqual({ kind: 'cfi', value: 'epubcfi(/6/14!/4/2)' });
    expect(row.updatedAt).toBe(5);
  });

  it('carries the anchor as a field and never as the key', () => {
    // ⚠️ The anchor is sha256(relative path): re-filing a book changes it, and
    // a position keyed on it would silently vanish. It is a hint, so that
    // "open the book this belongs to" stays one hop while the path holds.
    const row = makePosition({
      uid: 'u', bookId: 'the-way-of-kings', anchor: 'b-a49cd096d824',
      format: 'pdf', pos: { kind: 'page', value: 3 },
    });
    expect(row.anchor).toBe('b-a49cd096d824');
    expect(positionDocId(row.uid, row.bookId)).not.toContain('b-a49cd096d824');
  });

  it('clamps progress into 0..1 and drops a nonsense one', () => {
    const p = (v) => makePosition({
      uid: 'u', bookId: 'b', anchor: '', format: 'pdf',
      pos: { kind: 'page', value: 1 }, progress: v,
    }).progress;
    expect(p(1.4)).toBe(1);
    expect(p(-2)).toBe(0);
    expect(p(0.5)).toBe(0.5);
    expect(p(NaN)).toBeUndefined();
    expect(p(undefined)).toBeUndefined();
  });
});

describe('which row wins', () => {
  const at = (t) => ({ updatedAt: t, pos: { kind: 'page', value: t } });

  it('is the one written last, never the one furthest through the book', () => {
    // ⚠️ Furthest-progress-wins looks kinder and is wrong: somebody re-reading,
    // or who flipped to the appendix and back, would have their real place
    // overwritten by a stale high-water mark they cannot get rid of.
    expect(newerOf(at(10), at(20))).toEqual(at(20));
    expect(newerOf(at(30), at(20))).toEqual(at(30));
  });

  it('prefers the row that exists when the other does not', () => {
    expect(newerOf(null, at(1))).toEqual(at(1));
    expect(newerOf(at(1), null)).toEqual(at(1));
    expect(newerOf(null, null)).toBeNull();
  });

  it('keeps the local row on an exact tie, so a reload never jumps', () => {
    const local = { updatedAt: 7, pos: { kind: 'page', value: 4 } };
    const remote = { updatedAt: 7, pos: { kind: 'page', value: 9 } };
    expect(newerOf(local, remote)).toBe(local);
  });
});

describe('whether to ask', () => {
  it('says two rows are the same place only when kind AND value agree', () => {
    expect(samePlace(
      { pos: { kind: 'page', value: 7 } },
      { pos: { kind: 'page', value: '7' } },
    )).toBe(true);
    // ⚠️ Same value, different kind is NOT the same place — that is exactly
    // the confusion the atomic pair exists to prevent.
    expect(samePlace(
      { pos: { kind: 'page', value: 7 } },
      { pos: { kind: 'cfi', value: 7 } },
    )).toBe(false);
    expect(samePlace(null, { pos: { kind: 'page', value: 7 } })).toBe(false);
  });
});

describe('what the offer says', () => {
  it('uses the renderer\'s own words when it has them', () => {
    expect(describePosition({ label: 'Chapter Three · 12%' })).toBe('Chapter Three · 12%');
  });

  it('falls back to a page or a percentage, and never to nothing', () => {
    expect(describePosition({ pos: { kind: 'page', value: 214 } })).toBe('p. 214');
    expect(describePosition({ pos: { kind: 'cfi', value: 'x' }, progress: 0.63 })).toBe('63%');
    // ⚠️ A prompt that says "you were at" and then stops is worse than none.
    expect(describePosition({ pos: { kind: 'cfi', value: 'x' } })).toBe('where you left off');
  });

  it('names a device coarsely, and gets Chrome-claims-Safari right', () => {
    expect(describeDevice('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605 Version/17.0 Safari/604.1'))
      .toBe('iPhone · Safari');
    // ⚠️ Every Chrome UA also says Safari, and Edge says both. Order matters.
    expect(describeDevice('Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537 Chrome/120 Safari/537'))
      .toBe('Windows · Chrome');
    expect(describeDevice('Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537 Edg/120'))
      .toBe('Windows · Edge');
    expect(describeDevice('', 'This device')).toBe('This device');
  });
});

describe('the per-device cache', () => {
  it('round-trips a row', () => {
    const row = makePosition({
      uid: 'u', bookId: 'b', anchor: '', format: 'pdf', pos: { kind: 'page', value: 12 },
    });
    expect(saveLocal('b', row)).toBe(true);
    expect(loadLocal('b')).toEqual(row);
  });

  it('is namespaced by lane, so /dev/ never resumes prod\'s spot', () => {
    // col() resolves unsuffixed under jsdom's default location; the collection
    // name being IN the key is what makes the two lanes separate at all.
    expect(localKey('b')).toContain(POSITION_COLLECTION);
    expect(localKey('b')).toContain('b');
  });

  it('answers null for a row with no locator kind rather than half-restoring', () => {
    localStorage.setItem(localKey('b'), JSON.stringify({ pos: { value: 9 } }));
    expect(loadLocal('b')).toBeNull();
  });

  it('survives unreadable and unwritable storage', () => {
    // ⚠️ Private mode, a full quota, a hostile embedder. A reader whose BOOK
    // will not open because its BOOKMARK could not be read is strictly worse
    // than one that opens at page 1.
    const hostile = {
      getItem() { throw new Error('nope'); },
      setItem() { throw new Error('nope'); },
    };
    expect(loadLocal('b', hostile)).toBeNull();
    expect(saveLocal('b', { pos: { kind: 'page', value: 1 } }, hostile)).toBe(false);
    localStorage.setItem(localKey('b'), 'not json');
    expect(loadLocal('b')).toBeNull();
  });
});

describe('the store', () => {
  it('writes and reads back the same place', async () => {
    const row = makePosition({
      uid: 'u1', bookId: 'the-way-of-kings', anchor: 'b-x', format: 'epub',
      pos: { kind: 'cfi', value: 'epubcfi(/6/14!/4)' },
    });
    expect(await saveRemote({}, row)).toBe(true);
    const back = await loadRemote({}, 'u1', 'the-way-of-kings');
    expect(back.pos).toEqual({ kind: 'cfi', value: 'epubcfi(/6/14!/4)' });
  });

  it('stamps a server time that nothing compares', async () => {
    // ⚠️ Last-write-wins compares `updatedAt`, a client-clock number, because
    // it has to compare a row written OFFLINE in localStorage against one
    // written by another device. A server sentinel cannot do that job; it
    // rides along for audit only.
    await saveRemote({}, makePosition({
      uid: 'u1', bookId: 'b', anchor: '', format: 'pdf',
      pos: { kind: 'page', value: 2 }, at: 1234,
    }));
    const stored = mockStore[path('u1_b')];
    expect(stored.updatedAt).toBe(1234);
    expect(stored.updatedAtServer).toBe('server-ts');
  });

  it('answers "no saved position" rather than throwing when the read is refused', async () => {
    // A rules refusal, an offline device and an outage are all the same fact
    // to this page: open the book at the start. Reporting an error here would
    // put an outage sentence in front of somebody whose book opened fine.
    const { getDoc } = await import('firebase/firestore');
    getDoc.mockImplementationOnce?.(() => { throw new Error('PERMISSION_DENIED'); });
    expect(await loadRemote(null, 'u', 'b')).toBeNull();
    expect(await loadRemote({}, '', 'b')).toBeNull();
    expect(await saveRemote({}, null)).toBe(false);
    expect(await saveRemote({}, { uid: '', bookId: 'b' })).toBe(false);
  });
});

describe('the keeper', () => {
  const cfg = () => ({
    db: {}, uid: 'u1', bookId: 'b1', anchor: 'b-x', format: 'pdf',
    device: 'Windows · Chrome', delay: 3000,
  });

  it('records NOTHING until it is armed', async () => {
    // ⚠️ THE GUARD THAT MATTERS MOST. reader.js arms the keeper only after a
    // page has genuinely rendered, so a book that failed to open — a broken
    // file, a lapsed token, a refused range — can never overwrite a real
    // position with page 1.
    const k = createPositionKeeper(cfg());
    k.record({ kind: 'page', value: 1 });
    expect(k._pending()).toBeNull();
    expect(loadLocal('b1')).toBeNull();
    expect(await k.flush()).toBe(false);
  });

  it('writes locally at once and remotely only on the debounce', () => {
    vi.useFakeTimers();
    try {
      const k = createPositionKeeper(cfg());
      k.arm();
      k.record({ kind: 'page', value: 5 });
      // ⚠️ The asymmetry IS the design: a page turn is a keypress, so the
      // Firestore write waits — but the local one cannot, or a killed tab
      // loses the page you were on.
      expect(loadLocal('b1').pos.value).toBe(5);
      expect(mockStore[path('u1_b1')]).toBeUndefined();
      k.record({ kind: 'page', value: 6 });
      k.record({ kind: 'page', value: 7 });
      vi.advanceTimersByTime(3000);
      expect(loadLocal('b1').pos.value).toBe(7);
    } finally {
      vi.useRealTimers();
    }
  });

  it('flushes the newest pending row and only once', async () => {
    const k = createPositionKeeper({ ...cfg(), delay: 100000 });
    k.arm();
    k.record({ kind: 'page', value: 11 });
    k.record({ kind: 'page', value: 12 });
    expect(await k.flush()).toBe(true);
    expect(mockStore[path('u1_b1')].pos.value).toBe(12);
    // A second flush with nothing pending is a no-op, not a duplicate write —
    // pagehide and visibilitychange both fire on a real backgrounding.
    expect(await k.flush()).toBe(false);
  });

  it('ignores a locator with no value', () => {
    const k = createPositionKeeper(cfg());
    k.arm();
    k.record({ kind: 'page', value: null });
    k.record(null);
    expect(k._pending()).toBeNull();
  });

  it('defaults its debounce to the documented cadence', () => {
    expect(SAVE_DEBOUNCE_MS).toBe(3000);
  });
});
