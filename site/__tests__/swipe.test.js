/**
 * swipe.test.js — the gesture arithmetic, exercised.
 *
 * ⚠️ WHY THIS FILE IS WORTH ITS LINES. A swipe-to-turn is trivial to write and
 * three lines from being a nuisance: turning pages while somebody scrolls,
 * turning pages while somebody pinches a map, turning pages while somebody
 * pans a zoomed PDF. None of those is a crash, none appears in a log, and all
 * three are found by USING the reader on a phone — the one place nothing in
 * this repo can automate. So the decision was made pure (`swipeIntent`) for
 * exactly this: every false-turn case gets a test, and only "does the browser
 * deliver the events" is left to a thumb.
 */

import { describe, expect, it, vi } from 'vitest';
import {
  SWIPE_AXIS_RATIO,
  SWIPE_MAX_MS,
  SWIPE_MIN_PX,
  swipeIntent,
  wireSwipe,
} from '../swipe.js';

const g = (over) => ({ dx: 0, dy: 0, dt: 200, ...over });

describe('swipeIntent — the turns it allows', () => {
  it('a firm flick LEFT is the next page', () => {
    expect(swipeIntent(g({ dx: -120, dy: 4 }))).toBe('next');
  });

  it('a firm flick RIGHT is the previous page', () => {
    expect(swipeIntent(g({ dx: 120, dy: -6 }))).toBe('prev');
  });

  it('a diagonal flick still counts while the horizontal half dominates', () => {
    // 120 across, 60 down: 2x, comfortably over the ratio.
    expect(swipeIntent(g({ dx: -120, dy: 60 }))).toBe('next');
  });
});

describe('swipeIntent — ⚠️ the four false turns', () => {
  it('a VERTICAL SCROLL with sideways drift does not turn a page', () => {
    // The single most common false positive: a thumb travelling down a long
    // page does not travel straight, and a bare |dx| > 50 turns pages the
    // whole way down.
    expect(swipeIntent(g({ dx: -70, dy: 300 }))).toBe(null);
  });

  it('a PINCH does not turn a page, however far the fingers travel', () => {
    expect(swipeIntent(g({ dx: -300, dy: 0, multitouch: true }))).toBe(null);
  });

  it('a page left ZOOMED IN refuses turns — a sideways drag there is a pan', () => {
    expect(swipeIntent(g({ dx: -300, dy: 0, scale: 2.4 }))).toBe(null);
    // ...and the same gesture at rest is a turn, so the guard is the scale and
    // not something else about the case.
    expect(swipeIntent(g({ dx: -300, dy: 0, scale: 1 }))).toBe('next');
  });

  it('a caller that OWNS the horizontal axis blocks the turn', () => {
    // The reader passes this whenever the rendered PDF is wider than its
    // stage: the sideways axis already belongs to scrolling, and turning there
    // both loses the reader's place in the spread and changes the page.
    expect(swipeIntent(g({ dx: -300, dy: 0, axisTaken: true }))).toBe(null);
  });

  it('a SLOW DRAG is a fumble, not a swipe', () => {
    expect(swipeIntent(g({ dx: -300, dy: 0, dt: SWIPE_MAX_MS + 1 }))).toBe(null);
    expect(swipeIntent(g({ dx: -300, dy: 0, dt: SWIPE_MAX_MS - 1 }))).toBe('next');
  });

  it('a TAP, or anything shorter than the threshold, is not a turn', () => {
    expect(swipeIntent(g({ dx: 0, dy: 0 }))).toBe(null);
    expect(swipeIntent(g({ dx: -(SWIPE_MIN_PX - 1), dy: 0 }))).toBe(null);
    expect(swipeIntent(g({ dx: -SWIPE_MIN_PX, dy: 0 }))).toBe('next');
  });

  it('the axis ratio is enforced at its stated boundary, not approximately', () => {
    const dy = 60;
    const justUnder = dy * SWIPE_AXIS_RATIO - 1;
    const justOver = dy * SWIPE_AXIS_RATIO + 1;
    expect(swipeIntent(g({ dx: -justUnder, dy }))).toBe(null);
    expect(swipeIntent(g({ dx: -justOver, dy }))).toBe('next');
  });

  it('nonsense in is null out, never a turn', () => {
    expect(swipeIntent(null)).toBe(null);
    expect(swipeIntent(g({ dx: NaN }))).toBe(null);
    expect(swipeIntent(g({ dx: -300, dt: -5 }))).toBe(null);
  });
});

/* ── the wiring ─────────────────────────────────────────────────────────── */

/** A minimal EventTarget stand-in that records what was attached. */
function fakeTarget() {
  const handlers = {};
  return {
    handlers,
    addEventListener(name, fn) { (handlers[name] ||= []).push(fn); },
    removeEventListener(name, fn) {
      handlers[name] = (handlers[name] || []).filter((h) => h !== fn);
    },
    fire(name, ev) { (handlers[name] || []).forEach((h) => h(ev)); },
  };
}

const touch = (x, y) => ({ clientX: x, clientY: y });

describe('wireSwipe', () => {
  it('calls the CALLER’S turn functions — never a renderer directly', () => {
    // ⚠️ This is the load-bearing one. reader.js hands it goNext/goPrev, which
    // are what record the reading position; a swipe wired straight to
    // drawPage() or view.next() would turn pages perfectly and silently stop
    // saving anybody's place (docs/info/reader-page.md §7.6).
    const t = fakeTarget();
    const onNext = vi.fn();
    const onPrev = vi.fn();
    wireSwipe(t, { onNext, onPrev });

    t.fire('touchstart', { touches: [touch(300, 100)], timeStamp: 0 });
    t.fire('touchend', { changedTouches: [touch(100, 108)], timeStamp: 180 });
    expect(onNext).toHaveBeenCalledTimes(1);
    expect(onPrev).not.toHaveBeenCalled();

    t.fire('touchstart', { touches: [touch(100, 100)], timeStamp: 0 });
    t.fire('touchend', { changedTouches: [touch(300, 96)], timeStamp: 180 });
    expect(onPrev).toHaveBeenCalledTimes(1);
  });

  it('a second finger DURING the gesture cancels it, even if it lifts first', () => {
    const t = fakeTarget();
    const onNext = vi.fn();
    wireSwipe(t, { onNext, onPrev: vi.fn() });
    t.fire('touchstart', { touches: [touch(300, 100)], timeStamp: 0 });
    t.fire('touchmove', { touches: [touch(250, 100), touch(50, 100)], timeStamp: 60 });
    t.fire('touchend', { changedTouches: [touch(100, 100)], timeStamp: 180 });
    expect(onNext).not.toHaveBeenCalled();
  });

  it('a gesture that STARTS with two fingers is never tracked', () => {
    const t = fakeTarget();
    const onNext = vi.fn();
    wireSwipe(t, { onNext, onPrev: vi.fn() });
    t.fire('touchstart', { touches: [touch(300, 100), touch(100, 100)], timeStamp: 0 });
    t.fire('touchend', { changedTouches: [touch(100, 100)], timeStamp: 180 });
    expect(onNext).not.toHaveBeenCalled();
  });

  it('touchcancel drops the gesture — an interrupted swipe turns nothing', () => {
    const t = fakeTarget();
    const onNext = vi.fn();
    wireSwipe(t, { onNext, onPrev: vi.fn() });
    t.fire('touchstart', { touches: [touch(300, 100)], timeStamp: 0 });
    t.fire('touchcancel', {});
    t.fire('touchend', { changedTouches: [touch(100, 100)], timeStamp: 180 });
    expect(onNext).not.toHaveBeenCalled();
  });

  it('asks axisTaken at RELEASE, not at wiring time', () => {
    // The zoom buttons change the answer between gestures, so a captured
    // boolean would be right once and wrong afterwards.
    const t = fakeTarget();
    const onNext = vi.fn();
    let taken = true;
    wireSwipe(t, { onNext, onPrev: vi.fn(), axisTaken: () => taken });

    t.fire('touchstart', { touches: [touch(300, 100)], timeStamp: 0 });
    t.fire('touchend', { changedTouches: [touch(100, 100)], timeStamp: 180 });
    expect(onNext).not.toHaveBeenCalled();

    taken = false;
    t.fire('touchstart', { touches: [touch(300, 100)], timeStamp: 0 });
    t.fire('touchend', { changedTouches: [touch(100, 100)], timeStamp: 180 });
    expect(onNext).toHaveBeenCalledTimes(1);
  });

  it('detaching stops it — the FXL re-attach path must not accumulate handlers', () => {
    const t = fakeTarget();
    const onNext = vi.fn();
    const detach = wireSwipe(t, { onNext, onPrev: vi.fn() });
    detach();
    t.fire('touchstart', { touches: [touch(300, 100)], timeStamp: 0 });
    t.fire('touchend', { changedTouches: [touch(100, 100)], timeStamp: 180 });
    expect(onNext).not.toHaveBeenCalled();
  });

  it('a target that cannot listen is survived, not thrown on', () => {
    expect(() => wireSwipe(null, { onNext() {}, onPrev() {} })()).not.toThrow();
  });
});
