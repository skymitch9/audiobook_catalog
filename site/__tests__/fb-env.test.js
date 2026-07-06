// @vitest-environment jsdom
// Feature: dev/prod data lane switch (two-lane Pages deploy)
import { describe, it, expect, beforeEach, vi } from 'vitest';

async function importFbEnv(pathname) {
  vi.resetModules();
  window.history.replaceState(null, '', pathname);
  return await import('../fb-env.js');
}

describe('fb-env data lane switch', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('prod lane (site root) uses unsuffixed collection names', async () => {
    const env = await importFbEnv('/audiobook_catalog/index.html');
    expect(env.IS_DEV_LANE).toBe(false);
    expect(env.COLLECTION_SUFFIX).toBe('');
    expect(env.col('reviews')).toBe('reviews');
    expect(env.col('users')).toBe('users');
  });

  it('dev lane (/dev/ path) suffixes collection names with _dev', async () => {
    const env = await importFbEnv('/audiobook_catalog/dev/index.html');
    expect(env.IS_DEV_LANE).toBe(true);
    expect(env.COLLECTION_SUFFIX).toBe('_dev');
    expect(env.col('reviews')).toBe('reviews_dev');
    expect(env.col('leaderboard')).toBe('leaderboard_dev');
  });

  it('dev lane detection works on nested pages', async () => {
    const env = await importFbEnv('/audiobook_catalog/dev/community.html');
    expect(env.col('profiles')).toBe('profiles_dev');
  });

  it('a path merely containing "dev" without slashes stays prod', async () => {
    const env = await importFbEnv('/audiobook_catalog/devices.html');
    expect(env.IS_DEV_LANE).toBe(false);
    expect(env.col('reviews')).toBe('reviews');
  });
});
