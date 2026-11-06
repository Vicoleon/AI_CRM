// The Insights filter codec: what the URL means, what it is rewritten to, and
// what is remembered between visits.
//
//   cd <repo>/frontend && node --test "src/pages/insights/insightsFilters.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import {
    DEFAULT_FILTERS,
    FILTERS_STORAGE_KEY,
    MAX_CUSTOM_DAYS,
    RANGE_PRESETS,
    addDays,
    comparisonCaption,
    customRangeError,
    daysInclusive,
    expectedDays,
    filterQueryFromSearch,
    filtersKey,
    filtersToParams,
    filtersToQuery,
    hasFilterParams,
    isIsoDate,
    mergeFilterParams,
    parseDepartments,
    parseFilters,
    rangeCaption,
    rawFilterQuery,
    rememberFilters,
    restoreFilters,
    sameFilters,
    todayIn,
    toggleDepartment,
    withCustomDates,
    withDepartments,
    withFilterQuery,
    withRange,
} from './insightsFilters.ts';
import type { FilterStorage } from './insightsFilters.ts';

const memoryStorage = (): FilterStorage & { data: Map<string, string> } => {
    const data = new Map<string, string>();
    return {
        data,
        getItem: k => data.get(k) ?? null,
        setItem: (k, v) => void data.set(k, v),
    };
};

describe('presets', () => {
    it('are the contract’s six, defaulting to 30 days', () => {
        assert.deepEqual([...RANGE_PRESETS], ['7d', '30d', '90d', 'qtd', 'ytd', 'custom']);
        assert.equal(DEFAULT_FILTERS.range, '30d');
        assert.deepEqual(DEFAULT_FILTERS.dept, []);
    });
});

describe('dates', () => {
    it('accepts only real calendar days', () => {
        assert.ok(isIsoDate('2026-02-28'));
        assert.ok(!isIsoDate('2026-02-30'));
        assert.ok(!isIsoDate('2026-2-3'));
        assert.ok(!isIsoDate(null));
    });

    it('moves and counts days without DST surprises', () => {
        assert.equal(addDays('2026-03-07', 2), '2026-03-09');
        assert.equal(addDays('2026-01-01', -1), '2025-12-31');
        assert.equal(daysInclusive('2026-09-09', '2026-10-08'), 30);
        assert.equal(daysInclusive('2026-10-08', '2026-10-08'), 1);
    });

    it('explains why a custom range can’t be used', () => {
        assert.equal(customRangeError('2026-09-01', '2026-09-30'), null);
        assert.match(customRangeError(null, '2026-09-30')!, /start date and an end date/);
        assert.match(customRangeError('2026-09-30', '2026-09-01')!, /after the end/);
        assert.match(customRangeError('2025-01-01', '2026-09-30')!, new RegExp(String(MAX_CUSTOM_DAYS)));
        assert.equal(customRangeError('2025-10-08', '2026-10-08'), null, '366 days inclusive is allowed');
    });

    it('knows today in Chicago, not in UTC', () => {
        // 02:00 UTC on Oct 9 is still the evening of Oct 8 in Chicago.
        assert.equal(todayIn('America/Chicago', new Date('2026-10-09T02:00:00Z')), '2026-10-08');
        assert.equal(todayIn('America/Chicago', new Date('2026-10-09T12:00:00Z')), '2026-10-09');
    });
});

describe('parseFilters', () => {
    it('reads departments as unique lower-case slugs in order', () => {
        assert.deepEqual(parseDepartments('sales, Marketing,sales,,bad slug!,support'), ['sales', 'marketing', 'support']);
        assert.deepEqual(parseFilters('dept=sales,marketing&range=90d').dept, ['sales', 'marketing']);
    });

    it('falls back to the default range on anything unknown', () => {
        assert.equal(parseFilters('range=forever').range, '30d');
        assert.equal(parseFilters('').range, '30d');
        assert.equal(parseFilters('range=ytd').range, 'ytd');
    });

    it('keeps a valid custom range and drops an invalid one', () => {
        assert.deepEqual(parseFilters('range=custom&start=2026-09-01&end=2026-09-30'), {
            dept: [],
            range: 'custom',
            start: '2026-09-01',
            end: '2026-09-30',
        });
        assert.equal(parseFilters('range=custom&start=2026-09-30&end=2026-09-01').range, '30d');
        assert.equal(parseFilters('range=custom&start=2026-09-01').range, '30d');
    });

    it('ignores start and end on a preset', () => {
        const f = parseFilters('range=7d&start=2026-09-01&end=2026-09-30');
        assert.equal(f.start, null);
        assert.equal(f.end, null);
    });
});

describe('serialising', () => {
    it('always writes the range, so a shared link carries its own', () => {
        assert.deepEqual(filtersToParams(DEFAULT_FILTERS), { range: '30d' });
        assert.equal(filtersToQuery(DEFAULT_FILTERS), '?range=30d');
    });

    it('writes departments with a readable comma, and dates only for custom', () => {
        assert.equal(filtersToQuery({ dept: ['sales', 'marketing'], range: '90d', start: null, end: null }), '?dept=sales,marketing&range=90d');
        assert.equal(
            filtersToQuery({ dept: [], range: 'custom', start: '2026-09-01', end: '2026-09-30' }),
            '?range=custom&start=2026-09-01&end=2026-09-30',
        );
    });

    it('round-trips', () => {
        const f = { dept: ['sales'], range: 'custom' as const, start: '2026-08-01', end: '2026-09-15' };
        assert.deepEqual(parseFilters(filtersToQuery(f)), f);
        assert.ok(sameFilters(f, parseFilters(filtersToQuery(f))));
        assert.equal(filtersKey(f), filtersToQuery(f));
    });

    it('replaces only filter params, keeping anything else on the URL', () => {
        const prev = new URLSearchParams('tab=x&dept=ops&range=7d&start=2026-01-01');
        const next = mergeFilterParams(prev, { dept: ['sales'], range: '30d', start: null, end: null });
        assert.equal(next.get('tab'), 'x');
        assert.equal(next.get('dept'), 'sales');
        assert.equal(next.get('range'), '30d');
        assert.equal(next.has('start'), false);
    });

    it('spots a URL that needs rewriting to what the page shows', () => {
        assert.equal(rawFilterQuery(new URLSearchParams('range=30d&dept=sales')), '?dept=sales&range=30d');
        const bad = new URLSearchParams('range=forever');
        assert.notEqual(rawFilterQuery(bad), filtersToQuery(parseFilters(bad)));
        const good = new URLSearchParams('dept=sales&range=30d');
        assert.equal(rawFilterQuery(good), filtersToQuery(parseFilters(good)));
    });
});

describe('carrying filters between Insights pages', () => {
    it('takes only the filter params from the current URL, normalised', () => {
        assert.equal(filterQueryFromSearch('?dept=Sales&range=7d&tab=x'), '?dept=sales&range=7d');
        assert.equal(filterQueryFromSearch('?tab=x'), '');
        assert.equal(filterQueryFromSearch(''), '');
        assert.ok(hasFilterParams('?range=7d'));
        assert.ok(!hasFilterParams('?view=team'));
    });

    it('appends them to a link, keeping a query the link already has', () => {
        assert.equal(withFilterQuery('/insights/priorities', '?range=7d'), '/insights/priorities?range=7d');
        assert.equal(withFilterQuery('/insights/priorities', ''), '/insights/priorities');
        assert.equal(withFilterQuery('/insights/adoption?range=90d', '?dept=sales&range=7d'), '/insights/adoption?range=90d&dept=sales');
    });
});

describe('edits', () => {
    it('toggles and sets departments', () => {
        const one = toggleDepartment(DEFAULT_FILTERS, 'sales');
        assert.deepEqual(one.dept, ['sales']);
        assert.deepEqual(toggleDepartment(one, 'sales').dept, []);
        assert.deepEqual(withDepartments(DEFAULT_FILTERS, ['Sales', 'sales', 'support']).dept, ['sales', 'support']);
    });

    it('opens Custom on the last 30 days, and keeps an existing custom range', () => {
        const custom = withRange(DEFAULT_FILTERS, 'custom', '2026-10-08');
        assert.equal(custom.start, '2026-09-09');
        assert.equal(custom.end, '2026-10-08');
        assert.equal(withRange(custom, 'custom', '2026-12-01'), custom);
        assert.deepEqual(withRange(custom, '7d', '2026-10-08'), { dept: [], range: '7d', start: null, end: null });
    });

    it('refuses custom dates that can’t be used', () => {
        assert.equal(withCustomDates(DEFAULT_FILTERS, '2026-10-01', '2026-09-01'), null);
        assert.equal(withCustomDates(DEFAULT_FILTERS, '2026-09-01', '2026-10-01')?.range, 'custom');
    });
});

describe('captions', () => {
    it('names the range and the comparison window', () => {
        assert.equal(rangeCaption(DEFAULT_FILTERS), 'Last 30 days');
        assert.equal(rangeCaption({ dept: [], range: 'custom', start: '2026-09-01', end: '2026-09-30' }), 'Sep 1 – Sep 30, 2026');
        assert.equal(comparisonCaption(30), 'vs previous 30 days');
        assert.equal(comparisonCaption(1), 'vs previous day');
        assert.equal(comparisonCaption(null), 'vs previous period');
    });

    it('knows a preset’s length before the server answers, except QTD and YTD', () => {
        assert.equal(expectedDays({ ...DEFAULT_FILTERS, range: '7d' }), 7);
        assert.equal(expectedDays({ ...DEFAULT_FILTERS, range: 'qtd' }), null);
        assert.equal(expectedDays({ dept: [], range: 'custom', start: '2026-09-01', end: '2026-09-30' }), 30);
    });
});

describe('remember / restore', () => {
    it('round-trips through storage', () => {
        const store = memoryStorage();
        const f = { dept: ['sales'], range: '90d' as const, start: null, end: null };
        rememberFilters(f, store);
        assert.equal(store.data.get(FILTERS_STORAGE_KEY), '?dept=sales&range=90d');
        assert.deepEqual(restoreFilters(store), f);
    });

    it('returns nothing when nothing usable was stored', () => {
        assert.equal(restoreFilters(memoryStorage()), null);
        const junk = memoryStorage();
        junk.setItem(FILTERS_STORAGE_KEY, 'not a query');
        assert.equal(restoreFilters(junk), null);
        assert.equal(restoreFilters(null), null);
    });

    it('never throws when storage does', () => {
        const broken: FilterStorage = {
            getItem: () => {
                throw new Error('blocked');
            },
            setItem: () => {
                throw new Error('full');
            },
        };
        assert.doesNotThrow(() => rememberFilters(DEFAULT_FILTERS, broken));
        assert.equal(restoreFilters(broken), null);
    });

    it('validates what it restores through the URL codec', () => {
        const store = memoryStorage();
        store.setItem(FILTERS_STORAGE_KEY, '?range=custom&start=2026-09-30&end=2026-09-01');
        assert.equal(restoreFilters(store)?.range, '30d');
    });
});
