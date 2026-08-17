// estate-search-mount.js — the audiobook site's wiring for <estate-search>,
// the ONE shared cross-catalog search component (vendored copy:
// site/estate/estate-search.js; provenance and refresh procedure in
// site/estate/SOURCE-estate-search.txt). ES module, browser-native, no build
// step — the same shape as identity.js.
//
// ⚠️ This does NOT replace the catalog's own search box (#ab-search).
// That box filters THIS catalog, client-side, with the sort/pagination/TBR
// machinery around it. The estate box asks the other question — "do we own
// this on ANY shelf?" — against the shared index Worker at index.heygabi.ai,
// which also reaches the library books and the board games. Two boxes
// because they are two questions; this one lives folded away so it never
// competes for the catalog box's keystrokes.
//
// ⚠️ NO SECOND FIREBASE APP — the load-bearing rule of this file.
// identity.js already initialises Firebase for the page (index.html's module
// script calls initializeApp once and passes the app around). Left alone,
// <estate-search auth="authed"> would dynamically import a sibling
// estate-auth.js and let IT create a Firebase app — two sign-in states free
// to disagree. Both React consumers (library, games) solved this by handing
// the component an .authAdapter built over the app the page already has;
// this file is that same adapter, plain-page flavoured. Accordingly:
//   - this module NEVER imports firebase-app.js and NEVER calls
//     initializeApp — it only wraps the `app` instance it is handed;
//   - estate-auth.js is deliberately NOT vendored, so a regression that
//     drops the adapter fails loudly (the component logs a warning and
//     degrades to authless) instead of silently booting a second app.
//
// ⚠️ Property-before-append is load-bearing (the games embed's finding,
// Board_Game_Catalog components/EstateSearch.tsx): the component's
// connectedCallback boots auth immediately, and an upgrade CLOBBERS any
// pre-upgrade own property (`this.authAdapter = null` in the constructor).
// So the element is created programmatically after the module is imported
// and the adapter is set between createElement and appendChild — the only
// window that is provably after upgrade eligibility and before
// connectedCallback. Do not move the tag into static HTML.

import { signInWithGoogle, signOutGoogle } from './identity.js';
import { getAuth, onAuthStateChanged } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js';

/** Where the vendored component lives, relative to this module (site root). */
const COMPONENT_PATH = './estate/estate-search.js';

/**
 * The estate-auth surface <estate-search> expects, built over THIS page's
 * existing Firebase app. Shape per the component's header: watchAuth,
 * idToken, signIn, signOutUser — and NO handleRedirectResult, deliberately:
 * index.html already calls identity.js's handleRedirectResult(app) on load
 * (it must — see its warning comment), and a second getRedirectResult()
 * racing it is a bug looking for somewhere to happen. The component guards
 * on `typeof adapter.handleRedirectResult === 'function'`, so absence is a
 * supported configuration, not a gap.
 *
 * @param {import('firebase/app').FirebaseApp} app — the page's ONE app.
 */
export function buildEstateAuthAdapter(app) {
  return {
    watchAuth(cb) {
      // Normalise undefined → null: the component compares against null.
      return onAuthStateChanged(getAuth(app), (user) => cb(user || null));
    },

    async idToken() {
      const user = getAuth(app).currentUser;
      return user ? user.getIdToken() : null;
    },

    /**
     * identity.js's signInWithGoogle, translated to the component's result
     * vocabulary ({ ok | cancelled | redirecting | error }). The mapping
     * keys on identity.js's own literal strings — 'Sign-in cancelled.' and
     * 'Redirecting...' — which its tests pin; if those strings ever change,
     * the tests here fail alongside.
     */
    async signIn() {
      const r = await signInWithGoogle(app);
      if (r && r.success) return { ok: true };
      const msg = (r && r.error) || '';
      if (msg === 'Sign-in cancelled.') return { cancelled: true };
      if (msg === 'Redirecting...') return { redirecting: true };
      return { error: msg || 'Sign-in failed. Try again.' };
    },

    async signOutUser() {
      await signOutGoogle(app);
    },
  };
}

/**
 * Decide how an `estate-search:select` should route, purely from its detail.
 * Returns:
 *   { kind: 'hash', hash }  — an audiobook hit: stay in-page. The pusher
 *       (app/index_push.py detail_url_for) mints audiobook detail URLs as
 *       '<site>/#q=<title>' — the site's own hash idiom (_writeHash /
 *       _parseHash in index.html), so the in-page route is simply adopting
 *       the URL's own hash: assigning location.hash fires the page's
 *       hashchange listener, which fills #ab-search and re-renders.
 *   { kind: 'page', href }  — an ebook hit (same 'audiobook' source, per
 *       index_push.py's ebook rows): detail_url is '<site>/ebooks.html',
 *       which has no hash anchor. Routed same-tab via a RELATIVE href so
 *       the /dev/ lane stays on the /dev/ lane (the pushed URL is always
 *       the prod absolute).
 *   null — anything else (library/game/apex hits, unparseable URLs): leave
 *       the component's default window.open(url, '_blank') alone; those
 *       live on other origins and a new tab is the right answer.
 *
 * @param {string|null|undefined} url  detail.url off the event
 * @param {{source?: string}|null|undefined} hit  detail.hit off the event
 */
export function routeForSelect(url, hit) {
  if (!url || !hit || hit.source !== 'audiobook') return null;
  let parsed;
  try {
    parsed = new URL(url);
  } catch (e) {
    return null; // not swallowed — the component keeps its default
  }
  const sp = new URLSearchParams((parsed.hash || '').replace(/^#/, ''));
  const q = sp.get('q');
  if (q) return { kind: 'hash', hash: '#' + sp.toString() };
  if (/\/ebooks\.html$/.test(parsed.pathname)) return { kind: 'page', href: 'ebooks.html' };
  return null;
}

/**
 * Import the vendored component (once), create the element, wire it, append
 * it into `host`. Called lazily — on the fold's first open, not at page
 * load — because the component is ~78KB most visits never need.
 *
 * @param {{ app: import('firebase/app').FirebaseApp|null, host: HTMLElement }} opts
 *   `app` may be null (Firebase init failed): the box then mounts authless
 *   and still searches the public audiobook slice — degraded, never dead.
 * @returns {Promise<HTMLElement>} the mounted element.
 */
export async function mountEstateSearch({ app, host }) {
  await import(COMPONENT_PATH);
  await customElements.whenDefined('estate-search');

  const el = document.createElement('estate-search');
  if (app) {
    el.setAttribute('auth', 'authed');
    // ⚠️ BEFORE appendChild — see the property-before-append note above.
    el.authAdapter = buildEstateAuthAdapter(app);
  }
  // No `scan` attribute: estate-scan.js is not vendored (see
  // SOURCE-estate-search.txt) and scanning is not this page's surface.
  el.setAttribute(
    'hint',
    'Checks the library books and board games too — the search box above covers only this catalog. ' +
    'Anyone can search the audiobooks; sign in to search every shelf.',
  );

  el.addEventListener('estate-search:select', (event) => {
    const detail = event.detail || {};
    const route = routeForSelect(detail.url, detail.hit);
    if (!route) return; // another catalog — the component opens its tab
    event.preventDefault(); // cancelable by contract; this is the in-page hand-off
    if (route.kind === 'hash') {
      // Adopting the hash fires index.html's hashchange listener, which
      // fills #ab-search and re-renders — the same path a pasted #q= link
      // takes. If the hash is already set, the results already match it.
      if (location.hash !== route.hash) location.hash = route.hash;
      const controls = document.getElementById('controls');
      if (controls && typeof controls.scrollIntoView === 'function') {
        controls.scrollIntoView({ behavior: 'smooth' });
      }
    } else {
      location.href = route.href;
    }
  });

  host.appendChild(el);
  return el;
}
