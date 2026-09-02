// Feature: the chapter model behind the player — site/audio-chapters.js
//
// AUDIO PLAYER PHASE 2. Design: catalog-platform/docs/info/audio-player-design.md
//
// ⚠️ WHY THESE EXIST. Requirement 7 — the CHAPTER-RELATIVE scrub bar — is the
// owner's hardest ask and the sole reason this feature ships no player library
// (design §2.3). Every way it can be wrong is silent: a boundary off by one
// chapter is not an error, it is "next chapter played the end of the last one";
// a fraction computed against the BOOK instead of the CHAPTER is not an error,
// it is a bar that moves a hair an hour and looks broken to nobody in
// particular. Nothing throws, so nothing catches it but these.
//
// They pin DECISIONS, not plumbing:
//   * `start_sec` beats `start_min` (6-second rounding is AUDIBLE — §1.2)
//   * a chapter's end is the NEXT one's start, and the last has NO end until a
//     duration arrives (§8 cross-cutting)
//   * "previous" restarts the current chapter past 3 s (§8 #5)
//   * the sleep timer's "end of chapter" divides by playbackRate (§8 #8)
//   * nothing ever renders NaN
import { describe, it, expect } from 'vitest';

import {
  buildChapters,
  chapterStartSec,
  withDuration,
  chapterIndexAt,
  chapterFraction,
  timeAtChapterFraction,
  chapterTimes,
  previousChapterTarget,
  nextChapterTarget,
  msUntilChapterEnd,
  formatTime,
  PREV_RESTART_THRESHOLD_SEC,
} from '../audio-chapters.js';

// A three-chapter book shaped exactly like a real chapters.json entry.
const JSON_FIXTURE = {
  'Test Book': {
    source: 'm4b',
    chapters: [
      { title: 'Opening Credits', start_min: 0.0, start_sec: 0.0 },
      { title: 'Chapter 1', start_min: 0.3, start_sec: 15.488 },
      { title: 'Chapter 2', start_min: 1.9, start_sec: 113.964 },
    ],
    parts: [{ label: 'Part One', start_index: 1, end_index: 2 }],
  },
};

const CH = buildChapters(JSON_FIXTURE, 'Test Book');

describe('buildChapters', () => {
  it('reads a real chapters.json entry into indexed chapters', () => {
    expect(CH).toHaveLength(3);
    expect(CH[0]).toMatchObject({ index: 0, title: 'Opening Credits', startSec: 0 });
    expect(CH[1].title).toBe('Chapter 1');
  });

  // ⚠️ THE 6-SECOND ROUNDING. start_min 0.3 is 18 s; the truth is 15.488 s.
  // Preferring the wrong field lands "next chapter" 2.5 s late, every time.
  it('prefers start_sec over the 6-second-rounded start_min', () => {
    expect(CH[1].startSec).toBe(15.488);
    expect(CH[1].startSec).not.toBe(18);
  });

  it('falls back to start_min only when start_sec is absent', () => {
    expect(chapterStartSec({ start_min: 2 })).toBe(120);
    expect(chapterStartSec({ start_min: 2, start_sec: 119.2 })).toBe(119.2);
  });

  it('computes each end from the NEXT start, and leaves the last one open', () => {
    expect(CH[0].endSec).toBe(15.488);
    expect(CH[1].endSec).toBe(113.964);
    // ⚠️ The last chapter has NO end until a duration arrives. Not 0, not the
    // start — null, so the bar can say "not known yet" instead of NaN.
    expect(CH[2].endSec).toBeNull();
  });

  it('carries the part label for the 88 books that have parts', () => {
    expect(CH[0].partLabel).toBeNull();
    expect(CH[1].partLabel).toBe('Part One');
    expect(CH[2].partLabel).toBe('Part One');
  });

  it('sorts by start time rather than trusting file order', () => {
    const out = buildChapters({
      B: { chapters: [{ title: 'b', start_sec: 50 }, { title: 'a', start_sec: 10 }] },
    }, 'B');
    expect(out.map((c) => c.title)).toEqual(['a', 'b']);
  });

  // A chapter with no usable start is DROPPED, never defaulted to 0 — a
  // chapter silently parked at the beginning hijacks the first scrub.
  it('drops a chapter with no usable start rather than parking it at zero', () => {
    const out = buildChapters({
      B: { chapters: [{ title: 'ok', start_sec: 5 }, { title: 'junk' }] },
    }, 'B');
    expect(out).toHaveLength(1);
    expect(out[0].title).toBe('ok');
  });

  it('answers [] for an unknown book, a missing file or a chapterless entry', () => {
    expect(buildChapters(JSON_FIXTURE, 'Nope')).toEqual([]);
    expect(buildChapters(null, 'Test Book')).toEqual([]);
    expect(buildChapters(JSON_FIXTURE, '')).toEqual([]);
    expect(buildChapters({ B: { chapters: [] } }, 'B')).toEqual([]);
  });
});

describe('withDuration — the only way the last chapter gets an end', () => {
  it('closes the final chapter once loadedmetadata reports a duration', () => {
    const closed = withDuration(CH, 300);
    expect(closed[2].endSec).toBe(300);
    expect(closed[1].endSec).toBe(113.964); // the others are untouched
  });

  it('does not mutate the input', () => {
    withDuration(CH, 300);
    expect(CH[2].endSec).toBeNull();
  });

  // Infinity is what a still-loading stream reports in some browsers. A bad
  // duration must be ignored — a nonsense end is worse than no end.
  it('ignores a duration that is not a finite positive number', () => {
    expect(withDuration(CH, Infinity)[2].endSec).toBeNull();
    expect(withDuration(CH, NaN)[2].endSec).toBeNull();
    expect(withDuration(CH, 0)[2].endSec).toBeNull();
  });

  it('refuses a duration that lands before the last chapter starts', () => {
    // Corrupt pairing (wrong file). endSec < startSec makes every fraction
    // negative, which is a bar that runs backwards.
    expect(withDuration(CH, 50)[2].endSec).toBeNull();
  });
});

describe('chapterIndexAt', () => {
  it('finds the chapter containing a time', () => {
    expect(chapterIndexAt(CH, 0)).toBe(0);
    expect(chapterIndexAt(CH, 15.488)).toBe(1); // exactly on a boundary is the NEW chapter
    expect(chapterIndexAt(CH, 100)).toBe(1);
    expect(chapterIndexAt(CH, 113.964)).toBe(2);
    expect(chapterIndexAt(CH, 9999)).toBe(2);
  });

  it('puts a time before the first start in the first chapter', () => {
    const late = buildChapters({ B: { chapters: [{ title: 'a', start_sec: 10 }] } }, 'B');
    expect(chapterIndexAt(late, 2)).toBe(0);
  });

  it('answers -1 for a book with no chapters', () => {
    expect(chapterIndexAt([], 5)).toBe(-1);
  });
});

// 🔴 REQUIREMENT 7. The whole reason this module exists.
describe('chapterFraction — the CHAPTER-relative bar', () => {
  it('measures against the chapter, not the book', () => {
    // 64.726 s is the midpoint of chapter 1 (15.488 -> 113.964).
    expect(chapterFraction(CH, 64.726)).toBeCloseTo(0.5, 6);
    // Book-relative it would be ~0.2 of a 300 s book — the wrong answer, and
    // the one every surveyed player library gives (design §2.3).
    expect(chapterFraction(CH, 64.726)).not.toBeCloseTo(64.726 / 300, 2);
  });

  it('is 0 at a chapter start and 1 at its end', () => {
    expect(chapterFraction(CH, 15.488)).toBe(0);
    expect(chapterFraction(CH, 113.963)).toBeCloseTo(1, 4);
  });

  // ⚠️ The final chapter before loadedmetadata: indeterminate, NEVER NaN.
  it('answers null in the last chapter until a duration is known', () => {
    expect(chapterFraction(CH, 200)).toBeNull();
    expect(chapterFraction(withDuration(CH, 300), 206.982)).toBeCloseTo(0.5, 4);
  });

  it('never returns NaN or a value outside 0..1', () => {
    for (const t of [-50, 0, 15.488, 64, 113.964, NaN, undefined]) {
      const f = chapterFraction(withDuration(CH, 300), t);
      expect(f === null || (f >= 0 && f <= 1)).toBe(true);
      expect(Number.isNaN(f)).toBe(false);
    }
  });
});

describe('timeAtChapterFraction — a drag maps back the same way', () => {
  it('round-trips with chapterFraction', () => {
    const t = 64.726;
    const f = chapterFraction(CH, t);
    expect(timeAtChapterFraction(CH, 1, f)).toBeCloseTo(t, 6);
  });

  it('clamps a drag inside its own chapter', () => {
    expect(timeAtChapterFraction(CH, 1, -1)).toBe(15.488);
    expect(timeAtChapterFraction(CH, 1, 5)).toBe(113.964);
  });

  it('answers null for an open-ended or nonexistent chapter', () => {
    expect(timeAtChapterFraction(CH, 2, 0.5)).toBeNull();
    expect(timeAtChapterFraction(CH, 99, 0.5)).toBeNull();
  });
});

describe('chapterTimes', () => {
  it('reports elapsed-in-chapter and remaining-in-chapter', () => {
    const { index, elapsed, remaining } = chapterTimes(CH, 65.488);
    expect(index).toBe(1);
    expect(elapsed).toBeCloseTo(50, 6);
    expect(remaining).toBeCloseTo(48.476, 6);
  });

  it('reports a null remaining while the last chapter is open-ended', () => {
    expect(chapterTimes(CH, 200).remaining).toBeNull();
  });
});

// ⚠️ Design §8 #5 — its absence "feels broken" and nobody reports it.
describe('previousChapterTarget', () => {
  it('RESTARTS the current chapter when more than 3 s in', () => {
    expect(previousChapterTarget(CH, 60)).toBe(15.488);
  });

  it('steps back a chapter when near the current start', () => {
    expect(previousChapterTarget(CH, 17)).toBe(0);
  });

  it('uses 3 seconds as the boundary, either side of it', () => {
    expect(PREV_RESTART_THRESHOLD_SEC).toBe(3);
    expect(previousChapterTarget(CH, 15.488 + 2.9)).toBe(0);       // step back
    expect(previousChapterTarget(CH, 15.488 + 3.1)).toBe(15.488);  // restart
  });

  it('rewinds to the top rather than nowhere in the first chapter', () => {
    expect(previousChapterTarget(CH, 10)).toBe(0);
    expect(previousChapterTarget(CH, 1)).toBe(0);
  });

  it('answers null when there are no chapters', () => {
    expect(previousChapterTarget([], 10)).toBeNull();
  });
});

describe('nextChapterTarget', () => {
  it('seeks to the next chapter start', () => {
    expect(nextChapterTarget(CH, 0)).toBe(15.488);
    expect(nextChapterTarget(CH, 60)).toBe(113.964);
  });

  it('answers null in the last chapter so the control can be disabled', () => {
    expect(nextChapterTarget(CH, 200)).toBeNull();
  });
});

// ⚠️ Design §8 #8. A timer that ignores playbackRate overshoots by exactly the
// amount the listener sped the book up — two thirds of a chapter at 3×.
describe('msUntilChapterEnd — the sleep timer\'s "end of chapter"', () => {
  it('divides the remaining audio by the playback rate', () => {
    expect(msUntilChapterEnd(CH, 63.964, 1)).toBeCloseTo(50000, 3);
    expect(msUntilChapterEnd(CH, 63.964, 2)).toBeCloseTo(25000, 3);
    expect(msUntilChapterEnd(CH, 63.964, 3)).toBeCloseTo(50000 / 3, 3);
  });

  it('treats a nonsense rate as 1× rather than dividing by zero', () => {
    expect(msUntilChapterEnd(CH, 63.964, 0)).toBeCloseTo(50000, 3);
    expect(msUntilChapterEnd(CH, 63.964, NaN)).toBeCloseTo(50000, 3);
  });

  it('answers null while the chapter end is unknown', () => {
    expect(msUntilChapterEnd(CH, 200, 1)).toBeNull();
  });
});

describe('formatTime', () => {
  it('renders m:ss and h:mm:ss', () => {
    expect(formatTime(0)).toBe('0:00');
    expect(formatTime(65)).toBe('1:05');
    expect(formatTime(3725)).toBe('1:02:05');
  });

  // "0:00" for an unknown duration is a lie that looks like a zero-length book.
  it('renders an unknown value as --:--, never 0:00 and never NaN', () => {
    expect(formatTime(NaN)).toBe('--:--');
    expect(formatTime(Infinity)).toBe('--:--');
    expect(formatTime(undefined)).toBe('--:--');
    expect(formatTime(-5)).toBe('--:--');
  });
});
