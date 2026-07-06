// @vitest-environment jsdom
// Feature: dev/prod data lane switch (two-lane Pages deploy + local dev)
import { describe, it, expect } from 'vitest';
import { detectDevLane, col, IS_DEV_LANE, COLLECTION_SUFFIX } from '../fb-env.js';

const loc = (hostname, pathname) => ({ hostname, pathname });

describe('detectDevLane', () => {
  it('prod: the Pages site root is not the dev lane', () => {
    expect(detectDevLane(loc('skymitch9.github.io', '/audiobook_catalog/index.html'))).toBe(false);
    expect(detectDevLane(loc('skymitch9.github.io', '/audiobook_catalog/community.html'))).toBe(false);
  });

  it('dev: pages under /dev/ are the dev lane', () => {
    expect(detectDevLane(loc('skymitch9.github.io', '/audiobook_catalog/dev/index.html'))).toBe(true);
    expect(detectDevLane(loc('skymitch9.github.io', '/audiobook_catalog/dev/clubs.html'))).toBe(true);
  });

  it('dev: localhost and 127.0.0.1 are the dev lane regardless of path', () => {
    expect(detectDevLane(loc('localhost', '/clubs.html'))).toBe(true);
    expect(detectDevLane(loc('127.0.0.1', '/index.html'))).toBe(true);
  });

  it('a path merely containing "dev" without slashes stays prod', () => {
    expect(detectDevLane(loc('skymitch9.github.io', '/audiobook_catalog/devices.html'))).toBe(false);
  });

  it('handles missing location defensively', () => {
    expect(detectDevLane(null)).toBe(false);
  });
});

describe('module wiring under jsdom (localhost)', () => {
  it('resolves to _dev collections, proving window.location is consulted', () => {
    // vitest's jsdom URL is localhost, which is the dev lane by design
    expect(IS_DEV_LANE).toBe(true);
    expect(COLLECTION_SUFFIX).toBe('_dev');
    expect(col('reviews')).toBe('reviews_dev');
    expect(col('clubs')).toBe('clubs_dev');
  });
});
