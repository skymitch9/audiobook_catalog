// @vitest-environment jsdom
//
// The Universe filter (part of the audiobook-side universes port) lives as
// inline JS inside app/web/templates/index.html, the same page-scoped script
// block every other filter (My TBR List, My Reviews) lives in — there is no
// ES module to import here. Rather than reimplement filterByUniverse() /
// clearUniverseFilter() in the test (which could silently drift from the
// shipped code), this test extracts their real source straight out of the
// template — the same regex-extraction technique
// tests/test_book_modal_contract.py already uses on the Python side — and
// runs it against a synthetic DOM built the way app/web/html_builder.py
// actually shapes it: data-universe on the <tr> itself (table view) and
// inside the full book-modal data-attribute set on .ab-card (card view).
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TEMPLATE_PATH = path.resolve(__dirname, '../../app/web/templates/index.html');
const templateSource = readFileSync(TEMPLATE_PATH, 'utf-8');

function extractFunctionSource(source, functionName) {
  const startMatch = source.match(new RegExp(`function ${functionName}\\([^)]*\\)\\s*\\{`));
  if (!startMatch) {
    throw new Error(`Could not find function ${functionName}() in templates/index.html`);
  }
  const start = startMatch.index;
  const braceStart = source.indexOf('{', start);
  let depth = 0;
  for (let i = braceStart; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`Unbalanced braces while extracting ${functionName}()`);
}

/** Build the two real functions from the template, wired to a fake renderPage. */
function loadUniverseFilterFunctions(renderPage) {
  const filterSrc = extractFunctionSource(templateSource, 'filterByUniverse');
  const clearSrc = extractFunctionSource(templateSource, 'clearUniverseFilter');
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    'document',
    'renderPage',
    `
    var currentPage = 1;
    ${filterSrc}
    ${clearSrc}
    return { filterByUniverse: filterByUniverse, clearUniverseFilter: clearUniverseFilter };
    `
  );
  return factory(document, renderPage);
}

function buildRow(title, universe) {
  const tr = document.createElement('tr');
  tr.setAttribute('data-universe', universe || '');
  tr.innerHTML = `<td></td><td>${title}</td>`;
  return tr;
}

function buildCard(title, universe) {
  const card = document.createElement('div');
  card.className = 'ab-card';
  card.setAttribute('data-title', title);
  card.setAttribute('data-universe', universe || '');
  return card;
}

describe('Universe filter (extracted from templates/index.html)', () => {
  let renderPage;
  let fns;

  beforeEach(() => {
    document.body.innerHTML = `
      <table id="ab-table"><tbody></tbody></table>
      <div id="ab-cards"></div>
    `;
    const tbody = document.querySelector('#ab-table tbody');
    const cardsWrap = document.querySelector('#ab-cards');

    tbody.appendChild(buildRow('Warbreaker', 'The Cosmere'));
    tbody.appendChild(buildRow('Otherlife Dreams', 'Runnerverse'));
    tbody.appendChild(buildRow('Some Standalone Book', ''));

    cardsWrap.appendChild(buildCard('Warbreaker', 'The Cosmere'));
    cardsWrap.appendChild(buildCard('Otherlife Dreams', 'Runnerverse'));
    cardsWrap.appendChild(buildCard('Some Standalone Book', ''));

    renderPage = vi.fn();
    fns = loadUniverseFilterFunctions(renderPage);
  });

  it('marks only the matching universe as visible via dataset.searchMatch, in BOTH table and card views', () => {
    fns.filterByUniverse('The Cosmere');

    const rows = Array.from(document.querySelectorAll('#ab-table tbody tr'));
    const cards = Array.from(document.querySelectorAll('#ab-cards .ab-card'));

    expect(rows.map((r) => r.dataset.searchMatch)).toEqual(['1', '0', '0']);
    expect(cards.map((c) => c.dataset.searchMatch)).toEqual(['1', '0', '0']);
  });

  it('a different universe selects a different, disjoint set of rows', () => {
    fns.filterByUniverse('Runnerverse');

    const rows = Array.from(document.querySelectorAll('#ab-table tbody tr'));
    expect(rows.map((r) => r.dataset.searchMatch)).toEqual(['0', '1', '0']);
  });

  it('a universe with no matches hides everything, including books with no universe at all', () => {
    fns.filterByUniverse('Maasverse');

    const rows = Array.from(document.querySelectorAll('#ab-table tbody tr'));
    const cards = Array.from(document.querySelectorAll('#ab-cards .ab-card'));
    expect(rows.every((r) => r.dataset.searchMatch === '0')).toBe(true);
    expect(cards.every((c) => c.dataset.searchMatch === '0')).toBe(true);
  });

  it('calls renderPage() after filtering — the same choke point My TBR List / My Reviews use', () => {
    fns.filterByUniverse('The Cosmere');
    expect(renderPage).toHaveBeenCalledTimes(1);
  });

  it('clearUniverseFilter() resets every row and card back to visible', () => {
    fns.filterByUniverse('The Cosmere'); // narrow first
    fns.clearUniverseFilter();

    const rows = Array.from(document.querySelectorAll('#ab-table tbody tr'));
    const cards = Array.from(document.querySelectorAll('#ab-cards .ab-card'));
    expect(rows.every((r) => r.dataset.searchMatch === '1')).toBe(true);
    expect(cards.every((c) => c.dataset.searchMatch === '1')).toBe(true);
    expect(renderPage).toHaveBeenCalledTimes(2);
  });
});
