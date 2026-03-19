// identity.js — Passphrase-based identity system for the audiobook catalog
// ES module, browser-native (no build step)

import { doc, getDoc, setDoc, serverTimestamp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';

/**
 * Hash a passphrase using SHA-256 via the Web Crypto API.
 * @param {string} passphrase
 * @returns {Promise<string>} hex-encoded SHA-256 hash
 */
export async function hashPassphrase(passphrase) {
  const data = new TextEncoder().encode(passphrase);
  const hashBuffer = await crypto.subtle.digest('SHA-256', data);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
}

/**
 * Slugify a display name to lowercase for use as a Firestore document ID.
 * @param {string} displayName
 * @returns {string}
 */
export function slugifyName(displayName) {
  return displayName.toLowerCase();
}

/**
 * Validate a display name: must be 2-20 characters.
 * @param {string} name
 * @returns {boolean}
 */
export function validateDisplayName(name) {
  return typeof name === 'string' && name.length >= 2 && name.length <= 20;
}

/**
 * Validate a passphrase: must be 4+ characters.
 * @param {string} passphrase
 * @returns {boolean}
 */
export function validatePassphrase(passphrase) {
  return typeof passphrase === 'string' && passphrase.length >= 4;
}

/**
 * Register a new user with a display name and passphrase.
 * @param {string} displayName
 * @param {string} passphrase
 * @param {import('firebase/firestore').Firestore} db
 * @returns {Promise<{ success: boolean, error?: string }>}
 */
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

    await setDoc(userRef, {
      displayName,
      passphraseHash,
      createdAt: serverTimestamp(),
    });

    localStorage.setItem('ab_identity_name', displayName);
    localStorage.setItem('ab_identity_session', 'active');

    return { success: true };
  } catch (e) {
    return { success: false, error: 'Unable to connect. Please try again later.' };
  }
}

/**
 * Log in (reclaim) an existing user account by display name and passphrase.
 * Returns a generic error for both non-existent names and wrong passphrases
 * to avoid leaking which field was incorrect.
 * @param {string} displayName
 * @param {string} passphrase
 * @param {import('firebase/firestore').Firestore} db
 * @returns {Promise<{ success: boolean, error?: string }>}
 */
export async function login(displayName, passphrase, db) {
  try {
    const slug = slugifyName(displayName);
    const userRef = doc(db, 'users', slug);
    const snapshot = await getDoc(userRef);

    if (!snapshot.exists()) {
      return { success: false, error: 'Invalid display name or passphrase.' };
    }

    const userData = snapshot.data();

    // If password was reset by admin, prompt for new passphrase
    if (userData.passwordReset) {
      return { success: false, passwordReset: true, displayName: userData.displayName };
    }

    const inputHash = await hashPassphrase(passphrase);

    if (inputHash !== userData.passphraseHash) {
      return { success: false, error: 'Invalid display name or passphrase.' };
    }

    // Use the displayName from Firestore to preserve original casing
    localStorage.setItem('ab_identity_name', userData.displayName);
    localStorage.setItem('ab_identity_session', 'active');

    return { success: true };
  } catch (e) {
    return { success: false, error: 'Unable to connect. Please try again later.' };
  }
}

/**
 * Set a new passphrase after an admin-initiated password reset.
 * @param {string} displayName
 * @param {string} newPassphrase
 * @param {import('firebase/firestore').Firestore} db
 * @returns {Promise<{ success: boolean, error?: string }>}
 */
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

    return { success: true };
  } catch (e) {
    return { success: false, error: 'Unable to connect. Please try again later.' };
  }
}

/**
 * Admin: flag a user's account for password reset.
 * @param {string} displayName
 * @param {import('firebase/firestore').Firestore} db
 * @returns {Promise<{ success: boolean, error?: string }>}
 */
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

/**
 * Get the current session from localStorage.
 * Returns the session object if both keys are present and session is active,
 * otherwise returns null.
 * @returns {{ displayName: string } | null}
 */
export function getSession() {
  const displayName = localStorage.getItem('ab_identity_name');
  const session = localStorage.getItem('ab_identity_session');

  if (!displayName || session !== 'active') {
    return null;
  }

  return { displayName };
}

/**
 * Clear the current session from localStorage.
 */
export function logout() {
  localStorage.removeItem('ab_identity_name');
  localStorage.removeItem('ab_identity_session');
}

/**
 * Render the identity bar into the given container element.
 * Shows a login/register form when logged out, or a greeting + logout button when logged in.
 * @param {HTMLElement} containerEl
 * @param {import('firebase/firestore').Firestore} db
 * @param {{ onAuthChange?: (session: { displayName: string } | null) => void }} [options]
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

  const greeting = document.createElement('span');
  greeting.className = 'identity-bar__greeting';
  greeting.textContent = `Welcome, ${session.displayName}`;
  wrapper.appendChild(greeting);

  const logoutBtn = document.createElement('button');
  logoutBtn.className = 'identity-bar__logout-btn';
  logoutBtn.textContent = 'Logout';
  logoutBtn.addEventListener('click', () => {
    logout();
    renderIdentityBar(containerEl, db, options);
    options?.onAuthChange?.(getSession());
  });
  wrapper.appendChild(logoutBtn);

  containerEl.appendChild(wrapper);
}

function _renderLoggedOut(containerEl, db, options) {
  const wrapper = document.createElement('div');
  wrapper.className = 'identity-bar identity-bar--logged-out';

  const form = document.createElement('form');
  form.className = 'identity-bar__form';
  form.addEventListener('submit', (e) => e.preventDefault());

  // Display name field
  const nameLabel = document.createElement('label');
  nameLabel.className = 'identity-bar__label';
  nameLabel.textContent = 'Display Name';
  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'identity-bar__input';
  nameInput.setAttribute('aria-label', 'Display Name');
  nameLabel.appendChild(nameInput);
  form.appendChild(nameLabel);

  const nameError = document.createElement('span');
  nameError.className = 'identity-bar__error';
  nameError.setAttribute('role', 'alert');
  nameError.style.display = 'none';
  form.appendChild(nameError);

  // Passphrase field
  const passLabel = document.createElement('label');
  passLabel.className = 'identity-bar__label';
  passLabel.textContent = 'Passphrase';
  const passInput = document.createElement('input');
  passInput.type = 'password';
  passInput.className = 'identity-bar__input';
  passInput.setAttribute('aria-label', 'Passphrase');
  passLabel.appendChild(passInput);
  form.appendChild(passLabel);

  const passError = document.createElement('span');
  passError.className = 'identity-bar__error';
  passError.setAttribute('role', 'alert');
  passError.style.display = 'none';
  form.appendChild(passError);

  // General error area (for Firebase errors)
  const generalError = document.createElement('span');
  generalError.className = 'identity-bar__error identity-bar__error--general';
  generalError.setAttribute('role', 'alert');
  generalError.style.display = 'none';
  form.appendChild(generalError);

  // Buttons
  const btnGroup = document.createElement('div');
  btnGroup.className = 'identity-bar__buttons';

  const registerBtn = document.createElement('button');
  registerBtn.type = 'button';
  registerBtn.className = 'identity-bar__register-btn';
  registerBtn.textContent = 'Register';

  const signInBtn = document.createElement('button');
  signInBtn.type = 'button';
  signInBtn.className = 'identity-bar__signin-btn';
  signInBtn.textContent = 'Sign In';

  btnGroup.appendChild(registerBtn);
  btnGroup.appendChild(signInBtn);
  form.appendChild(btnGroup);

  wrapper.appendChild(form);
  containerEl.appendChild(wrapper);

  // Validation helper
  function validateFields() {
    let valid = true;
    nameError.style.display = 'none';
    nameError.textContent = '';
    passError.style.display = 'none';
    passError.textContent = '';
    generalError.style.display = 'none';
    generalError.textContent = '';

    if (!validateDisplayName(nameInput.value)) {
      nameError.textContent = 'Display name must be between 2 and 20 characters.';
      nameError.style.display = '';
      valid = false;
    }
    if (!validatePassphrase(passInput.value)) {
      passError.textContent = 'Passphrase must be at least 4 characters.';
      passError.style.display = '';
      valid = false;
    }
    return valid;
  }

  async function handleAuth(authFn) {
    if (!validateFields()) return;

    const result = await authFn(nameInput.value, passInput.value, db);
    if (result.success) {
      renderIdentityBar(containerEl, db, options);
      options?.onAuthChange?.(getSession());
    } else if (result.passwordReset) {
      _renderPasswordReset(containerEl, db, options, result.displayName);
    } else {
      generalError.textContent = result.error;
      generalError.style.display = '';
    }
  }

  async function handleSignIn() {
    // Only validate display name first
    nameError.style.display = 'none';
    passError.style.display = 'none';
    generalError.style.display = 'none';

    if (!validateDisplayName(nameInput.value)) {
      nameError.textContent = 'Display name must be between 2 and 20 characters.';
      nameError.style.display = '';
      return;
    }

    // Check for password reset before requiring passphrase
    try {
      const slug = slugifyName(nameInput.value);
      const userRef = doc(db, 'users', slug);
      const snapshot = await getDoc(userRef);
      if (snapshot.exists() && snapshot.data().passwordReset) {
        _renderPasswordReset(containerEl, db, options, snapshot.data().displayName);
        return;
      }
    } catch (e) {
      // Fall through to normal login
    }

    // Now validate passphrase for normal login
    if (!validatePassphrase(passInput.value)) {
      passError.textContent = 'Passphrase must be at least 4 characters.';
      passError.style.display = '';
      return;
    }

    const result = await login(nameInput.value, passInput.value, db);
    if (result.success) {
      renderIdentityBar(containerEl, db, options);
      options?.onAuthChange?.(getSession());
    } else {
      generalError.textContent = result.error;
      generalError.style.display = '';
    }
  }

  registerBtn.addEventListener('click', () => handleAuth(register));
  signInBtn.addEventListener('click', handleSignIn);
}

function _renderPasswordReset(containerEl, db, options, displayName) {
  containerEl.innerHTML = '';

  const wrapper = document.createElement('div');
  wrapper.className = 'identity-bar identity-bar--logged-out';

  const msg = document.createElement('p');
  msg.style.cssText = 'margin:0 0 8px; font-weight:600';
  msg.textContent = `Hi ${displayName}, your password was reset. Please set a new passphrase.`;
  wrapper.appendChild(msg);

  const form = document.createElement('form');
  form.className = 'identity-bar__form';
  form.addEventListener('submit', (e) => e.preventDefault());

  const passLabel = document.createElement('label');
  passLabel.className = 'identity-bar__label';
  passLabel.textContent = 'New Passphrase';
  const passInput = document.createElement('input');
  passInput.type = 'password';
  passInput.className = 'identity-bar__input';
  passInput.setAttribute('aria-label', 'New Passphrase');
  passLabel.appendChild(passInput);
  form.appendChild(passLabel);

  const confirmLabel = document.createElement('label');
  confirmLabel.className = 'identity-bar__label';
  confirmLabel.textContent = 'Confirm Passphrase';
  const confirmInput = document.createElement('input');
  confirmInput.type = 'password';
  confirmInput.className = 'identity-bar__input';
  confirmInput.setAttribute('aria-label', 'Confirm Passphrase');
  confirmLabel.appendChild(confirmInput);
  form.appendChild(confirmLabel);

  const errorEl = document.createElement('span');
  errorEl.className = 'identity-bar__error identity-bar__error--general';
  errorEl.setAttribute('role', 'alert');
  errorEl.style.display = 'none';
  form.appendChild(errorEl);

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'identity-bar__register-btn';
  saveBtn.textContent = 'Set New Passphrase';

  saveBtn.addEventListener('click', async () => {
    errorEl.style.display = 'none';
    if (!validatePassphrase(passInput.value)) {
      errorEl.textContent = 'Passphrase must be at least 4 characters.';
      errorEl.style.display = '';
      return;
    }
    if (passInput.value !== confirmInput.value) {
      errorEl.textContent = 'Passphrases do not match.';
      errorEl.style.display = '';
      return;
    }
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    const result = await setNewPassphrase(displayName, passInput.value, db);
    if (result.success) {
      renderIdentityBar(containerEl, db, options);
      options?.onAuthChange?.(getSession());
    } else {
      errorEl.textContent = result.error;
      errorEl.style.display = '';
      saveBtn.disabled = false;
      saveBtn.textContent = 'Set New Passphrase';
    }
  });

  form.appendChild(saveBtn);
  wrapper.appendChild(form);
  containerEl.appendChild(wrapper);
}
