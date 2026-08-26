// THE READ-STATE VOCABULARY — `READING_LIST_STATUSES` and the three readers
// around it.
//
// Owner, 2026-08-26: *"can we also add a filter in each of the search bars for
// tbr and other read states"*. This site's search bar gained a **My Read List**
// option beside its **My TBR List** one, `#list=read&user=…` now works beside
// `#list=tbr&user=…`, and the library catalogue's collection search grew the
// same pair spelled `?list=tbr` / `?list=read` on the same day.
//
// ⚠️ THE VOCABULARY IS MEASURED, NOT ASSUMED. Counted read-only against the
// live `readingLists` collection through the service account on 2026-08-26
// ~15:55 Phoenix: 555 documents, 393 `tbr`, 162 `read`, nothing else;
// `readingLists_dev` still 0. That is why the list is two entries and why this
// file asserts it is exactly two — a third value appearing without a fresh
// measurement is what the assertion refuses.
//
// ⚠️ THE TESTS THAT EARN THE FILE ARE REFUSALS, again:
//
//   * an unknown filter key answers `null` and NEVER defaults to 'tbr'. A
//     default would make a typo in a deep link quietly show the wrong list,
//     which is the class of wrong that looks right — and the page turns that
//     null into a sentence rather than a blank catalogue;
//   * `#list=junk` is not a status, and neither is `#list=reviews` — that one
//     is a different collection with a different key, branched on before any
//     status is asked for;
//   * the LABEL is one function, because four separate worded outcomes use it
//     and a 'read' filter that comes back empty must not say "TBR list" at the
//     person.

import { describe, it, expect } from 'vitest';
import {
  READING_LIST_STATUSES,
  isReadingListStatus,
  readingListLabel,
  readingListStatusFor,
  readingListStatusFromHash,
} from '../reviews.js';

describe('READING_LIST_STATUSES — what the store actually holds', () => {
  it('is exactly the two that were counted on 2026-08-26', () => {
    expect(READING_LIST_STATUSES).toEqual(['tbr', 'read']);
  });

  it('recognises both, and nothing else', () => {
    expect(isReadingListStatus('tbr')).toBe(true);
    expect(isReadingListStatus('read')).toBe(true);
    for (const junk of ['reviews', 'dnf', 'reading', 'TBR', '', null, undefined]) {
      expect(isReadingListStatus(junk)).toBe(false);
    }
  });
});

describe('readingListStatusFor — the dropdown key -> status mapping', () => {
  it('maps the two list keys', () => {
    expect(readingListStatusFor('_mytbr')).toBe('tbr');
    expect(readingListStatusFor('_myread')).toBe('read');
  });

  it('answers null for reviews — a different collection, branched on earlier', () => {
    expect(readingListStatusFor('_myreviews')).toBeNull();
  });

  it('⚠️ answers null for an unknown key and NEVER defaults to tbr', () => {
    // The page turns this null into "That reading list does not exist", which
    // is the difference between a typo you can see and a wrong list you cannot.
    for (const key of ['_myjunk', '_my', '', null, undefined, '_universe:The Cosmere']) {
      expect(readingListStatusFor(key)).toBeNull();
    }
  });
});

describe('readingListStatusFromHash — the #list= deep link', () => {
  it('accepts both real statuses', () => {
    expect(readingListStatusFromHash('tbr')).toBe('tbr');
    expect(readingListStatusFromHash('read')).toBe('read');
  });

  it('⚠️ refuses anything else, reviews included', () => {
    for (const v of ['reviews', 'junk', 'Read', '', null]) {
      expect(readingListStatusFromHash(v)).toBeNull();
    }
  });
});

describe('readingListLabel — one spelling per list', () => {
  it('names each list the way every worded outcome names it', () => {
    expect(readingListLabel('tbr')).toBe('TBR list');
    expect(readingListLabel('read')).toBe('read list');
  });

  it('⚠️ never returns an empty string — a sentence with a hole in it is worse', () => {
    for (const status of READING_LIST_STATUSES) {
      expect(readingListLabel(status).length).toBeGreaterThan(0);
    }
  });
});
