// @vitest-environment jsdom
//
// The Edit Club modal's SECTIONING (2026-08-18). Owner: "for the extra options
// in clubs we may need a new menu, it's getting pretty long." Twelve field
// blocks in one scroll became four <details> sections — Basics / Features /
// Discord / Managing — and the feature checkboxes, which had all lived in one
// container, were split across two.
//
// ⚠️ THE BUG THIS FILE EXISTS TO CATCH, measured in the club-questions build:
// updateClubDetails() REBUILDS the `features` map from its own known-keys list,
// so a key the save handler forgets to read is not "left alone" — it is
// DROPPED, silently, on the next save. Splitting the checkbox pile across two
// containers is exactly the edit that loses one. So rather than hard-coding a
// key->id map here (which would just be the same forgetting, twice), the map is
// PARSED OUT of the shipped save handler and compared against FEATURE_DEFAULTS.
// Add a key and forget its checkbox, or move a checkbox and forget its read,
// and this fails.
//
// The second half exercises syncEditSections() — lifted from the page, run
// against the page's own markup — for the two viewers that differ most: a
// plain member (who should never see an empty "Discord" header) and a manager
// (who sees all four).
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLUB_HTML = path.resolve(__dirname, '../club.html');
const source = readFileSync(CLUB_HTML, 'utf-8');

const { FEATURE_DEFAULTS } = await import('../clubs.js');

/** Body of a `function name(...) { ... }` in the page source, braces balanced. */
function extractFunctionSource(src, functionName) {
  const startMatch = src.match(new RegExp(`function ${functionName}\\([^)]*\\)\\s*\\{`));
  if (!startMatch) throw new Error(`Could not find function ${functionName}() in club.html`);
  const start = startMatch.index;
  let depth = 0;
  for (let i = src.indexOf('{', start); i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`Unbalanced braces in ${functionName}()`);
}

/** The `input.features = { key: document.getElementById('id').checked, ... }` object. */
function parseSavedFeatureReads(src) {
  const start = src.indexOf('input.features = {');
  if (start === -1) throw new Error('Could not find the input.features assignment in club.html');
  const end = src.indexOf('};', start);
  const block = src.slice(start, end);
  const out = {};
  const re = /(\w+):\s*document\.getElementById\('([^']+)'\)\.checked/g;
  let m;
  while ((m = re.exec(block)) !== null) {
    if (m[1] in out) throw new Error(`Feature key read twice on save: ${m[1]}`);
    out[m[1]] = m[2];
  }
  return out;
}

/** The Edit Club modal alone, mounted into this jsdom document. */
function mountModal() {
  const parsed = new DOMParser().parseFromString(source, 'text/html');
  const modal = parsed.getElementById('edit-modal');
  if (!modal) throw new Error('No #edit-modal in club.html');
  document.body.innerHTML = modal.outerHTML;
  return document.getElementById('edit-modal');
}

const FEATURE_CONTAINERS = ['edit-features-field', 'edit-discord-features-field'];

describe('Edit Club modal — feature checkboxes survive the sectioning', () => {
  it('the save handler reads EXACTLY the FEATURE_DEFAULTS keys — no key dropped, none invented', () => {
    const saved = parseSavedFeatureReads(source);
    expect(Object.keys(saved).sort()).toEqual(Object.keys(FEATURE_DEFAULTS).sort());
  });

  it('every saved key has its own checkbox, and every checkbox sits in a feature container', () => {
    mountModal();
    const saved = parseSavedFeatureReads(source);
    for (const [key, id] of Object.entries(saved)) {
      const el = document.getElementById(id);
      expect(el, `${key} -> #${id} is missing from the modal`).toBeTruthy();
      expect(el.type).toBe('checkbox');
      const container = el.closest('.field');
      expect(
        FEATURE_CONTAINERS.includes(container && container.id),
        `${key} (#${id}) is outside both feature containers — it would be drawn for viewers `
        + 'the isMod() gate is supposed to hide it from',
      ).toBe(true);
    }
  });

  it('splits them across BOTH containers — that split is the whole point of the regrouping', () => {
    mountModal();
    const saved = parseSavedFeatureReads(source);
    const byContainer = {};
    for (const id of Object.values(saved)) {
      const c = document.getElementById(id).closest('.field').id;
      byContainer[c] = (byContainer[c] || 0) + 1;
    }
    expect(byContainer['edit-features-field']).toBeGreaterThan(0);
    expect(byContainer['edit-discord-features-field']).toBeGreaterThan(0);
    // The Discord-shaped keys are the ones that moved.
    for (const key of ['discordAnnouncements', 'discordPollAnnouncements', 'discordQuestions']) {
      expect(document.getElementById(saved[key]).closest('.field').id)
        .toBe('edit-discord-features-field');
    }
  });

  it('every field block lives inside one of the four sections', () => {
    const modal = mountModal();
    const sections = modal.querySelectorAll('details.edit-sec');
    expect(sections.length).toBe(4);
    for (const field of modal.querySelectorAll('.field')) {
      expect(field.closest('details.edit-sec'), `${field.id || field.className} is outside every section`)
        .toBeTruthy();
    }
  });

  it('collapsing a section leaves each field\'s OWN inline display alone — the save gate reads that', () => {
    // The save handler asks each field `style.display !== 'none'` to decide
    // whether to send it. If closing a <details> could change that, a collapsed
    // section would quietly stop saving. Inline style is untouched by ancestors.
    const modal = mountModal();
    const meeting = document.getElementById('edit-meeting-field');
    meeting.style.display = 'block';
    const basics = modal.querySelector('#edit-sec-basics');
    basics.open = false;
    expect(meeting.style.display).toBe('block');
    basics.open = true;
    expect(meeting.style.display).toBe('block');
  });
});

describe('Edit Club modal — empty sections fold away', () => {
  /** syncEditSections() lifted from the page, bound to a mounted modal. */
  function loadSync(modal) {
    const src = extractFunctionSource(source, 'syncEditSections');
    // eslint-disable-next-line no-new-func
    return new Function('editModal', `${src}; return syncEditSections;`)(modal);
  }

  function loadFeaturesEditable() {
    const src = extractFunctionSource(source, 'featuresEditable');
    // eslint-disable-next-line no-new-func
    return new Function(`${src}; return featuresEditable;`)();
  }

  const MANAGER_ONLY = [
    'edit-avatar-field', 'edit-joinmode-field', 'edit-meeting-field',
    'edit-features-field', 'edit-discord-features-field',
    'edit-webhook-field', 'edit-webhook-restricted-field',
    'edit-reads-field', 'edit-reads-restricted-field', 'edit-roster-field',
  ];

  function setVisible(ids, on) {
    for (const id of ids) document.getElementById(id).style.display = on ? 'block' : 'none';
  }

  it('a plain member sees Basics and Features only — no header opening onto nothing', () => {
    const modal = mountModal();
    setVisible(MANAGER_ONLY, false); // exactly what openEditModal() leaves for a member
    loadSync(modal)();
    expect(modal.querySelector('#edit-sec-basics').style.display).toBe('block');
    // Starter questions (promptsEnabled) is every member's, so Features stays.
    expect(modal.querySelector('#edit-sec-features').style.display).toBe('block');
    expect(modal.querySelector('#edit-sec-discord').style.display).toBe('none');
    expect(modal.querySelector('#edit-sec-managing').style.display).toBe('none');
  });

  it('a bound manager sees all four', () => {
    const modal = mountModal();
    setVisible(MANAGER_ONLY, true);
    loadSync(modal)();
    for (const id of ['basics', 'features', 'discord', 'managing']) {
      expect(modal.querySelector(`#edit-sec-${id}`).style.display).toBe('block');
    }
  });

  it('featuresEditable() follows EITHER container — the two are shown together', () => {
    mountModal();
    const editable = loadFeaturesEditable();
    setVisible(FEATURE_CONTAINERS, false);
    expect(editable()).toBe(false);
    setVisible(FEATURE_CONTAINERS, true);
    expect(editable()).toBe(true);
    // Defensive: one container alone still counts as editable, so a future
    // edit that hides only one can never half-write the features map.
    setVisible(['edit-features-field'], false);
    expect(editable()).toBe(true);
  });
});
