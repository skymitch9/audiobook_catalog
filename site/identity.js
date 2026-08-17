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
  // A different person may now be signed in — drop any cached admin answer so
  // the next resolveAdmin() asks site_roles about THIS uid. (Defined in the
  // admin section below; hoisted.)
  resetAdminCache();
  localStorage.setItem('ab_identity_name', user.displayName || user.email);
  localStorage.setItem('ab_identity_session', 'active');
  localStorage.setItem('ab_identity_method', 'google');
  localStorage.setItem('ab_identity_photo', user.photoURL || '');
  // ⚠️ Captured for DISPLAY only (and community.html's owner link). This is
  // no longer what any admin decision keys on: as of 2026-08-16 resolveAdmin()
  // reads the email off the live Firebase token instead, because anything in
  // here is editable from devtools and therefore proves nothing.
  localStorage.setItem('ab_identity_email', user.email || '');
  localStorage.setItem(LIVE_MARKER, marker || '1');
}

/**
 * Clear the mirror — the WHOLE ab_identity_* family, tagged or not, so a
 * sign-out always lands in a truly signed-out UI whatever residue any lane's
 * code left behind. Public as `logout()` for legacy sessions and tests.
 * Also drops the per-session estate-approval cache and the cached admin
 * answer: a signed-out (or revoked-and-signed-out) person must not keep a
 * cached "approved", nor a cached "admin".
 */
export function logout() {
  sweepMirror();
  clearEstateCache();
  resetAdminCache();
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
        helloEstate(user);
      } else if (localStorage.getItem(LIVE_MARKER) === '1') {
        logout();
      }
    });
  } catch (e) {
    console.warn('[Identity] auth mirror attach failed:', e);
  }
}

// ==================== Estate enrollment (the /hello pipe) ====================
//
// Incident 2026-08-15: someone signed up here and never appeared in the
// estate directory (heygabi.ai/admin). This site is static — unlike the
// library/games apps, whose Workers report sign-ins to the estate's /seen
// endpoint server-side, nothing here ever told the directory anyone existed;
// the 2026-08-14 migration was a one-time backfill wearing the pipe's
// clothes. This is the pipe: POST /api/estate/hello with the caller's own
// Firebase ID token. The Worker verifies the token and upserts the caller as
// pending-if-new (it can NEVER change an existing status — same single-
// statement guarantee as /seen), so an approver just sees newcomers appear.
//
// Fire-and-forget from the auth listener — sign-in, redirect completion and
// restored sessions all pass through it, so a person missed while this code
// was broken still enrolls on their next page load, no re-sign-in needed.
// Once per browser session per uid; the marker is only kept on a 2xx so a
// failed attempt retries on the next page load rather than silently never.

const ESTATE_HELLO_URL = 'https://auth.heygabi.ai/api/estate/hello';
const HELLO_MARK_PREFIX = 'ab_identity_estate_hello_'; // + uid, sessionStorage

function helloEstate(user) {
  try {
    if (!user || !user.uid) return;
    const key = HELLO_MARK_PREFIX + user.uid;
    if (sessionStorage.getItem(key)) return;
    sessionStorage.setItem(key, '1');
    user
      .getIdToken()
      .then((token) =>
        fetch(ESTATE_HELLO_URL, {
          method: 'POST',
          headers: { Authorization: 'Bearer ' + token },
        }),
      )
      .then((res) => {
        if (!res || !res.ok) sessionStorage.removeItem(key);
      })
      .catch(() => {
        try { sessionStorage.removeItem(key); } catch (e) { /* next session retries */ }
      });
  } catch (e) {
    /* storage or fetch unavailable — enrollment is best-effort by design */
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

// ==================== Admin — ONE source of truth ====================
//
// Admin is a ROLE, not a name. The answer comes from site_roles/{uid} — the
// very doc firestore.rules consults (see getSiteRole further down) — so what
// the UI offers and what the server allows agree by construction.
//
// ⚠️ What this replaced, 2026-08-16. isAdmin() used to be:
//
//     return ADMIN_EMAILS.includes(email) || ADMIN_NAMES.includes(name);
//
// where `name` was the Google DISPLAY NAME read out of the localStorage
// mirror, matched against a hardcoded ADMIN_NAMES = ['!Sky', 'Skylar'].
// Anyone could rename their Google profile to "Skylar" — or simply type the
// key into devtools — and every isAdmin() gate on the site opened for them.
//
// It was TRUE that this protected nothing at the time (rules were shape-only,
// the pipeline trigger is token-protected), and the docblock said so. That is
// exactly why it was dangerous: it was a loaded gun aimed at the NEXT feature.
// An author reaching for the obvious-looking isAdmin() would inherit a
// name-spoofable gate and nothing whatsoever would warn them. The list is gone.

/**
 * Owner break-glass — EMAIL ONLY, deliberately.
 *
 * Why it exists: an incident that empties, corrupts or mis-writes site_roles
 * must not lock the owner out of the admin page he needs in order to FIX
 * site_roles. This mirrors the estate pattern (owner email always wins) and
 * is the only reason a hardcoded value survives here.
 *
 * ⚠️ This is NOT a general "admins" list. Do not add anybody else. Roles are
 * granted at heygabi.ai/admin, or scripts/seed_site_admin.py as break-glass.
 *
 * ⚠️ EMAIL only, never a display name, and never an email out of the
 * localStorage mirror. resolveAdmin() reads it from getLiveUser(), i.e. from
 * a verified Google ID token that Firebase Auth stands behind. A display name
 * cannot appear here at all: Google lets anyone set theirs to any string, so
 * it identifies nobody. A mirror email is devtools-editable, so it proves
 * nothing either. Only the live token does.
 */
export const ADMIN_EMAILS = ['nbaslamking@gmail.com'];

/**
 * The cached answer. Starts false, and false is where every failure path
 * leaves it — "we could not tell" must never render as admin.
 */
let _adminAnswer = false;

/** One shared in-flight promise, so N gates on a page cost ONE Firestore read. */
let _adminInFlight = null;

/**
 * Forget the cached answer. Called whenever WHO is signed in may have changed
 * (sign-in, sign-out, account switch) so a previous person's admin answer can
 * never survive into the next person's session. Also drops the cached
 * /api/me site-access answer (see resolveSiteAccess below) — same reason.
 */
function resetAdminCache() {
  _adminAnswer = false;
  _adminInFlight = null;
  _accessInFlight = null;
}

/**
 * Resolve — once per page load — whether the live session is a site admin.
 *
 * The async half of the pair. Since Phase 2 of the auth migration the answer
 * comes from resolveSiteAccess() below — GET /api/me on the audiobook worker,
 * server-verified, with the old site_roles read as the outage fallback —
 * and "admin" means the §6 capability that defines it: removeAnyReview
 * (held by admin and owner rungs only). The owner break-glass inside
 * resolveSiteAccess still short-circuits on the live token's email.
 *
 * Never rejects. Every error path answers false — the gate stays closed.
 *
 * @param {import('firebase/firestore').Firestore} db
 * @param {import('firebase/app').FirebaseApp} app
 * @returns {Promise<boolean>}
 */
export function resolveAdmin(db, app) {
  if (_adminInFlight) return _adminInFlight;
  _adminInFlight = (async () => {
    try {
      const access = await resolveSiteAccess(db, app);
      return !!access && access.capabilities.indexOf('removeAnyReview') !== -1;
    } catch (e) {
      return false; // fail closed — no answer is not a yes
    }
  })().then((ok) => {
    _adminAnswer = ok;
    return ok;
  });
  return _adminInFlight;
}

/**
 * The admin answer, synchronously, from the cache resolveAdmin() filled.
 *
 * WHAT THIS IS: a read of the rules-enforced site_roles/{uid} role (plus the
 * owner break-glass above), fetched once per page load.
 *
 * WHAT THIS IS NOT:
 *   - NOT a list. Do not re-add ADMIN_NAMES, a testers array, or any other
 *     client-side roster. That was the bug this replaced; see the section
 *     header. If you want to grant somebody admin, grant them the ROLE.
 *   - NOT keyed on display name. Nothing in this module is, any more.
 *   - NOT an access control by itself. It decides what the UI SHOWS. What the
 *     server ALLOWS is decided by firestore.rules reading the same site_roles
 *     doc, and by the token guarding the pipeline trigger. Those are the
 *     enforcement; this exists so we do not offer a control that would be
 *     refused. Do keep it honest anyway — it is now the same answer.
 *   - NOT answerable before resolveAdmin() has settled.
 *
 * ⚠️ Returns FALSE until resolveAdmin(db, app) settles, on purpose. Two ways
 * to get this wrong were rejected:
 *   - defaulting to TRUE (or rendering first and hiding after) flashes admin
 *     controls at every visitor for the length of a Firestore read;
 *   - blocking the page on that read delays sign-in and first paint for
 *     everyone, to answer a question that concerns one person.
 * So: every admin-only control ships HIDDEN in its markup and is REVEALED on
 * resolve — never hidden after the fact. A caller who forgets to resolve gets
 * "not admin", which hides a control that should have shown: visible,
 * reportable, and harmless. The opposite default is not.
 *
 * @returns {boolean}
 */
export function isAdmin(...args) {
  if (args.length) {
    // The old signature was isAdmin(session). Anything still passing one is
    // reading a name-based answer that no longer exists — say so loudly
    // rather than silently ignoring the argument.
    console.warn(
      '[Identity] isAdmin() takes no arguments — admin is a role now, not a session name. '
      + 'Call resolveAdmin(db, app) first, then read isAdmin(). See its docblock.',
    );
  }
  return _adminAnswer;
}

/**
 * The reveal. Runs `show` iff the live session resolves to admin — the
 * standard shape for an admin-only control, whose markup ships hidden and
 * which this is the only thing that unhides. `show` never runs on a failure.
 *
 * @param {import('firebase/firestore').Firestore} db
 * @param {import('firebase/app').FirebaseApp} app
 * @param {() => void} show
 * @returns {Promise<void>}
 */
export function whenAdmin(db, app, show) {
  return resolveAdmin(db, app)
    .then((ok) => { if (ok) show(); })
    .catch(() => { /* fail closed — show nothing */ });
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

/**
 * A Firebase ID token for the live session, or `null` when there is not one.
 *
 * ⚠️ THIS EXISTS BECAUSE `getLiveUser()` DELIBERATELY DOES NOT RETURN A TOKEN
 * GETTER, and a caller that assumed otherwise shipped a broken page.
 *
 * `getLiveUser()` answers a flat SNAPSHOT — `{uid, email, displayName}` — on
 * purpose: it is presentation data, and handing the live Firebase `User` object
 * to every caller is how a page ends up minting credentials in places nobody
 * audits. Every INTERNAL caller here that needs a token uses the private
 * `liveUser()` instead.
 *
 * ⚠️ The reader page did not have that option, called `user.getIdToken()` on
 * the snapshot, and threw `TypeError: user.getIdToken is not a function` for
 * every signed-in reader — surfacing as *"The shelf did not answer"*, an
 * outage sentence for something that was not an outage. It was invisible to
 * every test and to every signed-out check, and it is exactly the gap the
 * viewer design named in advance: *"the reader page needs a token getter that
 * account-modal.js does not expose … exporting a getIdToken() from the
 * identity module is a small, additive change."* This is that change, made
 * after the bug it predicted (2026-08-17, viewer phase 2).
 *
 * `null` rather than a throw, because "not signed in" is a state the caller
 * words, not an error it handles.
 *
 * @param app the Firebase app
 * @param {boolean} force skip the SDK's cache and mint a fresh token. Use it
 *   at the START of a long read (a token lasts an hour); do NOT use it per
 *   request — unforced, the SDK returns its cached token and refreshes near
 *   expiry by itself, which is what keeps a long session alive cheaply.
 * @returns {Promise<string|null>}
 */
export async function getIdToken(app, force = false) {
  const user = await liveUser(app).catch(() => null);
  if (!user || typeof user.getIdToken !== 'function') return null;
  try {
    return await user.getIdToken(force);
  } catch (e) {
    return null;
  }
}

// ==================== Site roles (rules-enforced admin) ====================
//
// site_roles/{uid} is the site's first REAL role: written only server-side
// (scripts/seed_site_admin.py via the service account; browsers are denied
// all writes), readable only as your own doc (rules: doc id must equal
// request.auth.uid; no listing). A site_roles answer is enforced end to end:
// the same doc gates club-manager writes inside firestore.rules.
//
// Since 2026-08-16 this is also what isAdmin() answers from — see the admin
// section above. There is no longer a second, spoofable notion of "admin".
//
// ⚠️ The collection is UNSUFFIXED on both lanes (like pipeline_*): a role
// belongs to the person, not the data lane. Do not wrap it in col().

/**
 * The signed-in person's own site-role doc, or null (signed out, legacy
 * session, no role granted, or the read failed). Never throws.
 * @returns {Promise<{uid: string, role: string}|null>}
 */
export async function getSiteRole(db, app) {
  return roleForUser(db, await liveUser(app).catch(() => null));
}

/**
 * The ONE implementation of "read this user's site_roles doc". Split out of
 * getSiteRole so resolveAdmin() can reuse it with a live user it has already
 * waited for, instead of waiting on auth state a second time or growing a
 * second copy of this read. Never throws; every failure is "no role".
 *
 * @param {import('firebase/firestore').Firestore} db
 * @param {{uid: string}|null} user
 * @returns {Promise<{uid: string, role: string}|null>}
 */
async function roleForUser(db, user) {
  try {
    if (!db || !user || !user.uid) return null;
    const snap = await getDoc(doc(db, 'site_roles', user.uid));
    if (!snap.exists()) return null;
    return { uid: user.uid, ...snap.data() };
  } catch (e) {
    return null; // no answer is "no role" — the gate stays closed
  }
}

/**
 * Is the signed-in person the rules-enforced site admin? The pure role
 * question, with no owner break-glass.
 *
 * ⚠️ For a UI GATE use resolveAdmin()/isAdmin() instead — it wraps this one,
 * adds the owner break-glass, and caches the answer so N gates on a page cost
 * one read. Use this directly only when you specifically mean "does the
 * site_roles doc say admin", ignoring break-glass.
 */
export async function isSiteAdmin(db, app) {
  const role = await getSiteRole(db, app);
  return !!role && role.role === 'admin';
}

/**
 * Is the signed-in person the rules-enforced site MODERATOR (three-tier
 * model, 2026-08-14)? Moderators get the operational subset across all
 * clubs — schedule, polls, next meeting, membership ops, content deletes —
 * never structural settings or site-wide powers.
 */
export async function isSiteModerator(db, app) {
  const role = await getSiteRole(db, app);
  return !!role && role.role === 'moderator';
}

// ==================== Site access (worker-verified /api/me) ====================
//
// Phase 2 of the auth migration (catalog-platform
// docs/info/audiobook-auth-migration.md): the UI's role answer comes from
// the audiobook worker's GET /api/me — the token is verified SERVER-side,
// the estate directory is consulted, and the reply names the caller's
// ladder role and §6 capabilities. This is presentation only: what the UI
// RENDERS. Enforcement stays where it is (firestore.rules today, worker
// endpoints from Phase 3), and every control still ships hidden in markup
// and is only ever REVEALED on resolve — the resolveAdmin() pattern.
//
// ⚠️ FALLBACK, deliberate: if /api/me is UNREACHABLE or answers with an
// outage (5xx — e.g. the worker's role store not answering), this falls
// back to the pre-Phase-2 behaviour — the owner break-glass plus the
// site_roles/{uid} own-doc read — because a worker outage must not lock
// the owner out of admin.html (the page he'd need to fix things from).
// A definitive worker refusal (401/403: the token did not verify) is NOT
// an outage and fails closed as guest.

export const SITE_ME_URL = 'https://audiobook-api.heygabi.ai/api/me';

/**
 * The §6 capabilities the UI consults, by role, for the FALLBACK path only —
 * when /api/me cannot answer, the stored site_roles role maps to the same
 * capability names the worker would have answered with. Kept to the four
 * capabilities any page actually renders on; the worker's live answer is
 * always the complete list.
 */
const FALLBACK_CAPABILITIES = {
  admin: ['operateClub', 'manageClub', 'administerClub', 'removeAnyReview'],
  moderator: ['operateClub'],
};

/** One in-flight/settled promise per page load (the resolveAdmin idiom);
 * reset alongside the admin cache whenever WHO is signed in may have changed. */
let _accessInFlight = null;

/**
 * Resolve — once per page load — the signed-in person's site access:
 * `{ role, capabilities, source }`, or null when there is no live session
 * (signed out, legacy, stub — nothing verifiable to ask about). Never
 * rejects. `source` says where the answer came from: 'worker' (server-
 * verified /api/me), 'fallback' (site_roles read during an outage) or
 * 'break-glass' (the owner email on the live token).
 *
 * @param {import('firebase/firestore').Firestore} db
 * @param {import('firebase/app').FirebaseApp} app
 * @returns {Promise<{role: string, capabilities: string[], source: string}|null>}
 */
export function resolveSiteAccess(db, app) {
  if (_accessInFlight) return _accessInFlight;
  _accessInFlight = (async () => {
    const user = await liveUser(app).catch(() => null);
    if (!user || !user.uid) return null;

    // Owner break-glass — unchanged from resolveAdmin's original contract:
    // an incident that empties site_roles, or misconfigures the worker,
    // must not lock the owner out of the page he'd fix it from. EMAIL from
    // the live token only; see ADMIN_EMAILS above.
    if (ADMIN_EMAILS.includes((user.email || '').trim().toLowerCase())) {
      return { role: 'admin', capabilities: FALLBACK_CAPABILITIES.admin, source: 'break-glass' };
    }

    try {
      const token = await user.getIdToken();
      const res = await fetch(SITE_ME_URL, { headers: { Authorization: 'Bearer ' + token } });
      if (res.ok) {
        const me = await res.json();
        if (me && typeof me.role === 'string' && Array.isArray(me.capabilities)) {
          return { role: me.role, capabilities: me.capabilities, source: 'worker' };
        }
        // A 200 with an unrecognisable body is a worker bug — treat as outage.
      } else if (res.status === 401 || res.status === 403) {
        // Definitive, server-verified refusal — fail closed, no fallback:
        // a token the worker cannot verify would not survive enforcement
        // either, and "we could not tell" must never render as a role.
        return { role: 'guest', capabilities: [], source: 'worker' };
      }
      // Any other status (502 role-store outage, 503, 500) falls through.
    } catch (e) {
      // fetch unavailable / network down / CORS — fall through to fallback.
    }

    // The pre-Phase-2 behaviour, verbatim: the own site_roles doc via the
    // ONE roleForUser implementation. An outage answers what the rules
    // still enforce, so UI and enforcement stay in step even offline.
    const stored = await roleForUser(db, user);
    const role = stored && FALLBACK_CAPABILITIES[stored.role] ? stored.role : 'guest';
    return {
      role,
      capabilities: FALLBACK_CAPABILITIES[role] || [],
      source: 'fallback',
    };
  })();
  return _accessInFlight;
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
      if (cached) {
        // TTL'd since 2026-08-15 (owner: role grants should not need a hard
        // refresh): entries are {v, at} and expire after 10 minutes, so an
        // approval or role granted mid-session shows up on the next page
        // load within minutes, not the next browser session. A legacy
        // bare-answer entry has no `at` and reads as expired.
        const parsed = JSON.parse(cached);
        if (parsed && parsed.at && Date.now() - parsed.at < 10 * 60 * 1000) return parsed.v;
      }
    } catch (e) { /* unreadable cache — fall through to a fresh fetch */ }
    const token = await user.getIdToken();
    const res = await fetch(ESTATE_ME_URL, { headers: { Authorization: 'Bearer ' + token } });
    if (!res.ok) return null; // a failure is not an answer — do not cache it
    const answer = await res.json();
    try { sessionStorage.setItem(cacheKey, JSON.stringify({ v: answer, at: Date.now() })); } catch (e) { /* full — refetch next time */ }
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
//
// ⚠️ Since 2026-08-16 the stub explicitly does NOT confer admin, whatever
// name or email it is handed. resolveAdmin() starts from getLiveUser(), and a
// stub mirror has no live Firebase user behind it, so it resolves false. That
// is correct: automation can exercise the signed-in UI, but the admin surfaces
// need a real signed-in account holding a real site_roles role.
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
