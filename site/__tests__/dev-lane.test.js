/**
 * dev-lane.test.js — the curtain's decisions.
 *
 * ⚠️ WHAT IS BEING DEFENDED HERE IS A DISTINCTION, not a feature: the
 * difference between "the estate said no" and "the estate did not say". Those
 * two collapse into one the moment somebody writes `if (!answer.dev_access)`,
 * and the result is a curtain that closes on an outage — an outage wearing a
 * permission refusal's clothes, which sends the household's own devops asking
 * for access they already hold.
 *
 * ⚠️ And the OTHER direction, tested just as hard: the prod lane must reach no
 * verdict, take no network call and add no failure mode. `isDevLane` is what
 * makes that true, and a host check dressed as a path check would pass every
 * other test in this file.
 */

import { describe, expect, it } from 'vitest';
import { DEV_CURTAIN, devAccessFrom, isDevLane, verdictFor } from '../dev-lane.js';

describe('isDevLane — ⚠️ a PATH, never a host', () => {
  it('recognises the dev lane', () => {
    expect(isDevLane('/dev/')).toBe(true);
    expect(isDevLane('/dev/ebooks')).toBe(true);
    expect(isDevLane('/dev/read')).toBe(true);
    expect(isDevLane('/dev')).toBe(true);
  });

  it('leaves every promoted path alone', () => {
    expect(isDevLane('/')).toBe(false);
    expect(isDevLane('/ebooks')).toBe(false);
    expect(isDevLane('/read')).toBe(false);
    expect(isDevLane('')).toBe(false);
  });

  it('is not fooled by a path that merely BEGINS with the letters', () => {
    // `/developer-notes` is not the dev lane, and a startsWith('/dev') check
    // would say it was.
    expect(isDevLane('/development')).toBe(false);
    expect(isDevLane('/devops')).toBe(false);
    expect(isDevLane('/x/dev/ebooks')).toBe(false);
  });
});

describe('devAccessFrom — ⚠️ "could not tell" is null, NEVER false', () => {
  it('reads the boolean the estate sends', () => {
    expect(devAccessFrom({ dev_access: true })).toBe(true);
    expect(devAccessFrom({ dev_access: false })).toBe(false);
  });

  it('answers null when the estate did not answer at all', () => {
    // getEstateStatus() returns null on a failed fetch, a non-ok status, or no
    // live session. None of those is a refusal.
    expect(devAccessFrom(null)).toBe(null);
    expect(devAccessFrom(undefined)).toBe(null);
  });

  it('answers null when the FIELD is missing', () => {
    // ⚠️ Not hypothetical. identity.js caches /api/estate/me in sessionStorage
    // for ten minutes, so for ten minutes after the estate half deployed there
    // were live cache entries shaped exactly like this. Reading a missing field
    // as `false` would have curtained everybody, including the owner.
    expect(devAccessFrom({ status: 'approved', is_approver: true })).toBe(null);
  });

  it('refuses a truthy non-boolean rather than believing it', () => {
    // A string "false" is truthy; a `1` is not a boolean. Either would be a
    // Worker bug, and guessing at a bug is how a curtain becomes a lottery.
    expect(devAccessFrom({ dev_access: 'true' })).toBe(null);
    expect(devAccessFrom({ dev_access: 1 })).toBe(null);
    expect(devAccessFrom({ dev_access: 0 })).toBe(null);
  });

  it('never re-derives the owner’s rule from is_devops', () => {
    // ⚠️ `devAccessAllows()` in the auth Worker is the ONE implementation of
    // "approved AND (dev_access OR is_devops OR is_approver)". A second copy
    // here would be free to drift the first time the owner changes the rule,
    // so a devops row WITHOUT the effective field is still "we cannot tell".
    expect(devAccessFrom({ is_devops: true, is_approver: true })).toBe(null);
  });
});

describe('verdictFor', () => {
  it('the prod lane reaches no verdict at all', () => {
    expect(verdictFor(false, { dev_access: false })).toBe('prod-lane');
    expect(verdictFor(false, null)).toBe('prod-lane');
  });

  it('dev access opens the dev lane', () => {
    expect(verdictFor(true, { dev_access: true })).toBe('allowed');
  });

  it('an explicit refusal draws the curtain', () => {
    expect(verdictFor(true, { dev_access: false })).toBe('curtain');
  });

  it('⚠️ an unknown answer FAILS OPEN, because this is a curtain and not a lock', () => {
    expect(verdictFor(true, null)).toBe('unknown');
    expect(verdictFor(true, {})).toBe('unknown');
  });
});

describe('the curtain’s words', () => {
  it('say what this is, what it needs, and where the regular site is', () => {
    // ROLES.md §1e: never a bare status, never a dead page. Three facts, and
    // the third is the one that makes the page useful rather than merely
    // polite — somebody who cannot have the dev lane still wants their books.
    expect(DEV_CURTAIN.why).toContain('/dev/');
    expect(DEV_CURTAIN.why).toContain('dev access');
    expect(DEV_CURTAIN.why).toContain('heygabi.ai/admin');
    expect(DEV_CURTAIN.why).toContain('ebooks.heygabi.ai');
    expect(DEV_CURTAIN.href).toBe('https://ebooks.heygabi.ai');
  });

  it('name the devops implication, so nobody is sent to ask for what they hold', () => {
    expect(DEV_CURTAIN.why).toMatch(/devops/);
  });

  it('do not call it a lock, and do not blame the reader’s account', () => {
    expect(DEV_CURTAIN.why).toContain('Nothing is wrong with your account');
  });
});
