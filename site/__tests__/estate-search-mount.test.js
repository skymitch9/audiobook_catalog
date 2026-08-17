// @vitest-environment jsdom
// @vitest-environment-options { "url": "https://audiobooks.heygabi.ai/" }
//
// Feature: the <estate-search> embed (2026-08-17) — the shared cross-catalog
// search component, vendored at site/estate/estate-search.js and wired by
// site/estate-search-mount.js.
//
// What these tests pin:
//   - the auth adapter has the estate-auth SHAPE (watchAuth / idToken /
//     signIn / signOutUser) and — load-bearing — NO handleRedirectResult:
//     index.html already collects the redirect result via identity.js, and a
//     second getRedirectResult() would race it (the games embed's finding);
//   - the adapter WRAPS the page's one Firebase app: it never calls
//     initializeApp, and mounting boots the component's auth through the
//     adapter (onAuthStateChanged on the page's app), never through a
//     dynamically imported estate-auth.js with a second app behind it;
//   - signIn translates identity.js's result vocabulary into the
//     component's ({ ok | cancelled | redirecting | error });
//   - estate-search:select routing: audiobook hits stay in-page via the
//     site's own #q= hash idiom, ebook hits go same-tab to ebooks.html via a
//     RELATIVE href (lane-preserving), everything else keeps the component's
//     default new tab.

import { describe, it, expect, beforeEach, vi } from 'vitest';

// --- Controllable firebase/auth mock (the gstatic URL is aliased to
// 'firebase/auth' in vitest.config.js, same as identity.test.js) ---
let authCallback = null;
const mockAuth = { currentUser: null };

vi.mock('firebase/auth', () => ({
  getAuth: () => mockAuth,
  onAuthStateChanged: (auth, cb) => {
    authCallback = cb;
    return () => { authCallback = null; };
  },
}));

// --- identity.js mock: the adapter must DELEGATE sign-in/out to the page's
// one implementation, so the mock is the assertion surface ---
const signInWithGoogleMock = vi.fn();
const signOutGoogleMock = vi.fn(async () => {});

vi.mock('../identity.js', () => ({
  signInWithGoogle: (...args) => signInWithGoogleMock(...args),
  signOutGoogle: (...args) => signOutGoogleMock(...args),
}));

import { buildEstateAuthAdapter, routeForSelect, mountEstateSearch } from '../estate-search-mount.js';

const app = { name: 'the-page-app' };

beforeEach(() => {
  document.body.innerHTML = '';
  authCallback = null;
  mockAuth.currentUser = null;
  signInWithGoogleMock.mockReset();
  signOutGoogleMock.mockClear();
  // Reset the hash without triggering jsdom's unimplemented navigation.
  history.replaceState(null, '', 'https://audiobooks.heygabi.ai/');
});

describe('buildEstateAuthAdapter — the estate-auth surface over the page app', () => {
  it('has exactly the shape the component expects, with NO handleRedirectResult', () => {
    const adapter = buildEstateAuthAdapter(app);
    expect(typeof adapter.watchAuth).toBe('function');
    expect(typeof adapter.idToken).toBe('function');
    expect(typeof adapter.signIn).toBe('function');
    expect(typeof adapter.signOutUser).toBe('function');
    // Deliberate absence — the component guards on typeof, and index.html
    // already calls identity.js's handleRedirectResult(app). A second
    // getRedirectResult() racing it is the bug this pins against.
    expect('handleRedirectResult' in adapter).toBe(false);
  });

  it('watchAuth subscribes on the page app and normalises falsy users to null', () => {
    const adapter = buildEstateAuthAdapter(app);
    const seen = [];
    adapter.watchAuth((u) => seen.push(u));
    expect(typeof authCallback).toBe('function');
    const user = { uid: 'u1' };
    authCallback(user);
    authCallback(undefined); // Firebase says "no user" as undefined sometimes
    expect(seen).toEqual([user, null]);
  });

  it('idToken answers null signed out, and the live token signed in', async () => {
    const adapter = buildEstateAuthAdapter(app);
    expect(await adapter.idToken()).toBeNull();
    mockAuth.currentUser = { getIdToken: async () => 'tok-123' };
    expect(await adapter.idToken()).toBe('tok-123');
  });

  it('signIn maps identity.js success to { ok: true }', async () => {
    signInWithGoogleMock.mockResolvedValue({ success: true, displayName: 'Sky' });
    const adapter = buildEstateAuthAdapter(app);
    expect(await adapter.signIn()).toEqual({ ok: true });
    expect(signInWithGoogleMock).toHaveBeenCalledWith(app);
  });

  it('signIn maps a cancelled popup to { cancelled: true }, not an error', async () => {
    signInWithGoogleMock.mockResolvedValue({ success: false, error: 'Sign-in cancelled.' });
    const adapter = buildEstateAuthAdapter(app);
    expect(await adapter.signIn()).toEqual({ cancelled: true });
  });

  it('signIn maps the redirect leg to { redirecting: true }', async () => {
    signInWithGoogleMock.mockResolvedValue({ success: false, error: 'Redirecting...' });
    const adapter = buildEstateAuthAdapter(app);
    expect(await adapter.signIn()).toEqual({ redirecting: true });
  });

  it('signIn passes a real failure through as { error }', async () => {
    signInWithGoogleMock.mockResolvedValue({ success: false, error: 'Sign-in failed. Try again.' });
    const adapter = buildEstateAuthAdapter(app);
    expect(await adapter.signIn()).toEqual({ error: 'Sign-in failed. Try again.' });
  });

  it('signOutUser delegates to identity.js with the page app', async () => {
    const adapter = buildEstateAuthAdapter(app);
    await adapter.signOutUser();
    expect(signOutGoogleMock).toHaveBeenCalledWith(app);
  });
});

describe('routeForSelect — where a hit goes', () => {
  it('routes an audiobook #q= hit in-page, keeping the URL-encoded hash', () => {
    // detail_url_for (app/index_push.py) mints '<site>/#q=<title>' with
    // urlencode, space → '+' — the same serialisation URLSearchParams uses,
    // so the hash survives the round trip unchanged.
    const route = routeForSelect(
      'https://audiobooks.heygabi.ai/#q=Project+Hail+Mary',
      { source: 'audiobook' },
    );
    expect(route).toEqual({ kind: 'hash', hash: '#q=Project+Hail+Mary' });
  });

  it('routes an ebook hit (same audiobook source) to a RELATIVE ebooks.html', () => {
    // Relative on purpose: the pushed URL is always the prod absolute, and a
    // /dev/-lane page must stay on the /dev/ lane.
    const route = routeForSelect(
      'https://audiobooks.heygabi.ai/ebooks.html',
      { source: 'audiobook', format: 'ebook' },
    );
    expect(route).toEqual({ kind: 'page', href: 'ebooks.html' });
  });

  it('leaves other catalogs to the component default (new tab)', () => {
    expect(routeForSelect('https://library.heygabi.ai/work/42', { source: 'library' })).toBeNull();
    expect(routeForSelect('https://boardgames.heygabi.ai/items/7', { source: 'game' })).toBeNull();
  });

  it('hands back unusable input rather than swallowing it', () => {
    expect(routeForSelect(null, { source: 'audiobook' })).toBeNull();
    expect(routeForSelect('not a url', { source: 'audiobook' })).toBeNull();
    expect(routeForSelect('https://audiobooks.heygabi.ai/', null)).toBeNull();
    // An audiobook URL with neither a #q hash nor an ebooks.html path.
    expect(routeForSelect('https://audiobooks.heygabi.ai/stats.html', { source: 'audiobook' })).toBeNull();
  });
});

describe('mountEstateSearch — the vendored component, wired for real', () => {
  async function mount() {
    document.body.innerHTML = '<div id="controls"></div><div id="host"></div>';
    return mountEstateSearch({ app, host: document.getElementById('host') });
  }

  it('defines the element, mounts authed, and boots auth through OUR adapter (no second Firebase app)', async () => {
    const el = await mount();
    expect(customElements.get('estate-search')).toBeTruthy();
    expect(el.getAttribute('auth')).toBe('authed');
    expect(typeof el.authAdapter?.watchAuth).toBe('function');
    expect('handleRedirectResult' in el.authAdapter).toBe(false);
    // The proof of "no second app": connectedCallback booted auth and, an
    // adapter being present, subscribed via onAuthStateChanged on the PAGE'S
    // app (our mock captured the callback) instead of dynamically importing
    // estate-auth.js — which is not vendored, precisely so this path breaks
    // loudly if the adapter ever goes missing.
    expect(typeof authCallback).toBe('function');
  });

  it('renders the signed-in state when the page app publishes a user', async () => {
    const el = await mount();
    authCallback({ displayName: 'Sky', email: 's@example.com', getIdToken: async () => 't' });
    const who = el.shadowRoot.querySelector('.es-who');
    expect(who.hidden).toBe(false);
    expect(who.textContent).toContain('Sky');
  });

  it('cancels the default and adopts the #q= hash for an audiobook hit', async () => {
    const el = await mount();
    const evt = new CustomEvent('estate-search:select', {
      cancelable: true, bubbles: true, composed: true,
      detail: { url: 'https://audiobooks.heygabi.ai/#q=Dune', hit: { source: 'audiobook' } },
    });
    el.dispatchEvent(evt);
    expect(evt.defaultPrevented).toBe(true);
    expect(location.hash).toBe('#q=Dune');
  });

  it('does NOT cancel a hit from another catalog — the component keeps its tab', async () => {
    const el = await mount();
    const evt = new CustomEvent('estate-search:select', {
      cancelable: true, bubbles: true, composed: true,
      detail: { url: 'https://library.heygabi.ai/work/42', hit: { source: 'library' } },
    });
    el.dispatchEvent(evt);
    expect(evt.defaultPrevented).toBe(false);
  });

  it('mounts authless (no adapter, no auth attribute) when Firebase init failed', async () => {
    document.body.innerHTML = '<div id="host"></div>';
    const el = await mountEstateSearch({ app: null, host: document.getElementById('host') });
    expect(el.hasAttribute('auth')).toBe(false);
    expect(el.authAdapter).toBeNull(); // the component's own constructor default
    // Authless resolves immediately: the input is usable, no sign-in button.
    expect(el.shadowRoot.querySelector('.es-input').disabled).toBe(false);
    expect(el.shadowRoot.querySelector('.es-signin').hidden).toBe(true);
  });
});
