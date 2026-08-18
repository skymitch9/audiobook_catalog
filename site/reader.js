/**
 * reader.js — the in-browser PDF reader's whole brain (viewer phase 1b).
 *
 * Loaded as a module by `read.html`. ⚠️ IT IS AN EXTERNAL FILE ON PURPOSE:
 * `/read`'s Content-Security-Policy (site/_headers) is `script-src 'self'` plus
 * gstatic for the Firebase SDK, with no `'unsafe-inline'`, so this logic could
 * not live in a <script> block in the page even if that were tidier. Moving it
 * inline breaks the page, silently, only in production where the header is
 * applied. (`ebook-notes.js` and `identity.js` sit beside this for the same
 * kind of reason.)
 *
 * ## What it does, in order
 *
 *   1. reads `?b=<anchor>` — the manifest's own anchor, never recomputed here
 *      (ONE implementation, in build_ebook_manifest.ebook_anchor);
 *   2. brings up a live Firebase session (identity.js v2 keeps one; the shelf
 *      does exactly this);
 *   3. fetches the GATED manifest to learn what this anchor names — the same
 *      call the shelf makes, so the two can never disagree about a book;
 *   4. hands pdf.js a URL on audiobook-api.heygabi.ai **plus an
 *      `Authorization` header**, and lets it range-stream.
 *
 * ## ⚠️ The three things that will bite whoever edits this next
 *
 * **1. `httpHeaders`, not a URL parameter.** The bearer rides in a header on
 * every range request. A signed URL would be the credential — surviving in
 * history, referrers, screenshots and any log that records request lines — and
 * could not be revoked mid-session, which is the estate's whole revocation
 * promise. `httpHeaders` reaching a real `Headers` object was VERIFIED against
 * the vendored bytes (pdfjs-dist 5.4.149; see site/static/pdfjs/VENDORED.md).
 *
 * **2. `disableAutoFetch` AND `disableStream` are BOTH required, and the
 * design only asked for one.** Measured 2026-08-17 on the real 181 MiB
 * Stormlight handbook: with `disableStream: false` (viewer design §5.1's
 * stated value) pdf.js opens a full-file GET beside its ranges and runs it to
 * completion — **all 189,930,310 bytes**, `disableAutoFetch` notwithstanding.
 * With `disableStream: true` the same GET is aborted after 655,360 B. That is
 * ~2.5 MiB to open the book instead of ~183 MiB, for a byte-identical render.
 * The two flags do different jobs: autoFetch governs speculative fetching of
 * the REST of the document; stream governs the whole-file read opened at the
 * start. See the full numbers at the `disableStream` line below.
 *
 * **3. ONE canvas is live at a time, by construction.** Memory in pdf.js is
 * per RENDERED PAGE, not per file: a rendered A4 page at 2× DPR is ~15–25 MB
 * of canvas, so the cap is how many pages are attached, not how big the book
 * is. A 181 MiB image-heavy RPG handbook is fine; forty attached canvases are
 * not. Any future continuous-scroll mode must window the page list.
 *
 * ## The EPUB half — viewer phase 2, 2026-08-17
 *
 * `openBook()` now has two arms. The EPUB one is foliate-js, vendored and
 * pinned, reading the SAME gated byte stream **over HTTP ranges**: the 393 MiB
 * White Sand Omnibus opens in 18 requests totalling 664,477 B — 0.16% of the
 * file — at 16.6 MB of peak heap. The renderer choice and the reason it is not
 * epub.js are in `EPUB_SEAM` below; the trap that undoes all of it is in
 * `site/epub-loader.js`'s header and it is one function call wide.
 *
 * ⚠️ A fourth thing that will bite whoever edits this next: **the EPUB path
 * re-asks for its token on every range**, where pdf.js captures headers once.
 * Do not "harmonise" them by capturing the EPUB one — that would re-introduce
 * the hour-long session expiry, not remove a difference.
 *
 * ## ⚠️ 5. TOKENS COME FROM `identity.getIdToken(app)`, NEVER FROM THE USER
 *
 * `getLiveUser()` answers a flat SNAPSHOT — `{uid, email, displayName}` — and
 * **has no `getIdToken` method.** That is deliberate on identity.js's side:
 * handing the live Firebase `User` to every caller is how a page ends up
 * minting credentials in places nobody audits.
 *
 * Phase 1b called `user.getIdToken()` on that snapshot. It threw
 * `TypeError: user.getIdToken is not a function` **for every signed-in
 * reader**, and the surrounding catch reported it as *"The shelf did not
 * answer"* — an OUTAGE sentence for something that was not an outage, and the
 * mislabelling ROLES.md §1e exists to forbid. Nothing caught it: every test
 * and every agent check was the signed-out half, where the code never runs.
 * Found on 2026-08-17 by opening the live dev lane in a signed-in browser.
 *
 * The fix is the token getter the viewer design asked for in advance
 * (`identity.js`'s `getIdToken(app, force)`), and it answers `null` rather
 * than throwing, so "not signed in" is a state this page WORDS instead of an
 * error it mistakes for an outage. ⚠️ If you see `user.getIdToken` reappear
 * here, it is this bug coming back; `tests/test_reader_page.py` fails on it.
 *
 * ## 6. SAVE YOUR SPOT — viewer phase 3, 2026-08-17
 *
 * Owner: *"for reading ebooks we also need to have it save your spot. this
 * will be so important for pwa."* The store, the key, the two-tier cache and
 * the reason a position is NOT keyed on the anchor all live in
 * `site/reading-position.js`; read its header before touching any of this.
 * What THIS file owns is the three joins:
 *
 *   1. The first paint NEVER waits for the network. The per-device
 *      localStorage row is read synchronously and the book opens THERE.
 *      Firestore's answer arrives afterwards and is reconciled — and when it
 *      is newer AND somewhere else, it is OFFERED ("Jump / Stay"), never
 *      applied under the reader's fingers.
 *   2. A book that FAILED TO OPEN records nothing. The keeper is armed only
 *      after a first successful render, so a broken file, a lapsed token or a
 *      refused range can never overwrite a good position with page 1.
 *   3. A STALE LOCATOR falls back to the start, never to a broken page. A PDF
 *      page number is clamped to the document; a CFI is handed to foliate
 *      AFTER `init()` has already put a readable page on screen, so a locator
 *      that no longer resolves leaves the reader at the text start with a
 *      console warning rather than an unopened book.
 *
 * ## 7. READING MODES + SWIPE + THE DEV CURTAIN — 2026-08-17
 *
 * Three owner asks landed together, and each has one thing worth knowing here.
 *
 * **Reading modes** (*"set the pages white and the font black or vice versa as
 * well as make it match the theme"*). The choice lives in `site/read-mode.js`,
 * a classic script in `<head>` so the stamp beats first paint; this file only
 * REACTS to it, and only where CSS cannot reach:
 *   - a reflowable EPUB is inside a shadow-rooted iframe, so its colours have
 *     to be pushed through `renderer.setStyles()` — `applyReadingMode()`;
 *   - a PDF is a canvas and is inverted by a CSS filter the page owns, so this
 *     file contributes only the honest SENTENCE about what an inversion does;
 *   - ⚠️ a FIXED-LAYOUT EPUB cannot be recoloured at all (`<foliate-fxl>` has
 *     no `setStyles`, measured at phase 2 — calling it THREW and cost a book
 *     that opens perfectly its "would not open" refusal), so it gets the
 *     chrome and a sentence. Stated, never silently skipped.
 *
 * **Swipe to turn** (*"we need a way to swipe to change pages"*). ⚠️ THE
 * MEASUREMENT THAT SHAPED IT: `site/static/foliate/paginator.js` ALREADY
 * carries touch handling — `#onTouchStart/#onTouchMove/#onTouchEnd` bound both
 * on the renderer and on every section document, `snap()` on release, and a
 * `visualViewport.scale > 1` pinch guard. A reflowable EPUB therefore swipes
 * with no code here at all, and adding a second handler would turn TWO pages
 * per flick. So this file wires only the two stages foliate does not cover:
 * the PDF canvas, and a fixed-layout EPUB's iframes (which `foliate-fxl` leaves
 * bare — grep it for `touch`; there is nothing).
 *
 * ⚠️ AND EVERY TURN GOES THROUGH `goNext`/`goPrev`, never through `drawPage()`
 * or `view.next()` directly. Those two are what run `recordPdfPosition()` and
 * what make foliate raise `relocate` → `recordEpubPosition()`. A turn path that
 * bypasses them stops saving the reader's spot, silently, which is exactly the
 * failure §7.6 of `docs/info/reader-page.md` was written after.
 *
 * ## ⚠️ 8. THE P1 THAT REWROTE THE FAILURE PATHS — 2026-08-18
 *
 * **Symptom.** iPhone Safari, PROD `/read`, signed in, an ordinary reflowable
 * EPUB. The whole chrome rendered — masthead, resolved title, the Page control,
 * both arrows, the footer — and the book area was an EMPTY BORDERED BOX. No
 * error anywhere on the page. Desktop Chrome rendered the same book perfectly,
 * which is why every review before it passed.
 *
 * **Cause.** `frame-ancestors 'none'` in `site/_headers`. A `blob:` document
 * inherits its creator's CSP, and WebKit then enforces `frame-ancestors` on it
 * — so the reader was refused permission to frame the `blob:` iframe FOLIATE
 * ITSELF creates for every section. ⚠️ Chromium does not enforce it there. The
 * fix is one word (`'self'`), it costs no security (see that file's comment),
 * and it is a promote away from production.
 *
 * **⚠️ But the cause is not the lesson, and this section is not about the CSP.**
 * The throw landed inside `paginator.js`'s iframe `load` LISTENER, so the
 * promise this file was awaiting was neither resolved nor rejected:
 * `await view.init()` hung forever. Every carefully-worded catch in `openEpub`
 * was on the wrong side of it. `closed()` was never called. The reader waited,
 * in silence, for a page that was never coming — and the owner had a screenshot
 * of an empty rectangle and nothing whatsoever to tell us.
 *
 * So the reader now has three things it did not have, and they ship whether or
 * not any particular bug is found:
 *
 *   1. **`window.onerror` + `unhandledrejection`** (`wireGlobalFailureReporting`,
 *      installed FIRST). The only mechanism that could have caught the above,
 *      because no try/catch of ours was ever on that stack.
 *   2. **`fault()`** — a worded panel in the BOOK AREA (`closed()`'s gate card
 *      cannot go there), carrying the error's own message and first stack frame
 *      in selectable monospace. ⚠️ Shown, not hidden in a console: iOS has no
 *      console to open, and the whole point is a sentence somebody can read
 *      aloud down a phone.
 *   3. **`armWatchdog()`** — because the failure produced no error in the chain
 *      at all. A book that neither opens nor fails inside `OPEN_DEADLINE_MS`
 *      IS a failure, and it reports the last step reached (`step()`), which is
 *      the most useful single fact in any account of a hang.
 *
 * ⚠️ And `fault()` NEVER blanks a book that is already rendering (`state.
 * rendered`). A stray error must not cost somebody the page they are reading.
 *
 * The same P1 also moved `#rd-busy` out of `#rd-stage`, which `openEpub()`
 * HIDES — so the entire EPUB open had been running with no spinner, and a slow
 * open was pixel-identical to a dead one. See `#rd-view` in `read.html`.
 *
 * **The dev curtain** (*"i need a way in the estate to manage dev access for
 * ebook … make devops always able to see dev envs"*). ⚠️ A CURTAIN, NOT A LOCK
 * — see `site/dev-lane.js`, which owns every decision. It runs on the `/dev/`
 * lane only, it never touches the promoted path, and the books stay gated by
 * `vis_ebooks` server-side on both lanes regardless of what it decides.
 */

import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-app.js';
import { getFirestore } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { FIREBASE_CONFIG } from './fb-env.js';
import { mountAccountModal } from './account-modal.js';
import { getLiveUser, getIdToken, signInWithGoogle, handleRedirectResult } from './identity.js';
// ⚠️ ONE implementation of "which title identifies this ebook across the
// estate", imported rather than re-derived. The shelf's content notes already
// answer it, and its header explains why keying on the epub's own title fails
// silently in both directions.
import { warningTitleFor } from './ebook-notes.js';
import {
  createPositionKeeper,
  describeDevice,
  describePosition,
  loadLocal,
  loadRemote,
  newerOf,
  samePlace,
} from './reading-position.js';
// ⚠️ The gesture's DECISION is pure and lives there, so every way a swipe is
// wrong (a vertical scroll's drift, a pinch, a slow drag, a pannable zoomed
// page) is exercised by site/__tests__/swipe.test.js instead of by a thumb.
import { wireSwipe } from './swipe.js';
// ⚠️ CURTAIN, NOT LOCK — that module's header says so at length and this file
// takes no view of its own. The real lock is `vis_ebooks`, server-side, on
// every manifest and every range, on BOTH lanes.
import { DEV_CURTAIN, devLaneVerdict } from './dev-lane.js';

/** The gated manifest — the same URL, and the same gate, the shelf uses. */
const MANIFEST_URL = 'https://audiobook-api.heygabi.ai/api/ebooks/manifest';
/** The gated byte stream. ⚠️ `:anchor` is substituted, nothing else. */
const FILE_URL = (anchor) =>
  `https://audiobook-api.heygabi.ai/api/ebook/${encodeURIComponent(anchor)}/file`;

/**
 * ⚠️ THE EPUB SEAM — CLOSED at viewer phase 2, 2026-08-17. Kept, because what
 * it warned about is now what the code does, and the reasons still bite.
 *
 * The seam was one branch in `openBook()`'s format switch. It now dispatches to
 * `openEpub()` beside `openPdf()`, and everything else in this file — sign-in,
 * the manifest lookup, the anchor contract, `describeFetchFailure()` — was
 * already format-agnostic and did not change.
 *
 * WHAT SHIPPED, against what the seam asked for:
 *
 *   - ✅ foliate-js, NOT epub.js, vendored at commit
 *     `78914aef4466eb960965702401634c2cb348e9b1` with @zip.js/zip.js 2.7.45
 *     (site/static/foliate/VENDORED.md, site/static/zipjs/VENDORED.md).
 *     Measured on the 393 MiB White Sand Omnibus: epub.js fetched
 *     412,436,591 B into 1,207 MB of JS heap; this reader opens the same book
 *     in 18 range requests totalling 664,477 B at 16.6 MB of peak heap.
 *   - ✅ the range-reading loader is injected DELIBERATELY, in
 *     `site/epub-loader.js`. ⚠️ foliate's own `view.js` `makeBook()` builds
 *     `new ZipReader(new BlobReader(file))` over a whole in-memory Blob; this
 *     reader never calls it, and foliate's `vendor/zip.js` is deliberately NOT
 *     vendored so that path cannot even resolve. Read that file's header before
 *     touching any of it.
 *   - ✅ no 32 MiB size gate and no "too large" refusal card. All three
 *     oversized books open; the gate would have been dead code.
 *   - ⚠️ `blob:` in `img-src`/`frame-src` was already there and was NOT enough.
 *     `style-src` and `font-src` needed it too, and the phase-1 note saying
 *     this file's CSP would not have to change was wrong by those two
 *     directives. Measured both ways: without them a book paginates perfectly
 *     in the browser's default serif with all of its own typography discarded,
 *     and the page's own `securitypolicyviolation` listener never hears it,
 *     because the section is a blob: iframe inside a CLOSED shadow root. See
 *     site/_headers.
 *
 * ✅ CLOSED at viewer phase 3, 2026-08-17: the position IS stored now, and the
 * CFI it stores is produced by this renderer — which is exactly why phase 2
 * settled the renderer first. See `site/reading-position.js`.
 */
const EPUB_SEAM =
  'This reader cannot open that format — only EPUB and PDF. This book is on the shelf but not readable here.';

/* ── page furniture ─────────────────────────────────────────────────────── */

const el = (id) => document.getElementById(id);
const gateEl = el('rd-gate');
const gateTitleEl = el('rd-gate-title');
const gateWhyEl = el('rd-gate-why');
const gateBtnEl = el('rd-gate-signin');
const gateBackEl = el('rd-gate-back');
/** The curtain's way OUT of the dev lane — absolute, unlike everything else. */
const gateProdEl = el('rd-gate-prod');
const shellEl = el('rd-shell');
const canvasEl = el('rd-canvas');
const stageEl = el('rd-stage');
const titleEl = el('rd-title');
const pageNowEl = el('rd-page-now');
const pageTotalEl = el('rd-page-total');
const prevEl = el('rd-prev');
const nextEl = el('rd-next');
const zoomInEl = el('rd-zoom-in');
const zoomOutEl = el('rd-zoom-out');
const busyEl = el('rd-busy');
const bookEl = el('rd-book');
const pagerPdfEl = el('rd-pager-pdf');
const pagerEpubEl = el('rd-pager-epub');
const locEl = el('rd-loc');
/** The honest line about what a display mode can and cannot do to this book. */
const modeNoteEl = el('rd-mode-note');
// The "you were somewhere else" offer — see offerJump(). Ships hidden.
const resumeEl = el('rd-resume');
const resumeWhyEl = el('rd-resume-why');
const resumeJumpEl = el('rd-resume-jump');
const resumeStayEl = el('rd-resume-stay');
// The fault panel — see fault() and §8. Ships hidden.
const faultEl = el('rd-fault');
const faultTitleEl = el('rd-fault-title');
const faultWhyEl = el('rd-fault-why');
const faultDetailEl = el('rd-fault-detail');
const faultRetryEl = el('rd-fault-retry');

/**
 * Show a closed/failed state. ⚠️ Every one says what happened, what it needs
 * and how to get it — never a bare status, never a raw body, never a dead
 * page (ROLES.md §1e). `signIn` draws the button; `back` draws the way home,
 * which every non-auth failure gets because a reader who cannot read this book
 * should still be one click from the shelf.
 */
function closed(title, why, opts = {}) {
  // ⚠️ A worded refusal disarms the watchdog and clears any fault panel. Two
  // explanations of the same failure, one of them a timeout, is worse than one.
  clearWatchdog();
  clearFault();
  gateTitleEl.textContent = title;
  gateWhyEl.textContent = why;
  gateBtnEl.hidden = !opts.signIn;
  gateBackEl.hidden = opts.back === false;
  // ⚠️ Only the dev curtain sets this. The shelf link above is RELATIVE, which
  // on `/dev/read` means `/dev/ebooks` — the page that just refused them. A
  // curtained reader needs the promoted site, and nothing else does.
  if (gateProdEl) gateProdEl.hidden = !opts.prod;
  gateEl.hidden = false;
  shellEl.hidden = true;
  busy(false);
}

function busy(on) {
  busyEl.hidden = !on;
}

/* ── §8. THE READER MUST NEVER FAIL SILENTLY ────────────────────────────── */

/**
 * ⚠️ WHY THIS EXISTS, AND WHY IT SHIPS EVEN WHEN NOTHING IS BROKEN.
 *
 * 2026-08-18, the P1 that wrote this section. iPhone Safari, PROD `/read`,
 * signed in, an ordinary reflowable EPUB. The page rendered its ENTIRE chrome —
 * masthead, resolved title, the Page control, both arrows, the footer — and an
 * EMPTY BORDERED BOX where the book goes. No error anywhere. Desktop Chrome
 * rendered the same book perfectly.
 *
 * The cause was `frame-ancestors 'none'` in `site/_headers` (its comment there
 * has the whole measurement): WebKit inherits this page's CSP into the `blob:`
 * iframe foliate makes for each section and then enforces `frame-ancestors` on
 * it, so the reader was refused permission to frame its own blob. Chromium does
 * not do this, which is why every desktop review passed.
 *
 * ⚠️ BUT THE CSP IS NOT THE LESSON. THIS IS: the throw happened inside
 * `paginator.js`'s iframe `load` LISTENER —
 *
 *     new Promise(resolve => { iframe.addEventListener('load', () => {
 *         const doc = this.document          // null: opaque sandboxed frame
 *         afterLoad?.(doc)                   // TypeError, thrown IN a listener
 *         ... resolve()                      // never reached
 *     }); iframe.src = src })
 *
 * — so the promise `openEpub()` was awaiting was **never resolved AND never
 * rejected**. `await view.init()` hung forever. Not one of this file's careful
 * catch blocks could ever have run; `closed()` was never called; the reader sat
 * waiting for a page that was not coming. The owner had a screenshot of an
 * empty rectangle and nothing else to tell us, and a ten-minute fix cost a day.
 *
 * So this section adds the three things that turn that shape into a sentence:
 *
 *   1. `window.onerror` + `unhandledrejection` — a throw that escapes every
 *      promise chain still reaches the page. This is the ONLY thing that would
 *      have caught the bug above.
 *   2. `fault()` — a worded panel IN THE BOOK AREA, carrying the error's own
 *      message and its first stack frame in selectable monospace. ⚠️ The
 *      technical line is shown ON PURPOSE, not hidden behind a console: on iOS
 *      there is no console to open, and "something went wrong" from another
 *      room is worth nothing. It exists to be read aloud down a phone.
 *   3. `armWatchdog()` — because the failure above produced NO error inside the
 *      promise chain at all. A book that neither opens nor fails within
 *      `OPEN_DEADLINE_MS` is itself a failure, and it names the last step the
 *      reader reached (`state.step`), which is the single most useful fact in
 *      any report of it.
 *
 * ⚠️ AND IT NEVER DESTROYS A WORKING READ. Once a page has genuinely rendered
 * (`state.rendered`), a later window error shows the panel WITHOUT hiding the
 * book — the same rule the position keeper follows. A stray error from an
 * unrelated script must not blank a book somebody is reading.
 */

/** How long a book may take to open before silence is itself the failure. */
const OPEN_DEADLINE_MS = 25000;

/**
 * The error's own words, trimmed to something a person can read out.
 *
 * ⚠️ Message AND the first stack frame, and no more. The message alone is
 * frequently useless ("null is not an object"); the frame is what names the
 * file and line, which is what turns a report into a fix. A whole stack is
 * unreadable on a phone and the tail of it is never the interesting part.
 */
function describeError(err) {
  if (err == null) return '';
  if (typeof err === 'string') return err;
  const name = err.name && err.name !== 'Error' ? `${err.name}: ` : '';
  const msg = err.message || String(err);
  const frame = String(err.stack || '')
    .split('\n')
    .map((s) => s.trim())
    .find((s) => s && !s.includes(msg) && /[(@]|^at /.test(s));
  return frame ? `${name}${msg}\n${frame}` : `${name}${msg}`;
}

/**
 * Show a failure IN THE BOOK AREA, in words, with the technical line.
 *
 * `closed()` is for a door that never opened — a gate card, before the shell.
 * This is for a reader that IS on screen and cannot show a book, which
 * `closed()` cannot express: it hides the shell, and hiding a shell the reader
 * is already looking at reads as the page breaking a second time.
 *
 * @param {string} title  what failed, as a heading
 * @param {string} why    what to do about it, in a sentence
 * @param {*} [err]       the error itself; its words go in the detail block
 * @param {{keepBook?: boolean, firstWins?: boolean}} [opts] `keepBook` leaves a
 *        rendered book on screen — used for anything that happens AFTER a
 *        successful render, where blanking the page would be the worse failure.
 *        ⚠️ `firstWins` keeps a panel that is already up, and the global
 *        reporter sets it: one blocked frame produced THREE errors in a row
 *        (`doc.head`, then a security error, then `doc.documentElement`), and
 *        the LAST one is the least informative — it is a knock-on of the first.
 *        Whoever reads the line down a phone should get the earliest failure,
 *        not the final splash.
 */
function fault(title, why, err, opts = {}) {
  if (!faultEl) return;
  if (opts.firstWins && !faultEl.hidden) {
    console.warn('[reader] a later failure, not shown (the first one is up):', err ?? '');
    return;
  }
  clearWatchdog();
  busy(false);
  faultTitleEl.textContent = title;
  faultWhyEl.textContent = why;
  const detail = describeError(err);
  faultDetailEl.textContent = detail;
  faultDetailEl.hidden = !detail;
  faultEl.hidden = false;
  if (!opts.keepBook) {
    stageEl.hidden = true;
    bookEl.hidden = true;
  }
  // The console still gets it, for anyone who has one.
  console.warn(`[reader] ${title}`, err ?? '');
}

/** Clear the panel — a retry, or a second book, starts from a clean page. */
function clearFault() {
  if (faultEl) faultEl.hidden = true;
}

/* ── the watchdog ───────────────────────────────────────────────────────── */

/**
 * ⚠️ THE ONE THAT CATCHES A HANG, which is the shape no catch block can see.
 * Armed when a book starts opening, disarmed by the first successful render or
 * by any worded failure. If it fires, the reader says so and names the last
 * step it reached — see `step()`.
 */
let watchdogTimer = null;
function armWatchdog() {
  clearWatchdog();
  watchdogTimer = setTimeout(() => {
    watchdogTimer = null;
    fault(
      'This book has not opened',
      'The reader started opening it and then stopped, without succeeding and '
      + 'without failing. That is a problem on our side, not a decision about '
      + 'your account — try again, and if it keeps happening send Mitch the '
      + 'line below and the book’s name.',
      // ⚠️ A STRING, not an Error. A synthesised Error's stack points at this
      // timer, which is the one place in the codebase guaranteed NOT to be
      // where the trouble is — pure noise in the line somebody reads out.
      `the reader got as far as: ${state.step || 'starting up'}`,
    );
  }, OPEN_DEADLINE_MS);
}
function clearWatchdog() {
  if (watchdogTimer) clearTimeout(watchdogTimer);
  watchdogTimer = null;
}

/**
 * Record where the reader has got to, in the words a person would use.
 *
 * ⚠️ This is not logging for its own sake. It is the payload of the watchdog
 * message, and it is the difference between "the book did not open" and "the
 * reader got as far as drawing the first page" — one of which is a mystery and
 * one of which is a bug report.
 */
function step(what) {
  state.step = what;
  console.info(`[reader] ${what}`);
}

/**
 * ⚠️ THE BACKSTOP THAT WOULD HAVE CAUGHT THE 2026-08-18 P1, and the reason it
 * listens at the WINDOW rather than wrapping more code in try/catch: the throw
 * that broke the reader happened inside a third-party iframe `load` listener,
 * where no try/catch of ours is on the stack. A window-level listener is the
 * only place it surfaces.
 *
 * Both events, deliberately: `error` for a synchronous throw that escapes, and
 * `unhandledrejection` for a promise nobody caught (this file has several
 * fire-and-forget chains, and `boot()` itself is one).
 */
function wireGlobalFailureReporting() {
  const report = (err, kind) => {
    // A book already on screen is never blanked by a later error.
    const keepBook = state.rendered;
    fault(
      keepBook ? 'Something went wrong while reading' : 'The reader hit a problem',
      keepBook
        ? 'The book is still on screen and you can carry on. If it starts '
          + 'misbehaving, reload the page — and send Mitch the line below.'
        : 'The reader could not finish opening this book. That is a fault on '
          + 'our side, not a decision about your account — try again, and if it '
          + 'keeps happening send Mitch the line below and the book’s name.',
      err,
      { keepBook, firstWins: true },
    );
    console.warn(`[reader] unhandled ${kind}:`, err);
  };
  window.addEventListener('error', (ev) => {
    // ⚠️ A failed <img>/<script> load also fires `error` at the window, with no
    // `error` property. Those are resource problems, not page faults, and
    // blanking the reader for a missing cover would be its own bug.
    if (!ev || (!ev.error && !ev.message)) return;
    report(ev.error || new Error(ev.message), 'error');
  });
  window.addEventListener('unhandledrejection', (ev) => {
    report(ev?.reason ?? new Error('a promise was rejected with no reason'), 'rejection');
  });
}

/* ── the anchor ─────────────────────────────────────────────────────────── */

/**
 * ⚠️ The anchor is READ, never computed. It is
 * `"b-" + sha256(relative_path)[:12]`, folded in exactly one place
 * (`scripts/build_ebook_manifest.ebook_anchor`), and a second implementation in
 * JavaScript would break every deep link silently.
 *
 * `?b=` is the canonical form here rather than the shelf's `#anchor`, because a
 * fragment is not sent in a `Referer` and is invisible to anything that logs a
 * URL — which is a virtue on the shelf and a nuisance on a page whose whole job
 * is one book. The hash form is still accepted, so a pasted shelf link works.
 */
function anchorFromLocation() {
  const q = new URLSearchParams(window.location.search).get('b');
  if (q && q.trim()) return q.trim();
  const h = (window.location.hash || '').replace(/^#/, '').trim();
  return h || '';
}

/* ── refusals, mapped from the endpoint's own words ─────────────────────── */

/**
 * Turn a failed gated fetch into a sentence.
 *
 * ⚠️ The Worker writes the sentence for each distinct cause — not signed in,
 * awaiting approval, revoked, without the ebook grant, the file not uploaded,
 * paced — so this shows ITS words wherever it has them and invents nothing.
 * A page that made up a fifth meaning would be the mislabelled-outage failure
 * §1e names: telling someone to ask for access they already hold.
 */
function describeFetchFailure(status, said) {
  if (status === 401) {
    return { title: 'Your sign-in has lapsed', why: said || 'Sign in again to carry on reading.', signIn: true };
  }
  if (status === 403) {
    return { title: 'This book is not open to you', why: said || 'Ask Mitch to switch on “Ebooks” for your account.' };
  }
  if (status === 404) {
    return { title: 'That book is not readable', why: said || 'No book on the shelf matches this link. Open it from the shelf instead.' };
  }
  if (status === 429) {
    return { title: 'Slow down a moment', why: said || 'The shelf is pacing requests. Nothing is wrong with your account — wait a moment and try again.' };
  }
  if (status === 0) {
    // ⚠️ A network failure is NOT a permission failure. Mislabelling an outage
    // sends people asking for access they already have.
    return { title: 'The shelf did not answer', why: 'The reader could not reach the shelf’s server. This is an outage, not a permission decision — try again shortly.' };
  }
  return { title: 'The shelf did not answer', why: said || 'Something went wrong on the shelf’s server. Try again shortly.' };
}

/* ── pdf.js ─────────────────────────────────────────────────────────────── */

/**
 * ⚠️ Resolved against THIS MODULE's URL, not against the page's. The dev lane
 * is a PATH (`/dev/read`), not a host, so a root-absolute `/static/...` would
 * silently load the PROD copy of pdf.js while reviewing the dev lane — the
 * kind of bug that makes a fix look like it did not deploy.
 */
const asset = (rel) => new URL(rel, import.meta.url).href;

let pdfjsLib = null;

async function loadPdfJs() {
  if (pdfjsLib) return pdfjsLib;
  pdfjsLib = await import(asset('./static/pdfjs/build/pdf.min.js'));
  pdfjsLib.GlobalWorkerOptions.workerSrc = asset('./static/pdfjs/build/pdf.worker.min.js');
  return pdfjsLib;
}

/* ── the reader ─────────────────────────────────────────────────────────── */

const state = {
  /** 'pdf' | 'epub' | null — which renderer owns the shared toolbar. */
  mode: null,
  /** foliate's <foliate-view>, once an EPUB is open. */
  view: null,
  /** EPUB type size, as a multiplier on the book's own. */
  fontScale: 1,
  doc: null,
  page: 1,
  /** null = fit the stage's width; a number is an explicit user zoom. */
  scale: null,
  renderTask: null,
  /** The scale actually used last draw — the zoom buttons' starting point. */
  lastScale: null,
  /** Guards against two renders racing after fast page turns. */
  renderSeq: 0,
  /** The live session's uid, once boot() has one. Never from the mirror. */
  uid: null,
  /**
   * The last thing the reader started doing, in a person's words. ⚠️ This is
   * the watchdog's payload — see step() — and it is what makes a hang report
   * "the reader got as far as drawing the first page" rather than nothing.
   */
  step: null,
  /**
   * TRUE only once a page has genuinely been drawn. ⚠️ The same guard the
   * keeper uses, for the same reason in the other direction: before it, a
   * failure REPLACES the book area; after it, a failure must never blank a
   * book somebody is already reading.
   */
  rendered: false,
  /**
   * The reading-position keeper, once a book has actually rendered.
   * ⚠️ NULL UNTIL THEN, and that is the guard: nothing records a position for
   * a book that failed to open.
   */
  keeper: null,
};

/* ── save your spot ─────────────────────────────────────────────────────── */

/**
 * Offer the remote position rather than taking it. See reading-position.js §4:
 * a reader relocated without being asked is the single most common complaint
 * about every cross-device reader ever shipped, and this page has TWO stages
 * that could be yanked out from under someone mid-sentence.
 *
 * ⚠️ Non-blocking and dismissible. It appears above the book, it never covers
 * the text, and ignoring it entirely is a valid answer — "Stay" is the same
 * outcome as never touching it, said out loud so the bar can be dismissed.
 */
function offerJump(row, jump) {
  if (!resumeEl || !row) return;
  const where = describePosition(row);
  const device = (row.device || '').trim();
  resumeWhyEl.textContent = device
    ? `You were at ${where} on ${device}.`
    : `You were at ${where} on another device.`;
  resumeEl.hidden = false;
  const close = () => { resumeEl.hidden = true; };
  resumeJumpEl.onclick = () => { close(); void Promise.resolve(jump()).catch(() => {}); };
  resumeStayEl.onclick = close;
}

/**
 * Start tracking, and restore.
 *
 * The order here is the whole design and it is easy to get subtly wrong:
 *
 *   1. the LOCAL row is read synchronously and handed back to the caller,
 *      which opens the book there — no await, no network, no spinner;
 *   2. the keeper is created but NOT armed (the caller arms it once a page is
 *      genuinely on screen), so the restore itself records nothing and cannot
 *      make the local row look newer than the remote one it is about to be
 *      compared against;
 *   3. Firestore is asked in the background. If its answer is newer and
 *      elsewhere, it is offered; if there was no local row at all there is
 *      nothing to conflict with, so it is simply applied.
 *
 * ⚠️ Every failure path answers "no saved position" rather than an error. A
 * signed-out or storage-less visitor never reaches this page's shelf, but the
 * code must not throw if identity is absent — a bookmark that cannot be read
 * must never be the reason a BOOK does not open.
 *
 * @param {{book: object, anchor: string, format: string,
 *          apply: (row: object) => any}} cfg
 * @returns {{local: object|null}}
 */
function beginPositionTracking(cfg) {
  const uid = state.uid;
  // ⚠️ Not the anchor. reading-position.js §1 — and not a second copy of the
  // title join either: warningTitleFor() is the estate's one answer.
  const ident = warningTitleFor(cfg.book);
  const bookId = ident.bookId;
  if (!uid || !bookId) return { local: null };

  const local = loadLocal(bookId);
  state.keeper = createPositionKeeper({
    db,
    uid,
    bookId,
    anchor: cfg.anchor,
    format: cfg.format,
    title: ident.title,
    device: describeDevice(typeof navigator !== 'undefined' ? navigator.userAgent : ''),
  });

  loadRemote(db, uid, bookId).then((remote) => {
    if (!remote) return;
    if (newerOf(local, remote) !== remote) return;   // ours is at least as new
    if (samePlace(local, remote)) return;            // the same place, silently
    if (!local) return void Promise.resolve(cfg.apply(remote)).catch(() => {});
    offerJump(remote, () => cfg.apply(remote));
  }).catch(() => { /* no answer is "no saved position" */ });

  return { local };
}

/**
 * Flush on the way out.
 *
 * ⚠️ `pagehide` and `visibilitychange`, NOT `beforeunload`. A mobile browser
 * routinely kills a backgrounded tab without ever firing an unload event —
 * which is precisely the life a PWA reader lives, and precisely the case the
 * owner asked for this feature for. Both are fire-and-forget: nothing here may
 * delay a page going away.
 */
window.addEventListener('pagehide', () => { void state.keeper?.flush(); });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') void state.keeper?.flush();
});

/**
 * Render one page onto the one canvas.
 *
 * ⚠️ ONE canvas, one live render task, and the previous one is CANCELLED
 * rather than left running. Holding several rendered pages is what actually
 * costs memory here (see the module header), and an uncancelled task on a
 * fast page-turn draws the old page over the new one.
 */
async function drawPage(n) {
  if (!state.doc) return;
  const seq = (state.renderSeq += 1);
  state.page = Math.min(Math.max(1, n), state.doc.numPages);
  pageNowEl.value = String(state.page);
  prevEl.disabled = state.page <= 1;
  nextEl.disabled = state.page >= state.doc.numPages;
  busy(true);

  if (state.renderTask) {
    try { state.renderTask.cancel(); } catch { /* already finished */ }
    state.renderTask = null;
  }

  // Where we are, remembered. Local immediately, Firestore on a debounce, and
  // NOTHING at all until the keeper is armed (i.e. until a page has genuinely
  // rendered once) — see beginPositionTracking.
  recordPdfPosition();

  const page = await state.doc.getPage(state.page);
  if (seq !== state.renderSeq) return; // a newer turn won

  const unscaled = page.getViewport({ scale: 1 });
  // ⚠️ The device pixel ratio is CAPPED at 2. Big RPG PDFs are image-heavy, so
  // one page can decode to a large bitmap even when the file streams
  // beautifully; a 3× phone DPR on an A3 spread is a tab the OS kills.
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const fit = Math.max(0.2, (stageEl.clientWidth - 8) / unscaled.width);
  const scale = state.scale ?? Math.min(fit, 3);
  // Remembered so the zoom buttons start from what is actually on screen
  // rather than from 1, which would jump the page on the first press.
  state.lastScale = scale;
  const viewport = page.getViewport({ scale });

  canvasEl.width = Math.floor(viewport.width * dpr);
  canvasEl.height = Math.floor(viewport.height * dpr);
  canvasEl.style.width = `${Math.floor(viewport.width)}px`;
  canvasEl.style.height = `${Math.floor(viewport.height)}px`;

  const ctx = canvasEl.getContext('2d', { alpha: false });
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  // ⚠️ Paper is painted explicitly. A PDF page is transparent where it has no
  // content, and an unpainted canvas is BLACK in a dark-mode browser — which
  // reads as "the book failed to load", not as a rendering choice.
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, canvasEl.width, canvasEl.height);

  // The DPR scale rides in pdf.js's own `transform` (documented, pdf.mjs:12997)
  // rather than a pre-set canvas transform, so nothing this file does can be
  // multiplied by something the renderer does.
  state.renderTask = page.render({
    canvasContext: ctx,
    viewport,
    transform: dpr === 1 ? null : [dpr, 0, 0, dpr, 0, 0],
  });
  try {
    await state.renderTask.promise;
  } catch (e) {
    if (e && e.name === 'RenderingCancelledException') return;
    throw e;
  } finally {
    state.renderTask = null;
    if (seq === state.renderSeq) busy(false);
  }
}

/**
 * Open one book: gate → manifest → the right renderer.
 *
 * ⚠️ THE FORMAT SWITCH IS THE SEAM. Phase 2 adds an `epub` arm here and
 * nothing else in this file has to change.
 */
async function openBook(anchor) {
  // 1. What is this anchor? Asked of the GATED manifest, which is also the
  //    gate: an unauthorised reader never learns a book exists.
  let res;
  step('asking the shelf which book this link names');
  const token = await getIdToken(app);
  if (!token) {
    closed('Your sign-in has lapsed', 'Sign in again to open this book.', { signIn: true });
    return;
  }
  try {
    res = await fetch(MANIFEST_URL, { headers: { Authorization: `Bearer ${token}` } });
  } catch {
    const d = describeFetchFailure(0);
    closed(d.title, d.why);
    return;
  }
  if (!res.ok) {
    let said = null;
    try { const b = await res.json(); said = typeof b.detail === 'string' ? b.detail : null; } catch { /* nothing worth reading */ }
    const d = describeFetchFailure(res.status, said);
    closed(d.title, d.why, { signIn: d.signIn });
    return;
  }

  let manifest;
  try { manifest = await res.json(); } catch {
    closed('The shelf did not answer', 'The shelf’s answer was unreadable. Try a refresh; if it keeps happening tell Mitch.');
    return;
  }

  const book = (manifest.ebooks || []).find((b) => b && b.anchor === anchor);
  if (!book) {
    closed(
      'That book is not on the shelf',
      'Nothing on the shelf matches this link. It may have been renamed or re-filed since the link was made — open it from the shelf instead.',
    );
    return;
  }

  document.title = `${book.title} — Read`;
  titleEl.textContent = book.title + (book.author ? ` · ${book.author}` : '');

  // ⚠️ THE FORMAT SWITCH. Two renderers, one gate, one manifest, one anchor.
  // A third format lands here as an honest sentence, never a spinner.
  const format = String(book.format || '').toLowerCase();
  if (format === 'pdf') return openPdf(anchor, book);
  if (format === 'epub') return openEpub(anchor, book);
  closed('This reader cannot open that format', EPUB_SEAM);
}

async function openPdf(anchor, book) {
  gateEl.hidden = true;
  shellEl.hidden = false;
  clearFault();
  state.mode = 'pdf';
  busy(true);
  // ⚠️ From here the shell is on screen, so silence is a visible empty stage.
  // The watchdog is what makes silence say something. Disarmed by the first
  // successful draw below, or by any worded refusal (closed() clears it).
  armWatchdog();

  let lib;
  try {
    step('loading the PDF reader');
    lib = await loadPdfJs();
  } catch (e) {
    console.warn('[reader] pdf.js failed to load:', e);
    closed(
      'The reader did not load',
      'The PDF reader’s own code failed to load. That is a loading problem on our side, not a decision about you — try a refresh, and if it keeps happening tell Mitch.',
    );
    return;
  }

  // ⚠️ FORCED REFRESH. A Firebase ID token lasts an hour and pdf.js captures
  // `httpHeaders` ONCE, at getDocument, for the whole session — so a token
  // that was 55 minutes old at open would expire mid-book and every later
  // range would 401. Taking a fresh one buys a full hour of reading. A session
  // longer than that still ends in a 401, which is caught below and offered as
  // a reload rather than a mystery. (A refreshing transport is phase-3 work.)
  const token = await getIdToken(app, true);
  if (!token) {
    closed('Your sign-in has lapsed', 'Sign in again to carry on reading.', { signIn: true });
    return;
  }

  const task = lib.getDocument({
    url: FILE_URL(anchor),
    // ⚠️ The whole no-credential-in-a-URL decision rests on this option.
    httpHeaders: { Authorization: `Bearer ${token}` },
    withCredentials: false,
    disableRange: false, // the entire point
    // ⚠️ TRUE, AND THE DESIGN SAID FALSE. This is a correction, made on a
    // measurement rather than an opinion — viewer design §5.1's config block
    // specifies `disableStream: false`, and with that value the §5.3 promise
    // ("opening page 1 of a 181 MiB book transfers a few hundred KB, not
    // 181 MB") is simply not true.
    //
    // MEASURED 2026-08-17 against the real 181 MiB Stormlight handbook
    // (189,930,310 B) with the vendored pdf.js 5.4.149, on a local server that
    // counted bytes actually delivered:
    //
    //   disableStream: false -> pdf.js opens a full-file GET ALONGSIDE its
    //                           ranges and lets it RUN TO COMPLETION:
    //                           189,930,310 B delivered — 100% of the file.
    //   disableStream: true  -> the same GET is opened and then ABORTED after
    //                           655,360 B — 0.3%.
    //
    // Roughly 2.5 MiB to open the book instead of 183 MiB: a ~70× reduction,
    // for a BYTE-IDENTICAL render (392 pages, 1,065,914 ink pixels on page 1
    // either way; 12 MB of JS heap; 113 ms to open).
    //
    // ⚠️ `disableAutoFetch: true` alone does NOT prevent this — that was the
    // assumption, and it is wrong. Both flags are needed and they do different
    // jobs: autoFetch governs speculative fetching of the REST of the
    // document, and stream governs the whole-file read opened at the start.
    //
    // ⚠️ Honest caveat on the measurement: it is from localhost, where the
    // full transfer completes in milliseconds, so `false` may abort earlier
    // over a real network. That does not change the decision — it makes the
    // 181 MiB the worst case rather than the certain case, and there is no
    // upside to `false` to weigh against it.
    disableStream: true,
    disableAutoFetch: true, // see the module header — this is not a nicety
    isEvalSupported: false, // keeps the CSP tight; no 'unsafe-eval' anywhere
    cMapUrl: asset('./static/pdfjs/cmaps/'),
    cMapPacked: true,
    standardFontDataUrl: asset('./static/pdfjs/standard_fonts/'),
  });

  try {
    step('opening the file on the shelf');
    state.doc = await task.promise;
  } catch (e) {
    // pdf.js wraps HTTP failures: UnexpectedResponseException carries `status`,
    // MissingPDFException means a 404. Anything else is a broken file.
    const status = typeof e?.status === 'number' ? e.status : e?.name === 'MissingPDFException' ? 404 : -1;
    if (status >= 0) {
      const d = describeFetchFailure(status, null);
      closed(d.title, d.why, { signIn: d.signIn });
    } else {
      console.warn('[reader] pdf.js could not open the document:', e);
      closed(
        'This book would not open',
        'The file is on the shelf but the reader could not make sense of it. That is a problem with the file, not with your account — tell Mitch which book it was.',
      );
    }
    return;
  }

  pageTotalEl.textContent = String(state.doc.numPages);
  pageNowEl.max = String(state.doc.numPages);

  // Save your spot. ⚠️ The local row is read SYNCHRONOUSLY and used for the
  // very first draw — no await on a network, no "restoring…" state, no second
  // render. The remote reconcile runs in the background inside here.
  const { local } = beginPositionTracking({
    book, anchor, format: 'pdf',
    apply: (row) => drawPage(pageFrom(row, state.doc.numPages)),
  });

  try {
    step('drawing the first page');
    await drawPage(pageFrom(local, state.doc.numPages));
  } catch (e) {
    // ⚠️ `fault`, not `closed`: the shell is already on screen and the reader
    // is looking at it. And it carries the error's OWN WORDS — "the first page
    // could not be drawn" alone told us nothing the last time this fired.
    fault(
      'The first page would not draw',
      'The file opened but the reader could not paint a page from it. That is '
      + 'a problem with the file or the renderer, not with your account — send '
      + 'Mitch the line below and the book’s name.',
      e,
    );
    return;
  }
  // ⚠️ RENDERED. The watchdog stands down, and from here a later failure shows
  // its panel WITHOUT blanking the book.
  state.rendered = true;
  clearWatchdog();
  step('reading');
  // Swipe, and the mode's honest line — both AFTER the first successful
  // render, for the same reason the keeper is armed there: a book that never
  // opened has no pages to turn and no colours to explain.
  wirePdfSwipe();
  applyReadingMode();
  // ⚠️ ARMED ONLY NOW. Everything above could still have ended in a closed
  // state, and a book that would not open must not record a position.
  state.keeper?.arm();
  // ⚠️ AND ONE RECORD IMMEDIATELY, which is not redundant — it closes a race
  // FOUND BY EXERCISING THIS, 2026-08-17. A reader who turns a page WHILE the
  // first page is still rendering turns it through an unarmed keeper: that
  // turn records nothing, and if they then stop, the session saves nothing at
  // all. `state.page` is whatever the newest draw settled on, so recording it
  // here catches that turn. The cost is one write per book opened, which is
  // the right price for "opening a book is itself where you are".
  recordPdfPosition();
}

/** The current PDF page, as the keeper wants it. One shape, two callers. */
function recordPdfPosition() {
  if (!state.doc) return;
  state.keeper?.record(
    { kind: 'page', value: state.page },
    {
      progress: state.doc.numPages ? state.page / state.doc.numPages : null,
      label: `p. ${state.page} of ${state.doc.numPages}`,
    },
  );
}

/**
 * The page a stored row means, clamped to this document.
 *
 * ⚠️ THE STALE-LOCATOR CASE, and it is not hypothetical: a book can be
 * re-uploaded shorter, or the row can predate a re-export. `drawPage` clamps
 * too, but doing it HERE is what makes "page 900 of a 300-page file" resolve
 * to the last page instead of quietly relying on a clamp somewhere else. A row
 * that is not a usable page number at all answers 1 — the start of the book,
 * never a broken render.
 */
function pageFrom(row, numPages) {
  const n = row && row.pos && row.pos.kind === 'page' ? Number(row.pos.value) : NaN;
  if (!Number.isFinite(n) || n < 1) return 1;
  return Math.min(Math.floor(n), Math.max(1, numPages || 1));
}

/* ── foliate-js, over byte ranges ───────────────────────────────────────── */

/**
 * The book's own CSS, gently overruled — and only where the shelf has an
 * opinion. ⚠️ NOT a full restyle: an EPUB's typography is the publisher's work
 * and this reader is not a better designer than they were. What it does set is
 * the page's PAPER and INK, so a book does not open as a white rectangle inside
 * a dark-mode shelf, and a comfortable measure.
 *
 * `line-height` and `font-family` are deliberately LEFT ALONE. Both were tried
 * and both look wrong on books that chose otherwise — a drop-cap layout in
 * Georgia is somebody else's book.
 */
function epubStyles(scale) {
  const cs = getComputedStyle(document.documentElement);
  // ⚠️ `--page` / `--page-ink`, NOT `--card` / `--ink`. Those two are the page
  // CHROME and the reading SURFACE, and the three reading modes are precisely
  // where they part company: in "paper" the toolbar keeps the shelf's warm
  // cream while the book is printed on plain white. Reading the chrome tokens
  // here is how a book ends up cream inside a white frame — a subtle wrongness
  // nobody can name and everybody sees. read.html defines the pair, and in
  // "match theme" they simply alias --card/--ink, so this is unchanged there.
  const paper = cs.getPropertyValue('--page').trim() || '#fdfaf2';
  const ink = cs.getPropertyValue('--page-ink').trim() || '#2c2418';
  const accent = cs.getPropertyValue('--accent').trim() || '#9d4a1c';
  return `
    @namespace epub "http://www.idpf.org/2007/ops";
    html, body { color: ${ink}; background: ${paper}; }
    /* The type size control. A multiplier, so the book's own relative sizes —
       its headings, its small caps — keep their proportions. */
    html { font-size: ${(scale * 100).toFixed(1)}%; }
    a, a:visited { color: ${accent}; }
    /* Images must not be taller than the column or they paginate into an
       empty page of their own. */
    img, svg { max-width: 100%; max-height: 100vh; height: auto; }
  `;
}

/** "Chapter Three · 12%" — what an EPUB has instead of "page 7 of 392". */
function describeLocation(detail) {
  const pct = typeof detail?.fraction === 'number' ? `${Math.round(detail.fraction * 100)}%` : '';
  const label = (detail?.tocItem?.label || '').trim();
  return [label, pct].filter(Boolean).join(' · ') || '';
}

/**
 * Go to a stored EPUB locator, or stay where the book already is.
 *
 * ⚠️ THE STALE-LOCATOR CASE, and why this is a SECOND navigation rather than
 * `view.init({ lastLocation })`. foliate's `init()` resolves the location and
 * then `await`s `renderer.goTo()` OUTSIDE any try — so a CFI that no longer
 * resolves (the book was re-exported, a section is gone) REJECTS init, and the
 * reader answers "this book would not open" for a book that opens perfectly.
 * `view.goTo()` catches its own failures and logs them, so the worst case here
 * is that the reader stays at the text start `init()` already rendered. A dead
 * bookmark must cost a bookmark, never a book.
 */
async function goToStoredLocation(view, row) {
  if (!view || !row || !row.pos || row.pos.kind !== 'cfi' || !row.pos.value) return;
  try {
    await view.goTo(String(row.pos.value));
  } catch (e) {
    console.warn('[reader] the stored location no longer resolves; staying at the start:', e);
  }
}

/**
 * One foliate location, as the keeper wants it. One shape, two callers.
 *
 * ⚠️ THE LOCATOR IS THE CFI, not the fraction. Both are on foliate's relocate
 * event and the fraction is the tempting one — it is a number, it survives
 * anything, and it is WRONG as a bookmark: it is a position in the BOOK'S
 * BYTES, so a different reflow (a phone, a bigger type size) lands somewhere
 * else on the page, and any re-export moves it. A CFI names a place in the
 * document's own structure, which is what "where I was" means. The fraction
 * rides along as `progress` — for a percentage label and a future progress
 * bar, never for navigation.
 */
function recordEpubPosition(detail) {
  const d = detail || {};
  if (!d.cfi) return;
  state.keeper?.record(
    { kind: 'cfi', value: d.cfi },
    {
      progress: typeof d.fraction === 'number' ? d.fraction : null,
      label: describeLocation(d),
    },
  );
}

async function openEpub(anchor, book) {
  gateEl.hidden = true;
  shellEl.hidden = false;
  clearFault();
  // The PDF furniture steps aside; the EPUB furniture steps up.
  stageEl.hidden = true;
  bookEl.hidden = false;
  pagerPdfEl.hidden = true;
  pagerEpubEl.hidden = false;
  zoomInEl.setAttribute('aria-label', 'Larger type');
  zoomOutEl.setAttribute('aria-label', 'Smaller type');
  state.mode = 'epub';
  busy(true);
  // ⚠️ THE LINE THE 2026-08-18 P1 WAS ABOUT. Every await below can hang rather
  // than throw — foliate's own `View.load()` resolves only from inside an
  // iframe `load` listener, so a blocked frame settles nothing at all and no
  // catch in this function is ever reached. The watchdog is the only thing that
  // turns that into words. See §8.
  armWatchdog();

  let loader;
  try {
    step('loading the EPUB reader');
    loader = await import('./epub-loader.js');
    await loader.loadFoliateView();
  } catch (e) {
    console.warn('[reader] foliate-js failed to load:', e);
    closed(
      'The reader did not load',
      'The EPUB reader’s own code failed to load. That is a loading problem on our side, not a decision about you — try a refresh, and if it keeps happening tell Mitch.',
    );
    return;
  }

  // ⚠️ A FRESH TOKEN AT OPEN, for the same reason the PDF path takes one: the
  // first thing that happens next is a network request, and starting with a
  // 59-minute-old token buys a minute of reading. Unlike pdf.js, the ranges
  // after this one re-ask (see below), so this is a floor rather than a ceiling.
  if (!(await getIdToken(app, true))) {
    closed('Your sign-in has lapsed', 'Sign in again to carry on reading.', { signIn: true });
    return;
  }

  let opened;
  try {
    step('reading the book’s index from the shelf');
    opened = await loader.openEpubOverRanges({
      url: FILE_URL(anchor),
      // ⚠️ PER REQUEST, not captured once. `getIdToken()` (unforced) returns the
      // SDK's cached token and refreshes it transparently near expiry, so a
      // reading session longer than an hour keeps working — the gap phase 1b
      // records as unhandled for pdf.js, which can only take headers once.
      getAuthHeader: async () => {
        const t = await getIdToken(app);
        return t ? `Bearer ${t}` : null;
      },
    });
  } catch (e) {
    if (e && e.name === 'HttpStatusError') {
      const d = describeFetchFailure(e.status, e.detail);
      closed(d.title, d.why, { signIn: d.signIn });
      return;
    }
    if (e && e.name === 'RangeUnsupportedError') {
      // ⚠️ Not a mystery and not a permission problem: the shelf answered a
      // byte-range request with the whole file, and the reader refused to
      // download it. Naming it is what stops the next person "fixing" it by
      // removing the refusal.
      console.warn('[reader] the byte stream did not honour Range:', e);
      closed(
        'This book would not open',
        'The shelf sent this book as one whole file instead of in pieces, and it is far too large to read that way. That is a problem on our side, not with your account — tell Mitch which book it was.',
      );
      return;
    }
    if (e && e.name === 'ObjectChangedError') {
      closed('This book changed while you were reading', e.message + ' Refresh to open it again.');
      return;
    }
    // ⚠️ NARROWED 2026-08-18, and the narrowing is the point. This branch used
    // to be a bare `e instanceof TypeError` mapped to "The shelf did not
    // answer" — an OUTAGE sentence. A failed `fetch()` IS a TypeError, so the
    // intent was right; but so is EVERY code fault in the loader, in zip.js and
    // in foliate's parser (`Object.groupBy is not a function` on a Safari older
    // than 17.4, to name a real one). Those are not outages, and telling
    // somebody the server is down when the renderer is broken sends them to
    // wait for a recovery that will never come — the exact mislabelling
    // ROLES.md §1e forbids, in the other direction. A network TypeError says
    // "Failed to fetch" (Chromium) or "Load failed" / "cancelled" (WebKit);
    // anything else gets its own words in the fault panel.
    if (e instanceof TypeError && /fetch|network|load failed|cancel/i.test(e.message || '')) {
      const d = describeFetchFailure(0);
      closed(d.title, d.why);
      return;
    }
    fault(
      'This book would not open',
      'The file is on the shelf but the reader could not make sense of it. '
      + 'That is a problem with the file or the reader, not with your account — '
      + 'send Mitch the line below and the book’s name.',
      e,
    );
    return;
  }

  console.info(
    `[reader] epub opened over ${opened.stats.requests} range requests, ${opened.stats.bytes} B`,
  );

  const view = document.createElement('foliate-view');
  state.view = view;
  view.addEventListener('relocate', (ev) => {
    locEl.textContent = describeLocation(ev.detail);
    // ⚠️ THE LOCATOR IS THE CFI, not the fraction. Both are on this event, and
    // the fraction is the tempting one — it is a number, it survives anything,
    // and it is WRONG as a bookmark: it is a position in the BOOK'S BYTES, so
    // a different reflow (a phone, a bigger type size) lands somewhere else on
    // the page, and any re-export moves it. A CFI names a place in the
    // document's own structure, which is what "where I was" means. The
    // fraction rides along as `progress` — for a percentage label and a future
    // progress bar, never for navigation.
    recordEpubPosition(ev.detail);
  });
  // ⚠️ FIXED-LAYOUT ONLY, and the guard is the whole point. A reflowable book
  // is already swiped by foliate's own paginator (see wireFxlSwipe's neighbour
  // above); wiring one here as well turns TWO pages per flick. `load` fires per
  // spread, which is what keeps the gesture attached as FXL swaps its iframes.
  view.addEventListener('load', (ev) => {
    if (view.isFixedLayout) wireFxlSwipe(ev.detail?.doc);
  });
  bookEl.append(view);

  try {
    await view.open(opened.book);
    // ⚠️ TWO RENDERERS BEHIND ONE `view`, and they do not share an API.
    // `View.open()` picks `<foliate-paginator>` for a reflowable book and
    // `<foliate-fxl>` for a `pre-paginated` one — and **`foliate-fxl` has no
    // `setStyles` and no `margin`/`gap`/`max-inline-size` attributes at all**.
    // Calling them anyway throws, and the reader answers "this book would not
    // open" for a book that opens perfectly. Found by opening the White Sand
    // Omnibus, which IS fixed-layout — i.e. by the acceptance test, not by
    // reading the source. Typography is meaningless for a fixed-layout book:
    // its pages are images with the type baked in.
    if (!view.isFixedLayout) {
      // Tuned minimally to the shelf. `max-column-count` is left at foliate's
      // own default of 2 and it is right: a wide window gets a spread, a phone
      // gets one column, and neither needs a setting.
      view.renderer.setAttribute('flow', 'paginated');
      view.renderer.setAttribute('margin', '36px');
      view.renderer.setAttribute('gap', '6%');
      view.renderer.setAttribute('max-inline-size', '38rem');
      view.renderer.setStyles(epubStyles(state.fontScale));
    } else {
      // The type-size buttons would be lying on a fixed-layout book. Hide them
      // rather than leave two controls that do nothing (ROLES.md §1e).
      zoomInEl.hidden = true;
      zoomOutEl.hidden = true;
    }
    // `showTextStart` skips the cover and front matter and opens where the
    // text does — which is what "Read" means. A book with no such landmark
    // falls back to its first section.
    //
    // ⚠️ ALWAYS `showTextStart`, even when a position is stored — see
    // goToStoredLocation for why the bookmark is a SECOND navigation and not
    // `init({ lastLocation })`. This one is inside the try because a book
    // whose text start will not render is genuinely a book that would not open.
    step('drawing the first page');
    await view.init({ showTextStart: true });
  } catch (e) {
    // ⚠️ `fault`, not `closed`, AND it carries the error. This is the exact
    // catch that did NOT run in the 2026-08-18 P1 — the failure hung instead of
    // throwing — but when it does run, "the first page could not be drawn" on
    // its own is the sentence that told us nothing. The words go in the panel.
    fault(
      'This book would not open',
      'The reader loaded the file but could not paint a page from it. That is a '
      + 'problem with the file or the renderer, not with your account — send '
      + 'Mitch the line below and the book’s name.',
      e,
    );
    return;
  }
  // ⚠️ RENDERED — foliate has a page on screen. The watchdog stands down, and
  // any later failure shows its panel WITHOUT blanking the book.
  state.rendered = true;
  clearWatchdog();
  step('reading');

  // Save your spot. The book is already readable on screen at this point, so
  // the jump to the stored location cannot cost anybody a render.
  const { local } = beginPositionTracking({
    book, anchor, format: 'epub',
    apply: (row) => goToStoredLocation(state.view, row),
  });
  await goToStoredLocation(view, local);
  // ⚠️ ARMED ONLY NOW, so neither `init()`'s relocate nor the restore's own
  // writes a position for a book that might still have failed to open.
  state.keeper?.arm();
  // ⚠️ AND ONE RECORD IMMEDIATELY — the same race the PDF half documents: a
  // reader who turns a page while the book is still opening turns it through
  // an unarmed keeper. `view.lastLocation` is foliate's own record of the
  // newest relocate, so this catches that turn.
  recordEpubPosition(view.lastLocation);
  // The mode's note, now that we know WHICH renderer opened: a fixed-layout
  // book has to say that its pages keep their own colours.
  applyReadingMode();
  busy(false);
}

/**
 * Type size, reflowable EPUB only. Re-applied through foliate so it
 * re-paginates. ⚠️ `setStyles?.()` is not defensive noise — `<foliate-fxl>`
 * genuinely does not have it (see openEpub).
 */
function setEpubScale(next) {
  if (state.view?.isFixedLayout) return;
  state.fontScale = Math.min(2.5, Math.max(0.6, next));
  state.view?.renderer?.setStyles?.(epubStyles(state.fontScale));
}

/* ── the three reading modes ────────────────────────────────────────────── */

/**
 * Owner, 2026-08-17: *"we need to be able to set the pages white and the font
 * black or vice versa as well as make it match the theme."*
 *
 * ⚠️ ALMOST NONE OF THIS IS HERE. `site/read-mode.js` owns the choice and
 * stamps `<html data-read-mode>` before first paint; `read.html`'s CSS turns
 * that stamp into a palette, and inverts the PDF canvas with a filter. This
 * function exists for the ONE surface CSS cannot reach — a reflowable EPUB's
 * text, which lives in an iframe inside a closed shadow root and can only be
 * recoloured by handing foliate a stylesheet.
 *
 * ⚠️ `setStyles?.()` is not defensive noise. `<foliate-fxl>` genuinely does not
 * have it, and the unconditional call THREW at phase 2 — the reader answered
 * "this book would not open" for a book that opens perfectly. The
 * `isFixedLayout` guard above it is the real protection; the `?.` is the belt.
 */
function applyReadingMode() {
  if (state.mode === 'epub' && state.view && !state.view.isFixedLayout) {
    state.view.renderer?.setStyles?.(epubStyles(state.fontScale));
  }
  updateModeNote();
}

/**
 * The sentence that stops a display mode being a surprise.
 *
 * ⚠️ TWO HONEST LIMITS, and neither may be silent (ROLES.md §1e — a control
 * that does something other than what it says is worse than one that refuses):
 *
 *   1. **An inverted PDF inverts its PICTURES.** There is no text layer to
 *      recolour on a canvas, so "ink" is `filter: invert(1) hue-rotate(180deg)`
 *      over the whole rendered page. Text becomes white-on-black exactly as
 *      asked; a photograph becomes a negative. Nobody should have to work that
 *      out from a map that has gone wrong.
 *   2. **A fixed-layout EPUB cannot be recoloured at all.** Its pages ARE
 *      images with the type baked into them, and `<foliate-fxl>` has no
 *      `setStyles` (measured at viewer phase 2, on the White Sand Omnibus).
 *      The chrome follows the mode; the book keeps its own colours. Saying so
 *      is the difference between a documented limit and a broken-looking
 *      control.
 */
function updateModeNote() {
  if (!modeNoteEl) return;
  const mode = (typeof window !== 'undefined' && window.readerMode)
    ? window.readerMode.get() : 'match';
  let note = '';
  if (state.mode === 'epub' && state.view?.isFixedLayout) {
    note = 'This book’s pages are fixed images with the type printed into them, '
      + 'so they keep their own colours — the mode changes the reader around them.';
  } else if (state.mode === 'pdf' && mode === 'ink') {
    note = 'Ink mode inverts the whole page, so pictures, maps and diagrams are '
      + 'inverted too. Switch to Paper to see them as printed.';
  }
  modeNoteEl.textContent = note;
  modeNoteEl.hidden = !note;
}

// read-mode.js fires this on every change (and once at boot, before this
// listener exists — which is why every open path calls applyReadingMode() too).
document.addEventListener('rd-modechange', applyReadingMode);

/* ── swipe to turn ──────────────────────────────────────────────────────── */

/**
 * Owner, 2026-08-17: *"we need a way to swipe to change pages."*
 *
 * ⚠️ MEASURED BEFORE ANY OF IT WAS WRITTEN, in the vendored source: foliate's
 * `<foliate-paginator>` ALREADY handles touch. `paginator.js` binds
 * `touchstart`/`touchmove`/`touchend` on itself AND on each section's document
 * (which is how it reaches inside the iframe that would otherwise swallow the
 * gesture), drags the columns live, and calls `snap()` on release — which
 * crosses a section boundary through `#goTo()` when the flick runs off the end.
 * It even guards `visualViewport.scale > 1` for pinch. And `#afterScroll()`
 * dispatches `relocate`, which `<foliate-view>` re-emits and this file already
 * listens to.
 *
 * ⚠️ SO A REFLOWABLE EPUB SWIPES FOR FREE, AND WIRING ONE HERE WOULD BE A BUG:
 * two handlers on one flick turn two pages. The two surfaces foliate does NOT
 * cover are the ones wired below — the PDF canvas, and a fixed-layout book,
 * whose renderer (`fixed-layout.js`) contains no touch handling whatsoever.
 *
 * ⚠️ AND BOTH GO THROUGH `goNext`/`goPrev`. Those are the same two functions
 * the arrows and the keyboard call, and they are what record the reader's spot
 * — `drawPage()` runs `recordPdfPosition()`, and foliate's own turn raises the
 * `relocate` this file turns into `recordEpubPosition()`. A swipe wired
 * straight to `drawPage()` or `view.next()` would turn pages perfectly and stop
 * saving anybody's place, with nothing anywhere saying so.
 */
function wirePdfSwipe() {
  wireSwipe(stageEl, {
    onNext: () => goNext(),
    onPrev: () => goPrev(),
    // ⚠️ A ZOOMED PDF OWNS ITS OWN HORIZONTAL AXIS. Once the rendered page is
    // wider than the stage, a sideways drag means "show me the right margin",
    // not "next page" — and the stage scrolls, so the reader would lose their
    // place in the spread AND land on a different page. Asked live rather than
    // captured, because the zoom buttons change it between gestures.
    axisTaken: () => stageEl.scrollWidth > stageEl.clientWidth + 2,
  });
}

/**
 * A fixed-layout EPUB's pages live in iframes inside `<foliate-fxl>`'s CLOSED
 * shadow root, and **touch events inside an iframe never reach the parent
 * document** — a separate browsing context, not a bubbling problem. So a
 * listener on `#rd-book` would hear nothing at all.
 *
 * The way in is foliate's own `load` event, which hands out `{doc, index}` for
 * each spread as it renders (`fixed-layout.js` dispatches it; `view.js`
 * re-emits it). Attaching to that `doc` is exactly what foliate's paginator
 * does for the reflowable case, so this is its trick borrowed rather than a
 * workaround.
 *
 * ⚠️ Re-attached per spread ON PURPOSE. FXL swaps its iframes as it moves, so
 * a document wired once is a document that stops answering two turns later.
 * Wiring the same document twice is harmless: `wireSwipe` adds one gesture, and
 * the handlers are idempotent per gesture — but the previous detach is called
 * anyway so a long book does not accumulate them.
 */
let detachFxlSwipe = null;
function wireFxlSwipe(doc) {
  if (!doc) return;
  try { detachFxlSwipe?.(); } catch { /* the old document is already gone */ }
  detachFxlSwipe = wireSwipe(doc, {
    onNext: () => goNext(),
    onPrev: () => goPrev(),
  });
}

/* ── controls ───────────────────────────────────────────────────────────── */

/**
 * ⚠️ ONE toolbar, TWO renderers, and every handler asks which. A PDF turns a
 * page by drawing a canvas; an EPUB turns one by asking foliate, which may
 * cross a section boundary and fetch more ranges. Wiring either half straight
 * to the other's function is the mistake this indirection exists to prevent.
 */
const goPrev = () => (state.mode === 'epub'
  ? void state.view?.prev().catch(() => {})
  : void drawPage(state.page - 1).catch(() => {}));
const goNext = () => (state.mode === 'epub'
  ? void state.view?.next().catch(() => {})
  : void drawPage(state.page + 1).catch(() => {}));

prevEl.addEventListener('click', goPrev);
nextEl.addEventListener('click', goNext);
pageNowEl.addEventListener('change', () => {
  const n = Number(pageNowEl.value);
  if (Number.isFinite(n)) void drawPage(n).catch(() => {});
});
zoomInEl.addEventListener('click', () => {
  if (state.mode === 'epub') return setEpubScale(state.fontScale * 1.15);
  state.scale = Math.min(4, (state.scale ?? state.lastScale ?? 1) * 1.25);
  void drawPage(state.page).catch(() => {});
});
zoomOutEl.addEventListener('click', () => {
  if (state.mode === 'epub') return setEpubScale(state.fontScale / 1.15);
  state.scale = Math.max(0.25, (state.scale ?? state.lastScale ?? 1) / 1.25);
  void drawPage(state.page).catch(() => {});
});

document.addEventListener('keydown', (ev) => {
  if (shellEl.hidden) return;
  if (ev.target && /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName || '')) return;
  if (ev.key === 'ArrowRight' || ev.key === 'PageDown') goNext();
  if (ev.key === 'ArrowLeft' || ev.key === 'PageUp') goPrev();
});

// Re-fit on resize, but only while the user has not chosen a zoom of their own.
// ⚠️ EPUB is exempt: foliate re-paginates itself on a ResizeObserver, and a
// second re-layout on top of its own fights it and loses the reader's place.
let resizeTimer = null;
window.addEventListener('resize', () => {
  if (state.mode === 'epub') return;
  if (state.scale !== null || !state.doc) return;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => void drawPage(state.page).catch(() => {}), 150);
});

/* ── boot ───────────────────────────────────────────────────────────────── */

let app = null;
/** The Firestore handle the reading-position store writes through. */
let db = null;

// ⚠️ FIRST, BEFORE ANYTHING ELSE CAN THROW. §8's backstop is worth nothing if
// it is installed after the failure it exists to report.
wireGlobalFailureReporting();

// "Try again" is a reload, deliberately: every piece of state this page holds —
// a half-built foliate view, a cancelled render task, a zip reader over a stale
// ETag — is easier to throw away than to unwind, and a retry that quietly
// reuses broken state is how a reader gets a SECOND mystery on top of the
// first. The address bar already carries `?b=`, so the same book comes back.
faultRetryEl?.addEventListener('click', () => window.location.reload());

try {
  app = initializeApp(FIREBASE_CONFIG);
  db = getFirestore(app);
  mountAccountModal(db, app, el('identity-bar'));
} catch (e) {
  console.warn('[reader] identity failed to initialise:', e);
  closed(
    'The reader could not check your sign-in',
    'Sign-in did not load, so the reader cannot tell who you are. This is a loading problem on our side, not a decision about you — try a refresh, and if it keeps happening tell Mitch.',
  );
}

// A blocked or very slow gstatic must not leave "Checking your sign-in…" on
// screen forever — the shelf's backstop, ported.
let resolved = false;
const backstop = setTimeout(() => {
  if (resolved) return;
  resolved = true;
  closed('The reader could not check your sign-in', 'Sign-in is taking too long to load. Try a refresh; if it keeps happening tell Mitch.');
}, 8000);

gateBtnEl.addEventListener('click', async () => {
  gateBtnEl.disabled = true;
  try {
    await signInWithGoogle(app);
    await boot();
  } catch {
    gateWhyEl.textContent = 'Sign-in did not complete. Try again, or tell Mitch if it keeps failing.';
  }
  gateBtnEl.disabled = false;
});

async function boot() {
  if (!app) return;
  const anchor = anchorFromLocation();
  const user = await getLiveUser(app).catch(() => null);
  resolved = true;
  clearTimeout(backstop);

  if (!user || !user.uid) {
    closed(
      'This reader is for the household',
      'These are the household’s own ebook files, so the reader is not public. Sign in with Google and, if you have been given the ebook grant, the book will open.',
      { signIn: true },
    );
    return;
  }

  // ⚠️ The uid comes from the LIVE session's snapshot, never from the
  // localStorage mirror: the mirror is devtools-editable and would let anybody
  // address anybody's position document. firestore.rules refuses that anyway
  // (the doc id must start with request.auth.uid), so the mirror would fail as
  // a PERMISSION_DENIED rather than as a leak — but a page should not send a
  // write it knows will be refused.
  state.uid = user.uid;

  // ⚠️ THE DEV-LANE CURTAIN — A CURTAIN, NOT A LOCK. site/dev-lane.js owns
  // every decision here and its header explains all of them; what matters at
  // this call site is the ORDER and the LANE.
  //
  //   * AFTER the sign-out branch above, deliberately. A signed-out visitor
  //     must meet "sign in", not "you need dev access" — telling somebody to
  //     ask for a grant when what they actually need is to sign in sends them
  //     to the wrong person.
  //   * BEFORE openBook(), so a curtained reader never sees a book title, a
  //     spinner or a half-drawn shelf.
  //   * `devLaneVerdict` returns 'prod-lane' WITHOUT a network call on any
  //     promoted page, so nothing below runs on ebooks.heygabi.ai and no new
  //     failure mode reaches production. The check is on the PATH, never the
  //     host.
  //   * 'unknown' (the estate did not answer, or answered without the field)
  //     falls through and OPENS THE BOOK. An outage is not a permission
  //     decision, and this curtain has never guarded a byte: the books are
  //     gated by `vis_ebooks` on the Worker, on both lanes, unchanged.
  if ((await devLaneVerdict(app)) === 'curtain') {
    closed(DEV_CURTAIN.title, DEV_CURTAIN.why, { back: false, prod: true });
    return;
  }

  if (!anchor) {
    closed(
      'No book was named',
      'This page opens one book at a time and this link did not say which. Pick a book on the shelf and press Read.',
    );
    return;
  }

  await openBook(anchor);
}

handleRedirectResult(app).catch(() => {}).then(boot);
