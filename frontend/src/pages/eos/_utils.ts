import { useMemo, useState } from 'react';
// Shared types + pure utility functions for the EOS pages.
// Kept in a .ts file (not .tsx) so the JSX components in _shared.tsx
// can satisfy the `react-refresh/only-export-components` lint rule.

export interface PersonLite {
    id: string;
    first_name: string;
    last_name: string;
    email: string;
}

/** Format a Person as "First Last" with email fallback, then a literal fallback. */
export const formatPerson = (p: PersonLite | undefined, fallback = 'Unassigned'): string => {
    if (!p) return fallback;
    const name = `${p.first_name || ''} ${p.last_name || ''}`.trim();
    return name || p.email || fallback;
};

// Team scoping. A "team" is a department or the Exec/Leadership team.
// Sourced from GET /api/eos/me/meeting-scopes, whose `value` is either a raw
// department name or the `__exec__` sentinel. The standalone Scorecard / Issues
// / To-Do create forms tag new items with a team so they show up in that
// team's L10.
/**
 * Cap for a To-Do's one text field.
 *
 * A To-Do is one small task — "Send the pilot deck to Acme" — and the list
 * renders it on a single line. Without a cap, whole paragraphs landed in it
 * (issue bodies, Slack rambles, meeting-prep ideas) and got silently clipped
 * with no detail view to recover the rest from. 120 matches the Issue title
 * cap and the backend's `derive_issue_title` limit, so the two entities agree
 * on what "one line" means. Anything longer belongs in an Issue, which has a
 * body and a detail modal.
 */
export const TODO_MAX_LEN = 120;

// ─── Scorecard week axis ────────────────────────────────────────────────────
//
// A scorecard week IS its start Sunday. Every surface — the page grid, the
// in-meeting grid, the xlsx importer, the backend `/grid` axis — has to agree
// on that or entries exist in the DB and render as "—" forever, which is the
// single most common scorecard bug (see `scorecard_xlsx.snap_to_sunday`).
// These live here so the page and the L10 panel can't drift apart.

/**
 * The past `n` weeks (Sundays), newest → oldest — current week first. Grids
 * render this left-to-right, so the most recent week sits on the left and
 * older weeks trail off to the right, matching the ClickUp Metrics convention.
 */
export const getPastWeeks = (n = 4): Date[] => {
    const out: Date[] = [];
    for (let i = 0; i < Math.max(n, 1); i++) {
        const d = new Date();
        d.setDate(d.getDate() - d.getDay() - 7 * i);
        d.setHours(0, 0, 0, 0);
        out.push(d);
    }
    return out;
};

/** The Sunday that starts the current scorecard week. */
export const currentWeekStart = (): Date => getPastWeeks(1)[0];

/**
 * A week's key: its start *calendar date* (YYYY-MM-DD), never an exact
 * instant. The ISO date prefix is the same string the entry was saved with
 * (`weekDate.toISOString()`) and echoed back in `week_start_date`, so
 * column↔entry matching is immune to timezone offset. A `getTime()` key
 * shifted by the local UTC offset (the backend returns a naive timestamp that
 * JS re-parses as local), so entries matched no column for any non-UTC user
 * and every value rendered as "—".
 */
export const weekKey = (d: Date): string => d.toISOString().slice(0, 10);

/** Mean of the non-null values, or null when there are none. */
export const avgOf = (xs: (number | null)[]): number | null => {
    const nums = xs.filter((x): x is number => x !== null);
    if (!nums.length) return null;
    return nums.reduce((a, b) => a + b, 0) / nums.length;
};

export const EXEC_SCOPE = '__exec__';

/** User-facing name of the Exec scope. Every picker, group heading and team
 * chip that names this scope must use it, so the app never shows two names
 * for the same thing. The server sends the same string for the scopes it
 * supplies (backend/routers/eos/me.py `EXEC_SCOPE_LABEL`, mirrored for Slack
 * in backend/routers/slack_commands.py) — this constant is for the labels the
 * frontend renders on its own. Note this names the *scope*; the short "Exec"
 * pill on an individual row and copy about the exec team as a group of
 * people are deliberately left alone. */
export const EXEC_SCOPE_LABEL = 'Executive View';

export interface TeamScope {
    kind: 'exec' | 'department';
    label: string;
    value: string; // department name, or EXEC_SCOPE
    /**
     * False for a team the caller only reaches by sitting on its L10 calendar
     * roster (see `_authz.roster_dept_names`). Such a team is a valid target
     * for FILING — the Issues / To-Dos / Rocks / Scorecard create forms should
     * offer it, which is the whole point — but not for SCHEDULING, so the
     * Meetings create/edit modal filters these out. Optional so a cached
     * response without the field keeps behaving as before.
     */
    can_schedule?: boolean;
    /**
     * True on the caller's OWN department — the team they mean when they say
     * "my Issues page". The list views open on it instead of "All my teams";
     * see `useTeamFilter`. At most one scope carries it, and never the Exec
     * one. Optional so a cached response without the field still works.
     */
    is_primary?: boolean;
}

/** Map a meeting-scope value to the team fields the create endpoints expect. */
export const scopeToTeam = (
    value: string | null | undefined,
): { department_name: string | null; is_exec: boolean } => {
    if (value === EXEC_SCOPE) return { department_name: null, is_exec: true };
    return { department_name: value || null, is_exec: false };
};

/** Canonical department slug — mirrors the backend's `slugify_department`
 * (lowercase; runs of non-alphanumerics collapse to one underscore; leading /
 * trailing underscores trimmed).
 *
 * A department's identity is this slug, NOT the literal label. The live
 * `departments` table can carry case-variant rows — `sales`/`Sales`,
 * `engineering`/`Engineering` (a lowercase row written by a local test run,
 * say) — and rows tagged with either spelling are real, so any comparison or
 * grouping on the raw string splits one team in two. */
export const deptSlug = (label: string | null | undefined): string =>
    (label ?? '')
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '_')
        .replace(/^_+|_+$/g, '');

/** Whether to offer the Restart button on a live L10. A mirror of the server,
 * which decides: the person must be on the `l10_meeting_restart` feature
 * (Admin → Feature Access; admins only by default) and able to run meetings,
 * and the Exec team's L10 is never restarted. Judged on the person, never on
 * the meeting's team. This only hides a button that would 403. */
export const canRestartMeeting = (
    meeting: { is_exec_meeting?: boolean | null; department_name?: string | null },
    access: { mayRunMeetings: boolean; restartFeature: boolean },
): boolean => access.restartFeature && access.mayRunMeetings && !meeting.is_exec_meeting;

/** Between two case-variant spellings of the same department, the one to show.
 * Prefers the spelling carrying capitals ("Sales" over "sales") — the
 * human-canonical label, and on the live table the variant most users sit on.
 * Mirrors `_canonical_departments` in backend/routers/eos/me.py so a group
 * heading and the team dropdown never disagree about one team's name. */
export const preferDeptLabel = (a: string, b: string): string => {
    const aLower = a === a.toLowerCase();
    const bLower = b === b.toLowerCase();
    if (aLower !== bLower) return aLower ? b : a;
    return a <= b ? a : b;
};

/** Does a team-scoped row match the selected team filter?
 * 'all' matches everything; EXEC_SCOPE matches Exec-team rows only; a
 * department matches non-exec rows with a slug-equal department
 * (a department's identity is its slug — "Sales" ≡ "sales"). */
export const matchesTeamFilter = (
    filter: string,
    row: { department_name?: string | null; is_exec?: boolean },
): boolean => {
    if (filter === 'all') return true;
    if (filter === EXEC_SCOPE) return !!row.is_exec;
    return !row.is_exec && deptSlug(row.department_name) === deptSlug(filter);
};

// --- Leadership Scorecard format -----------------------------------------
// A measurable carries a `value_type` (how to render a weekly number) and a
// `comparator` + `target_value` (the goal direction it's measured against, for
// green/red colouring). Percent values are stored as FRACTIONS (0.9495 = 94.95%)
// to match the source spreadsheet, so both the value and the target compare
// directly.
//
// STORAGE stays fractional, but nobody TYPES a fraction: percent inputs are
// edited in percent points (see `toEditableScorecardValue` /
// `fromEditableScorecardValue` below), so "50" means 50%.

export type ScorecardValueType = 'currency' | 'percent' | 'number';

export const VALUE_TYPES: { value: ScorecardValueType; label: string }[] = [
    { value: 'number', label: 'Number (8, 85)' },
    { value: 'currency', label: 'Currency ($34,300)' },
    { value: 'percent', label: 'Percent (94.95%)' },
];

export const COMPARATORS = ['>', '>=', '<', '<=', '='] as const;
export type Comparator = (typeof COMPARATORS)[number];

/** Parse a stored scorecard value/target into a number, tolerating the
 * thousands separators / $ / % a human might have typed. Returns null for
 * empty or non-numeric strings (e.g. "TBD") so they're skipped, not zeroed. */
export const parseNumeric = (raw: string | number | null | undefined): number | null => {
    if (raw == null) return null;
    if (typeof raw === 'number') return Number.isFinite(raw) ? raw : null;
    const trimmed = raw.trim().replace(/[$,%\s]/g, '');
    if (!trimmed) return null;
    const n = Number(trimmed);
    return Number.isFinite(n) ? n : null;
};

/** Render a stored value the way the source spreadsheet does. Non-numeric
 * strings pass through untouched; null/empty render as ''. */
export const formatScorecardValue = (
    raw: string | number | null | undefined,
    valueType: ScorecardValueType | string | null | undefined,
): string => {
    if (raw == null || raw === '') return '';
    const n = parseNumeric(raw);
    if (n === null) return String(raw); // e.g. "TBD"
    switch (valueType) {
        case 'currency':
            return n.toLocaleString('en-US', {
                style: 'currency',
                currency: 'USD',
                minimumFractionDigits: 0,
                maximumFractionDigits: Number.isInteger(n) ? 0 : 2,
            });
        case 'percent': {
            const pct = Math.round(n * 10000) / 100; // 0.9495 → 94.95
            return `${pct.toLocaleString('en-US', { maximumFractionDigits: 2 })}%`;
        }
        default:
            return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
    }
};

/** Comparators read as maths, not as code, in a goal a human is handed. */
const COMPARATOR_SYMBOLS: Record<string, string> = { '>=': '≥', '<=': '≤' };

/**
 * The Goal to SHOW for a measurable — the one answer every surface must give.
 *
 * `goal_description` is the authored goal ("> 98%", "<10,000 usd") and wins,
 * because that's what the Scorecard page's Goal column has always shown: the
 * L10 must not invent a second, different-looking goal for the same
 * measurable. Only when nothing was authored do we build one from the machine
 * fields — comparator + formatted target ("≥ 98%") — which reads as a goal in
 * a way a bare number doesn't, and which formatting keeps in the same units as
 * every weekly cell. Returns '' when the measurable has no goal at all.
 *
 * Note this renders the target FAITHFULLY: a percent target stored in percent
 * points instead of the canonical fraction (a legacy 80 meaning 80%) shows as
 * "8,000%" rather than being quietly rescaled — the same number `goalMet`
 * colours cells against, so the display can't disagree with the colours.
 */
export const formatScorecardGoal = (metric: {
    goal_description?: string | null;
    target_value?: string | number | null;
    comparator?: string | null;
    value_type?: ScorecardValueType | string | null;
    unit?: string | null;
}): string => {
    const authored = metric.goal_description?.trim();
    if (authored) return authored;
    const target = formatScorecardValue(metric.target_value, metric.value_type);
    if (!target) return '';
    // A formatted percent or currency already carries its own sign, so only a
    // plain number takes the metric's unit ("5 hours").
    const unit =
        metric.unit && metric.value_type !== 'percent' && metric.value_type !== 'currency'
            ? ` ${metric.unit}`
            : '';
    const cmp = metric.comparator
        ? `${COMPARATOR_SYMBOLS[metric.comparator] ?? metric.comparator} `
        : '';
    return `${cmp}${target}${unit}`;
};

/** Shift a number by `places` powers of ten, exactly: `shiftDecimal(0.0925, 2)`
 * is 9.25, not 9.250000000000002. Plain `* 100` / `/ 100` in binary floating
 * point leaves artifacts that we would then *store* as the canonical value, so
 * the shift is done on the exponent of the number's decimal (exponential) form,
 * which can't drift. */
const shiftDecimal = (n: number, places: number): number => {
    const [mantissa, exponent = '0'] = n.toExponential().split('e');
    return Number(`${mantissa}e${Number(exponent) + places}`);
};

/** The string to seed an edit box with for a stored value.
 *
 * Percent measurables are stored as fractions but edited in percent points, so
 * a stored 0.0925 comes back as "9.25" — type what you see, see what you typed.
 * Every other value type (and any non-numeric value like "TBD") is handed back
 * verbatim. Pair with `fromEditableScorecardValue` on save. */
export const toEditableScorecardValue = (
    raw: string | number | null | undefined,
    valueType: ScorecardValueType | string | null | undefined,
): string => {
    if (raw == null || raw === '') return '';
    if (valueType !== 'percent') return String(raw);
    const n = parseNumeric(raw);
    if (n === null) return String(raw);
    return String(shiftDecimal(n, 2));
};

/** The canonical string to STORE for what a human typed into an edit box.
 *
 * Percent measurables read the typed number as percent points — "50" → "0.5",
 * "9.25" → "0.0925" — which is what makes 50 mean 50% instead of 5000%. A typed
 * "%" is accepted and ignored ("50%" ≡ "50"). Note the flip side of one
 * consistent rule: "0.5" now means half a percent, not 50%. Every other value
 * type (and any non-numeric value like "TBD") is stored verbatim. */
export const fromEditableScorecardValue = (
    input: string,
    valueType: ScorecardValueType | string | null | undefined,
): string => {
    const trimmed = input.trim();
    if (!trimmed || valueType !== 'percent') return trimmed;
    const n = parseNumeric(trimmed);
    if (n === null) return trimmed;
    return String(shiftDecimal(n, -2));
};

/** Is this week's value meeting its goal? Returns true (green) / false (red),
 * or null when there's nothing to compare (no comparator/target, or the value
 * doesn't parse). Percent values + targets are both fractions, so they compare
 * directly. */
export const goalMet = (
    raw: string | number | null | undefined,
    comparator: string | null | undefined,
    targetValue: string | number | null | undefined,
): boolean | null => {
    if (!comparator) return null;
    const v = parseNumeric(raw);
    const t = parseNumeric(targetValue);
    if (v === null || t === null) return null;
    switch (comparator) {
        case '>':
            return v > t;
        case '>=':
            return v >= t;
        case '<':
            return v < t;
        case '<=':
            return v <= t;
        case '=':
            return v === t;
        default:
            return null;
    }
};

/** Which half of the goal is missing, or null when the measurable is
 * colourable.
 *
 * The Goal column and the green/red colouring read DIFFERENT fields: the
 * column shows `goal_description` (authored prose, "> 98%"), the colouring
 * needs `comparator` AND a numeric `target_value`. A measurable can therefore
 * state a goal and colour nothing — which reads as "the colouring is broken"
 * rather than "this measurable has no target", and is exactly how the Exec
 * scorecard arrived grey. Both halves are checked here so neither can go
 * missing silently. A non-numeric target ("TBD") counts as missing, because
 * `goalMet` can't compare against it either.
 */
export type GoalGap = 'comparator' | 'target' | 'both';

export const goalGap = (metric: {
    comparator?: string | null;
    target_value?: string | number | null;
}): GoalGap | null => {
    const hasComparator = !!metric.comparator?.trim();
    const hasTarget = parseNumeric(metric.target_value) !== null;
    if (hasComparator && hasTarget) return null;
    if (!hasComparator && !hasTarget) return 'both';
    return hasComparator ? 'target' : 'comparator';
};

/** Say which half is missing and what to do about it — a generic "no goal"
 * warning on a row that visibly shows a goal just looks wrong. */
export const GOAL_GAP_MESSAGE: Record<GoalGap, string> = {
    both: "No goal set on this measurable, so its cells can't be coloured green or red. Edit the measurable to set a goal direction and a target.",
    comparator:
        "No goal direction set on this measurable, so its cells can't be coloured green or red. Edit the measurable and pick a comparator.",
    target: "No goal target set on this measurable — the Goal shown here is just a label, so the cells can't be coloured green or red. Edit the measurable and enter the target number.",
};

export const GOAL_GAP_LABEL: Record<GoalGap, string> = {
    both: 'No goal set',
    comparator: 'No goal direction set',
    target: 'No goal target set',
};

// --- Rock milestones ------------------------------------------------
// A milestone is a checklist item on a Rock. Progress is now a simple
// completed/total count — the old per-milestone `weight` field is retired.
// `due_date` is an ISO "YYYY-MM-DD" *local* calendar date (or null).

/** A rock's percent complete: (completed / total) rounded to a whole percent.
 * An empty / missing list is 0 so callers can render an empty bar. */
export function computeMilestoneProgress(
    milestones?: { completed?: boolean }[] | null,
): number {
    if (!milestones || milestones.length === 0) return 0;
    const done = milestones.filter(m => m.completed).length;
    return Math.round((done / milestones.length) * 100);
}

/** Today's *local* calendar date as an ISO "YYYY-MM-DD" string, for comparing
 * against a milestone's due_date (also a local date, not a UTC instant). */
export function todayISO(): string {
    const d = new Date();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${d.getFullYear()}-${mm}-${dd}`;
}

/** A milestone is overdue when it isn't done yet and its due_date is strictly
 * before `today` (defaults to the local calendar date). */
export function isMilestoneOverdue(
    milestone: { completed?: boolean; due_date?: string | null },
    today: string = todayISO(),
): boolean {
    return !milestone.completed && !!milestone.due_date && milestone.due_date < today;
}

/** The incomplete milestone with the soonest due_date — the "next" one to work
 * on. Undated milestones are ignored (nothing to schedule against); returns
 * undefined when no dated, incomplete milestone remains. */
export function nextUpcomingMilestone<
    T extends { completed?: boolean; due_date?: string | null },
>(milestones?: T[] | null): T | undefined {
    if (!milestones) return undefined;
    return milestones
        .filter(m => !m.completed && !!m.due_date)
        .sort((a, b) =>
            a.due_date! < b.due_date! ? -1 : a.due_date! > b.due_date! ? 1 : 0,
        )[0];
}

/** Render an ISO "YYYY-MM-DD" local calendar date as a short locale date.
 * Parsed as local time so the day never shifts across a timezone boundary
 * (`new Date("2026-07-13")` would parse as UTC midnight). Falls back to the
 * raw string when it isn't a well-formed ISO date. */
export function formatISODate(iso: string): string {
    const [y, m, d] = iso.split('-').map(Number);
    if (!y || !m || !d) return iso;
    return new Date(y, m - 1, d).toLocaleDateString();
}

// ── L10 headlines / parking lot ordering ────────────────────────────────────

/** The shape `partitionL10Inputs` needs — the real row carries more. */
export interface L10InputLike {
    discussed: boolean;
    created_at: string;
}

/**
 * Split one tab's queue into what still needs covering and what's been checked
 * off, newest first within each.
 *
 * UAT feedback: "can we remove/hide headlines that have been checked off OR
 * move new headlines to the top?" — the list was append-ordered, so in a long
 * meeting the headline someone added thirty seconds ago landed at the bottom
 * under a stack of struck-through ones. Both halves of the ask are the same
 * fix: covered items leave the live list (the caller tucks them behind a
 * collapsed "Covered" section rather than deleting them — they're still the
 * record of what the meeting discussed), and what's left reads newest first.
 *
 * Ordering is by `created_at`, not array position, so an optimistic local add
 * and a row that arrived via the 2s session poll sort identically. Rows with an
 * unparseable timestamp keep their relative order (Array.sort is stable) rather
 * than being flung to one end.
 */
export function partitionL10Inputs<T extends L10InputLike>(
    items: T[],
): { pending: T[]; covered: T[] } {
    const at = (row: T): number => {
        const ms = Date.parse(row.created_at);
        return Number.isNaN(ms) ? 0 : ms;
    };
    const newestFirst = (rows: T[]) => [...rows].sort((a, b) => at(b) - at(a));
    return {
        pending: newestFirst(items.filter(i => !i.discussed)),
        covered: newestFirst(items.filter(i => i.discussed)),
    };
}

/**
 * Team-filter state for the EOS list views (Issues, To-Dos, Rocks, Scorecard).
 *
 * Two behaviours, both for the same problem. A person with an additional-team
 * grant (a marketer who also files to Sales, say) opens the Issues page and
 * finds the other team's issues in it. Nothing is leaking — the grant is what
 * lets them file there in the first place — but a filter that defaults to
 * "All my teams" makes a second team read as another team's rows invading
 * their own.
 *
 * So: default to the caller's OWN department (`is_primary`), and remember any
 * change. Widening to "All my teams" stays one click away and now sticks,
 * instead of resetting on every visit.
 *
 * Deliberately derived rather than stored-with-an-effect. `scopes` arrives
 * async, so "default once scopes load" would mean setting state from an effect
 * keyed on a fetch result — the pattern the set-state-in-effect lint rejects,
 * and a re-render loop waiting to happen. Instead the effective value is
 * computed each render and state holds only an explicit choice.
 *
 * The stored value is validated against the CURRENT scopes every render: a
 * grant that lapses (someone off an L10's calendar invite) would otherwise
 * leave a remembered filter pointing at a team they no longer hold, silently
 * emptying the page.
 *
 * localStorage, not the server: this is a per-viewer convenience, it must
 * survive a missing/blocked store, and every accessor is wrapped because a
 * private window or a browser set to block site data throws on read.
 */
export const useTeamFilter = (
    scopes: TeamScope[],
    storageKey: string,
): [string, (v: string) => void] => {
    const [chosen, setChosen] = useState<string | null>(() => {
        try {
            return localStorage.getItem(storageKey);
        } catch {
            return null;
        }
    });

    const effective = useMemo(() => {
        const known = new Set(scopes.map(s => s.value));
        // 'all' is always valid — it needs no scope to back it.
        if (chosen && (chosen === 'all' || known.has(chosen))) return chosen;
        return scopes.find(s => s.is_primary)?.value ?? 'all';
    }, [chosen, scopes]);

    const choose = (v: string) => {
        setChosen(v);
        try {
            localStorage.setItem(storageKey, v);
        } catch {
            /* private window / storage blocked — the choice just won't persist */
        }
    };

    return [effective, choose];
};

/**
 * The toast for a refused Scorecard cell save. A 403 carries the server's
 * reason ("Only this measurable's team can record…"); saying it beats a bare
 * "Failed to save", which leaves the person nothing to go on.
 */
export function scorecardSaveError(err: unknown, fallback: string): string {
    const res = (err as { response?: { status?: number; data?: { detail?: unknown } } })
        ?.response;
    const detail = res?.data?.detail;
    return res?.status === 403 && typeof detail === 'string' ? detail : fallback;
}
