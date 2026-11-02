import React, { useId, useState } from 'react';
import { BarChart3, Download, Inbox, Table2 } from 'lucide-react';
import type { SectionWidth } from '../../api/insights';
import { downloadCsv } from '../../pages/insights/csv';

// Full literal class strings so Tailwind keeps every span. Twelve columns,
// stacked to one on a phone.
const WIDTH: Record<SectionWidth, string> = {
    full: 'col-span-12',
    half: 'col-span-12 lg:col-span-6',
    third: 'col-span-12 md:col-span-6 xl:col-span-4',
};

const ICON_BUTTON =
    'inline-flex h-8 items-center gap-1.5 rounded-md border border-gray-200 bg-white px-2.5 text-xs font-medium text-gray-600 transition-colors hover:border-gray-300 hover:bg-gray-50 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30 disabled:cursor-not-allowed disabled:opacity-50';

/**
 * The frame every Insights section sits in: title and subtitle, a "View as
 * table" switch (the accessible alternative to every chart), a CSV download,
 * and a calm empty state that says why there is nothing to show.
 */
export const ChartCard: React.FC<{
    title: string;
    subtitle?: string | null;
    width?: SectionWidth;
    /** The chart. */
    children: React.ReactNode;
    /** The same numbers as a table; omit when the section already is one. */
    table?: React.ReactNode;
    /** Builds the CSV on click (so a hidden card costs nothing). */
    csv?: (() => string | null) | null;
    csvFilename?: string;
    isEmpty?: boolean;
    emptyHint?: string | null;
    /** Extra controls in the header (a legend, a scale key). */
    aside?: React.ReactNode;
}> = ({ title, subtitle, width = 'full', children, table, csv, csvFilename = 'insights.csv', isEmpty = false, emptyHint, aside }) => {
    const [asTable, setAsTable] = useState(false);
    const headingId = useId();

    const onDownload = () => {
        const text = csv?.();
        if (text) downloadCsv(csvFilename, text);
    };

    return (
        <section
            aria-labelledby={headingId}
            className={`${WIDTH[width] ?? WIDTH.full} flex min-w-0 flex-col rounded-lg border border-gray-200 bg-white shadow-sm`}
        >
            <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-4 pt-4 sm:px-5">
                <div className="min-w-0">
                    <h2 id={headingId} className="text-[15px] font-semibold text-gray-900">
                        {title}
                    </h2>
                    {subtitle && <p className="mt-0.5 text-xs leading-relaxed text-gray-500">{subtitle}</p>}
                </div>
                {!isEmpty && (table || csv) && (
                    <div className="flex flex-shrink-0 items-center gap-1.5">
                        {table && (
                            <button
                                type="button"
                                onClick={() => setAsTable(v => !v)}
                                aria-pressed={asTable}
                                className={ICON_BUTTON}
                                title={asTable ? 'Show the chart' : 'View as table'}
                            >
                                {asTable ? <BarChart3 size={14} aria-hidden="true" /> : <Table2 size={14} aria-hidden="true" />}
                                <span>{asTable ? 'Chart' : 'Table'}</span>
                                <span className="sr-only"> view of {title}</span>
                            </button>
                        )}
                        {csv && (
                            <button
                                type="button"
                                onClick={onDownload}
                                className={ICON_BUTTON}
                                aria-label={`Download ${title} as CSV`}
                                title="Download as CSV"
                            >
                                <Download size={14} aria-hidden="true" />
                                <span className="hidden sm:inline">CSV</span>
                            </button>
                        )}
                    </div>
                )}
            </header>
            {aside && !isEmpty && !asTable && <div className="px-4 pt-3 sm:px-5">{aside}</div>}
            <div className="min-w-0 flex-1 px-4 pb-4 pt-3 sm:px-5">
                {isEmpty ? (
                    <div className="flex h-full min-h-[9rem] flex-col items-center justify-center gap-2 rounded-md bg-gray-50/70 px-4 py-6 text-center">
                        <Inbox size={20} className="text-gray-300" aria-hidden="true" />
                        <p className="max-w-sm text-sm text-gray-500">{emptyHint || 'Nothing to show for this period yet.'}</p>
                    </div>
                ) : asTable && table ? (
                    table
                ) : (
                    children
                )}
            </div>
        </section>
    );
};

/**
 * The plain table behind a chart's "View as table". Already-formatted
 * strings; numbers right-aligned in tabular figures. Scrolls sideways in its
 * own box on a phone instead of widening the page.
 */
export const AltTable: React.FC<{
    caption: string;
    columns: { key: string; label: string; numeric?: boolean }[];
    rows: { key: string; cells: Record<string, React.ReactNode> }[];
}> = ({ caption, columns, rows }) => (
    <div className="relative max-h-[28rem] overflow-auto rounded-md border border-gray-100">
        <table className="min-w-full text-sm">
            <caption className="sr-only">{caption}</caption>
            <thead className="sticky top-0 z-[1] bg-gray-50">
                <tr>
                    {columns.map(c => (
                        <th
                            key={c.key}
                            scope="col"
                            className={`whitespace-nowrap px-3 py-2 text-xs font-semibold text-gray-600 ${c.numeric ? 'text-right' : 'text-left'}`}
                        >
                            {c.label}
                        </th>
                    ))}
                </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
                {rows.map(r => (
                    <tr key={r.key} className="hover:bg-gray-50/60">
                        {columns.map((c, i) =>
                            i === 0 ? (
                                <th key={c.key} scope="row" className="whitespace-nowrap px-3 py-1.5 text-left font-normal text-gray-800">
                                    {r.cells[c.key]}
                                </th>
                            ) : (
                                <td
                                    key={c.key}
                                    className={`whitespace-nowrap px-3 py-1.5 text-gray-700 ${c.numeric ? 'text-right tabular-nums' : ''}`}
                                >
                                    {r.cells[c.key]}
                                </td>
                            ),
                        )}
                    </tr>
                ))}
            </tbody>
        </table>
    </div>
);
