// @vitest-environment jsdom
//
// The WIRING half of the Universe filter, which universe-filter.test.js does
// not reach: that test exercises filterByUniverse()/clearUniverseFilter()
// directly, but nothing proved the #ab-sort <option> values actually ROUTE to
// them, or that adding a universe branch to _dispatchSortSelectAction() left
// the My TBR List / My Reviews and plain-sort branches alone.
//
// Two independent things are checked here, because they can drift apart:
//
//   1. _dispatchSortSelectAction(), extracted from the template the same way
//      universe-filter.test.js extracts the filter functions, so a rename or
//      a changed option grammar breaks the test rather than the page.
//
//   2. ⚠️ EXACT-STRING agreement between the option values in the SHIPPED
//      site/index.html and the data-universe attributes on its own rows. The
//      shared universe list deliberately holds near-identical names as
//      DIFFERENT universes ("The Cosmere" vs "Cosmere"), and filterByUniverse
//      compares with === , so a fold, a trim or a title-case anywhere between
//      _universe_filter_options() and _book_data_attrs() would leave an
//      <option> in the dropdown that silently matches nothing. Comparing the
//      generated artifact against itself is the only thing that catches it.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TEMPLATE_PATH = path.resolve(__dirname, '../../app/web/templates/index.html');
const GENERATED_PATH = path.resolve(__dirname, '../index.html');
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

/**
 * Build the real dispatcher from the template with every collaborator it
 * calls replaced by a spy, so what is asserted is purely the ROUTING.
 */
function loadDispatcher(spies) {
  const src = extractFunctionSource(templateSource, '_dispatchSortSelectAction');
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    'sortSelect',
    'clearUniverseFilter',
    'filterByUniverse',
    'filterByReadingList',
    'clearReadingListFilter',
    'doSort',
    `
    var descDefaultSorts = {rating:true, series_index_sort:true};
    ${src}
    return _dispatchSortSelectAction;
    `
  );
  return factory(
    spies.sortSelect,
    spies.clearUniverseFilter,
    spies.filterByUniverse,
    spies.filterByReadingList,
    spies.clearReadingListFilter,
    spies.doSort
  );
}

describe('#ab-sort dispatcher routing (extracted from templates/index.html)', () => {
  let spies;
  let dispatch;

  beforeEach(() => {
    spies = {
      sortSelect: { value: '' },
      clearUniverseFilter: vi.fn(),
      filterByUniverse: vi.fn(),
      filterByReadingList: vi.fn(),
      clearReadingListFilter: vi.fn(),
      doSort: vi.fn(),
    };
    dispatch = loadDispatcher(spies);
  });

  it('routes a "_universe:<Name>|filter" option to filterByUniverse with the bare name', () => {
    spies.sortSelect.value = '_universe:The Cosmere|filter';
    dispatch();

    expect(spies.filterByUniverse).toHaveBeenCalledWith('The Cosmere');
    expect(spies.filterByReadingList).not.toHaveBeenCalled();
    expect(spies.doSort).not.toHaveBeenCalled();
  });

  it('routes "_universe_clear|filter" to clearUniverseFilter, never to a sort', () => {
    spies.sortSelect.value = '_universe_clear|filter';
    dispatch();

    expect(spies.clearUniverseFilter).toHaveBeenCalledTimes(1);
    expect(spies.filterByUniverse).not.toHaveBeenCalled();
    expect(spies.doSort).not.toHaveBeenCalled();
  });

  it('leaves the My TBR List / My Reviews filters routing to filterByReadingList', () => {
    for (const key of ['_mytbr', '_myreviews']) {
      spies.filterByReadingList.mockClear();
      spies.filterByUniverse.mockClear();
      spies.sortSelect.value = `${key}|filter`;
      dispatch();

      expect(spies.filterByReadingList).toHaveBeenCalledWith(key);
      expect(spies.filterByUniverse).not.toHaveBeenCalled();
    }
  });

  it('still routes ordinary sort options to doSort, ascending by default', () => {
    spies.sortSelect.value = 'title|txt';
    dispatch();

    expect(spies.doSort).toHaveBeenCalledWith(true);
    expect(spies.clearReadingListFilter).toHaveBeenCalledTimes(1);
    expect(spies.filterByUniverse).not.toHaveBeenCalled();
    expect(spies.clearUniverseFilter).not.toHaveBeenCalled();
  });

  it('keeps rating / series-# descending by default', () => {
    for (const key of ['rating', 'series_index_sort']) {
      spies.doSort.mockClear();
      spies.sortSelect.value = `${key}|num`;
      dispatch();
      expect(spies.doSort).toHaveBeenCalledWith(false);
    }
  });

  it('a universe name containing spaces and a colon survives the option grammar', () => {
    // The value is split on "|" and sliced after the FIRST "_universe:", so a
    // name is free to contain colons and spaces. Only a literal "|" in a
    // universe name would break this, and none exists in the shared list.
    spies.sortSelect.value = '_universe:Star Wars: Legends|filter';
    dispatch();
    expect(spies.filterByUniverse).toHaveBeenCalledWith('Star Wars: Legends');
  });
});

describe('shipped site/index.html: dropdown options match the rows they filter', () => {
  // The generated page is a build artifact. It is tracked, but skip rather
  // than fail if a checkout somehow lacks it — this test guards drift, and a
  // missing artifact is a different problem with its own louder signal.
  const generated = existsSync(GENERATED_PATH) ? readFileSync(GENERATED_PATH, 'utf-8') : null;

  const decode = (s) =>
    s
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&amp;/g, '&');

  const optionNames = generated
    ? [...generated.matchAll(/<option value="_universe:([^"]*)\|filter">/g)].map((m) => decode(m[1]))
    : [];
  const rowNames = generated
    ? new Set(
        [...generated.matchAll(/data-universe="([^"]*)"/g)].map((m) => decode(m[1])).filter(Boolean)
      )
    : new Set();

  it.skipIf(!generated)('offers at least one universe option', () => {
    expect(optionNames.length).toBeGreaterThan(0);
  });

  it.skipIf(!generated)('every option name matches some row EXACTLY — no fold, trim or re-casing', () => {
    const orphans = optionNames.filter((name) => !rowNames.has(name));
    expect(orphans).toEqual([]);
  });

  it.skipIf(!generated)('every universe present on a row is offered as an option', () => {
    const offered = new Set(optionNames);
    const unreachable = [...rowNames].filter((name) => !offered.has(name));
    expect(unreachable).toEqual([]);
  });

  it.skipIf(!generated)('offers the clear option exactly once, and it precedes the universe options', () => {
    const clearIdx = generated.indexOf('<option value="_universe_clear|filter">');
    const firstUniverseIdx = generated.indexOf('<option value="_universe:');
    expect(clearIdx).toBeGreaterThan(-1);
    expect(generated.split('<option value="_universe_clear|filter">').length - 1).toBe(1);
    expect(clearIdx).toBeLessThan(firstUniverseIdx);
  });
});
