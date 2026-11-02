import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowDown, ArrowUp, ArrowUpDown, Search } from 'lucide-react';
import type { TableData } from '../../api/insights';
import { formatValue } from '../../pages/insights/insightsFormat';
import { PAGE_SIZE, ariaSort, filterRows, initialSort, nextSort, sortRows } from '../../pages/insights/tableSort';
import type { SortState } from '../../pages/insights/tableSort';

/** Search appears once a table is long enough to need it. */
const SEARCH_FROM = 8;

const NUMERIC = new Set(['count', 'pct', 'usd', 'ms', 'days', 'ratio', 'score']);

/**
 * A sortable, searchable table with a sticky header. Every cell is formatted
 * by its column's unit; a missing value reads "—" and always sorts last.
 * With `row_link: "person"` the first column opens the person's profile (only
 * when the viewer has the person view). Shows 25 rows, then "Show all".
 */
export const DataTable: React.FC<{
    data: TableData;
    title: string;
    personHref?: (email: string) => string | null;
}> = ({ data, title, personHref }) => {
    const [sort, setSort] = useState<SortState | null>(() => initialSort(data.columns, data.default_sort ?? null));
    const [query, setQuery] = useState('');
    const [showAll, setShowAll] = useState(false);

    const sortUnit = data.columns.find(c => c.key === sort?.key)?.unit ?? 'text';
    const rows = useMemo(
        () => sortRows(filterRows(data.rows, query, data.columns), sort, sortUnit),
        [data.rows, data.columns, query, sort, sortUnit],
    );
    const visible = showAll ? rows : rows.slice(0, PAGE_SIZE);
    const linkColumn = data.row_link === 'person' ? data.columns[0]?.key : null;

    return (
        <div className="space-y-3">
            {data.rows.length >= SEARCH_FROM && (
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <label className="relative block w-full sm:w-64">
                        <span className="sr-only">Search {title}</span>
                        <Search size={14} aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
                        <input
                            type="search"
                            value={query}
                            onChange={e => {
                                setQuery(e.target.value);
                                setShowAll(false);
                            }}
                            placeholder="Search…"
                            className="w-full rounded-md border border-gray-300 bg-white py-1.5 pl-8 pr-2 text-sm text-gray-800 placeholder:text-gray-400 focus:border-brand/50 focus:outline-none focus:ring-2 focus:ring-brand/30"
                        />
                    </label>
                    <p className="text-xs text-gray-500 tabular-nums" aria-live="polite">
                        {query ? `${rows.length} of ${data.rows.length} rows match` : `${data.rows.length} rows`}
                    </p>
                </div>
            )}
            <div className="relative max-h-[36rem] overflow-auto rounded-md border border-gray-100">
                <table className="min-w-full text-sm">
                    <caption className="sr-only">{title}. Column headers sort the table.</caption>
                    <thead className="sticky top-0 z-[1] bg-gray-50 shadow-[0_1px_0_0_rgb(243,244,246)]">
                        <tr>
                            {data.columns.map(c => {
                                const numeric = NUMERIC.has(c.unit);
                                const state = ariaSort(sort, c.key);
                                const Icon = state === 'ascending' ? ArrowUp : state === 'descending' ? ArrowDown : ArrowUpDown;
                                return (
                                    <th
                                        key={c.key}
                                        scope="col"
                                        aria-sort={state}
                                        className={`whitespace-nowrap px-3 py-2 text-xs font-semibold text-gray-600 ${numeric ? 'text-right' : 'text-left'}`}
                                    >
                                        <button
                                            type="button"
                                            onClick={() => setSort(prev => nextSort(prev, c.key, c.unit))}
                                            className={`inline-flex items-center gap-1 rounded px-1 py-0.5 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30 ${
                                                numeric ? 'flex-row-reverse' : ''
                                            } ${state !== 'none' ? 'text-gray-900' : ''}`}
                                        >
                                            {c.label}
                                            <Icon size={12} aria-hidden="true" className={state === 'none' ? 'text-gray-300' : 'text-brand'} />
                                        </button>
                                    </th>
                                );
                            })}
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                        {visible.map(r => (
                            <tr key={r.key} className="transition-colors hover:bg-gray-50/70">
                                {data.columns.map((c, i) => {
                                    const numeric = NUMERIC.has(c.unit);
                                    const text = formatValue(r.cells[c.key], c.unit);
                                    const href = c.key === linkColumn ? personHref?.(r.key) ?? null : null;
                                    const content = href ? (
                                        <Link
                                            to={href}
                                            className="font-medium text-gray-900 hover:text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30 rounded"
                                            title={`Open ${text}'s profile`}
                                        >
                                            {text}
                                        </Link>
                                    ) : (
                                        text
                                    );
                                    return i === 0 ? (
                                        <th key={c.key} scope="row" className="whitespace-nowrap px-3 py-2 text-left font-normal text-gray-800">
                                            {content}
                                        </th>
                                    ) : (
                                        <td
                                            key={c.key}
                                            className={`whitespace-nowrap px-3 py-2 ${numeric ? 'text-right tabular-nums text-gray-800' : 'text-gray-600'}`}
                                        >
                                            {content}
                                        </td>
                                    );
                                })}
                            </tr>
                        ))}
                        {visible.length === 0 && (
                            <tr>
                                <td colSpan={data.columns.length} className="px-3 py-6 text-center text-sm text-gray-500">
                                    No rows match “{query}”.
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
            {rows.length > PAGE_SIZE && (
                <button
                    type="button"
                    onClick={() => setShowAll(v => !v)}
                    className="rounded px-1 text-xs font-medium text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                >
                    {showAll ? `Show the first ${PAGE_SIZE}` : `Show all ${rows.length} rows`}
                </button>
            )}
        </div>
    );
};
