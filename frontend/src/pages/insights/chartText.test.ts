// Chart summaries, funnel arithmetic, heat-cell scaling, the activity
// calendar and the palette.
//
//   cd <repo>/frontend && node --test "src/pages/insights/chartText.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import type { MatrixData } from '../../api/insights';
import {
    CALENDAR_SHADES,
    OTHER_COLOR,
    SERIES_COLORS,
    divergingColor,
    inkOn,
    ordinalColor,
    sequentialColor,
    seriesColor,
} from '../../components/insights/palette.ts';
import { buildCalendar, calendarLevel, mondayIndex } from './activityCalendar.ts';
import { barSummary, funnelRows, funnelSummary, matrixCellText, matrixDomain, matrixPosition, trendSummary } from './chartText.ts';

describe('trendSummary', () => {
    it('says where each series starts, ends and peaks', () => {
        const text = trendSummary(
            'Daily active people',
            {
                x: ['2026-09-09', '2026-09-10', '2026-09-11'],
                chart: 'line',
                stacked: false,
                series: [{ key: 'dau', label: 'Daily active', unit: 'count', values: [3, 9, 5] }],
            },
            'day',
        );
        assert.equal(text, 'Daily active people. Daily active: 3 on Sep 9 to 5 on Sep 11, peak 9.');
    });

    it('handles an empty chart', () => {
        assert.match(trendSummary('X', { x: [], chart: 'bar', stacked: false, series: [] }, 'day'), /no data/);
    });
});

describe('barSummary', () => {
    it('reads each rate with what it is out of', () => {
        assert.equal(
            barSummary('Active by department', {
                unit: 'pct',
                items: [
                    { key: 'sales', label: 'Sales', value: 0.8, numerator: 12, denominator: 15 },
                    { key: 'mkt', label: 'Marketing', value: 0.5, numerator: 4, denominator: 8 },
                ],
            }),
            'Active by department: Sales 80% (12 of 15), Marketing 50% (4 of 8).',
        );
    });
});

describe('funnel', () => {
    const steps = [
        { key: 'surfaced', label: 'Surfaced', value: 640 },
        { key: 'acted', label: 'Acted on', value: 212 },
        { key: 'resolved', label: 'Resolved', value: 106 },
    ];

    it('rates each step against the first, and against the step before', () => {
        const rows = funnelRows(steps);
        assert.equal(rows[0].ofFirst, 1);
        assert.equal(rows[0].ofPrev, null);
        assert.equal(rows[1].ofFirst, 212 / 640);
        assert.equal(rows[2].ofPrev, 0.5);
    });

    it('never divides by an empty first step', () => {
        const rows = funnelRows([{ key: 'a', label: 'A', value: 0 }, { key: 'b', label: 'B', value: 0 }]);
        assert.equal(rows[1].ofFirst, null);
        assert.equal(rows[1].ofPrev, null);
    });

    it('summarises with denominators', () => {
        assert.match(funnelSummary('Priorities', steps), /Acted on 212 \(33% of surfaced, 212 of 640\)/);
    });
});

describe('heat cells', () => {
    const base: MatrixData = {
        unit: 'pct',
        scale: 'sequential',
        rows: [{ key: 'sales', label: 'Sales' }],
        cols: [{ key: 'chat', label: 'Ask Cubie' }],
        cells: [[{ value: 0.3, numerator: 3, denominator: 10 }]],
    };

    it('runs rates on a 0–100% scale, not "lowest to highest on the grid"', () => {
        assert.deepEqual(matrixDomain(base), { min: 0, max: 1 });
        assert.equal(matrixPosition(0.3, matrixDomain(base), 'sequential'), 0.3);
    });

    it('runs counts from zero to the largest cell', () => {
        const counts: MatrixData = { ...base, unit: 'count', cells: [[{ value: 4 }, { value: 8 }]] };
        assert.deepEqual(matrixDomain(counts), { min: 0, max: 8 });
    });

    it('centres a diverging scale on zero', () => {
        const div: MatrixData = { ...base, unit: 'count', scale: 'diverging', cells: [[{ value: -2 }, { value: 4 }]] };
        const d = matrixDomain(div);
        assert.deepEqual(d, { min: -4, max: 4 });
        assert.equal(matrixPosition(-2, d, 'diverging'), -0.5);
        assert.equal(matrixPosition(4, d, 'diverging'), 1);
    });

    it('describes a cell with what it is out of', () => {
        assert.equal(matrixCellText('Sales', 'Ask Cubie', base.cells[0][0], 'pct'), 'Sales × Ask Cubie: 30% (3 of 10)');
        assert.equal(matrixCellText('Sales', 'EOS', null, 'pct'), 'Sales × EOS: no data');
        // A value the server withheld says why, not "no data".
        assert.equal(
            matrixCellText('Ops', 'Call capture', { value: null, note: 'Hidden: fewer than 3 people' }, 'pct'),
            'Ops × Call capture: Hidden: fewer than 3 people',
        );
    });
});

describe('activity calendar', () => {
    it('finds the Monday-based weekday', () => {
        assert.equal(mondayIndex('2026-10-05'), 0); // a Monday
        assert.equal(mondayIndex('2026-10-11'), 6); // a Sunday
    });

    it('lays out 90 days in Monday-first weeks with padding outside the range', () => {
        const end = '2026-10-08'; // a Thursday
        const layout = buildCalendar([{ date: '2026-10-08', count: 4 }, { date: '2026-10-07', count: 1 }, { date: '2020-01-01', count: 99 }], end);
        const cells = layout.weeks.flat().filter(c => c !== null);
        assert.equal(cells.length, 90);
        assert.equal(cells[cells.length - 1]!.date, end);
        assert.equal(layout.weeks[0].length, 7);
        assert.equal(layout.weeks[layout.weeks.length - 1][3]?.date, end, 'Thursday is the fourth row');
        assert.equal(layout.weeks[layout.weeks.length - 1][4], null, 'days after the end are padding');
        assert.equal(layout.max, 4, 'counts outside the 90 days are ignored');
        assert.equal(layout.total, 5);
        assert.equal(layout.activeDays, 2);
        assert.ok(layout.months.length >= 3);
    });

    it('shades by share of the busiest day', () => {
        assert.equal(calendarLevel(0, 10), 0);
        assert.equal(calendarLevel(1, 10), 1);
        assert.equal(calendarLevel(10, 10), 4);
        assert.equal(calendarLevel(6, 10), 3);
    });

    it('returns an empty layout for a bad end date', () => {
        assert.equal(buildCalendar([], 'nope').weeks.length, 0);
    });
});

describe('palette', () => {
    it('has eight fixed categorical slots and never invents a ninth hue', () => {
        assert.equal(SERIES_COLORS.length, 8);
        assert.equal(seriesColor(0), '#7c3aed');
        assert.equal(seriesColor(8), OTHER_COLOR);
    });

    it('runs the sequential ramp light to dark', () => {
        assert.equal(sequentialColor(0), '#f5f3ff');
        assert.equal(sequentialColor(1), '#2e1065');
        assert.equal(sequentialColor(-5), sequentialColor(0));
        assert.equal(divergingColor(0), '#f0efec');
    });

    it('picks readable ink on a filled cell', () => {
        assert.equal(inkOn('#2e1065'), '#ffffff');
        assert.equal(inkOn('#f5f3ff'), '#111827');
    });

    it('orders funnel steps darkest first', () => {
        assert.notEqual(ordinalColor(0, 4), ordinalColor(3, 4));
        assert.equal(CALENDAR_SHADES.length, 5);
    });
});
