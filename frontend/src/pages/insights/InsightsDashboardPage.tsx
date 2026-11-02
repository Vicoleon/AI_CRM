// Insights → any dashboard. One page renders every dashboard from the
// server's declarative payload (docs/insights/README.md §4): KPI tiles, then
// sections by `kind` in a 12-column grid by `width`, then the coverage notes.
// Adding a dashboard is a backend module and one line in `dashboards.ts`.
//
// Routes: `/insights` (Overview) and `/insights/:dashboard`. Filters live in
// the URL (see `useInsightsFilters`), so every view can be bookmarked and
// shared.
import React, { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { MotionConfig } from 'framer-motion';
import { BookOpenText, Check, ChevronRight, Download, Link2 } from 'lucide-react';
import { useAuth } from '../../context/AuthContext';
import { fetchDashboard, insightsError, isForbidden, isNotEnabled } from '../../api/insights';
import type { DashboardPayload, InsightsError, Kpi, MetricDef } from '../../api/insights';
import { AiSummaryCard } from '../../components/insights/AiSummaryCard';
import { DefinitionsDrawer } from '../../components/insights/DefinitionsDrawer';
import { FilterBar } from '../../components/insights/FilterBar';
import { FreshnessBadge } from '../../components/insights/FreshnessBadge';
import { KpiGrid, KpiTile } from '../../components/insights/KpiTile';
import {
    ComingSoonState,
    ErrorRetryState,
    ForbiddenState,
    NotEnabledState,
    NotFoundState,
    NotesList,
    ScopeChips,
} from '../../components/insights/PageStates';
import { SectionRenderer } from '../../components/insights/SectionRenderer';
import { scrollParent } from '../help/scroll';
import type { SectionContext } from '../../components/insights/SectionRenderer';
import { DashboardSkeleton } from '../../components/insights/Skeleton';
import { DASHBOARD_ICONS } from '../../components/insights/dashboardIcons';
import { csvFilename, downloadCsv, exportValue, toCsv } from './csv';
import { DEFAULT_DASHBOARD, findDashboard, insightsPath, personPath } from './dashboards';
import {
    DEFAULT_FILTERS,
    comparisonCaption,
    expectedDays,
    filtersKey,
    longDate,
    shortDate,
    todayIn,
    withDepartments,
    withFilterQuery,
} from './insightsFilters';
import { groupKpisByDashboard } from './kpiGroups';
import { useInsightsFilters, useInsightsMeta } from './useInsights';

interface Loaded {
    /** slug + filters the response is for. */
    key: string;
    slug: string;
    /** The refresh click it answered (0 = none). */
    nonce: number;
    data: DashboardPayload | null;
    error: InsightsError | null;
    /** The same dashboard's last good numbers, kept through a transient failure. */
    lastGood: DashboardPayload | null;
}

const HEADER_BUTTON =
    'inline-flex h-8 items-center gap-1.5 rounded-md border border-gray-200 bg-white px-2.5 text-xs font-medium text-gray-600 shadow-sm transition-colors hover:border-gray-300 hover:bg-gray-50 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30 disabled:cursor-not-allowed disabled:opacity-50';

export const InsightsDashboardPage: React.FC = () => {
    const { dashboard: slugParam } = useParams<{ dashboard?: string }>();
    const slug = slugParam ?? DEFAULT_DASHBOARD;
    const entry = findDashboard(slug);
    const { hasFeature } = useAuth();
    const { meta, error: metaError, loading: metaLoading, retry: retryMeta } = useInsightsMeta();
    const { filters, setFilters, filterQuery } = useInsightsFilters();

    // Open each dashboard at the top. Layout's <main> keeps its scroll offset
    // across routes, so moving from far down one dashboard to the next would
    // otherwise land partway down it. A filter change keeps the reader's place.
    const [root, setRoot] = useState<HTMLDivElement | null>(null);
    useEffect(() => {
        scrollParent(root)?.scrollTo({ top: 0, behavior: 'instant' });
    }, [root, slug]);

    const metaDash = meta?.dashboards.find(d => d.slug === slug) ?? null;
    // The server's word wins; before it answers, the registry's.
    const available = metaDash ? metaDash.available : (entry?.live ?? false);
    const known = !!entry || !!metaDash;
    const shouldFetch = known && available && !isNotEnabled(metaError) && !isForbidden(metaError);

    const requestKey = `${slug}${filtersKey(filters)}`;
    const [loaded, setLoaded] = useState<Loaded | null>(null);
    const [refresh, setRefresh] = useState<{ nonce: number; key: string }>({ nonce: 0, key: '' });
    const [definitionsOpen, setDefinitionsOpen] = useState(false);
    const [copied, setCopied] = useState(false);

    // Every setState lands in the async callback (react-hooks/set-state-in-
    // effect); "loading" and "refreshing" are derived below.
    useEffect(() => {
        if (!shouldFetch) return;
        let cancelled = false;
        const nonce = refresh.nonce;
        const force = nonce > 0 && refresh.key === requestKey;
        (async () => {
            try {
                const data = await fetchDashboard(slug, filters, { refresh: force });
                if (!cancelled) setLoaded({ key: requestKey, slug, nonce, data, error: null, lastGood: null });
            } catch (e) {
                if (!cancelled) {
                    setLoaded(prev => ({
                        key: requestKey,
                        slug,
                        nonce,
                        data: null,
                        error: insightsError(e),
                        lastGood: prev && prev.slug === slug ? (prev.data ?? prev.lastGood) : null,
                    }));
                }
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [shouldFetch, slug, filters, requestKey, refresh]);

    const current = loaded && loaded.key === requestKey ? loaded : null;
    // Refetch keeps the frame: while new filters load, the same dashboard's
    // last numbers stay on screen, dimmed, instead of a skeleton flash.
    // A transient failure (network, 5xx) keeps the last good numbers under an
    // error banner; a refusal (403, switched off, bad filters) replaces them.
    const keepLastGood = loaded?.error?.code === 'error';
    const shown = loaded && loaded.slug === slug ? (loaded.data ?? (keepLastGood ? loaded.lastGood : null)) : null;
    const busy = shouldFetch && (!loaded || loaded.key !== requestKey || loaded.nonce !== refresh.nonce);
    const stale = !!shown && busy && loaded?.key !== requestKey;
    const error = current?.error ?? null;

    const title = entry?.title ?? metaDash?.title ?? shown?.title ?? 'Insights';
    const description = metaDash?.description || entry?.description || '';
    const Icon = entry ? DASHBOARD_ICONS[entry.icon] : DASHBOARD_ICONS.overview;
    const today = useMemo(() => todayIn(meta?.tz ?? 'UTC'), [meta?.tz]);
    const canViewPeople = meta?.can_view_people ?? hasFeature('insights_people');
    const metrics = useMemo(() => meta?.metrics ?? {}, [meta]);

    const period = shown?.period ?? null;
    const comparison = comparisonCaption(period?.days ?? expectedDays(filters));
    const periodCaption = period ? `${shortDate(period.start)} – ${longDate(period.end)}` : null;

    const pageMetrics = useMemo<MetricDef[]>(() => {
        const out: MetricDef[] = [];
        for (const k of shown?.kpis ?? []) {
            const m = metrics[k.id];
            if (m && !out.some(o => o.id === m.id)) out.push(m);
        }
        return out;
    }, [shown, metrics]);

    const onRefresh = () => setRefresh(r => ({ nonce: r.nonce + 1, key: requestKey }));
    const clearDepartments = () => setFilters(withDepartments(filters, []));

    const sectionCtx: SectionContext | null = period
        ? {
              period,
              dashboard: slug,
              onDrillDepartment: dept => setFilters(withDepartments(filters, [dept])),
              personHref: email => (canViewPeople ? withFilterQuery(personPath(email), filterQuery) : null),
              activeDepartments: filters.dept,
          }
        : null;

    const exportKpis = () => {
        if (!shown) return;
        const rows = shown.kpis.map(k => {
            const m = metrics[k.id];
            const unit = m?.unit ?? 'count';
            return [m?.label ?? k.id, exportValue(k.value, unit), exportValue(k.prev_value ?? null, unit), k.numerator, k.denominator, m?.denominator ?? '', unit];
        });
        downloadCsv(
            csvFilename(['insights', slug, 'headline', shown.period.start, 'to', shown.period.end]),
            toCsv(['Metric', 'Value', 'Previous period', 'Numerator', 'Denominator', 'Out of', 'Unit'], rows),
        );
    };

    const copyLink = async () => {
        try {
            await navigator.clipboard.writeText(window.location.href);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 2000);
        } catch {
            /* clipboard blocked: the address bar still has the link */
        }
    };

    // ── the body, by state ──────────────────────────────────────────────────
    let body: React.ReactNode;
    if (isNotEnabled(metaError)) {
        body = <NotEnabledState />;
    } else if (isForbidden(metaError)) {
        body = <ForbiddenState message={metaError?.message} />;
    } else if (!known && (meta || metaError)) {
        body = (
            <NotFoundState title="There is no dashboard here" href={withFilterQuery('/insights', filterQuery)} hrefLabel="Go to the Overview">
                <p>
                    &ldquo;{slug}&rdquo; isn&apos;t one of the Insights dashboards. Pick one from the panel on the left.
                </p>
            </NotFoundState>
        );
    } else if (!known && metaLoading) {
        body = <DashboardSkeleton />;
    } else if (!available) {
        body = <ComingSoonState title={title} description={description} overviewHref={withFilterQuery('/insights', filterQuery)} />;
    } else if (error && !shown) {
        if (isForbidden(error)) {
            body = <ForbiddenState message={error.message} onClearDepartments={filters.dept.length > 0 ? clearDepartments : null} />;
        } else if (isNotEnabled(error)) {
            // /meta answered, so the feature is on: this dashboard isn't served yet.
            body = meta ? (
                <ComingSoonState title={title} description={description} overviewHref={withFilterQuery('/insights', filterQuery)} />
            ) : (
                <NotEnabledState />
            );
        } else {
            body = (
                <ErrorRetryState
                    message={error.message}
                    onRetry={onRefresh}
                    onReset={error.code === 'bad_request' ? () => setFilters(DEFAULT_FILTERS) : null}
                />
            );
        }
    } else if (!shown || !sectionCtx) {
        body = <DashboardSkeleton tiles={slug === DEFAULT_DASHBOARD ? 5 : 4} />;
    } else {
        const renderTile = (k: Kpi) => {
            const target = k.href ? findDashboard(k.href.split('?')[0].split('/')[2] ?? DEFAULT_DASHBOARD) : null;
            return (
                <KpiTile
                    key={k.id}
                    kpi={k}
                    metric={metrics[k.id]}
                    comparison={comparison}
                    href={k.href ? withFilterQuery(k.href, filterQuery) : null}
                    hrefLabel={target?.title ?? null}
                />
            );
        };
        // Overview's twenty-odd tiles read better grouped by the dashboard
        // each one opens; a single group (or any other dashboard) stays flat.
        const grouped = slug === DEFAULT_DASHBOARD ? groupKpisByDashboard(shown.kpis) : [];
        const kpiGroups = grouped.length > 1 ? grouped : null;
        body = (
            <div
                className={`space-y-6 transition-opacity duration-200 motion-reduce:transition-none ${stale ? 'pointer-events-none opacity-60' : ''}`}
                aria-busy={busy}
            >
                {error && (
                    // A failed refresh keeps the numbers already on screen.
                    <ErrorRetryState message={error.message} onRetry={onRefresh} />
                )}
                <AiSummaryCard key={requestKey} slug={slug} filters={filters} disabled={stale} />
                {shown.kpis.length > 0 && (
                    <section
                        data-tour="insights-kpis"
                        aria-label="Headline numbers"
                        className={kpiGroups ? 'grid grid-cols-1 gap-x-6 gap-y-5 xl:grid-cols-2' : undefined}
                    >
                        {kpiGroups ? (
                            // Overview: one block per dashboard the tiles open, in nav order.
                            kpiGroups.map(group => {
                                // Two groups share a row on wide screens; the lead
                                // (unlinked) group and any of 3+ tiles take the row.
                                const span = !group.slug || group.kpis.length >= 3 ? 'min-w-0 xl:col-span-2' : 'min-w-0';
                                if (!group.slug || !group.title) {
                                    return (
                                        <div key="own" className={span}>
                                            <KpiGrid count={group.kpis.length}>{group.kpis.map(renderTile)}</KpiGrid>
                                        </div>
                                    );
                                }
                                const entry = findDashboard(group.slug);
                                const Icon = entry ? DASHBOARD_ICONS[entry.icon] : null;
                                const headingId = `kpi-group-${group.slug}`;
                                return (
                                    <div key={group.slug} role="group" aria-labelledby={headingId} className={span}>
                                        <div className="mb-2 flex items-center justify-between gap-3">
                                            <h2
                                                id={headingId}
                                                className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-gray-500"
                                            >
                                                {Icon && <Icon size={14} aria-hidden="true" className="shrink-0" />}
                                                {group.title}
                                            </h2>
                                            <Link
                                                to={withFilterQuery(insightsPath(group.slug), filterQuery)}
                                                className="shrink-0 rounded text-xs font-medium text-gray-500 hover:text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                                            >
                                                Open {group.title}
                                                <span aria-hidden="true"> →</span>
                                            </Link>
                                        </div>
                                        <KpiGrid count={group.kpis.length}>{group.kpis.map(renderTile)}</KpiGrid>
                                    </div>
                                );
                            })
                        ) : (
                            <KpiGrid count={shown.kpis.length}>{shown.kpis.map(renderTile)}</KpiGrid>
                        )}
                    </section>
                )}
                {shown.sections.length > 0 && (
                    <div className="grid grid-cols-12 gap-4">
                        {shown.sections.map(s => (
                            <SectionRenderer key={s.id} section={s} ctx={sectionCtx} />
                        ))}
                    </div>
                )}
                <NotesList notes={shown.notes} />
            </div>
        );
    }

    // A refusal of the whole feature hides the filters; a refused DEPARTMENT
    // keeps them, so the reader can pick another one right there.
    const blocked =
        isNotEnabled(metaError) ||
        isForbidden(metaError) ||
        (!!error && !shown && (isNotEnabled(error) || (isForbidden(error) && filters.dept.length === 0)));
    const showChrome = known && available && !blocked;

    return (
        <MotionConfig reducedMotion="user">
            <div ref={setRoot} className="mx-auto max-w-[1400px] px-4 pb-16 pt-6 sm:px-6 lg:px-8">
                <header data-tour="insights-header" className="mb-4 flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
                    <div className="min-w-0">
                        <nav aria-label="Breadcrumb" className="mb-1.5">
                            <ol className="flex items-center gap-1 text-xs text-gray-500">
                                <li>
                                    <Link to={withFilterQuery('/insights', filterQuery)} className="rounded hover:text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30">
                                        Insights
                                    </Link>
                                </li>
                                <li aria-hidden="true">
                                    <ChevronRight size={12} />
                                </li>
                                <li aria-current="page" className="font-medium text-gray-700">
                                    {title}
                                </li>
                            </ol>
                        </nav>
                        <div className="flex items-center gap-3">
                            <span className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-md bg-brand text-white" aria-hidden="true">
                                <Icon size={20} />
                            </span>
                            <div className="min-w-0">
                                <h1 className="text-2xl font-semibold tracking-tight text-gray-900">{title}</h1>
                                {description && <p className="mt-0.5 max-w-2xl text-sm text-gray-500">{description}</p>}
                            </div>
                        </div>
                        {shown && showChrome && (
                            <div className="mt-3">
                                <ScopeChips scope={shown.scope} />
                            </div>
                        )}
                    </div>
                    {showChrome && shown && (
                        <div data-tour="insights-actions" className="flex flex-wrap items-center gap-2">
                            <FreshnessBadge
                                generatedAt={shown?.generated_at}
                                cached={shown?.cached ?? false}
                                refreshing={busy}
                                onRefresh={onRefresh}
                            />
                            <button type="button" onClick={() => setDefinitionsOpen(true)} className={HEADER_BUTTON} disabled={pageMetrics.length === 0}>
                                <BookOpenText size={14} aria-hidden="true" />
                                Definitions
                            </button>
                            <button
                                type="button"
                                onClick={exportKpis}
                                className={HEADER_BUTTON}
                                disabled={!shown || shown.kpis.length === 0}
                                title="Download the headline numbers as CSV"
                            >
                                <Download size={14} aria-hidden="true" />
                                Export
                            </button>
                            <button type="button" onClick={() => void copyLink()} className={HEADER_BUTTON} title="Copy a link to this exact view">
                                {copied ? <Check size={14} aria-hidden="true" className="text-emerald-600" /> : <Link2 size={14} aria-hidden="true" />}
                                <span aria-live="polite">{copied ? 'Copied' : 'Copy link'}</span>
                            </button>
                        </div>
                    )}
                </header>

                {showChrome && (
                    <div data-tour="insights-filters" className="sticky top-0 z-20 -mx-4 mb-5 border-b border-gray-200/80 bg-gray-50/95 px-4 py-3 backdrop-blur supports-[backdrop-filter]:bg-gray-50/80 sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8">
                        <FilterBar
                            filters={filters}
                            onChange={setFilters}
                            departments={meta?.departments ?? []}
                            comparison={comparison}
                            periodCaption={periodCaption}
                            today={today}
                        />
                    </div>
                )}

                {metaError && !blocked && !meta && (
                    <div className="mb-4">
                        <ErrorRetryState message={`The department list and definitions didn't load: ${metaError.message}`} onRetry={retryMeta} />
                    </div>
                )}

                {body}
            </div>
            <DefinitionsDrawer
                open={definitionsOpen}
                onClose={() => setDefinitionsOpen(false)}
                metrics={pageMetrics}
                pageTitle={title}
            />
        </MotionConfig>
    );
};

