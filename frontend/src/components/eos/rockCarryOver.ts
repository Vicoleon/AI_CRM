// Quarter math + draft helpers for carrying an EOS Rock into a later quarter —
// the pure half of the Rocks page's "Carry over to next quarter" dialog.
//
// EOS practice: at quarter end an unfinished Rock is either dropped or carried
// into next quarter, usually re-dated and re-scoped. Carrying over CREATES a
// new rock in the target quarter; the source rock stays where it is as the
// record of what happened. These helpers pick the default target quarter and
// build the prefilled draft the user edits before saving.
//
// Dates are read off the ISO STRING, never through `new Date(iso)`: the API
// sends `Quarter.start_date` as a naive "2026-07-01T00:00:00", which a browser
// west of UTC parses as local time and one east of it can tip into the
// previous day — and "July 1" landing on June 30 puts the rock in the wrong
// quarter. It stays a leaf module (no React, no imports) so it runs under
// `node --test` and is covered by `tsc -b`.

/** A calendar quarter: `quarter` is 1–4. */
export interface YearQuarter {
    year: number;
    quarter: number;
}

/** The calendar quarter an ISO date/datetime string falls in, read from the
 *  string's own year and month; null when it doesn't start "YYYY-MM". */
export const quarterOfISO = (iso: string | null | undefined): YearQuarter | null => {
    const match = /^(\d{4})-(\d{2})/.exec((iso ?? '').trim());
    if (!match) return null;
    const month = Number(match[2]);
    if (month < 1 || month > 12) return null;
    return { year: Number(match[1]), quarter: Math.floor((month - 1) / 3) + 1 };
};

/** The calendar quarter a local Date falls in ("today" is the viewer's day). */
export const quarterOfDate = (d: Date): YearQuarter => ({
    year: d.getFullYear(),
    quarter: Math.floor(d.getMonth() / 3) + 1,
});

/** The quarter after `yq`; Q4 rolls over into Q1 of the next year. */
export const nextQuarter = ({ year, quarter }: YearQuarter): YearQuarter =>
    quarter >= 4 ? { year: year + 1, quarter: 1 } : { year, quarter: quarter + 1 };

/** Negative when `a` is earlier than `b`, 0 when equal, positive when later. */
export const compareQuarters = (a: YearQuarter, b: YearQuarter): number =>
    a.year !== b.year ? a.year - b.year : a.quarter - b.quarter;

/** "Q4 2026" — the same shape the server names the quarters it creates. */
export const quarterLabel = ({ year, quarter }: YearQuarter): string => `Q${quarter} ${year}`;

/** The quarter's first day as "YYYY-MM-DD" (Q4 2026 → "2026-10-01"). */
export const quarterStartISO = ({ year, quarter }: YearQuarter): string =>
    `${year}-${String((quarter - 1) * 3 + 1).padStart(2, '0')}-01`;

/**
 * Where a rock carries over to by default: the LATER of the quarter after the
 * rock's own and today's quarter. A Q3 rock carried in October lands in Q4 —
 * the quarter being planned — and a rock left over from two quarters back
 * lands in the current quarter too, never in one that has already ended. The
 * server applies the same rule when no quarter is sent. With the source
 * quarter unknown, the quarter after today's.
 */
export const defaultCarryTarget = (
    sourceQuarterStartISO: string | null | undefined,
    today: Date,
): YearQuarter => {
    const now = quarterOfDate(today);
    const source = quarterOfISO(sourceQuarterStartISO);
    if (!source) return nextQuarter(now);
    const afterSource = nextQuarter(source);
    return compareQuarters(afterSource, now) >= 0 ? afterSource : now;
};

/** A milestone as the carry-over draft needs it — structurally compatible
 *  with `RockMilestone`, declared here so this module keeps no imports. */
export interface CarryMilestoneSource {
    description: string;
    completed: boolean;
    due_date?: string | null;
}

export interface CarriedMilestone {
    description: string;
    completed: boolean;
    due_date: string | null;
}

/**
 * The milestones a carried rock starts with: only the unfinished ones, as
 * brand-new milestones (no `id` / `sort_order`, so the server inserts fresh
 * rows instead of re-attaching the source rock's — completed work stays with
 * the source as its record). A due date survives only when it still falls on
 * or after the target quarter's first day; one from the old quarter is
 * already past and would just read as overdue, so it is cleared for the user
 * (or the AI re-draft) to set.
 */
export const carryOverMilestones = (
    milestones: readonly CarryMilestoneSource[] | null | undefined,
    targetStartISO: string,
): CarriedMilestone[] =>
    (milestones ?? [])
        .filter(m => !m.completed)
        .map(m => {
            const due = (m.due_date ?? '').slice(0, 10);
            return {
                description: m.description,
                completed: false,
                due_date: due && due >= targetStartISO ? due : null,
            };
        });

/**
 * The same milestones with every due date before `startISO` cleared — applied
 * when the carry-over dialog's target quarter changes, so the draft keeps the
 * rule it opened with (nothing dated before the quarter it is carried into),
 * whether the dates came from the old rock or from an AI re-draft. Dates on or
 * after the start are kept; descriptions and anything else are untouched.
 */
export const clearDueDatesBefore = <T extends { due_date?: string | null }>(
    milestones: readonly T[],
    startISO: string,
): T[] =>
    milestones.map(m => {
        const due = (m.due_date ?? '').slice(0, 10);
        return due && due < startISO ? { ...m, due_date: null } : m;
    });
