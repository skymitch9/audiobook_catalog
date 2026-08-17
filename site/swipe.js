/**
 * swipe.js — "was that a page turn, or was it something else?"
 *
 * Owner, 2026-08-17: *"we need a way to swipe to change pages."*
 *
 * ⚠️ THE DECISION IS PURE AND THE WIRING IS THIN, on purpose. `swipeIntent()`
 * takes four numbers and answers `'next' | 'prev' | null`, so every one of the
 * ways a swipe is WRONG can be exercised in `site/__tests__/swipe.test.js`
 * rather than by flicking a phone. Nothing in a browser can be unit-tested;
 * the arithmetic that decides whether a gesture was a turn absolutely can.
 *
 * ## ⚠️ THE FOUR THINGS A NAIVE THRESHOLD GETS WRONG
 *
 * 1. **A vertical scroll has horizontal drift.** A thumb travelling down a
 *    long page moves sideways too, and a bare `|dx| > 50` turns pages while
 *    somebody is scrolling. Hence AXIS_RATIO: the horizontal component has to
 *    beat the vertical one by half again before it counts as intent.
 * 2. **A pinch-zoom is two fingers and a lot of movement.** On a zoomed PDF
 *    the fingers separate horizontally, which is a textbook false turn. Any
 *    second touch cancels the gesture outright (`multitouch`), and a page left
 *    zoomed in (`scale > 1`) refuses turns entirely — at that magnification a
 *    horizontal drag is a PAN, and the reader means to see the right-hand
 *    margin, not the next page.
 * 3. **A slow drag is not a swipe.** Holding a finger down for two seconds and
 *    releasing 60px away is a fumble, a text selection or a scroll that
 *    stalled. MAX_MS keeps it from turning a page.
 * 4. **A pannable page is not a swipeable one.** When the rendered PDF is
 *    wider than its stage, the horizontal axis already belongs to scrolling.
 *    `axisTaken` is how the caller says so, and the reader passes it whenever
 *    `scrollWidth > clientWidth`.
 *
 * ## ⚠️ WHAT WIRING THIS DOES *NOT* DO, and it is the load-bearing part
 *
 * `wireSwipe()` NEVER turns a page itself. It calls the `onNext` / `onPrev` it
 * was handed, and the reader hands it `goNext` / `goPrev` — the SAME two
 * functions the arrows and the keyboard use, which are the functions that run
 * `recordPdfPosition()` / raise foliate's `relocate`.
 *
 * ⚠️ A page-turn path that does not go through those stops saving the reader's
 * spot, silently, with nothing anywhere saying so — the exact failure
 * `site/reading-position.js` §7.6 was written after. Do not "optimise" this by
 * calling `drawPage()` or `view.next()` from here.
 */

/** Minimum horizontal travel, in CSS px, before a gesture is a turn at all. */
export const SWIPE_MIN_PX = 48;
/** Longer than this and it was a drag, not a swipe. */
export const SWIPE_MAX_MS = 900;
/** |dx| must beat |dy| by this much — the vertical-scroll guard. */
export const SWIPE_AXIS_RATIO = 1.5;
/** visualViewport.scale above this means the reader is panning, not turning. */
export const SWIPE_MAX_SCALE = 1.05;

/**
 * Did that gesture mean "next page", "previous page", or nothing?
 *
 * @param {{dx:number, dy:number, dt:number,
 *          scale?:number, multitouch?:boolean, axisTaken?:boolean}} g
 * @returns {'next'|'prev'|null}
 */
export function swipeIntent(g) {
  // ⚠️ The absent-argument guard is FIRST, and it caught a real defect the
  // moment the test ran: `Number(g && g.dx)` on a null `g` is `Number(null)`,
  // which is 0 — finite, so the finiteness check below waved it through and
  // the next line threw on `g.multitouch`. A missing gesture must be "no
  // turn", never an exception inside a touch handler.
  if (!g || typeof g !== 'object') return null;
  const dx = Number(g.dx);
  const dy = Number(g.dy);
  const dt = Number(g.dt);
  if (!Number.isFinite(dx) || !Number.isFinite(dy) || !Number.isFinite(dt)) return null;
  // A second finger at ANY point in the gesture: a pinch, a two-finger scroll,
  // or a palm. None of them is a page turn.
  if (g.multitouch) return null;
  // The page is magnified — horizontal movement is a pan and belongs to the
  // page, not to the pager.
  const scale = Number.isFinite(Number(g.scale)) ? Number(g.scale) : 1;
  if (scale > SWIPE_MAX_SCALE) return null;
  // The caller owns the horizontal axis (a PDF wider than its stage).
  if (g.axisTaken) return null;
  if (dt < 0 || dt > SWIPE_MAX_MS) return null;
  const ax = Math.abs(dx);
  if (ax < SWIPE_MIN_PX) return null;
  if (ax < Math.abs(dy) * SWIPE_AXIS_RATIO) return null;
  // Swipe LEFT (finger moves toward -x) reveals what is to the right: next.
  return dx < 0 ? 'next' : 'prev';
}

/**
 * Attach the gesture to an element (or a document — see the FXL note in
 * reader.js: a fixed-layout EPUB's pages live in iframes, and touches inside
 * an iframe never reach the parent, so the listeners have to go on the
 * iframe's OWN document, which foliate hands out via its `load` event).
 *
 * ⚠️ Every listener is `passive: true`. This never calls `preventDefault()`,
 * so scrolling, pinching and text selection all keep working exactly as they
 * did; the gesture only ever ADDS a turn when nothing else claimed the motion.
 *
 * @param {EventTarget} target
 * @param {{onNext:Function, onPrev:Function, axisTaken?: () => boolean}} cfg
 * @returns {() => void} detach
 */
export function wireSwipe(target, cfg) {
  if (!target || typeof target.addEventListener !== 'function') return () => {};
  let start = null;

  const onStart = (ev) => {
    const touches = ev.touches || [];
    if (touches.length !== 1) { start = null; return; }
    const t = touches[0];
    start = { x: t.clientX, y: t.clientY, t: ev.timeStamp, multitouch: false };
  };
  const onMove = (ev) => {
    if (!start) return;
    if ((ev.touches || []).length > 1) start.multitouch = true;
  };
  const onEnd = (ev) => {
    const s = start;
    start = null;
    if (!s) return;
    const t = (ev.changedTouches || [])[0];
    if (!t) return;
    const intent = swipeIntent({
      dx: t.clientX - s.x,
      dy: t.clientY - s.y,
      dt: ev.timeStamp - s.t,
      multitouch: s.multitouch,
      scale: (typeof window !== 'undefined' && window.visualViewport)
        ? window.visualViewport.scale : 1,
      axisTaken: typeof cfg.axisTaken === 'function' ? !!cfg.axisTaken() : false,
    });
    if (intent === 'next') cfg.onNext();
    else if (intent === 'prev') cfg.onPrev();
  };
  const onCancel = () => { start = null; };

  const opts = { passive: true };
  target.addEventListener('touchstart', onStart, opts);
  target.addEventListener('touchmove', onMove, opts);
  target.addEventListener('touchend', onEnd, opts);
  target.addEventListener('touchcancel', onCancel, opts);

  return () => {
    target.removeEventListener('touchstart', onStart, opts);
    target.removeEventListener('touchmove', onMove, opts);
    target.removeEventListener('touchend', onEnd, opts);
    target.removeEventListener('touchcancel', onCancel, opts);
  };
}
