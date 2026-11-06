/**
 * Cubie Sales Cockpit — the supervisor worklist. The sales supervisor's
 * screen.
 *
 * The whole design brief: give the supervisor one dashboard, a single source
 * where they see everything that is late and can go straight to the AE who
 * owns it. The supervisor is the one responsible for chasing the AEs and, once
 * taught what to look for, owns it.
 *
 * ── A WORKLIST, NOT A DASHBOARD ─────────────────────────────────────────────
 * Every row has exactly ONE action and that action writes something (Rule 3).
 * There are no tiles here, no rates, no trend. If a row has nothing to do it
 * does not belong on this screen — which is why `ActionRow` requires
 * `primaryAction` in the type system rather than trusting a reviewer.
 *
 * ── METADATA ONLY. NO TRANSCRIPT TEXT. NO EXCEPTION. ────────────────────────
 * The cockpit's access rule is per-FIELD, and for this view it reads:
 *
 *     Supervisor            team METADATA only   Transcript text: NONE, ever
 *
 * "No exception" includes a deal they are personally working. The router enforces
 * it structurally — `/api/sales/supervisor` selects `_METADATA_COLUMNS` and
 * `opened_evidence_text` is deliberately NOT in that SELECT, so there is no code
 * path where a serializer could leak it.
 *
 * ⚠⚠ `evidence_text` NEVERTHELESS EXISTS ON `CockpitItem`, because that type is
 * shared with My Day. So it compiles here. `ActionRow` takes an `evidence` prop.
 * Wiring the two together is a one-word change that no compiler and no test
 * would catch, and the next person to open this file will reach for it by
 * reflex. DO NOT. If the API ever starts sending `evidence_text` on this
 * endpoint, that is a SERVER bug to report — not a field to render. If the
 * supervisor needs the content they ask the AE, and that is the design, not a
 * gap.
 *
 * ── Other load-bearing choices ──────────────────────────────────────────────
 * - Rule 2: the ROUTER caps the list (`_sorted_and_capped`) and returns
 *   `suppressed_count`. The overflow renders as "+N more" and it is a LINK. A
 *   chevron that expands in place defeats the cap, and a list of twenty is a
 *   list of zero.
 * - A nudge stays visible after it lands. "Nudged twice, still open" is an
 *   escalation trigger rather than somebody's memory (Rule 4) — so the FIRST
 *   nudge has to be on the screen for the second to mean anything.
 * - A nudge returns 200 even when the Slack DM was skipped: `send_dm` returns
 *   None for a non-member and the cockpit counts that as a skip, not a
 *   failure. This page never prints a blind "Nudged"; it prints what
 *   `nudgeOutcomeMessage` reads off the body.
 * - 404 with no detail code is the feature flag, which ships OFF. Calm empty
 *   state, never a red error and never a retry.
 * - 409 `org_record_missing` / `team_unresolved` is the Accountability Chart
 *   being unable to say who their AEs are. It is a 409 and not a
 *   200-with-nothing on purpose: on a worklist, "your team has nothing
 *   outstanding" is the most damaging thing this feature could say wrongly.
 */
import React, { useEffect, useState } from 'react';
import { ChevronRight, Megaphone, RefreshCw } from 'lucide-react';
import { Link, useLocation, useSearchParams } from 'react-router-dom';

import {
    cockpitError,
    fetchSupervisorWorklist,
    isForbidden,
    isNotEnabled,
    isOrgRecordMissing,
    isTeamUnresolved,
    nudgeCommitment,
} from '../../api/cockpit';
import type { CockpitError, CockpitItem, SupervisorWorklist as Worklist } from '../../api/cockpit';
import {
    ActionRow,
    MAX_INAPP_ITEMS,
    ageChipPropsFor,
    nudgeCountFrom,
    nudgeNeedsEscalation,
    nudgeOutcomeMessage,
} from '../../components/sales';
import { EmptyState, EosPageHeader, ErrorState, LoadingState, PageContainer } from '../eos/_shared';
import { SecondaryButton } from '../../components/smartTeams/shared';
import { toast } from '../../components/eos/Toast';

const LIST_CARD = 'bg-white rounded-lg border border-gray-200 shadow-sm divide-y divide-gray-100';
const NOTE_CARD = 'rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900';

const FALLBACK = 'The nudge worklist could not be loaded.';

/** What this session knows about a row it has already nudged.
 *
 *  Client-local, and honestly so: `/api/sales/supervisor` returns commitment
 *  METADATA and carries no nudge history, so a nudge sent yesterday — or from
 *  the Monday roll-up — is not visible here. `unacknowledged_nudges` comes back
 *  on the write and IS server truth, which is why it is preferred over counting
 *  clicks: on the very first nudge of a row that was already chased once, the
 *  server says 2 and the copy says so. */
interface NudgeMemo {
    /** Local wall-clock of the click. Not age arithmetic — elapsed time is
     *  business-hours and belongs to the server. This is "when I did it",
     *  which the browser is the right clock for. */
    at: string;
    /** What actually went out, in the words of the response body. */
    message: string;
    /** The server's sent-and-unacknowledged count, when it reported one. */
    count: number | null;
    /** Rule 4 fired: two unacknowledged nudges is an escalation, not a third
     *  nudge. The button stops offering itself. */
    needsEscalation: boolean;
}

const clockTime = (): string =>
    new Date().toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });

export const SupervisorWorklist: React.FC = () => {
    const [data, setData] = useState<Worklist | null>(null);
    const [err, setErr] = useState<CockpitError | null>(null);
    const [refreshKey, setRefreshKey] = useState(0);
    const [refreshing, setRefreshing] = useState(false);
    const [nudged, setNudged] = useState<Record<string, NudgeMemo>>({});

    const [params] = useSearchParams();
    const location = useLocation();
    // Derived from the query string, never mirrored into state.
    const showAll = params.get('all') === '1';

    // Mount-only loader keyed on an explicit refresh counter. Every setState is
    // inside the async callback: a synchronous one in the effect body fails
    // `react-hooks/set-state-in-effect`, which is an ERROR in this repo. Which
    // is also why there is no `loading` boolean — it is DERIVED below.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const fresh = await fetchSupervisorWorklist();
                if (cancelled) return;
                setData(fresh);
                setErr(null);
            } catch (e) {
                if (!cancelled) setErr(cockpitError(e, FALLBACK));
            } finally {
                // Inside the async callback, so it is not the synchronous
                // effect-body setState the lint rule bans.
                if (!cancelled) setRefreshing(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [refreshKey]);

    const loading = data === null && err === null;

    // Both setStates are in an event handler, which is always allowed; the
    // effect above clears the spinner when the fetch settles.
    const refresh = () => {
        setRefreshing(true);
        setRefreshKey(k => k + 1);
    };

    /** The one action. Writes three times server-side — a HubSpot task on the
     *  AE, a Slack DM, and a `commitment_nudges` row — and the row is what turns
     *  "I told them" into a record. */
    const nudge = async (item: CockpitItem): Promise<void> => {
        try {
            const result = await nudgeCommitment(item.id);
            const message = nudgeOutcomeMessage(result);
            setNudged(prev => ({
                ...prev,
                [item.id]: {
                    at: clockTime(),
                    message,
                    count: nudgeCountFrom(result),
                    needsEscalation: nudgeNeedsEscalation(result),
                },
            }));
            // The refusals come back as a 200 with a named reason — they are
            // information, not success — so they are toasted as info/error
            // rather than dressed up as a completed nudge.
            if (result.nudge?.sent === false) toast.info(message);
            else toast.success(message);
            setRefreshKey(k => k + 1);
        } catch (e) {
            // A genuinely failed external write is a 502 with a sentence the
            // user can act on. Surfaced, never swallowed: a spinner that just
            // stops reads as "sent".
            toast.error(cockpitError(e, 'The nudge could not be sent.').message);
        }
    };

    /** A row's quiet second line. This is what makes the first nudge visible. */
    const footnoteFor = (item: CockpitItem): React.ReactNode => {
        const memo = nudged[item.id];
        if (!memo) return undefined;
        const counted =
            memo.count !== null && memo.count >= 2
                ? ` · ${memo.count} unacknowledged — this is escalation territory, not a third nudge`
                : memo.count !== null
                  ? ` · ${memo.count} unacknowledged`
                  : '';
        return (
            <span className={memo.needsEscalation ? 'text-red-700' : undefined}>
                Nudged {memo.at} — {memo.message}
                {counted}
            </span>
        );
    };

    const rowFor = (item: CockpitItem): React.ReactNode => {
        const memo = nudged[item.id];
        const action = item.primary_action;
        // The router sends `nudge` here and nothing else. Anything else is a
        // newer backend, and a row that explains itself beats a row that throws
        // or silently no-ops.
        const known = action.kind === 'nudge';
        return (
            <ActionRow
                key={item.id}
                title={item.title}
                subtitle={item.subtitle}
                age={ageChipPropsFor(item)}
                // NO `evidence` PROP. EVER. See the file header — this is the
                // one line on this screen that must not grow.
                footnote={footnoteFor(item)}
                primaryAction={{
                    label: action.label || 'Nudge',
                    onClick: () => nudge(item),
                    disabledReason: !known
                        ? `Cubie does not know how to "${action.label}" from this screen yet.`
                        : memo?.needsEscalation
                          ? 'Two nudges are already unacknowledged. The next step is escalating this to the manager, not chasing again.'
                          : null,
                }}
            />
        );
    };

    // ─── the states that are not errors ──────────────────────────────────────

    const header = (
        <EosPageHeader
            icon={<Megaphone size={20} />}
            title="Needs a nudge"
            subtitle="Every commitment on your team that is past its SLA, in one place, with one thing to do about each. Oldest and worst first."
            right={
                <SecondaryButton onClick={refresh} busy={refreshing}>
                    {!refreshing && <RefreshCw size={15} aria-hidden="true" />}
                    Refresh
                </SecondaryButton>
            }
        />
    );

    if (err && isNotEnabled(err)) {
        return (
            <PageContainer>
                {header}
                <EmptyState
                    title="Not enabled yet"
                    hint="The sales cockpit is switched off in this environment. Nothing is wrong with your account and there is nothing to retry — this list appears once the feature is turned on."
                />
            </PageContainer>
        );
    }

    if (err && isTeamUnresolved(err)) {
        return (
            <PageContainer>
                {header}
                <EmptyState
                    title={
                        isOrgRecordMissing(err)
                            ? 'Cubie has no org record for you yet'
                            : 'Cubie cannot tell who is on your team yet'
                    }
                    hint={`${err.message} Until that is filled in this page shows nothing — and an empty worklist is exactly what it must not imply, because "nobody is late" and "Cubie cannot see who is late" are opposite answers.`}
                />
            </PageContainer>
        );
    }

    if (err && isForbidden(err)) {
        return (
            <PageContainer>
                {header}
                <EmptyState
                    title="This list is for the sales supervisor"
                    hint={`${err.message} The server decides who chases the AEs; if that is your job and you are seeing this, ask an admin to add you in Admin → Feature Access.`}
                />
            </PageContainer>
        );
    }

    if (loading) {
        return (
            <PageContainer>
                {header}
                <LoadingState label="Loading the nudge list…" />
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

    // ─── the list ────────────────────────────────────────────────────────────

    const items = data.items ?? [];
    const suppressed = data.suppressed_count ?? 0;
    const teamSize = data.team_size;
    // The server already applied Rule 2's cap; this is the client half of the
    // same rule, so a backend that forgets cannot flood the screen.
    const shown = showAll ? items : items.slice(0, MAX_INAPP_ITEMS);
    const heldBack = items.length - shown.length;

    return (
        <PageContainer>
            {showAll && (
                <div className="mb-4">
                    <Link
                        to={{ pathname: location.pathname, search: '' }}
                        className="inline-flex items-center gap-1 text-sm font-medium text-brand hover:underline"
                    >
                        Back to the top three
                    </Link>
                </div>
            )}

            {header}

            {/* A refresh that failed while the previous list is still on screen.
                Blanking a good worklist over one dropped request is worse than
                showing it with a warning. */}
            {err && (
                <div className="mb-6">
                    <ErrorState message={err.message} />
                </div>
            )}

            {items.length === 0 ? (
                <>
                    <EmptyState
                        title="Nothing is past its SLA right now"
                        hint="This is a real answer, not a missing one: every commitment on your team is inside its clock, or has been accepted with a reason on the record."
                    />
                    {typeof teamSize === 'number' && teamSize <= 1 && (
                        <p className={`mt-4 ${NOTE_CARD}`} role="status">
                            Read that carefully: the Accountability Chart resolved only{' '}
                            {teamSize === 1 ? 'one person' : 'nobody'} for you, so an empty list
                            here can also mean the chart is thin rather than the pipeline being
                            clean. Ask an admin to check who is seated under you.
                        </p>
                    )}
                </>
            ) : (
                <>
                    <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
                        <div>
                            <h2 className="text-lg font-semibold text-gray-900">
                                Past SLA
                                <span className="ml-2 text-sm font-normal text-gray-500">
                                    {shown.length}
                                    {items.length !== shown.length ? ` of ${items.length}` : ''}
                                </span>
                            </h2>
                            <p className="mt-0.5 text-xs text-gray-500">
                                One action each: the nudge writes a HubSpot task to the AE, sends
                                them a Slack DM, and logs that it happened. The log is the point —
                                it is what makes "nudged twice, still open" a trigger instead of a
                                memory.
                            </p>
                        </div>
                        {typeof teamSize === 'number' && (
                            <p className="text-xs text-gray-500">
                                {teamSize} {teamSize === 1 ? 'person' : 'people'} resolved from the
                                Accountability Chart
                            </p>
                        )}
                    </div>

                    <div className={LIST_CARD} data-tour="sales-nudge-list">{shown.map(rowFor)}</div>

                    {/* Rule 2's overflow. A LINK, never an expander: a chevron
                        that expands in place defeats the cap. */}
                    {heldBack > 0 && (
                        <div className="mt-2 px-1">
                            <Link
                                to={{ pathname: location.pathname, search: '?all=1' }}
                                className="inline-flex items-center gap-1 text-sm font-medium text-brand hover:underline"
                            >
                                +{heldBack} more
                                <ChevronRight size={15} aria-hidden="true" />
                            </Link>
                        </div>
                    )}
                    {suppressed > 0 && (
                        <p className="mt-2 px-1 text-xs text-gray-500">
                            {suppressed} more are past SLA and are being held back on purpose. They
                            surface here as these are cleared — three at a time is what keeps this a
                            list you work rather than one you scroll.
                        </p>
                    )}
                </>
            )}
        </PageContainer>
    );
};

export default SupervisorWorklist;
