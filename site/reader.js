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
 * ## Not here yet, deliberately — the EPUB seam
 *
 * ⚠️ An EPUB anchor gets an honest, worded refusal, never a spinner and never
 * a dead page. The EPUB half is a SEPARATE build (viewer phase 2) and the seam
 * it plugs into is `openBook()`'s format switch plus `EPUB_SEAM` below — see
 * that constant's comment for exactly what phase 2 replaces and what it must
 * NOT re-derive.
 */

import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-app.js';
import { getFirestore } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { FIREBASE_CONFIG } from './fb-env.js';
import { mountAccountModal } from './account-modal.js';
import { getLiveUser, signInWithGoogle, handleRedirectResult } from './identity.js';

/** The gated manifest — the same URL, and the same gate, the shelf uses. */
const MANIFEST_URL = 'https://audiobook-api.heygabi.ai/api/ebooks/manifest';
/** The gated byte stream. ⚠️ `:anchor` is substituted, nothing else. */
const FILE_URL = (anchor) =>
  `https://audiobook-api.heygabi.ai/api/ebook/${encodeURIComponent(anchor)}/file`;

/**
 * ⚠️ THE EPUB SEAM — read this before building viewer phase 2.
 *
 * Everything the EPUB reader needs is already here and already gated: the
 * sign-in flow, the manifest lookup, the anchor contract, the error mapping in
 * `describeFetchFailure()`, and a byte stream that honours `Range` for BOTH
 * formats (measured: foliate-js + a zip.js `HttpRangeReader` opened the 393 MiB
 * White Sand Omnibus in 15 range requests totalling 76.9 KiB).
 *
 * So phase 2 replaces exactly ONE branch — this one — and touches nothing else:
 *
 *   - vendor foliate-js + @zip.js/zip.js into `site/static/` beside pdf.js,
 *     pinned, with their licences (see VENDORED.md's shape);
 *   - ⚠️ inject a RANGE-READING loader. foliate's own `view.js` builds
 *     `new ZipReader(new BlobReader(file))` over a whole in-memory Blob, so
 *     using it unmodified brings back the whole-file fetch and undoes the win;
 *   - ⚠️ foliate-js, NOT epub.js. Measured on the same file: epub.js fetched
 *     412,436,591 B into 1,207 MB of JS heap; foliate + ranges did it in
 *     78,741 B and 10.4 MB. And decide it BEFORE any reading position is
 *     stored: a stored CFI is a persisted key produced by a specific renderer,
 *     so swapping later is a migration, not an edit;
 *   - the 32 MiB size gate and its "this book is too large" refusal card that
 *     the design once called for are NOT needed and should not be built;
 *   - `blob:` must be in `/read`'s `img-src` and `frame-src` — an EPUB reader
 *     materialises extracted images as blob URLs, and omitting it produces a
 *     reader that paginates perfectly and shows no pictures.
 *
 * The Read button on the shelf's EPUB cards is deliberately absent until then
 * (app/web/templates/ebooks.html), so nobody is offered a door that opens onto
 * this message.
 */
const EPUB_SEAM =
  'EPUB reading is not switched on in this reader yet — only PDFs are, for now. The EPUB half is next; until then this book is on the shelf but not readable here.';

/* ── page furniture ─────────────────────────────────────────────────────── */

const el = (id) => document.getElementById(id);
const gateEl = el('rd-gate');
const gateTitleEl = el('rd-gate-title');
const gateWhyEl = el('rd-gate-why');
const gateBtnEl = el('rd-gate-signin');
const gateBackEl = el('rd-gate-back');
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

/**
 * Show a closed/failed state. ⚠️ Every one says what happened, what it needs
 * and how to get it — never a bare status, never a raw body, never a dead
 * page (ROLES.md §1e). `signIn` draws the button; `back` draws the way home,
 * which every non-auth failure gets because a reader who cannot read this book
 * should still be one click from the shelf.
 */
function closed(title, why, opts = {}) {
  gateTitleEl.textContent = title;
  gateWhyEl.textContent = why;
  gateBtnEl.hidden = !opts.signIn;
  gateBackEl.hidden = opts.back === false;
  gateEl.hidden = false;
  shellEl.hidden = true;
  busy(false);
}

function busy(on) {
  busyEl.hidden = !on;
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
  doc: null,
  page: 1,
  /** null = fit the stage's width; a number is an explicit user zoom. */
  scale: null,
  renderTask: null,
  /** The scale actually used last draw — the zoom buttons' starting point. */
  lastScale: null,
  /** Guards against two renders racing after fast page turns. */
  renderSeq: 0,
};

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
async function openBook(user, anchor) {
  // 1. What is this anchor? Asked of the GATED manifest, which is also the
  //    gate: an unauthorised reader never learns a book exists.
  let res;
  try {
    const token = await user.getIdToken();
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

  const format = String(book.format || '').toLowerCase();
  if (format !== 'pdf') {
    // The EPUB seam. An honest refusal, never a spinner that never ends.
    closed(format === 'epub' ? 'Not yet, for EPUBs' : 'This reader cannot open that format', EPUB_SEAM);
    return;
  }

  await openPdf(user, anchor);
}

async function openPdf(user, anchor) {
  gateEl.hidden = true;
  shellEl.hidden = false;
  busy(true);

  let lib;
  try {
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
  let token;
  try {
    token = await user.getIdToken(true);
  } catch {
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
  try {
    await drawPage(1);
  } catch (e) {
    console.warn('[reader] first page failed to render:', e);
    closed('This book would not open', 'The first page could not be drawn. Tell Mitch which book it was.');
  }
}

/* ── controls ───────────────────────────────────────────────────────────── */

prevEl.addEventListener('click', () => void drawPage(state.page - 1).catch(() => {}));
nextEl.addEventListener('click', () => void drawPage(state.page + 1).catch(() => {}));
pageNowEl.addEventListener('change', () => {
  const n = Number(pageNowEl.value);
  if (Number.isFinite(n)) void drawPage(n).catch(() => {});
});
zoomInEl.addEventListener('click', () => {
  state.scale = Math.min(4, (state.scale ?? state.lastScale ?? 1) * 1.25);
  void drawPage(state.page).catch(() => {});
});
zoomOutEl.addEventListener('click', () => {
  state.scale = Math.max(0.25, (state.scale ?? state.lastScale ?? 1) / 1.25);
  void drawPage(state.page).catch(() => {});
});

document.addEventListener('keydown', (ev) => {
  if (shellEl.hidden) return;
  if (ev.target && /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName || '')) return;
  if (ev.key === 'ArrowRight' || ev.key === 'PageDown') void drawPage(state.page + 1).catch(() => {});
  if (ev.key === 'ArrowLeft' || ev.key === 'PageUp') void drawPage(state.page - 1).catch(() => {});
});

// Re-fit on resize, but only while the user has not chosen a zoom of their own.
let resizeTimer = null;
window.addEventListener('resize', () => {
  if (state.scale !== null || !state.doc) return;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => void drawPage(state.page).catch(() => {}), 150);
});

/* ── boot ───────────────────────────────────────────────────────────────── */

let app = null;

try {
  app = initializeApp(FIREBASE_CONFIG);
  mountAccountModal(getFirestore(app), app, el('identity-bar'));
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

  if (!anchor) {
    closed(
      'No book was named',
      'This page opens one book at a time and this link did not say which. Pick a book on the shelf and press Read.',
    );
    return;
  }

  await openBook(user, anchor);
}

handleRedirectResult(app).catch(() => {}).then(boot);
