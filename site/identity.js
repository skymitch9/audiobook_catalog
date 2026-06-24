// identity.js — Google SSO + passphrase fallback identity system
// ES module, browser-native (no build step)

import { doc, getDoc, setDoc, serverTimestamp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { getAuth, signInWithPopup, GoogleAuthProvider, onAuthStateChanged, signOut } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js';

// ==================== Session Management ====================

/**
 * Get the current session. Checks Firebase Auth first, then localStorage fallback.
 * @returns {{ displayName: string, photoURL?: string, uid?: string, method: 'google'|'passphrase' } | null}
 */
export function getSession() {
  // Check localStorage for active session (works for both methods)
  const name = localStorage.getItem('ab_identity_name');
  const session = localStorage.getItem('ab_identity_session');
  const method = localStorage.getItem('ab_identity_method') || 'passphrase';
  const photoURL = localStorage.getItem('ab_identity_photo') || '';

  if (!name || session !== 'active') {
    return null;
  }

  return { displayName: name, photoURL, method };
}

/**
 * Clear the current session.
 */
export function logout() {
  localStorage.removeItem('ab_identity_name');
  localStorage.removeItem('ab_identity_session');
  localStorage.removeItem('ab_identity_method');
  localStorage.removeItem('ab_identity_photo');
}

// ==================== Google SSO ====================

/**
 * Sign in with Google via Firebase Auth popup.
 * @param {import('firebase/app').FirebaseApp} app
 * @returns {Promise<{ success: boolean, displayName?: string, error?: string }>}
 */
export async function signInWithGoogle(app) {
  try {
    const auth = getAuth(app);
    const provider = new GoogleAuthProvider();
    const result = await signInWithPopup(auth, provider);
    const user = result.user;

    localStorage.setItem('ab_identity_name', user.displayName || user.email);
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_method', 'google');
    localStorage.setItem('ab_identity_photo', user.photoURL || '');

    return { success: true, displayName: user.displayName || user.email };
  } catch (e) {
    if (e.code === 'auth/popup-closed-by-user') {
      return { success: false, error: 'Sign-in cancelled.' };
    }
    console.error('[Auth] Google sign-in failed:', e);
    return { success: false, error: 'Sign-in failed. Try again.' };
  }
}

/**
 * Sign out from Firebase Auth and clear session.
 * @param {import('firebase/app').FirebaseApp} app
 */
export async function signOutGoogle(app) {
  try {
    const auth = getAuth(app);
    await signOut(auth);
  } catch (e) {
    // Ignore signout errors
  }
  logout();
}

// ==================== Passphrase System (legacy) ====================

export async function hashPassphrase(passphrase) {
  const data = new TextEncoder().encode(passphrase);
  const hashBuffer = await crypto.subtle.digest('SHA-256', data);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
}

export function slugifyName(displayName) {
  return displayName.toLowerCase();
}

export function validateDisplayName(name) {
  return typeof name === 'string' && name.length >= 2 && name.length <= 20;
}

export function validatePassphrase(passphrase) {
  return typeof passphrase === 'string' && passphrase.length >= 4;
}

export async function register(displayName, passphrase, db) {
  if (!validateDisplayName(displayName)) {
    return { success: false, error: 'Display name must be between 2 and 20 characters.' };
  }
  if (!validatePassphrase(passphrase)) {
    return { success: false, error: 'Passphrase must be at least 4 characters.' };
  }
  try {
    const slug = slugifyName(displayName);
    const userRef = doc(db, 'users', slug);
    const existing = await getDoc(userRef);
    if (existing.exists()) {
      return { success: false, error: 'That display name is already taken.' };
    }
    const passphraseHash = await hashPassphrase(passphrase);
    await setDoc(userRef, { displayName, passphraseHash, createdAt: serverTimestamp() });
    localStorage.setItem('ab_identity_name', displayName);
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_method', 'passphrase');
    localStorage.removeItem('ab_identity_photo');
    return { success: true };
  } catch (e) {
    return { success: false, error: 'Unable to connect. Please try again later.' };
  }
}

export async function login(displayName, passphrase, db) {
  try {
    const slug = slugifyName(displayName);
    const userRef = doc(db, 'users', slug);
    const snapshot = await getDoc(userRef);
    if (!snapshot.exists()) {
      return { success: false, error: 'Invalid display name or passphrase.' };
    }
    const userData = snapshot.data();
    if (userData.passwordReset) {
      return { success: false, passwordReset: true, displayName: userData.displayName };
    }
    const inputHash = await hashPassphrase(passphrase);
    if (inputHash !== userData.passphraseHash) {
      return { success: false, error: 'Invalid display name or passphrase.' };
    }
    localStorage.setItem('ab_identity_name', userData.displayName);
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_method', 'passphrase');
    localStorage.removeItem('ab_identity_photo');
    return { success: true };
  } catch (e) {
    return { success: false, error: 'Unable to connect. Please try again later.' };
  }
}

export async function setNewPassphrase(displayName, newPassphrase, db) {
  if (!validatePassphrase(newPassphrase)) {
    return { success: false, error: 'Passphrase must be at least 4 characters.' };
  }
  try {
    const slug = slugifyName(displayName);
    const userRef = doc(db, 'users', slug);
    const newHash = await hashPassphrase(newPassphrase);
    await setDoc(userRef, { passphraseHash: newHash, passwordReset: false }, { merge: true });
    localStorage.setItem('ab_identity_name', displayName);
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_method', 'passphrase');
    return { success: true };
  } catch (e) {
    return { success: false, error: 'Unable to connect. Please try again later.' };
  }
}

export async function adminResetPassword(displayName, db) {
  try {
    const slug = slugifyName(displayName);
    const userRef = doc(db, 'users', slug);
    const snapshot = await getDoc(userRef);
    if (!snapshot.exists()) {
      return { success: false, error: 'User not found.' };
    }
    await setDoc(userRef, { passwordReset: true }, { merge: true });
    return { success: true };
  } catch (e) {
    return { success: false, error: 'Unable to connect. Please try again later.' };
  }
}

// ==================== UI Rendering ====================

/**
 * Render the identity bar. Shows Google SSO button (primary) + passphrase toggle (secondary).
 * @param {HTMLElement} containerEl
 * @param {import('firebase/firestore').Firestore} db
 * @param {{ onAuthChange?: Function, app?: any }} [options]
 */
export function renderIdentityBar(containerEl, db, options) {
  const session = getSession();
  containerEl.innerHTML = '';
  if (session) {
    _renderLoggedIn(containerEl, db, options, session);
  } else {
    _renderLoggedOut(containerEl, db, options);
  }
}

function _renderLoggedIn(containerEl, db, options, session) {
  const wrapper = document.createElement('div');
  wrapper.className = 'identity-bar identity-bar--logged-in';

  // Avatar (for Google users)
  if (session.photoURL) {
    const avatar = document.createElement('img');
    avatar.src = session.photoURL;
    avatar.alt = '';
    avatar.style.cssText = 'width:28px;height:28px;border-radius:50%;border:1px solid var(--neon-cyan)';
    wrapper.appendChild(avatar);
  }

  const greeting = document.createElement('span');
  greeting.className = 'identity-bar__greeting';
  greeting.textContent = session.displayName;
  wrapper.appendChild(greeting);

  const methodBadge = document.createElement('span');
  methodBadge.style.cssText = 'font-size:.7em;color:var(--muted);text-transform:uppercase;letter-spacing:.5px';
  methodBadge.textContent = session.method === 'google' ? 'Google' : 'Passphrase';
  wrapper.appendChild(methodBadge);

  const logoutBtn = document.createElement('button');
  logoutBtn.className = 'identity-bar__logout-btn';
  logoutBtn.textContent = 'Logout';
  logoutBtn.addEventListener('click', async () => {
    if (session.method === 'google' && options?.app) {
      await signOutGoogle(options.app);
    } else {
      logout();
    }
    renderIdentityBar(containerEl, db, options);
    options?.onAuthChange?.(null);
  });
  wrapper.appendChild(logoutBtn);

  containerEl.appendChild(wrapper);
}

function _renderLoggedOut(containerEl, db, options) {
  const wrapper = document.createElement('div');
  wrapper.className = 'identity-bar identity-bar--logged-out';

  // Single row: Google button only
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;align-items:center;gap:10px;flex-wrap:wrap';

  // Google SSO button (compact)
  const googleBtn = document.createElement('button');
  googleBtn.className = 'identity-bar__google-btn';
  googleBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 48 48" style="vertical-align:middle;margin-right:6px"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>Sign in';
  googleBtn.addEventListener('click', async () => {
    if (!options?.app) return;
    googleBtn.disabled = true;
    googleBtn.textContent = '...';
    const result = await signInWithGoogle(options.app);
    if (result.success) {
      renderIdentityBar(containerEl, db, options);
      options?.onAuthChange?.(getSession());
    } else {
      googleBtn.disabled = false;
      googleBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 48 48" style="vertical-align:middle;margin-right:6px"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>Sign in';
    }
  });
  row.appendChild(googleBtn);
  wrapper.appendChild(row);
  containerEl.appendChild(wrapper);
}
