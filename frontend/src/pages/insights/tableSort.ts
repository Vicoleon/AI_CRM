// Sorting, searching and paging for Insights tables. Pure, so the rules the
// DataTable follows are pinned by tests rather than by eye:
//
//   * Numbers sort as numbers ("10" after "9"), text in the reader's
//     alphabetical order, dates as dates.
//   * A missing value is always LAST, whichever way the column is sorted —
//     "no data" is never the top performer or the bottom one.
//   * The sort is stable, so equal values keep the server's order.
//   * A first click on a number column sorts high-to-low (the question is
//     usually "who has the most"); on a text column, A to Z.
//
// A leaf module (type-only imports), so `node --test` loads it without a bundler.
import type { InsightsUnit, TableCellValue, TableColumn } from '../../api/insights';

export type SortDir = 'asc' | 'desc';

export interface SortState {
    key: string;
    dir: SortDir;
}

/** Rows shown before "Show all". */
export const PAGE_SIZE = 25;

const TEXT_UNITS: readonly string[] = ['text'];

const isMissing = (v: TableCellValue | undefined): boolean => v === null || v === undefined || v === '';

const COLLATOR = new Intl.Collator('en-US', { sensitivity: 'base', numeric: true });

/** Compare two present values for `unit`, ascending. */
export function compareValues(a: TableCellValue, b: TableCellValue, unit: InsightsUnit | string): number {
    if (unit === 'date' || unit === 'datetime') {
        // ISO strings order lexically.
        const sa = String(a);
        const sb = String(b);
        return sa < sb ? -1 : sa > sb ? 1 : 0;
    }
    if (TEXT_UNITS.includes(unit)) return COLLATOR.compare(String(a), String(b));
    const na = typeof a === 'number' ? a : Number(a);
    const nb = typeof b === 'number' ? b : Number(b);
    if (Number.isFinite(na) && Number.isFinite(nb)) return na - nb;
    return COLLATOR.compare(String(a), String(b));
}

/** Stable sort; missing values last in both directions. */
export function sortRows<T extends { cells: Record<string, TableCellValue> }>(
    rows: readonly T[],
    sort: SortState | null,
    unit: InsightsUnit | string,
): T[] {
    if (!sort) return [...rows];
    const sign = sort.dir === 'asc' ? 1 : -1;
    return rows
        .map((row, index) => ({ row, index }))
        .sort((x, y) => {
            const a = x.row.cells[sort.key];
            const b = y.row.cells[sort.key];
            const ma = isMissing(a);
            const mb = isMissing(b);
            if (ma && mb) return x.index - y.index;
            if (ma) return 1;
            if (mb) return -1;
            const c = compareValues(a, b, unit) * sign;
            return c !== 0 ? c : x.index - y.index;
        })
        .map(({ row }) => row);
}

/** What a header click does: the same column flips, a new one starts at its natural direction. */
export function nextSort(current: SortState | null, key: string, unit: InsightsUnit | string): SortState {
    if (current && current.key === key) return { key, dir: current.dir === 'asc' ? 'desc' : 'asc' };
    return { key, dir: TEXT_UNITS.includes(unit) ? 'asc' : 'desc' };
}

export function ariaSort(sort: SortState | null, key: string): 'ascending' | 'descending' | 'none' {
    if (!sort || sort.key !== key) return 'none';
    return sort.dir === 'asc' ? 'ascending' : 'descending';
}

/** Case-insensitive match on any text cell, or on the row key (an email). */
export function filterRows<T extends { key: string; cells: Record<string, TableCellValue> }>(
    rows: readonly T[],
    query: string,
    columns: readonly TableColumn[],
): T[] {
    const q = query.trim().toLowerCase();
    if (!q) return [...rows];
    const textKeys = columns.filter(c => c.unit === 'text').map(c => c.key);
    return rows.filter(
        r =>
            r.key.toLowerCase().includes(q) ||
            textKeys.some(k => {
                const v = r.cells[k];
                return typeof v === 'string' && v.toLowerCase().includes(q);
            }),
    );
}

/** The column a table opens sorted by: the server's choice when it names a real column. */
export function initialSort(columns: readonly TableColumn[], defaultSort: SortState | null | undefined): SortState | null {
    if (defaultSort && columns.some(c => c.key === defaultSort.key)) return defaultSort;
    return null;
}
