import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { ChevronRight, RefreshCw, TrendingUp } from 'lucide-react';
import {
    MY_DAY_BLOCK_LABELS,
    MY_DAY_BLOCK_ORDER,
    acceptCommitmentBreach,
    applyStageLag,
    blockUnavailableCopy,
    closeCommitment,
    cockpitError,
    fetchMyDay,
    isCockpitActionKind,
    isForbidden,
    isNotEnabled,
    myDayBlock,
    nudgeCommitment,
    rejectStageLag,
    setCommitmentEta,
    wrapupRoute,
} from '../../api/cockpit';
import type {
    CockpitAction,
    CockpitError,
    CockpitItem,
    MyDay as MyDayData,
    MyDayBlockKey,
} from '../../api/cockpit';
import {
    EmptyState,
    EosPageHeader,
    ErrorState,
    LoadingState,
    PageContainer,
} from '../eos/_shared';
import { BackButton } from '../../components/BackButton';
import { Field, Modal, SubmitButton, inputCls } from '../../components/eos/Modal';
import { confirmDialog } from '../../components/eos/ConfirmDialog';
import { toast } from '../../components/eos/Toast';
import { SecondaryButton } from '../../components/smartTeams/shared';
import {
    ActionRow,
    DeferDealModal,
    DeferredDeals,
    MAX_INAPP_ITEMS,
    StageLagCard,
    ageChipPropsFor,
    isMyDayBlockKey,
    nudgeOutcomeMessage,
} from '../../components/sales';
import type { ActionRowAction } from '../../components/sales';
import type { DeferTarget } from '../../api/dealDefer';
import { useAuth } from '../../context/AuthContext';
import { DailyRhythm } from './DailyRhythm';
import { LooseEndsPanel } from './LooseEndsPanel';

// The AE view — an AE's morning, in the AE's own order:
//
//   1. Wrap-ups waiting     (CCS — the only new item, first because it decays)
//   2. Leads                (with the temperature filter the AEs asked for)
//   3. Pipeline hygiene     (stage-lag exceptions, one click each)
//   4. Deals                (commitments with age chips)
//   5. No contact in 90 days
//
// THE ORDER IS THE FEATURE. A parallel ritual will not survive, so the page
// mirrors the sequence they already work. Do not "improve" it, and do
// not sort by urgency across blocks.
//
// Rule 2, the anti-tsunami cap: at most three actionable items per block. The
// remainder renders as "+4 more" and LINKS to the block's own view. It must NOT
// expand inline — a chevron that expands defeats the cap, and a list of twenty
// is a list of zero.
//
// Rule 3: every row carries exactly one primary action, enforced by
// `ActionRow`'s props. A block with nothing in it gets a quiet one-line note,
// not a card full of whitespace.
//
// Defer: a row about one deal carries a `defer` handle (people on the
// `deal_defer` roster only), rendered as a quiet "Defer" link beside the row's
// buttons — it sets the deal aside until a follow-up date, it is not a second
// primary action. Deferred deals are left out of the blocks by the server and
// listed in "Deferred deals" below the five blocks, where Undefer lives.

// ─── lead temperature ────────────────────────────────────────────────────────
//
// ASSUMPTION, and it is the one place this page reads the server's prose:
// `CockpitItem` has no temperature field (api/cockpit.ts), and inventing
// HubSpot property names is forbidden — the lead-temperature property's
// internal name is still unknown, which is part of why
// `CCS_LEADS_ENABLED` ships off. So temperature is recovered from the words the
// server already puts on the row, and the filter only appears when at least one
// lead actually carries one. A control that cannot filter never renders.

const LEAD_TEMPERATURES = ['hot', 'warm', 'cold'] as const;
type LeadTemperature = (typeof LEAD_TEMPERATURES)[number];

const TEMPERATURE_LABELS: Record<LeadTemperature, string> = {
    hot: 'Hot',
    warm: 'Warm',
    cold: 'Cold',
};

// Full literal class strings — never interpolate a Tailwind fragment, the JIT
// purges what it cannot see whole.
const FILTER_CHIP: Record<'on' | 'off', string> = {
    on: 'bg-brand text-white border-brand',
    off: 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50',
};

const temperatureOf = (item: CockpitItem): LeadTemperature | null => {
    const hay = `${item.title} ${item.subtitle ?? ''}`.toLowerCase();
    return LEAD_TEMPERATURES.find(t => new RegExp(`\\b${t}\\b`).test(hay)) ?? null;
};

// ─── per-block copy ──────────────────────────────────────────────────────────

const EMPTY_LINES: Record<MyDayBlockKey, string> = {
    wrapups_pending: 'No calls waiting to be wrapped up.',
    leads: 'No leads waiting on a first touch.',
    pipeline_hygiene: 'Every deal stage agrees with its evidence.',
    deals: 'Nothing running against a clock.',
    no_contact_90_days: 'Every account has been touched in the last 90 days.',
};

const BLOCK_HINTS: Record<MyDayBlockKey, string> = {
    wrapups_pending: 'Time-sensitive: the transcript is fresh and so is your memory.',
    leads: 'First touch is a clock, same as everything else here.',
    pipeline_hygiene: 'What HubSpot says versus what the call said.',
    deals: 'Set an ETA and the generic SLA stops running against you.',
    no_contact_90_days: 'Quiet accounts, oldest first.',
};

const LIST_CARD = 'bg-white rounded-lg border border-gray-200 shadow-sm divide-y divide-gray-100';

const NOTE_CARD = 'rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900';

// `reject_stage` gets a form because the reason is the tuning data — the
// rejection rate per signal type is the only honest measure of whether the
// detector helps. The pipeline-hygiene CARD asks inline (StageLagCard); this
// modal is for the same action rendered as a plain row, which happens when the
// exception has no target stage in HubSpot and the router therefore promotes
// "Not right" to the PRIMARY action.
type FormMode = 'eta' | 'accept' | 'reject_stage';
interface FormTarget {
    mode: FormMode;
    id: string;
    title: string;
}

const FORM_TITLES: Record<FormMode, string> = {
    eta: 'Set an expected date',
    accept: 'Accept this breach',
    reject_stage: 'Tell Cubie it is not right',
};

const REASON_LABELS: Record<FormMode, string> = {
    eta: 'Why this date?',
    accept: 'Why is this acceptable?',
    reject_stage: 'What did Cubie get wrong? (optional)',
};

const REASON_PLACEHOLDERS: Record<FormMode, string> = {
    eta: 'Waiting on the scope call with their SME.',
    accept: 'Client asked us to hold until their board meets on the 14th.',
    reject_stage: 'They said that about the other deal, not this one.',
};

const SUBMIT_LABELS: Record<FormMode, string> = {
    eta: 'Save ETA',
    accept: 'Accept breach',
    reject_stage: 'Record it',
};

export const MyDay: React.FC = () => {
    const [data, setData] = useState<MyDayData | null>(null);
    const [error, setError] = useState<CockpitError | null>(null);
    const [refreshKey, setRefreshKey] = useState(0);
    const [temperature, setTemperature] = useState<LeadTemperature | null>(null);

    const [form, setForm] = useState<FormTarget | null>(null);
    const [promisedAt, setPromisedAt] = useState('');
    const [reason, setReason] = useState('');
    const [submitting, setSubmitting] = useState(false);

    const [deferTarget, setDeferTarget] = useState<DeferTarget | null>(null);
    const [deferredKey, setDeferredKey] = useState(0);

    const navigate = useNavigate();
    const [params] = useSearchParams();
    const { hasFeature } = useAuth();
    const canDefer = hasFeature('deal_defer');

    // Rule 2's overflow target: a single-block view at its own URL. Derived from
    // the query string, never mirrored into state.
    const blockParam = params.get('block');
    const focus: MyDayBlockKey | null = isMyDayBlockKey(blockParam) ? blockParam : null;

    // Every setState sits inside the async callback: a synchronous one in the
    // effect body fails `react-hooks/set-state-in-effect`, which is an ERROR in
    // this repo. Which is also why there is no `loading` boolean — the spinner
    // is DERIVED below.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const fresh = await fetchMyDay();
                if (cancelled) return;
                setData(fresh);
                setError(null);
            } catch (err) {
                if (!cancelled) setError(cockpitError(err));
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [refreshKey]);

    const loading = data === null && error === null;
    const refresh = () => setRefreshKey(k => k + 1);
    // A defer from any row (or from the Daily rhythm) takes the deal off these
    // blocks and adds it to Deferred deals, so both reload.
    const afterDefer = () => {
        refresh();
        setDeferredKey(k => k + 1);
    };

    /** The row's "Defer", when the server sent a handle and the reader may. */
    const deferActionFor = (item: CockpitItem): ActionRowAction | undefined => {
        const target = item.defer;
        if (!canDefer || !target?.deal_id) return undefined;
        return { label: 'Defer', onClick: async () => setDeferTarget(target) };
    };

    // ─── writes ──────────────────────────────────────────────────────────────
    //
    // Deliberately swallows: the caller is an ActionRow button whose pending
    // state must clear either way, and every outcome is already reported through
    // a toast. Nothing here re-throws, so the row's own backstop stays quiet.
    const runWrite = async (op: () => Promise<string>): Promise<void> => {
        try {
            toast.success(await op());
            refresh();
        } catch (err) {
            toast.error(cockpitError(err).message);
        }
    };

    const goTo = (href: string): void => {
        if (/^https?:\/\//i.test(href)) {
            window.open(href, '_blank', 'noopener,noreferrer');
        } else {
            navigate(href);
        }
    };

    const openForm = (mode: FormMode, item: CockpitItem): void => {
        setPromisedAt('');
        setReason('');
        setForm({ mode, id: item.id, title: item.title });
    };

    /** Anything this build cannot perform becomes a link, or a disabled button
     *  that says why. A row that throws is worse than a row that only links
     *  out (api/cockpit.ts, on the action vocabulary being an assumption). */
    const fallbackAction = (action: CockpitAction): ActionRowAction => {
        if (action.href) {
            const href = action.href;
            return { label: action.label, onClick: async () => goTo(href) };
        }
        return {
            label: action.label,
            onClick: async () => {},
            disabledReason: `Cubie does not know how to "${action.label}" yet. Open the record in HubSpot instead.`,
        };
    };

    // THE EIGHT WIRE STRINGS ARE THE ROUTER'S, NOT OURS.
    // `backend/routers/cockpit.py` emits exactly: review_wrapup,
    // close_commitment, set_eta, accept_breach, apply_stage, reject_stage,
    // log_outreach, nudge. This switch previously used `open`, `open_wrapup`,
    // `close`, `accept`, `stage_lag_apply` and `stage_lag_reject` — six names
    // the server has never sent — so six of the eight buttons on this page fell
    // through to the disabled "Cubie does not know how to…" fallback. Check the
    // router before renaming anything here; it is the server.
    const toRowAction = (item: CockpitItem, action: CockpitAction): ActionRowAction => {
        if (!isCockpitActionKind(action.kind)) return fallbackAction(action);
        const label = action.label;
        switch (action.kind) {
            // `item.id` is a call_sessions id here, not a commitment id. The
            // router emits no href on purpose (an invented in-app path would be
            // a link that silently does nothing), so the route lives in one
            // constant in api/cockpit.ts.
            //
            // THIS IS THE MOST VISIBLE CLICK IN THE FEATURE — the one primary
            // action on the block the spec puts first — and it shipped broken
            // once: `wrapupRoute` returned `/calls/wrapup?session=<id>`, which
            // App.tsx does not register, so "Review the wrap-up" navigated to a
            // blank page. The route is `calls/:sessionId/wrapup` and it is
            // pinned by a test; do not hand-roll a path here.
            case 'review_wrapup':
                return { label, onClick: async () => goTo(wrapupRoute(item.id)) };
            // Logging outreach is a HubSpot write; Cubie has no endpoint that
            // would make the same thing true. `href` is the deep link, and it
            // is null when the workspace HubSpot portal id is unset — `fallbackAction` then
            // disables the button with a sentence rather than opening nothing.
            case 'log_outreach':
                return fallbackAction(action);
            case 'set_eta':
                return { label, onClick: async () => openForm('eta', item) };
            case 'accept_breach':
                return { label, onClick: async () => openForm('accept', item) };
            case 'close_commitment':
                return {
                    label,
                    onClick: async () => {
                        const ok = await confirmDialog({
                            title: 'Close this commitment?',
                            message: `${item.title}\n\nThis records a manual close by you. Sent mail only ever suggests a close — a person confirms it.`,
                            confirmLabel: 'Close it',
                        });
                        if (!ok) return;
                        await runWrite(async () => {
                            await closeCommitment(item.id);
                            return 'Closed.';
                        });
                    },
                };
            case 'nudge':
                return {
                    label,
                    onClick: () =>
                        runWrite(async () => nudgeOutcomeMessage(await nudgeCommitment(item.id))),
                };
            case 'apply_stage':
                return {
                    label,
                    onClick: () =>
                        runWrite(async () => {
                            await applyStageLag(item.id);
                            return 'Stage updated in HubSpot.';
                        }),
                };
            case 'reject_stage':
                // Asks for the reason rather than firing blind: "Not right" is a
                // LABELLED SIGNAL, not a dismiss.
                return { label, onClick: async () => openForm('reject_stage', item) };
            default:
                return fallbackAction(action);
        }
    };

    // ─── form submit ─────────────────────────────────────────────────────────

    const canSubmit = (): boolean => {
        if (!form) return false;
        if (submitting) return false;
        // The reject reason is optional server-side (`RejectStageLagRequest`),
        // and asking for it is what makes it tuning data instead of a shrug —
        // but a required field would turn a shrug into a dead end.
        if (form.mode === 'reject_stage') return true;
        if (form.mode === 'accept') return reason.trim().length > 0;
        return promisedAt.trim().length > 0 && reason.trim().length > 0;
    };

    const onSubmitForm = async (e: React.FormEvent): Promise<void> => {
        e.preventDefault();
        if (!form || !canSubmit()) return;
        setSubmitting(true);
        try {
            if (form.mode === 'reject_stage') {
                const why = reason.trim();
                await rejectStageLag(form.id, why ? { reason: why } : {});
                toast.success('Signal recorded. Thanks — that is what tunes the detector.');
            } else if (form.mode === 'eta') {
                // `new Date`, deliberately: this is a `datetime-local` value
                // the user typed in their OWN timezone, not a backend timestamp.
                // Local is the right reading; `.toISOString()` below converts it.
                const when = new Date(promisedAt);
                if (Number.isNaN(when.getTime())) {
                    toast.error('That date could not be read. Pick it again.');
                    return;
                }
                await setCommitmentEta(form.id, {
                    promised_at: when.toISOString(),
                    reason: reason.trim(),
                });
                toast.success('ETA saved. The clock now runs to your date.');
            } else {
                // 422 on a blank reason is enforced server-side too; the disabled
                // button is the convenience, not the guard.
                await acceptCommitmentBreach(form.id, { reason: reason.trim() });
                toast.success('Breach accepted, with your reason on the record.');
            }
            setForm(null);
            refresh();
        } catch (err) {
            // Stays open on failure — a spinner that just stops reads as saved.
            toast.error(cockpitError(err).message);
        } finally {
            setSubmitting(false);
        }
    };

    // ─── block rendering ─────────────────────────────────────────────────────

    const renderRows = (key: MyDayBlockKey, items: CockpitItem[]): React.ReactNode => {
        if (key === 'pipeline_hygiene') {
            return (
                <div className="space-y-3">
                    {items.map(item =>
                        // The card exists to offer the one-click advance, so it
                        // is only right when there IS one. Not every signal maps
                        // to a stage on this portal ("verbal agreement" has
                        // none), and for those rows the router promotes
                        // `reject_stage` to the PRIMARY action — rendering that
                        // as an "Advance the stage" card would put two "Not
                        // right" buttons on one card, one of them unable to ask
                        // for a reason.
                        item.primary_action.kind === 'apply_stage' ? (
                            <StageLagCard
                                key={item.id}
                                title={item.title}
                                stageSummary={item.subtitle}
                                quote={item.evidence_text}
                                applyAction={{
                                    label: item.primary_action.label,
                                    onClick: toRowAction(item, item.primary_action).onClick,
                                }}
                                rejectAction={{
                                    label: item.secondary_action?.label ?? 'Not right',
                                    onClick: (why: string) =>
                                        runWrite(async () => {
                                            await rejectStageLag(item.id, why ? { reason: why } : {});
                                            return 'Signal recorded. Thanks — that is what tunes the detector.';
                                        }),
                                }}
                                deferAction={
                                    canDefer && item.defer?.deal_id
                                        ? {
                                              label: 'Defer',
                                              onClick: () => setDeferTarget(item.defer ?? null),
                                          }
                                        : undefined
                                }
                            />
                        ) : (
                            <div key={item.id} className={LIST_CARD}>
                                <ActionRow
                                    title={item.title}
                                    subtitle={item.subtitle}
                                    age={ageChipPropsFor(item)}
                                    evidence={item.evidence_text}
                                    footnote="Cubie has no HubSpot stage to advance this to, so the only action is telling it the signal was wrong. Move the deal by hand if it needs it."
                                    primaryAction={toRowAction(item, item.primary_action)}
                                    secondaryAction={
                                        item.secondary_action
                                            ? toRowAction(item, item.secondary_action)
                                            : undefined
                                    }
                                    tertiaryAction={deferActionFor(item)}
                                />
                            </div>
                        ),
                    )}
                </div>
            );
        }
        return (
            <div className={LIST_CARD}>
                {items.map(item => (
                    <ActionRow
                        key={item.id}
                        title={item.title}
                        subtitle={item.subtitle}
                        age={ageChipPropsFor(item)}
                        evidence={item.evidence_text}
                        primaryAction={toRowAction(item, item.primary_action)}
                        secondaryAction={
                            item.secondary_action
                                ? toRowAction(item, item.secondary_action)
                                : undefined
                        }
                        tertiaryAction={deferActionFor(item)}
                    />
                ))}
            </div>
        );
    };

    const renderBlock = (key: MyDayBlockKey, full: boolean): React.ReactNode => {
        const block = myDayBlock(data, key);
        const filtering = key === 'leads' && temperature !== null;
        const items = filtering
            ? block.items.filter(i => temperatureOf(i) === temperature)
            : block.items;
        const shown = full ? items : items.slice(0, MAX_INAPP_ITEMS);
        // Held back by the client cap, plus the ones the server already held
        // back. Not counted while a filter is on: the suppressed rows' own
        // temperature is unknown, so adding them would be a made-up number.
        const heldBack = items.length - shown.length + (filtering ? 0 : block.suppressed_count);

        const leadTemps =
            key === 'leads'
                ? LEAD_TEMPERATURES.filter(t => block.items.some(i => temperatureOf(i) === t))
                : [];

        // "You have nothing here" versus "Cubie could not look". The router
        // sends a NAMED CODE for the second (`unavailable_reason`), and an empty
        // list must never stand in for both — an AE told they are caught up
        // because call capture is switched off is the worst thing this block
        // could say. Rendered even when rows came back, because
        // `lead_temperature_unavailable` means the ROWS are fine and the FILTER
        // did not apply.
        const unavailable = blockUnavailableCopy(block.unavailable_reason);

        return (
            <section key={key} className="mb-8" data-tour={key === MY_DAY_BLOCK_ORDER[0] ? 'my-day-blocks' : undefined}>
                <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
                    <div>
                        <h2 className="text-lg font-semibold text-gray-900">
                            {MY_DAY_BLOCK_LABELS[key]}
                            {shown.length > 0 && (
                                <span className="ml-2 text-sm font-normal text-gray-500">
                                    {shown.length}
                                </span>
                            )}
                        </h2>
                        <p className="mt-0.5 text-xs text-gray-500">{BLOCK_HINTS[key]}</p>
                    </div>

                    {leadTemps.length > 0 && (
                        <div className="flex items-center gap-1.5" role="group" aria-label="Filter leads by temperature">
                            <button
                                type="button"
                                onClick={() => setTemperature(null)}
                                aria-pressed={temperature === null}
                                className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${temperature === null ? FILTER_CHIP.on : FILTER_CHIP.off}`}
                            >
                                All
                            </button>
                            {leadTemps.map(t => (
                                <button
                                    key={t}
                                    type="button"
                                    onClick={() => setTemperature(temperature === t ? null : t)}
                                    aria-pressed={temperature === t}
                                    className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${temperature === t ? FILTER_CHIP.on : FILTER_CHIP.off}`}
                                >
                                    {TEMPERATURE_LABELS[t]}
                                </button>
                            ))}
                        </div>
                    )}
                </div>

                {unavailable && (
                    <p className={`mb-3 ${NOTE_CARD}`} role="status">
                        {unavailable}
                    </p>
                )}

                {shown.length === 0 ? (
                    // Suppressed when the server already explained itself: the
                    // reason above is the answer, and "you are all caught up"
                    // underneath it would contradict it.
                    unavailable ? null : (
                        <p className="px-1 text-sm text-gray-500">
                            {filtering && temperature
                                ? `No ${TEMPERATURE_LABELS[temperature].toLowerCase()} leads right now.`
                                : EMPTY_LINES[key]}
                        </p>
                    )
                ) : (
                    renderRows(key, shown)
                )}

                {/* Rule 2's overflow. A LINK, never an expander. */}
                {!full && heldBack > 0 && (
                    <div className="mt-2 px-1">
                        <Link
                            to={`/sales?block=${key}`}
                            className="inline-flex items-center gap-1 text-sm font-medium text-brand hover:underline"
                        >
                            +{heldBack} more
                            <ChevronRight size={15} aria-hidden="true" />
                        </Link>
                    </div>
                )}
                {full && block.suppressed_count > 0 && (
                    <p className="mt-2 px-1 text-xs text-gray-500">
                        {block.suppressed_count} more qualify. They surface here as these are
                        cleared — three at a time is the point.
                    </p>
                )}
            </section>
        );
    };

    // ─── page ────────────────────────────────────────────────────────────────

    let body: React.ReactNode;
    if (loading) {
        body = <LoadingState label="Loading your day…" />;
    } else if (error && isNotEnabled(error)) {
        body = (
            <EmptyState
                title="The sales cockpit is not switched on yet."
                hint="Nothing is wrong with your account — the feature ships behind a flag and it is still off."
            />
        );
    } else if (error && isForbidden(error)) {
        body = (
            <EmptyState
                title="This view is not open to your account."
                hint="My Day is the AE surface. Ask Sales Ops if you should be on it."
            />
        );
    } else if (error) {
        body = <ErrorState message={error.message} />;
    } else if (focus) {
        body = renderBlock(focus, true);
    } else {
        body = <>{MY_DAY_BLOCK_ORDER.map(key => renderBlock(key, false))}</>;
    }

    // Two envelope-level honesty flags from `MyDayResponse`. Neither is an
    // error and neither blocks the page — but a CRM record rendered as "deal
    // <an id>", or a row with no quote, must be explained rather than silently
    // odd.
    const crmDown = data !== null && data.crm_available === false;
    const noQuotes = data !== null && data.transcript_policy === 'none';

    return (
        <PageContainer>
            {focus && (
                <div className="mb-4">
                    <BackButton fallback="/sales" label="My Day" />
                </div>
            )}
            <EosPageHeader
                icon={<TrendingUp size={20} />}
                title={focus ? MY_DAY_BLOCK_LABELS[focus] : 'My Day'}
                subtitle={
                    focus
                        ? 'The full list behind this morning block.'
                        : 'Your sales morning, in the order you already work it.'
                }
                right={
                    <SecondaryButton onClick={refresh}>
                        <RefreshCw size={15} aria-hidden="true" />
                        Refresh
                    </SecondaryButton>
                }
            />

            {crmDown && (
                <p className={`mb-6 ${NOTE_CARD}`} role="status">
                    HubSpot could not be read just now, so companies and deals show as ids and the
                    CRM-sourced blocks below say so. Your commitments and their clocks are Cubie's
                    own and are unaffected.
                </p>
            )}
            {noQuotes && (
                <p className="mb-6 px-1 text-xs text-gray-500">
                    Your account does not carry transcript text, so these rows are metadata only.
                    That is the access rule, not a missing quote.
                </p>
            )}

            {body}

            {/* The sales checklist's Morning / Mid-day / End-of-day routines.
                BELOW the five blocks on purpose — their order is the
                feature — and only on the full page, once My Day itself has
                loaded, for people on the `sales_rhythm` roster. */}
            {/* Loose Ends: the three oldest items from the morning loop-closure
                scan, for people on the `loop_closure` roster. Same placement
                rule as the rhythm: below the five blocks, full page only. */}
            {!focus && data !== null && hasFeature('loop_closure') && <LooseEndsPanel />}
            {!focus && data !== null && hasFeature('sales_rhythm') && (
                // Shares the reload key both ways: a defer here reloads the
                // blocks and Deferred deals, a defer above reloads the rhythm.
                <DailyRhythm reloadKey={deferredKey} onDeferred={afterDefer} />
            )}
            {/* Deferred deals: where Undefer lives, since a deferred deal is off
                every list above. Last on the page — it is what you set aside,
                not today's work — full page only, for people on the
                `deal_defer` roster. */}
            {!focus && data !== null && canDefer && (
                <DeferredDeals reloadKey={deferredKey} onChanged={refresh} />
            )}

            <DeferDealModal
                key={deferTarget?.deal_id ?? 'closed'}
                target={deferTarget}
                onClose={() => setDeferTarget(null)}
                onDeferred={afterDefer}
            />

            <Modal
                isOpen={form !== null}
                onClose={() => setForm(null)}
                title={form ? FORM_TITLES[form.mode] : FORM_TITLES.eta}
                maxWidth="max-w-lg"
            >
                <form onSubmit={onSubmitForm} className="space-y-4">
                    <p className="text-sm text-gray-600">{form?.title}</p>

                    {form?.mode === 'eta' && (
                        <>
                            <Field label="Expected date">
                                <input
                                    required
                                    type="datetime-local"
                                    className={inputCls}
                                    value={promisedAt}
                                    onChange={e => setPromisedAt(e.target.value)}
                                />
                            </Field>
                            <p className="text-xs text-gray-500">
                                A date later than the standard SLA moves the clock to your date —
                                a three-week RFP is not late on day four. A date earlier than the
                                SLA never shortens it, so promising a client something tight can
                                never create an internal breach.
                            </p>
                        </>
                    )}

                    <Field label={REASON_LABELS[form?.mode ?? 'eta']}>
                        <textarea
                            // Optional only for the rejection: the reason is
                            // tuning colour there, and requiring it would turn a
                            // shrug into a dead end. The other two are 422s
                            // server-side.
                            required={form?.mode !== 'reject_stage'}
                            rows={3}
                            className={inputCls}
                            value={reason}
                            onChange={e => setReason(e.target.value)}
                            placeholder={REASON_PLACEHOLDERS[form?.mode ?? 'eta']}
                        />
                    </Field>
                    {form?.mode === 'accept' && (
                        <p className="text-xs text-gray-500">
                            The reason is stored. It is the audit trail your sales supervisor keeps by
                            hand today, so write it for someone reading this in three months.
                        </p>
                    )}
                    {form?.mode === 'reject_stage' && (
                        <p className="text-xs text-gray-500">
                            Optional, and worth a line anyway: the "not right" rate per signal type
                            is the only honest measure of whether this detector is helping. Saying
                            no to this quote does not bury the deal — a stronger signal later still
                            surfaces.
                        </p>
                    )}

                    <SubmitButton disabled={!canSubmit()} onCancel={() => setForm(null)}>
                        {submitting ? 'Saving…' : SUBMIT_LABELS[form?.mode ?? 'eta']}
                    </SubmitButton>
                </form>
            </Modal>
        </PageContainer>
    );
};
