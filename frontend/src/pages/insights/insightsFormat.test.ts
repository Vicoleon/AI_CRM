// How every Insights number reads. Runs on Node's built-in test runner:
//
//   cd <repo>/frontend && node --test "src/pages/insights/insightsFormat.test.ts"
//
// Pinned here: each unit's format, that a missing value is a dash and never
// zero, that rates move in points, that a delta's colour follows the metric's
// good direction, and that a percentage's "x of y" is always available.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import {
    NO_VALUE,
    barFraction,
    bucketLabel,
    computeDelta,
    deltaArrow,
    deltaDirection,
    deltaSentence,
    deltaTone,
    denominatorSentence,
    denominatorText,
    formatCount,
    formatDays,
    formatDelta,
    formatMs,
    formatPct,
    formatRatio,
    formatScore,
    formatTick,
    formatUsd,
    formatValue,
    plural,
    ratioOf,
} from './insightsFormat.ts';

describe('formatValue by unit', () => {
    it('counts get thousands separators, and compact only when asked', () => {
        assert.equal(formatCount(1234), '1,234');
        assert.equal(formatCount(2.5), '2.5');
        assert.equal(formatCount(12_900, { compact: true }), '12.9K');
        assert.equal(formatCount(9_999, { compact: true }), '9,999');
        assert.equal(formatValue(1_250_000, 'count', { compact: true }), '1.3M');
        assert.equal(formatValue(1_250_000, 'count'), '1,250,000');
    });

    it('rates are 0–1 and keep a decimal under 10%', () => {
        assert.equal(formatPct(0.333), '33%');
        assert.equal(formatPct(0.045), '4.5%');
        assert.equal(formatPct(1), '100%');
        assert.equal(formatPct(0), '0%');
        assert.equal(formatValue(0.8, 'pct'), '80%');
    });

    it('dollars: cents under $100, whole dollars above, fractions of a cent kept visible', () => {
        assert.equal(formatUsd(12.345), '$12.35');
        assert.equal(formatUsd(1234.5), '$1,235');
        assert.equal(formatUsd(0.0031), '$0.0031');
        assert.equal(formatUsd(0), '$0.00');
        assert.equal(formatUsd(-5), '-$5.00');
        assert.equal(formatUsd(48_200, { compact: true }), '$48.2K');
    });

    it('milliseconds read as ms, seconds or minutes', () => {
        assert.equal(formatMs(850), '850 ms');
        assert.equal(formatMs(1200), '1.2 s');
        assert.equal(formatMs(1000), '1 s');
        assert.equal(formatMs(150_000), '2.5 min');
    });

    it('days, ratios and scores', () => {
        assert.equal(formatDays(1), '1 day');
        assert.equal(formatDays(2.5), '2.5 days');
        assert.equal(formatRatio(2.4), '2.4');
        assert.equal(formatScore(0.8234), '0.82');
    });

    it('text, dates and booleans', () => {
        assert.equal(formatValue('Sales', 'text'), 'Sales');
        assert.equal(formatValue('2026-10-07', 'date'), 'Oct 7, 2026');
        assert.equal(formatValue(true, 'count'), 'Yes');
        assert.match(formatValue('2026-10-08T15:42:00Z', 'datetime'), /Oct 8/);
    });

    it('a missing value is a dash, never zero', () => {
        for (const unit of ['count', 'pct', 'usd', 'ms', 'days', 'ratio', 'score', 'text', 'date']) {
            assert.equal(formatValue(null, unit), NO_VALUE, unit);
            assert.equal(formatValue(undefined, unit), NO_VALUE, unit);
        }
        assert.equal(formatValue(0, 'count'), '0');
    });

    it('numeric strings from the wire still format', () => {
        assert.equal(formatValue('1234', 'count'), '1,234');
        assert.equal(formatValue('0.5', 'pct'), '50%');
    });
});

describe('axis ticks', () => {
    it('are short', () => {
        assert.equal(formatTick(0.4, 'pct'), '40%');
        assert.equal(formatTick(12_000, 'count'), '12K');
        assert.equal(formatTick(5, 'count'), '5');
        assert.equal(formatTick(1500, 'ms'), '1.5 s');
    });
});

describe('denominators', () => {
    it('prints "x of y" with separators', () => {
        assert.equal(denominatorText(212, 640), '212 of 640');
        assert.equal(denominatorText(1212, 6400), '1,212 of 6,400');
        assert.equal(denominatorText(0, 0), '0 of 0');
    });

    it('is null when either side is missing', () => {
        assert.equal(denominatorText(null, 640), null);
        assert.equal(denominatorText(212, undefined), null);
    });

    it('names what it is out of when the catalog says', () => {
        assert.equal(denominatorSentence(12, 15, 'people with Cubie access in scope'), '12 of 15 people with Cubie access in scope');
        assert.equal(denominatorSentence(12, 15, null), '12 of 15');
        // Averages spread a total across a count; "of" would misread as a share.
        assert.equal(denominatorText(151.9, 23, 'usd'), '$152 across 23');
        assert.equal(denominatorText(1329, 400, 'ratio'), '1,329 across 400');
        assert.equal(denominatorSentence(151.9, 23, 'active people', 'usd'), '$152 across 23 active people');
    });

    it('prints a share of dollars in dollars when the catalog gives a part unit', () => {
        assert.equal(denominatorText(57.1, 215.4, 'pct', 'usd'), '$57 of $215');
        assert.equal(denominatorText(1234.5, 5000, 'pct', 'usd'), '$1,235 of $5,000');
        assert.equal(denominatorText(0.42, 3, 'pct', 'usd'), '$0.42 of $3');
        assert.equal(denominatorSentence(57.1, 215.4, 'AI spend in the period', 'pct', 'usd'), '$57 of $215 AI spend in the period');
        // No part unit (or null): counts, as before.
        assert.equal(denominatorText(57, 215, 'pct', null), '57 of 215');
    });

    it('ratioOf refuses to divide by zero', () => {
        assert.equal(ratioOf(1, 0), null);
        assert.equal(ratioOf(1, 4), 0.25);
    });
});

describe('deltas', () => {
    it('computes the absolute and relative change', () => {
        assert.deepEqual(computeDelta(0.33, 0.29)?.abs.toFixed(2), '0.04');
        const d = computeDelta(120, 100)!;
        assert.equal(d.abs, 20);
        assert.equal(d.rel, 0.2);
    });

    it('has no relative change from zero, and no delta without both sides', () => {
        assert.equal(computeDelta(5, 0)?.rel, null);
        assert.equal(computeDelta(null, 3), null);
        assert.equal(computeDelta(3, undefined), null);
    });

    it('colours by the metric’s good direction, not by up or down', () => {
        const up = computeDelta(10, 5);
        const down = computeDelta(5, 10);
        assert.equal(deltaTone(up, 'up'), 'good');
        assert.equal(deltaTone(down, 'up'), 'bad');
        assert.equal(deltaTone(down, 'down'), 'good');
        assert.equal(deltaTone(up, 'down'), 'bad');
        assert.equal(deltaTone(up, 'neutral'), 'neutral');
        assert.equal(deltaTone(computeDelta(5, 5), 'up'), 'neutral');
        assert.equal(deltaTone(null, 'up'), 'neutral');
    });

    it('arrows and directions', () => {
        assert.equal(deltaArrow(computeDelta(2, 1)), '▲');
        assert.equal(deltaArrow(computeDelta(1, 2)), '▼');
        assert.equal(deltaArrow(computeDelta(1, 1)), '–');
        assert.equal(deltaDirection(null), 'flat');
        // Too small to print is no change: no red "▼ −0.00".
        assert.equal(deltaTone(computeDelta(0.771, 0.773), 'up', 'score'), 'neutral');
        assert.equal(formatDelta(computeDelta(0.771, 0.773), 'score'), 'No change');
        assert.equal(deltaTone(computeDelta(0.3301, 0.3300), 'up', 'pct'), 'neutral');
        assert.equal(deltaTone(computeDelta(0.78, 0.77), 'up', 'score'), 'good');
    });

    it('rates move in points, other units in percent', () => {
        assert.equal(formatDelta(computeDelta(0.33, 0.29), 'pct'), '+4 pts');
        assert.equal(formatDelta(computeDelta(0.29, 0.33), 'pct'), '−4 pts');
        assert.equal(formatDelta(computeDelta(0.5, 0.2), 'pct'), '+30 pts');
        assert.equal(formatDelta(computeDelta(112, 100), 'count'), '+12%');
        assert.equal(formatDelta(computeDelta(5, 0), 'count'), '+5');
        assert.equal(formatDelta(computeDelta(1, 1), 'count'), 'No change');
        assert.equal(formatDelta(computeDelta(0.9, 0.85), 'score'), '+0.05');
    });

    it('says it in a sentence for tooltips and screen readers', () => {
        assert.equal(
            deltaSentence(computeDelta(0.33, 0.29), 'pct', 0.29, 'vs previous 30 days'),
            'Up 4 pts vs previous 30 days (was 29%)',
        );
        assert.equal(deltaSentence(null, 'count', null, 'vs previous 30 days'), 'No earlier figure to compare with the previous 30 days.');
    });
});

describe('bars and buckets', () => {
    it('draws rates against 100%, counts against the largest', () => {
        assert.equal(barFraction(0.3, 0.3, 'pct'), 0.3);
        assert.equal(barFraction(15, 30, 'count'), 0.5);
        assert.equal(barFraction(null, 30, 'count'), 0);
        assert.equal(barFraction(-2, 30, 'count'), 0);
    });

    it('labels buckets without a timezone shift', () => {
        assert.equal(bucketLabel('2026-09-09', 'day'), 'Sep 9');
        assert.equal(bucketLabel('2026-09-07', 'week'), 'Wk of Sep 7');
    });

    it('pluralises', () => {
        assert.equal(plural(1, 'person', 'people'), '1 person');
        assert.equal(plural(63, 'person', 'people'), '63 people');
        assert.equal(plural(2, 'day'), '2 days');
    });
});
