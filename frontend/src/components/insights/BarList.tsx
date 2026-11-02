import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { ChevronRight } from 'lucide-react';
import type { BarData, SectionDrill } from '../../api/insights';
import { barFraction, denominatorText, formatValue } from '../../pages/insights/insightsFormat';
import { SERIES_COLORS } from './palette';

const SHOW_FIRST = 10;

const ROW =
    'group grid w-full grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)_auto] items-center gap-3 rounded-md px-2 py-1.5 text-left sm:grid-cols-[minmax(0,11rem)_minmax(0,1fr)_auto]';
const ROW_ACTIVE = 'transition-colors hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30';

/**
 * A ranked list of horizontal bars, in the server's order. Each row prints
 * its value and, for a rate, what it is out of ("12 of 15").
 *
 * Rows drill: a department row filters the whole dashboard to it, a person
 * row opens their profile (only when the viewer has the person view).
 * Without a drill the rows are plain text.
 */
export const BarList: React.FC<{
    data: BarData;
    title: string;
    drill?: SectionDrill;
    onDrillDepartment?: (slug: string) => void;
    /** A person's profile path, or null when the viewer can't open one. */
    personHref?: (email: string) => string | null;
    /** Departments currently in the filter, to mark their rows. */
    activeKeys?: readonly string[];
}> = ({ data, title, drill, onDrillDepartment, personHref, activeKeys = [] }) => {
    const [showAll, setShowAll] = useState(false);
    const max = Math.max(0, ...data.items.map(i => (typeof i.value === 'number' ? i.value : 0)));
    const items = showAll ? data.items : data.items.slice(0, SHOW_FIRST);

    return (
        <div>
            <ul className="space-y-0.5" aria-label={title}>
                {items.map(item => {
                    const fraction = barFraction(item.value, max, data.unit);
                    const withheld = (item.value === null || item.value === undefined) && !!item.note;
                    const of = withheld ? item.note ?? null : denominatorText(item.numerator, item.denominator);
                    const valueText = withheld ? 'Hidden' : formatValue(item.value, data.unit);
                    const active = activeKeys.includes(item.key);
                    const body = (
                        <>
                            <span className={`truncate text-sm ${active ? 'font-semibold text-brand' : 'text-gray-700'}`} title={item.label}>
                                {item.label}
                            </span>
                            <span className="h-2.5 overflow-hidden rounded-r bg-gray-100" aria-hidden="true">
                                <span
                                    className="block h-full rounded-r transition-[width] duration-300 motion-reduce:transition-none"
                                    style={{ width: `${(fraction * 100).toFixed(1)}%`, backgroundColor: SERIES_COLORS[0] }}
                                />
                            </span>
                            <span className="flex items-center gap-1 text-right">
                                <span className="flex flex-col items-end leading-tight">
                                    <span className="text-sm font-medium text-gray-900 tabular-nums">{valueText}</span>
                                    {of && <span className="text-[11px] text-gray-500 tabular-nums">{of}</span>}
                                </span>
                                {drill && (
                                    <ChevronRight
                                        size={14}
                                        aria-hidden="true"
                                        className="text-gray-300 transition-colors group-hover:text-brand"
                                    />
                                )}
                            </span>
                        </>
                    );
                    const sentence = `${item.label}: ${valueText}${of ? ` (${of})` : ''}`;

                    if (drill === 'department' && onDrillDepartment) {
                        return (
                            <li key={item.key}>
                                <button
                                    type="button"
                                    onClick={() => onDrillDepartment(item.key)}
                                    className={`${ROW} ${ROW_ACTIVE} ${active ? 'bg-brand-tint/50' : ''}`}
                                    title={`${sentence}. Filter the dashboard to ${item.label}.`}
                                    aria-label={`${sentence}. Filter to ${item.label}`}
                                >
                                    {body}
                                </button>
                            </li>
                        );
                    }
                    const href = drill === 'person' ? personHref?.(item.key) ?? null : null;
                    if (href) {
                        return (
                            <li key={item.key}>
                                <Link to={href} className={`${ROW} ${ROW_ACTIVE}`} title={`${sentence}. Open their profile.`} aria-label={`${sentence}. Open profile`}>
                                    {body}
                                </Link>
                            </li>
                        );
                    }
                    return (
                        <li key={item.key} className={ROW} title={sentence}>
                            {body}
                        </li>
                    );
                })}
            </ul>
            {data.items.length > SHOW_FIRST && (
                <button
                    type="button"
                    onClick={() => setShowAll(v => !v)}
                    className="mt-2 rounded px-2 text-xs font-medium text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                >
                    {showAll ? 'Show fewer' : `Show all ${data.items.length}`}
                </button>
            )}
        </div>
    );
};
