// club-notify.js — "someone posted in your club" badges
// ES module, browser-native (no build step)
//
// Every read doc already carries commentCount (incremented on each post).
// Badges are the diff between those counts and what the user last saw.
// Seen state lives in Firestore (club_seen/{userSlug}, one doc per user,
// deep-merged) so it follows the user across devices; localStorage acts as
// an offline/instant cache and the two merge by taking the max per read.

import { doc, getDoc, setDoc, collection, getDocs, query, where } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';

const SEEN_KEY = 'ab_club_seen';

export function loadSeen() {
  try {
    return JSON.parse(localStorage.getItem(SEEN_KEY)) || {};
  } catch (e) {
    return {};
  }
}

function saveSeen(map) {
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify(map));
  } catch (e) { /* private mode etc. */ }
}

function mergeMax(a, b) {
  const out = JSON.parse(JSON.stringify(a || {}));
  for (const [clubId, reads] of Object.entries(b || {})) {
    out[clubId] = out[clubId] || {};
    for (const [readId, count] of Object.entries(reads || {})) {
      out[clubId][readId] = Math.max(out[clubId][readId] || 0, count || 0);
    }
  }
  return out;
}

/**
 * Pull the user's remote seen map, merge it with the local cache (max per
 * read wins), cache the result locally, and return it. Falls back to the
 * local cache offline or signed out.
 */
export async function syncSeen(db, slug) {
  if (!db || !slug) return loadSeen();
  try {
    const snap = await getDoc(doc(db, col('club_seen'), slug));
    const remote = snap.exists() ? (snap.data().seen || {}) : {};
    const merged = mergeMax(loadSeen(), remote);
    saveSeen(merged);
    return merged;
  } catch (e) {
    console.warn('[notify] seen sync failed:', e);
    return loadSeen();
  }
}

/**
 * Mark a read's comments as seen (call when the discussion page loads).
 * Writes locally right away and deep-merges into the user's Firestore doc.
 */
export async function markReadSeen(db, slug, clubId, readId, commentCount) {
  const map = loadSeen();
  map[clubId] = map[clubId] || {};
  map[clubId][readId] = Math.max(commentCount || 0, map[clubId][readId] || 0);
  saveSeen(map);
  if (!db || !slug) return;
  try {
    await setDoc(doc(db, col('club_seen'), slug), {
      seen: { [clubId]: { [readId]: map[clubId][readId] } },
    }, { merge: true });
  } catch (e) {
    console.warn('[notify] seen write failed:', e);
  }
}

/** New-comment count for one club given its reads (active only). */
export function newCountForClub(clubId, reads, seenMap) {
  const seen = (seenMap || loadSeen())[clubId] || {};
  let n = 0;
  for (const r of reads) {
    if (r.status !== 'active') continue;
    n += Math.max(0, (r.commentCount || 0) - (seen[r.id] || 0));
  }
  return n;
}

/**
 * Activity across every club the user belongs to (remote-seen aware).
 * Returns [{clubId, name, newCount}], noisiest first.
 */
export async function clubActivity(db, slug) {
  if (!slug) return [];
  const seen = await syncSeen(db, slug);
  const snap = await getDocs(query(
    collection(db, col('clubs')),
    where('memberSlugs', 'array-contains', slug),
  ));
  const out = [];
  for (const d of snap.docs) {
    const club = d.data();
    if (club.archived) continue;
    const readsSnap = await getDocs(collection(db, col('clubs'), d.id, 'reads'));
    const reads = readsSnap.docs.map(r => ({ id: r.id, ...r.data() }));
    out.push({ clubId: d.id, name: club.name || d.id, newCount: newCountForClub(d.id, reads, seen) });
  }
  return out.sort((a, b) => b.newCount - a.newCount);
}

/** Render "N new" into an element, hiding it when quiet. */
export function renderBadge(el, count) {
  if (!el) return;
  if (count > 0) {
    el.textContent = `${count} new`;
    el.style.display = '';
  } else {
    el.style.display = 'none';
  }
}
