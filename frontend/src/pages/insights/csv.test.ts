// CSV export for Insights: RFC 4180 quoting, raw values, and one CSV per
// section kind.
//
//   cd <repo>/frontend && node --test "src/pages/insights/csv.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import type { Section } from '../../api/insights';
import {
    barToCsv,
    csvEscape,
    csvFilename,
    exportValue,
    funnelToCsv,
    headerWithUnit,
    matrixToCsv,
    sectionToCsv,
    tableToCsv,
    timeseriesToCsv,
    toCsv,
} from './csv.ts';

describe('csvEscape (RFC 4180)', () => {
    it('leaves plain fields alone', () => {
        assert.equal(csvEscape('Sales'), 'Sales');
        assert.equal(csvEscape(12), '12');
        assert.equal(csvEscape(0.5), '0.5');
    });

    it('quotes commas, quotes and line breaks, doubling inner quotes', () => {
        assert.equal(csvEscape('Smith, Jane'), '"Smith, Jane"');
        assert.equal(csvEscape('She said "hi"'), '"She said ""hi"""');
        assert.equal(csvEscape('two\nlines'), '"two\nlines"');
        assert.equal(csvEscape(' padded'), '" padded"');
    });

    it('writes missing values as empty fields, not "null"', () => {
        assert.equal(csvEscape(null), '');
        assert.equal(csvEscape(undefined), '');
        assert.equal(csvEscape(Number.NaN), '');
    });

    it('keeps a formula-looking text value as text', () => {
        assert.equal(csvEscape('=HYPERLINK("x")'), '"\'=HYPERLINK(""x"")"');
        assert.equal(csvEscape('@sum'), "'@sum");
        assert.equal(csvEscape(-5), '-5', 'a real negative number is untouched');
    });
});

describe('toCsv', () => {
    it('joins with CRLF and ends with one', () => {
        assert.equal(toCsv(['a', 'b'], [[1, 'x,y']]), 'a,b\r\n1,"x,y"\r\n');
    });
});

describe('units', () => {
    it('labels the unit in the header', () => {
        assert.equal(headerWithUnit('Acted on', 'pct'), 'Acted on (%)');
        assert.equal(headerWithUnit('Spend', 'usd'), 'Spend (USD)');
        assert.equal(headerWithUnit('People', 'count'), 'People');
    });

    it('exports rates as percentages, everything else raw', () => {
        assert.equal(exportValue(0.3333, 'pct'), 33.33);
        assert.equal(exportValue(1234.5, 'usd'), 1234.5);
        assert.equal(exportValue(null, 'pct'), null);
    });
});

describe('per kind', () => {
    it('table', () => {
        const csv = tableToCsv({
            columns: [
                { key: 'name', label: 'Person', unit: 'text' },
                { key: 'rate', label: 'Acted on', unit: 'pct' },
                { key: 'days', label: 'Active days', unit: 'count' },
            ],
            rows: [
                { key: 'a@x.example', cells: { name: 'Doe, Jane', rate: 0.5, days: 12 } },
                { key: 'b@x.example', cells: { name: 'Bo', rate: null, days: 0 } },
            ],
            row_link: 'person',
            default_sort: null,
        });
        assert.equal(csv, 'Person,Acted on (%),Active days\r\n"Doe, Jane",50,12\r\nBo,,0\r\n');
    });

    it('bar carries numerator and denominator', () => {
        const csv = barToCsv({ unit: 'pct', items: [{ key: 'sales', label: 'Sales', value: 0.8, numerator: 12, denominator: 15 }] }, 'Department');
        assert.equal(csv, 'Department,Value (%),Numerator,Denominator\r\nSales,80,12,15\r\n');
    });

    it('timeseries is one row per bucket', () => {
        const csv = timeseriesToCsv({
            x: ['2026-09-09', '2026-09-10'],
            chart: 'line',
            stacked: false,
            series: [
                { key: 'dau', label: 'Daily active', unit: 'count', values: [3, null] },
                { key: 'rate', label: 'Success', unit: 'pct', values: [0.9, 1] },
            ],
        });
        assert.equal(csv, 'Date,Daily active,Success (%)\r\n2026-09-09,3,90\r\n2026-09-10,,100\r\n');
    });

    it('funnel shares are of the first step', () => {
        const csv = funnelToCsv({ steps: [
            { key: 'surfaced', label: 'Surfaced', value: 640 },
            { key: 'acted', label: 'Acted on', value: 212 },
        ] });
        assert.equal(csv, 'Step,Count,Share of first step (%)\r\nSurfaced,640,100\r\nActed on,212,33.13\r\n');
    });

    it('matrix is long format so denominators travel with each cell', () => {
        const csv = matrixToCsv(
            {
                unit: 'pct',
                scale: 'sequential',
                rows: [{ key: 'sales', label: 'Sales' }],
                cols: [{ key: 'chat', label: 'Ask Cubie' }, { key: 'eos', label: 'EOS' }],
                cells: [[{ value: 0.8, numerator: 12, denominator: 15 }, null]],
            },
            'Department',
            'Feature',
        );
        assert.equal(csv, 'Department,Feature,Value (%),Numerator,Denominator\r\nSales,Ask Cubie,80,12,15\r\nSales,EOS,,,\r\n');
    });

    it('sectionToCsv dispatches on kind and refuses an unknown one', () => {
        const section: Section = {
            id: 'x',
            kind: 'bar',
            title: 'By department',
            width: 'half',
            drill: 'department',
            data: { unit: 'count', items: [{ key: 'sales', label: 'Sales', value: 3 }] },
        };
        assert.match(sectionToCsv(section)!, /^Department,Value/);
        assert.equal(sectionToCsv({ ...section, kind: 'sankey' } as unknown as Section), null);
    });
});

describe('csvFilename', () => {
    it('is a safe slug', () => {
        assert.equal(csvFilename(['insights', 'priorities', 'By Department', '2026-09-09', 'to', '2026-10-08']), 'insights-priorities-by-department-2026-09-09-to-2026-10-08.csv');
        assert.equal(csvFilename([null, '']), 'insights.csv');
    });
});
