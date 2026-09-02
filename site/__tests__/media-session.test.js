// Feature: the lock screen and the car stereo — site/media-session.js
//
// AUDIO PLAYER PHASE 2. Design §4.4.
//
// 🔴 THE ONE BUG THESE EXIST FOR. `setActionHandler` **THROWS** for an action
// the browser does not support, and one throw aborts the rest of the wiring.
// A single try/catch around the whole block means an unsupported `seekto` on
// some browser silently costs you `play`, `pause` and both skips — the
// handlers that matter most — with no error anywhere and no way to notice
// except by locking a phone. Per-action guards are the design's own
// instruction and they are not defensive padding.
//
// The rest pin decisions: the chapter goes on the lock screen (otherwise it
// tells a listener nothing they did not know), the OS scrubber is
// BOOK-relative on purpose, and `setPositionState` is never called with input
// that makes it throw.
import { describe, it, expect, vi } from 'vitest';

import {
  hasMediaSession, wireHandlers, publishMetadata, publishPosition,
  publishPlaybackState, teardown,
} from '../media-session.js';

/** A navigator whose mediaSession refuses the named actions, as browsers do. */
function fakeNav({ unsupported = [], noPositionState = false } = {}) {
  const registered = {};
  const ms = {
    metadata: undefined,
    playbackState: 'none',
    setActionHandler(action, fn) {
      if (unsupported.includes(action)) throw new TypeError(`Unsupported action: ${action}`);
      registered[action] = fn;
    },
  };
  if (!noPositionState) ms.setPositionState = vi.fn();
  return { nav: { mediaSession: ms }, registered, ms };
}

class FakeMetadata {
  constructor(init) { Object.assign(this, init); }
}

describe('hasMediaSession', () => {
  it('is honest about a browser without one', () => {
    expect(hasMediaSession({})).toBe(false);
    expect(hasMediaSession({ mediaSession: {} })).toBe(true);
  });
});

describe('wireHandlers — one throw must not cost the rest', () => {
  const handlers = () => ({
    play: vi.fn(), pause: vi.fn(), seekbackward: vi.fn(), seekforward: vi.fn(),
    previoustrack: vi.fn(), nexttrack: vi.fn(), seekto: vi.fn(),
  });

  it('registers every handler a browser accepts', () => {
    const { nav, registered } = fakeNav();
    const accepted = wireHandlers(handlers(), 15, nav);
    expect(accepted).toEqual(['play', 'pause', 'seekbackward', 'seekforward',
      'previoustrack', 'nexttrack', 'seekto']);
    expect(Object.keys(registered)).toHaveLength(7);
  });

  // 🔴 THE HEADLINE. `seekto` is registered LAST in the object; a shared
  // try/catch around the loop would still have registered everything before
  // it — so the test uses an EARLY action to prove the loop continues.
  it('keeps going after an unsupported action throws', () => {
    const { nav, registered } = fakeNav({ unsupported: ['seekbackward'] });
    const accepted = wireHandlers(handlers(), 15, nav);
    expect(accepted).not.toContain('seekbackward');
    // Everything after the throw is still wired — this is the whole point.
    expect(accepted).toContain('seekforward');
    expect(accepted).toContain('play');
    expect(accepted).toContain('seekto');
    expect(registered.play).toBeTypeOf('function');
  });

  it('survives a browser that refuses every action', () => {
    const all = ['play', 'pause', 'seekbackward', 'seekforward', 'previoustrack', 'nexttrack', 'seekto'];
    const { nav } = fakeNav({ unsupported: all });
    expect(() => wireHandlers(handlers(), 15, nav)).not.toThrow();
    expect(wireHandlers(handlers(), 15, nav)).toEqual([]);
  });

  it('answers [] rather than throwing on a browser with no mediaSession', () => {
    expect(wireHandlers(handlers(), 15, {})).toEqual([]);
    expect(wireHandlers(handlers(), 15, null)).toEqual([]);
  });

  it('skips entries that are not functions', () => {
    const { nav } = fakeNav();
    expect(wireHandlers({ play: vi.fn(), pause: null, seekto: 'nope' }, 15, nav)).toEqual(['play']);
  });
});

describe('publishMetadata — the lock screen must name the CHAPTER', () => {
  it('puts the chapter in album, so a locked phone answers "where am I"', () => {
    const { nav, ms } = fakeNav();
    expect(publishMetadata({
      title: 'Skyward', author: 'Brandon Sanderson',
      chapterTitle: 'Chapter 12', coverUrl: 'https://covers.heygabi.ai/x.jpg',
    }, nav, FakeMetadata)).toBe(true);
    expect(ms.metadata.title).toBe('Skyward');
    expect(ms.metadata.artist).toBe('Brandon Sanderson');
    // ⚠️ Not the series, not the year — the CHAPTER.
    expect(ms.metadata.album).toBe('Chapter 12');
  });

  // ⚠️ Design §4.4, CITED: older iOS pixellated an upscaled small icon. The
  // extra entry costs one array element.
  it('ships a 96×96 as well as a 512×512', () => {
    const { nav, ms } = fakeNav();
    publishMetadata({ title: 'x', coverUrl: 'c.jpg' }, nav, FakeMetadata);
    expect(ms.metadata.artwork.map((a) => a.sizes)).toEqual(['96x96', '512x512']);
  });

  it('ships no artwork rather than a broken entry when there is no cover', () => {
    const { nav, ms } = fakeNav();
    publishMetadata({ title: 'x' }, nav, FakeMetadata);
    expect(ms.metadata.artwork).toEqual([]);
  });

  it('answers false rather than throwing where MediaMetadata does not exist', () => {
    const { nav } = fakeNav();
    expect(publishMetadata({ title: 'x' }, nav, undefined)).toBe(false);
    expect(publishMetadata({ title: 'x' }, {}, FakeMetadata)).toBe(false);
  });
});

describe('publishPosition — the OS scrubber is BOOK-relative on purpose', () => {
  it('publishes duration, position and rate', () => {
    const { nav, ms } = fakeNav();
    expect(publishPosition(1000, 250, 1.5, nav)).toBe(true);
    expect(ms.setPositionState).toHaveBeenCalledWith({ duration: 1000, position: 250, playbackRate: 1.5 });
  });

  // ⚠️ setPositionState THROWS on nonsense and browsers disagree about what
  // counts — so it is validated BEFORE the call, not caught after.
  it('refuses input that would make it throw', () => {
    const { nav, ms } = fakeNav();
    expect(publishPosition(NaN, 0, 1, nav)).toBe(false);
    expect(publishPosition(0, 0, 1, nav)).toBe(false);        // no duration yet
    expect(publishPosition(Infinity, 0, 1, nav)).toBe(false); // a loading stream
    expect(publishPosition(100, 200, 1, nav)).toBe(false);    // past the end
    expect(publishPosition(100, -1, 1, nav)).toBe(false);
    expect(publishPosition(100, 50, 0, nav)).toBe(false);     // a zero rate
    expect(ms.setPositionState).not.toHaveBeenCalled();
  });

  it('answers false where the browser has no setPositionState', () => {
    const { nav } = fakeNav({ noPositionState: true });
    expect(publishPosition(100, 50, 1, nav)).toBe(false);
  });
});

describe('publishPlaybackState', () => {
  it('tells the OS whether sound is coming out', () => {
    const { nav, ms } = fakeNav();
    publishPlaybackState('playing', nav);
    expect(ms.playbackState).toBe('playing');
  });

  it('does not throw on a browser without a mediaSession', () => {
    expect(publishPlaybackState('playing', {})).toBe(false);
  });
});

describe('teardown', () => {
  // ⚠️ A stale handler holding a closure over a dead <audio> element is a
  // lock-screen play button that does nothing — the estate's forbidden
  // silent-dead-control, in the one place there is no page to explain it on.
  it('clears every handler, including ones the browser refuses', () => {
    const cleared = [];
    const nav = {
      mediaSession: {
        setActionHandler(action, fn) {
          if (action === 'stop') throw new TypeError('unsupported');
          if (fn === null) cleared.push(action);
        },
      },
    };
    expect(() => teardown(nav)).not.toThrow();
    expect(cleared).toContain('play');
    expect(cleared).toContain('seekto');
    expect(cleared).toContain('nexttrack');
  });

  it('does not throw on a browser without a mediaSession', () => {
    expect(() => teardown({})).not.toThrow();
  });
});
