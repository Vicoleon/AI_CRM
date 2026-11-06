// Insights number formatting, deltas and denominators.
//
// One module decides how every number on every dashboard reads, so a KPI tile,
// a bar label, a table cell, a tooltip and the CSV header can never disagree
// about what "33%" or "1.2 s" means. Units are the contract's
// (docs/insights/README.md "Units"): `pct` and `score` arrive as 0–1.
//
// THE DENOMINATOR RULE. A percentage is never shown on its own: wherever a
// rate appears, `denominatorText` prints what it is out of ("212 of 640") in
// the same place. A number without its denominator is how a 90% and a 50%
// over different populations once caused a real escalation.
//
// A leaf module apart from two other leaf modules (imported WITH their `.ts`
// extension so `node --test` can load this file without a bundler).
import type { GoodDirection, InsightsUnit } from '../../api/insights';
import { parseInstant } from '../../utils/datetime.ts';
import { isIsoDate, longDate, shortDate } from './insightsFilters.ts';

// ─── numbers by unit ─────────────────────────────────────────────────────────

const INT = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const ONE = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });
const TWO = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const COMPACT = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

/** Numbers from 10,000 up read as "12.9K" when `compact` is asked for. */
const COMPACT_FROM = 10_000;

export interface FormatOptions {
    /** Shorten large counts and dollars ("12.9K", "$4.2M"). For tiles and axes, never tables. */
    compact?: boolean;
}

export const NO_VALUE = '—';

export function formatCount(v: number, { compact = false }: FormatOptions = {}): string {
    if (compact && Math.abs(v) >= COMPACT_FROM) return COMPACT.format(v);
    return Number.isInteger(v) ? INT.format(v) : ONE.format(v);
}

/** 0–1 → "33%". Under 10% keeps one decimal ("4.5%") so small rates don't round to nothing. */
export function formatPct(v: number): string {
    const p = v * 100;
    return `${Math.abs(p) < 10 ? ONE.format(p) : INT.format(p)}%`;
}

export function formatUsd(v: number, { compact = false }: FormatOptions = {}): string {
    const sign = v < 0 ? '-' : '';
    const a = Math.abs(v);
    if (compact && a >= COMPACT_FROM) return `${sign}$${COMPACT.format(a)}`;
    if (a >= 100) return `${sign}$${INT.format(a)}`;
    // Unit costs (per briefing, per answer) are fractions of a cent: keep them visible.
    if (a > 0 && a < 0.01) return `${sign}$${a.toFixed(4)}`;
    return `${sign}$${TWO.format(a)}`;
}

/** Milliseconds → "850 ms", "1.2 s", "2.5 min". */
export function formatMs(v: number): string {
    const a = Math.abs(v);
    if (a < 1000) return `${INT.format(v)} ms`;
    if (a < 60_000) return `${ONE.format(v / 1000)} s`;
    return `${ONE.format(v / 60_000)} min`;
}

export function formatDays(v: number): string {
    return `${ONE.format(v)} ${Math.abs(v) === 1 ? 'day' : 'days'}`;
}

/** "2.4×" — turns per conversation, revisions per proposal. */
// No "×": these are averages ("1.7 questions per conversation"), and a
// multiplier sign reads as "1.7 times more".
export const formatRatio = (v: number): string => ONE.format(v);

/** A 0–1 score, two places: "0.82". */
export const formatScore = (v: number): string => TWO.format(v);

/** "Oct 7, 2026" from `YYYY-MM-DD`, with no timezone shift. */
export const formatDate = (iso: string): string => (isIsoDate(iso) ? longDate(iso) : iso);

/** An instant in the viewer's own timezone: "Oct 7, 10:42 AM". */
export function formatDateTime(value: string): string {
    const at = parseInstant(value);
    if (!at) return value;
    return at.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

const asNumber = (value: unknown): number | null => {
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value === 'string' && value.trim() !== '') {
        const n = Number(value);
        return Number.isFinite(n) ? n : null;
    }
    return null;
};

/**
 * Format any value by its unit. Missing values read as an em dash, never as
 * "0" — "nothing to show" and "zero" are different answers.
 */
export function formatValue(value: unknown, unit: InsightsUnit | string, opts: FormatOptions = {}): string {
    if (value === null || value === undefined || value === '') return NO_VALUE;
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (unit === 'text') return String(value);
    if (unit === 'date') return formatDate(String(value));
    if (unit === 'datetime') return formatDateTime(String(value));
    const n = asNumber(value);
    if (n === null) return String(value);
    switch (unit) {
        case 'pct':
            return formatPct(n);
        case 'usd':
            return formatUsd(n, opts);
        case 'ms':
            return formatMs(n);
        case 'days':
            return formatDays(n);
        case 'ratio':
            return formatRatio(n);
        case 'score':
            return formatScore(n);
        default:
            return formatCount(n, opts);
    }
}

/** Short axis ticks: always compact, whole percentages. */
export function formatTick(value: number, unit: InsightsUnit | string): string {
    switch (unit) {
        case 'pct':
            return `${INT.format(value * 100)}%`;
        case 'usd':
            return formatUsd(value, { compact: true });
        case 'ms':
            return formatMs(value);
        case 'score':
            return ONE.format(value);
        default:
            return Math.abs(value) >= 1000 ? COMPACT.format(value) : ONE.format(value);
    }
}

/** Where a value sits between 0 and the top of its scale, for bar widths. */
export function barFraction(value: number | null | undefined, max: number, unit: InsightsUnit | string): number {
    if (value === null || value === undefined || !Number.isFinite(value) || value <= 0) return 0;
    // A rate is drawn against 100%, not against the biggest rate on the list:
    // two teams at 30% and 15% must not look like "full" and "half".
    const top = unit === 'pct' || unit === 'score' ? Math.max(1, max) : max;
    return top > 0 ? Math.min(1, value / top) : 0;
}

// ─── denominators ────────────────────────────────────────────────────────────

/** One side of a share in its own unit (the catalog's `part_unit`). Dollars
 *  read to the whole dollar ("$57 of $215"): cents add noise to a share. */
function formatPart(v: number, partUnit: InsightsUnit | string): string {
    if (partUnit === 'usd' && Math.abs(v) >= 1) return `${v < 0 ? '-' : ''}$${INT.format(Math.abs(v))}`;
    return formatValue(v, partUnit);
}

/** "212 of 640" — or null when either side is missing. With `partUnit` both
 *  sides are in that unit ("$57 of $215") rather than counts. */
export function denominatorText(
    numerator: number | null | undefined,
    denominator: number | null | undefined,
    unit: InsightsUnit | string = 'pct',
    partUnit?: InsightsUnit | string | null,
): string | null {
    if (numerator === null || numerator === undefined || denominator === null || denominator === undefined) return null;
    if (!Number.isFinite(numerator) || !Number.isFinite(denominator)) return null;
    if (partUnit) {
        const n = formatPart(numerator, partUnit);
        const d = formatPart(denominator, partUnit);
        return unit === 'pct' ? `${n} of ${d}` : `${n} across ${d}`;
    }
    // A rate is a part OF a whole ("212 of 640"). Anything else carrying a
    // numerator is an average — a total spread ACROSS a count ("$151.90 across
    // 23"), where "of" would read as if 151.9 were a share of 23.
    if (unit === 'pct') return `${formatCount(numerator)} of ${formatCount(denominator)}`;
    const total = unit === 'usd' ? formatValue(numerator, 'usd') : formatCount(numerator);
    return `${total} across ${formatCount(denominator)}`;
}

/** "212 of 640 people with Cubie access in scope". */
export function denominatorSentence(
    numerator: number | null | undefined,
    denominator: number | null | undefined,
    outOf: string | null | undefined,
    unit: InsightsUnit | string = 'pct',
    partUnit?: InsightsUnit | string | null,
): string | null {
    const base = denominatorText(numerator, denominator, unit, partUnit);
    if (!base) return null;
    return outOf ? `${base} ${outOf}` : base;
}

/** n ÷ d, or null when there is nothing to divide by. */
export const ratioOf = (n: number, d: number): number | null => (d > 0 ? n / d : null);

// ─── deltas ──────────────────────────────────────────────────────────────────

export interface Delta {
    /** value − previous. */
    abs: number;
    /** (value − previous) ÷ |previous|; null when the previous value was 0. */
    rel: number | null;
}

export type DeltaDirection = 'up' | 'down' | 'flat';
export type DeltaTone = 'good' | 'bad' | 'neutral';

const EPSILON = 1e-9;

export function computeDelta(value: number | null | undefined, prev: number | null | undefined): Delta | null {
    if (value === null || value === undefined || prev === null || prev === undefined) return null;
    if (!Number.isFinite(value) || !Number.isFinite(prev)) return null;
    const abs = value - prev;
    const rel = Math.abs(prev) < EPSILON ? null : abs / Math.abs(prev);
    return { abs, rel };
}

/** True when the change would print as zero at the chip's precision — a red
 *  "▼ −0.00" for a score that moved 0.001 is noise dressed as a signal. */
function roundsToZero(delta: Delta, unit: InsightsUnit | string | undefined): boolean {
    if (Math.abs(delta.abs) < EPSILON) return true;
    if (unit === 'pct') return Math.abs(delta.abs) * 100 < 0.05;
    if (unit === 'score') return Math.abs(delta.abs) < 0.005;
    if (unit && delta.rel !== null) return Math.abs(delta.rel) * 100 < 0.05;
    return false;
}

/** Pass the KPI's unit so a change too small to print counts as no change. */
export function deltaDirection(delta: Delta | null, unit?: InsightsUnit | string): DeltaDirection {
    if (!delta || roundsToZero(delta, unit)) return 'flat';
    return delta.abs > 0 ? 'up' : 'down';
}

/** Green when the number moved the way that is good for it, red when it moved
 *  the other way, grey when it didn't move or neither way is better. */
export function deltaTone(
    delta: Delta | null,
    good: GoodDirection | null | undefined,
    unit?: InsightsUnit | string,
): DeltaTone {
    const dir = deltaDirection(delta, unit);
    if (dir === 'flat' || !good || good === 'neutral') return 'neutral';
    return dir === good ? 'good' : 'bad';
}

export const deltaArrow = (delta: Delta | null, unit?: InsightsUnit | string): string => {
    const dir = deltaDirection(delta, unit);
    return dir === 'up' ? '▲' : dir === 'down' ? '▼' : '–';
};

const signed = (text: string, n: number): string => (n > 0 ? `+${text}` : n < 0 ? `−${text}` : text);

/**
 * The chip's text. Rates move in POINTS ("+4 pts": 29% → 33%), because "+14%"
 * of a percentage reads as the rate itself. Everything else moves in percent
 * of the previous value, or in its own unit when the previous value was zero.
 */
export function formatDelta(delta: Delta | null, unit: InsightsUnit | string): string {
    if (!delta) return '';
    if (deltaDirection(delta, unit) === 'flat') return 'No change';
    const a = Math.abs(delta.abs);
    if (unit === 'pct') {
        const pts = a * 100;
        return signed(`${pts < 10 ? ONE.format(pts) : INT.format(pts)} pts`, delta.abs);
    }
    if (unit === 'score') return signed(TWO.format(a), delta.abs);
    if (delta.rel !== null) {
        const r = Math.abs(delta.rel) * 100;
        return signed(`${r < 10 ? ONE.format(r) : INT.format(r)}%`, delta.abs);
    }
    return signed(formatValue(a, unit), delta.abs);
}

/** A sentence for a tooltip or screen reader: "Up 4 pts vs previous 30 days (was 29%)". */
export function deltaSentence(
    delta: Delta | null,
    unit: InsightsUnit | string,
    prev: number | null | undefined,
    comparison: string,
): string {
    if (!delta) return `No earlier figure to compare ${comparison.replace(/^vs /, 'with the ')}.`;
    const dir = deltaDirection(delta, unit);
    const was = `(was ${formatValue(prev, unit)})`;
    if (dir === 'flat') return `No change ${comparison} ${was}`;
    const moved = formatDelta(delta, unit).replace(/^[+−]/, '');
    return `${dir === 'up' ? 'Up' : 'Down'} ${moved} ${comparison} ${was}`;
}

// ─── time buckets ────────────────────────────────────────────────────────────

/** A chart label for a bucket start: "Sep 9", or "Wk of Sep 8". */
export function bucketLabel(iso: string, bucket: 'day' | 'week'): string {
    return bucket === 'week' ? `Wk of ${shortDate(iso)}` : shortDate(iso);
}

/** Plural helper: "1 person", "3 people". */
export const plural = (n: number, one: string, many = `${one}s`): string => `${formatCount(n)} ${n === 1 ? one : many}`;
