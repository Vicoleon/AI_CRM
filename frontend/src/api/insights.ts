// Insights — Cubie's BI dashboards (the "Insights" rail area).
//
// THE CONTRACT IS `docs/insights/README.md`. Every type and wire string in this
// file was read out of it: the endpoints (§4), the filter parameters (§3), the
// section kinds and their `data` shapes, and the units. The backend that keeps
// it is `backend/routers/insights.py` with the dashboards in
// `rubik_core/insights/dashboards/`. Where this file and the README disagree,
// fix whichever one is wrong in the same PR — never "tidy" a field here alone.
//
// Uses the shared axios instance like `cockpit.ts` and `looseEnds.ts`: full
// `/api/...` paths, no baseURL, no timeout of our own (client.ts is 105s).
//
// THREE THINGS IN HERE ARE LOAD-BEARING:
//
// 1. Kill switch off is a 404 on every endpoint (README §1), and a viewer the
//    feature is closed to gets a 403. Both are calm empty states on the page,
//    never a red error — `insightsError` names them so no screen can forget.
//    A 404 on the PERSON endpoint is different: it means "not in the
//    departments you can see", so that call maps it to `not_found` instead.
// 2. `pct` and `score` values are 0–1 (README "Units"), and every `pct` KPI
//    carries its numerator and denominator. The page never prints a percentage
//    without its "x of y" (see `insightsFormat.ts::denominatorText`).
// 3. `/meta` is fetched once per signed-in identity and shared: every dashboard
//    and the person profile read the same catalog of metric definitions.
import client, { impersonationHeaders } from './client';
import { activeToken } from '../brand/storage';
import { filtersToParams } from '../pages/insights/insightsFilters.ts';
import type { InsightsFilters } from '../pages/insights/insightsFilters.ts';

// ─── units and the metric catalog ────────────────────────────────────────────

/** README "Units". `pct` and `score` are 0–1. */
export type InsightsUnit =
    | 'count'
    | 'pct'
    | 'usd'
    | 'ms'
    | 'days'
    | 'ratio'
    | 'score'
    | 'text'
    | 'date'
    | 'datetime';

/** Which way is good for a metric; colours its delta chip. */
export type GoodDirection = 'up' | 'down' | 'neutral';

export interface MetricDef {
    id: string;
    label: string;
    /** One plain sentence: what the number counts. */
    definition: string;
    /** What a percentage (or a rate) is out of, e.g. "people with Cubie access in scope". */
    denominator: string | null;
    unit: InsightsUnit;
    good_direction: GoodDirection;
    /** The unit of a `pct` KPI's numerator and denominator when they are not
     *  counts, e.g. `usd` for a share of dollars ("$57 of $215"). */
    part_unit?: InsightsUnit | null;
}

// ─── /meta ───────────────────────────────────────────────────────────────────

export interface InsightsDepartment {
    slug: string;
    label: string;
    people: number;
}

/** The four nav groups (README §2), in render order. */
export type DashboardGroupKey = 'company' | 'work' | 'sales' | 'operations';

export interface DashboardInfo {
    slug: string;
    title: string;
    group: DashboardGroupKey | string;
    /** False until the dashboard ships; the nav hides it. */
    available: boolean;
    description: string;
}

export interface InsightsMeta {
    /** Already limited to the departments this viewer may see. */
    departments: InsightsDepartment[];
    /** True for admins and org-wide execs. */
    all_departments: boolean;
    /** The `insights_people` verdict: person tables and the person profile. */
    can_view_people: boolean;
    dashboards: DashboardInfo[];
    metrics: Record<string, MetricDef>;
    tz: string;
    ranges: string[];
}

// ─── the dashboard payload ───────────────────────────────────────────────────

export interface InsightsPeriod {
    start: string;
    end: string;
    prev_start: string;
    prev_end: string;
    days: number;
    bucket: 'day' | 'week';
    tz: string;
    /** Server-written, e.g. "Last 30 days". */
    label: string;
}

export interface InsightsScope {
    departments: string[];
    labels: string[];
    all: boolean;
    people: number;
}

export interface Kpi {
    /** Key into `InsightsMeta.metrics` (label, unit, definition, direction). */
    id: string;
    /** `null` when it cannot be computed (for example the denominator is 0). */
    value: number | null;
    prev_value?: number | null;
    numerator?: number | null;
    denominator?: number | null;
    spark?: (number | null)[] | null;
    /** On Overview: the dashboard that owns the number. */
    href?: string | null;
}

export type SectionWidth = 'full' | 'half' | 'third';
export type SectionDrill = 'department' | 'person' | null;

interface SectionBase {
    id: string;
    title: string;
    subtitle?: string | null;
    width: SectionWidth;
    drill?: SectionDrill;
    /** Why the section is empty, in the server's words. */
    empty_hint?: string | null;
}

export interface TimeseriesSeries {
    key: string;
    label: string;
    unit: InsightsUnit;
    values: (number | null)[];
}

export interface TimeseriesData {
    /** `YYYY-MM-DD` bucket starts (a Monday when the bucket is a week). */
    x: string[];
    chart: 'line' | 'bar';
    stacked: boolean;
    series: TimeseriesSeries[];
}

export interface BarItem {
    /** Department slug (drill `department`) or email (drill `person`). */
    key: string;
    label: string;
    value: number | null;
    numerator?: number | null;
    denominator?: number | null;
    /** Why `value` is null when the server withheld it on purpose, e.g.
     *  "Hidden: fewer than 3 people" (small groups would identify a person). */
    note?: string | null;
}

export interface BarData {
    unit: InsightsUnit;
    items: BarItem[];
}

export interface FunnelStep {
    key: string;
    label: string;
    value: number;
}

export interface FunnelData {
    /** `sequential` (default): each step is a subset of the one before.
     *  `breakdown`: the first step is the whole and the rest split it into
     *  outcomes that add up to it — no step-to-step rate is shown. */
    mode?: 'sequential' | 'breakdown';
    steps: FunnelStep[];
}

export interface MatrixCell {
    value: number | null;
    numerator?: number | null;
    denominator?: number | null;
    /** Why `value` is null when the server withheld it on purpose, e.g.
     *  "Hidden: fewer than 3 people" (small groups would identify a person). */
    note?: string | null;
}

export interface MatrixAxisItem {
    key: string;
    label: string;
}

export interface MatrixData {
    unit: InsightsUnit;
    scale: 'sequential' | 'diverging';
    rows: MatrixAxisItem[];
    cols: MatrixAxisItem[];
    /** `cells[row][col]`; `null` where there is nothing to show. */
    cells: (MatrixCell | null)[][];
}

export interface TableColumn {
    key: string;
    label: string;
    unit: InsightsUnit;
}

export type TableCellValue = string | number | boolean | null;

export interface TableRow {
    /** An email when `row_link` is `person`. */
    key: string;
    cells: Record<string, TableCellValue>;
}

export interface TableData {
    columns: TableColumn[];
    rows: TableRow[];
    row_link: 'person' | null;
    default_sort?: { key: string; dir: 'asc' | 'desc' } | null;
}

export type TimeseriesSection = SectionBase & { kind: 'timeseries'; data: TimeseriesData };
export type BarSection = SectionBase & { kind: 'bar'; data: BarData };
export type FunnelSection = SectionBase & { kind: 'funnel'; data: FunnelData };
export type MatrixSection = SectionBase & { kind: 'matrix'; data: MatrixData };
export type TableSection = SectionBase & { kind: 'table'; data: TableData };

/** Discriminated on `kind` (README §4 "Section"). */
export type Section = TimeseriesSection | BarSection | FunnelSection | MatrixSection | TableSection;

export const SECTION_KINDS = ['timeseries', 'bar', 'funnel', 'matrix', 'table'] as const;
export type SectionKind = (typeof SECTION_KINDS)[number];

export const isSectionKind = (v: unknown): v is SectionKind =>
    typeof v === 'string' && (SECTION_KINDS as readonly string[]).includes(v);

export interface DashboardPayload {
    dashboard: string;
    title: string;
    /** An instant (ISO, UTC). */
    generated_at: string;
    cached: boolean;
    period: InsightsPeriod;
    scope: InsightsScope;
    kpis: Kpi[];
    sections: Section[];
    /** Coverage caveats, e.g. "Slack delivery is tracked from Oct 9." */
    notes: string[];
}

// ─── the person profile ──────────────────────────────────────────────────────

export interface PersonSummary {
    email: string;
    name: string;
    department: string | null;
    department_label: string | null;
    /** A calendar day in the company's business time zone, `YYYY-MM-DD`, or
     *  null when never active. */
    last_active: string | null;
}

export interface CalendarDay {
    date: string;
    count: number;
}

export interface PersonCard {
    dashboard: string;
    title: string;
    kpis: Kpi[];
}

export interface PersonFeature {
    key: string;
    label: string;
    has_access: boolean;
    used: boolean;
}

export interface PersonProfile {
    person: PersonSummary;
    period: InsightsPeriod;
    /** Always the 90 days ending at `period.end`. */
    calendar: CalendarDay[];
    kpis: Kpi[];
    cards: PersonCard[];
    features: PersonFeature[];
    notes: string[];
}

// ─── the AI summary ──────────────────────────────────────────────────────────

export interface SummaryAvailable {
    available: true;
    headline: string;
    bullets: string[];
    watch: string[];
    generated_at: string;
    cached: boolean;
}

export interface SummaryUnavailable {
    available: false;
    reason: string;
}

export type InsightsSummary = SummaryAvailable | SummaryUnavailable;

// ─── errors ──────────────────────────────────────────────────────────────────

export type InsightsErrorCode = 'not_enabled' | 'forbidden' | 'not_found' | 'bad_request' | 'error';

export interface InsightsError {
    code: InsightsErrorCode;
    message: string;
    status: number | null;
}

interface MaybeAxiosError {
    response?: { status?: number; data?: { detail?: unknown } };
    message?: string;
}

const FALLBACK = 'Insights could not be reached. Try again in a moment.';

/** The server's own sentence, when it sent one worth showing. */
function detailMessage(detail: unknown): string | null {
    if (typeof detail === 'string' && detail.trim() && detail.trim() !== 'Not Found') return detail.trim();
    if (detail && typeof detail === 'object' && 'message' in detail) {
        const m = (detail as { message?: unknown }).message;
        if (typeof m === 'string' && m.trim()) return m.trim();
    }
    return null;
}

/**
 * Normalise a rejected request into one of five codes.
 *
 * `notFound` says what a 404 means for THIS endpoint: the kill switch on the
 * meta and dashboard reads (`not_enabled`), "outside your departments" on the
 * person read (`not_found`).
 */
export function insightsError(
    e: unknown,
    { notFound = 'not_enabled' }: { notFound?: 'not_enabled' | 'not_found' } = {},
): InsightsError {
    const err = (e ?? {}) as MaybeAxiosError;
    const status = err.response?.status ?? null;
    const detail = detailMessage(err.response?.data?.detail);

    if (status === 404) {
        return notFound === 'not_found'
            ? { code: 'not_found', status, message: detail ?? 'That person is not in the departments you can see.' }
            : { code: 'not_enabled', status, message: 'Insights is not switched on yet.' };
    }
    if (status === 403) {
        return { code: 'forbidden', status, message: detail ?? 'This view is not open to you.' };
    }
    if (status === 400 || status === 422) {
        return { code: 'bad_request', status, message: detail ?? 'Those filters could not be applied.' };
    }
    return { code: 'error', status, message: detail ?? err.message ?? FALLBACK };
}

export const isNotEnabled = (err: InsightsError | null | undefined): boolean => err?.code === 'not_enabled';
export const isForbidden = (err: InsightsError | null | undefined): boolean => err?.code === 'forbidden';
export const isNotFound = (err: InsightsError | null | undefined): boolean => err?.code === 'not_found';

// ─── endpoints ───────────────────────────────────────────────────────────────

const BASE = '/api/insights';

/** One `/meta` per signed-in identity (token + impersonation). Cleared on
 *  failure, so a transient error never sticks for the session. */
let metaCache: { key: string; promise: Promise<InsightsMeta> } | null = null;

const identityKey = (): string => {
    return `${activeToken() ?? ''}|${JSON.stringify(impersonationHeaders())}`;
};

export function fetchInsightsMeta({ force = false }: { force?: boolean } = {}): Promise<InsightsMeta> {
    const key = identityKey();
    if (!force && metaCache && metaCache.key === key) return metaCache.promise;
    const promise = client.get<InsightsMeta>(`${BASE}/meta`).then(res => res.data);
    metaCache = { key, promise };
    promise.catch(() => {
        if (metaCache?.promise === promise) metaCache = null;
    });
    return promise;
}

export async function fetchDashboard(
    slug: string,
    filters: InsightsFilters,
    { refresh = false }: { refresh?: boolean } = {},
): Promise<DashboardPayload> {
    const params = filtersToParams(filters);
    if (refresh) params.refresh = '1';
    const res = await client.get<DashboardPayload>(`${BASE}/dashboards/${encodeURIComponent(slug)}`, { params });
    return res.data;
}

/** The person profile. Only the date range applies (README §4); the
 *  department filter is the person's own. */
export async function fetchPerson(
    email: string,
    filters: InsightsFilters,
    { refresh = false }: { refresh?: boolean } = {},
): Promise<PersonProfile> {
    const params = filtersToParams({ ...filters, dept: [] });
    if (refresh) params.refresh = '1';
    const res = await client.get<PersonProfile>(`${BASE}/people/${encodeURIComponent(email)}`, { params });
    return res.data;
}

export async function summarizeDashboard(slug: string, filters: InsightsFilters): Promise<InsightsSummary> {
    const res = await client.post<InsightsSummary>(
        `${BASE}/dashboards/${encodeURIComponent(slug)}/summary`,
        null,
        { params: filtersToParams(filters) },
    );
    return res.data;
}
