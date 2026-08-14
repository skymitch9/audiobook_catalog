// @vitest-environment jsdom
// Feature: book-clubs backlog #5 — the RFC 5545 (.ics) calendar-file builder
// behind the meeting scheduler's "Add to calendar" download. Pure
// text-building logic (site/ics.js) — no Firestore, no DOM — so it's
// exercised directly, including a minimal round-trip PARSE to prove the
// escaping + folding actually produce a file a calendar app could read back.
import { describe, it, expect } from 'vitest';
import {
  escapeIcsText, formatIcsUtc, foldIcsLine, buildMeetingIcs, icsFilename,
} from '../ics.js';

// ==================== Escaping (RFC 5545 §3.3.11) ====================

describe('escapeIcsText', () => {
  it('escapes a literal backslash to two backslashes', () => {
    expect(escapeIcsText('a\\b')).toBe('a\\\\b');
  });

  it('escapes commas', () => {
    expect(escapeIcsText('a,b')).toBe('a\\,b');
  });

  it('escapes semicolons', () => {
    expect(escapeIcsText('a;b')).toBe('a\\;b');
  });

  it('escapes an embedded newline as the literal two-char sequence "\\n"', () => {
    expect(escapeIcsText('a\nb')).toBe('a\\nb');
  });

  it('collapses a CRLF pair to a single literal "\\n" (not two)', () => {
    expect(escapeIcsText('a\r\nb')).toBe('a\\nb');
  });

  it('collapses a bare CR to a single literal "\\n"', () => {
    expect(escapeIcsText('a\rb')).toBe('a\\nb');
  });

  it('escapes the backslash BEFORE comma/semicolon/newline so escapes are not double-escaped', () => {
    // A literal backslash followed by a comma: the backslash must become \\
    // first, THEN the comma becomes \, — never \\, collapsing into one \,.
    expect(escapeIcsText('\\,')).toBe('\\\\\\,');
  });

  it('handles null/undefined as empty string', () => {
    expect(escapeIcsText(null)).toBe('');
    expect(escapeIcsText(undefined)).toBe('');
  });

  it('leaves ordinary text and multiple newlines correctly escaped', () => {
    expect(escapeIcsText('Line one\nLine two\nLine three')).toBe('Line one\\nLine two\\nLine three');
  });

  it('a value containing every special character round-trips through unescape', () => {
    const raw = 'Snacks, drinks; bring a friend\nRSVP by Friday';
    const escaped = escapeIcsText(raw);
    // Minimal RFC 5545 TEXT unescape, mirroring what a real calendar parser
    // does: \\ -> \, \, -> ,, \; -> ;, \n -> real newline (order matters:
    // unescape backslash-pairs LAST so \\n isn't mistaken for \n).
    const unescaped = escaped
      .replace(/\\n/g, '\n')
      .replace(/\\,/g, ',')
      .replace(/\\;/g, ';')
      .replace(/\\\\/g, '\\');
    expect(unescaped).toBe(raw);
  });
});

// ==================== UTC DATE-TIME formatting (RFC 5545 §3.3.5) ====================

describe('formatIcsUtc', () => {
  it('formats a UTC instant as YYYYMMDDTHHMMSSZ', () => {
    const ms = Date.UTC(2026, 8, 1, 19, 30, 5); // 2026-09-01T19:30:05Z (month is 0-indexed)
    expect(formatIcsUtc(ms)).toBe('20260901T193005Z');
  });

  it('zero-pads single-digit month/day/hour/minute/second', () => {
    const ms = Date.UTC(2026, 0, 5, 3, 4, 6); // 2026-01-05T03:04:06Z
    expect(formatIcsUtc(ms)).toBe('20260105T030406Z');
  });

  it('converts a non-UTC local instant to its correct UTC representation', () => {
    // An ISO string with a +05:00 offset should format to the equivalent
    // UTC wall-clock time, proving this always emits absolute UTC.
    const ms = Date.parse('2026-09-01T23:00:00+05:00'); // == 2026-09-01T18:00:00Z
    expect(formatIcsUtc(ms)).toBe('20260901T180000Z');
  });
});

// ==================== Line folding (RFC 5545 §3.1) ====================

describe('foldIcsLine', () => {
  it('leaves a short line untouched', () => {
    expect(foldIcsLine('SUMMARY:Short')).toBe('SUMMARY:Short');
  });

  it('does not fold a line at exactly 75 octets', () => {
    const line = 'X'.repeat(75);
    expect(new TextEncoder().encode(line).length).toBe(75);
    expect(foldIcsLine(line)).toBe(line);
  });

  it('folds a line over 75 octets into CRLF + single-space continuations', () => {
    const line = 'DESCRIPTION:' + 'A'.repeat(100);
    const folded = foldIcsLine(line);
    const physicalLines = folded.split('\r\n');
    expect(physicalLines.length).toBeGreaterThan(1);
    // Every physical line (as UTF-8 octets) fits the 75-octet cap.
    for (const pl of physicalLines) {
      expect(new TextEncoder().encode(pl).length).toBeLessThanOrEqual(75);
    }
    // Every continuation line starts with exactly one leading space.
    for (const pl of physicalLines.slice(1)) {
      expect(pl.startsWith(' ')).toBe(true);
      expect(pl.startsWith('  ')).toBe(false);
    }
    // Unfolding (strip CRLF + the one leading space on each continuation)
    // reconstructs the original line exactly.
    const unfolded = physicalLines.map((pl, i) => (i === 0 ? pl : pl.slice(1))).join('');
    expect(unfolded).toBe(line);
  });

  it('never splits inside a multi-byte UTF-8 sequence (emoji/accented text)', () => {
    // Each 📅 is 4 UTF-8 octets; repeating well past 75 octets forces a fold
    // that must land on a codepoint boundary or the decode would corrupt it.
    const line = 'DESCRIPTION:' + '📅'.repeat(30);
    const folded = foldIcsLine(line);
    const physicalLines = folded.split('\r\n');
    for (const pl of physicalLines) {
      expect(new TextEncoder().encode(pl).length).toBeLessThanOrEqual(75);
      // decode/re-encode round trip proves no byte sequence was corrupted —
      // a mid-codepoint split would produce U+FFFD replacement characters.
      expect(pl).not.toContain('�');
    }
    const unfolded = physicalLines.map((pl, i) => (i === 0 ? pl : pl.slice(1))).join('');
    expect(unfolded).toBe(line);
  });
});

// ==================== Full VCALENDAR/VEVENT build ====================

describe('buildMeetingIcs', () => {
  const MEETING_AT = Date.UTC(2026, 8, 1, 19, 0, 0); // 2026-09-01T19:00:00Z

  it('throws without a finite meetingAt', () => {
    expect(() => buildMeetingIcs({ clubName: 'Book Nerds', meetingAt: null })).toThrow();
    expect(() => buildMeetingIcs({ clubName: 'Book Nerds', meetingAt: NaN })).toThrow();
    expect(() => buildMeetingIcs({ clubName: 'Book Nerds' })).toThrow();
  });

  it('produces a well-formed VCALENDAR/VEVENT with CRLF line endings throughout', () => {
    const ics = buildMeetingIcs({
      clubId: 'club1', clubName: 'Book Nerds', meetingAt: MEETING_AT,
      notes: 'Bring snacks', bookTitle: 'Mistborn', clubUrl: 'https://audiobooks.heygabi.ai/club.html?id=club1',
    });
    // No bare LF: every line break is CRLF.
    expect(ics.replace(/\r\n/g, '')).not.toMatch(/\n/);
    const lines = ics.split('\r\n').filter(Boolean);
    expect(lines[0]).toBe('BEGIN:VCALENDAR');
    expect(lines).toContain('VERSION:2.0');
    expect(lines).toContain('BEGIN:VEVENT');
    expect(lines).toContain('END:VEVENT');
    expect(lines[lines.length - 1]).toBe('END:VCALENDAR');
    expect(lines.some(l => l.startsWith('DTSTAMP:'))).toBe(true);
    expect(lines).toContain(`DTSTART:${formatIcsUtc(MEETING_AT)}`);
    expect(lines).toContain('SUMMARY:Book Nerds — book club');
    expect(lines.some(l => l.startsWith('UID:club1-'))).toBe(true);
  });

  it('DESCRIPTION carries the current read and notes, escaped (kept short here so the line is NOT folded — folding + escaping together are covered by the round-trip test below)', () => {
    const ics = buildMeetingIcs({
      clubName: 'Book Nerds', meetingAt: MEETING_AT,
      notes: 'Bring, snacks; chat',
      bookTitle: 'Kings',
    });
    expect(ics).toContain('DESCRIPTION:Currently reading: Kings\\nBring\\, snacks\\; chat');
  });

  it('omits DESCRIPTION entirely when there is no book and no notes', () => {
    const ics = buildMeetingIcs({ clubName: 'Book Nerds', meetingAt: MEETING_AT });
    expect(ics).not.toContain('DESCRIPTION');
  });

  it('includes URL when a club URL is given, and omits it otherwise', () => {
    const withUrl = buildMeetingIcs({ clubName: 'BN', meetingAt: MEETING_AT, clubUrl: 'https://example.com/club.html?id=x' });
    expect(withUrl).toContain('URL:https://example.com/club.html?id=x');
    const withoutUrl = buildMeetingIcs({ clubName: 'BN', meetingAt: MEETING_AT });
    expect(withoutUrl).not.toContain('URL:');
  });

  it('folds a long DESCRIPTION line so every physical line stays within 75 octets', () => {
    const ics = buildMeetingIcs({
      clubName: 'Book Nerds', meetingAt: MEETING_AT,
      notes: 'A'.repeat(200),
    });
    for (const pl of ics.split('\r\n')) {
      expect(new TextEncoder().encode(pl).length).toBeLessThanOrEqual(75);
    }
  });

  it('a distinct UID is stable for the same club+meeting and differs across meetings', () => {
    const a1 = buildMeetingIcs({ clubId: 'club1', clubName: 'BN', meetingAt: MEETING_AT, generatedAt: 1 });
    const a2 = buildMeetingIcs({ clubId: 'club1', clubName: 'BN', meetingAt: MEETING_AT, generatedAt: 999999 });
    const uidOf = (ics) => ics.split('\r\n').find(l => l.startsWith('UID:'));
    expect(uidOf(a1)).toBe(uidOf(a2)); // UID does not depend on generation time
    const b = buildMeetingIcs({ clubId: 'club1', clubName: 'BN', meetingAt: MEETING_AT + 604800000 });
    expect(uidOf(a1)).not.toBe(uidOf(b));
  });

  it('round-trip PARSE: unfolding + unescaping recovers the original values', () => {
    const clubName = 'Sci-Fi & Fantasy, Club';
    const notes = 'Snacks, drinks; chat at 7, read til 9\nSecond line of notes';
    const bookTitle = 'Ancillary Justice';
    const ics = buildMeetingIcs({
      clubId: 'club7', clubName, meetingAt: MEETING_AT, notes, bookTitle,
      clubUrl: 'https://audiobooks.heygabi.ai/club.html?id=club7',
    });

    // Minimal RFC 5545 parser: unfold (CRLF + single leading space means
    // "continuation of the previous line"), then split each logical line on
    // the first unescaped colon into PROPERTY:VALUE, then unescape TEXT
    // values. This proves the file is actually parseable, not just
    // "looks right".
    const physicalLines = ics.split('\r\n').filter((_, i, arr) => !(i === arr.length - 1 && arr[i] === ''));
    const logicalLines = [];
    for (const pl of physicalLines) {
      if (pl.startsWith(' ') && logicalLines.length) {
        logicalLines[logicalLines.length - 1] += pl.slice(1);
      } else {
        logicalLines.push(pl);
      }
    }
    const props = {};
    for (const line of logicalLines) {
      const idx = line.indexOf(':');
      if (idx === -1) continue;
      const key = line.slice(0, idx);
      const value = line.slice(idx + 1)
        .replace(/\\n/g, '\n')
        .replace(/\\,/g, ',')
        .replace(/\\;/g, ';')
        .replace(/\\\\/g, '\\');
      props[key] = value;
    }

    expect(logicalLines[0]).toBe('BEGIN:VCALENDAR');
    expect(logicalLines[logicalLines.length - 1]).toBe('END:VCALENDAR');
    expect(props.SUMMARY).toBe(`${clubName} — book club`);
    expect(props.DTSTART).toBe(formatIcsUtc(MEETING_AT));
    expect(props.DESCRIPTION).toBe(`Currently reading: ${bookTitle}\n${notes}\nhttps://audiobooks.heygabi.ai/club.html?id=club7`);
    expect(props.URL).toBe('https://audiobooks.heygabi.ai/club.html?id=club7');
  });
});

// ==================== Filename ====================

describe('icsFilename', () => {
  it('slugifies a club name into a safe filename', () => {
    expect(icsFilename('Sci-Fi & Fantasy Club')).toBe('sci-fi-fantasy-club-meeting.ics');
  });

  it('falls back to a generic name when blank', () => {
    expect(icsFilename('')).toBe('book-club-meeting.ics');
    expect(icsFilename(undefined)).toBe('book-club-meeting.ics');
  });
});
