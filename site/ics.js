// ics.js — RFC 5545 (.ics / iCalendar) builder for the meeting scheduler's
// "Add to calendar" download (book clubs backlog #5). ES module,
// browser-native (no build step). Pure text-building functions only — no
// Firestore — so the escaping/folding/formatting rules are unit-testable
// without a browser; downloadIcsFile is the one DOM-touching exception and
// is intentionally thin (everything it downloads is built by the pure
// functions above it).
//
// Client-generated per the spec: the whole file is assembled in the browser
// from data already on the page (club name, nextMeetingAt, notes, the
// current read, the club URL) and handed to the browser as a Blob download
// — no server round-trip, no new backend endpoint.

const CRLF = '\r\n';

// RFC 5545 §3.1: content lines SHOULD be no longer than 75 octets
// (UTF-8 bytes, not JS UTF-16 code units — a club name or notes with an
// emoji or accented character can be multi-byte), excluding the line break.
const FOLD_LIMIT_OCTETS = 75;

export const ICS_PRODID = '-//heygabi.ai//Audiobook Catalog Book Clubs//EN';

/**
 * Escape a TEXT property value per RFC 5545 §3.3.11, in the order that
 * matters: backslash FIRST (so the later escapes aren't themselves
 * re-escaped), then comma and semicolon, then embedded newlines collapsed
 * to the literal two-character escape sequence "\n" (backslash + n — NOT an
 * actual line break, which would corrupt the content line).
 */
export function escapeIcsText(value) {
  return String(value == null ? '' : value)
    .replace(/\\/g, '\\\\')
    .replace(/;/g, '\\;')
    .replace(/,/g, '\\,')
    .replace(/\r\n|\r|\n/g, '\\n');
}

/**
 * Format an epoch-millis instant as a UTC iCalendar DATE-TIME
 * (YYYYMMDDTHHMMSSZ), per RFC 5545 §3.3.5. nextMeetingAt is stored as an
 * absolute instant (same convention as the reading schedule's dueAt), so
 * this always renders in UTC with the trailing "Z" — calendar apps convert
 * to the viewer's own timezone, same as club.html's toLocaleString display.
 */
export function formatIcsUtc(ms) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  return (
    `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}`
    + `T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`
  );
}

/**
 * Fold one logical content line to the RFC 5545 §3.1 75-octet limit.
 * Continuation lines are CRLF followed by a single leading space, and that
 * space itself counts toward the following line's 75-octet budget (the
 * common convention: first line gets the full 75 octets, every continuation
 * line gets 74 octets of content plus its 1-octet leading space). Splits are
 * counted in UTF-8 octets and never land inside a multi-byte UTF-8
 * sequence — splitting mid-codepoint would hand a calendar app invalid
 * UTF-8 on unfold.
 */
export function foldIcsLine(line) {
  const bytes = new TextEncoder().encode(line);
  if (bytes.length <= FOLD_LIMIT_OCTETS) return line;
  const decoder = new TextDecoder();
  const parts = [];
  let start = 0;
  let first = true;
  while (start < bytes.length) {
    const budget = first ? FOLD_LIMIT_OCTETS : FOLD_LIMIT_OCTETS - 1;
    let end = Math.min(start + budget, bytes.length);
    // A UTF-8 continuation byte matches 10xxxxxx (0x80-0xBF) — back off
    // until `end` sits on a codepoint boundary.
    while (end > start && (bytes[end] & 0xc0) === 0x80) end--;
    parts.push(decoder.decode(bytes.slice(start, end)));
    start = end;
    first = false;
  }
  return parts.map((p, i) => (i === 0 ? p : ' ' + p)).join(CRLF);
}

/**
 * Build a full VCALENDAR/VEVENT .ics document for a club's next meeting.
 * SUMMARY is "<club name> — book club"; DESCRIPTION carries the currently
 * active read (if any) and the meeting notes, one paragraph each, joined
 * with a real newline that escapeIcsText then collapses to the RFC escape;
 * URL points back at the club page. DTSTART is the meeting instant in UTC.
 * Every content line is individually folded and the whole document uses
 * CRLF line endings throughout, per RFC 5545.
 *
 * @param {{clubId?: string, clubName: string, meetingAt: number, notes?: string, bookTitle?: string, clubUrl?: string, uid?: string, generatedAt?: number}} input
 * @returns {string} the complete .ics file text
 */
export function buildMeetingIcs(input) {
  const { clubId, clubName, meetingAt, notes, bookTitle, clubUrl, uid, generatedAt = Date.now() } = input || {};
  if (!Number.isFinite(meetingAt)) {
    throw new Error('meetingAt is required to build a calendar event.');
  }
  const summary = `${clubName || 'Book club'} — book club`;
  const descParas = [];
  if (bookTitle) descParas.push(`Currently reading: ${bookTitle}`);
  if ((notes || '').trim()) descParas.push(notes.trim());
  if (clubUrl) descParas.push(clubUrl);
  const eventUid = uid || `${clubId || 'club'}-${meetingAt}@heygabi.ai`;

  const lines = [
    'BEGIN:VCALENDAR',
    'VERSION:2.0',
    `PRODID:${ICS_PRODID}`,
    'CALSCALE:GREGORIAN',
    'BEGIN:VEVENT',
    `UID:${eventUid}`,
    `DTSTAMP:${formatIcsUtc(generatedAt)}`,
    `DTSTART:${formatIcsUtc(meetingAt)}`,
    `SUMMARY:${escapeIcsText(summary)}`,
  ];
  if (descParas.length) lines.push(`DESCRIPTION:${escapeIcsText(descParas.join('\n'))}`);
  if (clubUrl) lines.push(`URL:${escapeIcsText(clubUrl)}`);
  lines.push('END:VEVENT', 'END:VCALENDAR');

  return lines.map(foldIcsLine).join(CRLF) + CRLF;
}

/** Filesystem-safe .ics filename derived from the club name. */
export function icsFilename(clubName) {
  const base = (clubName || 'book-club')
    .trim()
    .replace(/[^\w\- ]+/g, '')
    .replace(/\s+/g, '-')
    .toLowerCase();
  return `${base || 'book-club'}-meeting.ics`;
}

/**
 * Trigger a browser download of the .ics text via a Blob object URL — the
 * "client-generated, no server round-trip" mechanism the spec calls for.
 * Deliberately thin and not unit-tested directly (it needs a real download
 * sink); everything it downloads is produced by buildMeetingIcs above,
 * which IS exhaustively tested.
 */
export function downloadIcsFile(filename, icsText) {
  const blob = new Blob([icsText], { type: 'text/calendar;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
