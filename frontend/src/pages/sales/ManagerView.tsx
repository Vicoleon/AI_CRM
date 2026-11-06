/**
 * Cubie Sales Cockpit — manager view (the sales manager).
 *
 * This is the view that replaces the sales director being the single point of
 * detection: the team roll-up of commitments by state, the breaches WITH THE
 * REASON, wrap-up completion as a process metric, and the dollar value sitting
 * behind deals whose stage and evidence disagree.
 *
 * ── THE CONSTRAINT THAT SHAPES THIS FILE ────────────────────────────────────
 * Google's Meet API documentation states it "isn't intended for performance
 * tracking or user evaluation within your domain," and these numbers derive in
 * part from Meet data (capture, transcripts, wrap-ups). So this page shows
 * "three commitments past SLA — here they are", never "this AE is 4th of 5 on
 * capture rate". Concretely, and deliberately:
 *   - NO ranked leaderboard and no sort-by-worst-performer default. The breach
 *     table keeps the server's order (age) and the wrap-up rates are sorted
 *     A–Z by name — an order that carries no performance meaning at all.
 *   - Every wrap-up donut is the same brand colour. Colouring a person's ring
 *     red or green is a score, and a score is the thing the terms warn about.
 *   - The exec view stays thin for the same reason; if the exec needs a
 *     person, they come here, and here the unit is the commitment, not the AE.
 *
 * ── Other load-bearing choices ──────────────────────────────────────────────
 * - The stage-lag exception count by AE arrives as its own
 *   `stage_lag_counts` array (see `StageLagCounts`), not as one of the generic
 *   `tiles`. `RollUp` renders whatever tiles the server sends instead of
 *   hard-coding a known set of labels.
 * - Aggregate reporting is EXEMPT from Rule 3 (one writing action per row):
 *   a roll-up number has no single action and must not pretend to. No
 *   ActionRow on this page, by design.
 * - `age_label` is server-rendered business-hours arithmetic
 *   (`rubik_core/cockpit/business_hours.py`). Printed verbatim — recomputing
 *   "6 days" in the browser with Date.now() produces the Friday-afternoon
 *   false positive that kills trust in week one.
 * - A 404 is the feature flag being off (the flags ship OFF), which is a calm
 *   empty state and never a red error.
 * - "Your team is empty" and "your org record is missing" are different
 *   problems and only the second is fixable, so they get different copy: a
 *   user with no `eos_people` row resolves to an empty scope that silently
 *   revokes access, and silence there is the bug.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
    AlertTriangle,
    ArrowDownCircle,
    CalendarClock,
    ClipboardCheck,
    ExternalLink,
    RefreshCw,
    ScrollText,
    Users,
} from 'lucide-react';

import {
    cockpitError,
    commitmentKindLabel,
    fetchManagerView,
    isForbidden,
    isNotEnabled,
    isOrgRecordMissing,
    isTeamUnresolved,
    type CockpitError,
    type CockpitTile,
    type ForecastIntegrity,
    type ManagerBreach,
    type ManagerDeferredDeal,
    type ManagerView as ManagerViewData,
    type StageLagCount,
    type WrapupRate,
} from '../../api/cockpit';
import {
    Donut,
    EmptyState,
    EosPageHeader,
    ErrorState,
    KpiCard,
    KpiStrip,
    LoadingState,
    OwnerChip,
    PageContainer,
    StatusBadge,
} from '../eos/_shared';
import { SecondaryButton } from '../../components/smartTeams/shared';
// `Donut` takes a CSS colour, not a Tailwind class: it reads the brand
// variable at render, so a workspace accent applies.
import { getBrandColor } from '../../brand/colors';

// ─── local helpers (not exported: react-refresh/only-export-components) ──────

const CARD = 'bg-white rounded-lg border border-gray-200 p-6 shadow-sm';

const FALLBACK = 'The team roll-up could not be loaded.';

// The codes for "the Accountability Chart cannot say who your team is" are
// `org_record_missing` and `team_unresolved`, raised as a 409 by
// `backend/routers/eos/_authz.py::require_team_scope`. They live in
// `api/cockpit.ts` (TEAM_SCOPE_CODES) because the supervisor worklist branches
// on the same two.
//
// ⚠ This file previously guessed five OTHER names (`no_org_record`,
// `no_eos_person`, `no_person_record`, `missing_person`, `no_team_scope`) and
// matched none of them — so a manager with no `eos_people` row got a red
// "could not be loaded" error instead of the fixable data problem it is. A
// missing team scope must be loud, never a quiet empty list: never guess a
// wire string, read the router.
const isMissingOrgRecord = isTeamUnresolved;

const usd = (n: number): string =>
    n.toLocaleString('en-US', {
        style: 'currency',
        currency: 'USD',
        maximumFractionDigits: 0,
    });

/** A tile value is pre-formatted by the server. When it is a percentage we can
 *  pair it with a ring; anything else renders as text only. The string itself
 *  is still printed verbatim — the ring is decoration, never the number. */
const PERCENT_RE = /^\s*(\d+(?:\.\d+)?)\s*%\s*$/;
const percentOf = (value: string | number): number | null => {
    if (typeof value !== 'string') return null;
    const m = PERCENT_RE.exec(value);
    return m ? Number(m[1]) : null;
};

/** KpiStrip only knows 2/3/4 columns (full literal class strings — an
 *  interpolated `md:grid-cols-${n}` would be purged by the JIT). */
const stripCols = (n: number): 2 | 3 | 4 => (n <= 2 ? 2 : n === 3 ? 3 : 4);

const SectionHeading: React.FC<{
    icon: React.ReactNode;
    title: string;
    note?: string;
    right?: React.ReactNode;
}> = ({ icon, title, note, right }) => (
    <div className="mb-3 flex items-end justify-between gap-4">
        <div>
            <div className="flex items-center gap-2">
                {icon}
                <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-500">
                    {title}
                </h2>
            </div>
            {note && <p className="mt-1 text-xs text-gray-400">{note}</p>}
        </div>
        {right}
    </div>
);

// ─── roll-up by state ────────────────────────────────────────────────────────

/** The server ships the state roll-up as pre-labelled tiles, so this renders
 *  whatever it sends rather than hard-coding the ladder — thresholds are config
 *  rows, not a deploy, and a hard-coded set of five labels would
 *  go stale the first time someone edits the table. */
const RollUp: React.FC<{ tiles: CockpitTile[]; onDrill: () => void; canDrill: boolean }> = ({
    tiles,
    onDrill,
    canDrill,
}) => {
    if (tiles.length === 0) {
        return (
            <p className="text-sm italic text-gray-500">
                The roll-up has no counts to show for your team yet.
            </p>
        );
    }
    return (
        <>
            <KpiStrip cols={stripCols(tiles.length)}>
                {tiles.map(t => {
                    const pct = percentOf(t.value);
                    return (
                        <KpiCard
                            key={t.label}
                            label={t.label}
                            value={t.value}
                            hint={t.hint ?? undefined}
                            leading={
                                pct === null ? undefined : (
                                    <Donut value={pct} color={getBrandColor('brand')} size={40} strokeWidth={6} />
                                )
                            }
                        />
                    );
                })}
            </KpiStrip>
            {canDrill && (
                <button
                    type="button"
                    onClick={onDrill}
                    className="-mt-2 inline-flex items-center gap-1.5 rounded text-sm font-medium text-brand transition-colors hover:text-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2"
                >
                    <ArrowDownCircle size={15} />
                    Drill to the rows behind these counts
                </button>
            )}
        </>
    );
};

// ─── forecast integrity ──────────────────────────────────────────────────────

/** Deals whose stage and evidence disagree, and the money behind the
 *  disagreement. The dollar figure is the reason the exec cares, so it is the
 *  larger of the two numbers and it is never hidden behind a hover. */
const ForecastIntegrityPanel: React.FC<{ data: ForecastIntegrity }> = ({ data }) => (
    <section data-tour="sales-team-rollup-forecast">
        <SectionHeading
            icon={<ScrollText size={14} className="text-gray-400" />}
            title="Forecast integrity"
            note="Deals where the stage in HubSpot and the evidence from the call disagree."
        />
        <KpiStrip cols={2}>
            <KpiCard
                label="Deals in disagreement"
                value={<span className="tabular-nums">{data.count}</span>}
                hint="Stage says one thing, the transcript evidence says another."
            />
            {/* `dollar_value_available` is false when HubSpot amounts could not
                be read, and then `dollar_value` is 0 because we could not look —
                NOT because nothing is at stake. A confident $0 there is worse
                than an em dash. */}
            <KpiCard
                label="Dollar value behind it"
                value={
                    data.dollar_value_available ? (
                        <span className="tabular-nums">{usd(data.dollar_value)}</span>
                    ) : (
                        <span className="text-gray-400">—</span>
                    )
                }
                hint={
                    data.dollar_value_available
                        ? 'Total amount on the deals whose stage the evidence does not support.'
                        : 'HubSpot could not be read, so the amount is unknown rather than zero.'
                }
                valueClass={
                    data.dollar_value_available && data.dollar_value > 0 ? 'text-brand' : undefined
                }
            />
        </KpiStrip>
    </section>
);

// ─── stage-lag exceptions by AE ──────────────────────────────────────────────

/** The stage-lag exception count by AE. The router sends it as its own
 *  `stage_lag_counts` array — NOT folded into the generic
 *  `tiles`, which is what this file previously assumed, so the number was
 *  dropped entirely.
 *
 *  Server order is preserved: `cockpit.py` sorts these ALPHABETICALLY by name,
 *  deliberately, so the section cannot read as a ranking. Do not reach for
 *  `sorted(..., reverse=True)` because it "reads better" — that is exactly the
 *  leaderboard the Meet API terms warn against. */
const StageLagCounts: React.FC<{ rows: StageLagCount[] }> = ({ rows }) => {
    if (rows.length === 0) {
        return (
            <p className={`${CARD} text-sm italic text-gray-500`}>
                No open stage-lag exceptions are attributable to anyone on your team.
            </p>
        );
    }
    return (
        <KpiStrip cols={stripCols(rows.length)}>
            {rows.map(r => (
                <KpiCard
                    key={r.owner_name}
                    label={r.owner_name}
                    value={<span className="tabular-nums">{r.count}</span>}
                    hint="Open exceptions where the HubSpot stage and the call evidence disagree on a deal this person is working."
                />
            ))}
        </KpiStrip>
    );
};

// ─── deferred deals ──────────────────────────────────────────────────────────

/** Deals a rep deferred: off THEIR lists until the follow-up date, never off
 *  this one. Each row carries the owner, the date and the rep's note, so a
 *  defer cannot be used to hide a bad deal. Server order (follow-up date,
 *  soonest first) is preserved — never by person. */
const DeferredDeals: React.FC<{ rows: ManagerDeferredDeal[] }> = ({ rows }) => {
    if (rows.length === 0) {
        return (
            <p className={`${CARD} text-sm italic text-gray-500`}>
                Nobody on your team has a deal deferred right now.
            </p>
        );
    }
    return (
        <div className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm">
            <header className="flex items-center gap-2 border-b border-gray-100 px-5 py-3">
                <CalendarClock size={16} className="text-brand" />
                <h3 className="text-sm font-semibold text-gray-900">
                    Deferred — {rows.length} {rows.length === 1 ? 'deal' : 'deals'}
                </h3>
            </header>
            <ul className="divide-y divide-gray-100">
                {rows.map(r => (
                    <li
                        key={`${r.deal_name}:${r.follow_up_on}:${r.owner_name}`}
                        className="flex flex-col gap-1 px-5 py-3 sm:flex-row sm:items-start sm:justify-between"
                    >
                        <div className="min-w-0 flex-1">
                            <p className="text-sm font-semibold text-gray-900">{r.deal_name}</p>
                            <p className="mt-1 text-sm text-gray-700">{r.note}</p>
                        </div>
                        <div className="flex flex-shrink-0 flex-wrap items-center gap-3 text-sm text-gray-600 sm:justify-end">
                            <OwnerChip name={r.owner_name} />
                            <span className="tabular-nums">Back on {r.follow_up_on}</span>
                            <span className="text-xs text-gray-400">deferred {r.deferred_on}</span>
                            {r.hubspot_url && (
                                <a
                                    href={r.hubspot_url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="inline-flex items-center gap-1 text-gray-500 hover:text-gray-800"
                                    aria-label={`Open ${r.deal_name} in HubSpot`}
                                >
                                    <ExternalLink size={14} aria-hidden="true" />
                                </a>
                            )}
                        </div>
                    </li>
                ))}
            </ul>
        </div>
    );
};

// ─── breaches, with the reason ───────────────────────────────────────────────

/** A count tells a manager nothing they can act on, so every row carries the
 *  reason. Server order is preserved — it is age order, and age is the
 *  only ordering that is not a ranking of people. */
const BreachTable: React.FC<{ rows: ManagerBreach[] }> = ({ rows }) => {
    const [kind, setKind] = useState<string>('all');

    // Kinds actually present, so the filter never offers an empty result.
    const kinds = useMemo(() => {
        const seen: string[] = [];
        rows.forEach(r => {
            if (r.kind && !seen.includes(r.kind)) seen.push(r.kind);
        });
        return seen;
    }, [rows]);

    const shown = useMemo(
        () => (kind === 'all' ? rows : rows.filter(r => r.kind === kind)),
        [rows, kind],
    );

    if (rows.length === 0) {
        return (
            <p className={`${CARD} text-sm italic text-gray-500`}>
                Nothing on your team is past its SLA right now.
            </p>
        );
    }

    return (
        <div className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm">
            <header className="flex items-center justify-between gap-4 border-b border-gray-100 px-5 py-3">
                <div className="flex items-center gap-2">
                    <AlertTriangle size={16} className="text-brand" />
                    <h3 className="text-sm font-semibold text-gray-900">
                        Past SLA — {shown.length} of {rows.length}
                    </h3>
                </div>
                {kinds.length > 1 && (
                    <select
                        value={kind}
                        onChange={e => setKind(e.target.value)}
                        aria-label="Filter breaches by commitment type"
                        title="Filter by commitment type"
                        className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand/30"
                    >
                        <option value="all">Every commitment type</option>
                        {kinds.map(k => (
                            <option key={k} value={k}>
                                {commitmentKindLabel(k)}
                            </option>
                        ))}
                    </select>
                )}
            </header>
            <div className="overflow-x-auto">
                <table className="w-full text-sm">
                    <thead className="bg-gray-50 text-[10px] uppercase tracking-wider text-gray-500">
                        <tr>
                            <th className="px-5 py-2 text-left font-semibold">Owner</th>
                            <th className="px-3 py-2 text-left font-semibold">Company</th>
                            <th className="px-3 py-2 text-left font-semibold">Type</th>
                            <th className="px-3 py-2 text-left font-semibold">Age</th>
                            <th className="px-3 py-2 text-left font-semibold">Reason</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                        {shown.map((r, i) => (
                            <tr
                                // No stable id on a breach row in the contract; the
                                // tuple plus the index is unique for one render and the
                                // list is read-only, so nothing depends on identity.
                                key={`${r.owner_name}|${r.company ?? ''}|${r.kind}|${i}`}
                                className="align-top"
                            >
                                <td className="px-5 py-3 whitespace-nowrap">
                                    <OwnerChip name={r.owner_name} />
                                </td>
                                <td className="px-3 py-3 font-medium text-gray-900">
                                    {r.company ?? <span className="text-gray-400">—</span>}
                                </td>
                                <td className="px-3 py-3 whitespace-nowrap text-gray-600">
                                    {commitmentKindLabel(r.kind)}
                                </td>
                                <td className="px-3 py-3 whitespace-nowrap">
                                    {/* Colour is never the only signal: the server's
                                        business-hours label prints inside the chip. */}
                                    <StatusBadge value={r.age_label} tone="red" />
                                </td>
                                <td className="px-3 py-3 text-gray-700">
                                    {r.reason ? (
                                        r.reason
                                    ) : (
                                        // A blank cell here reads as "no problem", which
                                        // is the opposite of true.
                                        <span className="italic text-gray-400">
                                            No reason recorded
                                        </span>
                                    )}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </div>
    );
};

// ─── wrap-up completion, a process metric ────────────────────────────────────

/** Sorted A–Z by name, on purpose. Sorting by rate would produce exactly the
 *  league table Google's Meet terms warn against, and the useful question here
 *  is "is the habit forming across the team", not "who is worst". */
const WrapupRates: React.FC<{ rows: WrapupRate[] }> = ({ rows }) => {
    const sorted = useMemo(
        () => [...rows].sort((a, b) => a.owner_name.localeCompare(b.owner_name)),
        [rows],
    );

    if (sorted.length === 0) {
        return (
            <p className={`${CARD} text-sm italic text-gray-500`}>
                No calls have been captured for your team yet, so there is no completion rate to
                report.
            </p>
        );
    }

    return (
        <KpiStrip cols={stripCols(sorted.length)}>
            {sorted.map(r => {
                const pct = Math.round(Math.max(0, Math.min(1, r.rate)) * 100);
                return (
                    <KpiCard
                        key={r.owner_name}
                        label={r.owner_name}
                        value={<span className="tabular-nums">{pct}%</span>}
                        hint="Share of this person's captured calls that were wrapped up. A process measure, not a performance score."
                        leading={
                            // Every ring is the same colour deliberately — see the
                            // constraint note at the top of this file.
                            <Donut value={pct} color={getBrandColor('brand')} size={42} strokeWidth={6} />
                        }
                    />
                );
            })}
        </KpiStrip>
    );
};

// ─── page ────────────────────────────────────────────────────────────────────

export const ManagerView: React.FC = () => {
    const [data, setData] = useState<ManagerViewData | null>(null);
    const [err, setErr] = useState<CockpitError | null>(null);
    const [refreshing, setRefreshing] = useState(false);
    // Typed HTMLElement, not HTMLDivElement — the target is a <section>.
    const breachRef = useRef<HTMLElement | null>(null);

    // Derived, never stored. A `loading` boolean set inside an effect is what
    // trips react-hooks/set-state-in-effect.
    const isFirstLoad = data === null && err === null;

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const d = await fetchManagerView();
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
            const d = await fetchManagerView();
            setData(d);
            setErr(null);
        } catch (e) {
            const ce = cockpitError(e, FALLBACK);
            setErr(ce);
            // A flag-off or access answer replaces the page. A transient failure
            // keeps the numbers already on screen and shows the banner above
            // them — blanking a working roll-up over one dropped request is
            // worse than showing it with a warning.
            if (isNotEnabled(ce) || isForbidden(ce) || isMissingOrgRecord(ce)) setData(null);
        } finally {
            setRefreshing(false);
        }
    };

    const header = (
        <EosPageHeader
            icon={<Users size={20} />}
            title="Sales team roll-up"
            subtitle="Commitments by state, breaches with their reason, and the money behind a disagreeing forecast"
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
                    hint="The sales cockpit is switched off in this environment. Nothing is wrong with your account and there is nothing to retry — it appears here once the feature is turned on."
                />
            </PageContainer>
        );
    }

    if (err && isMissingOrgRecord(err)) {
        return (
            <PageContainer>
                {header}
                <EmptyState
                    title={
                        isOrgRecordMissing(err)
                            ? 'Your org record is missing, so there is no team to roll up'
                            : 'Nobody resolves onto your team yet, so there is nothing to roll up'
                    }
                    hint={`${err.message} This is not the same as having an empty team — the roll-up resolves your team through your EOS person and seat, and one of those rows does not exist yet. Ask an admin to fill in the EOS org chart and this page fills in.`}
                />
            </PageContainer>
        );
    }

    if (err && isForbidden(err)) {
        return (
            <PageContainer>
                {header}
                <EmptyState
                    title="This view is for sales managers"
                    hint={`${err.message} The server decides who sees the team roll-up; if you manage a sales team and are seeing this, ask an admin to check your manager assignment.`}
                />
            </PageContainer>
        );
    }

    if (isFirstLoad) {
        return (
            <PageContainer>
                {header}
                <LoadingState label="Loading the team roll-up…" />
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
    const breaches = data.breaches ?? [];
    const rates = data.wrapup_rates ?? [];
    const lagCounts = data.stage_lag_counts ?? [];
    const deferred = data.deferred_deals ?? [];
    // `dollar_value_available: false` is the right default for a missing
    // payload: no data means "we could not look", never "nothing is at stake".
    const forecast = data.forecast_integrity ?? {
        count: 0,
        dollar_value: 0,
        dollar_value_available: false,
    };

    const nothingAtAll =
        tiles.length === 0 &&
        breaches.length === 0 &&
        rates.length === 0 &&
        lagCounts.length === 0 &&
        deferred.length === 0 &&
        forecast.count === 0 &&
        forecast.dollar_value === 0;

    return (
        <PageContainer>
            {header}

            {/* A refresh that failed while the previous numbers are still good. */}
            {err && (
                <div className="mb-6">
                    <ErrorState message={err.message} />
                </div>
            )}

            {/* HubSpot down degrades labels and the dollar figure, not the page.
                Said out loud so "company <an id>" and an em dash are explained
                rather than looking like bugs. */}
            {data.crm_available === false && (
                <div className="mb-6 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900" role="status">
                    HubSpot could not be read just now, so companies and deals below show as ids and
                    the dollar figure reads "—". The commitment clocks are Cubie's own and are
                    unaffected.
                </div>
            )}

            {nothingAtAll ? (
                <EmptyState
                    title="Your team is clear"
                    hint="No commitments past SLA, no forecast disagreements, and no captured calls awaiting a wrap-up. This is a real answer, not a missing one — if you expected people here, check that your team is assigned to you in the EOS org chart."
                />
            ) : (
                <div className="space-y-8">
                    <section data-tour="sales-team-rollup-states">
                        <SectionHeading
                            icon={<Users size={14} className="text-gray-400" />}
                            title="Commitments by state"
                            note="Every threshold behind these counts is a config row, not a deploy."
                        />
                        <RollUp
                            tiles={tiles}
                            canDrill={breaches.length > 0}
                            onDrill={() =>
                                breachRef.current?.scrollIntoView({
                                    behavior: 'smooth',
                                    block: 'start',
                                })
                            }
                        />
                    </section>

                    <ForecastIntegrityPanel data={forecast} />

                    <section ref={breachRef} data-tour="sales-team-rollup-breaches">
                        <SectionHeading
                            icon={<AlertTriangle size={14} className="text-gray-400" />}
                            title="Breaches, with the reason"
                            note="Oldest first. Ordered by age — never by person."
                        />
                        <BreachTable rows={breaches} />
                    </section>

                    <section data-tour="sales-team-rollup-deferred">
                        <SectionHeading
                            icon={<CalendarClock size={14} className="text-gray-400" />}
                            title="Deferred deals"
                            note="Off the rep's own lists until the follow-up date, never off this one. Soonest back first."
                        />
                        <DeferredDeals rows={deferred} />
                    </section>

                    <section>
                        <SectionHeading
                            icon={<ScrollText size={14} className="text-gray-400" />}
                            title="Stage-lag exceptions by AE"
                            note="Listed A–Z, same reason as below. A count of open disagreements, not a score."
                        />
                        <StageLagCounts rows={lagCounts} />
                    </section>

                    <section data-tour="sales-team-rollup-wrapups">
                        <SectionHeading
                            icon={<ClipboardCheck size={14} className="text-gray-400" />}
                            title="Wrap-up completion"
                            note="A process metric, listed A–Z. Not a ranking, and not for evaluating anyone."
                        />
                        <WrapupRates rows={rates} />
                    </section>
                </div>
            )}
        </PageContainer>
    );
};

export default ManagerView;
