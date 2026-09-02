// Feature: the AUTH SEAM — site/audio-seam.js
//
// AUDIO PLAYER PHASE 2. Design: catalog-platform/docs/info/audio-player-design.md §3
//
// 🔴 WHY THESE MATTER MORE THAN THE REST OF THE PHASE. §3 is the one genuine
// unknown the whole feasibility study named, and its failure mode is a play
// button that does nothing: no controlling service worker ⇒ a bare request ⇒ a
// correct worded 401 from the Worker ⇒ a bare `error` event on the <audio>
// element with NO STATUS the page can read. Nothing throws. Nothing logs. The
// person just presses play and nothing happens, which the estate's refusal
// rule calls worse than showing them a raw status code.
//
// So these pin the two guards that make it impossible to ship silently:
//   1. `swPaths` — the dev lane must not install the PRODUCTION worker at the
//      production scope. That bug is invisible on one lane and changes
//      behaviour for every visitor on the other.
//   2. `probe` — the page asks the question itself, reads the Worker's own
//      worded answer, and NEVER mistakes an outage for a refusal.
import { describe, it, expect, vi } from 'vitest';

import {
  swPaths, probe, fallbackDetail, audioFileUrl, ensureController,
  AUDIO_API_ORIGIN, DB_NAME, STORE_NAME, TOKEN_KEY,
} from '../audio-seam.js';

describe('swPaths — the lane, not the origin', () => {
  // 🔴 THE BUG THIS EXISTS TO PREVENT. `/dev/` is a PATH on
  // audiobooks.heygabi.ai, so a hard-coded '/' scope from a dev page installs
  // the PROMOTED worker and hands it control of the PROMOTED site.
  it('registers the DEV worker at the DEV scope from the dev lane', () => {
    expect(swPaths('/dev/listen')).toEqual({ script: '/dev/audio-sw.js', scope: '/dev/' });
  });

  it('registers the root worker at the root scope from the promoted lane', () => {
    expect(swPaths('/listen')).toEqual({ script: '/audio-sw.js', scope: '/' });
  });

  it('handles the trailing-slash form Cloudflare 308s to', () => {
    expect(swPaths('/dev/listen/')).toEqual({ script: '/dev/listen/audio-sw.js', scope: '/dev/listen/' });
  });

  it('never yields an empty scope for a degenerate path', () => {
    for (const p of ['', '/', undefined, null]) {
      const { script, scope } = swPaths(p);
      expect(scope.startsWith('/')).toBe(true);
      expect(script.endsWith('audio-sw.js')).toBe(true);
    }
  });
});

describe('audioFileUrl', () => {
  it('points at the audio API and encodes the anchor', () => {
    expect(audioFileUrl('b-4754c8e4548e'))
      .toBe('https://audiobook-api.heygabi.ai/api/audio/b-4754c8e4548e/file');
    expect(audioFileUrl('a/b')).toContain('a%2Fb');
  });
});

// ⚠️ The wire format between audio-seam.js and audio-sw.js, which cannot
// import from each other. Duplicated on purpose; tests/test_listen_page.py
// reads BOTH files and fails on drift. These pin the values themselves.
describe('the IndexedDB wire format', () => {
  it('names the store the service worker reads', () => {
    expect(DB_NAME).toBe('audio-auth');
    expect(STORE_NAME).toBe('tokens');
    expect(TOKEN_KEY).toBe('firebase-id-token');
    expect(AUDIO_API_ORIGIN).toBe('https://audiobook-api.heygabi.ai');
  });
});

const res = (status, body) => ({
  status,
  json: async () => body,
});

describe('probe — the MANDATORY HEAD probe (§3.2 item 5)', () => {
  it('accepts a 200 and a 206, which is what the byte route answers', async () => {
    for (const status of [200, 206]) {
      const f = vi.fn().mockResolvedValue(res(status));
      const out = await probe('b-1', 'tok', f);
      expect(out).toEqual({ ok: true, status, detail: '' });
      // ⚠️ ONE request on the happy path. The body follow-up is failure-only.
      expect(f).toHaveBeenCalledTimes(1);
    }
  });

  it('sends the bearer, as a HEAD, to the byte route', async () => {
    const f = vi.fn().mockResolvedValue(res(200));
    await probe('b-1', 'tok', f);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe('https://audiobook-api.heygabi.ai/api/audio/b-1/file');
    expect(init.method).toBe('HEAD');
    expect(init.headers.Authorization).toBe('Bearer tok');
  });

  it('omits the header entirely when there is no token', async () => {
    const f = vi.fn().mockResolvedValue(res(401));
    await probe('b-1', null, f);
    expect(f.mock.calls[0][1].headers).toEqual({});
  });

  // ⚠️ THE WORKER'S OWN SENTENCE WINS. It is the one that names the grant an
  // approver actually toggles; ours is a fallback for when it cannot be read.
  it('reads the Worker\'s worded detail on a refusal and prefers it', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(res(403))
      .mockResolvedValueOnce(res(403, { detail: 'Ask an approver to tick the Ebooks box.' }));
    const out = await probe('b-1', 'tok', f);
    expect(out.ok).toBe(false);
    expect(out.status).toBe(403);
    expect(out.detail).toBe('Ask an approver to tick the Ebooks box.');
  });

  // ⚠️ A RANGED get, not a plain one. range.ts answers an ABSENT Range with a
  // 200 — so a plain GET here would be a 601 MB download to read one sentence.
  it('fetches the refusal body with a one-byte Range, never bare', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(res(403))
      .mockResolvedValueOnce(res(403, { detail: 'x' }));
    await probe('b-1', 'tok', f);
    expect(f).toHaveBeenCalledTimes(2);
    expect(f.mock.calls[1][1].method).toBe('GET');
    expect(f.mock.calls[1][1].headers.Range).toBe('bytes=0-0');
  });

  it('falls back to our own words when the body cannot be read', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(res(403))
      .mockRejectedValueOnce(new Error('cors'));
    const out = await probe('b-1', 'tok', f);
    expect(out.detail).toBe(fallbackDetail(403));
  });

  it('falls back when the body carries no usable detail', async () => {
    for (const body of [null, {}, { detail: '' }, { detail: '   ' }]) {
      const f = vi.fn().mockResolvedValueOnce(res(404)).mockResolvedValueOnce(res(404, body));
      const out = await probe('b-1', 'tok', f);
      expect(out.detail).toBe(fallbackDetail(404));
    }
  });

  // 🔴 AN OUTAGE IS NOT A REFUSAL. Dressing one up as the other sends people
  // asking for access they already hold — the estate's rule, in the one place
  // it is easiest to get wrong.
  it('reports a network failure as status 0 and says it is not about the account', async () => {
    const f = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    const out = await probe('b-1', 'tok', f);
    expect(out.ok).toBe(false);
    expect(out.status).toBe(0);
    expect(out.detail).toMatch(/not a decision about your account/i);
    expect(out.detail).not.toMatch(/do not have|permission denied/i);
  });
});

describe('fallbackDetail — every refusal says three things', () => {
  // The estate's standing rule: what happened, what it needs, how to get it.
  // And ⚠️ NEVER a bare status: a person must not be shown "403".
  it('never renders a bare status code as the whole message', () => {
    for (const s of [0, 401, 403, 404, 429, 503]) {
      const d = fallbackDetail(s);
      expect(d.length).toBeGreaterThan(60);
      expect(d).not.toMatch(/^\d{3}$/);
      expect(d).toMatch(/[a-z]/);
    }
  });

  it('keeps the four causes distinct, because the fixes differ', () => {
    expect(fallbackDetail(401)).toMatch(/sign in/i);
    expect(fallbackDetail(403)).toMatch(/grant|Ebooks box/i);
    expect(fallbackDetail(404)).toMatch(/request/i);
    expect(fallbackDetail(0)).toMatch(/outage|reach/i);
  });

  it('never tells someone with an outage that they lack access', () => {
    expect(fallbackDetail(0)).not.toMatch(/grant|permission/i);
    expect(fallbackDetail(503)).not.toMatch(/your account does not/i);
  });

  it('words an unknown status without showing it alone', () => {
    const d = fallbackDetail(418);
    expect(d).toMatch(/problem on our side/i);
    expect(d).toMatch(/418/); // the code is offered as something to QUOTE, with words around it
  });
});

describe('ensureController — registration is not control', () => {
  const navWith = (overrides) => ({
    serviceWorker: {
      controller: null,
      register: vi.fn().mockResolvedValue({}),
      addEventListener: vi.fn(),
      ...overrides,
    },
  });

  it('answers false when the browser has no service workers at all', async () => {
    expect(await ensureController({}, '/listen', 5)).toBe(false);
  });

  it('answers true immediately when a controller is already in place', async () => {
    const nav = navWith({ controller: {} });
    expect(await ensureController(nav, '/listen', 5)).toBe(true);
  });

  it('registers the lane-correct script and scope', async () => {
    const nav = navWith({ controller: {} });
    await ensureController(nav, '/dev/listen', 5);
    expect(nav.serviceWorker.register).toHaveBeenCalledWith('/dev/audio-sw.js', { scope: '/dev/' });
  });

  // ⚠️ A failed registration must answer FALSE, not throw and not hang. The
  // caller words it ("the browser did not start it") — which is the whole
  // point: the alternative is a play button that silently 401s every range.
  it('answers false when registration is refused rather than throwing', async () => {
    const nav = navWith({ register: vi.fn().mockRejectedValue(new Error('blocked')) });
    expect(await ensureController(nav, '/listen', 5)).toBe(false);
  });

  it('gives up after the timeout when no controller ever arrives', async () => {
    const nav = navWith({});
    expect(await ensureController(nav, '/listen', 5)).toBe(false);
  });
});
