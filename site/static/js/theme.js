/**
 * theme.js — the estate theme switcher. Classic script, NOT a module, loaded
 * synchronously in <head> right after estate-theme.css so the persisted
 * theme/mode land on <html> before first paint (no flash of the wrong theme).
 *
 * Vendored from catalog-platform (canonical v2, commit 90c9703) with THREE
 * marked SITE-LOCAL ADDITIONS for the audiobook catalog — preserve them on
 * every re-vendor: (1) ab_theme migrate-once, (2) data-default-mode support
 * (identity: first visit boots dark, as prod always has), (3) theme-color
 * meta sync. This site carries NO #hg-cog markup — the appearance controls
 * live in the account modal (account-modal.js and index.html's inline modal)
 * and drive the window.estateTheme API; the cog wiring below simply no-ops.
 *
 * v2 (2026-08-13): themes persist PER PAGE. The owner: "let me set a theme per
 * page and it persist, sometimes i want different looks and feel for all my
 * pages." Resolution order, first hit wins:
 *
 *   1. this page's override — localStorage `hg_theme_page`, a JSON object
 *      keyed by normalised location.pathname (trailing slash and /index.html
 *      stripped, so /admin, /admin/ and /admin/index.html are ONE page);
 *   2. the site default the person chose — localStorage `hg_theme`;
 *   3. the site's identity — <html data-default-theme="…">;
 *   4. 'apple'.
 *
 * setTheme() writes the PAGE override; setSiteTheme() is the "apply to all
 * pages" lever — it writes `hg_theme` and DELETES the whole override map,
 * because "all pages" means what it says (docs/info/estate-themes.md §2a).
 * MODE stays site-wide (`hg_mode`) on purpose: per-page dark/light is chaos.
 *
 * ⚠️ SPA note: "the page" is location.pathname at the moment of boot or of a
 * setTheme() call. Client-side navigation does not re-resolve.
 */

(function () {
  'use strict';

  var docEl = document.documentElement;
  var THEMES = ['classic', 'apple', 'cyberpunk', 'retro'];
  var MODES = ['auto', 'light', 'dark'];
  var DEFAULT_THEME = docEl.getAttribute('data-default-theme') || 'apple';
  // SITE-LOCAL ADDITION (identity, owner 2026-08-14: "/dev/ must look like
  // the existing page"): the pre-theme site booted DARK for every first-time
  // visitor regardless of OS preference. data-default-mode="dark" preserves
  // that — an unset hg_mode means dark here, not auto. Picking Auto in the
  // modal still stores 'auto' and follows the OS from then on.
  var DEFAULT_MODE = docEl.getAttribute('data-default-mode') || 'auto';
  var PAGE_MAP_KEY = 'hg_theme_page';
  var media = window.matchMedia('(prefers-color-scheme: dark)');

  function read(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function write(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private mode etc. — selection just won't persist */ }
  }
  function remove(key) {
    try { localStorage.removeItem(key); } catch (e) { /* same */ }
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

  function validTheme(t) {
    return THEMES.indexOf(t) >= 0 ? t : null;
  }

  // One page, one key: strip /index.html and any trailing slash so a page
  // reached three ways cannot accumulate three overrides.
  function pageKey() {
    var p = location.pathname || '/';
    p = p.replace(/\/index\.html?$/i, '/');
    if (p.length > 1) p = p.replace(/\/+$/, '');
    return p === '' ? '/' : p;
  }

  // The override map. Corrupt JSON or a non-object reads as "no overrides";
  // unknown theme values are dropped rather than stamped.
  function readOverrides() {
    var raw = read(PAGE_MAP_KEY);
    if (!raw) return {};
    try {
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object' || Object.prototype.toString.call(parsed) !== '[object Object]') return {};
      var clean = {};
      for (var k in parsed) {
        if (Object.prototype.hasOwnProperty.call(parsed, k) && validTheme(parsed[k])) clean[k] = parsed[k];
      }
      return clean;
    } catch (e) { return {}; }
  }

  var overrides = readOverrides();
  var siteTheme = validTheme(read('hg_theme')) || DEFAULT_THEME;
  var storedMode = read('hg_mode');
  var bootOverride = validTheme(overrides[pageKey()]);

  var state = {
    theme: bootOverride || siteTheme,
    scope: bootOverride ? 'page' : 'site',
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
        detail: {
          theme: state.theme,
          mode: state.mode,
          resolvedMode: resolvedMode(),
          scope: state.scope,
          siteTheme: siteTheme,
        },
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
    get: function () {
      return {
        theme: state.theme,
        mode: state.mode,
        resolvedMode: resolvedMode(),
        scope: state.scope,
        siteTheme: siteTheme,
      };
    },
    /** Theme for THIS PAGE — writes the per-path override. */
    setTheme: function (t) {
      if (!validTheme(t)) return;
      state.theme = t;
      state.scope = 'page';
      overrides[pageKey()] = t;
      write(PAGE_MAP_KEY, JSON.stringify(overrides));
      apply();
    },
    /** Theme for ALL pages — writes the site default and clears EVERY page
     *  override, this page's and every other's. "All pages" means all pages;
     *  this is also the only reset lever, on purpose (estate-themes.md §2a). */
    setSiteTheme: function (t) {
      if (!validTheme(t)) return;
      siteTheme = t;
      state.theme = t;
      state.scope = 'site';
      overrides = {};
      remove(PAGE_MAP_KEY);
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
  // This site ships no cog markup (appearance lives in the account modal);
  // kept verbatim from canonical so re-vendors stay a clean diff.

  function wireCog() {
    var cog = document.getElementById('hg-cog');
    var panel = document.getElementById('hg-cog-panel');
    if (!cog || !panel) return;

    var themeSelect = document.getElementById('hg-theme-select');
    var applyAll = document.getElementById('hg-apply-all');
    var scopeNote = document.getElementById('hg-scope-note');
    var modeButtons = panel.querySelectorAll('[data-hg-mode]');

    function sync() {
      if (themeSelect) themeSelect.value = state.theme;
      if (scopeNote) scopeNote.hidden = state.scope !== 'page';
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
    if (applyAll) {
      applyAll.addEventListener('click', function () {
        window.estateTheme.setSiteTheme(state.theme);
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
