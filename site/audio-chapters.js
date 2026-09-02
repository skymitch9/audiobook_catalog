// audio-chapters.js — the chapter model behind the player
// ES module, browser-native (no build step). PURE: no DOM, no fetch, no clock.
//
// AUDIO PLAYER PHASE 2, 2026-09-02.
// Design of record: catalog-platform/docs/info/audio-player-design.md
//   §1.2 — chapters.json, its shape, and the `start_sec` precision fix (0a)
//   §8 #5/#6/#7 — chapter next/prev, the chapter list, THE CHAPTER-RELATIVE BAR
//   §8 (cross-cutting) — "chapter boundaries are COMPUTED, not given"
//
// ⚠️ WHY THIS IS A SEPARATE, PURE MODULE. The owner's hardest requirement is
// requirement 7 — *"the scrub bar should be per chapter not per book"* — and it
// is the single reason this feature ships no player library (§2.3: every
// surveyed player draws a BOOK-relative bar because that is what a media file
// is). So the chapter arithmetic is the load-bearing part of the whole phase,
// and every one of its failures is SILENT: an off-by-one boundary is not an
// error, it is "next chapter played the tail of the last one"; a bad fraction
// is not an error, it is a scrub bar that lies. None of it throws. So it lives
// here, with no DOM to mock, and it is pinned by tests.
//
// ⚠️ THE THREE FACTS ABOUT chapters.json THAT SHAPE THIS FILE:
//
// 1. **Chapters have STARTS, never ENDS** (§1.2). Chapter i's end is chapter
//    i+1's start. The LAST chapter's end is the book's duration, which is not
//    in this file at all — it arrives from the media element's own
//    `audio.duration` on `loadedmetadata`. Until that event the last chapter
//    has NO end, and every function here must answer "not known yet" rather
//    than render NaN.
// 2. **`start_sec` is the real number; `start_min` is rounded to 6 SECONDS**
//    (§1.2, MEASURED). Phase 0a added `start_sec` at full float precision and
//    it is present for all 1,088 books (re-measured 2026-09-02). `start_min`
//    is kept only as a fallback for a book the re-run somehow missed — six
//    seconds of error is audible, so `start_sec` wins wherever it exists.
// 3. **The key is the m4b title tag**, the same identity family
//    `bookIdFromTitle()` folds, so chapters and positions agree about which
//    book they are on (§1.2). Callers pass the catalogue title verbatim.

/**
 * How far into a chapter you have to be before "previous" means
 * "restart this chapter" instead of "go to the one before".
 *
 * ⚠️ Design §8 #5, verbatim: *"'Previous' should restart the current chapter
 * if more than ~3 s in, and only step back a chapter if near its start — every
 * audio player does this and its absence feels broken."* It is a number people
 * never notice when it is right and always notice when it is missing.
 */
export const PREV_RESTART_THRESHOLD_SEC = 3;

/**
 * One chapters.json entry -> the player's chapter array.
 *
 * @param {object|null} chaptersJson the whole parsed chapters.json
 * @param {string} bookTitle the catalogue/m4b title
 * @returns {Array<{index:number,title:string,startSec:number,endSec:number|null,partLabel:string|null}>}
 *          endSec is null ONLY for the final chapter, until a duration is known.
 *          An unknown book, a malformed entry or a chapterless book -> [].
 */
export function buildChapters(chaptersJson, bookTitle) {
  if (!chaptersJson || !bookTitle) return [];
  const entry = chaptersJson[bookTitle];
  if (!entry || !Array.isArray(entry.chapters) || entry.chapters.length === 0) return [];

  // ⚠️ Sorted by start time, not trusted in file order, and anything without a
  // usable start is DROPPED rather than defaulted to 0 — a chapter silently
  // parked at the beginning is a chapter that hijacks the first scrub.
  const raw = entry.chapters
    .map((ch) => {
      const startSec = chapterStartSec(ch);
      if (startSec === null) return null;
      return { title: (ch && ch.title) || 'Untitled', startSec };
    })
    .filter(Boolean)
    .sort((a, b) => a.startSec - b.startSec);

  if (!raw.length) return [];

  const parts = Array.isArray(entry.parts) ? entry.parts : [];
  return raw.map((ch, i) => ({
    index: i,
    title: ch.title,
    startSec: ch.startSec,
    // ⚠️ COMPUTED, never given. The last one stays null until a duration lands.
    endSec: i + 1 < raw.length ? raw[i + 1].startSec : null,
    partLabel: partLabelFor(parts, i),
  }));
}

/**
 * The start of one raw chapter in seconds, or null if it has none.
 *
 * ⚠️ `start_sec` first, ALWAYS. `start_min` is the 6-second-rounded field and
 * is a fallback only — see the header's fact 2.
 */
export function chapterStartSec(ch) {
  if (!ch || typeof ch !== 'object') return null;
  if (typeof ch.start_sec === 'number' && isFinite(ch.start_sec) && ch.start_sec >= 0) {
    return ch.start_sec;
  }
  if (typeof ch.start_min === 'number' && isFinite(ch.start_min) && ch.start_min >= 0) {
    return ch.start_min * 60;
  }
  return null;
}

/**
 * The `parts` label covering chapter `i`, or null.
 * 88 books carry these (§1.2) and the chapter list uses them as group headers.
 */
function partLabelFor(parts, i) {
  for (const p of parts) {
    if (!p) continue;
    const s = typeof p.start_index === 'number' ? p.start_index : null;
    const e = typeof p.end_index === 'number' ? p.end_index : null;
    if (s === null || e === null) continue;
    if (i >= s && i <= e) return p.label || null;
  }
  return null;
}

/**
 * Teach the chapter array the book's real duration once `loadedmetadata` fires.
 *
 * ⚠️ This is the ONLY way the final chapter ever gets an end, and until it is
 * called the bar must render "duration not yet known" rather than NaN (§8).
 * Returns a NEW array; the input is not mutated.
 *
 * A duration that is not a finite positive number (a stream still loading,
 * Infinity on some live sources) is IGNORED — a bad duration would give the
 * last chapter a nonsense end, which is worse than no end at all.
 */
export function withDuration(chapters, durationSec) {
  if (!Array.isArray(chapters) || !chapters.length) return chapters || [];
  if (typeof durationSec !== 'number' || !isFinite(durationSec) || durationSec <= 0) {
    return chapters;
  }
  const last = chapters[chapters.length - 1];
  // ⚠️ Refuse a duration that lands BEFORE the last chapter starts. That is a
  // corrupt pairing (wrong book, wrong file), and an endSec < startSec makes
  // every fraction below negative.
  if (durationSec <= last.startSec) return chapters;
  return chapters.map((ch, i) => (
    i === chapters.length - 1 ? { ...ch, endSec: durationSec } : ch
  ));
}

/**
 * Which chapter contains `timeSec`. -1 when there are no chapters.
 *
 * A time before the first chapter's start belongs to the first chapter —
 * m4b files often open a few tenths of a second before chapter 0.
 */
export function chapterIndexAt(chapters, timeSec) {
  if (!Array.isArray(chapters) || !chapters.length) return -1;
  const t = typeof timeSec === 'number' && isFinite(timeSec) ? timeSec : 0;
  for (let i = chapters.length - 1; i >= 0; i--) {
    if (t >= chapters[i].startSec) return i;
  }
  return 0;
}

/**
 * 🔴 REQUIREMENT 7 — the chapter-relative position of `timeSec`, 0..1.
 *
 * Design §8 #7: *"the bar's domain is [chapter.startSec, chapter.endSec], not
 * [0, duration]"*. This is the function that makes the scrub bar per-chapter
 * and it is the reason no player library is used.
 *
 * @returns {number|null} null when the chapter has no known end yet (the final
 *          chapter before `loadedmetadata`) — the caller must render an
 *          indeterminate bar, NEVER a NaN one.
 */
export function chapterFraction(chapters, timeSec) {
  const i = chapterIndexAt(chapters, timeSec);
  if (i < 0) return null;
  const ch = chapters[i];
  if (ch.endSec === null || ch.endSec <= ch.startSec) return null;
  const t = typeof timeSec === 'number' && isFinite(timeSec) ? timeSec : 0;
  const frac = (t - ch.startSec) / (ch.endSec - ch.startSec);
  return Math.min(1, Math.max(0, frac));
}

/**
 * The inverse of {@link chapterFraction}: a drag on the bar -> an absolute time.
 * Clamped inside the chapter, so a drag can never leave the chapter it is for.
 *
 * @returns {number|null} null when the chapter has no known end.
 */
export function timeAtChapterFraction(chapters, chapterIndex, fraction) {
  if (!Array.isArray(chapters) || chapterIndex < 0 || chapterIndex >= chapters.length) return null;
  const ch = chapters[chapterIndex];
  if (ch.endSec === null || ch.endSec <= ch.startSec) return null;
  const f = Math.min(1, Math.max(0, typeof fraction === 'number' && isFinite(fraction) ? fraction : 0));
  return ch.startSec + f * (ch.endSec - ch.startSec);
}

/**
 * Seconds elapsed in the current chapter, and seconds remaining in it.
 * `remaining` is null when the chapter has no known end.
 */
export function chapterTimes(chapters, timeSec) {
  const i = chapterIndexAt(chapters, timeSec);
  if (i < 0) return { index: -1, elapsed: 0, remaining: null, chapter: null };
  const ch = chapters[i];
  const t = typeof timeSec === 'number' && isFinite(timeSec) ? timeSec : 0;
  const elapsed = Math.max(0, t - ch.startSec);
  const remaining = ch.endSec === null ? null : Math.max(0, ch.endSec - t);
  return { index: i, elapsed, remaining, chapter: ch };
}

/**
 * Where "previous chapter" should go from `timeSec`.
 *
 * ⚠️ Design §8 #5: more than {@link PREV_RESTART_THRESHOLD_SEC} into a chapter
 * and this RESTARTS that chapter rather than stepping back. Its absence is the
 * kind of thing nobody files a bug about; they just decide the player is bad.
 *
 * @returns {number|null} an absolute time, or null when there is nowhere to go
 *          (no chapters, or already at the very start of the first chapter).
 */
export function previousChapterTarget(chapters, timeSec, thresholdSec = PREV_RESTART_THRESHOLD_SEC) {
  const i = chapterIndexAt(chapters, timeSec);
  if (i < 0) return null;
  const t = typeof timeSec === 'number' && isFinite(timeSec) ? timeSec : 0;
  const into = t - chapters[i].startSec;
  if (into > thresholdSec) return chapters[i].startSec;   // restart this chapter
  if (i === 0) return chapters[0].startSec;               // rewind to the top
  return chapters[i - 1].startSec;
}

/**
 * Where "next chapter" should go. null when already in the last chapter —
 * the caller disables the control rather than seeking to the end.
 */
export function nextChapterTarget(chapters, timeSec) {
  const i = chapterIndexAt(chapters, timeSec);
  if (i < 0 || i >= chapters.length - 1) return null;
  return chapters[i + 1].startSec;
}

/**
 * ⏰ Sleep timer, "end of chapter" — how many WALL-CLOCK milliseconds are left.
 *
 * ⚠️ Design §8 #8: the remaining audio must be divided by `playbackRate`.
 * Ten minutes of book at 2× is five minutes of sleep, and a timer that ignores
 * the rate fires late by exactly the amount the listener sped things up — on a
 * 3× listen it overshoots by two thirds of the chapter.
 *
 * @returns {number|null} ms, or null when the chapter end is not known yet.
 */
export function msUntilChapterEnd(chapters, timeSec, playbackRate = 1) {
  const { remaining } = chapterTimes(chapters, timeSec);
  if (remaining === null) return null;
  const rate = typeof playbackRate === 'number' && isFinite(playbackRate) && playbackRate > 0
    ? playbackRate
    : 1;
  return (remaining / rate) * 1000;
}

/**
 * `h:mm:ss` / `m:ss` for a duration in seconds. Never renders NaN — an
 * unknown value reads `--:--`, which is honest, where `0:00` is a lie that
 * looks like a book of zero length.
 */
export function formatTime(seconds) {
  if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) return '--:--';
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}
