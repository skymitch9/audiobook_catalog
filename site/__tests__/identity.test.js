// @vitest-environment jsdom
// @vitest-environment-options { "url": "https://audiobooks.heygabi.ai/" }
//
// Feature: estate identity (v2, 2026-08-14) — live Firebase sessions.
//
// The v1 model (capture identity → sign out immediately → localStorage
// session, with a passphrase fallback) retired when the site was brought in
// line with estate auth. These tests pin the v2 contract:
//   - TRUTH is the Firebase Auth session; the ab_identity_* keys are a
//     synchronous MIRROR written only from auth state (marker ab_identity_live)
//   - a mirror row WITHOUT the marker is a legacy v1 capture: never wiped by
//     the auth listener, surfaced as session.legacy for the one-time upgrade
//   - sign-in KEEPS the session (no signOut after popup success)
//   - the passphrase paths (register/login/reset) no longer exist
//
// ⚠️ The URL override above matters twice: signInWithGoogle uses the popup
// path only off-localhost, and fb-env's IS_DEV_LANE must be false so col()
// resolves unsuffixed names in the profile-write assertions.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as fc from 'fast-check';

// --- In-memory Firestore mock ---
let mockStore = {};

vi.mock('firebase/firestore', () => {
  return {
    doc: (db, collection, id) => ({ _path: `${collection}/${id}` }),
    getDoc: async (ref) => {
      const data = mockStore[ref._path];
      return {
        exists: () => !!data,
        data: () => data ? { ...data } : undefined,
      };
    },
    setDoc: async (ref, data) => {
      mockStore[ref._path] = { ...(mockStore[ref._path] || {}), ...data };
    },
    getFirestore: () => ({}),
    serverTimestamp: () => ({ _type: 'serverTimestamp' }),
  };
});

// --- Controllable Firebase Auth mock ---
// authCallback is the listener identity.js attaches; driving it simulates
// Firebase publishing auth state. signInWithPopupMock is per-test.
let authCallback = null;
const signOutSpy = vi.fn(async () => {});
const signInWithPopupMock = vi.fn();
const signInWithRedirectMock = vi.fn(async () => {});

vi.mock('firebase/auth', () => {
  return {
    getAuth: () => ({ currentUser: null }),
    onAuthStateChanged: (auth, cb) => { authCallback = cb; return () => {}; },
    signInWithPopup: (...args) => signInWithPopupMock(...args),
    signInWithRedirect: (...args) => signInWithRedirectMock(...args),
    getRedirectResult: async () => null,
    GoogleAuthProvider: class GoogleAuthProvider {},
    signOut: (...args) => signOutSpy(...args),
  };
});

import {
  validateDisplayName, getSession, logout, isAdmin, slugifyName,
  signInWithGoogle, signOutGoogle, handleRedirectResult, renderIdentityBar,
  getEstateStatus, isEstateApproved, renderDevSiteLink,
} from '../identity.js';

const fakeApp = {};

function setLegacyMirror(name, method = 'google', email = '') {
  localStorage.setItem('ab_identity_name', name);
  localStorage.setItem('ab_identity_session', 'active');
  localStorage.setItem('ab_identity_method', method);
  if (email) localStorage.setItem('ab_identity_email', email);
  // no ab_identity_live marker — that is what makes it legacy
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  mockStore = {};
  signOutSpy.mockClear();
  signInWithPopupMock.mockReset();
  signInWithRedirectMock.mockClear();
});

describe('Input validation', () => {
  it('validateDisplayName returns true iff string length is between 2 and 20 inclusive', () => {
    fc.assert(
      fc.property(fc.string(), (s) => {
        const result = validateDisplayName(s);
        const expected = s.length >= 2 && s.length <= 20;
        expect(result).toBe(expected);
      }),
      { numRuns: 100 }
    );
  });

  it('validateDisplayName rejects non-string inputs', () => {
    const nonStrings = [null, undefined, 42, true, {}, []];
    for (const val of nonStrings) {
      expect(validateDisplayName(val)).toBe(false);
    }
  });

  it('slugifyName lowercases — the profile/review doc-id convention', () => {
    expect(slugifyName('Skylar')).toBe('skylar');
    expect(slugifyName('!Sky')).toBe('!sky');
  });
});

describe('Session mirror round-trip', () => {
  it('getSession() returns the display name after a mirror row is present', () => {
    fc.assert(
      fc.property(
        fc.string({ minLength: 2, maxLength: 20 }),
        (displayName) => {
          localStorage.setItem('ab_identity_name', displayName);
          localStorage.setItem('ab_identity_session', 'active');

          const session = getSession();
          expect(session).not.toBeNull();
          expect(session.displayName).toBe(displayName);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('getSession() returns null after logout()', () => {
    fc.assert(
      fc.property(
        fc.string({ minLength: 2, maxLength: 20 }),
        (displayName) => {
          localStorage.setItem('ab_identity_name', displayName);
          localStorage.setItem('ab_identity_session', 'active');
          localStorage.setItem('ab_identity_live', '1');

          expect(getSession()).not.toBeNull();
          logout();
          expect(getSession()).toBeNull();
          // logout clears the live marker too — no half-cleared mirrors
          expect(localStorage.getItem('ab_identity_live')).toBeNull();
        }
      ),
      { numRuns: 100 }
    );
  });

  it('getSession() returns null when no session keys are set', () => {
    expect(getSession()).toBeNull();
  });

  it('getSession() returns null when session key is missing', () => {
    fc.assert(
      fc.property(
        fc.string({ minLength: 2, maxLength: 20 }),
        (displayName) => {
          localStorage.setItem('ab_identity_name', displayName);
          // ab_identity_session not set
          expect(getSession()).toBeNull();
        }
      ),
      { numRuns: 100 }
    );
  });
});

describe('Legacy detection — v1 captures vs live-backed mirrors', () => {
  it('a mirror without the live marker is a legacy session', () => {
    setLegacyMirror('OldTimer');
    const s = getSession();
    expect(s).not.toBeNull();
    expect(s.legacy).toBe(true);
  });

  it('a mirror with the live marker is not legacy', () => {
    localStorage.setItem('ab_identity_name', 'Fresh');
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_live', '1');
    expect(getSession().legacy).toBe(false);
  });

  it('the dev-lane stub marker also counts as live', () => {
    localStorage.setItem('ab_identity_name', 'Stubbed');
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_live', 'stub');
    expect(getSession().legacy).toBe(false);
  });
});

describe('Auth mirror — session state comes from onAuthStateChanged', () => {
  it('a published user writes the mirror with the live marker', async () => {
    await handleRedirectResult(fakeApp); // attaches the listener
    expect(authCallback).toBeTypeOf('function');

    authCallback({ displayName: 'Skylar', email: 'nbaslamking@gmail.com', photoURL: 'https://p/x.png' });

    const s = getSession();
    expect(s).not.toBeNull();
    expect(s.displayName).toBe('Skylar');
    expect(s.email).toBe('nbaslamking@gmail.com');
    expect(s.photoURL).toBe('https://p/x.png');
    expect(s.method).toBe('google');
    expect(s.legacy).toBe(false);
  });

  it('a user with no displayName falls back to email', async () => {
    await handleRedirectResult(fakeApp);
    authCallback({ displayName: '', email: 'plain@example.com', photoURL: '' });
    expect(getSession().displayName).toBe('plain@example.com');
  });

  it('a published null clears a live-backed mirror (signed out for real)', async () => {
    await handleRedirectResult(fakeApp);
    authCallback({ displayName: 'Skylar', email: 'e@x.com', photoURL: '' });
    expect(getSession()).not.toBeNull();

    authCallback(null);
    expect(getSession()).toBeNull();
  });

  it('⚠️ a published null NEVER wipes a legacy v1 capture', async () => {
    // The migration promise: Firebase reporting "no user" says nothing about
    // a v1 capture, which never had a live session behind it. Wiping it here
    // would silently strand a signed-in-looking person.
    setLegacyMirror('OldTimer', 'passphrase');
    await handleRedirectResult(fakeApp);

    authCallback(null);

    const s = getSession();
    expect(s).not.toBeNull();
    expect(s.displayName).toBe('OldTimer');
    expect(s.legacy).toBe(true);
  });
});

describe('signInWithGoogle — the session is KEPT', () => {
  it('popup success mirrors the user, writes the profile, and does NOT sign out', async () => {
    signInWithPopupMock.mockResolvedValue({
      user: { displayName: 'Skylar', email: 'nbaslamking@gmail.com', photoURL: 'https://p/x.png' },
    });

    const result = await signInWithGoogle(fakeApp);

    expect(result.success).toBe(true);
    expect(result.displayName).toBe('Skylar');
    // v1 signed out here on purpose; v2 must not — this is the inversion
    expect(signOutSpy).not.toHaveBeenCalled();

    const s = getSession();
    expect(s.displayName).toBe('Skylar');
    expect(s.legacy).toBe(false);

    // ensureProfile merge-write landed on the v1 doc id (slugified name),
    // so the returning account keeps its Community presence
    expect(mockStore['profiles/skylar']).toMatchObject({
      displayName: 'Skylar',
      photoURL: 'https://p/x.png',
    });
  });

  it('a closed popup is a cancellation, not an error state', async () => {
    signInWithPopupMock.mockRejectedValue({ code: 'auth/popup-closed-by-user' });
    const result = await signInWithGoogle(fakeApp);
    expect(result.success).toBe(false);
    expect(result.error).toBe('Sign-in cancelled.');
    expect(getSession()).toBeNull();
    expect(signInWithRedirectMock).not.toHaveBeenCalled();
  });

  it('popup-unavailable codes fall back to redirect; other errors do not', async () => {
    signInWithPopupMock.mockRejectedValue({ code: 'auth/popup-blocked' });
    const r1 = await signInWithGoogle(fakeApp);
    expect(signInWithRedirectMock).toHaveBeenCalledTimes(1);
    expect(r1.success).toBe(false);

    signInWithRedirectMock.mockClear();
    signInWithPopupMock.mockRejectedValue({ code: 'auth/network-request-failed' });
    const r2 = await signInWithGoogle(fakeApp);
    expect(signInWithRedirectMock).not.toHaveBeenCalled();
    expect(r2.success).toBe(false);
    expect(r2.error).toBe('Sign-in failed. Try again.');
  });
});

describe('signOutGoogle', () => {
  it('ends the Firebase session and clears the mirror', async () => {
    localStorage.setItem('ab_identity_name', 'Skylar');
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_live', '1');

    await signOutGoogle(fakeApp);

    expect(signOutSpy).toHaveBeenCalled();
    expect(getSession()).toBeNull();
  });

  it('also clears a legacy session (absent-session signOut is a no-op)', async () => {
    setLegacyMirror('OldTimer', 'passphrase');
    await signOutGoogle(fakeApp);
    expect(getSession()).toBeNull();
  });

  it('⚠️ sweeps the ENTIRE ab_identity_* family — untagged v1 residue and unknown keys included', async () => {
    // The owner's attended pass, 2026-08-14: after a v2 sign-out, untagged
    // v1-shape keys were present (prod v1 shares this origin's localStorage
    // until promotion) and the UI rendered a signed-in "Skylar" over an empty
    // Firebase session. Sign-out must land in a truly signed-out UI whatever
    // residue any lane's code left behind.
    setLegacyMirror('Skylar', 'google', 'nbaslamking@gmail.com');
    localStorage.setItem('ab_identity_photo', 'https://p/x.png');
    localStorage.setItem('ab_identity_v1_extra', 'residue'); // a key v2 never wrote
    localStorage.setItem('guessGame_Skylar_streak', '7'); // NOT ours — must survive

    await signOutGoogle(fakeApp);

    expect(getSession()).toBeNull();
    for (let i = 0; i < localStorage.length; i++) {
      expect(localStorage.key(i).startsWith('ab_identity')).toBe(false);
    }
    expect(localStorage.getItem('guessGame_Skylar_streak')).toBe('7');
  });
});

describe('Residue robustness — the owner-found failure modes', () => {
  it('sign-in ALWAYS invokes the clean popup, whatever the mirror holds', async () => {
    // Residue may inform the UI; it must never block the flow.
    setLegacyMirror('Skylar', 'google', 'nbaslamking@gmail.com');
    localStorage.setItem('ab_identity_v1_extra', 'residue');
    signInWithPopupMock.mockResolvedValue({
      user: { displayName: 'Skylar', email: 'nbaslamking@gmail.com', photoURL: '' },
    });

    const result = await signInWithGoogle(fakeApp);

    expect(signInWithPopupMock).toHaveBeenCalledTimes(1);
    expect(result.success).toBe(true);
    // The write superseded the whole family: live-tagged, residue gone
    expect(getSession().legacy).toBe(false);
    expect(localStorage.getItem('ab_identity_v1_extra')).toBeNull();
    expect(localStorage.getItem('ab_identity_live')).toBe('1');
  });

  it('the mirror listener also supersedes residue when a live user is published', async () => {
    setLegacyMirror('Skylar', 'google');
    localStorage.setItem('ab_identity_v1_extra', 'residue');
    await handleRedirectResult(fakeApp);

    authCallback({ displayName: 'Skylar', email: 'nbaslamking@gmail.com', photoURL: '' });

    expect(localStorage.getItem('ab_identity_v1_extra')).toBeNull();
    expect(getSession().legacy).toBe(false);
  });

  it('the identity bar renders the legacy upgrade panel from the owner\'s exact state', () => {
    // mirror-without-tag + no live session: must NOT render as a plain
    // signed-in chip with no way forward.
    setLegacyMirror('Skylar', 'google', 'nbaslamking@gmail.com');

    const container = document.createElement('div');
    renderIdentityBar(container, {}, { app: fakeApp });

    expect(container.textContent).toContain('Skylar');
    expect(container.textContent).toContain('Legacy');
    const buttons = Array.from(container.querySelectorAll('button')).map((b) => b.textContent);
    expect(buttons.some((t) => t.includes('Sign in'))).toBe(true); // the Google upgrade
    expect(buttons.some((t) => t.includes('Logout'))).toBe(true);
  });

  it('a live-backed session renders WITHOUT the legacy affordances', () => {
    localStorage.setItem('ab_identity_name', 'Skylar');
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_method', 'google');
    localStorage.setItem('ab_identity_live', '1');

    const container = document.createElement('div');
    renderIdentityBar(container, {}, { app: fakeApp });

    expect(container.textContent).toContain('Google');
    expect(container.textContent).not.toContain('Legacy');
  });
});

describe('isAdmin', () => {
  // PRESENTATION ONLY — see the isAdmin docblock; rules are shape-only (§4a)
  it('accepts the Google account by email, whatever the display name is', () => {
    expect(isAdmin({ displayName: 'Skylar', email: 'nbaslamking@gmail.com' })).toBe(true);
    // Renaming the Google profile must not silently drop admin
    expect(isAdmin({ displayName: 'totally new name', email: 'nbaslamking@gmail.com' })).toBe(true);
  });

  it('is case- and whitespace-insensitive on the email', () => {
    expect(isAdmin({ displayName: 'x', email: '  NBaslamKing@Gmail.com ' })).toBe(true);
  });

  it('still accepts the retired passphrase admin name (legacy sessions)', () => {
    expect(isAdmin({ displayName: '!Sky', email: '' })).toBe(true);
  });

  it('rejects everyone else', () => {
    expect(isAdmin({ displayName: 'Somebody', email: 'someone@example.com' })).toBe(false);
    expect(isAdmin({ displayName: '', email: '' })).toBe(false);
    expect(isAdmin(null)).toBe(false);
  });

  it('does not admit a near-miss name or email', () => {
    expect(isAdmin({ displayName: '!Sky2', email: '' })).toBe(false);
    expect(isAdmin({ displayName: 'sky', email: '' })).toBe(false);
    expect(isAdmin({ displayName: '', email: 'nbaslamking@gmail.com.evil.com' })).toBe(false);
  });

  it('reads the stored session when called with no argument', () => {
    localStorage.setItem('ab_identity_name', 'Skylar');
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_email', 'nbaslamking@gmail.com');
    expect(isAdmin()).toBe(true);
    localStorage.setItem('ab_identity_email', 'nope@example.com');
    localStorage.setItem('ab_identity_name', 'Nope');
    expect(isAdmin()).toBe(false);
  });
});

describe('Estate approval gate — the dev-site link', () => {
  // Testing permission IS estate approval (heygabi.ai/admin) — the modal
  // shows "Dev site →" iff GET /api/estate/me answers status 'approved' for
  // a LIVE Firebase session. Everything else stays silent.
  //
  // The auth mock never has a currentUser, so liveUser() waits on
  // onAuthStateChanged — each getEstateStatus call registers a listener in
  // authCallback, and driving it simulates Firebase publishing the session.
  const ME_URL = 'https://auth.heygabi.ai/api/estate/me';

  function fakeUser(uid, token = 'live-id-token') {
    return { uid, getIdToken: async () => token };
  }

  function stubFetch(answer, ok = true) {
    const mock = vi.fn(async () => ({ ok, json: async () => answer }));
    vi.stubGlobal('fetch', mock);
    return mock;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('sends the ID token as a bearer to /api/estate/me and answers approved', async () => {
    const fetchMock = stubFetch({ status: 'approved', is_approver: false, visibility: ['audiobook'] });

    const p = isEstateApproved(fakeApp);
    authCallback(fakeUser('uid-approved'));

    expect(await p).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe(ME_URL);
    expect(opts.headers.Authorization).toBe('Bearer live-id-token');
  });

  it('pending, revoked and not-in-directory are all NOT approved', async () => {
    for (const status of ['pending', 'revoked', null]) {
      sessionStorage.clear();
      stubFetch({ status, is_approver: false, visibility: [] });
      const p = isEstateApproved(fakeApp);
      authCallback(fakeUser('uid-x'));
      expect(await p).toBe(false);
    }
  });

  it('no live session → null, and the endpoint is never even asked', async () => {
    const fetchMock = stubFetch({ status: 'approved' });
    const p = getEstateStatus(fakeApp);
    authCallback(null); // Firebase publishes "no user"
    expect(await p).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('a failed fetch answers null and is NOT cached — the next open retries', async () => {
    const failing = vi.fn(async () => { throw new TypeError('network down'); });
    vi.stubGlobal('fetch', failing);

    const p1 = getEstateStatus(fakeApp);
    authCallback(fakeUser('uid-flaky'));
    expect(await p1).toBeNull();

    const p2 = getEstateStatus(fakeApp);
    authCallback(fakeUser('uid-flaky'));
    expect(await p2).toBeNull();
    expect(failing).toHaveBeenCalledTimes(2); // no failure was cached

    // A non-OK response is likewise not an answer.
    stubFetch({ error: 'unauthenticated' }, false);
    const p3 = getEstateStatus(fakeApp);
    authCallback(fakeUser('uid-flaky'));
    expect(await p3).toBeNull();
  });

  it('caches per uid in sessionStorage — one fetch per session, not per open', async () => {
    const fetchMock = stubFetch({ status: 'approved', is_approver: false, visibility: ['audiobook'] });

    const p1 = getEstateStatus(fakeApp);
    authCallback(fakeUser('uid-cached'));
    expect((await p1).status).toBe('approved');

    const p2 = getEstateStatus(fakeApp);
    authCallback(fakeUser('uid-cached'));
    expect((await p2).status).toBe('approved');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem('ab_identity_estate_me_uid-cached')).toContain('approved');
  });

  it('logout() drops the cache alongside the mirror sweep', async () => {
    const fetchMock = stubFetch({ status: 'approved', is_approver: false, visibility: ['audiobook'] });
    const p1 = getEstateStatus(fakeApp);
    authCallback(fakeUser('uid-out'));
    await p1;
    expect(sessionStorage.getItem('ab_identity_estate_me_uid-out')).not.toBeNull();

    logout();

    expect(sessionStorage.getItem('ab_identity_estate_me_uid-out')).toBeNull();
    const p2 = getEstateStatus(fakeApp);
    authCallback(fakeUser('uid-out'));
    await p2;
    expect(fetchMock).toHaveBeenCalledTimes(2); // refetched after sign-out
  });

  it('renderDevSiteLink renders a quiet /dev/ link for the approved only', async () => {
    stubFetch({ status: 'approved', is_approver: false, visibility: ['audiobook'] });
    const slot = document.createElement('div');
    renderDevSiteLink(slot, fakeApp);
    authCallback(fakeUser('uid-approved-dom'));
    await new Promise((r) => setTimeout(r, 0));

    const a = slot.querySelector('a');
    expect(a).not.toBeNull();
    expect(a.getAttribute('href')).toBe('/dev/');
    expect(a.textContent).toBe('Dev site →');
  });

  it('renderDevSiteLink leaves the slot empty for pending — silently', async () => {
    stubFetch({ status: 'pending', is_approver: false, visibility: ['audiobook'] });
    const slot = document.createElement('div');
    renderDevSiteLink(slot, fakeApp);
    authCallback(fakeUser('uid-pending-dom'));
    await new Promise((r) => setTimeout(r, 0));
    expect(slot.innerHTML).toBe('');
  });
});

describe('Retired passphrase surface', () => {
  it('register/login/adminResetPassword/validatePassphrase are gone from the module', async () => {
    const mod = await import('../identity.js');
    expect(mod.register).toBeUndefined();
    expect(mod.login).toBeUndefined();
    expect(mod.adminResetPassword).toBeUndefined();
    expect(mod.setNewPassphrase).toBeUndefined();
    expect(mod.validatePassphrase).toBeUndefined();
    expect(mod.hashPassphrase).toBeUndefined();
  });
});
