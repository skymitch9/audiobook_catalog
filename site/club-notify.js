// club-notify.js — "someone posted in your club" badges
// ES module, browser-native (no build step)
//
// The site is static, so notifications are client-side: every read doc
// already carries commentCount (incremented on each post). We diff those
// against a per-browser localStorage map of what the user last saw, and
// surface the difference as badges. Opening a discussion marks that read
// seen. No new Firestore writes anywhere.

import { collection, getDocs, query, where } from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
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

/** Mark a read's comments as seen (call when the discussion page loads). */
export function markReadSeen(clubId, readId, commentCount) {
  const map = loadSeen();
  map[clubId] = map[clubId] || {};
  map[clubId][readId] = Math.max(commentCount || 0, map[clubId][readId] || 0);
  saveSeen(map);
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
 * Activity across every club the user belongs to.
 * Returns [{clubId, name, newCount}], newest-noise first. One query for the
 * clubs + one reads fetch per club — fine at this scale.
 */
export async function clubActivity(db, slug) {
  if (!slug) return [];
  const snap = await getDocs(query(
    collection(db, col('clubs')),
    where('memberSlugs', 'array-contains', slug),
  ));
  const seen = loadSeen();
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
