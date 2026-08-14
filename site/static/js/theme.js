/**
 * theme.js — the estate theme switcher. Classic script, NOT a module, loaded
 * synchronously in <head> right after estate-theme.css so the persisted
 * theme/mode land on <html> before first paint (no flash of the wrong theme).
 *
 * What it does:
 *   - reads localStorage `hg_theme` ('apple'|'cyberpunk'|'retro') and
 *     `hg_mode` ('auto'|'light'|'dark'); origin-scoped, so each site keeps
 *     its own choice for free;
 *   - stamps <html data-theme="…" data-mode="light|dark"> — data-mode is
 *     always the RESOLVED mode ('auto' is resolved against
 *     prefers-color-scheme and re-resolved live when the OS flips);
 *   - exposes window.estateTheme { get, setTheme, setMode, themes, modes }
 *     and fires 'hg-themechange' on document — this API is how a consumer
 *     site's EXISTING settings cog integrates (docs/info/estate-themes.md);
 *   - wires the standard cog UI if the page carries the #hg-cog markup
 *     (button#hg-cog + div#hg-cog-panel with select#hg-theme-select and
 *     [data-hg-mode] buttons). Pages without the markup get the API only.
 *
 * The per-site DEFAULT is identity (owner, 2026-08-13): a site declares its
 * classic look via <html data-default-theme="…"> — apex + library 'apple',
 * audiobooks 'cyberpunk', games 'retro'. Unset falls back to 'apple'.
 */

(function () {
  'use strict';

  var docEl = document.documentElement;
  var THEMES = ['apple', 'cyberpunk', 'retro', 'classic'];
  var MODES = ['auto', 'light', 'dark'];
  var DEFAULT_THEME = docEl.getAttribute('data-default-theme') || 'apple';
  // SITE-LOCAL ADDITION (identity, owner 2026-08-14: "/dev/ must look like
  // the existing page"): the pre-theme site booted DARK for every first-time
  // visitor regardless of OS preference. data-default-mode="dark" preserves
  // that — an unset hg_mode means dark here, not auto. Picking Auto in the
  // cog still stores 'auto' and follows the OS from then on.
  var DEFAULT_MODE = docEl.getAttribute('data-default-mode') || 'auto';
  var media = window.matchMedia('(prefers-color-scheme: dark)');

  function read(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function write(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private mode etc. — selection just won't persist */ }
  }

  // SITE-LOCAL ADDITION (audiobook catalog): migrate-once from the legacy
  // mode key. ab_theme ('dark'|'light') predates the estate switcher and maps
  // 1:1 onto hg_mode. Read it only while hg_mode is unset; never write
  // ab_theme again — hg_theme/hg_mode own the choice from here on.
  // (docs/info/estate-themes.md §4.4 in catalog-platform.)
  if (read('hg_mode') === null) {
    var legacyMode = read('ab_theme');
    if (legacyMode === 'dark' || legacyMode === 'light') write('hg_mode', legacyMode);
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

  // SITE-LOCAL ADDITION: keep the theme-color meta in step with --et-bg per
  // mode (integration step 5 of the estate guide). Runs after apply() so the
  // stamped attributes are already on <html>; the stylesheet link precedes
  // this script, so the computed token is available.
  function syncThemeColor() {
    try {
      var bg = getComputedStyle(docEl).getPropertyValue('--et-bg').trim();
      if (!bg) return;
      var meta = document.querySelector('meta[name="theme-color"]');
      if (!meta) {
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
    get: function () { return { theme: state.theme, mode: state.mode, resolvedMode: resolvedMode() }; },
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
