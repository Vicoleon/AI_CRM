// What each Insights chart says in words, plus the small bits of arithmetic
// the chart components share (funnel conversions, heat-cell scaling). Pure, so
// the sentences a screen reader hears and the percentages a funnel prints are
// pinned by tests.
//
// Leaf apart from other leaf modules, imported with their `.ts` extension.
import type { BarData, FunnelStep, MatrixData, TimeseriesData } from '../../api/insights';
import { bucketLabel, denominatorText, formatPct, formatValue } from './insightsFormat.ts';

/** "Daily active: 3 on Sep 9 to 5 on Oct 8, peak 9." */
export function trendSummary(title: string, data: TimeseriesData, bucket: 'day' | 'week'): string {
    if (data.x.length === 0 || data.series.length === 0) return `${title}: no data for this period.`;
    const parts = data.series.slice(0, 4).map(s => {
        const pairs: { v: number; i: number }[] = [];
        s.values.forEach((v, i) => {
            if (typeof v === 'number' && Number.isFinite(v)) pairs.push({ v, i });
        });
        if (pairs.length === 0) return `${s.label}: no data`;
        const first = pairs[0];
        const last = pairs[pairs.length - 1];
        const peak = pairs.reduce((a, b) => (b.v > a.v ? b : a));
        return (
            `${s.label}: ${formatValue(first.v, s.unit)} on ${bucketLabel(data.x[first.i], bucket)}` +
            ` to ${formatValue(last.v, s.unit)} on ${bucketLabel(data.x[last.i], bucket)}, peak ${formatValue(peak.v, s.unit)}`
        );
    });
    const more = data.series.length > 4 ? ` And ${data.series.length - 4} more series.` : '';
    return `${title}. ${parts.join('. ')}.${more}`;
}

/** "By department: Sales 80% (12 of 15), Marketing 50% (4 of 8)…" — first five. */
export function barSummary(title: string, data: BarData): string {
    if (data.items.length === 0) return `${title}: nothing to show.`;
    const parts = data.items.slice(0, 5).map(i => {
        if ((i.value === null || i.value === undefined) && i.note) return `${i.label} ${i.note}`;
        const of = denominatorText(i.numerator, i.denominator);
        return `${i.label} ${formatValue(i.value, data.unit)}${of ? ` (${of})` : ''}`;
    });
    const more = data.items.length > 5 ? `, and ${data.items.length - 5} more` : '';
    return `${title}: ${parts.join(', ')}${more}.`;
}

export interface FunnelRow extends FunnelStep {
    /** Share of the first step, 0–1; null when the first step is 0. */
    ofFirst: number | null;
    /** Share of the step before, 0–1; null for the first step or after a 0. */
    ofPrev: number | null;
}

/** Each step's conversion from the first step and from the step before. */
export function funnelRows(steps: readonly FunnelStep[]): FunnelRow[] {
    const first = steps[0]?.value ?? 0;
    return steps.map((s, i) => {
        const prev = i > 0 ? steps[i - 1].value : null;
        return {
            ...s,
            ofFirst: first > 0 ? s.value / first : null,
            ofPrev: prev !== null && prev > 0 ? s.value / prev : null,
        };
    });
}

export function funnelSummary(title: string, steps: readonly FunnelStep[]): string {
    if (steps.length === 0) return `${title}: nothing to show.`;
    const rows = funnelRows(steps);
    const first = rows[0];
    const parts = rows.map((r, i) =>
        i === 0
            ? `${r.label} ${formatValue(r.value, 'count')}`
            : `${r.label} ${formatValue(r.value, 'count')}${r.ofFirst !== null ? ` (${formatPct(r.ofFirst)} of ${first.label.toLowerCase()}, ${denominatorText(r.value, first.value)})` : ''}`,
    );
    return `${title}: ${parts.join(', ')}.`;
}

export interface MatrixDomain {
    min: number;
    max: number;
}

/**
 * The colour scale's ends. Rates and scores run 0–100% so a pale cell always
 * means a low rate, not merely "lowest on this grid"; counts run from 0 to
 * the largest cell; a diverging scale is symmetric around 0.
 */
export function matrixDomain(data: MatrixData): MatrixDomain {
    const values: number[] = [];
    for (const row of data.cells) {
        for (const cell of row ?? []) {
            const v = cell?.value;
            if (typeof v === 'number' && Number.isFinite(v)) values.push(v);
        }
    }
    if (data.scale === 'diverging') {
        const m = Math.max(0, ...values.map(Math.abs));
        return { min: -(m || 1), max: m || 1 };
    }
    if (data.unit === 'pct' || data.unit === 'score') return { min: 0, max: Math.max(1, ...values) };
    return { min: Math.min(0, ...values), max: Math.max(1, ...values) };
}

/** Where a value sits on the scale: 0–1 sequential, −1…1 diverging. */
export function matrixPosition(value: number, domain: MatrixDomain, scale: MatrixData['scale']): number {
    if (scale === 'diverging') return value >= 0 ? value / domain.max : -value / domain.min;
    const span = domain.max - domain.min;
    return span > 0 ? (value - domain.min) / span : 0;
}

/** "Sales × Ask Cubie: 80% (12 of 15)". */
export function matrixCellText(rowLabel: string, colLabel: string, cell: MatrixData['cells'][number][number], unit: MatrixData['unit']): string {
    if (!cell || cell.value === null || cell.value === undefined)
        return `${rowLabel} × ${colLabel}: ${cell?.note ? cell.note : 'no data'}`;
    const of = denominatorText(cell.numerator, cell.denominator);
    return `${rowLabel} × ${colLabel}: ${formatValue(cell.value, unit)}${of ? ` (${of})` : ''}`;
}
