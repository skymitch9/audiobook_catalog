// @vitest-environment jsdom
// @vitest-environment-options { "url": "https://audiobooks.heygabi.ai/" }
//
// Feature: auth-migration Phase 3a — the gated-write client (worker-writes.js).
//
// The three rules from the module header, each pinned:
//
//   1. ⚠️ NO FALLBACK — every failure exits as `{success:false, error}`. This
//      file cannot prove the *absence* of a Firestore retry (that lives in the
//      callers, and their own tests assert `deleteDoc` was never called), but
//      it does prove this module never answers `success:true` on a failure,
//      which is what a caller would branch on.
//   2. ⚠️ NOBODY SEES A BARE STATUS OR RAW JSON. Every error string is a
//      sentence, and no status number appears in any of them.
//   3. ⚠️ A NETWORK/SERVER failure is worded as an outage, never as a
//      permission problem.
//
// Plus the transport contract: the Worker's own `detail` is preferred, the
// bearer rides an Authorization header, and the dev lane is carried as
// `?lane=dev` so a /dev/ page writes `_dev` collections whichever path it is
// on. (The lane VALUE comes from fb-env's IS_DEV_LANE, whose own detection is
// pinned in fb-env.test.js; the URL override above keeps this file on prod.)

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

let currentUser = null;
let getAuthThrows = false;

vi.mock('firebase/auth', () => ({
  getAuth: () => {
    if (getAuthThrows) throw new Error('no default app');
    return { currentUser };
  },
  onAuthStateChanged: (auth, cb) => { cb(currentUser); return () => {}; },
}));

import {
  NO_SESSION_MESSAGE,
  UNREACHABLE_MESSAGE,
  WORKER_BASE,
  describeWorkerRefusal,
  seg,
  workerUrl,
  workerWrite,
} from '../worker-writes.js';

function stubFetch(impl) {
  const mock = vi.fn(impl);
  vi.stubGlobal('fetch', mock);
  return mock;
}

/** A Response-shaped stub good enough for `.ok`, `.status` and `.json()`. */
function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (body === undefined) throw new Error('no body');
      return body;
    },
  };
}

beforeEach(() => {
  currentUser = { getIdToken: vi.fn(async () => 'live-token') };
  getAuthThrows = false;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('workerUrl and seg', () => {
  it('points at the same Worker origin the shadow reporter uses', () => {
    expect(WORKER_BASE).toBe('https://audiobook-api.heygabi.ai');
    expect(workerUrl('/api/reviews/x')).toBe('https://audiobook-api.heygabi.ai/api/reviews/x');
  });

  it('percent-encodes a path segment, so a slug with a slash cannot escape it', () => {
    expect(seg('a/b')).toBe('a%2Fb');
    expect(seg('two words')).toBe('two%20words');
    expect(seg(null)).toBe('');
  });
});

describe('workerWrite — the happy path', () => {
  it('sends the bearer, the JSON body and the method, and unwraps the answer', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, { success: true, pollId: 'p1' }));

    const result = await workerWrite('POST', '/api/clubs/c1/polls', { question: 'Which?' });

    expect(result).toEqual({ success: true, data: { success: true, pollId: 'p1' } });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://audiobook-api.heygabi.ai/api/clubs/c1/polls');
    expect(init.method).toBe('POST');
    expect(init.headers.Authorization).toBe('Bearer live-token');
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(init.body)).toEqual({ question: 'Which?' });
  });

  it('omits the body and its content type entirely for a bodiless DELETE', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, { success: true }));

    await workerWrite('DELETE', '/api/reviews/abc_sky');

    const [, init] = fetchMock.mock.calls[0];
    expect(init.body).toBeUndefined();
    expect(init.headers['Content-Type']).toBeUndefined();
    expect(init.headers.Authorization).toBe('Bearer live-token');
  });

  it('a 200 with an unparseable body still succeeds, with an empty data bag', async () => {
    stubFetch(async () => jsonResponse(204, undefined));
    await expect(workerWrite('DELETE', '/api/reviews/x')).resolves.toEqual({
      success: true,
      data: {},
    });
  });
});

describe('workerWrite — refused before the request when there is no session', () => {
  it('a signed-out caller is told to sign in, and NOTHING is sent', async () => {
    currentUser = null;
    const fetchMock = stubFetch(async () => jsonResponse(200, { success: true }));

    const result = await workerWrite('DELETE', '/api/reviews/x');

    expect(result.success).toBe(false);
    expect(result.error).toBe(NO_SESSION_MESSAGE);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('a session whose token cannot be produced is the same answer', async () => {
    currentUser = { getIdToken: vi.fn(async () => { throw new Error('token gone'); }) };
    const fetchMock = stubFetch(async () => jsonResponse(200, { success: true }));

    const result = await workerWrite('DELETE', '/api/reviews/x');

    expect(result.success).toBe(false);
    expect(result.error).toBe(NO_SESSION_MESSAGE);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('workerWrite — failures, all worded, none silent', () => {
  it('⚠️ an unreachable Worker is an OUTAGE sentence, not a permission one', async () => {
    stubFetch(async () => { throw new TypeError('Failed to fetch'); });

    const result = await workerWrite('DELETE', '/api/reviews/x');

    expect(result.success).toBe(false);
    expect(result.error).toBe(UNREACHABLE_MESSAGE);
    expect(result.error).toMatch(/connection problem, not a permission one/i);
  });

  it("passes the Worker's own §1e detail through verbatim", async () => {
    const detail =
      'This action needs the "removeAnyReview" capability, which the admin role '
      + '(and above) holds. Your signed-in account does not hold it.';
    stubFetch(async () => jsonResponse(403, { error: 'insufficient_role', detail }));

    const result = await workerWrite('DELETE', '/api/reviews/x');

    expect(result).toEqual({ success: false, error: detail });
  });

  it('never answers success:true on any non-2xx, whatever the body says', async () => {
    for (const status of [400, 401, 403, 404, 409, 429, 500, 502, 503]) {
      stubFetch(async () => jsonResponse(status, { success: true }));
      const result = await workerWrite('DELETE', '/api/reviews/x');
      expect(result.success, `status ${status}`).toBe(false);
      expect(typeof result.error).toBe('string');
    }
  });
});

describe('describeWorkerRefusal — the §1e translator', () => {
  it('prefers the detail the Worker sent', () => {
    expect(describeWorkerRefusal(403, { detail: '  Ask the owner.  ' })).toBe('Ask the owner.');
  });

  it('⚠️ no sentence contains the status number, on any branch', () => {
    for (const status of [400, 401, 403, 404, 409, 429, 500, 502, 503, 418]) {
      const sentence = describeWorkerRefusal(status, null);
      expect(sentence, `status ${status}`).not.toMatch(/\d{3}/);
      expect(sentence.length, `status ${status}`).toBeGreaterThan(30);
      expect(sentence, `status ${status}`).toMatch(/[.!]$/);
    }
  });

  it('⚠️ tells the four causes apart: not signed in / no role / outage / not enabled', () => {
    expect(describeWorkerRefusal(401, null)).toMatch(/not signed in/i);
    expect(describeWorkerRefusal(403, null)).toMatch(/permission/i);
    expect(describeWorkerRefusal(500, null)).toMatch(/fault on our side, not a permission problem/i);
    expect(describeWorkerRefusal(502, null)).toMatch(/fault on our side/i);
    expect(describeWorkerRefusal(503, null)).toMatch(/not switched on server-side/i);
  });

  it('a lost race and a vanished document read differently', () => {
    expect(describeWorkerRefusal(409, null)).toMatch(/somebody else changed this/i);
    expect(describeWorkerRefusal(404, null)).toMatch(/no longer there/i);
  });

  it('an unrecognised status still gets a sentence, never a blank', () => {
    expect(describeWorkerRefusal(418, null)).toMatch(/did not complete/i);
    expect(describeWorkerRefusal(0, null)).toMatch(/did not complete/i);
  });

  it('a raw JSON body with no `detail` never leaks into the sentence', () => {
    const sentence = describeWorkerRefusal(403, { error: 'insufficient_role', needs: 'manageClub' });
    expect(sentence).not.toMatch(/insufficient_role|manageClub|[{}]/);
  });
});
