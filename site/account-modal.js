// account-modal.js — the full account experience from the catalog page,
// mountable on any page. ES module, browser-native (no build step).
//
// Renders a Sign In / user button into a container; clicking opens the same
// modal as index.html: Google sign-in when logged out; profile (photo, name,
// review count + avg, game stats, dark-mode pref, currently reading,
// favorite books) plus logout when logged in. Catalog search comes from
// catalog.csv (index.html scrapes its own table instead; same data).

import { doc, getDoc, setDoc, collection, getDocs, serverTimestamp } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { getSession, signInWithGoogle, signOutGoogle, whenAdmin, handleRedirectResult, renderDevSiteLink } from './identity.js';
import { coverUrl } from './covers-base.js';
import { col } from './fb-env.js';
import { loadCatalogBooks } from './club-reads.js';

const GOOGLE_SVG = '<svg width="18" height="18" viewBox="0 0 48 48" style="vertical-align:middle;margin-right:8px"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>';

const CSS = `
#account-modal{
  position:fixed;inset:0;display:none;align-items:flex-start;justify-content:center;
  background:rgba(0,0,0,.85);z-index:9999;padding:40px 20px 20px;backdrop-filter:blur(4px);
  overflow-y:auto;
}
#account-modal.open{display:flex}
.account-modal-panel{
  background:var(--bg-2,#12121f);border:1px solid var(--neon-cyan,#05d9e8);
  width:min(420px,92vw);padding:28px;position:relative;color:var(--text,#e8e6e3);
  clip-path:polygon(0 0,calc(100% - 16px) 0,100% 16px,100% 100%,16px 100%,0 calc(100% - 16px));
}
.account-modal-close{
  position:absolute;top:10px;right:10px;background:none;border:1px solid var(--neon-magenta,#ff2a6d);
  color:var(--neon-magenta,#ff2a6d);width:28px;height:28px;cursor:pointer;font-weight:bold;
}
.account-modal-close:hover{background:var(--neon-magenta,#ff2a6d);color:#fff}
#account-btn{
  display:inline-flex;align-items:center;padding:8px 14px;border:1px solid var(--border,#2a2a3a);
  background:var(--bg-2,#12121f);color:var(--text,#e8e6e3);font-weight:700;font-size:.85em;
  cursor:pointer;transition:all .2s;font-family:inherit;white-space:nowrap;
}
#account-btn:hover{border-color:var(--neon-cyan,#05d9e8)}
.am-switch{display:inline-flex;align-items:center;gap:8px;user-select:none;cursor:pointer}
.am-switch input{display:none}
.am-switch .track{
  width:46px;height:26px;border-radius:4px;position:relative;
  background:var(--border,#2a2a3a);transition:background .2s;border:1px solid var(--border,#2a2a3a);
}
.am-switch .thumb{
  width:20px;height:20px;border-radius:2px;position:absolute;top:2px;left:3px;
  background:var(--muted,#8a8f98);transition:left .2s,background .2s;
}
.am-switch input:checked + .track .thumb{left:23px;background:var(--neon-cyan,#05d9e8);box-shadow:var(--et-glow)}
.am-switch input:checked + .track{background:var(--bg-2,#12121f);border-color:var(--neon-cyan,#05d9e8)}
.am-stat{background:var(--bg,#0a0a12);border:1px solid var(--border,#2a2a3a);padding:10px;text-align:center}
.am-stat .v{font-size:1.4em;font-weight:700;color:var(--neon-yellow,#fcee0a);font-family:var(--et-font-mono,monospace)}
.am-stat .l{font-size:.7em;color:var(--muted,#8a8f98);text-transform:var(--et-title-case)}
.am-input{
  width:100%;padding:6px 8px;border:1px solid var(--border,#2a2a3a);background:var(--bg,#0a0a12);
  color:var(--text,#e8e6e3);font-size:.85em;font-family:inherit;
}
.am-results{background:var(--bg-2,#12121f);border:1px solid var(--border,#2a2a3a);max-height:200px;overflow-y:auto;display:none}
.am-section-title{font-size:.8em;color:var(--neon-cyan,#05d9e8);text-transform:var(--et-title-case);letter-spacing:.5px;margin-bottom:8px}
.am-select{padding:6px 8px;border:1px solid var(--border,#2a2a3a);background:var(--bg,#0a0a12);color:var(--text,#e8e6e3);font-family:inherit;font-size:.85em;cursor:pointer}
.am-seg{display:inline-flex;border:1px solid var(--border,#2a2a3a)}
.am-seg button{padding:6px 12px;border:none;background:var(--bg,#0a0a12);color:var(--muted,#8a8f98);font-family:inherit;font-size:.8em;text-transform:var(--et-title-case,uppercase);letter-spacing:.5px;cursor:pointer}
.am-seg button + button{border-left:1px solid var(--border,#2a2a3a)}
.am-seg button.on{background:var(--neon-cyan,#05d9e8);color:#04121a;font-weight:700}
`;

// Appearance controls — the estate cog's options, modal-native (owner,
// 2026-08-14: the floating cog leaves the pages; theme + mode live HERE,
// replacing the old dark-mode switch). Everything drives window.estateTheme
// (static/js/theme.js). Theme choice is SITE-WIDE — one look per site
// (owner clarification 2026-08-14); the per-page scope row and its
// "apply to all pages" lever left with the canonical revert.
//
// ⚠️ THE THEME LIST IS READ FROM THE SWITCHER, NEVER WRITTEN DOWN HERE (owner
// order 2026-08-17: "when a theme is added all sites get it"). That was already
// true of the list; the FALLBACK below had gone stale (four themes, missing
// `hearts`), and so had the label logic. Both now come from window.estateTheme,
// which is installed by a synchronous <head> script on every page that loads
// this modal — the fallback is for a load where that script 404'd, and its only
// job is to look sane, since setTheme() cannot work without the switcher anyway.
function appearanceHTML() {
  const et = window.estateTheme;
  const themes = (et && et.themes && et.themes.length) ? et.themes
    : ['classic', 'apple', 'cyberpunk', 'retro', 'hearts'];
  const label = (t) => (et && typeof et.label === 'function')
    ? et.label(t)
    : t.charAt(0).toUpperCase() + t.slice(1);
  const options = themes.map(t =>
    `<option value="${t}">${label(t)}</option>`).join('');
  return `
      <div style="padding:10px 0;border-top:1px solid var(--border,#2a2a3a);margin-top:8px">
        <div class="am-section-title">Appearance</div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px">
          <span style="font-size:.85em;color:var(--muted,#8a8f98)">Theme</span>
          <select id="am-theme-select" class="am-select" aria-label="Theme">${options}</select>
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px">
          <span style="font-size:.85em;color:var(--muted,#8a8f98)">Mode</span>
          <div class="am-seg" role="group" aria-label="Light or dark mode">
            <button type="button" data-am-mode="auto">Auto</button>
            <button type="button" data-am-mode="light">Light</button>
            <button type="button" data-am-mode="dark">Dark</button>
          </div>
        </div>
      </div>`;
}

function wireAppearance(container) {
  const et = window.estateTheme;
  const sel = container.querySelector('#am-theme-select');
  const seg = container.querySelectorAll('[data-am-mode]');

  function sync() {
    if (!et) return;
    const s = et.get();
    if (sel) sel.value = s.theme;
    seg.forEach(b => {
      const on = b.getAttribute('data-am-mode') === s.mode;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  if (sel) sel.addEventListener('change', () => { if (et) et.setTheme(sel.value); });
  seg.forEach(b => b.addEventListener('click', () => { if (et) et.setMode(b.getAttribute('data-am-mode')); }));

  // One document-level listener at a time — re-renders must not accumulate.
  if (wireAppearance._sync) document.removeEventListener('hg-themechange', wireAppearance._sync);
  wireAppearance._sync = sync;
  document.addEventListener('hg-themechange', sync);
  sync();
}

export function mountAccountModal(db, app, containerEl) {
  if (!containerEl) return;
  // Handle redirect result from localhost Google SSO
  handleRedirectResult(app);
  if (!document.getElementById('account-modal-css')) {
    const style = document.createElement('style');
    style.id = 'account-modal-css';
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  const btn = document.createElement('button');
  btn.id = 'account-btn';
  containerEl.innerHTML = '';
  containerEl.appendChild(btn);

  let modal = document.getElementById('account-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'account-modal';
    modal.innerHTML = `
      <div class="account-modal-panel">
        <button class="account-modal-close" id="account-modal-close">X</button>
        <div id="account-modal-content"></div>
      </div>`;
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => { if (e.target === modal) modal.classList.remove('open'); });
    modal.querySelector('#account-modal-close').addEventListener('click', () => modal.classList.remove('open'));
  }

  function updateBtn() {
    const session = getSession();
    if (session) {
      const photo = session.photoURL
        ? `<img src="${session.photoURL}" style="width:20px;height:20px;border-radius:50%;vertical-align:middle;margin-right:6px">` : '';
      btn.innerHTML = `${photo}${session.displayName}`;
      btn.style.color = 'var(--neon-cyan, #05d9e8)';
    } else {
      btn.textContent = 'Sign In';
      btn.style.color = '';
    }
  }
  updateBtn();

  btn.addEventListener('click', () => {
    const content = modal.querySelector('#account-modal-content');
    const session = getSession();
    if (session) renderLoggedIn(content, session);
    else renderLoggedOut(content);
    modal.classList.add('open');
  });

  function renderLoggedOut(container) {
    container.innerHTML = `
      <div style="padding:20px 0;text-align:center">
        <h3 style="color:var(--neon-yellow,#fcee0a);text-transform:var(--et-title-case);letter-spacing:1px;margin:0 0 16px">Sign In</h3>
        <button id="am-google-btn" style="width:100%;justify-content:center;padding:12px 16px;font-size:.95em;display:inline-flex;align-items:center;border:1px solid var(--border,#2a2a3a);background:var(--bg,#0a0a12);color:var(--text,#e8e6e3);font-weight:700;cursor:pointer;font-family:inherit">
          ${GOOGLE_SVG}Continue with Google
        </button>
        <div id="am-auth-error" style="color:var(--neon-magenta,#ff2a6d);font-size:.8em;margin-top:8px;display:none"></div>
        ${appearanceHTML()}
      </div>`;
    wireAppearance(container);
    container.querySelector('#am-google-btn').addEventListener('click', async () => {
      const g = container.querySelector('#am-google-btn');
      g.disabled = true; g.textContent = 'Signing in...';
      const result = await signInWithGoogle(app);
      if (result.success) {
        location.reload();
      } else {
        g.disabled = false;
        g.innerHTML = `${GOOGLE_SVG}Continue with Google`;
        const err = container.querySelector('#am-auth-error');
        err.textContent = result.error; err.style.display = '';
      }
    });
  }

  function renderLoggedIn(container, session) {
    const photo = session.photoURL
      ? `<img src="${session.photoURL}" style="width:56px;height:56px;border-radius:50%;border:2px solid var(--neon-cyan,#05d9e8)">` : '';
    const prefix = `guessGame_${session.displayName}_`;
    const gameCorrect = parseInt(localStorage.getItem(prefix + 'correct') || '0');
    const gameWrong = parseInt(localStorage.getItem(prefix + 'wrong') || '0');
    const gameStreak = parseInt(localStorage.getItem(prefix + 'streak') || '0');
    const gameAccuracy = (gameCorrect + gameWrong) > 0
      ? Math.round((gameCorrect / (gameCorrect + gameWrong)) * 100) : 0;

    container.innerHTML = `
      <div style="text-align:center;padding:10px 0">
        ${photo}
        <div style="margin-top:8px;font-size:1.2em;font-weight:700;color:var(--neon-cyan,#05d9e8);text-transform:var(--et-title-case)">${session.displayName}</div>
        <div style="color:var(--muted,#8a8f98);font-size:.75em;margin-top:2px">${session.legacy ? 'Legacy session' : 'Google Account'}</div>
      </div>
      ${session.legacy ? `
      <div style="border:1px solid var(--border,#2a2a3a);padding:10px;margin:10px 0;font-size:.8em;color:var(--muted,#8a8f98)">
        The site now uses live Google sign-in. Sign in once to carry this account forward — your reviews and favorites stay put.
        <button id="am-legacy-upgrade" style="width:100%;margin-top:8px;justify-content:center;padding:8px 12px;display:inline-flex;align-items:center;border:1px solid var(--border,#2a2a3a);background:var(--bg,#0a0a12);color:var(--text,#e8e6e3);font-weight:700;cursor:pointer;font-family:inherit">${GOOGLE_SVG}Continue with Google</button>
      </div>` : ''}
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0;text-align:center">
        <div class="am-stat"><div class="v" id="am-review-count">...</div><div class="l">Reviews</div></div>
        <div class="am-stat"><div class="v" id="am-avg-rating">...</div><div class="l">Avg Rating</div></div>
        <div class="am-stat"><div class="v" style="color:var(--neon-magenta,#ff2a6d)">${gameStreak}</div><div class="l">Best Streak</div></div>
        <div class="am-stat"><div class="v" style="color:var(--neon-magenta,#ff2a6d)">${gameAccuracy}%</div><div class="l">Game Accuracy</div></div>
      </div>
      ${appearanceHTML()}
      <div id="am-dev-site-slot"></div>
      <button id="am-logout-btn" style="width:100%;margin-top:10px;background:var(--neon-magenta,#ff2a6d);color:#fff;border:none;padding:10px;cursor:pointer;font-weight:700;font-family:inherit">Logout</button>
      <div style="border-top:1px solid var(--border,#2a2a3a);margin-top:12px;padding-top:12px">
        <div class="am-section-title">Currently Reading</div>
        <input id="am-reading" type="text" class="am-input" placeholder="Search catalog..." autocomplete="off" />
        <div id="am-reading-results" class="am-results"></div>
        <div id="am-reading-current" style="margin-top:6px;display:flex;align-items:center;gap:8px"></div>
      </div>
      <div style="border-top:1px solid var(--border,#2a2a3a);margin-top:12px;padding-top:12px">
        <div class="am-section-title">Favorite Books <span style="color:var(--muted,#8a8f98)">(up to 5)</span></div>
        <div style="position:relative">
          <input id="am-fav-input" type="text" class="am-input" placeholder="Search catalog..." autocomplete="off" />
          <div id="am-fav-results" class="am-results"></div>
        </div>
        <div id="am-fav-list" style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px"></div>
        <div style="height:8px"></div>
      </div>`;

    // Admin portal link — only visible to admins.
    //
    // The link is REVEALED on resolve, not rendered-then-hidden: whenAdmin()
    // only ever runs its callback for a confirmed admin, so a non-admin (and
    // anyone we cannot get an answer for) simply never sees the row appear.
    // It arrives a beat after the modal opens, which is the cost of asking
    // site_roles instead of trusting a spoofable local name.
    //
    // ⚠️ This must stay AFTER the template literal above, not inside it. It was
    // once inserted mid-literal, which closed the string early and left the
    // remaining markup (Currently Reading, Favorite Books) sitting in the file
    // as bare HTML. That is a SyntaxError, so the whole module failed to parse
    // and every page importing it — community, clubs, club-read — died on load
    // showing only "Loading community…". A broken module is silent apart from
    // one console line, so it looked like a data problem for a long time.
    //
    // The DOM result is unchanged: insertAdjacentElement('afterend') still puts
    // the link directly below the logout button.
    if (!/\/admin\.html$/.test(location.pathname)) {
      whenAdmin(db, app, () => {
        const logoutBtn = container.querySelector('#am-logout-btn');
        if (!logoutBtn) return; // modal re-rendered while we were resolving
        const adminRow = document.createElement('a');
        adminRow.href = 'admin.html';
        adminRow.style.cssText = 'display:block;text-align:center;margin-top:10px;padding:10px;border:1px solid var(--border,#2a2a3a);text-decoration:none;color:var(--neon-cyan,#05d9e8);font-weight:700;font-size:.9em';
        adminRow.textContent = '🔧 Admin Portal';
        logoutBtn.insertAdjacentElement('afterend', adminRow);
      });
    }

    wireAppearance(container);

    // Estate-approved testers get a quiet link to the dev lane; everyone
    // else (pending, revoked, legacy, offline) sees nothing — silently.
    renderDevSiteLink(container.querySelector('#am-dev-site-slot'), app);

    container.querySelector('#am-logout-btn').addEventListener('click', async () => {
      await signOutGoogle(app);
      location.reload();
    });

    // Legacy v1 capture: one-time upgrade into a live Google session.
    const upgradeBtn = container.querySelector('#am-legacy-upgrade');
    if (upgradeBtn) {
      upgradeBtn.addEventListener('click', async () => {
        upgradeBtn.disabled = true; upgradeBtn.textContent = 'Signing in...';
        const result = await signInWithGoogle(app);
        if (result.success) {
          location.reload();
        } else {
          upgradeBtn.disabled = false;
          upgradeBtn.innerHTML = `${GOOGLE_SVG}Continue with Google`;
        }
      });
    }

    // Review stats
    (async () => {
      try {
        const snapshot = await getDocs(collection(db, col('reviews')));
        let count = 0, total = 0;
        snapshot.docs.forEach(d => {
          const data = d.data();
          if (data.displayName && data.displayName.toLowerCase() === session.displayName.toLowerCase()) {
            count++;
            total += data.rating || 0;
          }
        });
        const countEl = container.querySelector('#am-review-count');
        const avgEl = container.querySelector('#am-avg-rating');
        if (countEl) countEl.textContent = count;
        if (avgEl) avgEl.textContent = count > 0 ? (total / count).toFixed(1) : '—';
      } catch (e) {
        const countEl = container.querySelector('#am-review-count');
        if (countEl) countEl.textContent = '?';
      }
    })();

    // Currently reading + favorites, searching catalog.csv
    (async () => {
      const profileRef = doc(db, col('profiles'), session.displayName.toLowerCase());
      let profile = {};
      try {
        const snap = await getDoc(profileRef);
        if (snap.exists()) profile = snap.data();
      } catch (e) { /* offline */ }

      let catalogBooks = [];
      try {
        catalogBooks = (await loadCatalogBooks()).map(b => ({
          title: b.title, author: b.author, cover: b.coverHref || '',
        }));
      } catch (e) { /* no catalog on this page — search disabled */ }

      function searchCatalog(query) {
        if (!query || query.length < 2) return [];
        const q = query.toLowerCase();
        return catalogBooks.filter(b => b.title.toLowerCase().includes(q)).slice(0, 5);
      }

      function renderSearchResults(resultsEl, query, onSelect) {
        const matches = searchCatalog(query);
        if (matches.length === 0) { resultsEl.style.display = 'none'; return; }
        resultsEl.style.display = 'block';
        resultsEl.innerHTML = matches.map(b =>
          '<div class="am-sr" style="display:flex;align-items:center;gap:8px;padding:6px 8px;cursor:pointer;border-bottom:1px solid var(--border,#2a2a3a)" data-title="' + b.title.replace(/"/g, '&quot;') + '" data-cover="' + (b.cover || '') + '">' +
          (b.cover ? '<img src="' + b.cover + '" style="width:28px;height:auto;border-radius:2px">' : '') +
          '<div style="flex:1;min-width:0"><div style="font-size:.85em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + b.title + '</div><div style="font-size:.7em;color:var(--muted,#8a8f98)">' + b.author + '</div></div></div>'
        ).join('');
        resultsEl.querySelectorAll('.am-sr').forEach(item => {
          item.addEventListener('click', () => {
            onSelect(item.dataset.title, item.dataset.cover);
            resultsEl.style.display = 'none';
          });
        });
      }

      function renderBookWithCover(title, cover) {
        if (!title) return '';
        const img = cover ? '<img src="' + coverUrl(cover) + '" style="width:36px;height:auto;border-radius:2px;border:1px solid var(--border,#2a2a3a)">' : '';
        return img + '<span style="font-size:.85em">' + title + '</span>';
      }

      const readingInput = container.querySelector('#am-reading');
      const readingResults = container.querySelector('#am-reading-results');
      const readingCurrent = container.querySelector('#am-reading-current');
      if (profile.currentlyReading) {
        const match = catalogBooks.find(b => b.title === profile.currentlyReading);
        readingCurrent.innerHTML = renderBookWithCover(profile.currentlyReading, match?.cover || profile.currentlyReadingCover || '');
      }
      readingInput.addEventListener('input', () => {
        renderSearchResults(readingResults, readingInput.value, async (title, cover) => {
          readingInput.value = '';
          await setDoc(profileRef, { displayName: session.displayName, currentlyReading: title, currentlyReadingCover: cover, photoURL: session.photoURL || '', updatedAt: serverTimestamp() }, { merge: true });
          readingCurrent.innerHTML = renderBookWithCover(title, cover);
        });
      });
      readingInput.addEventListener('blur', () => setTimeout(() => { readingResults.style.display = 'none'; }, 200));

      const favInput = container.querySelector('#am-fav-input');
      const favResults = container.querySelector('#am-fav-results');
      const favList = container.querySelector('#am-fav-list');
      const favorites = profile.favorites || [];
      const favCovers = profile.favCovers || {};

      function renderFavs() {
        if (favorites.length === 0) {
          favList.innerHTML = '<span style="font-size:.8em;color:var(--muted,#8a8f98)">No favorites yet</span>';
          return;
        }
        favList.innerHTML = favorites.map((title, idx) => {
          const cover = coverUrl(favCovers[title] || '');
          const img = cover ? '<img src="' + coverUrl(cover) + '" style="width:40px;height:auto;border-radius:2px;border:1px solid var(--border,#2a2a3a)">' : '';
          return '<div style="position:relative;display:inline-block">' + img +
            '<button data-idx="' + idx + '" style="position:absolute;top:-4px;right:-4px;background:var(--neon-magenta,#ff2a6d);color:#fff;border:none;border-radius:50%;width:16px;height:16px;font-size:10px;cursor:pointer;line-height:1">×</button>' +
            (!img ? '<div style="width:40px;height:55px;background:var(--border,#2a2a3a);display:flex;align-items:center;justify-content:center;font-size:.6em;text-align:center;padding:2px">' + title.slice(0, 15) + '</div>' : '') +
            '</div>';
        }).join('');
        favList.querySelectorAll('button[data-idx]').forEach(x => {
          x.addEventListener('click', async () => {
            const idx = parseInt(x.dataset.idx);
            const removed = favorites.splice(idx, 1)[0];
            delete favCovers[removed];
            await setDoc(profileRef, { favorites, favCovers, updatedAt: serverTimestamp() }, { merge: true });
            renderFavs();
          });
        });
      }
      renderFavs();

      favInput.addEventListener('input', () => {
        renderSearchResults(favResults, favInput.value, async (title, cover) => {
          favInput.value = '';
          if (favorites.length >= 5 || favorites.includes(title)) return;
          favorites.push(title);
          if (cover) favCovers[title] = cover;
          await setDoc(profileRef, { displayName: session.displayName, favorites, favCovers, photoURL: session.photoURL || '', updatedAt: serverTimestamp() }, { merge: true });
          renderFavs();
        });
      });
      favInput.addEventListener('blur', () => setTimeout(() => { favResults.style.display = 'none'; }, 200));
    })();
  }
}
