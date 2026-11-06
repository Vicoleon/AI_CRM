// CSV export for Insights: every chart and table on a dashboard can be taken
// away as a spreadsheet, so a number on screen is always one click from its
// rows.
//
// RFC 4180: fields are comma-separated, a field holding a comma, a quote or a
// line break is wrapped in double quotes with inner quotes doubled, and lines
// end in CRLF. A UTF-8 byte-order mark goes first so Excel reads "Café" and
// "–" correctly.
//
// Values are exported RAW, not as the page formats them, so a spreadsheet can
// do arithmetic on them: dollars as numbers, milliseconds as milliseconds.
// Rates are the exception that people asked for — a 0.33 in a sheet reads as
// a third of a person — so `pct` columns carry 33.33 and say "(%)" in the
// header.
//
// A spreadsheet treats a cell starting with `=`, `+`, `-` or `@` as a formula.
// A text value from the server (a person's name, a department label) that
// starts that way is prefixed with an apostrophe so it stays text.
//
// Leaf module apart from `utils/download.ts` (only touched by `downloadCsv`),
// so `node --test` can load it.
import type {
    BarData,
    FunnelData,
    InsightsUnit,
    MatrixData,
    Section,
    TableData,
    TimeseriesData,
} from '../../api/insights';
import { downloadBlob } from '../../utils/download.ts';

export type CsvValue = string | number | boolean | null | undefined;

const NEEDS_QUOTES = /[",\r\n]|^\s|\s$/;
const FORMULA_START = /^[=+\-@\t\r]/;

/** One field, quoted only when it has to be. */
export function csvEscape(value: CsvValue): string {
    if (value === null || value === undefined) return '';
    if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '';
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    let s = String(value);
    if (FORMULA_START.test(s)) s = `'${s}`;
    return NEEDS_QUOTES.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** A whole document: header row first, CRLF line ends, trailing CRLF. */
export function toCsv(header: readonly CsvValue[], rows: readonly (readonly CsvValue[])[]): string {
    return [header, ...rows].map(r => r.map(csvEscape).join(',')).join('\r\n') + '\r\n';
}

const UNIT_SUFFIX: Partial<Record<InsightsUnit, string>> = {
    pct: ' (%)',
    usd: ' (USD)',
    ms: ' (ms)',
    days: ' (days)',
};

/** A column header carrying its unit, e.g. "Acted on (%)". */
export const headerWithUnit = (label: string, unit: InsightsUnit | string): string =>
    `${label}${UNIT_SUFFIX[unit as InsightsUnit] ?? ''}`;

/** A raw value made spreadsheet-friendly for its unit. */
export function exportValue(value: CsvValue, unit: InsightsUnit | string): CsvValue {
    if (unit === 'pct' && typeof value === 'number' && Number.isFinite(value)) {
        return Math.round(value * 10_000) / 100;
    }
    return value;
}

export function tableToCsv(data: TableData): string {
    const header = data.columns.map(c => headerWithUnit(c.label, c.unit));
    const rows = data.rows.map(r => data.columns.map(c => exportValue(r.cells[c.key], c.unit)));
    return toCsv(header, rows);
}

export function barToCsv(data: BarData, label = 'Item'): string {
    const header = [label, headerWithUnit('Value', data.unit), 'Numerator', 'Denominator'];
    const rows = data.items.map(i => [i.label, exportValue(i.value, data.unit), i.numerator, i.denominator]);
    return toCsv(header, rows);
}

export function timeseriesToCsv(data: TimeseriesData): string {
    const header = ['Date', ...data.series.map(s => headerWithUnit(s.label, s.unit))];
    const rows = data.x.map((x, i) => [x, ...data.series.map(s => exportValue(s.values[i] ?? null, s.unit))]);
    return toCsv(header, rows);
}

export function funnelToCsv(data: FunnelData): string {
    const first = data.steps[0]?.value ?? 0;
    const header = ['Step', 'Count', 'Share of first step (%)'];
    const rows = data.steps.map(s => [s.label, s.value, first > 0 ? Math.round((s.value / first) * 10_000) / 100 : null]);
    return toCsv(header, rows);
}

/** Long format (one row per cell), so numerator and denominator travel with
 *  every value instead of being lost in a grid. */
export function matrixToCsv(data: MatrixData, rowLabel = 'Row', colLabel = 'Column'): string {
    const header = [rowLabel, colLabel, headerWithUnit('Value', data.unit), 'Numerator', 'Denominator'];
    const rows: CsvValue[][] = [];
    data.rows.forEach((row, ri) => {
        data.cols.forEach((col, ci) => {
            const cell = data.cells[ri]?.[ci] ?? null;
            rows.push([row.label, col.label, exportValue(cell?.value ?? null, data.unit), cell?.numerator, cell?.denominator]);
        });
    });
    return toCsv(header, rows);
}

/** The CSV for any section, by kind; `null` for a kind this build can't export. */
export function sectionToCsv(section: Section): string | null {
    switch (section.kind) {
        case 'table':
            return tableToCsv(section.data);
        case 'bar':
            return barToCsv(section.data, section.drill === 'person' ? 'Person' : section.drill === 'department' ? 'Department' : 'Item');
        case 'timeseries':
            return timeseriesToCsv(section.data);
        case 'funnel':
            return funnelToCsv(section.data);
        case 'matrix':
            return matrixToCsv(section.data, section.drill === 'department' ? 'Department' : 'Row', 'Column');
        default:
            return null;
    }
}

/** "insights-priorities-by-department-2026-09-09-to-2026-10-08.csv". */
export function csvFilename(parts: readonly (string | null | undefined)[]): string {
    const slug = parts
        .filter((p): p is string => !!p)
        .join('-')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '');
    return `${slug || 'insights'}.csv`;
}

/** Hand the browser a CSV file. */
export function downloadCsv(filename: string, csv: string): void {
    downloadBlob(new Blob(['﻿', csv], { type: 'text/csv;charset=utf-8' }), filename);
}
