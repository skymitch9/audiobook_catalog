// identity.js — Google SSO + passphrase fallback identity system
// ES module, browser-native (no build step)

import { doc, getDoc, setDoc, getFirestore, serverTimestamp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { getAuth, signInWithPopup, signInWithRedirect, getRedirectResult, GoogleAuthProvider, onAuthStateChanged, signOut } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js';
import { col } from './fb-env.js';

// ==================== Session Management ====================

/**
 * Set for the duration of a redirect sign-in — from just before we navigate
 * away to the moment the result is collected on the way back.
 *
 * It has to survive a full page navigation, so module state cannot hold it and
 * sessionStorage does. `detachStaleFirebaseAuth()` reads it to know it must
 * keep its hands off the Auth session; see the comment there.
 */
const REDIRECT_PENDING = 'ab_identity_redirect_pending';

/**
 * True from the moment a sign-in starts until it has been captured and
 * deliberately torn down.
 *
 * ⚠️ Without this, the load-time cleanup in `detachStaleFirebaseAuth()` races
 * the sign-in it is supposed to run before. A successful popup makes Firebase
 * publish the new user, that listener sees "a user appeared" and signs it out —
 * while `signInWithPopup` is still completing its credential exchange, so the
 * promise never settles and the button sits on "Signing in…" for ever. Nothing
 * is thrown, so there is no catch and no console error; the only visible symptom
 * is a dead button.
 *
 * Declared here, above its first use, rather than beside the function that
 * clears it: both `handleRedirectResult()` and `signInWithGoogle()` touch it,
 * and a `let` referenced from a function defined above its declaration is a
 * temporal-dead-zone trap for whoever next moves a call site.
 */
let _signInInFlight = false;

/**
 * Call on page load to complete a redirect-based Google sign-in.
 * If there's a pending redirect result, it finishes the sign-in and reloads.
 *
 * ⚠️ **Every page that offers sign-in must call this.** It is not optional and
 * it is not localhost-only any more: `signInWithGoogle()` falls back to the
 * redirect flow whenever a popup cannot be opened, which is the normal case on
 * mobile and inside in-app browsers. A page that omits this call sends the user
 * to Google, brings them back, and drops the credential on the floor — they
 * land on the page still signed out, with no error to explain it.
 *
 * @param {import('firebase/app').FirebaseApp} app
 */
export async function handleRedirectResult(app) {
  if (!app) return;
  // Same claim as the popup path: collecting a redirect result publishes an auth
  // state change, and nothing else must interpret that as a stale session.
  _signInInFlight = true;
  try {
    const auth = getAuth(app);
    const result = await getRedirectResult(auth);
    // Cleared whether or not a result came back: a `null` means there was no
    // redirect in flight, so leaving the flag set would suppress the auth
    // detach on every subsequent load of this tab.
    sessionStorage.removeItem(REDIRECT_PENDING);
    if (result && result.user) {
      const user = result.user;
      localStorage.setItem('ab_identity_name', user.displayName || user.email);
      localStorage.setItem('ab_identity_session', 'active');
      localStorage.setItem('ab_identity_method', 'google');
      localStorage.setItem('ab_identity_photo', user.photoURL || '');
      localStorage.setItem('ab_identity_email', user.email || '');
      await ensureProfile(getFirestore(app), user.displayName || user.email, user.photoURL || '');
      try { await signOut(auth); } catch (e) { /* non-fatal */ }
      location.reload();
    }
  } catch (e) {
    console.warn('[Identity] redirect result error:', e);
  } finally {
    _signInInFlight = false;
  }
}

/**
 * Get the current session. Checks Firebase Auth first, then localStorage fallback.
 * @returns {{ displayName: string, photoURL?: string, uid?: string, method: 'google'|'passphrase' } | null}
 */
let _authDetached = false;

/** One-time cleanup: drop any persisted Firebase Auth session left over
 * from before the detach-after-SSO fix. Identity lives in localStorage. */
function detachStaleFirebaseAuth() {
  if (_authDetached) return;
  _authDetached = true;
  // On localhost we use redirect auth — don't detach or getRedirectResult returns null
  if (['localhost', '127.0.0.1'].includes(location.hostname)) return;
  // Same hazard, now reachable on any host: a redirect sign-in is mid-flight and
  // the credential Firebase is about to hand back lives in the very Auth session
  // this would tear down. Sign out here and getRedirectResult() returns null, so
  // the user comes back from Google still signed out. getSession() runs on load
  // and can easily win the race against getRedirectResult(), so the guard has to
  // be a flag that survived the navigation rather than anything in module state.
  try {
    if (sessionStorage.getItem(REDIRECT_PENDING) === '1') return;
  } catch (e) { /* storage disabled — fall through and detach as before */ }
  try {
    const auth = getAuth();
    if (auth.currentUser) { signOut(auth).catch(() => {}); return; }
    // ⚠️ ONE SHOT — unsubscribe inside the first callback.
    //
    // This used to be a permanent subscription, which is not what "one-time
    // cleanup" means and is not what it did: it stayed armed for the life of the
    // page and signed out *every* user who ever appeared, including the one a
    // sign-in had just produced a second earlier.
    //
    // Unsubscribing on the first callback is still correct for the stated job.
    // Firebase does not fire this until persistence has been read, so the first
    // callback already carries any restored session — which is exactly the stale
    // session this exists to drop. Anything arriving after that is a live
    // sign-in, and is none of this function's business.
    const unsubscribe = onAuthStateChanged(auth, (user) => {
      unsubscribe();
      if (user && !_signInInFlight) signOut(auth).catch(() => {});
    });
  } catch (e) { /* no default app yet — harmless */ }
}

export function getSession() {
  detachStaleFirebaseAuth();
  // Check localStorage for active session (works for both methods)
  const name = localStorage.getItem('ab_identity_name');
  const session = localStorage.getItem('ab_identity_session');
  const method = localStorage.getItem('ab_identity_method') || 'passphrase';
  const photoURL = localStorage.getItem('ab_identity_photo') || '';

  if (!name || session !== 'active') {
    return null;
  }

  const email = localStorage.getItem('ab_identity_email') || '';
  return { displayName: name, photoURL, method, email };
}

// Accounts treated as admin. Both lanes (dev and prod) use the same list —
// they are the same person and the same Firebase project.
//   - emails come from Google SSO (stable; survives a display-name change)
//   - names cover the passphrase fallback, which has no email
export const ADMIN_EMAILS = ['nbaslamking@gmail.com'];
export const ADMIN_NAMES = ['!Sky', 'Skylar'];

/**
 * Is this session an admin?
 *
 * PRESENTATION ONLY — this decides what the UI shows, nothing more. The
 * session comes from localStorage, so anyone can set ab_identity_name and
 * pass this check. It is not, and cannot be, an access control: enforcing it
 * needs Firebase Auth plus request.auth in firestore.rules. Never rely on it
 * to protect an action that matters; the pipeline trigger is protected by a
 * token the client never ships instead. See the 2026-08-04 handoff in
 * docs/TODO.md.
 *
 * @param {{displayName?: string, email?: string}|null} [session] defaults to getSession()
 * @returns {boolean}
 */
export function isAdmin(session) {
  const s = session === undefined ? getSession() : session;
  if (!s) return false;
  const email = (s.email || '').trim().toLowerCase();
  const name = (s.displayName || '').trim();
  return ADMIN_EMAILS.includes(email) || ADMIN_NAMES.includes(name);
}

/**
 * Clear the current session.
 */
export function logout() {
  localStorage.removeItem('ab_identity_name');
  localStorage.removeItem('ab_identity_session');
  localStorage.removeItem('ab_identity_method');
  localStorage.removeItem('ab_identity_photo');
  localStorage.removeItem('ab_identity_email');
}

// ==================== Google SSO ====================

/**
 * Popup failures that mean *the browser will not give us a popup*, as opposed to
 * *the person changed their mind*. Only these justify falling back to redirect.
 */
const POPUP_UNAVAILABLE = new Set([
  'auth/popup-blocked',
  'auth/operation-not-supported-in-this-environment',
  'auth/web-storage-unsupported',
]);

/**
 * Begin the redirect sign-in. Navigates away; nothing after it runs.
 *
 * The flag is set *before* the call, because once `signInWithRedirect` starts
 * the navigation there is no later moment on this page to set it in.
 */
async function startRedirect(auth, provider) {
  try {
    sessionStorage.setItem(REDIRECT_PENDING, '1');
  } catch (e) { /* storage disabled — the redirect is still worth attempting */ }
  try {
    await signInWithRedirect(auth, provider);
  } catch (e) {
    // ⚠️ The navigation never happened, so nothing will ever come back to clear
    // this. Left set, it makes detachStaleFirebaseAuth() a no-op for the rest of
    // the tab's life and a stale Auth session survives to poison Firestore
    // writes later. Observed for real: serving on 127.0.0.1 (not an authorized
    // domain) failed here and stranded the flag.
    try { sessionStorage.removeItem(REDIRECT_PENDING); } catch (e2) { /* ignore */ }
    throw e;
  }
}

/**
 * Sign in with Google via Firebase Auth popup.
 * @param {import('firebase/app').FirebaseApp} app
 * @returns {Promise<{ success: boolean, displayName?: string, error?: string }>}
 */
export async function signInWithGoogle(app) {
  // Claim the flag before anything can publish an auth state change, so the
  // load-time cleanup cannot mistake this sign-in for a stale session and sign
  // it out mid-flight. Released in the finally below, after the intentional
  // detach has already run.
  _signInInFlight = true;
  try {
    const auth = getAuth(app);
    const provider = new GoogleAuthProvider();

    // On localhost, Chrome's COOP blocks popup communication — use redirect instead
    const isLocal = ['localhost', '127.0.0.1'].includes(location.hostname);
    let user;
    if (isLocal) {
      // Redirect flow: this call navigates away, result is picked up on return
      await startRedirect(auth, provider);
      return { success: false, error: 'Redirecting...' }; // won't reach here
    } else {
      // Popup first — it keeps the page state, and it is what desktop has always
      // used. But a popup is a privilege the browser can simply decline, and on
      // mobile and inside in-app browsers (the Gmail/Slack/Instagram webviews)
      // it usually does. Falling back to redirect turns a dead button into a
      // slower sign-in rather than no sign-in at all.
      let result;
      try {
        result = await signInWithPopup(auth, provider);
      } catch (popupErr) {
        if (!POPUP_UNAVAILABLE.has(popupErr?.code)) throw popupErr;
        // ⚠️ Deliberately NOT triggered by auth/popup-closed-by-user or
        // auth/cancelled-popup-request. Those mean the person chose to back out,
        // and answering a cancellation by navigating the whole page to Google is
        // a worse surprise than the failure it would be papering over.
        console.warn('[Auth] popup unavailable (%s) — falling back to redirect', popupErr.code);
        await startRedirect(auth, provider);
        return { success: false, error: 'Redirecting...' }; // won't reach here
      }
      user = result.user;
    }

    localStorage.setItem('ab_identity_name', user.displayName || user.email);
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_method', 'google');
    localStorage.setItem('ab_identity_photo', user.photoURL || '');
    // Captured so isAdmin() can key on the Google account rather than a
    // display name, which a user can change at any time in their Google
    // profile and which would silently drop admin access if it did.
    localStorage.setItem('ab_identity_email', user.email || '');

    await ensureProfile(getFirestore(app), user.displayName || user.email, user.photoURL || '');

    // Google is only used to capture identity — the site's Firestore rules
    // never check auth. Detach immediately so a persisted auth session can't
    // later expire and poison Firestore writes with PERMISSION_DENIED
    // (stale-token refresh failures, esp. mobile Safari).
    try { await signOut(auth); } catch (e) { /* non-fatal */ }

    return { success: true, displayName: user.displayName || user.email };
  } catch (e) {
    if (e.code === 'auth/popup-closed-by-user') {
      return { success: false, error: 'Sign-in cancelled.' };
    }
    console.error('[Auth] Google sign-in failed:', e);
    return { success: false, error: 'Sign-in failed. Try again.' };
  } finally {
    // Cleared on every exit, including the redirect path — that one navigates
    // away and the whole module is reloaded on the way back, so the flag is
    // module state that simply ceases to exist rather than something to unset.
    _signInInFlight = false;
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

/**
 * Make sure a profile doc exists so the user shows up on the Community
 * page. Merge-write: never clobbers reading/favorites/game fields.
 */
async function ensureProfile(db, displayName, photoURL) {
  try {
    const data = { displayName };
    if (photoURL) data.photoURL = photoURL;
    await setDoc(doc(db, col('profiles'), slugifyName(displayName)), data, { merge: true });
  } catch (e) {
    console.warn('[Identity] profile ensure failed:', e);
  }
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
    const userRef = doc(db, col('users'), slug);
    const existing = await getDoc(userRef);
    if (existing.exists()) {
      return { success: false, error: 'That display name is already taken.' };
    }
    const passphraseHash = await hashPassphrase(passphrase);
    await setDoc(userRef, { displayName, passphraseHash, createdAt: serverTimestamp() });
    await ensureProfile(db, displayName);
    localStorage.setItem('ab_identity_name', displayName);
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_method', 'passphrase');
    localStorage.removeItem('ab_identity_photo');
    localStorage.removeItem('ab_identity_email');
    return { success: true };
  } catch (e) {
    return { success: false, error: 'Unable to connect. Please try again later.' };
  }
}

export async function login(displayName, passphrase, db) {
  try {
    const slug = slugifyName(displayName);
    const userRef = doc(db, col('users'), slug);
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
    await ensureProfile(db, userData.displayName);
    localStorage.setItem('ab_identity_name', userData.displayName);
    localStorage.setItem('ab_identity_session', 'active');
    localStorage.setItem('ab_identity_method', 'passphrase');
    localStorage.removeItem('ab_identity_photo');
    localStorage.removeItem('ab_identity_email');
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
    const userRef = doc(db, col('users'), slug);
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
    const userRef = doc(db, col('users'), slug);
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
  methodBadge.style.cssText = 'font-size:.7em;color:var(--muted);text-transform:var(--et-title-case);letter-spacing:.5px';
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
