import React, { useMemo, useState } from 'react';
import type { CalendarDay } from '../../api/insights';
import { buildCalendar } from '../../pages/insights/activityCalendar';
import { formatCount, plural } from '../../pages/insights/insightsFormat';
import { longDate } from '../../pages/insights/insightsFilters';
import { CALENDAR_SHADES } from './palette';

const CELL = 14;
const GAP = 3;
const STEP = CELL + GAP;
const LEFT = 26;
const TOP = 16;
const WEEKDAYS = ['Mon', '', 'Wed', '', 'Fri', '', ''];

/**
 * Ninety days of activity, one square per business-time-zone day, a column per week
 * (Monday on top). Darker means more recorded activity that day; hover or
 * focus a square for the exact count. The SVG is one image with a sentence
 * summary; the squares are focusable for keyboard readers.
 */
export const ActivityCalendar: React.FC<{ days: readonly CalendarDay[]; end: string; name: string }> = ({ days, end, name }) => {
    const layout = useMemo(() => buildCalendar(days, end), [days, end]);
    const [hover, setHover] = useState<{ date: string; count: number; x: number; y: number } | null>(null);

    if (layout.weeks.length === 0) return <p className="text-sm text-gray-500">No activity calendar for this period.</p>;

    const width = LEFT + layout.weeks.length * STEP;
    const height = TOP + 7 * STEP;
    const summary = `${name}: active on ${plural(layout.activeDays, 'day')} of the last ${layout.days}, ${formatCount(layout.total)} recorded actions in all.`;

    return (
        <div className="space-y-3">
            <div className="relative overflow-x-auto">
                <svg
                    role="img"
                    aria-label={summary}
                    width={width}
                    height={height}
                    viewBox={`0 0 ${width} ${height}`}
                    className="block"
                    onMouseLeave={() => setHover(null)}
                >
                    {layout.months.map(m => (
                        <text key={`${m.label}-${m.week}`} x={LEFT + m.week * STEP} y={10} className="fill-gray-400" fontSize={10}>
                            {m.label}
                        </text>
                    ))}
                    {WEEKDAYS.map((d, i) =>
                        d ? (
                            <text key={d} x={0} y={TOP + i * STEP + CELL - 2} className="fill-gray-400" fontSize={9}>
                                {d}
                            </text>
                        ) : null,
                    )}
                    {layout.weeks.map((week, wi) =>
                        week.map((cell, di) => {
                            if (!cell) return null;
                            const x = LEFT + wi * STEP;
                            const y = TOP + di * STEP;
                            const label = `${longDate(cell.date)}: ${cell.count === 0 ? 'no activity' : plural(cell.count, 'action')}`;
                            return (
                                <rect
                                    key={cell.date}
                                    x={x}
                                    y={y}
                                    width={CELL}
                                    height={CELL}
                                    rx={2.5}
                                    fill={CALENDAR_SHADES[cell.level]}
                                    stroke={hover?.date === cell.date ? '#374151' : 'none'}
                                    strokeWidth={1}
                                    tabIndex={0}
                                    aria-label={label}
                                    className="cursor-default outline-none focus:stroke-gray-700"
                                    onMouseEnter={() => setHover({ date: cell.date, count: cell.count, x, y })}
                                    onFocus={() => setHover({ date: cell.date, count: cell.count, x, y })}
                                    onBlur={() => setHover(null)}
                                >
                                    <title>{label}</title>
                                </rect>
                            );
                        }),
                    )}
                </svg>
                {hover && (
                    <div
                        className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded bg-gray-900 px-2 py-1 text-[11px] text-white shadow"
                        style={{ left: hover.x + CELL / 2, top: hover.y - 4 }}
                        aria-hidden="true"
                    >
                        <span className="font-semibold tabular-nums">{hover.count === 0 ? 'No activity' : plural(hover.count, 'action')}</span>
                        <span className="text-gray-300"> · {longDate(hover.date)}</span>
                    </div>
                )}
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-gray-500">
                <span className="tabular-nums">
                    Active on <span className="font-medium text-gray-700">{layout.activeDays}</span> of the last {layout.days} days
                </span>
                <span className="flex items-center gap-1" aria-hidden="true">
                    Less
                    {CALENDAR_SHADES.map(c => (
                        <span key={c} className="inline-block h-2.5 w-2.5 rounded-[3px]" style={{ backgroundColor: c }} />
                    ))}
                    More
                </span>
            </div>
        </div>
    );
};
