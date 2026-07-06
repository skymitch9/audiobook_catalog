// club-reads.js — book club system, Phase 2: club reads, milestones,
// per-milestone discussions, member progress + spoiler shield.
// ES module, browser-native (no build step)

import {
  collection, doc, getDoc, getDocs, setDoc, deleteDoc, updateDoc,
  serverTimestamp, runTransaction, increment,
} from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';
import { slugifyName } from './identity.js';

export const MAX_ACTIVE_READS = 2;
export const MAX_MILESTONES = 20;
export const MAX_COMMENT_LENGTH = 2000;
export const GENERAL_MILESTONE = 'general';

// ==================== Pure utilities ====================

/** Parse "10:07" (hh:mm) into minutes. Returns 0 for blank/invalid. */
export function parseHhmm(hhmm) {
  const m = /^(\d+):(\d{1,2})$/.exec((hhmm || '').trim());
  if (!m) return 0;
  return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
}

/** Format minutes as "h:mm". */
export function formatHhmm(minutes) {
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return `${h}:${String(m).padStart(2, '0')}`;
}

/**
 * Auto-split an audiobook into n equal milestones by duration.
 * @returns {Array<{id: string, label: string, position: number}>}
 */
export function splitByDuration(durationMinutes, n) {
  const parts = Math.max(1, Math.min(MAX_MILESTONES, Math.floor(n) || 1));
  const milestones = [];
  for (let i = 0; i < parts; i++) {
    const start = Math.round((durationMinutes * i) / parts);
    const end = Math.round((durationMinutes * (i + 1)) / parts);
    const range = durationMinutes > 0 ? ` (${formatHhmm(start)}–${formatHhmm(end)})` : '';
    milestones.push({ id: `m${i}`, label: `Part ${i + 1}${range}`, position: i });
  }
  return milestones;
}

/**
 * Parse a manual milestone list — one label per line, blanks ignored.
 * @returns {{ milestones?: Array, error?: string }}
 */
export function parseManualMilestones(text) {
  const labels = (text || '').split('\n').map(l => l.trim()).filter(Boolean);
  if (labels.length === 0) return { error: 'Add at least one milestone (one per line).' };
  if (labels.length > MAX_MILESTONES) return { error: `At most ${MAX_MILESTONES} milestones.` };
  return { milestones: labels.map((label, i) => ({ id: `m${i}`, label, position: i })) };
}

/**
 * Spoiler shield predicate. General is never locked; a milestone is locked
 * while the member's progress (-1 = not started) is behind it.
 */
export function isMilestoneLocked(milestonePosition, myPosition, milestoneId) {
  if (milestoneId === GENERAL_MILESTONE) return false;
  return milestonePosition > (typeof myPosition === 'number' ? myPosition : -1);
}

/** Minimal RFC-4180 CSV parser (quoted fields, embedded commas/newlines). */
export function parseCsv(text) {
  const rows = [];
  let field = '';
  let row = [];
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += ch;
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ',') {
      row.push(field); field = '';
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      row.push(field); field = '';
      if (row.length > 1 || row[0] !== '') rows.push(row);
      row = [];
    } else field += ch;
  }
  if (field !== '' || row.length > 0) { row.push(field); rows.push(row); }
  if (rows.length === 0) return [];
  const header = rows[0];
  return rows.slice(1).map(r => Object.fromEntries(header.map((h, i) => [h, r[i] ?? ''])));
}

/**
 * Fetch and parse the catalog for the book picker.
 * @returns {Promise<Array<{title, author, durationHhmm, durationMinutes, coverHref}>>}
 */
export async function loadCatalogBooks(url = 'catalog.csv') {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load catalog (${res.status})`);
  const rows = parseCsv(await res.text());
  return rows
    .filter(r => (r.title || '').trim())
    .map(r => ({
      title: r.title,
      author: r.author || '',
      durationHhmm: r.duration_hhmm || '',
      durationMinutes: parseHhmm(r.duration_hhmm),
      coverHref: r.cover_href || '',
    }))
    .sort((a, b) => a.title.localeCompare(b.title));
}

// ==================== Reads ====================

/**
 * Start a club read. Transactional: at most MAX_ACTIVE_READS active books
 * per club; assigns the first free slot (1 = main read, 2 = side read).
 */
export async function startRead(db, clubId, input, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to start a read.' };
  }
  if (!(input.bookTitle || '').trim()) {
    return { success: false, error: 'Pick a book first.' };
  }
  const milestones = input.milestones || [];
  if (milestones.length < 1 || milestones.length > MAX_MILESTONES) {
    return { success: false, error: `Milestones must number 1–${MAX_MILESTONES}.` };
  }
  const clubRef = doc(db, col('clubs'), clubId);
  const readRef = doc(collection(db, col('clubs'), clubId, 'reads'));
  try {
    await runTransaction(db, async (tx) => {
      const clubSnap = await tx.get(clubRef);
      if (!clubSnap.exists()) throw new Error('Club not found.');
      const activeSlots = clubSnap.data().activeSlots || [];
      if (activeSlots.length >= MAX_ACTIVE_READS) {
        throw new Error('This club already has 2 active books. Finish or swap one first.');
      }
      const slot = activeSlots.includes(1) ? 2 : 1;
      tx.update(clubRef, { activeSlots: [...activeSlots, slot] });
      tx.set(readRef, {
        bookTitle: input.bookTitle.trim(),
        bookAuthor: (input.bookAuthor || '').trim(),
        coverHref: input.coverHref || '',
        durationMinutes: input.durationMinutes || 0,
        status: 'active',
        slot,
        milestones,
        startedAt: serverTimestamp(),
        finishedAt: null,
        startedBy: session.displayName,
        commentCount: 0,
      });
    });
    return { success: true, readId: readRef.id };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/** Fetch all reads for a club (active and archived). */
export async function getReads(db, clubId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'reads'));
  return snap.docs.map(d => ({ id: d.id, ...d.data() }));
}

/**
 * Remove a read entirely (any member — "the book we're reading" is
 * club-editable). Frees its active slot and deletes its comments and
 * progress docs.
 */
export async function removeRead(db, clubId, readId) {
  const clubRef = doc(db, col('clubs'), clubId);
  const readRef = doc(db, col('clubs'), clubId, 'reads', readId);
  try {
    const readSnap = await getDoc(readRef);
    if (!readSnap.exists()) return { success: false, error: 'Read not found.' };
    const { slot, status } = readSnap.data();
    if (status === 'active') {
      await runTransaction(db, async (tx) => {
        const clubSnap = await tx.get(clubRef);
        if (!clubSnap.exists()) throw new Error('Club not found.');
        const activeSlots = [...(clubSnap.data().activeSlots || [])];
        const idx = activeSlots.indexOf(slot);
        if (idx !== -1) activeSlots.splice(idx, 1);
        tx.update(clubRef, { activeSlots });
      });
    }
    for (const sub of ['comments', 'progress']) {
      const snap = await getDocs(collection(db, col('clubs'), clubId, 'reads', readId, sub));
      for (const d of snap.docs) {
        await deleteDoc(doc(db, col('clubs'), clubId, 'reads', readId, sub, d.id));
      }
    }
    await deleteDoc(readRef);
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/** Fetch a single read. Returns null if missing. */
export async function getRead(db, clubId, readId) {
  const snap = await getDoc(doc(db, col('clubs'), clubId, 'reads', readId));
  return snap.exists() ? { id: snap.id, ...snap.data() } : null;
}

// ==================== Comments ====================

/**
 * Add a comment (or a reply when parentId is set) to a milestone discussion.
 */
export async function addComment(db, clubId, readId, input, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to comment.' };
  }
  const text = (input.text || '').trim();
  if (!text) return { success: false, error: 'Comment cannot be empty.' };
  if (text.length > MAX_COMMENT_LENGTH) {
    return { success: false, error: `Comments must be ${MAX_COMMENT_LENGTH} characters or fewer.` };
  }
  try {
    const commentRef = doc(collection(db, col('clubs'), clubId, 'reads', readId, 'comments'));
    await setDoc(commentRef, {
      milestoneId: input.milestoneId || GENERAL_MILESTONE,
      parentId: input.parentId || null,
      displayName: session.displayName,
      slug: slugifyName(session.displayName),
      text,
      createdAt: serverTimestamp(),
    });
    await updateDoc(doc(db, col('clubs'), clubId, 'reads', readId), { commentCount: increment(1) });
    return { success: true, commentId: commentRef.id };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/** Delete a comment (author or host/moderator — enforced in the UI). */
export async function deleteComment(db, clubId, readId, commentId) {
  try {
    await deleteDoc(doc(db, col('clubs'), clubId, 'reads', readId, 'comments', commentId));
    await updateDoc(doc(db, col('clubs'), clubId, 'reads', readId), { commentCount: increment(-1) });
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/** Fetch all comments for a read (grouped/sorted client-side). */
export async function getComments(db, clubId, readId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'reads', readId, 'comments'));
  return snap.docs.map(d => ({ id: d.id, ...d.data() }));
}

// ==================== Progress ====================

/**
 * Record how far a member has gotten. position -1 = not started;
 * otherwise the highest milestone position they have finished.
 */
export async function setProgress(db, clubId, readId, position, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to track progress.' };
  }
  try {
    const slug = slugifyName(session.displayName);
    await setDoc(doc(db, col('clubs'), clubId, 'reads', readId, 'progress', slug), {
      displayName: session.displayName,
      milestonePosition: position,
      updatedAt: serverTimestamp(),
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/** Fetch every member's progress for a read. */
export async function getProgressAll(db, clubId, readId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'reads', readId, 'progress'));
  return snap.docs.map(d => ({ slug: d.id, ...d.data() }));
}
