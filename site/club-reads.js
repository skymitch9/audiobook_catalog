// club-reads.js — book club system, Phase 2: club reads, milestones,
// per-milestone discussions, member progress + spoiler shield.
// ES module, browser-native (no build step)

import {
  collection, doc, getDoc, getDocs, setDoc, deleteDoc, updateDoc,
  query, where, serverTimestamp, runTransaction, increment,
} from 'https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js';
import { col } from './fb-env.js';
import { coverUrl } from './covers-base.js';
import { slugifyName } from './identity.js';
import { describeActionError } from './permission-ux.js';
import { reportGate } from './gate-shadow.js';

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

// ==================== Reading schedule (due dates) ====================
//
// Milestone entries on the read doc gain an OPTIONAL `dueAt`: epoch millis at
// end-of-day (23:59:59.999) local to whoever set the schedule — the same
// absolute-instant-in-millis convention as the club doc's nextMeetingAt.
// Absent/null = no due date; partial schedules are fine. Saving a schedule
// also stamps `scheduleUpdatedAt` on the read doc. The Discord notifier
// (backlog #2) consumes exactly this: reads[].milestones[].dueAt vs the
// progress subcollection. Feature-gated per club (clubs.js FEATURE_DEFAULTS
// key `readingSchedule`).

/** 'YYYY-MM-DD' -> local end-of-day epoch millis, or null for blank/invalid. */
export function dateInputToDueAt(yyyyMmDd) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec((yyyyMmDd || '').trim());
  if (!m) return null;
  const d = new Date(+m[1], +m[2] - 1, +m[3], 23, 59, 59, 999);
  return Number.isFinite(d.getTime()) ? d.getTime() : null;
}

/** Epoch millis -> local 'YYYY-MM-DD' for a date input, or '' when unset. */
export function dueAtToDateInput(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return '';
  const d = new Date(ms);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Short viewer-local label for a due date, e.g. "Aug 20". */
export function formatDueDate(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return '';
  const d = new Date(ms);
  const opts = { month: 'short', day: 'numeric' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString([], opts);
}

/**
 * Spread `count` due dates evenly from startInput to endInput (both local
 * 'YYYY-MM-DD'; the LAST milestone lands exactly on endInput).
 * @returns {{ dates?: string[], error?: string }}
 */
export function spreadScheduleDates(count, startInput, endInput) {
  const n = Math.floor(count);
  if (!(n >= 1)) return { error: 'Nothing to schedule.' };
  const parse = (s) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec((s || '').trim());
    return m ? new Date(+m[1], +m[2] - 1, +m[3], 12) : null; // noon dodges DST edges
  };
  const start = parse(startInput);
  const end = parse(endInput);
  if (!end) return { error: 'Pick a finish-by date.' };
  if (!start) return { error: 'Pick a start date.' };
  const DAY = 24 * 60 * 60 * 1000;
  const totalDays = Math.round((end.getTime() - start.getTime()) / DAY);
  if (totalDays < 0) return { error: 'The finish date is before the start date.' };
  const pad = (x) => String(x).padStart(2, '0');
  const dates = [];
  for (let i = 0; i < n; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(),
      start.getDate() + Math.round((totalDays * (i + 1)) / n), 12);
    dates.push(`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
  }
  return { dates };
}

/** True when at least one milestone carries a due date. */
export function hasSchedule(milestones) {
  return (milestones || []).some(m => typeof m.dueAt === 'number' && Number.isFinite(m.dueAt));
}

/**
 * The position a member should have reached by `nowMs`: the highest
 * milestone position whose dueAt has passed. -1 when nothing is due yet.
 */
export function expectedSchedulePosition(milestones, nowMs) {
  let best = -1;
  for (const m of milestones || []) {
    if (typeof m.dueAt === 'number' && m.dueAt <= nowMs && m.position > best) best = m.position;
  }
  return best;
}

/**
 * A member's effective milestone position from their progress doc.
 * Chaptered reads store chapterIndex; a part-shaped milestone counts as
 * completed once chapterIndex reaches its chEnd. -1 = not started.
 */
export function memberSchedulePosition(milestones, progress, chaptered) {
  const list = milestones || [];
  const last = list.length ? Math.max(...list.map(m => m.position)) : -1;
  if (!progress) return -1;
  if (progress.finished) return last;
  if (chaptered) {
    const ch = typeof progress.chapterIndex === 'number' ? progress.chapterIndex : -1;
    let best = -1;
    for (const m of list) {
      if (typeof m.chEnd === 'number' && ch >= m.chEnd && m.position > best) best = m.position;
    }
    return best;
  }
  return typeof progress.milestonePosition === 'number' ? progress.milestonePosition : -1;
}

/** The next upcoming due milestone (smallest future dueAt), or null. */
export function nextDueMilestone(milestones, nowMs) {
  let next = null;
  for (const m of milestones || []) {
    if (typeof m.dueAt === 'number' && m.dueAt > nowMs && (!next || m.dueAt < next.dueAt)) next = m;
  }
  return next;
}

/**
 * On-track / behind verdict for one member against the schedule.
 * @returns {{status: 'none'|'done'|'on-track'|'behind', behindBy: number}}
 *   'none' = no schedule set; 'done' = member finished the book;
 *   behindBy = how many past-due milestones the member hasn't completed.
 */
export function scheduleStatus(milestones, progress, chaptered, nowMs) {
  if (!hasSchedule(milestones)) return { status: 'none', behindBy: 0 };
  const list = milestones || [];
  const last = list.length ? Math.max(...list.map(m => m.position)) : -1;
  const pos = memberSchedulePosition(list, progress, chaptered);
  if ((progress && progress.finished) || (pos >= 0 && pos >= last)) {
    return { status: 'done', behindBy: 0 };
  }
  const behindBy = list.filter(m =>
    typeof m.dueAt === 'number' && m.dueAt <= nowMs && m.position > pos).length;
  return behindBy > 0
    ? { status: 'behind', behindBy }
    : { status: 'on-track', behindBy: 0 };
}

/**
 * Save the read's schedule: dueAts is an array of epoch millis (or null)
 * aligned with the milestones sorted by position. Null clears a date.
 * Rewrites the milestones array in place on the read doc — no parallel
 * schedule structure — and stamps scheduleUpdatedAt.
 */
export async function setReadSchedule(db, clubId, readId, dueAts) {
  try {
    const readRef = doc(db, col('clubs'), clubId, 'reads', readId);
    const snap = await getDoc(readRef);
    if (!snap.exists()) return { success: false, error: 'Read not found.' };
    const ordered = [...(snap.data().milestones || [])].sort((a, b) => a.position - b.position);
    const milestones = ordered.map((m, i) => {
      const next = { ...m };
      const due = Array.isArray(dueAts) ? dueAts[i] : null;
      if (typeof due === 'number' && Number.isFinite(due)) next.dueAt = due;
      else delete next.dueAt;
      return next;
    });
    await updateDoc(readRef, { milestones, scheduleUpdatedAt: serverTimestamp() });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the host, moderator, or site moderator role' }) };
  } finally {
    reportGate('club.setSchedule', { clubId }); // Phase 1 shadow — fire-and-forget
  }
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
 *
 * `coverHref` comes back as a FULL URL (covers live in R2, not on this site —
 * see covers-base.js). Callers put it straight in an <img src>, and the value
 * is what gets stored in Firestore for club reads / favourites, so it must be
 * absolute or those records break the moment the page moves.
 *
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
      coverHref: coverUrl(r.cover_href || ''),
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
    return { success: false, error: describeActionError(e) };
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
    // Ratings: best-effort only. While the read's ratings are still blind,
    // firestore.rules makes the subcollection unreadable to EVERYONE (see
    // the "Blind ratings" section below), so getDocs() throws
    // permission-denied here exactly as it would for any other caller.
    // Orphaned rating docs under a deleted read are harmless (unreachable
    // through the UI, still individually deletable) — don't let cleanup
    // block removing the read itself.
    try {
      const ratingsSnap = await getDocs(collection(db, col('clubs'), clubId, 'reads', readId, 'ratings'));
      for (const d of ratingsSnap.docs) {
        await deleteDoc(doc(db, col('clubs'), clubId, 'reads', readId, 'ratings', d.id));
      }
    } catch { /* blind ratings unreadable pre-reveal; leave orphaned docs */ }
    await deleteDoc(readRef);
    await refreshClubAvatar(db, clubId);
    return { success: true };
  } catch (e) {
    return {
      success: false,
      error: describeActionError(e, {
        fallback: `Remove failed: ${e.message} — try a hard refresh and sign in again if this persists.`,
      }),
    };
  } finally {
    reportGate('read.remove', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

/**
 * Rename a read's label ("Current read" / "Side read" by default) —
 * free-form, member-editable, becomes the card's header title.
 */
export async function updateReadLabel(db, clubId, readId, label) {
  const trimmed = (label || '').trim();
  if (trimmed.length > 40) return { success: false, error: 'Labels must be 40 characters or fewer.' };
  try {
    await updateDoc(doc(db, col('clubs'), clubId, 'reads', readId), { slotLabel: trimmed });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
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
    return {
      success: false,
      error: describeActionError(e, {
        need: 'the club host/moderator role, or site admin',
        fallback: `Finish failed: ${e.message} — try a hard refresh and sign in again if this persists.`,
      }),
    };
  } finally {
    reportGate('read.finish', { clubId }); // Phase 1 shadow — fire-and-forget
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
    return { success: false, error: describeActionError(e) };
  }
}

/** Remove a TBR suggestion. */
export async function removeTbrItem(db, clubId, itemId) {
  try {
    await deleteDoc(doc(db, col('clubs'), clubId, 'tbr', itemId));
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
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
    return { success: false, error: describeActionError(e) };
  }
}

// ==================== Comments ====================

/** Display identity for AI-posted starter questions ("Post this"). */
export const GABI = { displayName: 'GABI', slug: 'gabi' };

/**
 * Add a comment (or a reply when parentId is set) to a milestone discussion.
 * input.asBot posts under the GABI identity (session still required — it
 * records who triggered the post in postedBySlug).
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
      partIndex: typeof input.partIndex === 'number' && input.partIndex >= 0 ? input.partIndex : null,
      displayName: input.asBot ? GABI.displayName : session.displayName,
      slug: input.asBot ? GABI.slug : slugifyName(session.displayName),
      isBot: !!input.asBot,
      postedBySlug: input.asBot ? slugifyName(session.displayName) : null,
      text,
      reactions: {},
      isPinned: false,
      createdAt: serverTimestamp(),
    });
    await updateDoc(doc(db, col('clubs'), clubId, 'reads', readId), { commentCount: increment(1) });
    return { success: true, commentId: commentRef.id };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
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
    return { success: false, error: describeActionError(e) };
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
    return { success: false, error: describeActionError(e, { need: 'the host or moderator role' }) };
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
    return { success: false, error: describeActionError(e) };
  }
}

/** All quotes for a read, oldest first. */
export async function getQuotes(db, clubId, readId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'reads', readId, 'quotes'));
  return snap.docs
    .map(d => ({ id: d.id, ...d.data() }))
    .sort((a, b) => (a.createdAt?.seconds || 0) - (b.createdAt?.seconds || 0));
}

/**
 * Delete a quote (saver or host/moderator — enforced in the UI).
 *
 * `opts.asModerator` marks a MODERATION delete (someone else's quote) —
 * callers who know the viewer is not the saver pass true. Only that case is
 * a worker-bound surface in the auth migration, so only that case reports
 * to the Phase 1 shadow; deleting your own quote stays browser-direct and
 * unreported (a self-delete report would pollute the would_deny soak).
 */
export async function deleteQuote(db, clubId, readId, quoteId, opts) {
  try {
    await deleteDoc(doc(db, col('clubs'), clubId, 'reads', readId, 'quotes', quoteId));
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'to be the person who saved it, or hold the host/moderator role' }) };
  } finally {
    if (opts && opts.asModerator) {
      reportGate('quote.modDelete', { clubId }); // Phase 1 shadow — fire-and-forget
    }
  }
}

/**
 * Delete a comment (author or host/moderator — enforced in the UI).
 * `opts.asModerator` — same contract as deleteQuote above: only a
 * moderation delete (not the author's own) reports to the Phase 1 shadow.
 */
export async function deleteComment(db, clubId, readId, commentId, opts) {
  try {
    await deleteDoc(doc(db, col('clubs'), clubId, 'reads', readId, 'comments', commentId));
    await updateDoc(doc(db, col('clubs'), clubId, 'reads', readId), { commentCount: increment(-1) });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'to be the comment author, or hold the host/moderator role' }) };
  } finally {
    if (opts && opts.asModerator) {
      reportGate('comment.modDelete', { clubId }); // Phase 1 shadow — fire-and-forget
    }
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
 *
 * Also grows the pace-graph history (see "Buddy-read pace graph" below):
 * reads the prior doc to append a point rather than clobbering it, since
 * this is a full setDoc. The extra getDoc costs one read per progress
 * update — the same trade the poll-vote / rating writes already make for
 * their own guard reads.
 */
export async function setProgress(db, clubId, readId, position, session, finished = false) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to track progress.' };
  }
  try {
    const slug = slugifyName(session.displayName);
    const ref = doc(db, col('clubs'), clubId, 'reads', readId, 'progress', slug);
    const prevSnap = await getDoc(ref);
    const history = appendPaceHistory(
      prevSnap.exists() ? prevSnap.data().history : null,
      buildHistoryPoint(position, finished));
    await setDoc(ref, {
      displayName: session.displayName,
      milestonePosition: position,
      finished,
      updatedAt: serverTimestamp(),
      history,
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/**
 * Record chapter-level progress ("I'm at chapter N"). -1 = not started.
 * Used for reads whose book has chapter data; drives per-comment spoilers
 * and chapter-mapped section locks. Grows pace-graph history — see setProgress.
 */
export async function setChapterProgress(db, clubId, readId, chapterIndex, session, finished = false) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to track progress.' };
  }
  try {
    const slug = slugifyName(session.displayName);
    const ref = doc(db, col('clubs'), clubId, 'reads', readId, 'progress', slug);
    const prevSnap = await getDoc(ref);
    const history = appendPaceHistory(
      prevSnap.exists() ? prevSnap.data().history : null,
      buildHistoryPoint(chapterIndex, finished));
    await setDoc(ref, {
      displayName: session.displayName,
      chapterIndex,
      finished,
      updatedAt: serverTimestamp(),
      history,
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/** Fetch every member's progress for a read. */
export async function getProgressAll(db, clubId, readId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'reads', readId, 'progress'));
  return snap.docs.map(d => ({ slug: d.id, ...d.data() }));
}

/** Fetch one member's progress doc for a read (null if none). */
export async function getMyProgress(db, clubId, readId, slug) {
  try {
    const snap = await getDoc(doc(db, col('clubs'), clubId, 'reads', readId, 'progress', slug));
    return snap.exists() ? { slug, ...snap.data() } : null;
  } catch { return null; }
}

// ==================== Buddy-read pace graph (backlog #6) ====================
//
// Progress-over-time lines, one per member, feature-gated by clubs.js
// FEATURE_DEFAULTS.paceGraph (default OFF, checkbox in club.html's Edit
// Club modal). The progress subcollection above stores ONLY a member's
// CURRENT position — setProgress/setChapterProgress are full setDoc calls,
// so the prior value (and its updatedAt) is gone the instant a new one
// lands. No time series exists there today.
//
// Shape decision: rather than add a brand-new history subcollection (its
// own rules block on both `clubs` and `clubs_dev`, its own deploy + REST
// smoke-test cycle, its own read/write pair per data point), the SAME
// progress doc grows one more field: an append-only `history` array,
// `{ position, finished, at }[]`, written alongside milestonePosition/
// chapterIndex in the same setDoc (see setProgress/setChapterProgress
// above). This is the "needs NO backfill and degrades gracefully" shape the
// brief asks for: a progress doc written before this feature shipped simply
// has no `history` field (`appendPaceHistory` treats that as an empty
// list), so the graph starts accumulating from the first progress write
// after paceGraph existed — exactly "starts from feature-enable day", no
// migration required.
//
// ⚠️ NO RULES CHANGE was needed. `firestore.rules`' progress write rule
// (both lanes) only requires `milestonePosition` or `chapterIndex` to be a
// number and places no restriction on any other field on the doc — so
// `history` rides the exact same open, member-level write every other
// progress field (displayName, finished, updatedAt) already uses. Capping
// growth (MAX_PACE_HISTORY_POINTS) is enforced client-side only, same trust
// tier as everything else in this open-write model (poll votes, ratings,
// rsvps: "no rule can bind a display name to a person").
//
// Points are recorded on every REAL change only (see appendPaceHistory's
// dedupe) — repeatedly saving the same spot doesn't grow the array, so in
// practice most reads stay far under the cap without ever thinning.
//
// Spoiler care: the graph plots POSITIONS on a numeric/percent axis only
// (see paceAxisLabel) — it never renders a milestone/section TITLE, on
// either axis or in tooltips, so there is no new spoiler surface and no
// title-locking convention (the polls/comments pattern) is needed at all —
// this is the "safest" option the brief names, taken deliberately instead
// of building a locking mechanism for an axis that doesn't need one.

export const MAX_PACE_HISTORY_POINTS = 100;

/** One pace-history point. `at` defaults to Date.now() (epoch millis, the
 * same client-stamped-instant convention as dueAt/meetingAt/nextMeetingAt —
 * serverTimestamp() sentinels aren't usable inside array elements). */
export function buildHistoryPoint(position, finished, nowMs = Date.now()) {
  return { position, finished: !!finished, at: nowMs };
}

/**
 * Append a pace-history point. A write that repeats the immediately-prior
 * point's position + finished state is a no-op (no new point) — re-saving
 * the same spot shouldn't grow the array. Thins down to the cap afterward.
 */
export function appendPaceHistory(history, point, maxPoints = MAX_PACE_HISTORY_POINTS) {
  const list = Array.isArray(history) ? history : [];
  const lastPt = list[list.length - 1];
  if (lastPt && lastPt.position === point.position && !!lastPt.finished === !!point.finished) {
    return list;
  }
  return thinPaceHistory([...list, point], maxPoints);
}

/**
 * Cap a history array at `maxPoints`, thinning the OLDEST points first.
 * Repeatedly drops the entry at index 1 (never index 0) until back at the
 * cap, so the very FIRST point ever recorded always survives as a
 * start-of-history anchor, and the most recent (maxPoints - 1) points stay
 * fully dense — recent pace matters most for "are we on track today"; the
 * far past only needs to show where a member started, not every step.
 */
export function thinPaceHistory(history, maxPoints = MAX_PACE_HISTORY_POINTS) {
  const list = Array.isArray(history) ? history : [];
  if (list.length <= maxPoints) return list;
  const out = [...list];
  while (out.length > maxPoints) out.splice(1, 1);
  return out;
}

/**
 * A history point's position normalized onto the SAME 0..last milestone
 * scale memberSchedulePosition (and the reading schedule) already use.
 * Reuses that function directly by rebuilding the progress-doc shape it
 * expects — one stored raw `position` value covers both the chaptered and
 * milestone-based read cases, since only one of chapterIndex/milestonePosition
 * is ever consulted (memberSchedulePosition picks by `chaptered`).
 */
export function pacePosition(point, milestones, chaptered) {
  return memberSchedulePosition(
    milestones,
    { chapterIndex: point.position, milestonePosition: point.position, finished: point.finished },
    chaptered);
}

/**
 * One member's pace-graph line: history points normalized + sorted by time,
 * with "not started" (-1) points dropped (the graph plots real forward
 * movement, not its absence) and consecutive duplicate positions collapsed.
 * @returns {Array<{atMs: number, position: number}>}
 */
export function paceSeriesForMember(history, milestones, chaptered) {
  const sorted = (Array.isArray(history) ? [...history] : []).sort((a, b) => a.at - b.at);
  const out = [];
  for (const pt of sorted) {
    const position = pacePosition(pt, milestones, chaptered);
    if (position < 0) continue;
    const prev = out[out.length - 1];
    if (prev && prev.position === position) continue;
    out.push({ atMs: pt.at, position });
  }
  return out;
}

/**
 * The schedule's expected-pace reference line: one {atMs, position} per
 * dated milestone, in position order. Callers gate on hasSchedule(milestones)
 * before drawing it — empty here just means "nothing dated".
 * @returns {Array<{atMs: number, position: number}>}
 */
export function expectedPaceLine(milestones) {
  return (milestones || [])
    .filter(m => typeof m.dueAt === 'number' && Number.isFinite(m.dueAt))
    .sort((a, b) => a.position - b.position)
    .map(m => ({ atMs: m.dueAt, position: m.position }));
}

/** The graph's y-axis ceiling: the highest milestone position. */
export function paceAxisMax(milestones) {
  const list = milestones || [];
  return list.length ? Math.max(...list.map(m => m.position)) : 0;
}

/** Numeric/percent y-axis tick label — NEVER a milestone title (spoiler care). */
export function paceAxisLabel(position, maxPosition) {
  if (!(maxPosition > 0)) return '0%';
  return `${Math.round((Math.max(0, position) / maxPosition) * 100)}%`;
}

/** True when at least one member has at least one plottable pace point. */
export function hasPaceData(seriesByMember) {
  return Object.values(seriesByMember || {}).some(s => Array.isArray(s) && s.length > 0);
}

/**
 * The graph's x-axis end instant: "now" for an active read, so the line
 * keeps drawing toward today; the read's finish time (falling back to its
 * last data point) for a finished/abandoned read, so the graph FREEZES
 * instead of drifting toward an ever-later "today" that no longer means
 * anything once the read is closed.
 */
export function paceAxisEndMs(isActive, finishedAtMs, seriesTimesMs, nowMs = Date.now()) {
  if (isActive) return nowMs;
  if (typeof finishedAtMs === 'number' && Number.isFinite(finishedAtMs)) return finishedAtMs;
  const times = seriesTimesMs || [];
  return times.length ? Math.max(...times) : nowMs;
}

/**
 * Deterministic member -> {color, dashed} assignment for the pace lines.
 * `slugsSorted` should be in a stable, caller-chosen order (e.g. by display
 * name) so the mapping doesn't shuffle between renders. The 8 colors are
 * this estate's validated categorical palette (dataviz skill default
 * instance — fixed hue order, CVD-checked); legible up to 8 distinct hues.
 * Members 9-12 recycle hues 1-4 with a dashed stroke as a second, non-color
 * channel, so a repeated hue is still visually distinct from its twin —
 * identity is never color-alone here anyway, since the legend always shows
 * the name too.
 */
export const PACE_PALETTE = [
  'var(--pace-c1)', 'var(--pace-c2)', 'var(--pace-c3)', 'var(--pace-c4)',
  'var(--pace-c5)', 'var(--pace-c6)', 'var(--pace-c7)', 'var(--pace-c8)',
];

export function assignPaceColors(slugsSorted) {
  const map = {};
  (slugsSorted || []).forEach((slug, i) => {
    map[slug] = {
      color: PACE_PALETTE[i % PACE_PALETTE.length],
      dashed: i >= PACE_PALETTE.length,
    };
  });
  return map;
}

/**
 * Assemble everything the read page needs to draw the Pace panel from live
 * progress docs + the read's milestones, in one pure call — series per
 * member, the expected-schedule reference line, and the shared time/
 * position domain — so club-read.html's rendering code only turns numbers
 * into hand-built SVG.
 * @param {Array<{slug: string, history?: Array}>} progressAll
 * @param {Array} milestones
 * @param {boolean} chaptered
 * @param {{status: string, finishedAtMs: number|null}} readMeta
 * @param {number} [nowMs]
 */
export function computePaceGraphModel(progressAll, milestones, chaptered, readMeta, nowMs = Date.now()) {
  const maxPosition = paceAxisMax(milestones);
  const seriesByMember = {};
  for (const p of progressAll || []) {
    seriesByMember[p.slug] = paceSeriesForMember(p.history, milestones, chaptered);
  }
  const expected = hasSchedule(milestones) ? expectedPaceLine(milestones) : [];
  const isActive = !readMeta || readMeta.status === 'active';
  const allTimesMs = [
    ...Object.values(seriesByMember).flat().map(pt => pt.atMs),
    ...expected.map(pt => pt.atMs),
  ];
  const endMs = paceAxisEndMs(isActive, readMeta && readMeta.finishedAtMs, allTimesMs, nowMs);
  const startMs = allTimesMs.length ? Math.min(...allTimesMs) : endMs;
  return {
    empty: !hasPaceData(seriesByMember),
    maxPosition,
    seriesByMember,
    expected,
    startMs,
    endMs,
    frozen: !isActive,
    todayMs: isActive ? nowMs : null,
  };
}

/** Linear time -> x pixel scale for the hand-built SVG. */
export function paceScaleX(atMs, minMs, maxMs, width) {
  if (!(maxMs > minMs)) return 0;
  return ((atMs - minMs) / (maxMs - minMs)) * width;
}

/** Linear position -> y pixel scale (0 at the bottom) for the hand-built SVG. */
export function paceScaleY(position, maxPosition, height) {
  if (!(maxPosition > 0)) return height;
  return height - (Math.max(0, position) / maxPosition) * height;
}

// ==================== Club polls (backlog #3) ====================
//
// clubs/{id}/polls/{pollId}: free-form question + 2-10 options, optionally
// tagged to a section of a specific read (readId + milestoneId +
// milestonePosition, the same milestone vocabulary as the reading schedule).
// Two creation surfaces, split deliberately:
//   - club.html creates UNTAGGED polls (readId/milestoneId/milestonePosition
//     all null) — club-wide, no book context, voted on directly on the club
//     page.
//   - club-read.html creates polls TAGGED to one of that read's sections —
//     these render ONLY on the read page, near their section, and are
//     spoiler-gated exactly like milestone comments (see isPollLocked below,
//     which mirrors isMilestoneLocked / memberSchedulePosition: reuse the
//     SAME position the reading schedule already computes, don't invent a
//     second one). Untagged polls are never spoiler-gated.
// Feature-gated per club: clubs.js FEATURE_DEFAULTS key `polls` (OFF by
// default, one Edit Club checkbox, same pattern as readingSchedule).
//
// Data shape is deliberately the future announcement-engine contract too
// (see docs/TODO.md "Club polls" backlog note): a poll closing is a natural
// announceable event, same family as read started/finished.
//
// votes subcollection (clubs/{id}/polls/{pollId}/votes/{memberSlug}): one
// doc per member, doc id = slug (member-identity convention used everywhere
// else in this file — progress, TBR voterSlugs, comments). setDoc upsert
// makes a vote changeable while the poll is open, per the spec ("changeable
// while open"); rules refuse writes once a poll is closed (see
// firestore.rules pollIsOpen()).
//
// ---- 'next book' poll TYPE (backlog #3b) -----------------------------
// A poll gains a `type` field: 'freeform' (default — omitted on every poll
// created before this shipped, so `poll.type` is undefined on legacy docs;
// every reader here and in firestore.rules treats missing/undefined the
// same as 'freeform', never a validation failure) or 'nextBook'. This is a
// poll TYPE, not a new feature — still gated behind the one `polls` toggle.
// A nextBook poll's `options` are book refs `{title, author, coverHref}`
// (picked via type-ahead against the site catalog — loadCatalogBooks above
// is the exact picker data source club.html's Start-a-Read and Suggest-Book
// modals already use) instead of bare strings. No separate "book id" field
// is stored — same precedent as TBR items and read docs elsewhere in this
// file, which also carry title/author/coverHref only; a stable id is
// derived on demand via reviews.js bookIdFromTitle() wherever one is
// needed. Deliberately club-wide only (created from club.html; the type
// choice is hidden on club-read.html's tagged-poll composer — see the
// comment there for why) since "what should we read next" doesn't belong
// to any one section of a book already in progress, the same reasoning
// that keeps the TBR list on the club page rather than the read page.
// Voting/closing/results/spoiler mechanics are all UNCHANGED from #3 —
// tallyPollVotes/myPollVote/pollResultsVisible/isPollLocked work on option
// INDEX regardless of what shape options[i] is.

export const MIN_POLL_OPTIONS = 2;
export const MAX_POLL_OPTIONS = 10;
export const MAX_POLL_QUESTION_LENGTH = 200;
export const MAX_POLL_OPTION_LENGTH = 80;
export const MAX_POLL_BOOK_TITLE_LENGTH = 200;
export const MAX_POLL_BOOK_AUTHOR_LENGTH = 200;
export const POLL_TYPE_FREEFORM = 'freeform';
export const POLL_TYPE_NEXT_BOOK = 'nextBook';

/** Validate a poll question. Required, up to MAX_POLL_QUESTION_LENGTH chars. */
export function validatePollQuestion(question) {
  const trimmed = (question || '').trim();
  if (!trimmed) return { valid: false, error: 'Add a poll question.' };
  if (trimmed.length > MAX_POLL_QUESTION_LENGTH) {
    return { valid: false, error: `Poll question must be ${MAX_POLL_QUESTION_LENGTH} characters or fewer.` };
  }
  return { valid: true };
}

/**
 * Validate + clean poll options: 2-10 non-blank entries, each up to
 * MAX_POLL_OPTION_LENGTH chars. Blank entries (empty option rows in the
 * composer) are dropped before the count check.
 * @returns {{ valid: boolean, options?: string[], error?: string }}
 */
export function validatePollOptions(options) {
  const cleaned = (options || []).map(o => (o || '').trim()).filter(Boolean);
  if (cleaned.length < MIN_POLL_OPTIONS) {
    return { valid: false, error: `Add at least ${MIN_POLL_OPTIONS} options.` };
  }
  if (cleaned.length > MAX_POLL_OPTIONS) {
    return { valid: false, error: `At most ${MAX_POLL_OPTIONS} options.` };
  }
  if (cleaned.some(o => o.length > MAX_POLL_OPTION_LENGTH)) {
    return { valid: false, error: `Each option must be ${MAX_POLL_OPTION_LENGTH} characters or fewer.` };
  }
  return { valid: true, options: cleaned };
}

/**
 * Validate + clean 'next book' poll options: 2-10 book refs, each needing a
 * non-blank title (author/coverHref are optional — a catalog entry can lack
 * either). Mirrors validatePollOptions' count checks; entries with a blank
 * title are dropped before counting, same "blank rows don't count" rule as
 * the free-form composer's empty option inputs. Per-field length caps are
 * separate constants (MAX_POLL_BOOK_TITLE_LENGTH/MAX_POLL_BOOK_AUTHOR_LENGTH)
 * since book titles/authors routinely exceed MAX_POLL_OPTION_LENGTH (80,
 * sized for a short free-form answer, not "The Fellowship of the Ring").
 * @returns {{ valid: boolean, options?: Array<{title,author,coverHref}>, error?: string }}
 */
export function validateNextBookOptions(options) {
  const cleaned = (options || [])
    .filter(o => o && (o.title || '').trim())
    .map(o => ({
      title: (o.title || '').trim(),
      author: (o.author || '').trim(),
      coverHref: (o.coverHref || '').trim(),
    }));
  if (cleaned.length < MIN_POLL_OPTIONS) {
    return { valid: false, error: `Pick at least ${MIN_POLL_OPTIONS} books.` };
  }
  if (cleaned.length > MAX_POLL_OPTIONS) {
    return { valid: false, error: `At most ${MAX_POLL_OPTIONS} books.` };
  }
  if (cleaned.some(o => o.title.length > MAX_POLL_BOOK_TITLE_LENGTH || o.author.length > MAX_POLL_BOOK_AUTHOR_LENGTH)) {
    return { valid: false, error: 'A book title or author is too long.' };
  }
  return { valid: true, options: cleaned };
}

/** True when a poll is the 'next book' type. Missing/undefined `type` (every
 * poll created before backlog #3b) counts as free-form, never next-book. */
export function isNextBookPoll(poll) {
  return !!poll && poll.type === POLL_TYPE_NEXT_BOOK;
}

/**
 * Render one poll option's inner (label) HTML — a plain escaped string for
 * a free-form poll, or a cover-thumbnail + title/author row for a next-book
 * poll. The one function both club.html and club-read.html call so the two
 * pages can never drift on how a book-ref option renders. Caller supplies
 * the surrounding <button class="poll-opt"> / <div class="poll-result">
 * element and its vote-count chrome; this only produces the label content.
 */
export function pollOptionContentHtml(poll, opt) {
  if (!isNextBookPoll(poll)) return escapeHtmlText(typeof opt === 'string' ? opt : '');
  const title = escapeHtmlText((opt && opt.title) || '');
  const author = escapeHtmlText((opt && opt.author) || '');
  const cover = opt && opt.coverHref
    ? `<img class="poll-opt-cover" src="${escapeHtmlText(opt.coverHref)}" alt="" onerror="this.remove()">`
    : '';
  return `<span class="poll-opt-book">${cover}<span class="poll-opt-book-text">`
    + `<span class="poll-opt-book-title">${title}</span>`
    + (author ? `<span class="poll-opt-book-author"> — ${author}</span>` : '')
    + `</span></span>`;
}

/**
 * Winning option index for a poll, or null when there are no votes yet, or
 * when two or more options are tied for the lead (an honest tie shows no
 * single winner rather than an arbitrary pick — same "don't fake certainty"
 * instinct as the blind-ratings design). Used for the next-book poll winner
 * badge/start-a-read affordance on a CLOSED poll; the caller decides when
 * it's meaningful to call this (an open poll has a "leader" but not yet a
 * "winner").
 * @returns {number|null}
 */
export function pollWinnerIndex(options, votes) {
  const { counts, total } = tallyPollVotes(options, votes);
  if (total === 0) return null;
  const max = Math.max(...counts);
  const leaders = [];
  counts.forEach((c, i) => { if (c === max) leaders.push(i); });
  return leaders.length === 1 ? leaders[0] : null;
}

/**
 * Spoiler-gate predicate for a tagged poll, mirroring isMilestoneLocked:
 * an untagged poll (milestonePosition null/undefined) is never locked; a
 * tagged one is locked while the viewer's position (from
 * memberSchedulePosition — the SAME number the reading schedule uses, -1 =
 * not started) hasn't reached it.
 */
export function isPollLocked(poll, myPosition) {
  const pos = poll && poll.milestonePosition;
  if (pos === null || pos === undefined) return false;
  return pos > (typeof myPosition === 'number' ? myPosition : -1);
}

/**
 * Tally votes per option index.
 * @returns {{ counts: number[], total: number }}
 */
export function tallyPollVotes(options, votes) {
  const counts = (options || []).map(() => 0);
  for (const v of votes || []) {
    if (typeof v.optionIndex === 'number' && v.optionIndex >= 0 && v.optionIndex < counts.length) {
      counts[v.optionIndex]++;
    }
  }
  const total = counts.reduce((a, b) => a + b, 0);
  return { counts, total };
}

/** The caller's own vote (option index), or null if they haven't voted. */
export function myPollVote(votes, slug) {
  const mine = (votes || []).find(v => v.slug === slug);
  return mine && typeof mine.optionIndex === 'number' ? mine.optionIndex : null;
}

/**
 * Results visibility: closed polls show results to everyone ("freezes it
 * and shows results to all"); an open poll shows live counts only after the
 * viewer has voted, or always for managers.
 */
export function pollResultsVisible(poll, hasVoted, isManager) {
  if (poll && poll.status === 'closed') return true;
  return !!hasVoted || !!isManager;
}

/**
 * Create a poll. `input.readId`/`milestoneId`/`milestonePosition` are all
 * optional together (an untagged, club-wide poll); when tagging a section,
 * pass all three so the spoiler gate and read-page placement both work.
 * `input.type` (POLL_TYPE_FREEFORM default, or POLL_TYPE_NEXT_BOOK) picks
 * which option validator/shape applies — free-form strings, or next-book
 * refs `{title, author, coverHref}` from the catalog type-ahead.
 */
export async function createPoll(db, clubId, input, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to create a poll.' };
  }
  const type = input.type === POLL_TYPE_NEXT_BOOK ? POLL_TYPE_NEXT_BOOK : POLL_TYPE_FREEFORM;
  const qCheck = validatePollQuestion(input.question);
  if (!qCheck.valid) return { success: false, error: qCheck.error };
  const oCheck = type === POLL_TYPE_NEXT_BOOK
    ? validateNextBookOptions(input.options)
    : validatePollOptions(input.options);
  if (!oCheck.valid) return { success: false, error: oCheck.error };
  try {
    const ref = doc(collection(db, col('clubs'), clubId, 'polls'));
    await setDoc(ref, {
      type,
      question: input.question.trim(),
      options: oCheck.options,
      readId: input.readId || null,
      milestoneId: input.milestoneId || null,
      milestonePosition: typeof input.milestonePosition === 'number' ? input.milestonePosition : null,
      status: 'open',
      createdBy: session.displayName,
      createdBySlug: slugifyName(session.displayName),
      createdAt: serverTimestamp(),
      closedAt: null,
    });
    return { success: true, pollId: ref.id };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  } finally {
    reportGate('poll.create', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

/** Fetch every poll for a club (untagged + every read's tagged polls). */
export async function getPolls(db, clubId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'polls'));
  return snap.docs.map(d => ({ id: d.id, ...d.data() }));
}

/** Fetch a single poll. Returns null if missing. */
export async function getPoll(db, clubId, pollId) {
  const snap = await getDoc(doc(db, col('clubs'), clubId, 'polls', pollId));
  return snap.exists() ? { id: snap.id, ...snap.data() } : null;
}

/**
 * Open or close a poll (manager action, enforced in the UI + rules on
 * claimed clubs). Closing freezes it — votes are refused by rules once
 * status is 'closed' — and stamps closedAt for the future announcements
 * engine.
 */
export async function setPollStatus(db, clubId, pollId, status) {
  if (status !== 'open' && status !== 'closed') return { success: false, error: 'Invalid status.' };
  try {
    await updateDoc(doc(db, col('clubs'), clubId, 'polls', pollId), {
      status,
      closedAt: status === 'closed' ? serverTimestamp() : null,
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the host or moderator role' }) };
  } finally {
    reportGate('poll.setStatus', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

/** Delete a poll and its votes (manager action). */
export async function deletePoll(db, clubId, pollId) {
  try {
    const votesSnap = await getDocs(collection(db, col('clubs'), clubId, 'polls', pollId, 'votes'));
    for (const v of votesSnap.docs) {
      await deleteDoc(doc(db, col('clubs'), clubId, 'polls', pollId, 'votes', v.id));
    }
    await deleteDoc(doc(db, col('clubs'), clubId, 'polls', pollId));
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the host or moderator role' }) };
  } finally {
    reportGate('poll.delete', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

/**
 * Cast (or change) the caller's vote. One doc per member — setDoc upsert
 * makes it changeable while the poll is open; rules refuse the write once
 * the poll is closed.
 */
export async function castVote(db, clubId, pollId, optionIndex, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to vote.' };
  }
  if (typeof optionIndex !== 'number' || optionIndex < 0) {
    return { success: false, error: 'Pick an option.' };
  }
  try {
    const slug = slugifyName(session.displayName);
    await setDoc(doc(db, col('clubs'), clubId, 'polls', pollId, 'votes', slug), {
      displayName: session.displayName,
      optionIndex,
      updatedAt: serverTimestamp(),
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/** Fetch every vote on a poll. */
export async function getPollVotes(db, clubId, pollId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'polls', pollId, 'votes'));
  return snap.docs.map(d => ({ slug: d.id, ...d.data() }));
}

// ==================== Blind ratings reveal (backlog #4) ====================
//
// clubs/{id}/reads/{readId}/ratings/{memberSlug}: one doc per member (the
// progress/votes doc-id-is-slug convention), { displayName, rating (0.5-5,
// the site's existing star scale — see reviews.js renderStars), comment
// (optional, one line), createdAt, updatedAt }. Feature-gated per club:
// FEATURE_DEFAULTS key `blindRatings` in clubs.js (OFF by default, one Edit
// Club checkbox, same pattern as readingSchedule/polls).
//
// ⚠️ BLIND-STORAGE DESIGN — read this before touching the rules block.
// This site's member identity is a slugified display name with NO auth
// binding for ordinary members (only managerUids carries real uids, and
// only for claimed clubs). That rules out the tempting "allow get where
// doc id == caller's own slug" shape: the doc id in a Firestore request is
// just whatever path the CLIENT asks for, and any browser that knows (or
// can trivially compute via slugifyName) another member's display name can
// request that exact same path — there is nothing rules could check to
// tell the true owner from a forger. A per-slug read rule here would not
// be "a bit forgeable", it would be no blind window at all: anyone who can
// see the members list already has every other member's slug for free.
//
// So the chosen design is the simplest honest one: ratings are
// UNREADABLE TO EVERYONE, including their own author, until the read's
// ratingsRevealed flag flips (firestore.rules `ratingsRevealed()`, same
// `allow read: if false`-style shape as clubs/{id}/settings). To let a
// member see THEIR OWN rating during the blind window without a Firestore
// read, rateBook() mirrors it into localStorage at write time
// (storeMyRatingLocally / getMyStoredRating below); the UI reads that
// mirror while blind and switches to the real subcollection once revealed.
//
// RESIDUAL GAP, stated plainly: the localStorage mirror is per-device. A
// member who rates on their phone and opens the read on a laptop during
// the SAME blind window will not see "you already rated" there — nothing
// server-side can hand it back without breaking the blind guarantee for
// everyone else. This is judged an acceptable trade for a real (not
// merely CSS-hidden) blind window; it self-heals at reveal, when the real
// data becomes readable everywhere.
//
// "N members have rated" is `ratingCount` on the read doc itself — open
// write, incremented once per NEW rating, same pattern as commentCount
// (bumped by every commenter, never rules-gated). It stays visible through
// the blind window without exposing any individual value, matching the
// spec's "count only" requirement.
//
// ⚠️ IMPLEMENTATION GOTCHA (hit and fixed while building this): rateBook()
// CANNOT `get()` the caller's own rating doc to check "have I already
// rated?" before deciding whether to bump ratingCount — that get() is
// itself a read of the ratings subcollection, and the rule above denies
// ALL reads while blind, including to the write's own author. An earlier
// version wrapped the write in a transaction that opened with exactly that
// get() and failed every blind-window rating with PERMISSION_DENIED. The
// fix: "have I rated before" is answered from the localStorage mirror
// (client-side truth, no read involved) instead of Firestore. A rater on a
// SECOND device without a local mirror yet will bump ratingCount again —
// an accepted extra edge in the same "open, unenforced counter" class as
// commentCount (nothing here stops anyone writing ratingCount to any
// number directly either).
//
// After reveal, ratings keep landing (rules allow create/update at any
// time) but "arrive visibly" per the spec: createdAt is stamped fresh with
// serverTimestamp() on every write (create AND edit — the same
// no-pre-read constraint rules out fetching an original value to keep),
// so isRatingAfterReveal reflects "this rating's current value was posted
// or last edited after the reveal", not literally "first ever submitted
// after reveal". A member who rated blind and never touches it again
// keeps their pre-reveal timestamp and no badge; editing after reveal
// picks one up — read as "this is what changed since the reveal moment."

export const MIN_RATING = 0.5;
export const MAX_RATING = 5;
export const RATING_STEP = 0.5;
export const MAX_RATING_COMMENT_LENGTH = 140;

/** Validate a star rating: 0.5-5 in half-star steps (this site's existing
 * review scale — see reviews.js submitReview). */
export function validateRatingValue(rating) {
  if (typeof rating !== 'number' || !Number.isFinite(rating)) {
    return { valid: false, error: 'Pick a star rating.' };
  }
  if (rating < MIN_RATING || rating > MAX_RATING) {
    return { valid: false, error: `Rating must be between ${MIN_RATING} and ${MAX_RATING} stars.` };
  }
  const steps = rating / RATING_STEP;
  if (Math.abs(steps - Math.round(steps)) > 1e-9) {
    return { valid: false, error: 'Ratings are in half-star increments.' };
  }
  return { valid: true };
}

/** Validate the optional one-line comment. Trims and caps length. */
export function validateRatingComment(comment) {
  const trimmed = (comment || '').trim();
  if (trimmed.length > MAX_RATING_COMMENT_LENGTH) {
    return { valid: false, error: `Comments must be ${MAX_RATING_COMMENT_LENGTH} characters or fewer.` };
  }
  return { valid: true, comment: trimmed };
}

/** True once a manager has flipped the read's ratings visible to everyone. */
export function ratingsAreRevealed(read) {
  return !!(read && read.ratingsRevealed === true);
}

/**
 * Tally the average + count from a list of rating docs. Average is rounded
 * to 1 decimal, same convention as reviews.js computeAverageRating.
 * @returns {{ average: number, count: number }}
 */
export function tallyRatings(ratings) {
  const list = (ratings || []).filter(r => typeof r.rating === 'number');
  if (!list.length) return { average: 0, count: 0 };
  const sum = list.reduce((acc, r) => acc + r.rating, 0);
  return { average: Math.round((sum / list.length) * 10) / 10, count: list.length };
}

/**
 * A rating "lands visibly" (per spec) when it was first created after the
 * manager's reveal — compares stamped millis, not a client-asserted flag.
 */
export function isRatingAfterReveal(ratingCreatedAtMs, revealedAtMs) {
  if (typeof ratingCreatedAtMs !== 'number' || typeof revealedAtMs !== 'number') return false;
  return ratingCreatedAtMs > revealedAtMs;
}

/** Reveal-moment ordering: highest stars first, then name — a little
 * ceremony for the "everyone's ratings unveil together" moment. */
export function sortRatingsForReveal(ratings) {
  return [...(ratings || [])].sort((a, b) => {
    const byRating = (b.rating || 0) - (a.rating || 0);
    return byRating !== 0 ? byRating : (a.displayName || '').localeCompare(b.displayName || '');
  });
}

// ---- localStorage mirror of the caller's OWN rating (blind window only;
// see the design note above for why this exists instead of a read rule) ----

/** localStorage key for one member's own blind-window rating mirror. */
export function myRatingStorageKey(clubId, readId, slug) {
  return `blindRating:${clubId}:${readId}:${slug}`;
}

/** Best-effort write; localStorage can be unavailable (private mode, quota). */
export function storeMyRatingLocally(clubId, readId, slug, rating, comment) {
  try {
    localStorage.setItem(
      myRatingStorageKey(clubId, readId, slug),
      JSON.stringify({ rating, comment: comment || '', savedAt: Date.now() })
    );
  } catch { /* non-fatal — the Firestore write already succeeded */ }
}

/** Read the local mirror of the caller's own rating, or null if unset/unavailable. */
export function getMyStoredRating(clubId, readId, slug) {
  try {
    const raw = localStorage.getItem(myRatingStorageKey(clubId, readId, slug));
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

/**
 * Submit (or update) the caller's own rating. Deliberately does NOT read
 * the ratings subcollection first (see the ⚠️ gotcha note above) — "have I
 * rated before, on this device" comes from the localStorage mirror, and
 * that alone decides whether to bump the read's open `ratingCount`.
 * createdAt is stamped fresh with serverTimestamp() on every write.
 */
export async function rateBook(db, clubId, readId, rating, comment, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to rate.' };
  }
  const ratingCheck = validateRatingValue(rating);
  if (!ratingCheck.valid) return { success: false, error: ratingCheck.error };
  const commentCheck = validateRatingComment(comment);
  if (!commentCheck.valid) return { success: false, error: commentCheck.error };
  const slug = slugifyName(session.displayName);
  const ratingRef = doc(db, col('clubs'), clubId, 'reads', readId, 'ratings', slug);
  const readRef = doc(db, col('clubs'), clubId, 'reads', readId);
  const isFirstOnThisDevice = !getMyStoredRating(clubId, readId, slug);
  try {
    await setDoc(ratingRef, {
      displayName: session.displayName,
      rating,
      comment: commentCheck.comment,
      createdAt: serverTimestamp(),
      updatedAt: serverTimestamp(),
    });
    if (isFirstOnThisDevice) {
      await updateDoc(readRef, { ratingCount: increment(1) });
    }
    storeMyRatingLocally(clubId, readId, slug, rating, commentCheck.comment);
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/**
 * Reveal a read's ratings (manager action, enforced in the UI + rules on
 * claimed clubs via MANAGED_READ_FIELDS). Stamps revealedAt so
 * isRatingAfterReveal has a real instant to compare against.
 */
export async function revealRatings(db, clubId, readId) {
  try {
    await updateDoc(doc(db, col('clubs'), clubId, 'reads', readId), {
      ratingsRevealed: true,
      revealedAt: serverTimestamp(),
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e, { need: 'the club host/moderator role, or site admin' }) };
  } finally {
    reportGate('read.revealRatings', { clubId }); // Phase 1 shadow — fire-and-forget
  }
}

/**
 * Fetch every rating for a read. Only succeeds once ratingsAreRevealed(read)
 * — firestore.rules denies this read while blind, for every caller. Callers
 * should gate the call on ratingsAreRevealed() rather than relying on the
 * catch, so the blind-window UI never even attempts (and doesn't log) a
 * denied read.
 */
export async function getRatings(db, clubId, readId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'reads', readId, 'ratings'));
  return snap.docs.map(d => ({ slug: d.id, ...d.data() }));
}

/**
 * Remove one member's rating (moderation action — three-tier model:
 * club host/mod, site moderator or site admin; the UI shows the affordance
 * post-reveal only). Also decrements the read's open ratingCount so the
 * "N members rated" line stays honest. The delete itself is open in rules
 * (removeRead's cleanup loop depends on that) — the UI gate is the control,
 * same as every deleteComment-style moderation affordance on this site.
 */
export async function deleteRating(db, clubId, readId, slug) {
  try {
    await deleteDoc(doc(db, col('clubs'), clubId, 'reads', readId, 'ratings', slug));
    await updateDoc(doc(db, col('clubs'), clubId, 'reads', readId), {
      ratingCount: increment(-1),
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

// ==================== Meeting RSVP (backlog #5) ====================
//
// clubs/{id}/rsvps/{memberSlug}: one doc per member (doc id = slug, the
// progress/votes/ratings convention), { displayName, response:
// 'going'|'maybe'|'cant', meetingAt, updatedAt }. Feature-gated per club:
// FEATURE_DEFAULTS key `meetingRsvp` in clubs.js (OFF by default, one Edit
// Club checkbox) — the SAME key also gates the .ics download button, since
// the spec treats RSVP + calendar file as one feature. Extends the
// next-meeting field shipped 2026-07-14 (club.nextMeetingAt/
// nextMeetingNotes); no new club-doc field, so no MANAGED_CLUB_FIELDS change.
//
// ⚠️ STALENESS DESIGN — read this before touching the tally/lookup helpers.
// Responses are keyed by member slug, not by meeting instant, because the
// spec calls for "one response per member (slug-keyed subcollection,
// changeable)" — the same doc-per-member shape as polls/ratings/progress.
// That means an old response document can OUTLIVE the meeting it was made
// for: a manager reschedules or clears nextMeetingAt, and nothing rewrites
// or deletes the member's existing rsvp doc (this trust model has no safe
// manager-only bulk-delete to grant, and looping every member's doc on every
// reschedule would race the very tally it's trying to keep honest).
//
// The shape that makes a stale response IMPOSSIBLE TO MISCOUNT: every
// response is stamped with `meetingAt`, the exact epoch of the meeting it
// answered. Every reader — the tally, "my response" lookup, everything —
// filters on `meetingAt === club.nextMeetingAt` (see isRsvpCurrent /
// currentRsvps below) before counting anything. A response for a meeting
// that was later moved or cleared simply never matches the live epoch and
// drops out of every calculation on its own, with no cleanup write, no
// extra read, and no race: the SAME club doc snapshot that supplies
// nextMeetingAt is what every caller already has in hand. When a member
// RSVPs again for the new meeting time, setDoc upserts their one doc with
// the new meetingAt — the old answer is simply superseded, not archived.

export const RSVP_RESPONSES = ['going', 'maybe', 'cant'];

/** Validate an RSVP response string. */
export function validateRsvpResponse(response) {
  return RSVP_RESPONSES.includes(response);
}

/**
 * Is this RSVP response still relevant to the CURRENT meeting? See the
 * staleness design note above — this exact-epoch comparison is the whole
 * mechanism. A cleared meeting (nextMeetingAt null/undefined) makes every
 * stored response stale, by construction (nothing is ever === a non-number).
 */
export function isRsvpCurrent(rsvp, meetingAt) {
  return !!rsvp && Number.isFinite(meetingAt) && rsvp.meetingAt === meetingAt;
}

/** Filter a list of RSVP docs down to only those answering the club's
 * CURRENT meeting instant. Callers should tally/display only this list. */
export function currentRsvps(rsvps, meetingAt) {
  return (rsvps || []).filter(r => isRsvpCurrent(r, meetingAt));
}

/**
 * The caller's own response, or null if they haven't RSVP'd to the current
 * meeting. Pass an already-`currentRsvps`-filtered list — same pattern as
 * myPollVote, which trusts its caller to have filtered first.
 */
export function myRsvpResponse(rsvps, slug) {
  const mine = (rsvps || []).find(r => r.slug === slug);
  return mine && RSVP_RESPONSES.includes(mine.response) ? mine.response : null;
}

/**
 * Tally an already-current-filtered RSVP list by response.
 * @returns {{ counts: {going:number,maybe:number,cant:number}, byResponse: {going:Array,maybe:Array,cant:Array}, total:number }}
 */
export function tallyRsvps(rsvps) {
  const byResponse = { going: [], maybe: [], cant: [] };
  for (const r of rsvps || []) {
    if (RSVP_RESPONSES.includes(r.response)) byResponse[r.response].push(r);
  }
  const counts = {
    going: byResponse.going.length,
    maybe: byResponse.maybe.length,
    cant: byResponse.cant.length,
  };
  return { counts, byResponse, total: counts.going + counts.maybe + counts.cant };
}

/**
 * Cast (or change) the caller's RSVP for the given meeting instant.
 * setDoc upsert makes it changeable, per the spec — one doc per member.
 */
export async function castRsvp(db, clubId, response, meetingAt, session) {
  if (!session || !session.displayName) {
    return { success: false, error: 'Sign in to RSVP.' };
  }
  if (!validateRsvpResponse(response)) {
    return { success: false, error: 'Invalid RSVP response.' };
  }
  if (!Number.isFinite(meetingAt)) {
    return { success: false, error: 'No meeting is currently scheduled.' };
  }
  try {
    const slug = slugifyName(session.displayName);
    await setDoc(doc(db, col('clubs'), clubId, 'rsvps', slug), {
      displayName: session.displayName,
      response,
      meetingAt,
      updatedAt: serverTimestamp(),
    });
    return { success: true };
  } catch (e) {
    return { success: false, error: describeActionError(e) };
  }
}

/** Fetch every RSVP doc for a club (current AND stale — filter with
 * currentRsvps before tallying or displaying). */
export async function getRsvps(db, clubId) {
  const snap = await getDocs(collection(db, col('clubs'), clubId, 'rsvps'));
  return snap.docs.map(d => ({ slug: d.id, ...d.data() }));
}
