// Stored/cross-user XSS guard for site/community.html.
//
// renderUsers() and openUserModal() build member cards / the profile modal as
// template strings assigned to innerHTML. displayName, currentlyReading,
// favorites, photoURL and club emoji are user-controlled profile fields; every
// sibling page (clubs.html, club.html, club-read.html) escapes them, but
// community.html shipped without an escapeHtml call. This test reads the shipped
// HTML source and asserts the user-controlled fields are HTML-escaped before
// innerHTML assignment.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(
  path.resolve(__dirname, '../community.html'),
  'utf-8'
);

describe('community.html output escaping', () => {
  it('defines an escapeHtml helper', () => {
    expect(SRC).toMatch(/function\s+escapeHtml\s*\(/);
  });

  it('escapes displayName in the card and modal', () => {
    // No bare interpolation of the whole displayName into innerHTML.
    expect(SRC).not.toMatch(/\$\{u\.displayName\}/);
    expect(SRC).not.toMatch(/\$\{user\.displayName\}/);
    // The escaped forms are present.
    expect(SRC).toContain('${escapeHtml(u.displayName)}');
    expect(SRC).toContain('${escapeHtml(user.displayName)}');
  });

  it('escapes currentlyReading in the card and modal', () => {
    expect(SRC).not.toMatch(/\$\{u\.currentlyReading\}/);
    expect(SRC).not.toMatch(/\$\{user\.currentlyReading\}/);
    expect(SRC).toContain('${escapeHtml(u.currentlyReading)}');
    expect(SRC).toContain('${escapeHtml(user.currentlyReading)}');
  });

  it('escapes photoURL used as an <img src>', () => {
    expect(SRC).not.toMatch(/src="\$\{u\.photoURL\}"/);
    expect(SRC).not.toMatch(/src="\$\{user\.photoURL\}"/);
    expect(SRC).toContain('${escapeHtml(u.photoURL)}');
    expect(SRC).toContain('${escapeHtml(user.photoURL)}');
  });

  it('escapes favorite titles and the club emoji', () => {
    expect(SRC).toContain("escapeHtml(f)");
    expect(SRC).toContain('${escapeHtml(c.emoji)}');
    expect(SRC).toContain('${escapeHtml(c.name)}');
  });
});
