// WHAT KIND OF BOOK IS THIS — `readingListMediaTags` / `readingListMediaCounts`.
//
// Owner, 2026-08-26, on his phone:
//
//   "in the tbr list, not all have sync'd -- can we audit Diva's; also I don't
//    see the tag for what type of media a book is."
//
// The first half was audited and measured clean (the library repo's
// `docs/info/tbr.md` section 10: 53 of Samantha's 358 entries name books her
// catalogue has no row for, and zero of them is a rung that should have fired).
// This file is the second half.
//
// ⚠️ THE TAG IS PROVENANCE, and that is the strongest thing this site has. A
// `readingLists` document says nothing about media; what it says is WHICH
// CATALOGUE wrote it, and each catalogue only offers its button on its own
// books. A composite `workKey` is written by exactly one thing in this estate
// (`tbrDocFor` in the library's @lc/core), so it means the library shelves;
// anything else was written here, from the audiobook modal or a club page.
//
// ⚠️ THE THREE TESTS THAT EARN THE FILE ARE ALL REFUSALS:
//
//   * a `workKey` with no '|' is NOT a library stamp — the same guard
//     `readingListFoldKey` applies, and for the same reason: a bare title
//     collides two books called "Gold";
//   * an empty group draws NO chips, because a chip that is always there says
//     nothing;
//   * ⚠️ there is NO ebook tag, and the test asserts its absence on purpose.
//     The ebook shelf is permission-gated by owner directive; publishing a
//     title list to power a chip is access-increasing, which is the owner's
//     call and not a side effect of a chip. If a future session adds one, this
//     test fails and makes them read KI-7 first. That is the whole point.
import { describe, it, expect, vi } from 'vitest';

vi.mock('../gate-shadow.js', () => ({ reportGate: vi.fn() }));

import {
  foldReadingList,
  readingListMediaCounts,
  readingListMediaTags,
  READING_LIST_MEDIA,
} from '../reviews.js';

/** A Firestore snapshot, as `getDocs(...).docs` hands them over. */
function snap(id, fields) {
  return { id, data: () => fields };
}

const FIREFIGHT_KEY = 'firefight|brandon sanderson';

describe('readingListMediaTags', () => {
  it('a composite workKey means the LIBRARY shelves', () => {
    const tags = readingListMediaTags([{ workKey: FIREFIGHT_KEY, bookId: 'firefight' }]);
    expect(tags).toEqual([READING_LIST_MEDIA.library]);
    expect(tags[0].emoji).toBe('📕');
  });

  it('no workKey means it was written HERE — an audiobook', () => {
    const tags = readingListMediaTags([{ bookId: 'firefight-the-reckoners-book-2' }]);
    expect(tags).toEqual([READING_LIST_MEDIA.audiobook]);
    expect(tags[0].emoji).toBe('🎧');
  });

  // ⚠️ The function's contract, exercised directly. Whether the FOLD on this
  // site can currently produce such a group is a separate question with a
  // separate answer — KI-7, pinned at the bottom of this file. The tag
  // derivation must be right either way, so that closing KI-7 needs no change
  // here.
  it('a group in BOTH media carries BOTH tags — that is the whole point', () => {
    const tags = readingListMediaTags([
      { workKey: FIREFIGHT_KEY, bookId: 'firefight' },
      { bookId: 'firefight-the-reckoners-book-2' },
    ]);
    expect(tags).toEqual([READING_LIST_MEDIA.library, READING_LIST_MEDIA.audiobook]);
  });

  it('the order is FIXED, whichever document arrived first', () => {
    // Two groups drawing the same pair of chips in two orders reads as two
    // different answers. The fold's own order is arrival order and is not
    // stable across a re-fetch, so the tag order must not inherit it.
    const tags = readingListMediaTags([
      { bookId: 'firefight-the-reckoners-book-2' },
      { workKey: FIREFIGHT_KEY, bookId: 'firefight' },
    ]);
    expect(tags.map((t) => t.emoji)).toEqual(['📕', '🎧']);
  });

  it('⚠️ a workKey with no "|" is NOT a library stamp', () => {
    // `workKeyFor` always joins a folded title to a folded author. A bare
    // title is not one of ours — same guard, same reason, as the fold key.
    expect(readingListMediaTags([{ workKey: 'firefight', bookId: 'firefight' }]))
      .toEqual([READING_LIST_MEDIA.audiobook]);
    expect(readingListMediaTags([{ workKey: '   ', bookId: 'firefight' }]))
      .toEqual([READING_LIST_MEDIA.audiobook]);
  });

  it('⚠️ draws NOTHING for an empty or absent group', () => {
    expect(readingListMediaTags([])).toEqual([]);
    expect(readingListMediaTags(null)).toEqual([]);
    expect(readingListMediaTags(undefined)).toEqual([]);
    expect(readingListMediaTags([null, undefined])).toEqual([]);
  });

  it('🔴 THERE IS NO EBOOK TAG, and its absence is asserted on purpose', () => {
    // `site/ebooks.json` is gitignored AND left the deployment (owner,
    // 2026-08-17: "I don't want people scraping my books"); the manifest is
    // served only behind a Firebase token and the estate's `ebooks` grant.
    // A chip on a public page needs a public title list, which would be
    // ACCESS-INCREASING — the owner's call, not a chip's side effect.
    // KI-7 records what still needs the D1 bridge.
    expect(READING_LIST_MEDIA.ebook).toBeUndefined();
    const every = readingListMediaTags([
      { workKey: FIREFIGHT_KEY, bookId: 'firefight' },
      { bookId: 'firefight-the-reckoners-book-2' },
    ]);
    expect(every.map((t) => t.emoji)).not.toContain('📖');
  });
});

describe('foldReadingList carries the group’s documents', () => {
  it('exposes `docs` so the tags can see every document in the group', () => {
    const groups = foldReadingList([
      snap('u_firefight', { workKey: FIREFIGHT_KEY, bookId: 'firefight', bookTitle: 'Firefight' }),
      snap('u_firefight-the-reckoners-book-2', {
        workKey: FIREFIGHT_KEY,
        bookId: 'firefight-the-reckoners-book-2',
        bookTitle: 'Firefight - The Reckoners, Book 2',
      }),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].docs).toHaveLength(2);
    // ⚠️ And the existing shape is untouched — `titles` still keeps EVERY
    // spelling, because a caller filtering a catalogue must match on any of
    // them. Only the COUNT is one per group.
    expect(groups[0].titles).toEqual(['Firefight', 'Firefight - The Reckoners, Book 2']);
    expect(groups[0].docIds).toHaveLength(2);
  });
});

describe('readingListMediaCounts', () => {
  it('counts folded BOOKS, never documents', () => {
    // One book, two documents, one library tag — not two.
    const groups = foldReadingList([
      snap('u_firefight', { workKey: FIREFIGHT_KEY, bookId: 'firefight', bookTitle: 'Firefight' }),
      snap('u_ff2', {
        workKey: FIREFIGHT_KEY,
        bookId: 'firefight-the-reckoners-book-2',
        bookTitle: 'Firefight - The Reckoners, Book 2',
      }),
    ]);
    expect(readingListMediaCounts(groups)).toEqual({ library: 1, audiobook: 0 });
  });

  it('⚠️ the two numbers do NOT sum to the total when a group carries both', () => {
    // The counter must not assume one tag per group. A group carrying both is
    // counted in both, because that is what it is — a caller rendering these
    // as a split of a whole would be reporting a number nobody measured.
    //
    // ⚠️ Built by hand rather than through the fold, and THE REASON IS KI-7:
    // see the next test.
    expect(
      readingListMediaCounts([
        { docs: [{ workKey: FIREFIGHT_KEY }, { bookId: 'firefight-the-reckoners-book-2' }] },
      ]),
    ).toEqual({ library: 1, audiobook: 1 });
  });

  it('🔴 KI-7, PINNED: this site cannot yet fold a library entry with an audio one', () => {
    // A paperback entry written in the library and an audiobook entry written
    // here are the SAME BOOK and still count as 2 on this site. The only thing
    // that joins them is `audiobook_holding`, a D1 table inside the library
    // Worker's database that a static page cannot reach — `readingListFoldKey`
    // says so in its own header.
    //
    // ⚠️ So in practice every group here carries exactly ONE tag today. That is
    // the residual, asserted on purpose: whoever closes KI-7 (by writing a
    // `workKey` on this side, or by publishing a bridge) must change this test
    // and say which route they took.
    const groups = foldReadingList([
      snap('u_a', { workKey: FIREFIGHT_KEY, bookId: 'firefight', bookTitle: 'Firefight' }),
      snap('u_b', { bookId: 'firefight-the-reckoners-book-2', bookTitle: 'Firefight - Bk 2' }),
    ]);
    expect(groups).toHaveLength(2); // ← THE RESIDUAL. One book, two rows.
    expect(readingListMediaCounts(groups)).toEqual({ library: 1, audiobook: 1 });
    for (const g of groups) expect(readingListMediaTags(g.docs)).toHaveLength(1);
  });

  it('separate books count separately, and an empty list is two zeroes', () => {
    const groups = foldReadingList([
      snap('u_a', { workKey: FIREFIGHT_KEY, bookId: 'firefight', bookTitle: 'Firefight' }),
      snap('u_b', { workKey: 'steelheart|brandon sanderson', bookId: 'steelheart', bookTitle: 'Steelheart' }),
      snap('u_c', { bookId: 'alchemised', bookTitle: 'Alchemised' }),
    ]);
    expect(groups).toHaveLength(3);
    expect(readingListMediaCounts(groups)).toEqual({ library: 2, audiobook: 1 });
    expect(readingListMediaCounts([])).toEqual({ library: 0, audiobook: 0 });
    expect(readingListMediaCounts(null)).toEqual({ library: 0, audiobook: 0 });
  });
});
