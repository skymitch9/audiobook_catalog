// GENERATED COPY - DO NOT EDIT. Source of truth:
// catalog-platform/sites/heygabi-home/public/assets/theme.js
// Refresh it with: python scripts/sync_estate_theme.py
// A theme fix or a NEW THEME goes THERE and reaches every estate
// site; an edit here dies at this repo and is overwritten.
// tests/test_estate_theme_vendor.py fails while this is stale. 

/**
 * theme.js — the estate theme switcher. Classic script, NOT a module, loaded
 * synchronously in <head> right after estate-theme.css so the persisted
 * theme/mode land on <html> before first paint (no flash of the wrong theme).
 *
 * ⚠️ Theme choice is SITE-WIDE — one look per site, owner clarification
 * 2026-08-14: "Each site should have unified theme choice — by per page I
 * meant per site." A per-page override map (`hg_theme_page`) was built and
 * reverted the same day; this script deletes that stale key on boot so nobody
 * stays stuck on a forgotten page override. Do not reintroduce per-page
 * resolution from git history — it was a misread brief, not lost work.
 *
 * ⚠️ THE THEME LIST LIVES HERE AND NOWHERE ELSE (owner order 2026-08-17:
 * "Add the pink theme as an option for every site, when a theme is added all
 * sites get it some may just default right away"). THEMES below is the ONE
 * registry, LABELS the ONE set of human names, and `wireCog` BUILDS the
 * <select>'s options from them — a page's markup may not carry its own
 * <option> list. That was the actual drift mechanism: `hearts` shipped
 * 2026-08-16 and the apex, games and audiobook cogs each kept offering four
 * themes, because each had written the four names down. A consumer that
 * cannot help hardcoding (a React cog) reads `window.estateTheme.themes`
 * and `.label()` at runtime instead. Adding theme #6 must be one edit to
 * this file plus the CSS beside it.
 *
 * What it does:
 *   - reads localStorage `hg_theme` ('classic'|'apple'|'cyberpunk'|'retro'
 *     |'hearts') and `hg_mode` ('auto'|'light'|'dark'); origin-scoped, so
 *     each site keeps its own choice for free;
 *   - migrates the pre-estate mode keys once (`ab_theme`, `bgc-theme`) —
 *     see LEGACY_MODE_KEYS;
 *   - stamps <html data-theme="…" data-mode="light|dark"> — data-mode is
 *     always the RESOLVED mode ('auto' is resolved against
 *     prefers-color-scheme and re-resolved live when the OS flips);
 *   - keeps <meta name="theme-color"> in step with the active --et-bg
 *     (integration step 5 of docs/info/estate-themes.md — the browser chrome
 *     on a phone is a visible band of colour touching the design);
 *   - exposes window.estateTheme { get, setTheme, setMode, themes, modes,
 *     labels, label } and fires 'hg-themechange' on document — this API is
 *     how a consumer site's EXISTING settings cog integrates;
 *   - wires the standard cog UI if the page carries the #hg-cog markup
 *     (button#hg-cog + div#hg-cog-panel with select#hg-theme-select and
 *     [data-hg-mode] buttons). Pages without the markup get the API only.
 *
 * The per-site DEFAULT is identity (owner, 2026-08-13): a site declares its
 * classic look via <html data-default-theme="…"> — apex 'classic', /admin +
 * library 'apple', audiobooks 'cyberpunk', games 'retro', padhard (the
 * library's second instance) 'hearts'. Unset falls back to 'apple'.
 * `<html data-default-mode="dark">` does the same job for MODE, for a site
 * whose identity is that a first visit boots dark regardless of the OS (the
 * audiobook catalog); unset means 'auto'.
 *
 * ⚠️ `data-default-theme` is deliberately NOT validated against THEMES: a
 * consumer may declare a default this copy of the switcher has not heard of
 * yet (vendored copies drift by days), and stamping the name it asked for
 * degrades to an unstyled-but-honest page rather than silently wearing
 * apple. A STORED choice is validated, because that one came from this UI.
 */

(function () {
  'use strict';

  var docEl = document.documentElement;

  // ⚠️ THE registry. A new theme is added HERE (plus its token block in
  // estate-theme.css) and reaches every cog on the estate — the apex pages
  // build their <select> from it below, and React consumers read
  // window.estateTheme.themes. Nothing else may keep a copy of this list.
  var THEMES = ['classic', 'apple', 'cyberpunk', 'retro', 'hearts'];

  // Human names, beside the ids they name, for the same reason: a cog that
  // wrote its own labels would still need editing for theme #6. Unknown ids
  // degrade to a capitalised id rather than showing nothing — an older
  // vendored copy meeting a newer name should look plain, not broken.
  var LABELS = {
    classic: 'Classic',
    apple: 'Apple',
    cyberpunk: 'Cyberpunk',
    retro: 'Retro',
    hearts: 'Hearts',
  };
  function labelFor(id) {
    if (LABELS[id]) return LABELS[id];
    return id ? String(id).charAt(0).toUpperCase() + String(id).slice(1) : '';
  }

  var MODES = ['auto', 'light', 'dark'];
  var DEFAULT_THEME = docEl.getAttribute('data-default-theme') || 'apple';
  // Identity for MODE, the same shape as data-default-theme: the audiobook
  // catalog booted DARK for every first-time visitor long before the estate
  // switcher existed (owner 2026-08-14: "/dev/ must look like the existing
  // page"), so an unset hg_mode means dark THERE and 'auto' everywhere else.
  // Picking Auto in a cog still stores 'auto' and follows the OS from then on.
  // Unlike data-default-theme, this one IS validated: MODES is a closed set
  // of three that will never grow, so an unknown value is a typo, not a
  // newer name this copy has not heard of yet.
  var DEFAULT_MODE = docEl.getAttribute('data-default-mode');
  if (MODES.indexOf(DEFAULT_MODE) < 0) DEFAULT_MODE = 'auto';
  var media = window.matchMedia('(prefers-color-scheme: dark)');

  // Pre-estate mode keys, migrated once. localStorage is ORIGIN-SCOPED, so
  // each of these can only exist on the site that wrote it — running the
  // whole table on every site is inert everywhere but there, and that is why
  // it lives in canonical instead of as a per-site fork of this file. Read
  // the old key only while hg_mode is unset; never write the old key again.
  var LEGACY_MODE_KEYS = [
    { key: 'ab_theme', map: { dark: 'dark', light: 'light' } },
    { key: 'bgc-theme', map: { dark: 'dark', light: 'light', system: 'auto' } },
  ];

  function read(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function write(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private mode etc. — selection just won't persist */ }
  }

  // Retire the reverted per-page override map (built + reverted 2026-08-14).
  // Safe to drop this line once it has plausibly run everywhere it ever wrote.
  try { localStorage.removeItem('hg_theme_page'); } catch (e) { /* same guard as write() */ }

  if (read('hg_mode') === null) {
    for (var L = 0; L < LEGACY_MODE_KEYS.length; L++) {
      var legacy = read(LEGACY_MODE_KEYS[L].key);
      var mapped = legacy === null ? null : LEGACY_MODE_KEYS[L].map[legacy];
      if (mapped) { write('hg_mode', mapped); break; }
    }
  }

  var storedTheme = read('hg_theme');
  var storedMode = read('hg_mode');
  var state = {
    theme: THEMES.indexOf(storedTheme) >= 0 ? storedTheme : DEFAULT_THEME,
    mode: MODES.indexOf(storedMode) >= 0 ? storedMode : DEFAULT_MODE,
  };

  function resolvedMode() {
    return state.mode === 'auto' ? (media.matches ? 'dark' : 'light') : state.mode;
  }

  function apply() {
    docEl.setAttribute('data-theme', state.theme);
    docEl.setAttribute('data-mode', resolvedMode());
    try {
      document.dispatchEvent(new CustomEvent('hg-themechange', {
        detail: { theme: state.theme, mode: state.mode, resolvedMode: resolvedMode() },
      }));
    } catch (e) { /* CustomEvent should exist everywhere we run; stay quiet if not */ }
  }

  // OS mode flips follow live while the person has chosen 'auto'.
  if (media.addEventListener) {
    media.addEventListener('change', function () { if (state.mode === 'auto') apply(); });
  }

  apply();

  // ---- <meta name="theme-color"> follows --et-bg ----------------------------
  // Integration step 5 of docs/info/estate-themes.md, done ONCE here rather
  // than as an inline script per site (games carried its own copy until
  // 2026-08-17). Runs after apply() so the stamped attributes are already on
  // <html>; every consumer links estate-theme.css BEFORE this script, so the
  // computed token is available synchronously and pre-paint.
  function syncThemeColor() {
    try {
      var bg = getComputedStyle(docEl).getPropertyValue('--et-bg').trim();
      if (!bg) return;
      var meta = document.querySelector('meta[name="theme-color"]');
      if (!meta) {
        if (!document.head) return; // stamped before <head> exists: nothing to do
        meta = document.createElement('meta');
        meta.setAttribute('name', 'theme-color');
        document.head.appendChild(meta);
      }
      meta.setAttribute('content', bg);
    } catch (e) { /* decorative — never let it break theming */ }
  }
  document.addEventListener('hg-themechange', syncThemeColor);
  syncThemeColor();

  window.estateTheme = {
    themes: THEMES.slice(),
    modes: MODES.slice(),
    /** Human names, for a consumer cog that renders its own control. Read
     *  these instead of writing the names down — that is the whole point. */
    labels: (function () {
      var out = {};
      for (var i = 0; i < THEMES.length; i++) out[THEMES[i]] = labelFor(THEMES[i]);
      return out;
    })(),
    label: labelFor,
    get: function () { return { theme: state.theme, mode: state.mode, resolvedMode: resolvedMode() }; },
    /** Theme for the whole site — one look per site (owner, 2026-08-14). */
    setTheme: function (t) {
      if (THEMES.indexOf(t) < 0) return;
      state.theme = t;
      write('hg_theme', t);
      apply();
    },
    setMode: function (m) {
      if (MODES.indexOf(m) < 0) return;
      state.mode = m;
      write('hg_mode', m);
      apply();
    },
  };

  // ---- the cog UI (only when the page carries the markup) ------------------

  function wireCog() {
    var cog = document.getElementById('hg-cog');
    var panel = document.getElementById('hg-cog-panel');
    if (!cog || !panel) return;

    var themeSelect = document.getElementById('hg-theme-select');
    var modeButtons = panel.querySelectorAll('[data-hg-mode]');

    // ⚠️ THE OPTIONS ARE BUILT FROM THEMES, NOT READ FROM THE MARKUP. Any
    // <option> a page happens to carry is replaced — the page owns the cog's
    // PLACE, this file owns its CONTENTS. This is the mechanism that makes
    // "when a theme is added all sites get it" true (owner, 2026-08-17)
    // rather than a promise somebody has to keep by hand.
    if (themeSelect) {
      while (themeSelect.firstChild) themeSelect.removeChild(themeSelect.firstChild);
      for (var t = 0; t < THEMES.length; t++) {
        var opt = document.createElement('option');
        opt.value = THEMES[t];
        opt.textContent = labelFor(THEMES[t]);
        themeSelect.appendChild(opt);
      }
    }

    function sync() {
      if (themeSelect) themeSelect.value = state.theme;
      for (var i = 0; i < modeButtons.length; i++) {
        var b = modeButtons[i];
        b.setAttribute('aria-pressed', b.getAttribute('data-hg-mode') === state.mode ? 'true' : 'false');
      }
    }

    function setOpen(open) {
      panel.hidden = !open;
      cog.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    cog.addEventListener('click', function () {
      setOpen(panel.hidden);
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !panel.hidden) {
        setOpen(false);
        cog.focus();
      }
    });

    // A tap anywhere else closes the panel — it is a popover, not a page.
    document.addEventListener('pointerdown', function (e) {
      if (panel.hidden) return;
      if (panel.contains(e.target) || cog.contains(e.target)) return;
      setOpen(false);
    });

    if (themeSelect) {
      themeSelect.addEventListener('change', function () {
        window.estateTheme.setTheme(themeSelect.value);
      });
    }
    for (var i = 0; i < modeButtons.length; i++) {
      (function (btn) {
        btn.addEventListener('click', function () {
          window.estateTheme.setMode(btn.getAttribute('data-hg-mode'));
        });
      })(modeButtons[i]);
    }

    document.addEventListener('hg-themechange', sync);
    sync();
    setOpen(false);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireCog);
  } else {
    wireCog();
  }
})();
