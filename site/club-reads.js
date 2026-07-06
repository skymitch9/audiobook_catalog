// club-reads.js — book club system, Phase 2: club reads, milestones,
// per-milestone discussions, member progress + spoiler shield.
// ES module, browser-native (no build step)

import {
  collection, doc, getDoc, getDocs, setDoc, deleteDoc, updateDoc,
  query, where, serverTimestamp, runTransaction, increment,
} from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';
import { slugifyName } from './identity.js';

export const MAX_ACTIVE_READS = 2;
export const MAX_MILESTONES = 400;
export const MAX_COMMENT_LENGTH = 2000;
export const MAX_QUOTE_LENGTH = 500;
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

const PART_TITLE_RE = /^\s*(part|book|disc|volume)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b/i;
const SYNTHETIC_GROUP_SIZE = 25;

/**
 * Group a chapter-title list for friendlier long dropdowns (optgroups).
 * Uses real "Part N" / "Book N" headings found in the titles; when a book
 * has no such headings, falls back to fixed chunks of 25 ("Ch 1-25").
 * @returns {Array<{label: string, start: number, end: number}>}
 */
export function groupChapters(chapterTitles) {
  const n = chapterTitles.length;
  if (n === 0) return [];
  const boundaries = [];
  chapterTitles.forEach((t, i) => {
    if (PART_TITLE_RE.test(t || '')) boundaries.push(i);
  });
  if (boundaries.length >= 2) {
    const groups = [];
    if (boundaries[0] > 0) {
      groups.push({ label: 'Beginning', start: 0, end: boundaries[0] - 1 });
    }
    boundaries.forEach((b, k) => {
      groups.push({
        label: (chapterTitles[b] || '').trim(),
        start: b,
        end: k + 1 < boundaries.length ? boundaries[k + 1] - 1 : n - 1,
      });
    });
    return groups;
  }
  if (n <= SYNTHETIC_GROUP_SIZE) {
    return [{ label: 'Chapters', start: 0, end: n - 1 }];
  }
  const groups = [];
  for (let start = 0; start < n; start += SYNTHETIC_GROUP_SIZE) {
    const end = Math.min(start + SYNTHETIC_GROUP_SIZE, n) - 1;
    groups.push({ label: `Ch ${start + 1}–${end + 1}`, start, end });
  }
  return groups;
}

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

function escapeHtmlText(s) {
  return (s || '').replace(/[&<>"']/g, c => HTML_ESCAPES[c]);
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Escape text for HTML and wrap @mentions of club members in
 * <span class="mention"> (plus .mention-me when it's the viewer).
 * Longest names match first so "@Jane Doe" beats "@Jane".
 */
export function highlightMentions(rawText, memberNames, myDisplayName) {
  const html = escapeHtmlText(rawText);
  const names = [...(memberNames || [])].filter(Boolean)
    .sort((a, b) => b.length - a.length);
  if (!names.length) return html;
  const alternation = names.map(n => escapeRegExp(escapeHtmlText(n))).join('|');
  const re = new RegExp(`@(${alternation})(?![\\w])`, 'gi');
  return html.replace(re, (match, name) => {
    const me = myDisplayName && name.toLowerCase() === myDisplayName.toLowerCase();
    return `<span class="mention${me ? ' mention-me' : ''}">@${name}</span>`;
  });
}

/** True when the text @mentions the given display name. */
export function mentionsUser(rawText, displayName) {
  if (!displayName) return false;
  const re = new RegExp(`@${escapeRegExp(escapeHtmlText(displayName))}(?![\\w])`, 'i');
  return re.test(escapeHtmlText(rawText || ''));
}

/**
 * Per-comment spoiler predicate: a comment tagged with a chapter is a
 * spoiler for viewers whose chapter progress (-1 = not started) hasn't
 * reached that chapter. Untagged comments are never spoilers.
 */
export function isCommentSpoiler(commentChapterIndex, viewerChapterIndex) {
  if (commentChapterIndex === null || commentChapterIndex === undefined) return false;
  return commentChapterIndex > (typeof viewerChapterIndex === 'number' ? viewerChapterIndex : -1);
}

/**
 * Spoiler shield predicate. General is never locked; a milestone is locked
 * while the member's progress (-1 = not started) is behind it.
 */
export function isMilestoneLocked(milestonePosition, myPosition, milestoneId) {
  if (milestoneId === GENERAL_MILESTONE) return false;
  return milestonePosition > (typeof myPosition === 'number' ? myPosition : -1);
}

// ---- Chapter-based milestones (data from site/chapters.json, generated by
// ---- app/tools/extract_chapters.py on the machine with the audio library) ----

let chaptersCache = null;

/** Fetch chapters.json once. Missing file = no chapter data (graceful). */
export async function loadChaptersData(url = 'chapters.json') {
  if (chaptersCache) return chaptersCache;
  try {
    const res = await fetch(url);
    chaptersCache = res.ok ? await res.json() : {};
  } catch {
    chaptersCache = {};
  }
  return chaptersCache;
}

/**
 * Chapter entry for a book, or null when unavailable.
 * @returns {Promise<{source: string, chapters: Array<{title, start_min}>, parts: Array}|null>}
 */
export async function getBookChapters(title) {
  const data = await loadChaptersData();
  const entry = data[title];
  return entry && Array.isArray(entry.chapters) && entry.chapters.length > 0 ? entry : null;
}

/** One milestone per chapter. Errors when the book has too many chapters. */
export function milestonesFromChapters(chapters) {
  if (chapters.length > MAX_MILESTONES) {
    return { error: `${chapters.length} chapters is too many for one-per-chapter (max ${MAX_MILESTONES}) — use chapter ranges.` };
  }
  return {
    milestones: chapters.map((c, i) => ({ id: `m${i}`, label: c.title, position: i, chStart: i, chEnd: i })),
  };
}

/** Group chapters into n contiguous ranges ("Ch 1–5"). */
export function milestonesFromChapterRanges(chapters, n) {
  const groups = Math.max(1, Math.min(Math.min(MAX_MILESTONES, chapters.length), Math.floor(n) || 1));
  const milestones = [];
  for (let i = 0; i < groups; i++) {
    const start = Math.floor((chapters.length * i) / groups);
    const end = Math.floor((chapters.length * (i + 1)) / groups) - 1;
    const label = start === end ? `Ch ${start + 1}: ${chapters[start].title}` : `Ch ${start + 1}–${end + 1}`;
    milestones.push({ id: `m${i}`, label, position: i, chStart: start, chEnd: end });
  }
  return milestones;
}

/** One milestone per detected part ("Part One", "Book 2", ...). */
export function milestonesFromParts(parts) {
  return parts.map((p, i) => ({
    id: `m${i}`, label: p.label, position: i,
    chStart: p.start_index, chEnd: p.end_index,
  }));
}

/** A single milestone covering the whole book. */
export function wholeBookMilestones() {
  return [{ id: 'm0', label: 'Whole book', position: 0 }];
}

let promptsCache = null;

/** Discussion prompts for a book (site/discussion_prompts.json), or null. */
export async function getBookPrompts(title) {
  if (!promptsCache) {
    try {
      const res = await fetch('discussion_prompts.json');
      promptsCache = res.ok ? await res.json() : {};
    } catch {
      promptsCache = {};
    }
  }
  const entry = promptsCache[title];
  return entry && Array.isArray(entry.prompts) && entry.prompts.length ? entry.prompts : null;
}

let warningsCache = null;

/** Published content warnings for a book (site/content_warnings.json), or null. */
export async function getBookWarnings(title) {
  if (!warningsCache) {
    try {
      const res = await fetch('content_warnings.json');
      warningsCache = res.ok ? await res.json() : {};
    } catch {
      warningsCache = {};
    }
  }
  const entry = warningsCache[title];
  return entry && Array.isArray(entry.warnings) && entry.warnings.length ? entry.warnings : null;
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
        chapterTitles: (input.chapters || []).map(c => c.title).slice(0, MAX_MILESTONES),
        startedAt: serverTimestamp(),
        finishedAt: null,
        startedBy: session.displayName,
        commentCount: 0,
      });
    });
    await refreshClubAvatar(db, clubId);
    return { success: true, readId: readRef.id };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/**
 * Recompute the club's avatar — the cover of the current book. Defaults to
 * the lowest-slot active read; a specific book can be chosen via
 * avatarReadId (honored while that read stays active). Cleared when no
 * book is active (UI falls back to the club emoji). Best-effort.
 */
export async function refreshClubAvatar(db, clubId) {
  try {
    const clubRef = doc(db, col('clubs'), clubId);
    const [clubSnap, reads] = await Promise.all([getDoc(clubRef), getReads(db, clubId)]);
    if (!clubSnap.exists()) return;
    const active = reads.filter(r => r.status === 'active').sort((a, b) => a.slot - b.slot);
    const chosen = active.find(r => r.id === clubSnap.data().avatarReadId) || active[0] || null;
    await updateDoc(clubRef, {
      avatarReadId: chosen ? chosen.id : null,
      avatarCoverHref: chosen ? (chosen.coverHref || '') : '',
    });
  } catch { /* avatar refresh must never break the main action */ }
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
    await refreshClubAvatar(db, clubId);
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

/**
 * Finish or abandon an active read: frees its slot and moves it to History.
 * @param {'finished'|'abandoned'} status
 */
export async function finishRead(db, clubId, readId, status) {
  if (status !== 'finished' && status !== 'abandoned') {
    return { success: false, error: 'Invalid status.' };
  }
  const clubRef = doc(db, col('clubs'), clubId);
  const readRef = doc(db, col('clubs'), clubId, 'reads', readId);
  try {
    await runTransaction(db, async (tx) => {
      const readSnap = await tx.get(readRef);
      if (!readSnap.exists()) throw new Error('Read not found.');
      const read = readSnap.data();
      if (read.status !== 'active') throw new Error('This read is already archived.');
      const clubSnap = await tx.get(clubRef);
      if (!clubSnap.exists()) throw new Error('Club not found.');
      const activeSlots = [...(clubSnap.data().activeSlots || [])];
      const idx = activeSlots.indexOf(read.slot);
      if (idx !== -1) activeSlots.splice(idx, 1);
      tx.update(clubRef, { activeSlots });
      tx.update(readRef, { status, finishedAt: serverTimestamp() });
    });
    await refreshClubAvatar(db, clubId);
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

// ==================== Club TBR ====================

/** Fetch the club's TBR list, most-voted first. */
export async function getTbr(db, clubId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'tbr'));
  return snap.docs
    .map(d => ({ id: d.id, ...d.data() }))
    .sort((a, b) => (b.voterSlugs || []).length - (a.voterSlugs || []).length);
}

/** Suggest a book for the club TBR. Duplicate titles are rejected. */
export async function addTbrItem(db, clubId, book, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to suggest a book.' };
  }
  if (!(book.title || '').trim()) return { success: false, error: 'Pick a book first.' };
  try {
    const existing = await getDocs(query(
      collection(db, col('clubs'), clubId, 'tbr'),
      where('bookTitle', '==', book.title)
    ));
    if (existing.docs.length > 0) {
      return { success: false, error: 'That book is already on the club TBR.' };
    }
    const slug = slugifyName(session.displayName);
    const itemRef = doc(collection(db, col('clubs'), clubId, 'tbr'));
    await setDoc(itemRef, {
      bookTitle: book.title,
      bookAuthor: book.author || '',
      coverHref: book.coverHref || '',
      durationMinutes: book.durationMinutes || 0,
      durationHhmm: book.durationHhmm || '',
      suggestedBy: session.displayName,
      voterSlugs: [slug],
      createdAt: serverTimestamp(),
    });
    return { success: true, itemId: itemRef.id };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/** Remove a TBR suggestion. */
export async function removeTbrItem(db, clubId, itemId) {
  try {
    await deleteDoc(doc(db, col('clubs'), clubId, 'tbr', itemId));
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/** Toggle the caller's vote on a TBR suggestion. */
export async function toggleTbrVote(db, clubId, itemId, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to vote.' };
  }
  const slug = slugifyName(session.displayName);
  const itemRef = doc(db, col('clubs'), clubId, 'tbr', itemId);
  try {
    await runTransaction(db, async (tx) => {
      const snap = await tx.get(itemRef);
      if (!snap.exists()) throw new Error('Suggestion not found.');
      const voters = snap.data().voterSlugs || [];
      tx.update(itemRef, {
        voterSlugs: voters.includes(slug) ? voters.filter(s => s !== slug) : [...voters, slug],
      });
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
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
      chapterIndex: typeof input.chapterIndex === 'number' && input.chapterIndex >= 0 ? input.chapterIndex : null,
      displayName: session.displayName,
      slug: slugifyName(session.displayName),
      text,
      reactions: {},
      isPinned: false,
      createdAt: serverTimestamp(),
    });
    await updateDoc(doc(db, col('clubs'), clubId, 'reads', readId), { commentCount: increment(1) });
    return { success: true, commentId: commentRef.id };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

export const REACTION_EMOJI = ['👍', '❤️', '😂', '😮', '😢', '🎉'];

/** Toggle the caller's reaction (one of REACTION_EMOJI) on a comment. */
export async function toggleReaction(db, clubId, readId, commentId, emoji, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to react.' };
  }
  if (!REACTION_EMOJI.includes(emoji)) return { success: false, error: 'Invalid reaction.' };
  const slug = slugifyName(session.displayName);
  const ref = doc(db, col('clubs'), clubId, 'reads', readId, 'comments', commentId);
  try {
    await runTransaction(db, async (tx) => {
      const snap = await tx.get(ref);
      if (!snap.exists()) throw new Error('Comment not found.');
      const reactions = { ...(snap.data().reactions || {}) };
      const who = reactions[emoji] || [];
      const next = who.includes(slug) ? who.filter(s => s !== slug) : [...who, slug];
      if (next.length) reactions[emoji] = next; else delete reactions[emoji];
      tx.update(ref, { reactions });
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/** Pin/unpin a comment (host/moderator — enforced in the UI). Pinned
 * comments sort to the top of their milestone's discussion. */
export async function togglePin(db, clubId, readId, commentId) {
  const ref = doc(db, col('clubs'), clubId, 'reads', readId, 'comments', commentId);
  try {
    await runTransaction(db, async (tx) => {
      const snap = await tx.get(ref);
      if (!snap.exists()) throw new Error('Comment not found.');
      tx.update(ref, { isPinned: !snap.data().isPinned });
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

// ==================== Quotes ====================

/** Save a favorite quote from the book. Chapter-taggable like comments. */
export async function addQuote(db, clubId, readId, input, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to save quotes.' };
  }
  const text = (input.text || '').trim();
  if (!text) return { success: false, error: 'Quote cannot be empty.' };
  if (text.length > MAX_QUOTE_LENGTH) {
    return { success: false, error: `Quotes must be ${MAX_QUOTE_LENGTH} characters or fewer.` };
  }
  try {
    const ref = doc(collection(db, col('clubs'), clubId, 'reads', readId, 'quotes'));
    await setDoc(ref, {
      text,
      chapterIndex: typeof input.chapterIndex === 'number' && input.chapterIndex >= 0 ? input.chapterIndex : null,
      displayName: session.displayName,
      slug: slugifyName(session.displayName),
      createdAt: serverTimestamp(),
    });
    return { success: true, quoteId: ref.id };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

/** All quotes for a read, oldest first. */
export async function getQuotes(db, clubId, readId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'reads', readId, 'quotes'));
  return snap.docs
    .map(d => ({ id: d.id, ...d.data() }))
    .sort((a, b) => (a.createdAt?.seconds || 0) - (b.createdAt?.seconds || 0));
}

/** Delete a quote (saver or host/moderator — enforced in the UI). */
export async function deleteQuote(db, clubId, readId, quoteId) {
  try {
    await deleteDoc(doc(db, col('clubs'), clubId, 'reads', readId, 'quotes', quoteId));
    return { success: true };
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

/**
 * Record chapter-level progress ("I'm at chapter N"). -1 = not started.
 * Used for reads whose book has chapter data; drives per-comment spoilers
 * and chapter-mapped section locks.
 */
export async function setChapterProgress(db, clubId, readId, chapterIndex, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to track progress.' };
  }
  try {
    const slug = slugifyName(session.displayName);
    await setDoc(doc(db, col('clubs'), clubId, 'reads', readId, 'progress', slug), {
      displayName: session.displayName,
      chapterIndex,
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
