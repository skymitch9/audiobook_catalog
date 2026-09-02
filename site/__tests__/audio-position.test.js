// Feature: the audio locator — site/audio-position.js
//
// AUDIO PLAYER PHASE 3 ("save your spot"), 2026-09-02.
// Design §7.4 (the position store), §8 #1 (when to write), §10.1 (eviction).
//
// ⚠️ WHY THESE EXIST. Every failure in this file is SILENT, and each one lands
// on a person rather than on a log:
//
//   🔴 navigating by the absolute `seconds` looks perfect until the day a file
//      is re-encoded, and then it drops everybody a few minutes off their
//      place with no error anywhere. Design §7.4 says carry it and NEVER
//      navigate by it; the enforcement is a test, not a comment.
//   🔴 resolving a locator that cannot be honoured must REFUSE, not answer 0.
//      Answering 0 restarts a 30-hour book from the beginning, which is
//      precisely the thing this whole phase exists to stop.
//   🔴 the eviction stamp is in epoch MILLISECONDS. In seconds it reads as
//      1970 on the Python side, which is older than any cutoff — so a wrong
//      unit does not fail, it DELETES a book somebody is listening to.
import { describe, it, expect } from 'vitest';

import {
  RECORD_INTERVAL_MS, STAMP_INTERVAL_MS, STAMP_COLLECTION,
  toLocator, resolveLocator, progressFor, positionLabel, shouldStamp, stampBody,
} from '../audio-position.js';
import { buildChapters, formatTime } from '../audio-chapters.js';

/** Skyward-shaped: three chapters, the last one open-ended until metadata. */
const CHAPTERS = buildChapters({
  Skyward: {
    chapters: [
      { title: 'Prologue', start_sec: 0 },
      { title: 'Chapter One', start_sec: 300.5 },
      { title: 'Chapter Two', start_sec: 1200.25 },
    ],
  },
}, 'Skyward');

describe('toLocator — a playhead becomes a chapter and an offset', () => {
  it('measures the offset from the chapter it lands in', () => {
    expect(toLocator(CHAPTERS, 400)).toEqual({
      chapter: 1, offsetSec: 99.5, seconds: 400,
    });
  });

  it('keeps the absolute seconds alongside, for display', () => {
    // Design §7.4: "carry an absolute `seconds` field alongside for display
    // and as a fallback". It is carried; resolveLocator below proves it is
    // never NAVIGATED by.
    expect(toLocator(CHAPTERS, 1500).seconds).toBe(1500);
  });

  it('lands the last chapter even with no known end', () => {
    expect(CHAPTERS[2].endSec).toBeNull();
    expect(toLocator(CHAPTERS, 5000)).toMatchObject({ chapter: 2, offsetSec: 3799.75 });
  });

  it('never records a negative offset', () => {
    // ⚠️ An m4b routinely opens a few tenths before chapter 0 starts.
    const late = buildChapters({ B: { chapters: [{ title: 'One', start_sec: 0.4 }] } }, 'B');
    expect(toLocator(late, 0.1).offsetSec).toBe(0);
  });

  it('treats a chapterless book as chapter 0 with an absolute offset', () => {
    expect(toLocator([], 812.4)).toEqual({ chapter: 0, offsetSec: 812.4, seconds: 812.4 });
  });

  it('refuses a playhead it cannot compute rather than recording zero', () => {
    // An unparseable position stored as 0 is indistinguishable from "at the
    // very start", and it would overwrite a real one.
    for (const bad of [NaN, Infinity, -1, null, undefined, '400']) {
      expect(toLocator(CHAPTERS, bad)).toBeNull();
    }
  });
});

describe('resolveLocator — and the absolute seconds it must never use', () => {
  it('round-trips a playhead through the store', () => {
    const t = 1450.75;
    expect(resolveLocator(CHAPTERS, toLocator(CHAPTERS, t))).toBeCloseTo(t, 6);
  });

  // 🔴 THE ONE THAT PINS DESIGN §7.4.
  it('IGNORES `seconds` completely — a re-encode moves the file, not the chapter', () => {
    // The same chapter+offset, with an absolute `seconds` that disagrees
    // wildly (what a re-encode or a boundary correction produces). The chapter
    // locator must win; if `seconds` were consulted the answer would be 9999.
    const stale = { chapter: 1, offsetSec: 60, seconds: 9999 };
    expect(resolveLocator(CHAPTERS, stale)).toBe(360.5);
  });

  it('clamps inside the chapter it names, never past its end', () => {
    // A re-encode that SHORTENED chapter 1 must not push the playhead into
    // chapter 2 and start playing the wrong scene.
    const withEnd = CHAPTERS;          // chapter 1 ends at 1200.25
    expect(resolveLocator(withEnd, { chapter: 1, offsetSec: 99999 })).toBe(1200.25);
  });

  it('honours a chapterless locator against a chapterless book', () => {
    expect(resolveLocator([], { chapter: 0, offsetSec: 812.4, seconds: 812.4 })).toBe(812.4);
  });

  // 🔴 THE REFUSALS. Each one returns null so the caller leaves the playhead
  // alone; a 0 here silently restarts a 30-hour book.
  it('refuses a mid-book locator when the chapter table did not load', () => {
    expect(resolveLocator([], { chapter: 7, offsetSec: 120, seconds: 5000 })).toBeNull();
  });

  it('refuses a chapter this book no longer has', () => {
    // 68 chapters re-cut to 12: every boundary moved, so landing somebody at
    // the end of the new chapter 11 would be a confident wrong answer.
    expect(resolveLocator(CHAPTERS, { chapter: 40, offsetSec: 10 })).toBeNull();
  });

  it('refuses a malformed locator instead of guessing at it', () => {
    for (const bad of [null, undefined, {}, 'x', { chapter: -1, offsetSec: 1 },
      { chapter: 1.5, offsetSec: 1 }, { chapter: 1 }, { chapter: 1, offsetSec: NaN },
      { chapter: 1, offsetSec: -5 }]) {
      expect(resolveLocator(CHAPTERS, bad)).toBeNull();
    }
  });
});

describe('progressFor — honest about a duration that is not known yet', () => {
  it('is a fraction of the book', () => {
    expect(progressFor(3000, 10000)).toBeCloseTo(0.3, 6);
  });

  it('answers null, not zero, before loadedmetadata', () => {
    // ⚠️ A confident `progress: 0` written in that window would later render
    // as "not started" on a continue-listening shelf.
    expect(progressFor(3000, null)).toBeNull();
    expect(progressFor(3000, Infinity)).toBeNull();
    expect(progressFor(3000, 0)).toBeNull();
  });

  it('clamps to 0..1', () => {
    expect(progressFor(20000, 10000)).toBe(1);
  });
});

describe('positionLabel — what the resume bar actually says', () => {
  it('names the chapter and how far into it', () => {
    expect(positionLabel(CHAPTERS, toLocator(CHAPTERS, 400), formatTime))
      .toBe('Chapter 2 — Chapter One · 1:39 in');
  });

  it('falls back to book time when there is no chapter to name', () => {
    expect(positionLabel([], toLocator([], 812.4), formatTime)).toBe('13:32 in');
  });

  it('never renders undefined', () => {
    expect(positionLabel(CHAPTERS, null, formatTime)).toBe('');
  });
});

describe('the eviction stamp — the MID-BOOK SHIELD', () => {
  it('is its own opaque collection, not readingPositions', () => {
    // readingPositions is `allow list: if false` in both lanes on purpose, and
    // the evictor lists with the PUBLIC web key. See audio-position.js §4.
    expect(STAMP_COLLECTION).toBe('audio_positions');
  });

  // 🔴 THE UNIT. `_parse_stamp` in fulfill_audio_requests.py reads anything
  // under 1e11 as SECONDS, so seconds here decode to 1970 — older than every
  // cutoff, i.e. "evict this book" about a book being listened to right now.
  it('writes epoch MILLISECONDS', () => {
    const now = 1788000000000;
    expect(stampBody('b-4754c8e4548e', now)).toEqual({
      anchor: 'b-4754c8e4548e', lastPositionAt: 1788000000000,
    });
    expect(String(stampBody('b-x', now).lastPositionAt).length).toBeGreaterThan(11);
  });

  it('carries the anchor in the body as well as the id, so a doc is self-describing', () => {
    expect(stampBody('b-abc', 1788000000000).anchor).toBe('b-abc');
  });

  it('stamps on the first touch of a session', () => {
    // Somebody who opens a book, listens two minutes and closes the tab must
    // still have shielded it.
    expect(shouldStamp(null, 1788000000000)).toBe(true);
    expect(shouldStamp(0, 1788000000000)).toBe(true);
  });

  it('then throttles to one write per interval', () => {
    const t0 = 1788000000000;
    expect(shouldStamp(t0, t0 + 1000)).toBe(false);
    expect(shouldStamp(t0, t0 + STAMP_INTERVAL_MS)).toBe(true);
  });

  it('is far coarser than the position write, and that is the point', () => {
    // The evictor asks about 30 DAYS. One stamp per session is generous; this
    // bounds a 15-hour listen at ~90 writes rather than ~3,600.
    expect(STAMP_INTERVAL_MS).toBeGreaterThan(RECORD_INTERVAL_MS * 10);
  });
});
