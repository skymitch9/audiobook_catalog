// @vitest-environment node
//
// Cross-language drift guard for the TITLE/KEY functions (normalization item
// 1), run against catalog-platform/data/title-key-fixtures.json.
//
// bookIdFromTitle in THIS file is the CANON for that one function — every
// existing Firestore review document id in production was built with this
// exact function (see the docstring on it below, and
// library_catalog/packages/core/src/reviews.ts's own header, which ports it
// verbatim and says so). This test therefore isn't checking whether this
// repo's bookIdFromTitle agrees with someone else's canon — it verifies the
// FIXTURE FILE itself was generated correctly from this function, so the
// other two repos' tests (library_catalog's TS, this repo's own Python
// mirrors) are actually pinned to what production runs, not to a
// hand-computed guess.
//
// No sync step exists in this repo (unlike library_catalog's
// scripts/sync-universes.mjs) — the file is read straight off the sibling
// checkout, same posture as app/core/universes.py: SKIPPED, not failed, when
// catalog-platform is not found next to this repo.
import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { bookIdFromTitle } from '../reviews.js';

const ENV_VAR = 'CATALOG_PLATFORM_DIR';
const CANDIDATES = ['../catalog-platform', '../../catalog-platform', '../../../catalog-platform'];

function findPlatformDir() {
  const fromEnv = process.env[ENV_VAR];
  if (fromEnv) {
    const dir = resolve(fromEnv);
    return existsSync(resolve(dir, 'data', 'title-key-fixtures.json')) ? dir : null;
  }
  for (const rel of CANDIDATES) {
    const dir = resolve(rel);
    if (existsSync(resolve(dir, 'data', 'title-key-fixtures.json'))) return dir;
  }
  return null;
}

const platformDir = findPlatformDir();
const describeOrSkip = platformDir ? describe : describe.skip;

describeOrSkip('title-key fixtures (bookIdFromTitle is canon here)', () => {
  // Read lazily: describe.skip still runs this factory to collect skipped
  // children (vitest 4), so an unconditional read here throws resolve(null,...)
  // when the sibling checkout is absent — exactly the CI condition.
  const fixtures = platformDir
    ? JSON.parse(readFileSync(resolve(platformDir, 'data', 'title-key-fixtures.json'), 'utf8'))
    : null;

  it('has the expected schema version and is not truncated', () => {
    expect(fixtures.schemaVersion).toBe(1);
    expect(fixtures.bookIds.length).toBeGreaterThanOrEqual(10);
  });

  it('every bookIds case reproduces through THIS repo\'s bookIdFromTitle', () => {
    for (const { raw, expect: expected, why } of fixtures.bookIds) {
      expect(bookIdFromTitle(raw), `bookIdFromTitle(${JSON.stringify(raw)}) — ${why}`).toBe(expected);
    }
  });

  it('keeps the leading article — the property library_catalog ports verbatim', () => {
    expect(bookIdFromTitle('The Lake House')).toBe('the-lake-house');
  });
});

describe('title-key fixtures: skip reason is visible when the sibling checkout is absent', () => {
  it('is a no-op assertion so this file always reports at least one test', () => {
    if (!platformDir) {
      // eslint-disable-next-line no-console
      console.warn(`catalog-platform not found (tried ${ENV_VAR} and ${CANDIDATES.join(', ')}); title-key fixtures skipped.`);
    }
    expect(true).toBe(true);
  });
});
