// identity.js — estate Google sign-in with a LIVE Firebase session
// ES module, browser-native (no build step)
//
// v2, 2026-08-14 (owner: "bring the audiobook site in line with auth").
// The v1 model captured a name into localStorage and signed the Firebase
// session OUT immediately, because a persisted session that later failed a
// token refresh could poison Firestore writes. The estate settled the other
// way — apex (assets/estate-auth.js) and the library keep the live session —
// and this site now matches: sign in once with Google, stay signed in.
//
// ⚠️ What did NOT change, and must not (catalog-platform PLATFORM.md §4a):
// firestore.rules stays SHAPE-ONLY and never checks request.auth. The live
// session is an identity convenience, not an access control — the site stays
// world-readable, anonymous reads (crons included) keep working, and the
// work_key carry across review docs depends on rules that ignore auth.
// A live token is simply present now; the rules keep ignoring it.
//
// Session model:
//   - TRUTH is the Firebase Auth session (SDK persistence + onAuthStateChanged).
//   - The ab_identity_* localStorage keys survive as a synchronous MIRROR of
//     that truth, written only from auth state. They exist because pages read
//     identity synchronously at load (guess-game.html reads the keys raw), and
//     Firebase publishes its restored session asynchronously.
//   - A mirror row carries ab_identity_live='1' proving a live session wrote
//     it. A row WITHOUT that marker is a legacy v1 capture (google-detach or
//     passphrase). Legacy rows are never wiped by the auth listener — the
//     person keeps their name and their writes keep working (shape-only
//     rules) — but the UI offers a one-time "sign in with Google" upgrade.
//   - The passphrase system is retired. Its users were the owner and one
//     retired account; nobody migrates, the paths simply end.

import { doc, getDoc, setDoc, getFirestore } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { getAuth, signInWithPopup, signInWithRedirect, getRedirectResult, GoogleAuthProvider, onAuthStateChanged, signOut } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js';
import { col, IS_DEV_LANE } from './fb-env.js';

// ==================== The mirror ====================

/**
 * Marker proving the mirror was written from a live Firebase session.
 *   '1'    — written by the auth listener / a completed sign-in
 *   'stub' — written by the dev-lane test seam (see the bottom of this file)
 *   absent — a legacy v1 capture; see the module comment
 */
const LIVE_MARKER = 'ab_identity_live';

/**
 * Remove EVERY ab_identity_* key — known names, v1-era names, and anything a
 * future writer mints — by prefix sweep, not a fixed list.
 *
 * ⚠️ Why a sweep and not a list (owner's attended pass, 2026-08-14): until
 * this ships to prod, BOTH lanes share this origin's localStorage and prod
 * still runs v1, whose sign-in writes the same key names WITHOUT the live
 * marker and then detaches Firebase. Key shape therefore cannot distinguish
 * "pre-v2 legacy capture" (honor it) from "post-v2 residue" (clear it) — the
 * v1 writer keeps producing the legacy shape while v2 is live. The
 * distinction is BEHAVIORAL instead: every v2 sign-out sweeps the whole
 * family, and every v2 mirror write supersedes it, so an untagged row can
 * only mean "written by a v1 page and not yet touched by any v2 action" —
 * which is exactly the legacy case the upgrade panel exists for.
 */
function sweepMirror() {
  const stale = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k && k.indexOf('ab_identity') === 0) stale.push(k);
  }
  stale.forEach((k) => localStorage.removeItem(k));
}

/** Write the mirror from a live Firebase user. Supersedes any stale
 * ab_identity_* keys this writer does not own (v1 residue included). */
function mirrorUser(user, marker) {
  sweepMirror();
  localStorage.setItem('ab_identity_name', user.displayName || user.email);
  localStorage.setItem('ab_identity_session', 'active');
  localStorage.setItem('ab_identity_method', 'google');
  localStorage.setItem('ab_identity_photo', user.photoURL || '');
  // Captured so isAdmin() can key on the Google account rather than a
  // display name, which the user can change in their Google profile at any
  // time and which would silently drop admin presentation if it did.
  localStorage.setItem('ab_identity_email', user.email || '');
  localStorage.setItem(LIVE_MARKER, marker || '1');
}

/**
 * Clear the mirror — the WHOLE ab_identity_* family, tagged or not, so a
 * sign-out always lands in a truly signed-out UI whatever residue any lane's
 * code left behind. Public as `logout()` for legacy sessions and tests.
 * Also drops the per-session estate-approval cache: a signed-out (or
 * revoked-and-signed-out) person must not keep a cached "approved".
 */
export function logout() {
  sweepMirror();
  clearEstateCache();
}

let _mirrorAttached = false;

/**
 * Keep the mirror in sync with Firebase auth state. Attached once per page by
 * whichever entry point first receives the app (every page that offers
 * sign-in calls handleRedirectResult on load, so this always runs).
 *
 * ⚠️ The null branch only clears mirrors the listener itself wrote
 * (marker '1'). A legacy v1 row has no live session behind it, so Firebase
 * reporting "no user" says nothing about it — wiping it here would silently
 * strand a signed-in-looking person, which is exactly what the migration
 * must never do. The dev-lane stub ('stub') is likewise not the listener's
 * to clear.
 */
function attachAuthMirror(app) {
  if (_mirrorAttached || !app) return;
  _mirrorAttached = true;
  try {
    onAuthStateChanged(getAuth(app), (user) => {
      if (user) {
        mirrorUser(user);
      } else if (localStorage.getItem(LIVE_MARKER) === '1') {
        logout();
      }
    });
  } catch (e) {
    console.warn('[Identity] auth mirror attach failed:', e);
  }
}

/**
 * Get the current session, synchronously, from the mirror.
 *
 * `legacy: true` marks a v1 capture with no live Firebase session behind it.
 * Everything still works for that person (shape-only rules never needed a
 * token), but sign-in surfaces should offer the one-time Google upgrade —
 * see renderIdentityBar / account-modal.js.
 *
 * @returns {{ displayName: string, photoURL: string, method: string,
 *             email: string, legacy: boolean } | null}
 */
export function getSession() {
  const name = localStorage.getItem('ab_identity_name');
  const session = localStorage.getItem('ab_identity_session');
  const method = localStorage.getItem('ab_identity_method') || 'passphrase';
  const photoURL = localStorage.getItem('ab_identity_photo') || '';

  if (!name || session !== 'active') {
    return null;
  }

  const email = localStorage.getItem('ab_identity_email') || '';
  const legacy = localStorage.getItem(LIVE_MARKER) === null;
  return { displayName: name, photoURL, method, email, legacy };
}

// Accounts treated as admin. Both lanes (dev and prod) use the same list —
// they are the same person and the same Firebase project.
//   - emails come from Google SSO (stable; survives a display-name change)
//   - names cover retired passphrase identities that may live on as legacy
//     sessions until their one-time Google upgrade
export const ADMIN_EMAILS = ['nbaslamking@gmail.com'];
export const ADMIN_NAMES = ['!Sky', 'Skylar'];

/**
 * Is this session an admin?
 *
 * PRESENTATION ONLY — this decides what the UI shows, nothing more. The
 * session comes from a localStorage mirror, so anyone can set
 * ab_identity_name and pass this check. It is not, and cannot be, an access
 * control: enforcing it needs request.auth in firestore.rules, which §4a
 * (catalog-platform PLATFORM.md) deliberately rules out. Never rely on it to
 * protect an action that matters; the pipeline trigger is protected by a
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

// ==================== Google sign-in ====================

/**
 * Popup failures that mean *the browser will not give us a popup*, as opposed
 * to *the person changed their mind*. Only these justify falling back to
 * redirect.
 */
const POPUP_UNAVAILABLE = new Set([
  'auth/popup-blocked',
  'auth/operation-not-supported-in-this-environment',
  'auth/web-storage-unsupported',
]);

/**
 * Call on page load to complete a redirect-based Google sign-in.
 *
 * ⚠️ **Every page that offers sign-in must call this.** It is not optional:
 * `signInWithGoogle()` falls back to the redirect flow whenever a popup
 * cannot be opened, which is the normal case on mobile and inside in-app
 * browsers. A page that omits this call sends the user to Google, brings
 * them back, and drops the credential on the floor — they land on the page
 * still signed out, with no error to explain it.
 *
 * Also attaches the auth mirror, so simply calling this keeps the page's
 * synchronous identity view honest.
 *
 * @param {import('firebase/app').FirebaseApp} app
 */
export async function handleRedirectResult(app) {
  if (!app) return;
  attachAuthMirror(app);
  try {
    const auth = getAuth(app);
    const result = await getRedirectResult(auth);
    if (result && result.user) {
      const user = result.user;
      mirrorUser(user);
      await ensureProfile(getFirestore(app), user.displayName || user.email, user.photoURL || '');
      // The session stays live — no signOut. Reload so every part of the
      // page (much of it rendered synchronously at load) sees the session.
      location.reload();
    }
  } catch (e) {
    console.warn('[Identity] redirect result error:', e);
  }
}

/**
 * Sign in with Google via Firebase Auth popup, redirect as fallback.
 * The session is KEPT — Firebase persistence makes it survive reloads and
 * the auth mirror keeps getSession() in step from then on.
 *
 * @param {import('firebase/app').FirebaseApp} app
 * @returns {Promise<{ success: boolean, displayName?: string, error?: string }>}
 */
export async function signInWithGoogle(app) {
  attachAuthMirror(app);
  try {
    const auth = getAuth(app);
    const provider = new GoogleAuthProvider();

    // On localhost, Chrome's COOP blocks popup communication — use redirect
    const isLocal = ['localhost', '127.0.0.1'].includes(location.hostname);
    let user;
    if (isLocal) {
      await signInWithRedirect(auth, provider);
      return { success: false, error: 'Redirecting...' }; // won't reach here
    } else {
      // Popup first — it keeps the page state, and it is what desktop has
      // always used. But a popup is a privilege the browser can decline, and
      // on mobile and inside in-app browsers it usually does. Falling back to
      // redirect turns a dead button into a slower sign-in.
      let result;
      try {
        result = await signInWithPopup(auth, provider);
      } catch (popupErr) {
        if (!POPUP_UNAVAILABLE.has(popupErr?.code)) throw popupErr;
        // ⚠️ Deliberately NOT triggered by auth/popup-closed-by-user or
        // auth/cancelled-popup-request. Those mean the person chose to back
        // out, and answering a cancellation by navigating the whole page to
        // Google is a worse surprise than the failure it would paper over.
        console.warn('[Auth] popup unavailable (%s) — falling back to redirect', popupErr.code);
        await signInWithRedirect(auth, provider);
        return { success: false, error: 'Redirecting...' }; // won't reach here
      }
      user = result.user;
    }

    mirrorUser(user);
    await ensureProfile(getFirestore(app), user.displayName || user.email, user.photoURL || '');

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
 * Sign out: end the Firebase session and clear the mirror. Also the right
 * call for legacy sessions — the signOut of an absent session is a no-op.
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

// ==================== Estate approval (the dev-site gate) ====================
//
// Testing permission IS estate approval — granted and revoked at
// heygabi.ai/admin, no parallel testers list. The auth Worker's
// GET /api/estate/me answers { status, is_approver, visibility } for the
// caller's own Firebase ID token; the account modals show a quiet
// "Dev site →" link iff status === 'approved'. Everything else — signed
// out, legacy/stub session (no live token to send), pending, revoked,
// unknown, fetch failure — silently shows nothing.
//
// The answer is cached per browser session in sessionStorage, keyed by uid,
// so the modal does not refetch on every open. logout() drops the cache
// alongside the ab_identity_* sweep, so sign-out forgets the approval too.

const ESTATE_ME_URL = 'https://auth.heygabi.ai/api/estate/me';
const ESTATE_CACHE_PREFIX = 'ab_identity_estate_me_'; // + uid, in sessionStorage

function clearEstateCache() {
  try {
    const stale = [];
    for (let i = 0; i < sessionStorage.length; i++) {
      const k = sessionStorage.key(i);
      if (k && k.indexOf(ESTATE_CACHE_PREFIX) === 0) stale.push(k);
    }
    stale.forEach((k) => sessionStorage.removeItem(k));
  } catch (e) { /* storage unavailable — nothing cached anyway */ }
}

/**
 * The live Firebase user, or null. Waits for the SDK to publish its restored
 * session (the mirror is synchronous but the session is not), so a modal
 * opened right after load still gets an answer. A legacy or stub mirror has
 * no live user behind it and resolves null.
 */
function liveUser(app) {
  return new Promise((resolve) => {
    if (!app) return resolve(null);
    try {
      const auth = getAuth(app);
      if (auth.currentUser) return resolve(auth.currentUser);
      const unsub = onAuthStateChanged(auth, (user) => { unsub(); resolve(user || null); });
    } catch (e) {
      resolve(null);
    }
  });
}

/**
 * Public access to the live Firebase user (or null). The mirror
 * (getSession) has no uid on purpose — it is synchronous presentation. Any
 * caller that needs the ENFORCED identity (club manager claims, site-role
 * lookups) must go through here and handle null: a legacy or stub session
 * resolves null, because nothing verifiable stands behind it.
 * @returns {Promise<{uid: string, email: string|null, displayName: string|null}|null>}
 */
export async function getLiveUser(app) {
  const user = await liveUser(app);
  return user && user.uid
    ? { uid: user.uid, email: user.email || null, displayName: user.displayName || null }
    : null;
}

// ==================== Site roles (rules-enforced admin) ====================
//
// site_roles/{uid} is the site's first REAL role: written only server-side
// (scripts/seed_site_admin.py via the service account; browsers are denied
// all writes), readable only as your own doc (rules: doc id must equal
// request.auth.uid; no listing). So unlike isAdmin() above — presentation
// only, spoofable from devtools — a site_roles answer is enforced end to
// end: the same doc gates club-manager writes inside firestore.rules.
//
// ⚠️ The collection is UNSUFFIXED on both lanes (like pipeline_*): a role
// belongs to the person, not the data lane. Do not wrap it in col().

/**
 * The signed-in person's own site-role doc, or null (signed out, legacy
 * session, no role granted, or the read failed). Never throws.
 * @returns {Promise<{uid: string, role: string}|null>}
 */
export async function getSiteRole(db, app) {
  try {
    const user = await liveUser(app);
    if (!user || !user.uid) return null;
    const snap = await getDoc(doc(db, 'site_roles', user.uid));
    if (!snap.exists()) return null;
    return { uid: user.uid, ...snap.data() };
  } catch (e) {
    return null; // no answer is "no role" — the gate stays closed
  }
}

/** Is the signed-in person the rules-enforced site admin? */
export async function isSiteAdmin(db, app) {
  const role = await getSiteRole(db, app);
  return !!role && role.role === 'admin';
}

/**
 * The estate's answer about the current signed-in person, or null when there
 * is no live session or the answer cannot be had. Never throws.
 * @returns {Promise<{status: string|null, is_approver: boolean, visibility: string[]}|null>}
 */
export async function getEstateStatus(app) {
  try {
    const user = await liveUser(app);
    if (!user || !user.uid) return null;
    const cacheKey = ESTATE_CACHE_PREFIX + user.uid;
    try {
      const cached = sessionStorage.getItem(cacheKey);
      if (cached) return JSON.parse(cached);
    } catch (e) { /* unreadable cache — fall through to a fresh fetch */ }
    const token = await user.getIdToken();
    const res = await fetch(ESTATE_ME_URL, { headers: { Authorization: 'Bearer ' + token } });
    if (!res.ok) return null; // a failure is not an answer — do not cache it
    const answer = await res.json();
    try { sessionStorage.setItem(cacheKey, JSON.stringify(answer)); } catch (e) { /* full — refetch next time */ }
    return answer;
  } catch (e) {
    return null; // fetch failed / offline — the gate simply stays closed
  }
}

/** Approved in the estate directory? The one question the dev-site link asks. */
export async function isEstateApproved(app) {
  const me = await getEstateStatus(app);
  return !!me && me.status === 'approved';
}

/**
 * Fill `slotEl` with a quiet "Dev site →" link iff the current live session
 * is estate-approved; otherwise leave it empty, silently. Both account
 * modals (index.html inline + account-modal.js) call this with an empty
 * slot div rendered near the Appearance section.
 */
export function renderDevSiteLink(slotEl, app) {
  if (!slotEl || !app) return;
  isEstateApproved(app).then((ok) => {
    if (!ok) return;
    const a = document.createElement('a');
    a.href = '/dev/';
    a.textContent = 'Dev site →';
    a.style.cssText = 'display:block;text-align:center;margin-top:8px;padding:8px;border:1px solid var(--border,#2a2a3a);text-decoration:none;color:var(--muted,#8a8f98);font-size:.8em;letter-spacing:.5px';
    slotEl.innerHTML = '';
    slotEl.appendChild(a);
  }).catch(() => { /* silently no link */ });
}

// ==================== Profiles ====================

export function slugifyName(displayName) {
  return displayName.toLowerCase();
}

/**
 * Make sure a profile doc exists so the user shows up on the Community
 * page. Merge-write: never clobbers reading/favorites/game fields.
 *
 * Reviews key on display name (`{bookId}_{displayNameLower}` doc ids), and
 * profiles key on slugifyName(displayName) — both unchanged from v1, so a
 * returning Google account lands on exactly the docs it always had.
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

// ==================== UI Rendering ====================

const GOOGLE_SVG = '<svg width="16" height="16" viewBox="0 0 48 48" style="vertical-align:middle;margin-right:6px"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>';

/**
 * Render the identity bar: Google sign-in when logged out; name + logout when
 * logged in, plus a one-time Google upgrade button for legacy sessions.
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

function _googleButton(label) {
  const btn = document.createElement('button');
  btn.className = 'identity-bar__google-btn';
  btn.innerHTML = GOOGLE_SVG + label;
  return btn;
}

function _renderLoggedIn(containerEl, db, options, session) {
  const wrapper = document.createElement('div');
  wrapper.className = 'identity-bar identity-bar--logged-in';

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
  methodBadge.textContent = session.legacy ? 'Legacy' : 'Google';
  wrapper.appendChild(methodBadge);

  // Legacy v1 capture: the name works, but no live session backs it. Offer
  // the one-time upgrade rather than stranding or silently wiping it.
  if (session.legacy && options?.app) {
    const upgradeBtn = _googleButton('Sign in');
    upgradeBtn.title = 'The site now uses live Google sign-in. Sign in once to carry this account forward.';
    upgradeBtn.addEventListener('click', async () => {
      upgradeBtn.disabled = true;
      const result = await signInWithGoogle(options.app);
      renderIdentityBar(containerEl, db, options);
      if (result.success) options?.onAuthChange?.(getSession());
    });
    wrapper.appendChild(upgradeBtn);
  }

  const logoutBtn = document.createElement('button');
  logoutBtn.className = 'identity-bar__logout-btn';
  logoutBtn.textContent = 'Logout';
  logoutBtn.addEventListener('click', async () => {
    if (options?.app) {
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

  const row = document.createElement('div');
  row.style.cssText = 'display:flex;align-items:center;gap:10px;flex-wrap:wrap';

  const googleBtn = _googleButton('Sign in');
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
      googleBtn.innerHTML = GOOGLE_SVG + 'Sign in';
    }
  });
  row.appendChild(googleBtn);
  wrapper.appendChild(row);
  containerEl.appendChild(wrapper);
}

// ==================== Dev-lane test seam ====================

// A real Google popup cannot be driven by automation. On the DEV LANE ONLY
// (fb-env.js: /dev/ paths and localhost), expose a stub that writes the
// mirror as if a sign-in completed, so automated browser flows can exercise
// the signed-in UI. The 'stub' marker keeps the auth listener's null branch
// from wiping it, and getSession() treats it as live (legacy: false).
// This is presentation-only by construction — Firestore rules are shape-only
// (§4a) and never consult auth, so the stub grants nothing a devtools
// localStorage edit would not.
if (IS_DEV_LANE && typeof window !== 'undefined') {
  window.__abIdentityStub = {
    /** @param {{displayName?: string, email?: string, photoURL?: string}} user */
    signIn(user) {
      mirrorUser({
        displayName: user.displayName || '',
        email: user.email || '',
        photoURL: user.photoURL || '',
      }, 'stub');
    },
    signOut() { logout(); },
  };
}
