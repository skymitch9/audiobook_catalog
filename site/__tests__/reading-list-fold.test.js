// ONE BOOK, ONE COUNT — `readingListFoldKey` / `foldReadingList` (reviews.js).
//
// Owner, 2026-08-26, verbatim:
//
//   "for the tbr list, it's double counting if something is owned in multiple
//    media sources. So if a book is audio, physical and ebook or any
//    combination we need to have it single count with a link to all formats."
//
// A `readingLists` document id is `{uid}_{bookId}` and `bookId` is a slug of
// the title AS THAT CATALOGUE SPELLS IT, so one intention becomes two
// documents and every counter counted it twice.
//
// ⚠️ Two surfaces on this site count that collection — `community.html`'s
// per-person TBR stat and `index.html`'s "Reading lists" filter — and they now
// share ONE fold. The two tests that earn this file are:
//
//   * the fold that FIXES the report (two spellings, one `workKey`);
//   * ⚠️ the fold that must NOT happen. A fold that is too eager is silent and
//     permanent — a book vanishes from somebody's list and nothing anywhere
//     says so — while a fold that is too shy leaves a count slightly high,
//     which is visible and reportable.
import { describe, it, expect, vi } from 'vitest';

// reviews.js fires the shadow reporter from its gated write paths on import;
// mock it so no test touches the network. Its contract lives in its own file.
vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

import { foldReadingList, readingListFoldKey } from '../reviews.js';

/** A Firestore snapshot, as `getDocs(...).docs` hands them over. */
function snap(id, fields) {
  return { id, data: () => fields };
}

const FIREFIGHT_KEY = 'firefight|brandon sanderson';

describe('readingListFoldKey', () => {
  it('prefers the cross-catalogue workKey', () => {
    expect(readingListFoldKey({ workKey: FIREFIGHT_KEY, bookId: 'firefight' }, 'u_firefight'))
      .toBe('work:' + FIREFIGHT_KEY);
  });

  it('⚠️ a key with no "|" is NOT one of ours and is refused', () => {
    // `workKeyFor` always joins a folded title to a folded author. A bare title
    // would collide two books called "Gold" — the library catalogue's
    // `myTbrEntries` states the same rule, and the two must agree.
    expect(readingListFoldKey({ workKey: 'gold', bookId: 'gold' }, 'u_gold')).toBe('book:gold');
  });

  it('falls back to the document own bookId', () => {
    expect(readingListFoldKey({ bookId: 'the-court-of-the-dead' }, 'u_court'))
      .toBe('book:the-court-of-the-dead');
  });

  it('⚠️ a document with NEITHER key is its own group, never everyone else’s', () => {
    // Otherwise every fieldless document in the collection would collapse into
    // one entry and quietly delete itself from somebody's count.
    expect(readingListFoldKey({}, 'u_a')).toBe('doc:u_a');
    expect(readingListFoldKey({}, 'u_a')).not.toBe(readingListFoldKey({}, 'u_b'));
  });

  it('tolerates a missing document entirely', () => {
    expect(readingListFoldKey(null, 'u_a')).toBe('doc:u_a');
    expect(readingListFoldKey(undefined, undefined)).toBe('doc:');
  });
});

describe('foldReadingList — the reported bug', () => {
  it('⚠️ TWO SPELLINGS, ONE workKey → ONE entry (the double count)', () => {
    // The library wrote both: the paperback under its own spelling, and a
    // second entry under the audiobook packaging. Same book, same key.
    const groups = foldReadingList([
      snap('uid_firefight', {
        bookId: 'firefight',
        bookTitle: 'Firefight',
        workKey: FIREFIGHT_KEY,
        status: 'tbr',
      }),
      snap('uid_firefight-the-reckoners-book-2', {
        bookId: 'firefight-the-reckoners-book-2',
        bookTitle: 'Firefight - The Reckoners, Book 2',
        workKey: FIREFIGHT_KEY,
        status: 'tbr',
      }),
    ]);

    expect(groups).toHaveLength(1);
    // ⚠️ BOTH spellings are kept. The filter has to match a catalogue row under
    // either of them — folding the MATCH set would hide the book rather than
    // stop repeating it. Only the COUNT is one per group.
    expect(groups[0].titles).toEqual(['Firefight', 'Firefight - The Reckoners, Book 2']);
    // ⚠️ And every document id, so a removal can take them all.
    expect(groups[0].docIds).toEqual([
      'uid_firefight',
      'uid_firefight-the-reckoners-book-2',
    ]);
  });

  it('⚠️ two DIFFERENT books do NOT fold, whatever they are called', () => {
    const groups = foldReadingList([
      snap('uid_gold-1', { bookId: 'gold', bookTitle: 'Gold' }),
      snap('uid_gold-2', { bookId: 'gold-a-novel', bookTitle: 'Gold' }),
    ]);
    expect(groups).toHaveLength(2);
  });

  it('⚠️ two different works never share a key even under one title', () => {
    const groups = foldReadingList([
      snap('uid_a', { bookId: 'gold', bookTitle: 'Gold', workKey: 'gold|isaac asimov' }),
      snap('uid_b', { bookId: 'gold-2', bookTitle: 'Gold', workKey: 'gold|chris cleave' }),
    ]);
    expect(groups).toHaveLength(2);
  });

  it('⚠️ THE RESIDUAL, pinned so nobody thinks it is fixed', () => {
    // This is the case this site CANNOT fold: the library wrote one entry (with
    // a workKey) and this site wrote the other (with none, because it has no
    // author to build one from). They are the same book and they still count as
    // two here — the bridge that joins them is `audiobook_holding`, a D1 table
    // in the library's own database that this page cannot see.
    //
    // ⚠️ Asserted as TWO on purpose. If somebody later makes this one, they
    // will have either added a workKey to this side's writes or published a
    // bridge — and this test is where they must come and say which.
    const groups = foldReadingList([
      snap('uid_firefight', {
        bookId: 'firefight',
        bookTitle: 'Firefight',
        workKey: FIREFIGHT_KEY,
      }),
      snap('uid_firefight-the-reckoners-book-2', {
        bookId: 'firefight-the-reckoners-book-2',
        bookTitle: 'Firefight - The Reckoners, Book 2',
      }),
    ]);
    expect(groups).toHaveLength(2);
  });
});

describe('foldReadingList — shapes and edges', () => {
  it('accepts plain objects as well as Firestore snapshots', () => {
    const groups = foldReadingList([
      { id: 'uid_a', bookId: 'firefight', bookTitle: 'Firefight', workKey: FIREFIGHT_KEY },
      { id: 'uid_b', bookId: 'firefight-b2', bookTitle: 'Firefight - Book 2', workKey: FIREFIGHT_KEY },
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].docIds).toEqual(['uid_a', 'uid_b']);
  });

  it('a repeated title inside one group is listed once', () => {
    const groups = foldReadingList([
      snap('uid_a', { bookId: 'a', bookTitle: 'Firefight', workKey: FIREFIGHT_KEY }),
      snap('uid_b', { bookId: 'b', bookTitle: 'Firefight', workKey: FIREFIGHT_KEY }),
    ]);
    expect(groups[0].titles).toEqual(['Firefight']);
  });

  it('a titleless document still counts as a book', () => {
    // The count must not depend on a display field being present.
    const groups = foldReadingList([snap('uid_a', { bookId: 'firefight' })]);
    expect(groups).toHaveLength(1);
    expect(groups[0].titles).toEqual([]);
  });

  it('an empty list folds to nothing, and so does nothing at all', () => {
    expect(foldReadingList([])).toEqual([]);
    expect(foldReadingList(null)).toEqual([]);
    expect(foldReadingList(undefined)).toEqual([]);
  });

  it('groups come back in first-seen order', () => {
    const groups = foldReadingList([
      snap('uid_b', { bookId: 'b', bookTitle: 'Beta' }),
      snap('uid_a', { bookId: 'a', bookTitle: 'Alpha' }),
    ]);
    expect(groups.map((g) => g.titles[0])).toEqual(['Beta', 'Alpha']);
  });
});
