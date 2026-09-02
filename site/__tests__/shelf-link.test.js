// Feature: the ONE canonical catalog→Audiobookshelf join — site/shelf-link.js
//
// ⚠️ WHY THESE EXIST. Every assertion below is traced to a recorded constraint
// in docs/TODO.md ("Shelf link on every book"), and each one is a failure that
// SHIPPED or nearly shipped:
//
//   * The version live until 2026-09-02 linked to `/audiobookshelf/item/<uuid>`
//     for 1,077 books. ABS item ids are not stable — every id from the
//     2026-08-20 flat layout 404'd after the hardlink reshape. The ids happened
//     to be live when re-measured, which measures only that the map was rebuilt
//     recently. `neverEmitsAnItemLink` is the guard, and it is the single most
//     important test in this file.
//   * That version also said "🎧 Play / Download in Shelf" with no hint that
//     clicking it drops you at a Google sign-in. The owner confirmed the
//     bounce happens to real people ("it made Justin sign in").
//   * And it rendered a search link for EVERY unmatched row, so the 132
//     ebook-only items got a button that searched a library for a title it
//     does not hold. `null` is the required answer.
import { describe, it, expect } from 'vitest';

import {
  SHELF_ORIGIN, SHELF_APP, AUDIO_LIBRARY_ID, ACCESS_NOTE,
  bookIdFromTitle, shelfSearchUrl, normalizeShelfMap, shelfLinkFor,
} from '../shelf-link.js';

/** The current map shape, as `scripts/build_shelf_map.py` now writes it. */
const CURRENT = {
  generatedAt: '2026-09-02T18:00:00Z',
  libraryId: AUDIO_LIBRARY_ID,
  ebookLibraryId: null,
  books: {
    'a-brief-history-of-time': { t: 'A Brief History of Time', m: 'audio' },
    unsouled: { t: 'Unsouled - Will Wight', m: 'ebook' },
  },
};

/** The flat shape shipped until 2026-09-02 — a cached browser still has it. */
const LEGACY = {
  'a-brief-history-of-time': '7a00991a-d4a7-455f-8646-376e520f2d06',
  unsouled: '2a2adee7-3480-4fd8-9d98-9e77eafc6092',
};

describe('constraint 1 — ABS item ids rot, so no link may contain one', () => {
  it('never emits an /item/ link, from either map shape', () => {
    for (const raw of [CURRENT, LEGACY]) {
      const map = normalizeShelfMap(raw);
      for (const slug of Object.keys(map.books)) {
        const link = shelfLinkFor(slug.replace(/-/g, ' '), map);
        if (!link) continue;
        expect(link.href).not.toContain('/item/');
      }
    }
  });

  it('discards the uuid in a legacy map rather than routing through it', () => {
    const map = normalizeShelfMap(LEGACY);
    const link = shelfLinkFor('A Brief History of Time', map);
    expect(link).not.toBeNull();
    // The uuid must appear nowhere in the produced link.
    expect(link.href).not.toContain('7a00991a');
    expect(JSON.stringify(map.books)).not.toContain('7a00991a');
  });

  it('searches the ABS-side title when the map carries one', () => {
    const map = normalizeShelfMap(CURRENT);
    // The catalogue calls it "Unsouled"; ABS calls it "Unsouled - Will Wight".
    // Measured 2026-09-02: searching the ABS title returns the intended item
    // first for 57 of 60 sampled books and inside the top 10 for the rest.
    const link = shelfLinkFor('Unsouled', map);
    expect(link.query).toBe('Unsouled - Will Wight');
    expect(link.href).toContain(encodeURIComponent('Unsouled - Will Wight'));
  });

  it('falls back to the catalogue title when the map is legacy', () => {
    const map = normalizeShelfMap(LEGACY);
    const link = shelfLinkFor('Unsouled', map);
    expect(link.query).toBe('Unsouled');
  });
});

describe('constraint 2 — everyone who clicks meets Cloudflare Access', () => {
  it('names the sign-in in the note and the hover text', () => {
    const map = normalizeShelfMap(CURRENT);
    const link = shelfLinkFor('A Brief History of Time', map);
    expect(link.note).toBe(ACCESS_NOTE);
    expect(link.title).toContain(ACCESS_NOTE);
    expect(link.title).toContain('new tab');
  });

  it('says the sign-in for an ebook link too, not only an audio one', () => {
    const map = normalizeShelfMap(CURRENT);
    expect(shelfLinkFor('Unsouled', map).note).toBe(ACCESS_NOTE);
  });
});

describe('constraint 3 — a row with no counterpart gets NO link', () => {
  it('returns null for a book the shelf does not hold', () => {
    const map = normalizeShelfMap(CURRENT);
    expect(shelfLinkFor('A Book Nobody Owns', map)).toBeNull();
  });

  it('returns null for an empty or missing title', () => {
    const map = normalizeShelfMap(CURRENT);
    expect(shelfLinkFor('', map)).toBeNull();
    expect(shelfLinkFor(undefined, map)).toBeNull();
  });

  it('returns null when the map failed to load — an outage is not a verdict', () => {
    // ⚠️ fetch() failing must not produce a link into an empty search.
    expect(shelfLinkFor('A Brief History of Time', normalizeShelfMap(null))).toBeNull();
    expect(shelfLinkFor('A Brief History of Time', normalizeShelfMap(undefined))).toBeNull();
  });
});

describe('the map reader tolerates both deployed shapes', () => {
  it('reads the current shape with its build stamp', () => {
    const map = normalizeShelfMap(CURRENT);
    expect(map.generatedAt).toBe('2026-09-02T18:00:00Z');
    expect(Object.keys(map.books)).toHaveLength(2);
    expect(map.books.unsouled.m).toBe('ebook');
  });

  it('reads the legacy flat shape, with no stamp and everything audio', () => {
    const map = normalizeShelfMap(LEGACY);
    expect(map.generatedAt).toBeNull();
    expect(Object.keys(map.books)).toHaveLength(2);
    expect(map.books.unsouled.m).toBe('audio');
  });

  it('survives junk without throwing', () => {
    for (const junk of [null, undefined, 42, 'nope', [], { books: 5 }]) {
      expect(() => normalizeShelfMap(junk)).not.toThrow();
      expect(normalizeShelfMap(junk).books).toEqual({});
    }
  });

  it('picks up an ebook library id once the owner creates one', () => {
    // ⚠️ The Ebooks library does not exist yet (runbook:
    // docs/access/SHELF_EBOOKS_LIBRARY.md). When it does, the generator stamps
    // its id here and ebook links retarget with NO code change. This test is
    // what makes that claim true rather than hopeful.
    const withEbooks = { ...CURRENT, ebookLibraryId: 'eb00-lib-id' };
    const map = normalizeShelfMap(withEbooks);
    expect(shelfLinkFor('Unsouled', map).href).toContain('eb00-lib-id');
    // …while the audio book keeps pointing at the Audio library.
    expect(shelfLinkFor('A Brief History of Time', map).href)
      .toContain(AUDIO_LIBRARY_ID);
  });

  it('leaves ebook links on the Audio library until then', () => {
    // The 132 ebook-only items live in the Audio library TODAY, so this is
    // correct rather than a degraded fallback.
    const map = normalizeShelfMap(CURRENT);
    expect(shelfLinkFor('Unsouled', map).href).toContain(AUDIO_LIBRARY_ID);
  });
});

describe('url construction', () => {
  it('builds a search url under the shelf origin', () => {
    const u = shelfSearchUrl('The Way of Kings');
    expect(u.startsWith(SHELF_APP + '/library/')).toBe(true);
    expect(u).toContain(AUDIO_LIBRARY_ID);
    expect(u).toContain('search?q=');
    expect(SHELF_APP.startsWith(SHELF_ORIGIN)).toBe(true);
  });

  it('encodes characters that would otherwise break the query', () => {
    const u = shelfSearchUrl('Cradle #1 & Co / "quoted"');
    expect(u).toContain('%23');   // #  — would truncate the URL at a fragment
    expect(u).toContain('%26');   // &  — would start a second query param
    expect(u).toContain('%2F');   // /  — would look like another path segment
    expect(u).not.toContain(' ');
  });

  it('labels audio and ebook differently', () => {
    const map = normalizeShelfMap(CURRENT);
    expect(shelfLinkFor('A Brief History of Time', map).label).toContain('Listen');
    expect(shelfLinkFor('Unsouled', map).label).toContain('Read');
  });
});

describe('the slug is the estate-wide one, not a fourth copy of it', () => {
  it('matches the reviews.js implementation it re-exports', async () => {
    const reviews = await import('../reviews.js');
    expect(bookIdFromTitle).toBe(reviews.bookIdFromTitle);
  });

  it('slugifies the way the map keys are written', () => {
    expect(bookIdFromTitle('A Brief History of Time')).toBe('a-brief-history-of-time');
    expect(bookIdFromTitle('10 Things I Hate About Christmas'))
      .toBe('10-things-i-hate-about-christmas');
  });
});
