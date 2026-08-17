// ⚠️ DO NOT EDIT — VENDORED COPY. Upstream owns this file:
//   catalog-platform/sites/heygabi-home/public/assets/estate-search.js
// Vendored 2026-08-17 (includes the .es-scan-row[hidden] fix of 2026-08-16).
// This site has no sync-from-sibling build step (unlike library/games), so
// refresh is manual: see site/estate/SOURCE-estate-search.txt for the exact
// command. Everything below this banner is upstream, verbatim.
/**
 * estate-search.js — <estate-search>: the ONE reusable cross-catalog search
 * component (search-normalization phase 1, docs/TODO.md item 0, owner-
 * adopted 2026-08-15). Framework-agnostic custom element, no build step,
 * Shadow DOM for style isolation — same "browser-native, no build, no
 * bundler" precedent as estate-auth.js and theme.js.
 *
 * EXTRACTED FROM find.js (heygabi-home's front-door search): this file IS
 * find.js's behavior, turned into a configurable custom element, so a search
 * improvement made HERE reaches every site that embeds the tag instead of
 * dying at the apex the way every previous find.js fix has. The apex itself
 * now consumes this file (see index.html) rather than carrying its own copy
 * of the logic — one implementation, not a fork.
 *
 * WHY a custom element and not a framework component: three of the four
 * consuming sites are plain HTML/JS (apex, audiobook, /universes) and two are
 * React (library, games) — a vanilla element with attributes/properties is
 * the one shape every one of them can consume, directly or through a two-line
 * wrapper.
 *
 * ---------------------------------------------------------------------------
 * CONFIGURATION — attributes (kebab-case) each mirrored by a JS property
 * (camelCase); properties always win when both are set.
 * ---------------------------------------------------------------------------
 *   index-url      The index Worker origin. Default 'https://index.heygabi.ai'.
 *   source         Scope preset: 'all' | 'audiobook' | 'library' | 'game'.
 *                  Sent to GET /api/search as &source=… (search-route.ts,
 *                  added for this component). It can only NARROW the
 *                  caller's own visibility, never widen it — an authless
 *                  library-scoped box searches nothing, because anonymous
 *                  visibility is {audiobook} only (estate design §4.5).
 *                  Default 'all'.
 *   auth           'authless' (default) | 'authed'.
 *                    authless — every query goes tokenless, forever: the
 *                      public audiobook slice, zero setup, no Firebase cost.
 *                      Nothing is ever imported beyond this file.
 *                    authed — wires the neutral-boot / sign-in / bearer-token
 *                      pattern find.js pioneered, via a DYNAMICALLY imported
 *                      auth adapter module (see auth-module) — never a static
 *                      import, so authless embeds never pay for Firebase.
 *   auth-module    Path to the adapter module, imported only when
 *                  auth="authed" and no .authAdapter property is set. Must
 *                  export the estate-auth.js surface: watchAuth, idToken,
 *                  signIn, signOutUser, handleRedirectResult. Default:
 *                  'estate-auth.js' resolved NEXT TO THIS FILE
 *                  (import.meta.url) — the sync scripts' own precedent is to
 *                  vendor sibling assets together (sync-estate-theme.mjs),
 *                  so a site that copies both files this way gets a working
 *                  default for free.
 *   min-chars      Minimum query length before a search fires. Default 2.
 *   debounce-ms    Debounce delay in ms. Default 250.
 *   placeholder    Input placeholder once ready (authless-ready, or signed
 *                  out once auth resolves). Default: find.js's own copy.
 *   placeholder-authed  Input placeholder once signed in (authed mode only).
 *   sign-in-label  Text on the sign-in button (authed mode only).
 *   hint           The helper line under the box. Omit the attribute (rather
 *                  than passing empty) to keep find.js's own copy; pass an
 *                  empty string explicitly to hide the line.
 *   universes      'true' (default) | 'false' — show the cross-catalog
 *                  "Universes" result group and "everything in X →" follow-
 *                  up buttons. /api/universe stays members-only server-side
 *                  regardless; authless/signed-out callers get the sign-in
 *                  invitation, unchanged from find.js. Also gates
 *                  `universeSuggestions` rendering (see below) — one flag,
 *                  both surfaces, since they share the exact idiom.
 *   scan           Presence-gated (boolean attribute; any value, including
 *                  none, turns it on — absence turns it off). Shows two
 *                  ICON-ONLY buttons (aria-label + title carry the words;
 *                  hover tooltips are the discoverability, per owner order):
 *                  the BARCODE-glyph button opens the rear camera and decodes
 *                  a book's back-cover barcode, resolving it to a title/author
 *                  via the public Open Library API and feeding the title into
 *                  this component's own search path. The CAMERA-glyph button
 *                  ("Scan a shelf",
 *                  authed mode only — vision costs money) opens a native
 *                  file input (`accept="image/*" capture="environment"`,
 *                  camera on mobile / picker on desktop) and identifies every
 *                  spine it can read, rendering a per-title scoped search
 *                  answer for each. There is no separate manual-ISBN box any
 *                  more (owner: "why can we not just search an isbn?") — the
 *                  MAIN search input itself detects a complete, checksum-
 *                  valid ISBN (10 or 13 digits, via estate-scan.js's
 *                  parseIsbnQuery) and routes it through the same resolve
 *                  flow automatically; see `_scheduleSearch`/
 *                  `_runSearchOrIsbn`. ALL of the scanning logic (camera,
 *                  barcode decode, ISBN parse/resolve, shelf-photo capture/
 *                  identify) lives in the sibling canonical module
 *                  `estate-scan.js` (see `scan-module` below) — this
 *                  component only owns the UI wiring, per the "change
 *                  scanning in ONE place" rule. Ignored entirely unless
 *                  `scan-module` resolves; see its own header for the full
 *                  scanning contract and the library_catalog provenance.
 *   scan-module    Path to the estate-scan.js adapter, imported ONLY when a
 *                  `scan`-gated control is first used (📷 tapped, 📚 tapped,
 *                  or a typed query first parses as a candidate ISBN) —
 *                  never a static import, so a site that never sets `scan`
 *                  never pays for the scanner module, same reasoning as
 *                  `auth-module` below. Default: 'estate-scan.js' resolved
 *                  NEXT TO THIS FILE.
 *
 * PROPERTIES (JS-only — for callback/object config no attribute can carry)
 *   .intakeFilter(data, { kind }) → data
 *       Per-site INTAKE FILTER hook: called on every parsed /api/search or
 *       /api/universe response before render (kind: 'search' | 'universe'),
 *       so a host page can narrow further — e.g. a library app dropping
 *       non-library entries out of a same-work group, or a per-format filter
 *       — without forking the component. Return the data to render (mutate
 *       in place and return it, or return a replacement). Identity by
 *       default.
 *   .authAdapter = { watchAuth, idToken, signIn, signOutUser,
 *       handleRedirectResult }
 *       Set directly to skip the dynamic import entirely — for a host that
 *       already has an estate-auth-shaped module loaded (e.g. a React app
 *       importing it as a normal module dependency).
 *
 * EVENTS (bubbles: true, composed: true — cross the shadow boundary)
 *   'estate-search:auth'    detail: { user, resolved } — fires once auth
 *       state is known, and again on every change. authed mode only.
 *   'estate-search:select'  detail: { url, hit }, CANCELABLE — fired instead
 *       of the default `window.open(url, '_blank', 'noopener')` whenever a
 *       result would open (Enter, click, or a card's own link). Call
 *       preventDefault() to take over navigation (an SPA router, for
 *       instance) — the component does nothing else once prevented.
 *
 * WHAT IS PRESERVED FROM find.js, VERBATIM (do not re-derive, do not "fix"):
 *   - the sign-in flash fix: neutral boot (disabled input, hidden buttons)
 *     until watchAuth's first callback, 8s backstop (authed mode);
 *   - §4.5's anonymous rule: signed out (or authless) searches go tokenless
 *     and the server answers the public slice — never disabled, never a 401;
 *   - the debounced-abortable query pattern (AbortController, 250ms pause,
 *     an aborted request renders nothing — a newer keystroke always wins);
 *   - ranked-group rendering (books = same-work groups, games = individual
 *     rows, universes = follow-up buttons) and the "in catalog, not owned"
 *     caveat copy — load-bearing, per find.js's own header, unchanged here;
 *   - full keyboard nav (↑/↓/Enter/Escape, aria-activedescendant / aria-
 *     selected, role="combobox"/"listbox"/"option").
 *
 * ADDED SINCE (four owner-ordered upgrades, this pass):
 *   - accessories de-clutter (task 1): `_renderUniverse` splits game rows by
 *     kind — base/expansion stay in "Games", kind='accessory'/'promo' collapse
 *     into a collapsed-by-default `_accessoriesDetails()` subsection. The
 *     RANKING half (accessories/promos sorting below books/audiobooks/base-
 *     expansion games) is server-side, in index-worker's search.ts — this
 *     component inherits it for free by rendering server order unchanged;
 *   - member-implied universe autofill (task 4): `_renderSearch` merges the
 *     server's additive `data.universeSuggestions` into the same "Universes"
 *     group as `data.universes` — the server already excludes any name the
 *     query text itself matched, so the merge needs no client-side dedup.
 *
 * SERIES FOLDS (owner: "add sub sections that can collapse for series. No
 * need to see loose books", 2026-08-15): `_renderUniverse`'s "Books &
 * audiobooks" and "Games" groups are now each broken into per-series
 * `<details>` (collapsed by default, summary "SeriesName (N)" — the count is
 * the overview, which is the de-clutter), plus one collapsed catch-all fold
 * for rows with no series ("Standalones" for books, "Other games" for games).
 * The accessories fold is unaffected — it still collapses straight out of
 * `games`, upstream of this split, and stays last. Series folds sort
 * alphabetically; the catch-all fold sorts last, before the next group.
 * The exported `groupBySeries()` below is the shared grouping logic —
 * universes.js (the /universes page's OWN hand-rolled renderer, duplicated
 * from this file's idiom on purpose per its own header) imports it rather
 * than re-deriving a third copy, since both renderers already draw on the
 * same `entry.series`/`series_index` columns (index-worker's read.ts
 * ENTRY_COLS, ordered `series, series_index, title` at the DB) and the
 * grouping math has nothing DOM- or Shadow-DOM-specific about it. Each
 * renderer keeps its OWN details/summary DOM-building (different class names,
 * `.es-*` vs `.find-*`) — only the pure data grouping is shared.
 *
 * NOT ported: the apex's approver-probe "Admin" chip (find.js's
 * probeApprover()). That is heygabi-home-specific admin surface, not a
 * generic search behavior — baking it in here would make every future
 * consumer carry an apex opinion. The apex keeps it as a thin adapter that
 * listens for 'estate-search:auth' (see index.html) instead.
 */

/**
 * Groups universe-view rows (already filtered to one kind — books, or games)
 * by `series` for the series-folds treatment (see header). Pure data, no DOM,
 * so both this component and universes.js's hand-rolled renderer can share
 * it without sharing render idiom.
 *
 * Returns { seriesGroups, standalone } —
 *   seriesGroups: [{ name, rows }], sorted alphabetically by `name`; each
 *     group's own rows sorted by `series_index` (numeric rows first, in
 *     order; a row with a null index — not supposed to happen once a series
 *     is assigned, but never trusted — falls after its numbered siblings;
 *     ties/nulls-vs-nulls break on title). This is belt-and-suspenders: the
 *     server already orders by `series, series_index, title` (read.ts), so
 *     in practice this sort is a no-op confirming that order, not the source
 *     of truth for it.
 *   standalone: rows with no `series` at all, in their incoming (server)
 *     order — the caller renders these as one collapsed catch-all fold.
 */
export function groupBySeries(rows) {
  const bySeries = new Map();
  const standalone = [];
  for (const row of rows) {
    if (row.series) {
      if (!bySeries.has(row.series)) bySeries.set(row.series, []);
      bySeries.get(row.series).push(row);
    } else {
      standalone.push(row);
    }
  }
  const seriesGroups = [...bySeries.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([name, seriesRows]) => ({
      name,
      rows: [...seriesRows].sort((x, y) => {
        const xi = x.series_index, yi = y.series_index;
        if (xi != null && yi != null && xi !== yi) return xi - yi;
        if (xi != null && yi == null) return -1;
        if (xi == null && yi != null) return 1;
        return x.title.localeCompare(y.title);
      }),
    }));
  return { seriesGroups, standalone };
}

(function () {
  'use strict';

  const DEFAULT_INDEX_ORIGIN = 'https://index.heygabi.ai';
  const DEFAULT_DEBOUNCE_MS = 250;
  const DEFAULT_MIN_CHARS = 2;
  const AUTH_BACKSTOP_MS = 8000;
  const FULL_SCOPE_SIZE = 3;

  const SOURCE_LABELS = { game: 'board games', library: 'library', audiobook: 'audiobooks' };
  /** The server's scope vocabulary (§4.5 catalogs), spoken like a person. */
  const SCOPE_LABELS = { audiobook: 'audiobooks', library: 'the library', games: 'board games' };

  const DEFAULT_PLACEHOLDER_ANON = 'Search the audiobook shelf…';
  const DEFAULT_PLACEHOLDER_AUTHED = 'Start typing a title, author or series…';
  const DEFAULT_SIGNIN_LABEL = 'Sign in to search everything';
  const DEFAULT_HINT =
    '“Do we own this in any format?” — one title, checked against every shelf at once.';

  /**
   * Inline SVG icons (owner order 2026-08-15): the barcode button shows a
   * BARCODE and the shelf/cover button shows a CAMERA. The previous 📷/📚
   * pair read exactly backwards — the camera glyph sat on the barcode
   * scanner. There is no barcode emoji, so both became SVGs; currentColor
   * keeps them on-theme, aria-hidden because the words stay in
   * aria-label/title (icon-only rule unchanged). `stop`/`busy` are the
   * in-flight states of those same two buttons.
   */
  const ES_ICONS = {
    barcode:
      '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true"><path d="M2 5h2v14H2zM5.5 5h1v14h-1zM8 5h2v14H8zM11.5 5h1v14h-1zM14 5h3v14h-3zM18.5 5h1v14h-1zM21 5h1v14h-1z"/></svg>',
    photo:
      '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>',
    stop:
      '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>',
    busy:
      '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 2h10M7 22h10M8 2v3.5L12 10l4-4.5V2M8 22v-3.5L12 14l4 4.5V22"/></svg>',
  };

  const TEMPLATE = document.createElement('template');
  TEMPLATE.innerHTML = `
    <style>
      :host { display: block; }
      .es-box {
        padding: 1rem 1.15rem 1.1rem;
        border: 1px solid var(--et-hairline);
        border-radius: var(--et-radius-lg);
        background: color-mix(in srgb, var(--et-surface) 65%, transparent);
      }
      .es-form { display: flex; gap: .6rem; align-items: stretch; flex-wrap: wrap; }
      .es-input {
        flex: 1 1 14rem;
        min-height: 44px;
        padding: .5rem .85rem;
        border: 1px solid var(--et-field-border, var(--et-hairline));
        border-radius: var(--et-radius);
        background: var(--et-surface);
        color: var(--et-fg);
        font: inherit;
      }
      .es-input::placeholder { color: var(--et-muted); }
      .es-input:focus-visible { outline: 2px solid var(--et-accent); outline-offset: 1px; }
      .es-input:disabled { opacity: .6; }
      .es-btn {
        min-height: 44px;
        padding: .5rem 1.05rem;
        border: var(--et-btn-border);
        border-radius: var(--et-radius);
        background: var(--et-btn-bg);
        color: var(--et-btn-fg);
        box-shadow: var(--et-btn-shadow);
        font: inherit;
        font-weight: 600;
        cursor: pointer;
      }
      .es-btn:hover { filter: brightness(1.06); }
      .es-btn:active { transform: var(--et-press); }
      .es-btn:focus-visible { outline: 2px solid var(--et-accent); outline-offset: 2px; }
      .es-btn:disabled { opacity: .6; cursor: default; transform: none; }
      .es-hint { margin: .6rem 0 0; color: var(--et-muted); font-size: var(--et-text-small); text-wrap: pretty; }
      .es-who { margin: .6rem 0 0; color: var(--et-muted); font-size: var(--et-text-small); }
      .es-linkbtn {
        border: 0; padding: 0; min-height: 0; background: none;
        color: var(--et-accent); font: inherit; font-size: inherit; font-weight: 600;
        cursor: pointer; text-decoration: underline;
      }
      .es-linkbtn:focus-visible { outline: 2px solid var(--et-accent); outline-offset: 2px; border-radius: 4px; }
      .es-status {
        margin: .8rem 0 0; padding: .6rem .85rem;
        border: 1px solid var(--et-hairline); border-radius: var(--et-radius);
        background: color-mix(in srgb, var(--et-surface) 55%, transparent);
        color: var(--et-muted); font-size: var(--et-text-small); text-wrap: pretty;
      }
      .es-status[data-tone="warn"] { border-color: color-mix(in srgb, var(--et-danger) 45%, var(--et-hairline)); color: var(--et-fg); }
      .es-status[data-tone="owner"] { border-color: color-mix(in srgb, var(--et-accent-2) 55%, var(--et-hairline)); color: var(--et-fg); }
      .es-caveat { margin: 1rem 0 .3rem; color: var(--et-muted); font-size: var(--et-text-small); text-wrap: pretty; }
      .es-group {
        margin: 1rem 0 .5rem; font-size: var(--et-text-micro); font-weight: 700;
        letter-spacing: .1em; text-transform: uppercase; color: var(--et-muted);
      }
      /* Accessories de-clutter (task 1) and series folds (owner: "add sub
         sections that can collapse for series"): both are a native <details>,
         collapsed by default — one shared look for every fold the universe
         expansion view uses (accessories, one per series, and the
         Standalones/Other-games catch-all). */
      details.es-accessories, details.es-series { margin: 1rem 0 .5rem; }
      details.es-accessories > summary, details.es-series > summary {
        cursor: pointer; margin: 0; font-size: var(--et-text-micro); font-weight: 700;
        letter-spacing: .1em; text-transform: uppercase; color: var(--et-muted);
      }
      details.es-accessories > summary:hover, details.es-series > summary:hover { color: var(--et-accent); }
      details.es-accessories > summary:focus-visible, details.es-series > summary:focus-visible { outline: 2px solid var(--et-accent); outline-offset: 2px; }
      details.es-accessories > .es-hits, details.es-series > .es-hits { margin-top: .5rem; }
      .es-hits { list-style: none; margin: 0; padding: 0; display: grid; gap: .5rem; }
      .es-hit {
        display: flex; gap: .8rem; align-items: flex-start;
        padding: .65rem .75rem; border: 1px solid var(--et-hairline);
        border-radius: var(--et-radius); background: var(--et-surface);
      }
      .es-hit[aria-selected="true"] { border-color: var(--et-accent); outline: 1px solid var(--et-accent); }
      .es-hit-cover {
        flex: none; display: block; width: 2.6rem; height: 3.6rem;
        border-radius: calc(var(--et-radius) / 2); border: 1px solid var(--et-hairline);
        background: var(--et-bg); overflow: hidden;
      }
      .es-hit-cover img { display: block; width: 100%; height: 100%; object-fit: cover; }
      .es-hit-body { display: flex; flex-direction: column; gap: .15rem; min-width: 0; }
      .es-hit-title { font-weight: 600; }
      .es-hit-title a { color: var(--et-fg); text-decoration: none; }
      .es-hit-title a:hover { color: var(--et-accent); text-decoration: underline; }
      .es-hit-meta { color: var(--et-muted); font-size: var(--et-text-small); }
      .es-hit-meta a { color: var(--et-accent); font-weight: 600; text-decoration: none; }
      .es-hit-meta a:hover { text-decoration: underline; }
      .es-hit-universe { align-self: flex-start; font-size: var(--et-text-small); margin-top: .15rem; }
      /* Barcode/shelf scan (scan attribute) — see estate-scan.js for the
         logic; this is UI only. Icon-only controls (owner order): the words
         live in aria-label/title, not in the button's visible text. */
      .es-scan-row { display: flex; gap: .6rem; align-items: stretch; flex-wrap: wrap; margin-top: .6rem; }
      /* ⚠️ An author display rule BEATS the UA stylesheet's [hidden] rule, so
         .es-scan-row rendered its two scan buttons on every embed that never
         asked for them — non-functional, because nothing had wired them up.
         Found 2026-08-16 by exercising the component in a real browser during
         the library adoption; typecheck, unit tests and vite build were all
         green on it, which is exactly the point: an attribute can read
         hidden=true while the pixels show the button.
         .es-camera-stage[hidden] below already did this; the omission here was
         an oversight, not a decision.
         ⚠️ NO BACKTICKS IN THIS BLOCK — it sits inside a JS template literal,
         and one backtick ends the string. That mistake was made writing this
         very comment and caught by node --check. */
      .es-scan-row[hidden] { display: none; }
      .es-icon-btn { flex: none; width: 44px; padding: 0; font-size: 1.2rem; line-height: 1; display: inline-flex; align-items: center; justify-content: center; }
      .es-icon-btn svg { display: block; }
      .es-camera-stage { margin-top: .6rem; display: flex; flex-direction: column; gap: .5rem; align-items: flex-start; }
      .es-camera-stage[hidden] { display: none; }
      .es-scan-video { width: 100%; max-width: 24rem; border-radius: var(--et-radius); border: 1px solid var(--et-hairline); background: #000; }
      .es-scan-resolve { margin: .8rem 0 0; padding: .6rem .85rem; border: 1px solid var(--et-hairline); border-radius: var(--et-radius); background: color-mix(in srgb, var(--et-accent) 8%, var(--et-surface)); font-size: var(--et-text-small); }
      .es-scan-resolve[data-tone="bad"] { border-color: color-mix(in srgb, var(--et-danger) 45%, var(--et-hairline)); }
      .es-scan-add { margin-top: .5rem; display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }
      /* Shelf-scan per-title results (Scan a shelf → identifyPhoto). */
      .es-shelf-list { list-style: none; margin: 0; padding: 0; display: grid; gap: .6rem; }
      .es-shelf-row { padding: .6rem .75rem; border: 1px solid var(--et-hairline); border-radius: var(--et-radius); background: var(--et-surface); }
      .es-shelf-title { margin: 0; font-weight: 600; }
      .es-shelf-title button { background: none; border: 0; padding: 0; font: inherit; font-weight: 600; color: var(--et-fg); cursor: pointer; text-align: left; }
      .es-shelf-title button:hover { color: var(--et-accent); text-decoration: underline; }
      .es-shelf-note { margin: .2rem 0 0; color: var(--et-muted); font-size: var(--et-text-small); font-style: italic; }
      .es-shelf-answer { margin: .3rem 0 0; color: var(--et-muted); font-size: var(--et-text-small); }
    </style>
    <div class="es-box">
      <form class="es-form">
        <input class="es-input" type="search" placeholder="One moment…" autocomplete="off" disabled aria-label="Search the catalogues by title">
        <button class="es-btn es-submit" type="submit" hidden>Search</button>
        <button class="es-btn es-signin" type="button" hidden>Sign in to search everything</button>
      </form>
      <div class="es-scan-row" hidden>
        <button class="es-btn es-icon-btn es-scan-btn" type="button" aria-label="Scan a barcode" title="Scan a barcode">${ES_ICONS.barcode}</button>
        <button class="es-btn es-icon-btn es-shelf-btn" type="button" aria-label="Scan a shelf" title="Scan a shelf" hidden>${ES_ICONS.photo}</button>
        <input class="es-shelf-file" type="file" accept="image/*" capture="environment" hidden aria-hidden="true" tabindex="-1">
      </div>
      <div class="es-camera-stage" hidden>
        <!-- muted + playsinline are load-bearing on iOS: without them WebKit
             either blocks autoplay or takes the video fullscreen. -->
        <video class="es-scan-video" playsinline muted></video>
        <button class="es-btn es-scan-stop" type="button">Stop camera</button>
      </div>
      <p class="es-scan-resolve" hidden></p>
      <p class="es-hint"></p>
      <p class="es-who" hidden></p>
      <p class="es-status" hidden></p>
      <div class="es-results"></div>
    </div>
  `;

  /**
   * The signed-in "who" line supports ONE extension point: a light-DOM child
   * carrying slot="who-extra" (e.g. `<span slot="who-extra" hidden> ·
   * <a href="/admin">Admin</a></span>`) renders right after "Signed in as …
   * · sign out". This is how the apex adds its approver-only Admin chip
   * (find.js's probeApprover()) without the component knowing "Admin" is a
   * concept — a host-page adapter toggles the slotted element's `hidden`.
   * Unused by a host that supplies no such child; nothing renders.
   */

  class EstateSearch extends HTMLElement {
    static get observedAttributes() {
      return [
        'index-url', 'source', 'auth', 'auth-module', 'min-chars', 'debounce-ms',
        'placeholder', 'placeholder-authed', 'sign-in-label', 'hint', 'universes',
        'scan', 'scan-module',
      ];
    }

    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(TEMPLATE.content.cloneNode(true));

      this.intakeFilter = null;
      this.authAdapter = null;

      this._currentUser = null;
      this._authResolved = false;
      this._authBackstop = null;
      this._isApprover = false; // unused by this component; kept for host adapters that peek at internals

      this._navItems = [];
      this._activeIndex = -1;
      this._debounceTimer = 0;
      this._inflight = null;

      // -- scan (estate-scan.js, dynamically imported on first use) --------
      this._scanModulePromise = null;
      this._scanStream = null;
      this._scanStopLoop = null;
      this._scanRunning = false;
      this._shelfBusy = false;

      this._onInput = this._onInput.bind(this);
      this._onKeydown = this._onKeydown.bind(this);
      this._onSubmit = this._onSubmit.bind(this);
      this._onSigninClick = this._onSigninClick.bind(this);
      this._onScanBtnClick = this._onScanBtnClick.bind(this);
      this._onShelfBtnClick = this._onShelfBtnClick.bind(this);
      this._onShelfFileChange = this._onShelfFileChange.bind(this);
    }

    // -- config -----------------------------------------------------------

    get indexUrl() { return this.getAttribute('index-url') || DEFAULT_INDEX_ORIGIN; }
    get sourcePreset() {
      const v = (this.getAttribute('source') || 'all').trim().toLowerCase();
      return v === '' ? 'all' : v;
    }
    get authMode() {
      const v = (this.getAttribute('auth') || 'authless').trim().toLowerCase();
      return v === 'authed' ? 'authed' : 'authless';
    }
    get authModulePath() { return this.getAttribute('auth-module') || null; }
    get minChars() {
      const n = parseInt(this.getAttribute('min-chars'), 10);
      return Number.isFinite(n) && n > 0 ? n : DEFAULT_MIN_CHARS;
    }
    get debounceMs() {
      const n = parseInt(this.getAttribute('debounce-ms'), 10);
      return Number.isFinite(n) && n >= 0 ? n : DEFAULT_DEBOUNCE_MS;
    }
    get showUniverses() { return this.getAttribute('universes') !== 'false'; }
    get scanEnabled() { return this.hasAttribute('scan'); }
    get scanModulePath() { return this.getAttribute('scan-module') || null; }

    // -- lifecycle ----------------------------------------------------------

    connectedCallback() {
      const root = this.shadowRoot;
      this._form = root.querySelector('.es-form');
      this._input = root.querySelector('.es-input');
      this._submitBtn = root.querySelector('.es-submit');
      this._signinBtn = root.querySelector('.es-signin');
      this._hintEl = root.querySelector('.es-hint');
      this._whoEl = root.querySelector('.es-who');
      this._statusEl = root.querySelector('.es-status');
      this._resultsEl = root.querySelector('.es-results');
      this._scanRow = root.querySelector('.es-scan-row');
      this._scanBtn = root.querySelector('.es-scan-btn');
      this._shelfBtn = root.querySelector('.es-shelf-btn');
      this._shelfFileInput = root.querySelector('.es-shelf-file');
      this._cameraStage = root.querySelector('.es-camera-stage');
      this._scanVideo = root.querySelector('.es-scan-video');
      this._scanStopBtn = root.querySelector('.es-scan-stop');
      this._scanResolveEl = root.querySelector('.es-scan-resolve');

      this._input.setAttribute('role', 'combobox');
      this._input.setAttribute('aria-autocomplete', 'list');
      this._input.setAttribute('aria-expanded', 'false');
      this._resultsEl.setAttribute('role', 'listbox');
      this._resultsEl.setAttribute('aria-label', 'Search results');

      const hintAttr = this.getAttribute('hint');
      this._hintEl.textContent = hintAttr !== null ? hintAttr : DEFAULT_HINT;
      if (this._hintEl.textContent === '') this._hintEl.hidden = true;

      this._input.addEventListener('input', this._onInput);
      this._input.addEventListener('keydown', this._onKeydown);
      this._form.addEventListener('submit', this._onSubmit);
      this._signinBtn.addEventListener('click', this._onSigninClick);
      this._signinBtn.textContent = this.getAttribute('sign-in-label') || DEFAULT_SIGNIN_LABEL;

      if (this.scanEnabled) {
        this._scanRow.hidden = false;
        this._scanBtn.addEventListener('click', this._onScanBtnClick);
        this._scanStopBtn.addEventListener('click', () => this._stopScan());
        // Scan-a-shelf is authed-only — vision costs money, and authless mode
        // has no path to ever get an idToken, so the button stays hidden
        // there rather than existing only to fail. Re-checked in
        // _renderAuthState() too (sign-in can happen after this runs).
        this._shelfBtn.hidden = this.authMode !== 'authed';
        this._shelfBtn.addEventListener('click', this._onShelfBtnClick);
        this._shelfFileInput.addEventListener('change', this._onShelfFileChange);
      }

      if (this.authMode === 'authed') {
        this._bootAuthed();
      } else {
        this._authResolved = true;
        this._renderAuthState();
      }
    }

    disconnectedCallback() {
      if (this._authBackstop) clearTimeout(this._authBackstop);
      if (this._inflight) this._inflight.abort();
      clearTimeout(this._debounceTimer);
      this._stopScan();
    }

    attributeChangedCallback(name) {
      if (!this.shadowRoot || !this._input) return; // before connectedCallback finishes
      if (name === 'sign-in-label' && this._signinBtn) {
        this._signinBtn.textContent = this.getAttribute('sign-in-label') || DEFAULT_SIGNIN_LABEL;
      }
      if (name === 'hint' && this._hintEl) {
        const hintAttr = this.getAttribute('hint');
        this._hintEl.textContent = hintAttr !== null ? hintAttr : DEFAULT_HINT;
        this._hintEl.hidden = this._hintEl.textContent === '';
      }
      if (name === 'placeholder' || name === 'placeholder-authed') {
        this._renderAuthState();
      }
    }

    // -- auth (authed mode only) --------------------------------------------

    /**
     * ⚠️ THE SIGN-IN FLASH FIX, preserved verbatim (owner-found, find.js,
     * 2026-08-14): Firebase reads its persisted session ASYNCHRONOUSLY, so a
     * component that renders signed-out immediately shows a returning member
     * "sign in" for however long the SDK takes. The box boots NEUTRAL
     * (disabled, both buttons hidden) and renders nothing decisive until the
     * adapter's watchAuth FIRST callback. The backstop below is only for the
     * adapter never answering at all (blocked script, dead network).
     */
    async _bootAuthed() {
      this._authBackstop = setTimeout(() => {
        if (!this._authResolved) {
          this._authResolved = true;
          this._renderAuthState();
        }
      }, AUTH_BACKSTOP_MS);

      let adapter = this.authAdapter;
      if (!adapter) {
        try {
          const path = this.authModulePath || new URL('estate-auth.js', import.meta.url).href;
          adapter = await import(/* @vite-ignore */ path);
          this.authAdapter = adapter;
        } catch (e) {
          console.warn('[estate-search] auth adapter failed to load; falling back to authless behavior:', e);
          clearTimeout(this._authBackstop);
          this._authResolved = true;
          this._renderAuthState();
          return;
        }
      }

      adapter.watchAuth((user) => {
        this._authResolved = true;
        clearTimeout(this._authBackstop);
        const changed = this._currentUser !== user;
        this._currentUser = user;
        this._renderAuthState();
        this.dispatchEvent(new CustomEvent('estate-search:auth', {
          bubbles: true, composed: true, detail: { user, resolved: true },
        }));
        // Crossing the sign-in boundary changes the scope: re-run a standing
        // query so results widen (or narrow) without a re-type.
        if (changed && this._input.value.trim().length >= this.minChars) this._scheduleSearch();
      });

      if (typeof adapter.handleRedirectResult === 'function') {
        adapter.handleRedirectResult().then((err) => {
          if (err) this._setStatus(err, err.includes('authorised domain') ? 'owner' : 'warn');
        });
      }
    }

    async _idToken() {
      if (this.authMode !== 'authed' || !this._currentUser || !this.authAdapter) return null;
      return this.authAdapter.idToken();
    }

    // -- UI state -------------------------------------------------------------

    _setStatus(text, tone) {
      this._statusEl.textContent = text || '';
      this._statusEl.dataset.tone = tone || '';
      this._statusEl.hidden = !text;
    }

    _renderAuthState() {
      const input = this._input;
      if (this.authMode === 'authed' && !this._authResolved) {
        // Neutral: no claim either way until the adapter answers.
        input.disabled = true;
        input.placeholder = 'One moment…';
        this._submitBtn.hidden = true;
        this._signinBtn.hidden = true;
        this._whoEl.hidden = true;
        return;
      }
      const signedIn = this.authMode === 'authed' && this._currentUser !== null;
      // §4.5: the box works for EVERYONE — signed out (or authless) it
      // searches the public slice, so it is never disabled once ready.
      input.disabled = false;
      this._submitBtn.hidden = false;
      this._signinBtn.hidden = signedIn || this.authMode !== 'authed';
      if (signedIn) {
        this._whoEl.innerHTML = '';
        const name = document.createElement('span');
        name.textContent = this._currentUser.displayName || this._currentUser.email;
        const out = document.createElement('button');
        out.type = 'button';
        out.className = 'es-linkbtn';
        out.textContent = 'sign out';
        out.addEventListener('click', async () => {
          if (this.authAdapter && this.authAdapter.signOutUser) await this.authAdapter.signOutUser();
          this._clearResults();
          this._setStatus('');
        });
        this._whoEl.append('Signed in as ', name, ' · ', out);
        const slot = document.createElement('slot');
        slot.name = 'who-extra';
        this._whoEl.appendChild(slot);
        this._whoEl.hidden = false;
        input.placeholder = this.getAttribute('placeholder-authed') || DEFAULT_PLACEHOLDER_AUTHED;
      } else {
        this._whoEl.hidden = true;
        this._whoEl.innerHTML = '';
        input.placeholder = this.getAttribute('placeholder') || DEFAULT_PLACEHOLDER_ANON;
      }
    }

    // -- results + keyboard nav ----------------------------------------------

    _clearResults() {
      this._resultsEl.innerHTML = '';
      this._navItems = [];
      this._activeIndex = -1;
      this._input.removeAttribute('aria-activedescendant');
      this._input.setAttribute('aria-expanded', 'false');
    }

    _setActive(i) {
      if (this._activeIndex >= 0 && this._navItems[this._activeIndex]) {
        this._navItems[this._activeIndex].el.setAttribute('aria-selected', 'false');
      }
      this._activeIndex = i;
      const item = this._navItems[i];
      if (i >= 0 && item) {
        item.el.setAttribute('aria-selected', 'true');
        this._input.setAttribute('aria-activedescendant', item.el.id);
        item.el.scrollIntoView({ block: 'nearest' });
      } else {
        this._input.removeAttribute('aria-activedescendant');
      }
    }

    _registerNav(el, open) {
      el.id = `es-opt-${this._instanceId()}-${this._navItems.length}`;
      el.setAttribute('role', 'option');
      el.setAttribute('aria-selected', 'false');
      this._navItems.push({ el, open });
    }

    _instanceId() {
      if (!this.__iid) this.__iid = Math.random().toString(36).slice(2, 8);
      return this.__iid;
    }

    _sourceLabel(source) { return SOURCE_LABELS[source] || source; }

    _scopePhrase(scope) { return scope.map((c) => SCOPE_LABELS[c] || c).join(' and '); }

    _scopeNote(scope) {
      if (!Array.isArray(scope) || scope.length === 0 || scope.length >= FULL_SCOPE_SIZE) return null;
      const p = document.createElement('p');
      p.className = 'es-caveat';
      p.setAttribute('role', 'presentation');
      p.textContent = `Searching ${this._scopePhrase(scope)} only.` +
        (this._currentUser ? '' : ' Sign in to search every shelf.');
      return p;
    }

    _openHit(url, hit) {
      const evt = new CustomEvent('estate-search:select', {
        bubbles: true, composed: true, cancelable: true, detail: { url, hit },
      });
      const proceed = this.dispatchEvent(evt);
      if (proceed && url) window.open(url, '_blank', 'noopener');
    }

    _metaBits(row) {
      const bits = [];
      if (row.creator) bits.push(row.creator);
      bits.push(row.format);
      if (row.kind && row.kind !== 'base') bits.push(row.kind);
      if (row.parent_source_id) bits.push('belongs with a base game');
      if (row.series) bits.push(row.series_index != null ? `${row.series} #${row.series_index}` : row.series);
      if (row.year) bits.push(String(row.year));
      if (row.publisher) bits.push(row.publisher);
      return bits.join(' · ');
    }

    _coverFor(li, row) {
      const box = document.createElement('span');
      box.className = 'es-hit-cover';
      box.setAttribute('aria-hidden', 'true');
      if (row && row.cover_url) {
        const img = document.createElement('img');
        img.alt = '';
        img.loading = 'lazy';
        img.width = 42;
        img.height = 58;
        img.src = row.cover_url;
        img.addEventListener('error', () => img.remove());
        box.appendChild(img);
      }
      li.appendChild(box);
    }

    _rowCard(row) {
      const li = document.createElement('li');
      li.className = 'es-hit';
      this._coverFor(li, row);

      const body = document.createElement('div');
      body.className = 'es-hit-body';

      const title = document.createElement('span');
      title.className = 'es-hit-title';
      if (row.detail_url) {
        const a = document.createElement('a');
        a.href = row.detail_url;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = row.title;
        a.addEventListener('click', (e) => {
          e.preventDefault();
          this._openHit(row.detail_url, row);
        });
        title.appendChild(a);
      } else {
        title.textContent = row.title;
      }
      body.appendChild(title);

      const meta = document.createElement('span');
      meta.className = 'es-hit-meta';
      meta.textContent = this._metaBits(row);
      body.appendChild(meta);

      if (row.universe && this.showUniverses) {
        const uni = document.createElement('button');
        uni.type = 'button';
        uni.className = 'es-linkbtn es-hit-universe';
        uni.textContent = `everything in ${row.universe} →`;
        uni.addEventListener('click', () => this._runUniverse(row.universe));
        body.appendChild(uni);
      }

      li.appendChild(body);
      this._registerNav(li, () => this._openHit(row.detail_url, row));
      return li;
    }

    _workCard(hit) {
      const li = document.createElement('li');
      li.className = 'es-hit';
      this._coverFor(li, hit.entries.find((e) => e.cover_url) || null);

      const body = document.createElement('div');
      body.className = 'es-hit-body';

      const title = document.createElement('span');
      title.className = 'es-hit-title';
      title.textContent = hit.title;
      body.appendChild(title);

      const meta = document.createElement('span');
      meta.className = 'es-hit-meta';
      meta.textContent = hit.creator || '';
      if (meta.textContent) body.appendChild(meta);

      const formats = document.createElement('span');
      formats.className = 'es-hit-meta';
      hit.entries.forEach((e, i) => {
        if (i > 0) formats.append(' · ');
        const label = `${this._sourceLabel(e.source)}: ${e.format}`;
        if (e.detail_url) {
          const a = document.createElement('a');
          a.href = e.detail_url;
          a.target = '_blank';
          a.rel = 'noopener';
          a.textContent = label;
          a.addEventListener('click', (ev) => {
            ev.preventDefault();
            this._openHit(e.detail_url, e);
          });
          formats.appendChild(a);
        } else {
          formats.append(label);
        }
      });
      body.appendChild(formats);

      const withUniverse = hit.entries.find((e) => e.universe);
      if (withUniverse && this.showUniverses) {
        const uni = document.createElement('button');
        uni.type = 'button';
        uni.className = 'es-linkbtn es-hit-universe';
        uni.textContent = `everything in ${withUniverse.universe} →`;
        uni.addEventListener('click', () => this._runUniverse(withUniverse.universe));
        body.appendChild(uni);
      }

      li.appendChild(body);
      const first = hit.entries.find((e) => e.detail_url);
      this._registerNav(li, () => this._openHit(first ? first.detail_url : null, first));
      return li;
    }

    _groupHeading(text) {
      const h = document.createElement('h3');
      h.className = 'es-group';
      h.setAttribute('role', 'presentation');
      h.textContent = text;
      return h;
    }

    /** kind='accessory'/'promo' — the accessories de-clutter (owner: "make
     * accessories a sub category in a universe page"). Always present, just
     * collapsed and out of the way; no include-checkbox. */
    _isAccessoryOrPromo(row) {
      return row.kind === 'accessory' || row.kind === 'promo';
    }

    /** A native <details>, COLLAPSED BY DEFAULT (no `open` attribute) — the
     * one shape shared by the accessories fold, each per-series fold, and the
     * Standalones/Other-games catch-all fold. */
    _foldDetails(label, rows, className) {
      const details = document.createElement('details');
      details.className = className;
      const summary = document.createElement('summary');
      summary.textContent = `${label} (${rows.length})`;
      details.appendChild(summary);
      const ul = document.createElement('ul');
      ul.className = 'es-hits';
      ul.setAttribute('role', 'presentation');
      for (const row of rows) ul.appendChild(this._rowCard(row));
      details.appendChild(ul);
      return details;
    }

    _accessoriesDetails(rows) {
      return this._foldDetails('Accessories & promos', rows, 'es-accessories');
    }

    /** Series folds (owner: "add sub sections that can collapse for series.
     * No need to see loose books"): groups `rows` by `series` via the shared
     * groupBySeries() (module-level export above), renders one collapsed
     * fold per series (alphabetical), then one collapsed catch-all fold for
     * the series-less rows, labeled `otherLabel` ("Standalones" for books,
     * "Other games" for games) — last, per the owner's ordering. */
    _seriesFolds(rows, otherLabel) {
      const { seriesGroups, standalone } = groupBySeries(rows);
      const frag = document.createDocumentFragment();
      for (const g of seriesGroups) frag.appendChild(this._foldDetails(g.name, g.rows, 'es-series'));
      if (standalone.length) frag.appendChild(this._foldDetails(otherLabel, standalone, 'es-series'));
      return frag;
    }

    _caveatLine(headingText) {
      const heading = document.createElement('p');
      heading.className = 'es-caveat';
      heading.setAttribute('role', 'presentation');
      // ⚠ Load-bearing copy (find.js's own header): in-catalog, not owned.
      heading.textContent =
        `${headingText} A result means it is in the catalog — some entries are wanted, not owned. ` +
        'Tap through to the owning catalog for owned-versus-wanted.';
      return heading;
    }

    _renderSearch(data) {
      this._clearResults();

      if (data.reason === 'no_catalogs_visible') {
        this._setStatus('Your account currently has no catalogs to search. An approver can restore them.', 'warn');
        return;
      }

      // MEMBER-IMPLIED UNIVERSE AUTOFILL (task 4): `universeSuggestions` is
      // additive to `universes` — the server already excludes any name the
      // query itself matched, so the two arrays never overlap and can be
      // rendered as one combined list, same row idiom, no dedup needed here.
      const universeRows = this.showUniverses
        ? [...data.universes, ...(data.universeSuggestions || [])]
        : [];
      const total = data.books.length + data.games.length + universeRows.length;
      if (total === 0) {
        const where = Array.isArray(data.scope) && data.scope.length < FULL_SCOPE_SIZE
          ? `in ${this._scopePhrase(data.scope)}` : 'on any shelf';
        this._setStatus(
          `Nothing ${where} matches “${data.query}”. The search tries titles, authors and series — a couple more letters can help.` +
          (this._currentUser || !Array.isArray(data.scope) || data.scope.length >= FULL_SCOPE_SIZE
            ? '' : ' Signing in searches every shelf.'),
        );
        return;
      }
      this._setStatus('');
      this._input.setAttribute('aria-expanded', 'true');

      this._resultsEl.appendChild(this._caveatLine(`Matches for “${data.query}”.`));
      const note = this._scopeNote(data.scope);
      if (note) this._resultsEl.appendChild(note);

      if (universeRows.length) {
        this._resultsEl.appendChild(this._groupHeading('Universes — every catalog, every format'));
        const ul = document.createElement('ul');
        ul.className = 'es-hits';
        ul.setAttribute('role', 'presentation');
        for (const u of universeRows) {
          const li = document.createElement('li');
          li.className = 'es-hit';
          const body = document.createElement('div');
          body.className = 'es-hit-body';
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'es-linkbtn es-hit-universe';
          btn.textContent = `everything in ${u.name} (${u.count}) →`;
          btn.addEventListener('click', () => this._runUniverse(u.name));
          body.appendChild(btn);
          li.appendChild(body);
          this._registerNav(li, () => this._runUniverse(u.name));
          ul.appendChild(li);
        }
        this._resultsEl.appendChild(ul);
      }

      if (data.books.length) {
        this._resultsEl.appendChild(this._groupHeading('Books & audiobooks — same work, any format'));
        const ul = document.createElement('ul');
        ul.className = 'es-hits';
        ul.setAttribute('role', 'presentation');
        for (const hit of data.books) ul.appendChild(this._workCard(hit));
        this._resultsEl.appendChild(ul);
      }

      if (data.games.length) {
        this._resultsEl.appendChild(this._groupHeading('Board games — matched on title'));
        const ul = document.createElement('ul');
        ul.className = 'es-hits';
        ul.setAttribute('role', 'presentation');
        for (const hit of data.games) ul.appendChild(this._rowCard(hit));
        this._resultsEl.appendChild(ul);
      }
    }

    _renderUniverse(data) {
      this._clearResults();

      if (!data.matches.length) {
        this._setStatus(`Nothing in any catalog sits in ${data.universe} right now.`);
        return;
      }
      this._setStatus('');
      this._input.setAttribute('aria-expanded', 'true');

      this._resultsEl.appendChild(this._caveatLine(`Everything in ${data.universe} — every catalog, every format.`));

      // Accessories de-clutter (task 1): base/expansion games render in
      // "Games" as before; kind='accessory'/'promo' collapse into a
      // collapsed-by-default details subsection, out of the main list.
      const bookRows = data.matches.filter((m) => m.source === 'library' || m.source === 'audiobook');
      const gameRows = data.matches.filter((m) => m.source === 'game');
      const games = gameRows.filter((m) => !this._isAccessoryOrPromo(m));
      const accessories = gameRows.filter((m) => this._isAccessoryOrPromo(m));

      // Series folds (owner-ordered, this pass): each group's rows are
      // broken into per-series collapsed folds plus one collapsed catch-all
      // ("Standalones" / "Other games") — no plain flat list anymore, so a
      // universe with several series does not dump every volume in the
      // reader's face. Series folds sort alphabetically, catch-all last.
      const groups = [
        { name: 'Books & audiobooks', rows: bookRows, otherLabel: 'Standalones' },
        { name: 'Games', rows: games, otherLabel: 'Other games' },
      ];
      for (const g of groups) {
        if (!g.rows.length) continue;
        this._resultsEl.appendChild(this._groupHeading(g.name));
        this._resultsEl.appendChild(this._seriesFolds(g.rows, g.otherLabel));
      }
      if (accessories.length) this._resultsEl.appendChild(this._accessoriesDetails(accessories));
    }

    // -- queries: debounced, abortable ---------------------------------------

    async _callIndex(path, signal) {
      // Signed out (or authless), the request goes TOKENLESS on purpose —
      // §4.5's anonymous rule answers with the public slice server-side.
      const headers = {};
      if (this.authMode === 'authed' && this._currentUser) {
        const token = await this._idToken();
        if (!token) {
          this._setStatus('Your sign-in has lapsed — sign in again.', 'warn');
          return null;
        }
        headers.authorization = `Bearer ${token}`;
      }
      let res;
      try {
        res = await fetch(`${this.indexUrl}${path}`, { headers, signal });
      } catch (e) {
        if (e && e.name === 'AbortError') return { aborted: true };
        this._setStatus('The index did not answer (network). Try again shortly.', 'warn');
        return null;
      }

      if (res.ok) return res.json();

      let body = null;
      try { body = await res.json(); } catch (e) { /* non-JSON error body; the status still speaks */ }

      switch (body?.error) {
        case 'estate_pending':
          this._setStatus('Your account is awaiting approval. An approver admits new members; nothing more for you to do.', 'warn');
          break;
        case 'estate_revoked':
          this._setStatus('Your access has been revoked.', 'warn');
          break;
        case 'estate_unreachable':
          this._setStatus('The estate directory did not answer, so new admissions cannot be checked right now. Try again shortly.', 'warn');
          break;
        case 'query_too_short':
          break; // the client already gates at minChars; stay quiet mid-keystroke
        case 'unfoldable_query':
          this._setStatus('That title cannot be key-matched (it folds to nothing — wholly non-Latin or punctuation-only titles do this). Browse the owning catalog instead.', 'warn');
          break;
        case 'unauthenticated':
          this._setStatus('The index did not accept the sign-in token. Sign out and back in.', 'warn');
          break;
        default:
          // §1e: never a bare HTTP status alone — say it failed, pass along
          // the server's own words when it gave any.
          this._setStatus(`Search failed${body?.error ? ` (${body.error})` : ''}. Try again shortly.`, 'warn');
      }
      return null;
    }

    _searchPath(q) {
      const params = new URLSearchParams({ q });
      if (this.sourcePreset !== 'all') params.set('source', this.sourcePreset);
      return `/api/search?${params.toString()}`;
    }

    async _runSearch(q) {
      if (this._inflight) this._inflight.abort();
      this._inflight = new AbortController();
      let data = await this._callIndex(this._searchPath(q), this._inflight.signal);
      if (!data || data.aborted) return; // a newer keystroke owns the box now
      if (typeof this.intakeFilter === 'function') {
        const filtered = this.intakeFilter(data, { kind: 'search' });
        if (filtered) data = filtered;
      }
      this._renderSearch(data);
    }

    async _runUniverse(name) {
      if (!this.showUniverses) return;
      if (this.authMode !== 'authed' || !this._currentUser) {
        // /api/universe is members-only server-side (§4.5's carve-out is
        // search-only). Say so as an invitation, before a 401 says it worse.
        this._setStatus(`The universe view spans every shelf, so it needs a sign-in. Sign in to see everything in ${name}.`);
        return;
      }
      if (this._inflight) this._inflight.abort();
      this._inflight = new AbortController();
      this._setStatus(`Everything in ${name}…`);
      let data = await this._callIndex(`/api/universe/${encodeURIComponent(name)}`, this._inflight.signal);
      if (!data || data.aborted) return;
      if (typeof this.intakeFilter === 'function') {
        const filtered = this.intakeFilter(data, { kind: 'universe' });
        if (filtered) data = filtered;
      }
      this._renderUniverse(data);
    }

    _scheduleSearch() {
      clearTimeout(this._debounceTimer);
      const q = this._input.value.trim();
      if (q.length < this.minChars) {
        if (this._inflight) this._inflight.abort();
        this._clearResults();
        this._setStatus('');
        if (this.scanEnabled) this._setScanResolve('');
        return;
      }
      this._debounceTimer = setTimeout(() => void this._runQuery(q), this.debounceMs);
    }

    /**
     * The debounced query dispatch: plain text search, UNLESS `scan` is
     * enabled AND the query parses as a complete ISBN (estate-scan.js's
     * parseIsbnQuery) — the search-bar ISBN upgrade (owner: "why can we not
     * just search an isbn?"), replacing the old separate manual-ISBN box.
     * Shared by the debounce timer and a flushed Enter/submit so typing fast
     * and pressing Enter cannot disagree about which path a query takes.
     */
    async _runQuery(q) {
      if (!this.scanEnabled) return this._runSearch(q);

      // Cheap local pre-check before paying for the scan module's dynamic
      // import: only a COMPLETE 10 or 13 digit run is ever worth it — the
      // load-bearing rule is "never fire Open Library on a partial digit
      // string", so anything else (including every partial digit run
      // mid-type) goes straight to plain text search.
      const digits = q.replace(/[^0-9Xx]/gi, '');
      if (digits.length !== 10 && digits.length !== 13) {
        this._setScanResolve('');
        return this._runSearch(q);
      }

      const scan = await this._loadScanModule().catch(() => null);
      const parsed = scan?.parseIsbnQuery ? scan.parseIsbnQuery(q) : { kind: 'not_isbn' };

      if (parsed.kind === 'isbn13') {
        return this._onIsbnResolved(parsed.isbn13, scan);
      }
      if (parsed.kind === 'invalid') {
        // Clearly ISBN-shaped (13 digits, 978/979 prefix) but the checksum
        // fails: a quiet hint, not silence — and still an ordinary text
        // search, because failing a checksum does not mean the digits were
        // never meant as a search at all.
        this._setScanResolve('That does not look like a valid ISBN — the check digit does not match.', 'bad');
      } else {
        this._setScanResolve('');
      }
      return this._runSearch(q);
    }

    // -- wiring ---------------------------------------------------------------

    _onInput() {
      if (this.authMode === 'authed' && !this._authResolved) return; // neutral boot: no claims yet
      this._scheduleSearch();
    }

    _onKeydown(e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!this._navItems.length) return;
        e.preventDefault();
        const delta = e.key === 'ArrowDown' ? 1 : -1;
        const next = this._activeIndex + delta;
        this._setActive(next < -1 ? this._navItems.length - 1 : next >= this._navItems.length ? -1 : next);
      } else if (e.key === 'Enter') {
        if (this._activeIndex >= 0 && this._navItems[this._activeIndex]) {
          e.preventDefault();
          this._navItems[this._activeIndex].open();
        }
        // Plain Enter falls through to form submit: flush the debounce.
      } else if (e.key === 'Escape') {
        clearTimeout(this._debounceTimer);
        if (this._inflight) this._inflight.abort();
        this._clearResults();
        this._setStatus('');
      }
    }

    _onSubmit(e) {
      e.preventDefault();
      if (this.authMode === 'authed' && !this._authResolved) return;
      clearTimeout(this._debounceTimer);
      const q = this._input.value.trim();
      if (q.length < this.minChars) return;
      void this._runQuery(q);
    }

    async _onSigninClick() {
      if (this.authMode !== 'authed' || !this.authAdapter) return;
      this._signinBtn.disabled = true;
      const r = await this.authAdapter.signIn();
      this._signinBtn.disabled = false;
      if (r.error) this._setStatus(r.error, r.ownerAction ? 'owner' : 'warn');
      else if (r.cancelled) this._setStatus('');
      // ok / redirecting need nothing: watchAuth re-renders, or the page leaves.
    }

    // -- scan (barcode-glyph button, camera-glyph shelf button, search-bar
    //    ISBN — logic lives in estate-scan.js) -----------------------------

    /**
     * Dynamically imported ONLY on first use (either scan button tapped, or a
     * typed query first parses as a candidate ISBN) — same reasoning as the
     * auth adapter above: a site that embeds `scan` but whose visitor never
     * touches it never pays for the module, and a site that never sets
     * `scan` at all never even requests it (the row stays `hidden` and
     * nothing here runs).
     */
    async _loadScanModule() {
      if (!this._scanModulePromise) {
        const path = this.scanModulePath || new URL('estate-scan.js', import.meta.url).href;
        this._scanModulePromise = import(/* @vite-ignore */ path).catch((e) => {
          this._scanModulePromise = null;
          throw e;
        });
      }
      return this._scanModulePromise;
    }

    _setScanResolve(text, tone) {
      this._scanResolveEl.textContent = text || '';
      this._scanResolveEl.dataset.tone = tone || '';
      this._scanResolveEl.hidden = !text;
    }

    /**
     * Icon-only state (owner order): the visible glyph and the disabled flag
     * carry "what's happening" at a glance; the WORDS live in aria-label and
     * title (tooltip), never in the button's text content.
     */
    _setBarcodeBtnState(mode) {
      const label = mode === 'running' ? 'Stop camera' : mode === 'opening' ? 'Opening camera…' : 'Scan a barcode';
      this._scanBtn.innerHTML = mode === 'running' ? ES_ICONS.stop : ES_ICONS.barcode;
      this._scanBtn.setAttribute('aria-label', label);
      this._scanBtn.title = label;
    }

    async _onScanBtnClick() {
      if (this._scanRunning) {
        this._stopScan();
        return;
      }
      this._setStatus('');
      this._setScanResolve('');
      this._scanBtn.disabled = true;
      this._setBarcodeBtnState('opening');
      try {
        const scan = await this._loadScanModule();
        if (!scan.cameraPlausible()) {
          this._setScanResolve(
            'This browser will not give a camera to this page. Type the title or a full ISBN in the search box instead.',
            'bad',
          );
          return;
        }
        scan.preloadBarcodeDetector();
        const stream = await scan.openRearCamera();
        this._scanStream = stream;
        this._scanVideo.srcObject = stream;
        await this._scanVideo.play();
        this._cameraStage.hidden = false;
        this._scanRunning = true;
        this._setBarcodeBtnState('running');

        this._scanStopLoop = scan.startBarcodeScanLoop({
          video: this._scanVideo,
          onScan: ({ code }) => {
            this._stopScan();
            void this._onIsbnResolved(code, scan);
          },
          onError: (err) => {
            this._setScanResolve(err instanceof Error ? err.message : String(err), 'bad');
          },
        });
      } catch (err) {
        const scan = await this._loadScanModule().catch(() => null);
        const CameraErrorCtor = scan?.CameraError;
        let message = err instanceof Error ? err.message : String(err);
        if (CameraErrorCtor && err instanceof CameraErrorCtor) {
          message =
            err.reason === 'denied'
              ? 'Camera permission was refused. Allow it for this site, then try again — or type the title/ISBN in the search box.'
              : err.message;
        }
        this._setScanResolve(message, 'bad');
      } finally {
        this._scanBtn.disabled = false;
      }
    }

    _stopScan() {
      if (this._scanStopLoop) {
        this._scanStopLoop();
        this._scanStopLoop = null;
      }
      if (this._scanStream) {
        this._scanStream.getTracks().forEach((t) => t.stop());
        this._scanStream = null;
      }
      this._scanVideo.srcObject = null;
      this._cameraStage.hidden = true;
      this._scanRunning = false;
      this._setBarcodeBtnState('idle');
    }

    /**
     * Shared tail for both the camera path and the search-bar ISBN upgrade
     * (_runQuery): resolve the ISBN via Open Library, show the "ISBN → Title,
     * Author" line (so a wrong resolve is visible), feed the title into this
     * component's own search, and — signed in — offer to queue it in the
     * library's own Add screen (estate-scan.js's addToCatalog(), which
     * reuses the library app's real, proven barcode-intake endpoint rather
     * than guessing at a catalog write).
     */
    async _onIsbnResolved(isbn, scan) {
      this._setScanResolve(`Looking up ${isbn}…`);
      const resolved = await scan.resolveIsbn(isbn).catch(() => null);
      if (!resolved) {
        this._setScanResolve(`${isbn} — Not identified. Try a different title, or check the number.`, 'bad');
        return;
      }
      const byline = resolved.author ? `${resolved.title}, ${resolved.author}` : resolved.title;
      this._setScanResolve(`${isbn} → ${byline}`);
      this._input.value = resolved.title;
      clearTimeout(this._debounceTimer);
      void this._runSearch(resolved.title);
      this._renderAddAffordance({ isbn13: isbn, title: resolved.title, author: resolved.author }, scan);
    }

    // -- scan (camera-glyph shelf button — logic lives in estate-scan.js) -----

    _setShelfBtnState(mode) {
      const label = mode === 'busy' ? 'Reading the shelf…' : 'Scan a shelf';
      this._shelfBtn.innerHTML = mode === 'busy' ? ES_ICONS.busy : ES_ICONS.photo;
      this._shelfBtn.setAttribute('aria-label', label);
      this._shelfBtn.title = label;
      this._shelfBtn.disabled = mode === 'busy';
    }

    /**
     * Opens the native file input (`accept="image/*" capture="environment"`)
     * — camera directly on mobile, a file picker on desktop; the "photo/
     * upload" flow in one control, no second camera-stream UI needed for a
     * single still shot. Gated to authed + signed-in: vision costs money, so
     * an anonymous tap gets the sign-in prompt, never a free shot at the
     * model (the endpoint enforces this too — this is the honest UI half).
     */
    async _onShelfBtnClick() {
      if (this._shelfBusy) return;
      this._setStatus('');
      const idToken = await this._idToken();
      if (!idToken) {
        this._setStatus('Sign in to scan a shelf — reading a photo costs a little, so it is members-only.', 'warn');
        return;
      }
      this._shelfFileInput.click();
    }

    async _onShelfFileChange() {
      const file = this._shelfFileInput.files?.[0] || null;
      this._shelfFileInput.value = ''; // allow picking the exact same file again
      if (!file) return;

      this._shelfBusy = true;
      this._setShelfBtnState('busy');
      this._setStatus('Reading the shelf…');
      try {
        const scan = await this._loadScanModule();
        const idToken = await this._idToken();
        if (!idToken) {
          this._setStatus('Your sign-in has lapsed — sign in again.', 'warn');
          return;
        }
        // ~1600px long edge before upload (docs/info/estate-scan-adoption.md's
        // sizing) — enough to read a dozen spines without paying for a raw
        // phone-camera resolution the model would downscale anyway.
        const photo = await scan.downscaleImagePhoto(file, 1600);
        const reading = await scan.identifyPhoto(photo, {
          endpoint: `${this.indexUrl}/api/scan/shelf`,
          idToken,
          kind: 'shelf',
        });
        this._renderShelfReading(reading);
      } catch (err) {
        this._setStatus(err instanceof Error ? err.message : String(err), 'warn');
      } finally {
        this._shelfBusy = false;
        this._setShelfBtnState('idle');
      }
    }

    /**
     * Per-title scoped search answers, per estate-scan-adoption.md: the
     * server does ONLY photo → structured titles (no catalog match of its
     * own — this Worker has no per-catalog work index to match against), so
     * matching is THIS component re-running its own scoped search once per
     * identified title, the exact path a normal typed query already uses.
     */
    _renderShelfReading(reading) {
      this._clearResults();

      if (reading.unreadable || reading.books.length === 0) {
        // The explicit "could not identify anything" case — never invents a
        // title, ported refusal discipline (vision.ts's SHELF_SYSTEM: set
        // unreadable rather than guess). A shelf that is readable but simply
        // has no books on it also lands here as an honest empty list.
        this._setStatus('No titles could be read from that photo. Try a closer, better-lit shot of the spines.');
        return;
      }
      this._setStatus('');
      this._input.setAttribute('aria-expanded', 'true');

      this._resultsEl.appendChild(
        this._caveatLine(`${reading.books.length} title${reading.books.length === 1 ? '' : 's'} read off that photo.`),
      );

      const list = document.createElement('ul');
      list.className = 'es-shelf-list';
      list.setAttribute('role', 'presentation');
      this._resultsEl.appendChild(list);

      for (const book of reading.books) {
        const li = document.createElement('li');
        li.className = 'es-shelf-row';

        const head = document.createElement('p');
        head.className = 'es-shelf-title';
        const titleBtn = document.createElement('button');
        titleBtn.type = 'button';
        titleBtn.textContent = book.author ? `${book.text} — ${book.author}` : book.text;
        titleBtn.title = 'Search this title directly';
        titleBtn.addEventListener('click', () => {
          this._input.value = book.text;
          clearTimeout(this._debounceTimer);
          void this._runSearch(book.text);
        });
        head.appendChild(titleBtn);
        li.appendChild(head);

        if (book.confidence === 'low' && book.note) {
          const note = document.createElement('p');
          note.className = 'es-shelf-note';
          note.textContent = `Uncertain read: ${book.note}`;
          li.appendChild(note);
        }

        const answer = document.createElement('p');
        answer.className = 'es-shelf-answer';
        answer.textContent = 'Checking…';
        li.appendChild(answer);

        list.appendChild(li);
        void this._fillShelfAnswer(book.text, answer);
      }
    }

    /** Runs this component's OWN scoped search for one identified title and renders a one-line own-it/not-owned answer beside it. */
    async _fillShelfAnswer(title, el) {
      const data = await this._callIndex(this._searchPath(title));
      if (!data || data.aborted) {
        el.textContent = 'Could not check this title.';
        return;
      }
      const bookHit = data.books?.[0];
      const gameHit = data.games?.[0];
      if (bookHit) {
        const sources = bookHit.entries.map((e) => this._sourceLabel(e.source)).join(', ');
        el.textContent = `In the catalog — ${sources}.`;
      } else if (gameHit) {
        el.textContent = 'In the catalog — board games.';
      } else {
        el.textContent = 'Not found in any catalog.';
      }
    }

    /**
     * The honest "Add to Books" affordance (owner: "if I scan a book and end
     * up buying it I want to be able to add it from the main page"): shows
     * exactly what will be sent BEFORE the click, and only to a signed-in
     * member (adding costs a write on the library's own catalog — the search
     * box's public/authless path never reaches this).
     */
    _renderAddAffordance(candidate, scan) {
      const old = this._scanResolveEl.nextElementSibling;
      if (old && old.classList?.contains('es-scan-add')) old.remove();
      if (this.authMode !== 'authed' || !this._currentUser) return;

      const row = document.createElement('div');
      row.className = 'es-scan-add';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'es-btn';
      btn.textContent = `Add “${candidate.title}” to Books →`;
      btn.title = `Sends ISBN ${candidate.isbn13} to the library's own add queue for review.`;
      btn.addEventListener('click', () => void this._onAddToCatalog(candidate, scan, btn, row));
      row.appendChild(btn);
      this._scanResolveEl.insertAdjacentElement('afterend', row);
    }

    async _onAddToCatalog(candidate, scan, btn, row) {
      btn.disabled = true;
      btn.textContent = 'Adding…';
      try {
        const idToken = await this._idToken();
        if (!idToken) {
          this._setStatus('Your sign-in has lapsed — sign in again.', 'warn');
          return;
        }
        const result = await scan.addToCatalog(candidate.isbn13, { idToken });
        const line = result?.line;
        const msg = document.createElement('span');
        msg.className = 'muted small';
        if (result?.duplicate || line?.state === 'owned') {
          msg.textContent = 'Already on the shelf — no new entry created.';
        } else {
          const jobId = result?.job?.id;
          const a = document.createElement('a');
          a.href = jobId
            ? `https://library.heygabi.ai/add?job=${encodeURIComponent(jobId)}`
            : 'https://library.heygabi.ai/add';
          a.target = '_blank';
          a.rel = 'noopener';
          a.textContent = 'Queued — finish it in the library →';
          msg.appendChild(a);
        }
        row.replaceChildren(msg);
      } catch (err) {
        btn.disabled = false;
        btn.textContent = `Add “${candidate.title}” to Books →`;
        const detail = err instanceof Error ? err.message : String(err);
        this._setScanResolve(`Could not add it: ${detail}`, 'bad');
      }
    }
  }

  if (!customElements.get('estate-search')) {
    customElements.define('estate-search', EstateSearch);
  }
})();
