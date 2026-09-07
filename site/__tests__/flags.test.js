// @vitest-environment jsdom
// @vitest-environment-options { "url": "https://audiobooks.heygabi.ai/" }
//
// Feature: auth-migration Phase 3a — the per-surface rollout flags (flags.js).
//
// The contract under test is the one that matters at rollout time:
//
//   1. ⚠️ EVERY FLAG SHIPS OFF. A regression here would move three write
//      surfaces onto the Worker for every visitor without anyone deciding to,
//      which is the one thing §5 Phase 3a says must not happen.
//   2. An unknown name is false — a typo fails closed onto the
//      browser-direct path rather than silently enabling something.
//   3. The per-browser override is read ONCE at module load, and only a JSON
//      object of real booleans counts. Anything else leaves the default
//      standing, because a half-understood override would put a browser on a
//      path nobody could name from reading the file.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

import { FLAG_DEFAULTS, FLAG_OVERRIDE_KEY, flagEnabled, parseFlagOverrides } from '../flags.js';

describe('flags — the shipped defaults', () => {
  it('⚠️ every flag is OFF, and that is the whole safety property of Phase 3a', () => {
    const values = Object.values(FLAG_DEFAULTS);
    expect(values.length).toBeGreaterThan(0);
    for (const [name, value] of Object.entries(FLAG_DEFAULTS)) {
      expect(value, `${name} must ship false`).toBe(false);
      expect(flagEnabled(name)).toBe(false);
    }
  });

  it('names the four Phase 3a surfaces and nothing else', () => {
    // ⚠️ AUTH_ROUTES_WARNINGS joined 2026-09-07 with the two content-note
    // routes (site/user-warnings.js). A fifth name appearing here without a
    // surface behind it is a flag nobody can explain from reading flags.js.
    expect(Object.keys(FLAG_DEFAULTS).sort()).toEqual([
      'AUTH_ROUTES_CLUBS',
      'AUTH_ROUTES_CLUB_READS',
      'AUTH_ROUTES_REVIEWS',
      'AUTH_ROUTES_WARNINGS',
    ]);
  });

  it('an unknown flag name is false — a typo fails closed', () => {
    expect(flagEnabled('AUTH_ROUTES_TYPO')).toBe(false);
    expect(flagEnabled('')).toBe(false);
    expect(flagEnabled(undefined)).toBe(false);
  });
});

describe('parseFlagOverrides — strict, and silent about rubbish', () => {
  it('reads real booleans for known keys', () => {
    expect(parseFlagOverrides(JSON.stringify({ AUTH_ROUTES_REVIEWS: true }))).toEqual({
      AUTH_ROUTES_REVIEWS: true,
    });
    expect(parseFlagOverrides(JSON.stringify({ AUTH_ROUTES_CLUBS: false }))).toEqual({
      AUTH_ROUTES_CLUBS: false,
    });
  });

  it('⚠️ ignores a string "true", a 1, and any other non-boolean', () => {
    expect(parseFlagOverrides(JSON.stringify({ AUTH_ROUTES_REVIEWS: 'true' }))).toEqual({});
    expect(parseFlagOverrides(JSON.stringify({ AUTH_ROUTES_REVIEWS: 1 }))).toEqual({});
    expect(parseFlagOverrides(JSON.stringify({ AUTH_ROUTES_REVIEWS: null }))).toEqual({});
  });

  it('ignores unknown keys entirely', () => {
    expect(parseFlagOverrides(JSON.stringify({ NOT_A_FLAG: true }))).toEqual({});
  });

  it('answers {} for malformed, empty, array and non-string input', () => {
    expect(parseFlagOverrides('{not json')).toEqual({});
    expect(parseFlagOverrides('')).toEqual({});
    expect(parseFlagOverrides(null)).toEqual({});
    expect(parseFlagOverrides(JSON.stringify([1, 2]))).toEqual({});
    expect(parseFlagOverrides(JSON.stringify('AUTH_ROUTES_REVIEWS'))).toEqual({});
  });
});

describe('the per-browser override — read once, at module load', () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
  });
  afterEach(() => {
    // ⚠️ Unstub FIRST: one case replaces localStorage with a throwing stub
    // that has no clear().
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('turns one surface on for this browser without touching the others', async () => {
    localStorage.setItem(FLAG_OVERRIDE_KEY, JSON.stringify({ AUTH_ROUTES_REVIEWS: true }));
    const mod = await import('../flags.js');
    expect(mod.flagEnabled('AUTH_ROUTES_REVIEWS')).toBe(true);
    expect(mod.flagEnabled('AUTH_ROUTES_CLUBS')).toBe(false);
    expect(mod.flagEnabled('AUTH_ROUTES_CLUB_READS')).toBe(false);
    // ⚠️ and the SHIPPED default is untouched by the override
    expect(mod.FLAG_DEFAULTS.AUTH_ROUTES_REVIEWS).toBe(false);
  });

  it('a later localStorage write does NOT take effect without a reload', async () => {
    const mod = await import('../flags.js');
    expect(mod.flagEnabled('AUTH_ROUTES_REVIEWS')).toBe(false);
    localStorage.setItem(FLAG_OVERRIDE_KEY, JSON.stringify({ AUTH_ROUTES_REVIEWS: true }));
    // Same module instance: an action started on one path finishes on it.
    expect(mod.flagEnabled('AUTH_ROUTES_REVIEWS')).toBe(false);
  });

  it('a storage accessor that THROWS leaves the shipped defaults standing', async () => {
    vi.stubGlobal('localStorage', {
      getItem() { throw new Error('storage blocked'); },
    });
    const mod = await import('../flags.js');
    expect(mod.flagEnabled('AUTH_ROUTES_REVIEWS')).toBe(false);
    expect(mod.flagEnabled('AUTH_ROUTES_CLUBS')).toBe(false);
  });
});
