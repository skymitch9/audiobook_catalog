// Feature: what the player remembers — site/audio-prefs.js
//
// AUDIO PLAYER PHASE 2. Design §6 (speed to 3×, per BOOK), §8 #4 (a
// configurable skip interval), §9.2 #2.
//
// ⚠️ WHY THESE EXIST. Two of the three failures here are silent and one is
// worse than silent:
//   * `localStorage` THROWS in Safari private browsing and with cookies
//     blocked — not "returns null", throws. An unguarded read takes the whole
//     player down for the people most likely to be on a phone.
//   * a global (rather than per-book) speed opens every new book at the pace
//     that suited a different narrator, which reads as a broken player rather
//     than as a setting.
//   * the ladder must actually REACH 3×, because "speed to 3×" is the owner's
//     ask in his own words and a ladder stopping at 2× fails it quietly.
import { describe, it, expect, beforeEach } from 'vitest';

import {
  SPEEDS, SKIP_INTERVALS, SLEEP_PRESETS_MIN, DEFAULT_SKIP_SEC,
  nearestSpeed, getBookRate, setBookRate, getSkipSec, setSkipSec,
} from '../audio-prefs.js';

/** A localStorage stand-in that behaves, and one that does not. */
function fakeStore() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    _map: map,
  };
}

const hostileStore = {
  getItem() { throw new DOMException('The operation is insecure.'); },
  setItem() { throw new DOMException('The operation is insecure.'); },
};

describe('the offered ladders', () => {
  // The owner's ask, verbatim: "speed to 3x".
  it('offers speeds up to and including 3×', () => {
    expect(SPEEDS).toContain(3);
    expect(Math.max(...SPEEDS)).toBe(3);
  });

  it('matches the design\'s ladder exactly', () => {
    expect(SPEEDS).toEqual([0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]);
  });

  it('offers the three skip intervals people argue about', () => {
    expect(SKIP_INTERVALS).toEqual([10, 15, 30]);
    expect(DEFAULT_SKIP_SEC).toBe(15);
  });

  it('offers the sleep presets, and 60 minutes is the longest', () => {
    expect(SLEEP_PRESETS_MIN).toEqual([5, 10, 15, 30, 45, 60]);
  });
});

describe('nearestSpeed', () => {
  it('snaps to the closest offered speed', () => {
    expect(nearestSpeed(1.3)).toBe(1.25);
    expect(nearestSpeed(2.9)).toBe(3.0);
    expect(nearestSpeed(0.1)).toBe(0.75);
    expect(nearestSpeed(99)).toBe(3.0);
  });

  it('answers 1.0 for nonsense rather than NaN', () => {
    expect(nearestSpeed(NaN)).toBe(1.0);
    expect(nearestSpeed('fast')).toBe(1.0);
    expect(nearestSpeed(undefined)).toBe(1.0);
  });
});

describe('per-BOOK speed memory', () => {
  let store;
  beforeEach(() => { store = fakeStore(); });

  it('remembers a rate for one book and not for another', () => {
    setBookRate('skyward', 2, store);
    expect(getBookRate('skyward', store)).toBe(2);
    // ⚠️ THE POINT OF PER-BOOK. A different narrator opens at 1×.
    expect(getBookRate('mistborn', store)).toBe(1);
  });

  it('defaults to 1× when nothing is remembered', () => {
    expect(getBookRate('anything', store)).toBe(1);
  });

  it('ignores a stored value that is not a usable rate', () => {
    store.setItem('ab:audio:rate:skyward', 'quickly');
    expect(getBookRate('skyward', store)).toBe(1);
    store.setItem('ab:audio:rate:skyward', '-2');
    expect(getBookRate('skyward', store)).toBe(1);
  });

  // ⚠️ A stored rate OUTLIVES the ladder. If SPEEDS is ever re-cut, a book
  // remembered at a retired speed must still open at something sensible —
  // snapping keeps the memory, rejecting would throw every book back to 1×.
  it('snaps a stored value from a retired ladder rather than discarding it', () => {
    store.setItem('ab:audio:rate:skyward', '2.8');
    expect(getBookRate('skyward', store)).toBe(3);
    // An exact tie (2.75 sits between 2.5 and 3.0) resolves to the LOWER
    // speed, which is the safer half of an arbitrary choice: too slow is
    // noticed and corrected, too fast is blamed on the narrator.
    store.setItem('ab:audio:rate:skyward', '2.75');
    expect(getBookRate('skyward', store)).toBe(2.5);
  });

  it('refuses to store against an empty bookId', () => {
    expect(setBookRate('', 2, store)).toBe(false);
    expect(getBookRate('', store)).toBe(1);
  });

  // 🔴 Safari private browsing THROWS. Losing a remembered speed must never
  // take the player down with it.
  it('degrades to 1× when the store throws, rather than propagating', () => {
    expect(() => getBookRate('skyward', hostileStore)).not.toThrow();
    expect(getBookRate('skyward', hostileStore)).toBe(1);
    expect(() => setBookRate('skyward', 2, hostileStore)).not.toThrow();
    expect(setBookRate('skyward', 2, hostileStore)).toBe(false);
  });
});

describe('the device\'s skip interval', () => {
  let store;
  beforeEach(() => { store = fakeStore(); });

  it('defaults to 15 seconds', () => {
    expect(getSkipSec(store)).toBe(15);
  });

  it('round-trips an offered interval', () => {
    expect(setSkipSec(30, store)).toBe(true);
    expect(getSkipSec(store)).toBe(30);
  });

  // ⚠️ REFUSED, not snapped — unlike the speed, and the difference is
  // deliberate: this comes from a fixed three-way control, so anything else is
  // a bug in the caller and should not be silently accepted.
  it('refuses an interval that is not offered', () => {
    expect(setSkipSec(7, store)).toBe(false);
    expect(setSkipSec('15', store)).toBe(false);
    expect(getSkipSec(store)).toBe(15);
  });

  it('ignores a corrupted stored value', () => {
    store.setItem('ab:audio:skip', '7');
    expect(getSkipSec(store)).toBe(15);
  });

  it('degrades to the default when the store throws', () => {
    expect(getSkipSec(hostileStore)).toBe(15);
    expect(setSkipSec(30, hostileStore)).toBe(false);
  });
});
