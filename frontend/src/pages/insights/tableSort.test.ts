// Sorting, searching and paging rules for Insights tables.
//
//   cd <repo>/frontend && node --test "src/pages/insights/tableSort.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import type { TableColumn, TableRow } from '../../api/insights';
import { PAGE_SIZE, ariaSort, compareValues, filterRows, initialSort, nextSort, sortRows } from './tableSort.ts';

const row = (key: string, cells: TableRow['cells']): TableRow => ({ key, cells });

const ROWS: TableRow[] = [
    row('a@x.example', { name: 'Ana', days: 9, last: '2026-10-01' }),
    row('b@x.example', { name: 'bo', days: 10, last: null }),
    row('c@x.example', { name: 'Cy', days: null, last: '2026-09-01' }),
    row('d@x.example', { name: 'Di', days: 9, last: '2026-10-05' }),
];

const keys = (rows: TableRow[]) => rows.map(r => r.key);

describe('compareValues', () => {
    it('sorts numbers as numbers, text alphabetically ignoring case', () => {
        assert.ok(compareValues(9, 10, 'count') < 0);
        assert.ok(compareValues('9', '10', 'count') < 0);
        assert.ok(compareValues('bo', 'Cy', 'text') < 0);
        assert.ok(compareValues('2026-09-01', '2026-10-01', 'date') < 0);
    });
});

describe('sortRows', () => {
    it('sorts numbers high to low and puts missing values last', () => {
        assert.deepEqual(keys(sortRows(ROWS, { key: 'days', dir: 'desc' }, 'count')), ['b@x.example', 'a@x.example', 'd@x.example', 'c@x.example']);
    });

    it('keeps missing values last when ascending too', () => {
        assert.deepEqual(keys(sortRows(ROWS, { key: 'days', dir: 'asc' }, 'count')), ['a@x.example', 'd@x.example', 'b@x.example', 'c@x.example']);
    });

    it('is stable for ties (the server’s order wins)', () => {
        const sorted = sortRows(ROWS, { key: 'days', dir: 'desc' }, 'count');
        assert.ok(sorted.indexOf(ROWS[0]) < sorted.indexOf(ROWS[3]));
    });

    it('sorts dates and text', () => {
        assert.deepEqual(keys(sortRows(ROWS, { key: 'last', dir: 'desc' }, 'date')), ['d@x.example', 'a@x.example', 'c@x.example', 'b@x.example']);
        assert.deepEqual(keys(sortRows(ROWS, { key: 'name', dir: 'asc' }, 'text')), ['a@x.example', 'b@x.example', 'c@x.example', 'd@x.example']);
    });

    it('returns a copy in the server’s order with no sort', () => {
        const out = sortRows(ROWS, null, 'text');
        assert.notEqual(out, ROWS);
        assert.deepEqual(out, ROWS);
    });
});

describe('header clicks', () => {
    it('start numbers high-to-low and text A-to-Z, then flip', () => {
        assert.deepEqual(nextSort(null, 'days', 'count'), { key: 'days', dir: 'desc' });
        assert.deepEqual(nextSort(null, 'name', 'text'), { key: 'name', dir: 'asc' });
        assert.deepEqual(nextSort({ key: 'days', dir: 'desc' }, 'days', 'count'), { key: 'days', dir: 'asc' });
    });

    it('report aria-sort per column', () => {
        assert.equal(ariaSort({ key: 'days', dir: 'desc' }, 'days'), 'descending');
        assert.equal(ariaSort({ key: 'days', dir: 'asc' }, 'days'), 'ascending');
        assert.equal(ariaSort({ key: 'days', dir: 'asc' }, 'name'), 'none');
        assert.equal(ariaSort(null, 'name'), 'none');
    });
});

describe('filterRows', () => {
    const columns: TableColumn[] = [
        { key: 'name', label: 'Person', unit: 'text' },
        { key: 'days', label: 'Days', unit: 'count' },
    ];

    it('matches text cells and the row key, ignoring case', () => {
        assert.deepEqual(keys(filterRows(ROWS, 'BO', columns)), ['b@x.example']);
        assert.deepEqual(keys(filterRows(ROWS, 'd@x', columns)), ['d@x.example']);
        assert.equal(filterRows(ROWS, '  ', columns).length, ROWS.length);
    });
});

describe('initialSort', () => {
    it('uses the server’s default only when it names a real column', () => {
        const columns: TableColumn[] = [{ key: 'days', label: 'Days', unit: 'count' }];
        assert.deepEqual(initialSort(columns, { key: 'days', dir: 'desc' }), { key: 'days', dir: 'desc' });
        assert.equal(initialSort(columns, { key: 'nope', dir: 'desc' }), null);
        assert.equal(initialSort(columns, null), null);
    });

    it('pages at 25', () => {
        assert.equal(PAGE_SIZE, 25);
    });
});
