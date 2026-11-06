// Insights → one person: `/insights/people/:email`.
//
// What Cubie looks like for this person: 90 days of activity, their headline
// numbers, a card per dashboard, and the features they can use versus the
// ones they do. Behind the narrower `insights_people` verdict (README §1); the
// server writes an audit row on every read, and a person outside the viewer's
// departments is a 404, never a peek.
//
// Counts only, never content: no message, email, prompt or transcript text
// reaches this page.
import React, { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { MotionConfig } from 'framer-motion';
import { ArrowLeft, ArrowUpRight, ChevronRight, CircleCheck, CircleMinus, CircleX, Clock } from 'lucide-react';
import { fetchPerson, insightsError, isForbidden, isNotEnabled, isNotFound } from '../../api/insights';
import type { InsightsError, Kpi, MetricDef, PersonProfile as PersonProfileData } from '../../api/insights';
import { ActivityCalendar } from '../../components/insights/ActivityCalendar';
import { DeltaChip } from '../../components/insights/DeltaChip';
import { FilterBar } from '../../components/insights/FilterBar';
import { KpiGrid, KpiTile } from '../../components/insights/KpiTile';
import { MetricInfo } from '../../components/insights/MetricInfo';
import { ErrorRetryState, ForbiddenState, NotEnabledState, NotFoundState, NotesList } from '../../components/insights/PageStates';
import { ProfileSkeleton } from '../../components/insights/Skeleton';
import { DASHBOARD_ICONS } from '../../components/insights/dashboardIcons';
import { findDashboard, insightsPath } from './dashboards';
import { comparisonCaption, expectedDays, filtersKey, filtersToQuery, longDate, shortDate, todayIn, withFilterQuery } from './insightsFilters';
import { denominatorText, formatValue } from './insightsFormat';
import { useInsightsFilters, useInsightsMeta } from './useInsights';

interface Loaded {
    key: string;
    nonce: number;
    data: PersonProfileData | null;
    error: InsightsError | null;
}

const initials = (name: string): string =>
    name
        .trim()
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .map(w => w[0]?.toUpperCase() ?? '')
        .join('') || '?';

export const PersonProfile: React.FC = () => {
    const { email: rawEmail = '' } = useParams<{ email: string }>();
    const email = rawEmail.trim().toLowerCase();
    const navigate = useNavigate();
    const { meta, error: metaError } = useInsightsMeta();
    const { filters, setFilters, filterQuery } = useInsightsFilters();

    // Only the date range applies to a person (README §4).
    const rangeOnly = useMemo(() => ({ ...filters, dept: [] }), [filters]);
    const requestKey = `${email}${filtersKey(rangeOnly)}`;
    const [loaded, setLoaded] = useState<Loaded | null>(null);
    const [retry, setRetry] = useState(0);

    useEffect(() => {
        if (!email) return;
        let cancelled = false;
        const nonce = retry;
        (async () => {
            try {
                const data = await fetchPerson(email, rangeOnly);
                if (!cancelled) setLoaded({ key: requestKey, nonce, data, error: null });
            } catch (e) {
                if (!cancelled) setLoaded({ key: requestKey, nonce, data: null, error: insightsError(e, { notFound: 'not_found' }) });
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [email, rangeOnly, requestKey, retry]);

    const current = loaded && loaded.key === requestKey ? loaded : null;
    const shown = loaded?.data && loaded.data.person.email.toLowerCase() === email ? loaded.data : null;
    const busy = !loaded || loaded.key !== requestKey || loaded.nonce !== retry;
    const stale = !!shown && busy;
    const error = current?.error ?? null;
    const metrics = useMemo(() => meta?.metrics ?? {}, [meta]);
    const today = useMemo(() => todayIn(meta?.tz ?? 'UTC'), [meta?.tz]);
    const period = shown?.period ?? null;
    const comparison = comparisonCaption(period?.days ?? expectedDays(filters));
    const fallbackBack = withFilterQuery('/insights', filterQuery);

    // Back goes back (keeping the dashboard's scroll and filters) when there is
    // an in-app page to go back to; a pasted link falls back to Insights.
    const goBack = () => {
        const idx = (window.history.state as { idx?: number } | null)?.idx ?? 0;
        if (idx > 0) navigate(-1);
        else navigate(fallbackBack);
    };

    let body: React.ReactNode;
    if (isNotEnabled(metaError) || isNotEnabled(error)) {
        body = <NotEnabledState />;
    } else if (isForbidden(error)) {
        body = <ForbiddenState personView message={error?.message} />;
    } else if (isNotFound(error)) {
        body = (
            <NotFoundState title="This person isn’t in your view" href={fallbackBack} hrefLabel="Back to Insights">
                <p>
                    Either they aren&apos;t in a department you can see, or they don&apos;t use Cubie. Insights shows
                    the departments you manage plus your own.
                </p>
            </NotFoundState>
        );
    } else if (error && !shown) {
        body = <ErrorRetryState message={error.message} onRetry={() => setRetry(r => r + 1)} />;
    } else if (!shown) {
        body = <ProfileSkeleton />;
    } else {
        const p = shown.person;
        const dashboardQuery = (dept: string | null) =>
            dept ? filtersToQuery({ ...filters, dept: [dept] }) : filterQuery;
        body = (
            <div className={`space-y-6 transition-opacity duration-200 motion-reduce:transition-none ${stale ? 'opacity-60' : ''}`} aria-busy={busy}>
                <div className="flex flex-col gap-4 rounded-lg border border-gray-200 bg-white p-5 shadow-sm sm:flex-row sm:items-center">
                    <span
                        className="flex h-14 w-14 flex-shrink-0 items-center justify-center rounded-full bg-brand-tint text-lg font-semibold tracking-wide text-brand"
                        aria-hidden="true"
                    >
                        {initials(p.name || p.email)}
                    </span>
                    <div className="min-w-0 flex-1">
                        <h1 className="truncate text-2xl font-semibold tracking-tight text-gray-900">{p.name || p.email}</h1>
                        <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-gray-500">
                            <span className="truncate">{p.email}</span>
                            {p.department_label && (
                                <span className="rounded-full bg-brand-tint px-2 py-0.5 text-xs font-medium text-brand">{p.department_label}</span>
                            )}
                            <span className="inline-flex items-center gap-1 text-xs">
                                <Clock size={12} aria-hidden="true" />
                                {p.last_active ? `Last active ${longDate(p.last_active)}` : 'No recorded activity yet'}
                            </span>
                        </p>
                    </div>
                </div>

                <div className="grid grid-cols-12 gap-4">
                    <section
                        data-tour="insights-person-activity"
                        aria-labelledby="activity-heading"
                        className="col-span-12 rounded-lg border border-gray-200 bg-white p-4 shadow-sm sm:px-5 xl:col-span-5"
                    >
                        <h2 id="activity-heading" className="text-[15px] font-semibold text-gray-900">
                            Activity, last 90 days
                        </h2>
                        <p className="mb-3 mt-0.5 text-xs text-gray-500">
                            Any recorded use of Cubie on a day, counted in the company&apos;s business time zone.
                            Ends {longDate(shown.period.end)}.
                        </p>
                        <ActivityCalendar days={shown.calendar} end={shown.period.end} name={p.name || p.email} />
                    </section>
                    <section aria-label="Headline numbers" className="col-span-12 xl:col-span-7">
                        {shown.kpis.length > 0 ? (
                            <KpiGrid count={Math.min(shown.kpis.length, 4)}>
                                {shown.kpis.map(k => (
                                    <KpiTile key={k.id} kpi={k} metric={metrics[k.id]} comparison={comparison} />
                                ))}
                            </KpiGrid>
                        ) : (
                            <p className="rounded-lg border border-gray-200 bg-white p-6 text-sm text-gray-500">
                                No headline numbers for this period.
                            </p>
                        )}
                    </section>
                </div>

                {shown.cards.length > 0 && (
                    <section data-tour="insights-person-dashboards" aria-label="By dashboard">
                        <h2 className="mb-3 text-[15px] font-semibold text-gray-900">By dashboard</h2>
                        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                            {shown.cards.map(card => {
                                const entry = findDashboard(card.dashboard);
                                const CardIcon = entry ? DASHBOARD_ICONS[entry.icon] : DASHBOARD_ICONS.overview;
                                return (
                                    <article key={card.dashboard} className="flex flex-col rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
                                        <header className="mb-3 flex items-center justify-between gap-2">
                                            <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900">
                                                <CardIcon size={16} aria-hidden="true" className="text-brand" />
                                                {card.title}
                                            </h3>
                                            {(entry?.live ?? false) && (
                                                <Link
                                                    to={`${insightsPath(card.dashboard)}${dashboardQuery(p.department)}`}
                                                    className="inline-flex items-center gap-0.5 rounded text-xs font-medium text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                                                    title={p.department_label ? `Open ${card.title} for ${p.department_label}` : `Open ${card.title}`}
                                                >
                                                    Open
                                                    <ArrowUpRight size={12} aria-hidden="true" />
                                                </Link>
                                            )}
                                        </header>
                                        <dl className="divide-y divide-gray-100">
                                            {card.kpis.map(k => (
                                                <CardKpi key={k.id} kpi={k} metric={metrics[k.id]} comparison={comparison} />
                                            ))}
                                        </dl>
                                    </article>
                                );
                            })}
                        </div>
                    </section>
                )}

                {shown.features.length > 0 && (
                    <section data-tour="insights-person-features" aria-labelledby="features-heading" className="rounded-lg border border-gray-200 bg-white shadow-sm">
                        <div className="px-4 pt-4 sm:px-5">
                            <h2 id="features-heading" className="text-[15px] font-semibold text-gray-900">
                                Features: access and use
                            </h2>
                            <p className="mt-0.5 text-xs text-gray-500">
                                What {p.name ? p.name.split(' ')[0] : 'they'} can open, and whether it was used in this period.
                            </p>
                        </div>
                        <div className="overflow-x-auto px-4 pb-4 pt-3 sm:px-5">
                            <table className="min-w-full text-sm">
                                <caption className="sr-only">Features this person can use, and which they used</caption>
                                <thead>
                                    <tr className="border-b border-gray-100 text-left text-xs text-gray-500">
                                        <th scope="col" className="py-2 pr-4 font-semibold">Feature</th>
                                        <th scope="col" className="py-2 pr-4 font-semibold">Access</th>
                                        <th scope="col" className="py-2 font-semibold">Used in this period</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-gray-50">
                                    {shown.features.map(f => (
                                        <tr key={f.key}>
                                            <th scope="row" className="whitespace-nowrap py-2 pr-4 text-left font-normal text-gray-800">{f.label}</th>
                                            <td className="whitespace-nowrap py-2 pr-4">
                                                {f.has_access ? (
                                                    <span className="inline-flex items-center gap-1.5 text-gray-700">
                                                        <CircleCheck size={15} aria-hidden="true" className="text-emerald-600" />
                                                        Has access
                                                    </span>
                                                ) : (
                                                    <span className="inline-flex items-center gap-1.5 text-gray-400">
                                                        <CircleMinus size={15} aria-hidden="true" />
                                                        No access
                                                    </span>
                                                )}
                                            </td>
                                            <td className="whitespace-nowrap py-2">
                                                {!f.has_access && !f.used ? (
                                                    <span className="text-gray-300">—</span>
                                                ) : f.used ? (
                                                    <span className="inline-flex items-center gap-1.5 text-gray-700">
                                                        <CircleCheck size={15} aria-hidden="true" className="text-emerald-600" />
                                                        Used
                                                    </span>
                                                ) : (
                                                    <span className="inline-flex items-center gap-1.5 text-amber-700">
                                                        <CircleX size={15} aria-hidden="true" />
                                                        Not used yet
                                                    </span>
                                                )}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>
                )}

                <NotesList notes={shown.notes} />
            </div>
        );
    }

    const name = shown?.person.name || email;

    return (
        <MotionConfig reducedMotion="user">
            <div className="mx-auto max-w-[1400px] px-4 pb-16 pt-6 sm:px-6 lg:px-8">
                <div data-tour="insights-person-header" className="mb-4 flex flex-wrap items-center justify-between gap-3">
                    <nav aria-label="Breadcrumb" className="min-w-0">
                        <ol className="flex flex-wrap items-center gap-1 text-xs text-gray-500">
                            <li>
                                <button
                                    type="button"
                                    onClick={goBack}
                                    className="mr-2 inline-flex items-center gap-1 rounded-md border border-gray-200 bg-white px-2 py-1 font-medium text-gray-600 shadow-sm hover:bg-gray-50 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                                >
                                    <ArrowLeft size={13} aria-hidden="true" />
                                    Back
                                </button>
                            </li>
                            <li>
                                <Link to={fallbackBack} className="rounded hover:text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30">
                                    Insights
                                </Link>
                            </li>
                            <li aria-hidden="true">
                                <ChevronRight size={12} />
                            </li>
                            <li>People</li>
                            <li aria-hidden="true">
                                <ChevronRight size={12} />
                            </li>
                            <li aria-current="page" className="max-w-[16rem] truncate font-medium text-gray-700">
                                {name}
                            </li>
                        </ol>
                    </nav>
                </div>

                {!error && (
                    <div data-tour="insights-person-range" className="sticky top-0 z-20 -mx-4 mb-5 border-b border-gray-200/80 bg-gray-50/95 px-4 py-3 backdrop-blur supports-[backdrop-filter]:bg-gray-50/80 sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8">
                        <FilterBar
                            filters={filters}
                            onChange={setFilters}
                            departments={[]}
                            showDepartments={false}
                            comparison={comparison}
                            periodCaption={period ? `${shortDate(period.start)} – ${longDate(period.end)}` : null}
                            today={today}
                        />
                    </div>
                )}

                {body}
            </div>
        </MotionConfig>
    );
};

/** One number inside a per-dashboard card: label + ⓘ, value, change, "x of y". */
const CardKpi: React.FC<{ kpi: Kpi; metric: MetricDef | undefined; comparison: string }> = ({ kpi, metric, comparison }) => {
    const unit = metric?.unit ?? 'count';
    const of = denominatorText(kpi.numerator, kpi.denominator, unit, metric?.part_unit);
    const label = metric?.label ?? kpi.id;
    return (
        <div className="flex items-center justify-between gap-3 py-2">
            <dt className="flex min-w-0 items-center gap-1 text-[13px] text-gray-600">
                <span className="truncate">{label}</span>
                <MetricInfo metric={metric} label={label} align="left" />
            </dt>
            <dd className="flex flex-shrink-0 flex-col items-end">
                <span className="flex items-center gap-1.5">
                    <span className="text-sm font-semibold text-gray-900 tabular-nums">{formatValue(kpi.value, unit)}</span>
                    <DeltaChip value={kpi.value} prev={kpi.prev_value} unit={unit} good={metric?.good_direction} comparison={comparison} />
                </span>
                {of && <span className="text-[11px] text-gray-500 tabular-nums">{of}</span>}
            </dd>
        </div>
    );
};
