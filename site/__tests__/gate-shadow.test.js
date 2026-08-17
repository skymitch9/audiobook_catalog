// @vitest-environment jsdom
// @vitest-environment-options { "url": "https://audiobooks.heygabi.ai/" }
//
// Feature: auth-migration Phase 1 — the shadow reporter (gate-shadow.js).
//
// The contract under test is the IRON RULE: reportGate() is fire-and-forget
// and can NEVER affect the user's action — it returns nothing, throws
// nothing, and swallows every failure (auth SDK broken, token unobtainable,
// fetch missing, network down). And when it CAN report, it sends exactly one
// simple CORS POST to the worker's shadow receiver with the payload shape
// gate-shadow.ts parses: { action, lane, clubId?, token? } — token from the
// live session's CACHED token (no forced refresh), absent otherwise
// (tokenless reports are measurement #2 of the design, not an error).
//
// The URL override matters: fb-env's IS_DEV_LANE must be false so the lane
// reports as 'prod'; the dev-lane path is asserted separately via detectDevLane
// in fb-env.test.js (the lane value here is a straight IS_DEV_LANE read).

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

// --- Controllable Firebase Auth mock (identity.test.js idiom) ---
let authCallback = null;
let currentUser = null;
let getAuthThrows = false;

vi.mock('firebase/auth', () => ({
  getAuth: () => {
    if (getAuthThrows) throw new Error('no default app');
    return { currentUser };
  },
  onAuthStateChanged: (auth, cb) => { authCallback = cb; return () => {}; },
}));

import { reportGate, GATE_SHADOW_URL } from '../gate-shadow.js';

/** Wait for the fire-and-forget chain to settle (a few microtask turns). */
const settle = () => new Promise((r) => setTimeout(r, 0));

function stubFetch() {
  const mock = vi.fn(async () => ({ ok: true, status: 204 }));
  vi.stubGlobal('fetch', mock);
  return mock;
}

function sentPayload(fetchMock, call = 0) {
  return JSON.parse(fetchMock.mock.calls[call][1].body);
}

beforeEach(() => {
  authCallback = null;
  currentUser = null;
  getAuthThrows = false;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('gate-shadow reporter — payload and transport', () => {
  it('POSTs one simple CORS request to the shadow receiver with action, lane and clubId', async () => {
    const fetchMock = stubFetch();
    currentUser = { getIdToken: vi.fn(async () => 'live-token') };

    reportGate('club.setSchedule', { clubId: 'club-42' });
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe(GATE_SHADOW_URL);
    expect(url).toBe('https://audiobook-api.heygabi.ai/api/gate/shadow');
    expect(opts.method).toBe('POST');
    expect(opts.keepalive).toBe(true); // must survive a click-triggered navigation
    expect(opts.headers).toBeUndefined(); // no headers => no CORS preflight
    expect(sentPayload(fetchMock)).toEqual({
      action: 'club.setSchedule',
      lane: 'prod',
      clubId: 'club-42',
      token: 'live-token',
    });
  });

  it('reads the CACHED token — getIdToken is called with no forceRefresh argument', async () => {
    stubFetch();
    const getIdToken = vi.fn(async () => 'tok');
    currentUser = { getIdToken };

    reportGate('review.delete');
    await settle();

    expect(getIdToken).toHaveBeenCalledTimes(1);
    expect(getIdToken.mock.calls[0]).toHaveLength(0);
  });

  it('omits clubId when the action has no club scope', async () => {
    const fetchMock = stubFetch();
    currentUser = { getIdToken: async () => 'tok' };

    reportGate('review.submit');
    await settle();

    expect(sentPayload(fetchMock)).toEqual({ action: 'review.submit', lane: 'prod', token: 'tok' });
  });

  it('reports TOKENLESS when Firebase publishes no user — that IS the measurement', async () => {
    const fetchMock = stubFetch();

    reportGate('club.delete', { clubId: 'c1' });
    // No currentUser: the reporter waits on onAuthStateChanged; publish null
    // (a signed-out or legacy/v1 session — nothing verifiable behind it).
    authCallback(null);
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const payload = sentPayload(fetchMock);
    expect(payload.token).toBeUndefined();
    expect(payload.action).toBe('club.delete');
  });

  it('reports tokenless when getIdToken rejects — an unobtainable token is not an error', async () => {
    const fetchMock = stubFetch();
    currentUser = { getIdToken: vi.fn(async () => { throw new Error('token refresh down'); }) };

    reportGate('poll.create', { clubId: 'c2' });
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(sentPayload(fetchMock).token).toBeUndefined();
  });

  it('still reports (tokenless) when the auth SDK itself throws', async () => {
    const fetchMock = stubFetch();
    getAuthThrows = true;

    reportGate('read.finish', { clubId: 'c3' });
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(sentPayload(fetchMock).token).toBeUndefined();
  });
});

describe('gate-shadow reporter — the iron rule (user-action immunity)', () => {
  it('returns nothing, synchronously — there is no promise for a caller to await or branch on', () => {
    stubFetch();
    currentUser = { getIdToken: async () => 'tok' };
    expect(reportGate('review.delete')).toBeUndefined();
  });

  it('never throws when fetch rejects (network down / CORS / CSP refusal)', async () => {
    const failing = vi.fn(async () => { throw new TypeError('Failed to fetch'); });
    vi.stubGlobal('fetch', failing);
    currentUser = { getIdToken: async () => 'tok' };

    expect(() => reportGate('review.delete')).not.toThrow();
    await settle();
    expect(failing).toHaveBeenCalledTimes(1); // it tried — and the failure died here
  });

  it('never throws even when fetch does not exist at all', async () => {
    vi.stubGlobal('fetch', undefined);
    currentUser = { getIdToken: async () => 'tok' };

    expect(() => reportGate('review.delete')).not.toThrow();
    await settle();
  });

  it('never throws when fetch throws synchronously', async () => {
    vi.stubGlobal('fetch', () => { throw new Error('blocked'); });
    currentUser = { getIdToken: async () => 'tok' };

    expect(() => reportGate('club.setWebhook', { clubId: 'c' })).not.toThrow();
    await settle();
  });
});
