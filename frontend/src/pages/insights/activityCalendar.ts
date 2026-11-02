// The person profile's 90-day activity calendar, laid out GitHub-style: one
// column per week (Monday on top, per docs/insights/README.md §3), one square
// per day in the company's business time zone.
//
// Dates are the server's business-day calendar days and are only ever moved
// with UTC arithmetic on the `YYYY-MM-DD` string — never through the viewer's
// own clock — so a square never slides to the neighbouring day for someone in
// another time zone.
//
// Leaf apart from another leaf module, imported with its `.ts` extension.
import type { CalendarDay } from '../../api/insights';
import { addDays, isIsoDate } from './insightsFilters.ts';

export const CALENDAR_SPAN_DAYS = 90;

export type CalendarLevel = 0 | 1 | 2 | 3 | 4;

export interface CalendarCell {
    date: string;
    count: number;
    level: CalendarLevel;
}

export interface CalendarLayout {
    /** Columns of seven (Mon…Sun); `null` pads days outside the 90. */
    weeks: (CalendarCell | null)[][];
    /** Month labels and the week column they start over. */
    months: { label: string; week: number }[];
    max: number;
    total: number;
    activeDays: number;
    days: number;
}

const MONTH = new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'UTC' });

const utc = (iso: string): Date => {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, d));
};

/** 0 = Monday … 6 = Sunday. */
export const mondayIndex = (iso: string): number => (utc(iso).getUTCDay() + 6) % 7;

/** Shade by share of the busiest day: 1 is a quiet day, 4 the busiest quarter. */
export function calendarLevel(count: number, max: number): CalendarLevel {
    if (count <= 0 || max <= 0) return 0;
    return Math.min(4, Math.max(1, Math.ceil((count / max) * 4))) as CalendarLevel;
}

export function buildCalendar(days: readonly CalendarDay[], end: string, span = CALENDAR_SPAN_DAYS): CalendarLayout {
    if (!isIsoDate(end)) return { weeks: [], months: [], max: 0, total: 0, activeDays: 0, days: 0 };
    const start = addDays(end, -(span - 1));
    const counts = new Map<string, number>();
    for (const d of days) {
        if (isIsoDate(d.date) && d.date >= start && d.date <= end) {
            counts.set(d.date, (counts.get(d.date) ?? 0) + Math.max(0, d.count || 0));
        }
    }
    const max = Math.max(0, ...counts.values());
    let total = 0;
    let activeDays = 0;
    for (const c of counts.values()) {
        total += c;
        if (c > 0) activeDays += 1;
    }

    const weeks: (CalendarCell | null)[][] = [];
    const months: { label: string; week: number }[] = [];
    let cursor = addDays(start, -mondayIndex(start));
    let lastMonth = '';
    while (cursor <= end) {
        const week: (CalendarCell | null)[] = [];
        for (let i = 0; i < 7; i++) {
            const date = addDays(cursor, i);
            if (date < start || date > end) {
                week.push(null);
                continue;
            }
            const count = counts.get(date) ?? 0;
            week.push({ date, count, level: calendarLevel(count, max) });
            const month = MONTH.format(utc(date));
            if (month !== lastMonth) {
                // Label the column where the month's first in-range day falls.
                months.push({ label: month, week: weeks.length });
                lastMonth = month;
            }
        }
        weeks.push(week);
        cursor = addDays(cursor, 7);
    }
    return { weeks, months, max, total, activeDays, days: span };
}
