/**
 * Cubie Sales Cockpit — exec summary (the sales director).
 *
 * Three numbers and a trend. Open commitments past SLA (count and dollar
 * value), median elapsed client-request → SOW-sent trending, and capture
 * coverage — the share of AE calls with a transcript.
 *
 * ── THE CONSTRAINT THAT SHAPES THIS FILE ────────────────────────────────────
 * Google's Meet API documentation states it "isn't intended for performance
 * tracking or user evaluation within your domain," and these numbers derive in
 * part from Meet data (capture, transcripts, wrap-ups). So this page shows
 * "three commitments past SLA — here they are", never "this AE is 4th of 5 on
 * capture rate". Concretely:
 *   - NO ranked leaderboard, no sort-by-worst-performer default, and no
 *     per-person ordering of any kind. Nothing on this page is keyed to a
 *     person at all.
 *   - THIS PAGE BEING THIN IS THE FEATURE. Keeping the exec view thin is what
 *     stops the cockpit becoming the leaderboard those terms warn against. Do
 *     not add a per-person drill-down here even though the data would support
 *     one — if the exec needs to click into a person, the manager view exists, and
 *     there the unit is the commitment rather than the AE.
 *   - The trend bars are one colour. Direction-of-good differs per metric
 *     (a falling median is good, a falling coverage is not), so painting bars
 *     green and red would be a guess dressed as a judgement.
 *
 * ── Other load-bearing choices ──────────────────────────────────────────────
 * - Aggregate reporting is EXEMPT from Rule 3 (one writing action per row):
 *   a roll-up number has no single action and must not pretend to. No
 *   ActionRow on this page, by design.
 * - `tiles[].value` and `trend[].period` are server-formatted and printed
 *   verbatim. Elapsed time is business-hours arithmetic in one module
 *   (`rubik_core/cockpit/business_hours.py`); re-deriving or re-labelling it
 *   in the browser is how the Friday-afternoon false positive gets back in.
 * - A 404 is the feature flag being off (the flags ship OFF): a calm empty
 *   state, never a red error.
 * - "You have no org record" and "the org is quiet" are different problems and
 *   only the first is fixable, so they get different copy — a user with no
 *   `eos_people` row resolves to an empty scope that silently revokes
 *   access, and silence there is the bug.
 */
import React, { useEffect, useState } from 'react';
import { LineChart, RefreshCw, TrendingUp } from 'lucide-react';

import {
    cockpitError,
    fetchExecSummary,
    isForbidden,
    isNotEnabled,
    type CockpitError,
    type CockpitTile,
    type ExecSummary as ExecSummaryData,
    type ExecTrendPoint,
} from '../../api/cockpit';
import {
    Donut,
    EmptyState,
    EosPageHeader,
    ErrorState,
    KpiCard,
    KpiStrip,
    LoadingState,
    PageContainer,
} from '../eos/_shared';
import { SecondaryButton } from '../../components/smartTeams/shared';
// `Donut` takes a CSS colour, not a Tailwind class: it reads the brand
// variable at render, so a workspace accent applies.
import { getBrandColor } from '../../brand/colors';

// ─── local helpers (not exported: react-refresh/only-export-components) ──────

const CARD = 'bg-white rounded-lg border border-gray-200 p-6 shadow-sm';

const FALLBACK = 'The executive summary could not be loaded.';

/** ASSUMED: the contract pins no machine code for "this viewer has no
 *  `eos_people` row", only that the case exists and must be handled loudly
 *  rather than defaulting open. Any of these codes is read as that case; an
 *  unrecognised one degrades to the ordinary error path, which is safe. */
const NO_ORG_RECORD_CODES: readonly string[] = [
    'no_org_record',
    'no_eos_person',
    'no_person_record',
    'missing_person',
    'no_team_scope',
];

const isMissingOrgRecord = (err: CockpitError): boolean =>
    err.code !== null && NO_ORG_RECORD_CODES.includes(err.code);

/** A tile value is pre-formatted by the server. When it is a percentage —
 *  capture coverage is the one that will be — we can pair it with a ring. The
 *  string itself still prints verbatim; the ring is decoration, never the
 *  number. */
const PERCENT_RE = /^\s*(\d+(?:\.\d+)?)\s*%\s*$/;
const percentOf = (value: string | number): number | null => {
    if (typeof value !== 'string') return null;
    const m = PERCENT_RE.exec(value);
    return m ? Number(m[1]) : null;
};

/** KpiStrip only knows 2/3/4 columns (full literal class strings — an
 *  interpolated `md:grid-cols-${n}` would be purged by the JIT). */
const stripCols = (n: number): 2 | 3 | 4 => (n <= 2 ? 2 : n === 3 ? 3 : 4);

// ─── the trend ───────────────────────────────────────────────────────────────

/** Hand-rolled bars rather than a chart library: eight points with their
 *  numbers printed is more useful to a reader than an axis, and the value is
 *  always legible in a screenshot pasted into Slack.
 *
 *  `period` is whatever bucket label the server chose ("Jul", "2026-W34") and
 *  is printed as given — parsing it here would invent a calendar the backend
 *  did not promise. The unit is deliberately unlabelled for the same reason:
 *  the series carries bare numbers, and captioning them "days" would be the
 *  frontend asserting something only the API knows. */
const TrendBars: React.FC<{ points: ExecTrendPoint[] }> = ({ points }) => {
    if (points.length === 0) {
        return (
            <p className={`${CARD} text-sm italic text-gray-500`}>
                Not enough history yet to draw a trend.
            </p>
        );
    }

    const max = points.reduce((m, p) => (p.value > m ? p.value : m), 0);
    const scale = max > 0 ? max : 1;
    const latest = points[points.length - 1];

    return (
        <div className={CARD}>
            <div className="flex items-end gap-3 overflow-x-auto pb-1">
                {points.map((p, i) => {
                    const height = Math.max(2, (Math.max(0, p.value) / scale) * 100);
                    const isLatest = i === points.length - 1;
                    return (
                        <div
                            key={`${p.period}|${i}`}
                            className="flex min-w-[3rem] flex-1 flex-col items-center gap-2"
                        >
                            <span className="text-xs font-semibold tabular-nums text-gray-700">
                                {p.value}
                            </span>
                            <div className="flex h-32 w-full items-end">
                                <div
                                    // One colour for every bar; see the constraint note
                                    // at the top of this file. The latest bar is only
                                    // distinguished by opacity, which carries no verdict.
                                    className={`w-full rounded-t-md ${isLatest ? 'bg-brand' : 'bg-brand/40'}`}
                                    style={{ height: `${height}%` }}
                                    role="img"
                                    aria-label={`${p.period}: ${p.value}`}
                                />
                            </div>
                            <span className="text-[11px] text-gray-500">{p.period}</span>
                        </div>
                    );
                })}
            </div>
            {latest && (
                <p className="mt-4 border-t border-gray-100 pt-3 text-xs text-gray-500">
                    Most recent bucket: <span className="font-medium text-gray-700">{latest.period}</span>{' '}
                    at <span className="font-medium tabular-nums text-gray-700">{latest.value}</span>.
                    Business-hours elapsed time, computed once on the server.
                </p>
            )}
        </div>
    );
};

// ─── the three numbers ───────────────────────────────────────────────────────

const HeadlineTiles: React.FC<{ tiles: CockpitTile[] }> = ({ tiles }) => {
    if (tiles.length === 0) {
        return (
            <p className={`${CARD} text-sm italic text-gray-500`}>
                No headline figures were returned for the organisation.
            </p>
        );
    }
    return (
        <KpiStrip cols={stripCols(tiles.length)}>
            {tiles.map(t => {
                const pct = percentOf(t.value);
                return (
                    <KpiCard
                        key={t.label}
                        label={t.label}
                        value={<span className="tabular-nums">{t.value}</span>}
                        hint={t.hint ?? undefined}
                        leading={
                            pct === null ? undefined : (
                                <Donut value={pct} color={getBrandColor('brand')} size={42} strokeWidth={6} />
                            )
                        }
                    />
                );
            })}
        </KpiStrip>
    );
};

// ─── page ────────────────────────────────────────────────────────────────────

export const ExecSummary: React.FC = () => {
    const [data, setData] = useState<ExecSummaryData | null>(null);
    const [err, setErr] = useState<CockpitError | null>(null);
    const [refreshing, setRefreshing] = useState(false);

    // Derived, never stored. A `loading` boolean set inside an effect is what
    // trips react-hooks/set-state-in-effect.
    const isFirstLoad = data === null && err === null;

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const d = await fetchExecSummary();
                if (!cancelled) {
                    setData(d);
                    setErr(null);
                }
            } catch (e) {
                if (!cancelled) setErr(cockpitError(e, FALLBACK));
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    const refresh = async () => {
        setRefreshing(true);
        try {
            const d = await fetchExecSummary();
            setData(d);
            setErr(null);
        } catch (e) {
            const ce = cockpitError(e, FALLBACK);
            setErr(ce);
            // A flag-off or access answer replaces the page. A transient failure
            // keeps the numbers already on screen and shows the banner above
            // them.
            if (isNotEnabled(ce) || isForbidden(ce) || isMissingOrgRecord(ce)) setData(null);
        } finally {
            setRefreshing(false);
        }
    };

    const header = (
        <EosPageHeader
            icon={<LineChart size={20} />}
            title="Sales health"
            subtitle="Three numbers and a trend for the whole organisation"
            right={
                <SecondaryButton onClick={() => void refresh()} busy={refreshing}>
                    {!refreshing && <RefreshCw size={15} />}
                    Refresh
                </SecondaryButton>
            }
        />
    );

    // ── the three states that are not errors ─────────────────────────────────
    if (err && isNotEnabled(err)) {
        return (
            <PageContainer>
                {header}
                <EmptyState
                    title="Not enabled yet"
                    hint="The sales cockpit is switched off in this environment. Nothing is wrong with your account and there is nothing to retry — these figures appear once the feature is turned on."
                />
            </PageContainer>
        );
    }

    if (err && isMissingOrgRecord(err)) {
        return (
            <PageContainer>
                {header}
                <EmptyState
                    title="Your org record is missing, so nothing can be scoped to you"
                    hint={`${err.message} This is not the same as a quiet week — the figures are scoped through your EOS person and seat, and one of those rows does not exist yet. Ask an admin to add you to the EOS org chart and this page fills in.`}
                />
            </PageContainer>
        );
    }

    if (err && isForbidden(err)) {
        return (
            <PageContainer>
                {header}
                <EmptyState
                    title="This view is for the executive team"
                    hint={`${err.message} The server decides who sees the organisation-wide figures. For a team-level picture, the sales manager roll-up is the view to ask for.`}
                />
            </PageContainer>
        );
    }

    if (isFirstLoad) {
        return (
            <PageContainer>
                {header}
                <LoadingState label="Loading sales health…" />
            </PageContainer>
        );
    }

    if (!data) {
        return (
            <PageContainer>
                {header}
                <ErrorState message={err?.message ?? FALLBACK} />
            </PageContainer>
        );
    }

    // Defensive reads: the routers are still in flight and there is no error
    // boundary anywhere in this app, so a missing key must not white-screen.
    const tiles = data.tiles ?? [];
    const trend = data.trend ?? [];

    if (tiles.length === 0 && trend.length === 0) {
        return (
            <PageContainer>
                {header}
                {err && (
                    <div className="mb-6">
                        <ErrorState message={err.message} />
                    </div>
                )}
                <EmptyState
                    title="Nothing to report yet"
                    hint="No commitments are past SLA and there is not enough captured history to trend. This is a real answer, not a missing one."
                />
            </PageContainer>
        );
    }

    return (
        <PageContainer>
            {header}

            {/* A refresh that failed while the previous numbers are still good. */}
            {err && (
                <div className="mb-6">
                    <ErrorState message={err.message} />
                </div>
            )}

            <div className="space-y-8">
                <section data-tour="sales-exec-figures">
                    <HeadlineTiles tiles={tiles} />
                </section>

                <section data-tour="sales-exec-trend">
                    <div className="mb-3 flex items-center gap-2">
                        <TrendingUp size={14} className="text-gray-400" />
                        <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-500">
                            Trend
                        </h2>
                    </div>
                    <TrendBars points={trend} />
                </section>

                {/* Says out loud why there is no name anywhere on this page. */}
                <p className="text-xs leading-relaxed text-gray-400">
                    Organisation-wide only, and intentionally so. These figures derive in part from
                    Google Meet data, which is not intended for performance tracking or evaluating
                    people — so there is no breakdown by person here. For the rows behind a number,
                    the sales manager roll-up lists them by age and by dollar value.
                </p>
            </div>
        </PageContainer>
    );
};

export default ExecSummary;
