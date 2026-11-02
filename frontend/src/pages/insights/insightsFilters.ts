// Insights filters: the URL codec, the range presets, and "remember my last
// filters".
//
// THE URL IS THE STATE (docs/insights/README.md §3). `?dept=sales,marketing
// &range=30d` (or `&range=custom&start=…&end=…`) is what every dashboard reads,
// so any view can be bookmarked, reloaded or pasted to a colleague and land on
// the same numbers. Nothing here is mirrored into React state: the page derives
// its filters from `useSearchParams` and writes them back with `replace`.
//
// `range` is ALWAYS written, even at its default. A link copied from the app
// therefore always carries its own range, and "restore my remembered filters"
// (which only runs on a URL with no filter params at all) can never override a
// link someone shared.
//
// A leaf module (no imports), so `node --test` loads it without a bundler.

export const RANGE_PRESETS = ['7d', '30d', '90d', 'qtd', 'ytd', 'custom'] as const;
export type RangePreset = (typeof RANGE_PRESETS)[number];

export const DEFAULT_RANGE: RangePreset = '30d';

/** At most this many days in a custom range (README §3). */
export const MAX_CUSTOM_DAYS = 366;

/** The query parameters that are filters. Anything else on the URL is left alone. */
export const FILTER_PARAMS = ['dept', 'range', 'start', 'end'] as const;

export const RANGE_LABELS: Record<RangePreset, string> = {
    '7d': 'Last 7 days',
    '30d': 'Last 30 days',
    '90d': 'Last 90 days',
    qtd: 'Quarter to date',
    ytd: 'Year to date',
    custom: 'Custom range',
};

/** The segmented control's button text. */
export const RANGE_SHORT: Record<RangePreset, string> = {
    '7d': '7D',
    '30d': '30D',
    '90d': '90D',
    qtd: 'QTD',
    ytd: 'YTD',
    custom: 'Custom',
};

export interface InsightsFilters {
    /** Department slugs. Empty means everything in the viewer's scope. */
    dept: string[];
    range: RangePreset;
    /** `YYYY-MM-DD`, set only when `range` is `custom`. */
    start: string | null;
    end: string | null;
}

export const DEFAULT_FILTERS: InsightsFilters = { dept: [], range: DEFAULT_RANGE, start: null, end: null };

export const isRangePreset = (v: unknown): v is RangePreset =>
    typeof v === 'string' && (RANGE_PRESETS as readonly string[]).includes(v);

// ─── dates (calendar days, never instants) ───────────────────────────────────

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const DAY_MS = 86_400_000;

/** A real calendar day written `YYYY-MM-DD` (rejects 2026-02-30). */
export function isIsoDate(value: unknown): value is string {
    if (typeof value !== 'string') return false;
    const m = ISO_DATE.exec(value);
    if (!m) return false;
    const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
    const at = new Date(Date.UTC(y, mo - 1, d));
    return at.getUTCFullYear() === y && at.getUTCMonth() === mo - 1 && at.getUTCDate() === d;
}

const toUtcMs = (iso: string): number => {
    const [y, m, d] = iso.split('-').map(Number);
    return Date.UTC(y, m - 1, d);
};

const fromUtcMs = (ms: number): string => new Date(ms).toISOString().slice(0, 10);

/** `iso` moved by `n` calendar days. UTC arithmetic, so no DST surprises. */
export const addDays = (iso: string, n: number): string => fromUtcMs(toUtcMs(iso) + n * DAY_MS);

/** Days from `start` to `end`, both included. */
export const daysInclusive = (start: string, end: string): number =>
    Math.round((toUtcMs(end) - toUtcMs(start)) / DAY_MS) + 1;

/** Today as a calendar day in `tz`: the company's business time zone, UTC when none is set. */
export function todayIn(tz = 'UTC', now: Date = new Date()): string {
    try {
        // en-CA formats as YYYY-MM-DD.
        return new Intl.DateTimeFormat('en-CA', {
            timeZone: tz,
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
        }).format(now);
    } catch {
        return now.toISOString().slice(0, 10);
    }
}

/** Why a custom range can't be used, as a sentence; `null` when it can. */
export function customRangeError(start: string | null | undefined, end: string | null | undefined): string | null {
    if (!start || !end) return 'Pick a start date and an end date.';
    if (!isIsoDate(start) || !isIsoDate(end)) return 'Dates must be real calendar days.';
    if (toUtcMs(start) > toUtcMs(end)) return 'The start date is after the end date.';
    if (daysInclusive(start, end) > MAX_CUSTOM_DAYS) return `A custom range can cover at most ${MAX_CUSTOM_DAYS} days.`;
    return null;
}

// ─── the codec ───────────────────────────────────────────────────────────────

/** A department slug as `slugify_department` writes it. */
const SLUG = /^[a-z0-9][a-z0-9_-]{0,63}$/;

/** `dept=` → unique, valid slugs in the order given. */
export function parseDepartments(raw: string | null | undefined): string[] {
    if (!raw) return [];
    const out: string[] = [];
    for (const part of raw.split(',')) {
        const slug = part.trim().toLowerCase();
        if (SLUG.test(slug) && !out.includes(slug)) out.push(slug);
    }
    return out;
}

/**
 * Read the filters off a query string. Never throws and never returns an
 * invalid combination: an unknown range falls back to the default, and a
 * custom range that is incomplete, inverted or too long falls back too (the
 * page then rewrites the URL to what it is actually showing).
 */
export function parseFilters(source: URLSearchParams | string): InsightsFilters {
    const params = typeof source === 'string' ? new URLSearchParams(source) : source;
    const dept = parseDepartments(params.get('dept'));
    const rawRange = params.get('range');
    const range: RangePreset = isRangePreset(rawRange) ? rawRange : DEFAULT_RANGE;
    if (range === 'custom') {
        const start = params.get('start');
        const end = params.get('end');
        if (customRangeError(start, end) === null) return { dept, range, start, end };
        return { dept, range: DEFAULT_RANGE, start: null, end: null };
    }
    return { dept, range, start: null, end: null };
}

/** The filters as request parameters (also what the URL carries). */
export function filtersToParams(filters: InsightsFilters): Record<string, string> {
    const out: Record<string, string> = {};
    if (filters.dept.length > 0) out.dept = filters.dept.join(',');
    out.range = filters.range;
    if (filters.range === 'custom' && filters.start && filters.end) {
        out.start = filters.start;
        out.end = filters.end;
    }
    return out;
}

export function serializeFilters(filters: InsightsFilters): URLSearchParams {
    return new URLSearchParams(filtersToParams(filters));
}

/** `?dept=…&range=…` for a link, with the comma left readable. */
export function filtersToQuery(filters: InsightsFilters): string {
    const qs = serializeFilters(filters).toString().replace(/%2C/gi, ',');
    return qs ? `?${qs}` : '';
}

/** Does this query string carry any filter at all? */
export function hasFilterParams(source: URLSearchParams | string): boolean {
    const params = typeof source === 'string' ? new URLSearchParams(source) : source;
    return FILTER_PARAMS.some(k => params.has(k));
}

/**
 * The filter part of `location.search`, normalised, for carrying the current
 * filters onto another Insights link (the nav, the KPI tiles, the person
 * profile's back link). Empty when the current URL has no filters, so the
 * destination falls back to the viewer's remembered ones.
 */
export function filterQueryFromSearch(search: string): string {
    return hasFilterParams(search) ? filtersToQuery(parseFilters(search)) : '';
}

/** `path` with the current filters carried over. Keeps a query `path` already has. */
export function withFilterQuery(path: string, filterQuery: string): string {
    if (!filterQuery) return path;
    const [base, existing] = path.split('?', 2);
    if (!existing) return `${base}${filterQuery}`;
    const merged = new URLSearchParams(existing);
    for (const [k, v] of new URLSearchParams(filterQuery.slice(1))) if (!merged.has(k)) merged.set(k, v);
    return `${base}?${merged.toString().replace(/%2C/gi, ',')}`;
}

/** `prev` with its filter params replaced by `filters`; anything else kept. */
export function mergeFilterParams(prev: URLSearchParams, filters: InsightsFilters): URLSearchParams {
    const next = new URLSearchParams(prev);
    for (const k of FILTER_PARAMS) next.delete(k);
    for (const [k, v] of Object.entries(filtersToParams(filters))) next.set(k, v);
    return next;
}

/** A stable identity for "the same filters", e.g. to key a fetch. */
export const filtersKey = (filters: InsightsFilters): string => filtersToQuery(filters);

export const sameFilters = (a: InsightsFilters, b: InsightsFilters): boolean => filtersKey(a) === filtersKey(b);

// ─── edits ───────────────────────────────────────────────────────────────────

export function withDepartments(filters: InsightsFilters, dept: readonly string[]): InsightsFilters {
    return { ...filters, dept: parseDepartments(dept.join(',')) };
}

export function toggleDepartment(filters: InsightsFilters, slug: string): InsightsFilters {
    const has = filters.dept.includes(slug);
    return withDepartments(filters, has ? filters.dept.filter(d => d !== slug) : [...filters.dept, slug]);
}

/**
 * Switch to a preset. Choosing Custom with no dates yet opens on the last 30
 * days ending `today`, so the two date inputs never start empty.
 */
export function withRange(filters: InsightsFilters, range: RangePreset, today: string): InsightsFilters {
    if (range !== 'custom') return { ...filters, range, start: null, end: null };
    if (filters.range === 'custom' && filters.start && filters.end) return filters;
    return { ...filters, range, start: addDays(today, -29), end: today };
}

/** A custom range, or `null` when those two dates can't be used. */
export function withCustomDates(filters: InsightsFilters, start: string, end: string): InsightsFilters | null {
    if (customRangeError(start, end) !== null) return null;
    return { ...filters, range: 'custom', start, end };
}

// ─── captions ────────────────────────────────────────────────────────────────

const SHORT_DATE = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
const LONG_DATE = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });

/** "Sep 9" — a calendar day, formatted without any timezone shift. */
export const shortDate = (iso: string): string => (isIsoDate(iso) ? SHORT_DATE.format(new Date(toUtcMs(iso))) : iso);

/** "Sep 9, 2026". */
export const longDate = (iso: string): string => (isIsoDate(iso) ? LONG_DATE.format(new Date(toUtcMs(iso))) : iso);

/** What the range control shows: "Last 30 days", or "Sep 1 – Sep 30, 2026". */
export function rangeCaption(filters: InsightsFilters): string {
    if (filters.range === 'custom' && filters.start && filters.end) {
        return `${shortDate(filters.start)} – ${longDate(filters.end)}`;
    }
    return RANGE_LABELS[filters.range];
}

/** How many days a range covers before the server has said: exact for the
 *  fixed presets and a custom range, unknown (null) for QTD and YTD. */
export function expectedDays(filters: InsightsFilters): number | null {
    switch (filters.range) {
        case '7d':
            return 7;
        case '30d':
            return 30;
        case '90d':
            return 90;
        case 'custom':
            return filters.start && filters.end ? daysInclusive(filters.start, filters.end) : null;
        default:
            return null;
    }
}

/** The raw filter params a URL carries, in canonical order and spelling, so
 *  the page can tell when its URL needs rewriting to what it is showing. */
export function rawFilterQuery(params: URLSearchParams): string {
    const raw = new URLSearchParams();
    for (const k of FILTER_PARAMS) {
        const v = params.get(k);
        if (v !== null) raw.set(k, v);
    }
    const qs = raw.toString().replace(/%2C/gi, ',');
    return qs ? `?${qs}` : '';
}

/** "vs previous 30 days" — every delta on the page compares to that window. */
export function comparisonCaption(days: number | null | undefined): string {
    if (!days || days < 1) return 'vs previous period';
    return days === 1 ? 'vs previous day' : `vs previous ${days} days`;
}

// ─── remember / restore ──────────────────────────────────────────────────────

export const FILTERS_STORAGE_KEY = 'insights.filters.v1';

/** The bit of `Storage` this module needs, so tests can hand in a stub. */
export interface FilterStorage {
    getItem(key: string): string | null;
    setItem(key: string, value: string): void;
}

function browserStorage(): FilterStorage | null {
    try {
        return typeof window !== 'undefined' ? window.localStorage : null;
    } catch {
        return null;
    }
}

/** Remember the last filters used. A full or blocked store costs the
 *  convenience, nothing else. */
export function rememberFilters(filters: InsightsFilters, storage: FilterStorage | null = browserStorage()): void {
    if (!storage) return;
    try {
        storage.setItem(FILTERS_STORAGE_KEY, filtersToQuery(filters));
    } catch {
        /* ignore */
    }
}

/** The remembered filters, validated through the same codec as the URL;
 *  `null` when nothing (usable) was remembered. */
export function restoreFilters(storage: FilterStorage | null = browserStorage()): InsightsFilters | null {
    if (!storage) return null;
    try {
        const raw = storage.getItem(FILTERS_STORAGE_KEY);
        if (!raw || !hasFilterParams(raw)) return null;
        return parseFilters(raw.startsWith('?') ? raw.slice(1) : raw);
    } catch {
        return null;
    }
}
