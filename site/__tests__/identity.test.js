// @vitest-environment jsdom
// Feature: book-reviews-and-user-identity
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
      mockStore[ref._path] = { ...data };
    },
    serverTimestamp: () => ({ _type: 'serverTimestamp' }),
  };
});

import { validateDisplayName, validatePassphrase, getSession, logout, register, login, renderIdentityBar } from '../identity.js';

// Generators for valid display names (alphanumeric, 2-20 chars) and valid passphrases (4+ chars)
const alphanumChars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
const validDisplayName = fc.array(
  fc.constantFrom(...alphanumChars.split('')),
  { minLength: 2, maxLength: 20 }
).map(arr => arr.join(''));
const validPassphrase = fc.array(
  fc.constantFrom(...(alphanumChars + '!@#$%').split('')),
  { minLength: 4, maxLength: 30 }
).map(arr => arr.join(''));

const fakeDb = {};

describe('Property 2: Identity input validation', () => {
  // **Validates: Requirements 1.3, 1.4**

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

  it('validatePassphrase returns true iff string length is at least 4', () => {
    fc.assert(
      fc.property(fc.string(), (s) => {
        const result = validatePassphrase(s);
        const expected = s.length >= 4;
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

  it('validatePassphrase rejects non-string inputs', () => {
    const nonStrings = [null, undefined, 42, true, {}, []];
    for (const val of nonStrings) {
      expect(validatePassphrase(val)).toBe(false);
    }
  });
});

describe('Property 5: Session persistence round-trip', () => {
  // **Validates: Requirements 3.1, 3.2**

  beforeEach(() => {
    localStorage.removeItem('ab_identity_name');
    localStorage.removeItem('ab_identity_session');
  });

  it('getSession() returns the display name after storing a valid session', () => {
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

          // Verify session exists first
          expect(getSession()).not.toBeNull();

          logout();

          expect(getSession()).toBeNull();
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


// Feature: book-reviews-and-user-identity, Property 1: Registration and login round-trip
describe('Property 1: Registration and login round-trip', () => {
  // **Validates: Requirements 1.1, 1.5, 1.6, 2.1, 2.2**

  beforeEach(() => {
    mockStore = {};
    localStorage.removeItem('ab_identity_name');
    localStorage.removeItem('ab_identity_session');
  });

  it('register then login with same credentials succeeds and getSession() returns the display name', async () => {
    await fc.assert(
      fc.asyncProperty(validDisplayName, validPassphrase, async (name, pass) => {
        // Clear state between iterations
        mockStore = {};
        localStorage.removeItem('ab_identity_name');
        localStorage.removeItem('ab_identity_session');

        const regResult = await register(name, pass, fakeDb);
        expect(regResult.success).toBe(true);

        // Session should be set after registration
        const sessionAfterReg = getSession();
        expect(sessionAfterReg).not.toBeNull();
        expect(sessionAfterReg.displayName).toBe(name);

        // Logout, then login with same credentials
        logout();
        expect(getSession()).toBeNull();

        const loginResult = await login(name, pass, fakeDb);
        expect(loginResult.success).toBe(true);

        const sessionAfterLogin = getSession();
        expect(sessionAfterLogin).not.toBeNull();
        expect(sessionAfterLogin.displayName).toBe(name);
      }),
      { numRuns: 100 }
    );
  });
});

// Feature: book-reviews-and-user-identity, Property 3: Duplicate display name rejection
describe('Property 3: Duplicate display name rejection', () => {
  // **Validates: Requirements 1.2**

  beforeEach(() => {
    mockStore = {};
    localStorage.removeItem('ab_identity_name');
    localStorage.removeItem('ab_identity_session');
  });

  it('registering the same display name twice (case-insensitive) fails the second time', async () => {
    await fc.assert(
      fc.asyncProperty(validDisplayName, validPassphrase, validPassphrase, async (name, pass1, pass2) => {
        mockStore = {};
        localStorage.removeItem('ab_identity_name');
        localStorage.removeItem('ab_identity_session');

        const first = await register(name, pass1, fakeDb);
        expect(first.success).toBe(true);

        // Try registering again with same name (same case)
        const second = await register(name, pass2, fakeDb);
        expect(second.success).toBe(false);
        expect(second.error).toBe('That display name is already taken.');
      }),
      { numRuns: 100 }
    );
  });

  it('registering with a case-variant of an existing name fails', async () => {
    await fc.assert(
      fc.asyncProperty(validDisplayName, validPassphrase, validPassphrase, async (name, pass1, pass2) => {
        mockStore = {};
        localStorage.removeItem('ab_identity_name');
        localStorage.removeItem('ab_identity_session');

        const first = await register(name, pass1, fakeDb);
        expect(first.success).toBe(true);

        // Flip case of the name
        const flipped = name.split('').map(c =>
          c === c.toUpperCase() ? c.toLowerCase() : c.toUpperCase()
        ).join('');

        // Only test if flipped is still a valid display name (2-20 chars)
        if (flipped.length >= 2 && flipped.length <= 20) {
          const second = await register(flipped, pass2, fakeDb);
          expect(second.success).toBe(false);
          expect(second.error).toBe('That display name is already taken.');
        }
      }),
      { numRuns: 100 }
    );
  });
});

// Feature: book-reviews-and-user-identity, Property 4: Generic error for invalid credentials
describe('Property 4: Generic error for invalid credentials', () => {
  // **Validates: Requirements 2.3, 2.4**

  beforeEach(() => {
    mockStore = {};
    localStorage.removeItem('ab_identity_name');
    localStorage.removeItem('ab_identity_session');
  });

  it('login with wrong passphrase returns same generic error as non-existent name', async () => {
    await fc.assert(
      fc.asyncProperty(
        validDisplayName,
        validPassphrase,
        validPassphrase,
        validDisplayName,
        async (name, correctPass, wrongPass, nonExistentName) => {
          mockStore = {};
          localStorage.removeItem('ab_identity_name');
          localStorage.removeItem('ab_identity_session');

          // Register a user
          await register(name, correctPass, fakeDb);

          // Ensure wrongPass differs from correctPass
          const actualWrongPass = wrongPass === correctPass ? wrongPass + 'x' : wrongPass;

          // Ensure nonExistentName differs from registered name (case-insensitive)
          const actualNonExistent = nonExistentName.toLowerCase() === name.toLowerCase()
            ? nonExistentName + 'zz'
            : nonExistentName;

          // Login with wrong passphrase
          const wrongPassResult = await login(name, actualWrongPass, fakeDb);
          expect(wrongPassResult.success).toBe(false);

          // Login with non-existent name
          const noUserResult = await login(actualNonExistent, correctPass, fakeDb);
          expect(noUserResult.success).toBe(false);

          // Both should return the same generic error message
          expect(wrongPassResult.error).toBe('Invalid display name or passphrase.');
          expect(noUserResult.error).toBe('Invalid display name or passphrase.');
          expect(wrongPassResult.error).toBe(noUserResult.error);
        }
      ),
      { numRuns: 100 }
    );
  });
});


// Feature: book-reviews-and-user-identity — renderIdentityBar unit tests
// **Validates: Requirements 1.3, 1.4, 3.3, 8.5**
// SKIPPED: these tests target the pre-Google-SSO identity bar UI (inline
// name/passphrase form, "Welcome, X" greeting) and were silently dead until
// the firebase-auth vitest alias was added — the suite could not even load.
// Rewrite them against the current SSO-first UI to re-enable.

