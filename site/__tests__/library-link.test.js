// Feature: the ONE canonical catalog→library_catalog join — site/library-link.js
//
// ⚠️ WHY THESE EXIST. Every assertion is traced to a recorded constraint in
// that module's header, and each is a failure that SHIPPED or was about to:
//
//   * The version live until 2026-09-02 was twelve inline lines in index.html
//     that hardcoded `https://library.heygabi.ai/work/` — a second copy of the
//     origin, in the file the estate's own rule says must not hold one.
//   * It said "View record" with no hint that the destination needs a sign-in.
//     Measured live 2026-09-02: /work/229 answers 200 and /api/works/229
//     answers 401, so a stranger who clicks gets an empty shell — a bare
//     refusal wearing a page's clothes, which the never-a-bare-status rule
//     forbids. `ACCESS_NOTE` is on every entry, and this file pins it.
//   * And it could point at exactly ONE work, so *The Wandering Inn* Book 1 —
//     one audiobook, printed as TWO paperbacks (works 229 + 230) — rendered
//     NOTHING. That is the owner's acceptance case, 2026-09-02, and the last
//     describe block below is it, spelled out.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';

import {
  LIBRARY_ORIGIN, ACCESS_NOTE, libraryWorkUrl, normalizeOverrides, libraryLinksFor,
} from '../library-link.js';

/** The shipped file, read rather than mocked — see the last describe block. */
const SHIPPED = JSON.parse(
  readFileSync(new URL('../cross-catalog-overrides.json', import.meta.url), 'utf8'),
);

describe('libraryWorkUrl', () => {
  it('uses /work/ — SINGULAR', () => {
    // ⚠️ /works/<id> is the API. In the browser it answers "Not a page"
    // (apps/web/src/router.tsx `workPath`, read 2026-09-02). The plural form
    // appears in library_catalog's own docs/TODO.md, which is exactly why this
    // is a test and not a memory.
    expect(libraryWorkUrl(229)).toBe('https://library.heygabi.ai/work/229');
    expect(libraryWorkUrl(229)).not.toContain('/works/');
  });

  it('accepts a number or a string, and encodes', () => {
    expect(libraryWorkUrl('229')).toBe(libraryWorkUrl(229));
    expect(libraryWorkUrl('a b')).toBe(LIBRARY_ORIGIN + '/work/a%20b');
  });
});

describe('normalizeOverrides', () => {
  it('yields an empty lookup for junk, rather than throwing', () => {
    for (const junk of [null, undefined, 0, '', 'nope', [], {}, { overrides: null }]) {
      expect(normalizeOverrides(junk).size).toBe(0);
    }
  });

  it('groups several works under one audiobook title', () => {
    const m = normalizeOverrides({
      overrides: [
        { audiobookTitle: 'A', libraryWorkId: 1, libraryTitle: 'One', format: 'Paperback' },
        { audiobookTitle: 'A', libraryWorkId: 2, libraryTitle: 'Two', format: 'Paperback' },
        { audiobookTitle: 'B', libraryWorkId: 3, libraryTitle: 'Three', format: 'Hardcover' },
      ],
    });
    expect(m.get('A').map((e) => e.libraryWorkId)).toEqual(['1', '2']);
    expect(m.get('B')).toHaveLength(1);
  });

  it('keys VERBATIM — no folding, no case insensitivity', () => {
    // ⚠️ A curated row is a claim about ONE catalogue row. A folded key would
    // quietly let it claim a family of them, which is the failure the whole
    // mechanism exists to avoid rather than reproduce.
    const m = normalizeOverrides({
      overrides: [{ audiobookTitle: 'The Wandering Inn', libraryWorkId: 1, format: 'Paperback' }],
    });
    expect(m.get('the wandering inn')).toBeUndefined();
    expect(m.get('The  Wandering Inn')).toBeUndefined();
  });

  it('DROPS a row whose work id is not a positive integer', () => {
    // Each of these would otherwise become /work/NaN or /work/0 — a dead link
    // with a plausible shape, which is worse than no link at all.
    for (const bad of [0, -1, 1.5, 'x', null, undefined, '', {}]) {
      const m = normalizeOverrides({
        overrides: [{ audiobookTitle: 'A', libraryWorkId: bad, format: 'Paperback' }],
      });
      expect(m.size).toBe(0);
    }
  });

  it('DROPS a row with no format word', () => {
    // The owner's spec (2026-08-14) is that every entry says the form the
    // media is in. An entry that cannot is not rendered at all.
    const m = normalizeOverrides({
      overrides: [{ audiobookTitle: 'A', libraryWorkId: 1, libraryTitle: 'One' }],
    });
    expect(m.size).toBe(0);
  });

  it('drops one bad row without losing the good ones', () => {
    const m = normalizeOverrides({
      overrides: [
        { audiobookTitle: 'A', libraryWorkId: 'nope', format: 'Paperback' },
        { audiobookTitle: 'A', libraryWorkId: 2, libraryTitle: 'Two', format: 'Paperback' },
      ],
    });
    expect(m.get('A')).toHaveLength(1);
  });
});

describe('libraryLinksFor — the automatic stamp', () => {
  const row = { title: 'Elantris', libraryWorkId: '514', libraryFormats: 'Hardcover|Ebook' };

  it('renders one entry per format, all pointing at the one work', () => {
    const links = libraryLinksFor(row, normalizeOverrides(null));
    expect(links.map((l) => l.formatLabel)).toEqual(['Hardcover', 'Ebook']);
    expect(new Set(links.map((l) => l.href))).toEqual(new Set([libraryWorkUrl('514')]));
  });

  it('EVERY entry names the sign-in in its hover text', () => {
    for (const l of libraryLinksFor(row, normalizeOverrides(null))) {
      expect(l.title).toContain(ACCESS_NOTE);
    }
  });

  it('returns [] when there is no counterpart — never a guessed link', () => {
    // 960 of 1,088 catalog rows, measured 2026-09-02. The section is hidden
    // for all of them; nothing here may invent a destination.
    expect(libraryLinksFor({ title: 'X' }, normalizeOverrides(null))).toEqual([]);
    expect(libraryLinksFor({}, normalizeOverrides(null))).toEqual([]);
    expect(libraryLinksFor(null, normalizeOverrides(null))).toEqual([]);
  });

  it('returns [] when EITHER half of the stamp is missing', () => {
    // A work id with no format word would render an entry that does not say
    // what form the media is in; a format with no id has nowhere to point.
    expect(libraryLinksFor({ title: 'X', libraryWorkId: '5' }, undefined)).toEqual([]);
    expect(libraryLinksFor({ title: 'X', libraryFormats: 'Ebook' }, undefined)).toEqual([]);
    expect(libraryLinksFor({ title: 'X', libraryWorkId: '5', libraryFormats: ' | ' }, undefined)).toEqual([]);
  });

  it('never emits a /works/ link', () => {
    for (const l of libraryLinksFor(row, normalizeOverrides(null))) {
      expect(l.href.startsWith(LIBRARY_ORIGIN + '/work/')).toBe(true);
    }
  });
});

describe('libraryLinksFor — curated overrides', () => {
  const overrides = normalizeOverrides({
    overrides: [
      { audiobookTitle: 'A', libraryWorkId: 1, libraryTitle: 'One', libraryLabel: 'Part 1', format: 'Paperback' },
      { audiobookTitle: 'A', libraryWorkId: 2, libraryTitle: 'Two', libraryLabel: 'Part 2', format: 'Paperback' },
    ],
  });

  it('renders one link per curated work', () => {
    const links = libraryLinksFor({ title: 'A' }, overrides);
    expect(links.map((l) => l.href)).toEqual([libraryWorkUrl(1), libraryWorkUrl(2)]);
    expect(links.map((l) => l.linkText)).toEqual(['One (Part 1)', 'Two (Part 2)']);
    expect(links.every((l) => l.curated)).toBe(true);
  });

  it('a curated row REPLACES the automatic stamp, never joins it', () => {
    // Showing both would put one book on screen twice and let the two claims
    // disagree in public. The reviewed one wins.
    const links = libraryLinksFor(
      { title: 'A', libraryWorkId: '999', libraryFormats: 'Hardcover' },
      overrides,
    );
    expect(links).toHaveLength(2);
    expect(links.some((l) => l.href.includes('999'))).toBe(false);
  });

  it('leaves an uncurated row on the automatic path', () => {
    const links = libraryLinksFor(
      { title: 'B', libraryWorkId: '514', libraryFormats: 'Ebook' },
      overrides,
    );
    expect(links).toHaveLength(1);
    expect(links[0].curated).toBe(false);
  });

  it('omits the bracket when there is no label', () => {
    const m = normalizeOverrides({
      overrides: [{ audiobookTitle: 'A', libraryWorkId: 1, libraryTitle: 'One', format: 'Ebook' }],
    });
    expect(libraryLinksFor({ title: 'A' }, m)[0].linkText).toBe('One');
  });
});

describe('THE ACCEPTANCE CASE — The Wandering Inn, against the SHIPPED file', () => {
  // Owner, 2026-09-02: audiobook "Book 1" must reach BOTH library works 229 and
  // 230; audiobook "Book 2" must reach 231 and 232. The auto-matchers on both
  // sides refuse this two-works-one-audiobook shape and are right to — one id
  // column cannot hold two destinations. These assertions read the file that
  // actually ships, not a fixture, because a fixture would keep passing after
  // somebody edited the real rows away.
  const overrides = normalizeOverrides(SHIPPED);
  const BOOK1 = 'The Wandering Inn - The Wandering Inn, Book 1';
  const BOOK2 = 'Fae and Fare - The Wandering Inn, Book 2';

  it('Book 1 links to works 229 AND 230', () => {
    const links = libraryLinksFor({ title: BOOK1 }, overrides);
    expect(links.map((l) => l.href)).toEqual([
      'https://library.heygabi.ai/work/229',
      'https://library.heygabi.ai/work/230',
    ]);
    expect(links.map((l) => l.linkText)).toEqual([
      'The Wandering Inn (Book 1, Part 1)',
      'No Killing Goblins (Book 1, Part 2)',
    ]);
  });

  it('Book 2 links to works 231 AND 232', () => {
    const links = libraryLinksFor({ title: BOOK2 }, overrides);
    expect(links.map((l) => l.href)).toEqual([
      'https://library.heygabi.ai/work/231',
      'https://library.heygabi.ai/work/232',
    ]);
    expect(links.map((l) => l.linkText)).toEqual([
      'Fae and Fare (Book 2, Part 1)',
      'Immortal Games (Book 2, Part 2)',
    ]);
  });

  it('every entry says the format, and names the sign-in', () => {
    for (const t of [BOOK1, BOOK2]) {
      for (const l of libraryLinksFor({ title: t }, overrides)) {
        expect(l.formatLabel).toBe('Paperback');
        expect(l.title).toContain(ACCESS_NOTE);
      }
    }
  });

  it('the shipped file holds EXACTLY these four rows and nothing else', () => {
    // ⚠️ The owner said seed it with these four and bulk-curate nothing else.
    // A fifth row appearing without this test being updated is a curation
    // nobody reviewed, which is the one thing this mechanism must not become.
    expect(SHIPPED.overrides).toHaveLength(4);
    expect(SHIPPED.overrides.map((o) => o.libraryWorkId)).toEqual([229, 230, 231, 232]);
    expect([...overrides.keys()]).toEqual([BOOK1, BOOK2]);
  });
});
