/**
 * read-mode.js — the reader's THREE reading modes, stamped before first paint.
 *
 * Owner, 2026-08-17: *"we need to be able to set the pages white and the font
 * black or vice versa as well as make it match the theme."*
 *
 *   paper  white page, black text — the printed-book default
 *   ink    black page, white text — reading in the dark
 *   match  follow the site's own light/dark stamp (theme.js's data-mode)
 *
 * ⚠️ A CLASSIC SCRIPT IN <head>, NOT A MODULE, and that is the whole reason it
 * is a separate file from reader.js. reader.js is `type="module"`, which is
 * deferred by definition: a mode applied there lands AFTER first paint, so a
 * reader who chose `ink` gets a flash of white page on every load. theme.js
 * solves the identical problem the identical way, and this sits beside it.
 *
 * ⚠️ AND IT CANNOT BE INLINE. `/read`'s CSP (site/_headers) is
 * `script-src 'self'` with NO `'unsafe-inline'`, so an inline stamp would run
 * perfectly on a local server and be BLOCKED in production — the worst kind of
 * bug to find. `tests/test_reader_page.py` fails on any inline <script> here.
 *
 * ## What it stamps, and why the attribute is absent for `match`
 *
 * `<html data-read-mode="paper|ink">`. `match` REMOVES the attribute rather
 * than stamping a third value, because "match" is precisely "let the existing
 * cascade decide" — the page's `:root` / `html[data-mode="dark"]` /
 * `prefers-color-scheme` blocks already answer it, completely, in both
 * directions. A `data-read-mode="match"` block would be an empty rule whose
 * only job is to not exist.
 *
 * ⚠️ SPECIFICITY, and it is not padding. `read.html` writes the mode blocks as
 * BOTH `html[data-read-mode="ink"]` and `html[data-read-mode="ink"][data-mode]`
 * — the second is what beats the `@media (prefers-color-scheme: dark)` block's
 * `html:not([data-mode="light"])` (0,2,1), which a single-attribute selector
 * (0,1,1) loses to. Written the obvious way, "paper" silently does nothing for
 * anyone whose OS is in dark mode and who never touched the theme cog.
 *
 * ## The storage key is a DEVICE preference
 *
 * `localStorage` on purpose, not Firestore: which colours a screen is
 * comfortable at is a fact about the screen — a phone in bed and a desktop at
 * noon legitimately disagree — where a reading POSITION is a fact about the
 * person and syncs (site/reading-position.js). Storage that throws (Safari
 * private mode, a locked-down profile) costs the preference and nothing else.
 */

(function () {
  'use strict';

  var KEY = 'rd:mode';
  /** ⚠️ THE registry, in one place — the same discipline theme.js applies to
   *  its theme list, and for the same reason: `wire()` below BUILDS the
   *  <select>'s options from this, so the page's markup may not carry its own
   *  <option> list and cannot drift from it. */
  var MODES = ['match', 'paper', 'ink'];
  var LABELS = { match: 'Match theme', paper: 'Paper', ink: 'Ink' };
  var DEFAULT_MODE = 'match';

  function read() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function write(v) {
    try { localStorage.setItem(KEY, v); } catch (e) { /* preference just won't persist */ }
  }

  var stored = read();
  var state = MODES.indexOf(stored) >= 0 ? stored : DEFAULT_MODE;

  function apply() {
    var docEl = document.documentElement;
    if (state === 'match') docEl.removeAttribute('data-read-mode');
    else docEl.setAttribute('data-read-mode', state);
    try {
      document.dispatchEvent(new CustomEvent('rd-modechange', { detail: { mode: state } }));
    } catch (e) { /* CustomEvent exists everywhere we run; stay quiet if not */ }
  }

  apply();

  window.readerMode = {
    modes: MODES.slice(),
    labels: (function () {
      var out = {};
      for (var i = 0; i < MODES.length; i++) out[MODES[i]] = LABELS[MODES[i]];
      return out;
    })(),
    get: function () { return state; },
    set: function (m) {
      if (MODES.indexOf(m) < 0) return;
      state = m;
      write(m);
      apply();
    },
  };

  // ---- the toolbar control (only when the page carries the markup) ---------
  // ⚠️ Options are BUILT from MODES, never read from the markup — theme.js's
  // wireCog rule, applied here so a fourth mode is one edit to this file.
  function wire() {
    var sel = document.getElementById('rd-mode');
    if (!sel) return;
    while (sel.firstChild) sel.removeChild(sel.firstChild);
    for (var i = 0; i < MODES.length; i++) {
      var opt = document.createElement('option');
      opt.value = MODES[i];
      opt.textContent = LABELS[MODES[i]];
      sel.appendChild(opt);
    }
    sel.value = state;
    sel.addEventListener('change', function () { window.readerMode.set(sel.value); });
    document.addEventListener('rd-modechange', function () { sel.value = state; });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
