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

import { describe, it, expect, beforeEach, vi } from 'vitest';
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
  signInWithGoogle, signOutGoogle, handleRedirectResult,
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
