// The wrap-up screen — the one that decides whether Sales Call Capture is used
// at all.
//
// An AE's baseline is 15 minutes of typing HubSpot fields after a call. The
// design target is a TESTABLE NUMBER: ≤3 clicks and ≤90 seconds.
// Everything below is subordinate to that.
//
// EIGHT THINGS HERE ARE LOAD-BEARING:
//
// 1. EVERY ROW DEFAULTS TO TICKED. Approving is one click; rejecting is the
//    deliberate act. That includes the SOW row: an unticked-by-default row
//    costs a click on the common path.
// 2. "Nothing here" is NOT a cancel. It records an explicit negative: a
//    labelled training signal and a measured outcome. A call marked "nothing
//    here" is COMPLETE, not skipped. So it sits next to Approve, never beside a
//    close X, and the copy says what it does.
// 3. `wrap_opened_at` + `client_elapsed_ms` are captured on mount and sent with
//    the approve. The wrap-up timing is measured from them, so they exist
//    from day one — a retrofit measures nothing about launch.
// 4. An `is_overwrite` row cannot ride along on "Approve all". It needs its own
//    confirm first, and until it gets one the button is held. That is a ground
//    rule, not polish: a confident model overwriting good vendor
//    data is the failure that ends adoption in week one. The confirm is also a
//    WIRE field: that row's decision carries `confirm_overwrite: true`, set only
//    from the AE's confirmation on that row, and the server refuses the write
//    with `needs_confirmation` without it. Holding the button is the courtesy;
//    the flag is the contract.
// 7. Four of the server's ten per-item outcomes leave the proposal CLAIMABLE:
//    `needs_confirmation`, `invalid`, `drift`, `write_failed`. The session is
//    wrapped, those rows are not — so the screen re-reads itself and stays
//    usable instead of locking behind a green banner. A `drift` row comes back
//    re-anchored on what HubSpot actually holds now, which is exactly the
//    second look the server asked for.
// 5. An empty proposal set is NOT an error. Transcription may have been off,
//    the call may have run under a minute, or it was confidential. The wrap-up
//    still opens, empty, and says the call was logged. The call is never
//    invisible, even when its content is.
// 6. A 404 is the feature flag, not a fault. `isNotEnabled` → EmptyState.
// 8. A PARTIAL transcript is announced BEFORE the rows, not after. Transcription
//    can be stopped mid-call from the Meet UI, and a half-covered transcript
//    reports as ENDED and wraps up looking entirely normal. The AE has to know
//    the model only read part of the call before they start ticking rows —
//    people stop transcription exactly when the conversation turns to pricing or
//    a complaint, which is where the SOW ask tends to live. Amber and factual:
//    it is usually their own deliberate choice, so the band informs and never
//    accuses. `truncated: false` and a null coverage both render NOTHING.
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import {
    CaptionsOff,
    CheckCircle2,
    CircleSlash,
    EyeOff,
    FileText,
    ListChecks,
    PhoneCall,
    RefreshCw,
    Split,
    TriangleAlert,
} from 'lucide-react';
import {
    approveWrapUp,
    callsError,
    countWrapUpItems,
    coverageMinutes,
    fetchWrapUp,
    isCoverageReason,
    isNotEnabled,
    isSessionNotFound,
    isTruncatedCoverage,
    isUnresolvedResult,
    ITEM_KIND_ORDER,
    itemKindLabel,
    retryWrapUpExtraction,
    type ApproveDecision,
    type ApproveResult,
    type CallLogResult,
    type CallsError,
    type CoverageReason,
    type ItemKind,
    type ScriptCoverageSection,
    type WrapUp as WrapUpData,
    type WrapUpGroup,
    type WrapUpItem,
} from '../../api/calls';
import { BackButton } from '../../components/BackButton';
import { parseInstant } from '../../utils/datetime';
import { DraftProposalCta, ProposalRow } from '../../components/calls';
import { confirmDialog } from '../../components/eos/ConfirmDialog';
import { toast } from '../../components/eos/Toast';
import { PrimaryButton, SecondaryButton } from '../../components/smartTeams/shared';
import {
    EmptyState,
    EosPageHeader,
    ErrorState,
    LoadingState,
    PageContainer,
} from '../eos/_shared';
import { isWrapUpComplete } from './wrapUpCompletion';

/** What the call was supposed to establish, and what it did not.
 *
 *  This is the half of the checklist feature that pays for the other half. The
 *  panel before the call is a reminder; THIS is the part that stops an
 *  incomplete call becoming an incomplete HubSpot record silently — the
 *  failure the checklist feature exists for: whatever the call did not ask or
 *  cover is missed, no matter what automation runs afterwards.
 *
 *  FOUR THINGS HERE ARE DELIBERATE.
 *
 *  1. IT RENDERS THE QUESTION, NEVER THE `fact_key`. "rate" tells nobody what
 *     to go and chase; "What rate did we quote?" does. The AE is about to email
 *     a client about it.
 *  2. ONE SECTION PER PRODUCT, never a blended score. The failure this feature
 *     exists to catch is an AE treating a three-product call as one thing
 *     (each product is its own lead), so merging two products' coverage into
 *     one percentage would hide the exact thing we are looking for.
 *  3. REQUIRED AND OPTIONAL MISSES ARE DIFFERENT FACTS. A missing required fact
 *     blocks a buildable SOW; a missing optional one is a nice-to-have. Showing
 *     them identically would train the AE to ignore both.
 *  4. NOTHING HERE BLOCKS THE WRAP-UP. Gaps are informational — the AE can
 *     still approve every proposal and close the call. A checklist that refused
 *     to let someone file a call would be worse than the problem it fixes, and
 *     on day one most products have no script at all.
 */
const ScriptGaps: React.FC<{
    sections: ScriptCoverageSection[];
    unscripted: string[];
}> = ({ sections, unscripted }) => {
    // Both empty is the common case — no product picked, or no approved script
    // for what was picked. Render nothing at all rather than an empty shell
    // announcing that there is nothing to say.
    if (sections.length === 0 && unscripted.length === 0) return null;

    const productLabel = (key: string) =>
        key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

    return (
        <div className="space-y-3">
            {sections.map(section => {
                const missed = section.items.filter(i => !i.found);
                const missedRequired = missed.filter(i => i.required);
                const missedOptional = missed.filter(i => !i.required);
                const covered = section.items.length - missed.length;
                return (
                    <div
                        key={section.script_id}
                        className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm"
                    >
                        <div className="flex items-center gap-2 border-b border-gray-100 px-4 py-3">
                            <ListChecks size={16} className="flex-shrink-0 text-brand" />
                            <span className="text-sm font-semibold text-gray-900">
                                {section.name}
                            </span>
                            <span className="ml-auto text-xs text-gray-500">
                                {covered} of {section.items.length} covered
                            </span>
                        </div>
                        {missed.length === 0 ? (
                            <p className="flex items-center gap-2 px-4 py-3 text-sm text-green-700">
                                <CheckCircle2 size={15} className="flex-shrink-0" />
                                Everything on this checklist came up on the call.
                            </p>
                        ) : (
                            <div className="px-4 py-3">
                                {missedRequired.length > 0 && (
                                    <>
                                        <p className="text-xs font-semibold uppercase tracking-wide text-amber-700">
                                            Not discussed — needed for the SOW
                                        </p>
                                        <ul className="mt-2 space-y-1.5">
                                            {missedRequired.map(item => (
                                                <li
                                                    key={item.fact_key}
                                                    className="flex items-start gap-2 text-sm text-gray-800"
                                                >
                                                    <CircleSlash
                                                        size={14}
                                                        className="mt-0.5 flex-shrink-0 text-amber-600"
                                                    />
                                                    <span>{item.prompt_text}</span>
                                                </li>
                                            ))}
                                        </ul>
                                    </>
                                )}
                                {missedOptional.length > 0 && (
                                    <>
                                        <p
                                            className={`text-xs font-semibold uppercase tracking-wide text-gray-500 ${
                                                missedRequired.length > 0 ? 'mt-4' : ''
                                            }`}
                                        >
                                            Also not discussed
                                        </p>
                                        <ul className="mt-2 space-y-1.5">
                                            {missedOptional.map(item => (
                                                <li
                                                    key={item.fact_key}
                                                    className="flex items-start gap-2 text-sm text-gray-600"
                                                >
                                                    <CircleSlash
                                                        size={14}
                                                        className="mt-0.5 flex-shrink-0 text-gray-400"
                                                    />
                                                    <span>{item.prompt_text}</span>
                                                </li>
                                            ))}
                                        </ul>
                                    </>
                                )}
                            </div>
                        )}
                    </div>
                );
            })}

            {/* The split prompt. This fires even with an empty `call_scripts`
                table, so it can arrive with no coverage sections above it —
                which is exactly the day-one case and is not a bug. */}
            {unscripted.length > 0 && (
                <div className="flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4">
                    <Split size={18} className="mt-0.5 flex-shrink-0 text-amber-600" />
                    <div className="min-w-0">
                        <p className="text-sm font-medium text-amber-900">
                            {unscripted.length === 1
                                ? `${productLabel(unscripted[0])} came up on this call, and it was not what the call was for.`
                                : 'Products came up on this call that it was not for: ' +
                                  unscripted.map(productLabel).join(', ') + '.'}
                        </p>
                        <p className="mt-1 text-xs text-amber-900">
                            A lead is one product, so this belongs in its own lead rather than
                            folded into this one. Worth raising before the record is written —
                            splitting it later means reconstructing who wanted what.
                        </p>
                    </div>
                </div>
            )}
        </div>
    );
};

const CARD = 'bg-white rounded-lg border border-gray-200 p-6 shadow-sm';

// ─── the partial-transcript band ─────────────────────────────────────────────
//
// Full literal class strings, like ProposalRow's row tones: an interpolated
// fragment is invisible to the Tailwind JIT and gets purged. AMBER, never red —
// this is not an error, and it is usually the AE's own deliberate choice to stop
// transcription when a call turns sensitive. There is no second variant on
// purpose: the band has exactly one appearance, and `truncated: false` renders
// nothing at all rather than a reassuring badge that would be noise on every
// normal call.
const COVERAGE_BAND =
    'mb-6 flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4';
const COVERAGE_ICON = 'mt-0.5 flex-shrink-0 text-amber-600';
const COVERAGE_TITLE = 'text-sm font-medium text-amber-900';
const COVERAGE_BODY = 'mt-1 text-xs text-amber-800';

/** What happened, in the AE's words, keyed by the reason codes that can arrive
 *  WITH `truncated: true`. Partial on purpose — most coverage reasons are not
 *  truncations and will never reach this band. Passive throughout: Cubie cannot
 *  tell who stopped transcription and has no business implying it. */
const TRUNCATION_CAUSE: Partial<Record<CoverageReason, string>> = {
    truncated: 'Transcription was stopped during the call',
};

/** True of every truncation, whatever caused it. An unrecognised reason code —
 *  a newer backend, an older frontend — still gets an honest sentence here
 *  instead of silence or a raw wire string. */
const NEUTRAL_CAUSE = 'The transcript stops before the end of the call';

// ─── group headings ──────────────────────────────────────────────────────────
//
// The mock's own wording: "Update 3 fields on ABC Corp", "Create lead ·
// …", "Create task · …". Row labels are server-rendered and already carry the
// field/company name, so the heading only counts and frames.
const GROUP_HEADING: Record<ItemKind, (n: number, company: string | null) => string> = {
    vendor_field: (n, company) =>
        `Update ${n} ${n === 1 ? 'field' : 'fields'}${company ? ` on ${company}` : ''}`,
    lead: n => `Create ${n} ${n === 1 ? 'lead' : 'leads'}`,
    task: n => `Create ${n} ${n === 1 ? 'task' : 'tasks'}`,
    sow_request: n => (n === 1 ? 'SOW requested' : `${n} SOW requests`),
    stage_signal: n => (n === 1 ? 'Deal stage signal' : `${n} deal stage signals`),
    contract_expiry: n => (n === 1 ? 'Contract expiry' : `${n} contract expiries`),
};

const KNOWN_KINDS = new Set<string>(ITEM_KIND_ORDER);

const groupHeading = (kind: string, n: number, company: string | null): string => {
    const build = GROUP_HEADING[kind as ItemKind];
    return build ? build(n, company) : `${itemKindLabel(kind)} · ${n}`;
};

/** Server order wins (see api/calls.ts). This only pushes a kind we do not
 *  recognise — a newer backend — to the end rather than dropping it. */
const orderGroups = (groups: WrapUpGroup[]): WrapUpGroup[] => [
    ...groups.filter(g => KNOWN_KINDS.has(g.kind)),
    ...groups.filter(g => !KNOWN_KINDS.has(g.kind)),
];

// ─── row decision state ──────────────────────────────────────────────────────

interface RowState {
    accept: boolean;
    /** `is_overwrite` rows only. */
    confirmed: boolean;
    /** The AE's correction, or null for "send the proposal as-is". */
    edited: string | null;
}

/** A `needs_new_option` row can never be written from here, so it is never
 *  ticked. It still renders — the AE has to see what was heard. */
const isTriaged = (item: WrapUpItem): boolean => Boolean(item.needs_new_option);

const seedRows = (groups: WrapUpGroup[]): Record<string, RowState> => {
    const seed: Record<string, RowState> = {};
    for (const group of groups) {
        for (const item of group.items ?? []) {
            seed[item.id] = {
                accept: !isTriaged(item),
                confirmed: false,
                edited: item.edited_value,
            };
        }
    }
    return seed;
};

/** Only send `edited_value` when it is a real correction: blank, or identical
 *  to the proposal, means "as-is" and the field is omitted. */
const normalizeEdit = (item: WrapUpItem, edited: string | null): string | null => {
    if (edited === null) return null;
    const trimmed = edited.trim();
    if (!trimmed) return null;
    if (trimmed === (item.proposed_value ?? '').trim()) return null;
    return trimmed;
};

// ─── header line ─────────────────────────────────────────────────────────────

const shortDate = (iso: string | null): string | null => {
    const d = parseInstant(iso);
    if (!d) return null;
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
};

// ─── loader shape ────────────────────────────────────────────────────────────
//
// One state object keyed by the session id it belongs to. That is what lets the
// spinner be DERIVED (`ready === null`) instead of stored: a `loading` boolean
// set synchronously in an effect keyed on a changing param is an ERROR under
// `react-hooks/set-state-in-effect`, and a stored one would
// also let a previous session's rows flash on the next session's screen.
interface Loaded {
    sessionId: string;
    data: WrapUpData | null;
    err: CallsError | null;
}

/** One read of the wrap-up, never throwing. Module-level so both the mount
 *  effect and the post-approve re-read use the same path without either one
 *  becoming an effect dependency. A re-read is cheap and safe: the server
 *  stamps `wrap_opened_at` once, on the AE's first read only, and never moves
 *  it — so re-reading cannot shorten or restart the 90-second measure. */
const load = async (sessionId: string): Promise<Loaded> => {
    try {
        const data = await fetchWrapUp(sessionId);
        return { sessionId, data, err: null };
    } catch (e) {
        return { sessionId, data: null, err: callsError(e) };
    }
};

export const WrapUp: React.FC = () => {
    const params = useParams<{ sessionId?: string; id?: string }>();
    const [search] = useSearchParams();
    const sessionId = params.sessionId ?? params.id ?? search.get('session') ?? '';

    const [loaded, setLoaded] = useState<Loaded | null>(null);
    const [rows, setRows] = useState<Record<string, RowState>>({});
    const [results, setResults] = useState<Record<string, ApproveResult>>({});
    const [submitting, setSubmitting] = useState(false);
    const [checkingAnalysisFor, setCheckingAnalysisFor] = useState<string | null>(null);
    const currentSessionId = useRef(sessionId);
    currentSessionId.current = sessionId;
    const checkingAnalysis = checkingAnalysisFor === sessionId;
    // What THIS visit recorded, so the completion banner can say the truth: an
    // approve and a "nothing here" are different completed outcomes, and a
    // session that arrived already wrapped is a third.
    const [outcome, setOutcome] = useState<'approved' | 'nothing_here' | null>(null);
    // The call engagement — the one write that ALWAYS happens, even for
    // "nothing here" and even on a confidential call. It is not a decision, so
    // no row reports it; if it failed, this screen is the only place anyone
    // would ever find out.
    const [callLog, setCallLog] = useState<CallLogResult | null>(null);

    // The wrap-up timing's numerator. Captured at first render, not at
    // first click — the clock the AE experiences starts when the screen appears.
    const openedAtIso = useRef<string>(new Date().toISOString());
    const openedAtMs = useRef<number>(Date.now());

    useEffect(() => {
        if (!sessionId) return;
        let cancelled = false;
        (async () => {
            const next = await load(sessionId);
            if (cancelled) return;
            setLoaded(next);
            setRows(seedRows(next.data?.groups ?? []));
        })();
        return () => {
            cancelled = true;
        };
    }, [sessionId]);

    const ready = loaded && loaded.sessionId === sessionId ? loaded : null;
    const loading = Boolean(sessionId) && ready === null;

    const groups = useMemo(() => orderGroups(ready?.data?.groups ?? []), [ready]);
    const session = ready?.data?.session ?? null;
    const extraction = ready?.data?.extraction;
    const analysisUnavailable =
        extraction?.status === 'pending' || extraction?.status === 'retrying';
    const partialAnalysis = extraction?.status === 'partial';

    const tally = useMemo(() => {
        let selectable = 0;
        let accepted = 0;
        let heldOverwrites = 0;
        for (const group of groups) {
            for (const item of group.items ?? []) {
                if (isTriaged(item)) continue;
                selectable += 1;
                const state = rows[item.id];
                if (!state?.accept) continue;
                accepted += 1;
                if (item.is_overwrite && !state.confirmed) heldOverwrites += 1;
            }
        }
        return { selectable, accepted, heldOverwrites };
    }, [groups, rows]);

    const total = countWrapUpItems(groups);

    // Purely derived from what the server sent — no state, no effect. The
    // server owns the verdict (`truncated`, judged against two settings); this
    // screen only decides how to say it. `coverage` is null on every session
    // ingest never measured, and null renders nothing.
    const coverage = session?.transcript_coverage ?? null;
    const partialTranscript = isTruncatedCoverage(coverage);
    const coveredOf = coverageMinutes(coverage);
    const truncationCause =
        (coverage && isCoverageReason(coverage.reason)
            ? TRUNCATION_CAUSE[coverage.reason]
            : undefined) ?? NEUTRAL_CAUSE;

    // Rows the server handed back as still-claimable: confirm the overwrite,
    // correct the value, look at what changed in HubSpot, or retry the write.
    const unresolved = useMemo(
        () => Object.values(results).filter(r => isUnresolvedResult(r.status)).length,
        [results],
    );

    const complete = isWrapUpComplete({
        session,
        selectable: tally.selectable,
        analysisUnavailable,
        outcomeRecorded: outcome !== null,
        unresolved,
    });
    const locked = submitting || complete || analysisUnavailable;

    const checkAnalysis = async (retry: boolean) => {
        if (!sessionId || checkingAnalysis) return;
        const requestedSessionId = sessionId;
        setCheckingAnalysisFor(requestedSessionId);
        try {
            let retryRequestFailed = false;
            if (retry) {
                try {
                    await retryWrapUpExtraction(requestedSessionId);
                } catch {
                    // The scheduled sweep may have finished between this read
                    // and the click. Re-read even when the retry request failed.
                    retryRequestFailed = true;
                }
            }
            const next = await load(requestedSessionId);
            if (currentSessionId.current !== requestedSessionId) return;
            if (!next.data) throw new Error('analysis refresh failed');
            setLoaded(next);
            setRows(seedRows(next.data.groups ?? []));
            if (next.data.extraction.status === 'retrying') {
                if (retryRequestFailed) toast.error('Could not retry analysis. Please try again.');
                else toast.info('Analysis is still unavailable. Cubie will retry automatically.');
            } else if (next.data.extraction.status === 'complete') {
                toast.success('Suggestions are ready for review.');
            }
        } catch {
            if (currentSessionId.current === requestedSessionId) {
                toast.error('Could not check the analysis. Please try again.');
            }
        } finally {
            setCheckingAnalysisFor(current =>
                current === requestedSessionId ? null : current,
            );
        }
    };

    const setRow = (id: string, patch: Partial<RowState>) =>
        setRows(prev => {
            const base: RowState = prev[id] ?? { accept: false, confirmed: false, edited: null };
            return { ...prev, [id]: { ...base, ...patch } };
        });

    const toggleGroup = (group: WrapUpGroup, next: boolean) =>
        setRows(prev => {
            const out = { ...prev };
            for (const item of group.items ?? []) {
                if (isTriaged(item)) continue;
                out[item.id] = {
                    accept: next,
                    confirmed: prev[item.id]?.confirmed ?? false,
                    edited: prev[item.id]?.edited ?? null,
                };
            }
            return out;
        });

    const submit = async (nothingHere: boolean) => {
        if (!sessionId || analysisUnavailable) return;
        setSubmitting(true);
        try {
            const decisions: ApproveDecision[] = [];
            if (!nothingHere) {
                for (const group of groups) {
                    for (const item of group.items ?? []) {
                        // Deliberately omitted, not sent as a rejection. The AE
                        // did not decline this — HubSpot has no such option yet,
                        // so it stays PENDING for triage. Sending accept:false
                        // would record a decision nobody made and lose the row.
                        if (isTriaged(item)) continue;
                        const state = rows[item.id];
                        const accept = state?.accept ?? false;
                        const edited = normalizeEdit(item, state?.edited ?? null);
                        const decision: ApproveDecision = { item_id: item.id, accept };
                        if (edited !== null) decision.edited_value = edited;
                        // MANDATORY for an overwrite, and sourced ONLY from the
                        // confirmation the AE gave on that one row. Without it
                        // the server answers `needs_confirmation` and writes
                        // nothing — never a blind overwrite of a non-null
                        // HubSpot value. "Approve all" cannot manufacture it: the
                        // flag is per row, and the button is held while any
                        // accepted overwrite lacks one.
                        if (item.is_overwrite && accept && state?.confirmed) {
                            decision.confirm_overwrite = true;
                        }
                        decisions.push(decision);
                    }
                }
            }

            const out = await approveWrapUp(sessionId, {
                decisions,
                ...(nothingHere ? { nothing_here: true } : {}),
                // The wrap-up timing reaches the server here, on every
                // approve, or it is not measured at all.
                wrap_opened_at: openedAtIso.current,
                client_elapsed_ms: Date.now() - openedAtMs.current,
            });

            const byItem: Record<string, ApproveResult> = {};
            for (const r of out.results) byItem[r.item_id] = r;
            setResults(byItem);
            setCallLog(out.call_logged);
            // The server's own verdict, not the request's: it drops
            // `nothing_here` when the same submission accepted something.
            setOutcome(out.nothing_here ? 'nothing_here' : 'approved');

            const stillOpen = out.results.filter(r => isUnresolvedResult(r.status));
            const filed = out.results.filter(
                r =>
                    r.status === 'written' ||
                    r.status === 'already_written' ||
                    r.status === 'recorded',
            ).length;
            const triaged = out.results.filter(r => r.status === 'triage').length;

            // A claimable row means the wrap-up is not finished. Re-read it so
            // the screen shows what the server now holds — a `drift` row comes
            // back re-anchored on HubSpot's current value, and its confirmation
            // is reset, which is the whole point of asking again.
            if (stillOpen.length > 0) {
                const next = await load(sessionId);
                // Only ADOPT a successful re-read. A failed one must not replace
                // the rows the AE is looking at with an error screen — the
                // decisions are already durable on the server, and the outcomes
                // on those rows are the only copy of what just happened.
                if (next.data) {
                    setLoaded(next);
                    setRows(seedRows(next.data.groups ?? []));
                }
            }

            if (stillOpen.length > 0) {
                toast.error(
                    `${stillOpen.length} ${stillOpen.length === 1 ? 'row' : 'rows'} still ` +
                        `${stillOpen.length === 1 ? 'needs' : 'need'} you — each one below says why.`,
                );
            } else if (out.nothing_here) {
                toast.success('Call logged — nothing to file. Wrap-up complete.');
            } else if (filed > 0) {
                toast.success(`${filed} ${filed === 1 ? 'item' : 'items'} filed in HubSpot.`);
            } else if (triaged > 0) {
                toast.info(
                    `${triaged} ${triaged === 1 ? 'value' : 'values'} sent for triage — nothing was written.`,
                );
            } else {
                toast.info('Wrap-up complete — nothing was filed.');
            }

            if (out.call_logged?.status === 'write_failed') {
                toast.error(
                    out.call_logged.message ||
                        'The call itself could not be logged in HubSpot. Log it by hand.',
                );
            }
        } catch (e) {
            const err = callsError(e, 'Approving the wrap-up failed.');
            if (isNotEnabled(err) || isSessionNotFound(err)) toast.info(err.message);
            else toast.error(err.message);
        } finally {
            setSubmitting(false);
        }
    };

    const onNothingHere = async () => {
        if (total > 0) {
            const ok = await confirmDialog({
                title: 'Log this call with nothing to file?',
                message:
                    `Cubie found ${total} ${total === 1 ? 'proposal' : 'proposals'} on this call.\n\n` +
                    '"Nothing here" records that none of them were worth filing. That is a real ' +
                    'answer, not a skip — the wrap-up counts as complete. It only returns ' +
                    'if more transcript arrives, and the answer helps the next call.',
                confirmLabel: 'Yes, nothing to file',
                cancelLabel: 'Keep reviewing',
            });
            if (!ok) return;
        }
        await submit(true);
    };

    // ─── shells ──────────────────────────────────────────────────────────────

    if (!sessionId) {
        return (
            <PageContainer>
                <EosPageHeader icon={<PhoneCall size={20} />} title="Call wrap-up" />
                <ErrorState message="No call was named in the link." />
            </PageContainer>
        );
    }

    const subtitleParts = [
        session?.ae_name,
        session?.duration_minutes != null ? `${session.duration_minutes} min` : null,
        shortDate(session?.ended_at ?? null),
    ].filter((p): p is string => Boolean(p));

    const approveLabel =
        tally.accepted === tally.selectable && tally.selectable > 0
            ? 'Approve all'
            : tally.accepted === 0
              ? `Reject all ${tally.selectable}`
              : `Approve ${tally.accepted} of ${tally.selectable}`;

    return (
        <PageContainer>
            <div data-tour="wrapup-extraction-status">
            <EosPageHeader
                icon={<PhoneCall size={20} />}
                title={
                    session?.company_name
                        ? `Call with ${session.company_name}`
                        : 'Call wrap-up'
                }
                subtitle={subtitleParts.length ? subtitleParts.join(' · ') : undefined}
                right={
                    <>
                        {/* The hand-off to the Proposal Generator. Renders nothing
                            for a reader who cannot reach it or is not the AE. */}
                        {sessionId && session?.can_read_transcript && (
                            <DraftProposalCta sessionId={sessionId} />
                        )}
                        <BackButton fallback="/sales" label="My Day" />
                    </>
                }
            />
            </div>

            {loading && <LoadingState label="Opening the wrap-up…" />}

            {ready?.err && isNotEnabled(ready.err) && (
                <EmptyState
                    title="Call capture is not enabled yet."
                    hint="Nothing is wrong with your account. When Sales Call Capture is switched on, wrap-ups appear here and at the top of My Day."
                />
            )}

            {/* A 404 that named a call rather than the flag. "We can't find that
                call" and "the feature is off" are different sentences, and this
                is the one the link was wrong for. */}
            {ready?.err && isSessionNotFound(ready.err) && (
                <EmptyState
                    title="Cubie cannot find that call."
                    hint="The link may be wrong, or the call may have been purged. Open the wrap-up from the top of My Day instead."
                />
            )}

            {ready?.err && !isNotEnabled(ready.err) && !isSessionNotFound(ready.err) && (
                <ErrorState message={ready.err.message} />
            )}

            {ready?.data && (
                <>
                    {complete && (
                        <div className="mb-6 flex items-start gap-3 rounded-lg border border-green-200 bg-green-50 p-4">
                            <CheckCircle2
                                size={18}
                                className="mt-0.5 flex-shrink-0 text-green-600"
                            />
                            <div>
                                <p className="text-sm font-medium text-green-900">
                                    Wrap-up complete.
                                </p>
                                <p className="mt-1 text-xs text-green-800">
                                    {outcome === 'nothing_here' ||
                                    session?.nothing_here ||
                                    total === 0
                                        ? 'This call is logged with nothing to file. That is a recorded answer, not a gap. It only returns if more transcript arrives.'
                                        : outcome === 'approved'
                                          ? 'Every row below shows what happened to it. Nothing else is waiting on you for this call.'
                                          : 'This call was wrapped up already — the rows below are the record of what was proposed.'}
                                </p>
                                {(callLog?.status === 'written' ||
                                    callLog?.status === 'already_written') && (
                                    <p className="mt-1 text-xs text-green-800">
                                        The call itself is logged on the HubSpot record.
                                    </p>
                                )}
                            </div>
                        </div>
                    )}

                    {/* Recorded, but not finished. The session IS wrapped — the
                        call is logged and it has left the pending list — and
                        these rows are still the AE's, so the screen stays
                        usable rather than pretending either that nothing
                        happened or that everything did. */}
                    {outcome !== null && unresolved > 0 && (
                        <div className="mb-6 flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4">
                            <TriangleAlert
                                size={18}
                                className="mt-0.5 flex-shrink-0 text-amber-600"
                            />
                            <div>
                                <p className="text-sm font-medium text-amber-900">
                                    The call is logged. {unresolved}{' '}
                                    {unresolved === 1 ? 'row' : 'rows'}{' '}
                                    {unresolved === 1 ? 'still needs' : 'still need'} you.
                                </p>
                                <p className="mt-1 text-xs text-amber-800">
                                    Each one below says what happened — a value HubSpot changed
                                    since Cubie proposed it, a value it would not accept, a
                                    confirmation it needs, or a write it refused. Fix and approve
                                    again; nothing is written twice.
                                </p>
                            </div>
                        </div>
                    )}

                    {/* The call engagement is the write that always happens, and
                        no row reports it. A failure here means the CRM has no
                        record of the call at all — the one thing this feature
                        exists to guarantee — so it is stated, not toasted away. */}
                    {callLog?.status === 'write_failed' && (
                        <div className="mb-6 flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 p-4">
                            <TriangleAlert size={18} className="mt-0.5 flex-shrink-0 text-red-600" />
                            <div>
                                <p className="text-sm font-medium text-red-900">
                                    The call itself could not be logged in HubSpot.
                                </p>
                                <p className="mt-1 text-xs text-red-800">
                                    {callLog.message ||
                                        'HubSpot refused the call log. Log the call by hand, or approve again once HubSpot is reachable.'}
                                </p>
                            </div>
                        </div>
                    )}

                    {analysisUnavailable && (
                        <div className="mb-6 flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4" role="status">
                            <TriangleAlert size={18} className="mt-0.5 flex-shrink-0 text-amber-600" />
                            <div>
                                <p className="text-sm font-medium text-amber-900">
                                    {extraction?.status === 'retrying'
                                        ? 'Cubie could not finish analyzing this call.'
                                        : 'Cubie is preparing suggestions for this call.'}
                                </p>
                                <p className="mt-1 text-xs text-amber-800">
                                    {extraction?.status === 'retrying'
                                        ? 'The transcript is saved and Cubie will retry automatically. No suggestions are ready to approve yet. If the AI provider was fixed, retry now.'
                                        : 'No suggestions are ready to approve yet. This call stays on My Day while analysis finishes.'}
                                </p>
                                <SecondaryButton
                                    onClick={() => void checkAnalysis(Boolean(extraction?.can_retry))}
                                    disabled={checkingAnalysis}
                                >
                                    <RefreshCw size={14} className="mr-1 inline" />
                                    {checkingAnalysis
                                        ? 'Checking…'
                                        : extraction?.can_retry
                                          ? 'Retry analysis'
                                          : 'Check again'}
                                </SecondaryButton>
                            </div>
                        </div>
                    )}

                    {partialAnalysis && (
                        <div className="mb-6 flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4" role="status">
                            <TriangleAlert size={18} className="mt-0.5 flex-shrink-0 text-amber-600" />
                            <div>
                                <p className="text-sm font-medium text-amber-900">
                                    Some of this call was not analyzed.
                                </p>
                                <p className="mt-1 text-xs text-amber-800">
                                    {extraction?.chunks_failed} of {extraction?.chunks_total} transcript sections could not be analyzed. Review the suggestions below and add anything missing to HubSpot yourself before closing the wrap-up.
                                </p>
                            </div>
                        </div>
                    )}

                    {/* Transcript text is a FIELD-level permission, separate
                        from the right to open the row: a manager reads their
                        team's quotes, the supervisor reads none. Say it once,
                        rather than rendering rows whose evidence is silently
                        missing. Deliberately makes no claim about WHO is
                        reading — the supervisor fails this gate on their own
                        calls too, so "you are not the AE" would be a lie. */}
                    {session && !session.can_read_transcript && (
                        <div className="mb-6 flex items-start gap-3 rounded-lg border border-gray-200 bg-gray-50 p-4">
                            <EyeOff size={18} className="mt-0.5 flex-shrink-0 text-gray-400" />
                            <p className="text-xs text-gray-700">
                                This is what Cubie proposed from the call, without the transcript
                                lines behind it — quote text is restricted.{' '}
                                <span className="font-medium">
                                    Approving a wrap-up is the AE&rsquo;s own action
                                    {session.ae_name ? `, and this call is ${session.ae_name}'s` : ''}
                                    .
                                </span>
                            </p>
                        </div>
                    )}

                    {session?.status === 'no_transcript' && (
                        <div className="mb-6 flex items-start gap-3 rounded-lg border border-gray-200 bg-gray-50 p-4">
                            <FileText size={18} className="mt-0.5 flex-shrink-0 text-gray-400" />
                            <p className="text-xs text-gray-700">
                                No transcript reached Cubie for this call — the host may have had
                                transcription off, the meeting may have run under a minute, or it
                                was a confidential call.{' '}
                                <span className="font-medium">
                                    The call itself is still logged.
                                </span>
                            </p>
                        </div>
                    )}

                    {/* A transcript that stops short of the call. LAST of the
                        banners and first thing above the rows: the AE has to
                        read this BEFORE they start ticking, not after they have
                        approved. Informative, never accusatory — stopping
                        transcription when a call turns to pricing or a
                        complaint is a legitimate thing to do, and the only
                        problem is that the wrap-up would otherwise look
                        complete. */}
                    {partialTranscript && (
                        <div className={COVERAGE_BAND}>
                            <CaptionsOff size={18} className={COVERAGE_ICON} />
                            <div>
                                <p className={COVERAGE_TITLE}>
                                    {coveredOf
                                        ? `This transcript covers ${coveredOf.covered} of ${coveredOf.call} minutes.`
                                        : 'This transcript does not cover the whole call.'}
                                </p>
                                <p className={COVERAGE_BODY}>
                                    {truncationCause}, so anything said afterwards was not read.{' '}
                                    {total > 0
                                        ? 'The proposals below come only from the captured part — add anything from the rest of the call by hand.'
                                        : 'Nothing was proposed from the captured part, so anything agreed later needs adding by hand.'}
                                </p>
                            </div>
                        </div>
                    )}

                    {analysisUnavailable ? null : total === 0 ? (
                        <div className={CARD}>
                            <h2 className="text-base font-semibold text-gray-900">
                                {partialAnalysis
                                    ? 'No suggestions from the sections Cubie analyzed.'
                                    : 'Nothing to file from this call.'}
                            </h2>
                            <p className="mt-2 text-sm text-gray-600">
                                {partialAnalysis
                                    ? 'Some transcript sections were missed. Check the call for anything that needs filing before you log it.'
                                    : 'Cubie found no vendor fields, leads, tasks or commitments worth proposing. The call is on the record either way — a call is never invisible even when its content is.'}
                            </p>
                            {!complete && (
                                <div className="mt-5">
                                    <PrimaryButton
                                        onClick={() => void submit(true)}
                                        busy={submitting}
                                        disabled={locked}
                                    >
                                        Log the call — nothing to file
                                    </PrimaryButton>
                                </div>
                            )}
                        </div>
                    ) : (
                        <>
                            {/* Before the rows, for the same reason the partial-
                                transcript band is: the AE has to know what the
                                call MISSED before they start ticking what it
                                caught. Afterwards it reads as an epilogue to a
                                decision already made. */}
                            {/* The prospect asked for a proposal on this call. The
                                SOW clock is already running in the Cockpit; this is
                                the shortest path to the thing the clock is waiting
                                for. Same button as the header, primary here. */}
                            {sessionId &&
                                session?.can_read_transcript &&
                                groups.some(g => g.kind === 'sow_request') && (
                                    <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-brand/20 bg-red-50/60 p-4">
                                        <div className="flex items-start gap-3">
                                            <FileText size={18} className="mt-0.5 flex-shrink-0 text-brand" />
                                            <div>
                                                <p className="text-sm font-medium text-gray-900">
                                                    The prospect asked for a proposal on this call.
                                                </p>
                                                <p className="mt-0.5 text-xs text-gray-600">
                                                    Cubie can draft it from what was said. You confirm the
                                                    facts first; nothing is sent by Cubie.
                                                </p>
                                            </div>
                                        </div>
                                        <DraftProposalCta sessionId={sessionId} emphasized />
                                    </div>
                                )}
                            <div className="mb-4">
                                <ScriptGaps
                                    sections={ready?.data?.script_coverage ?? []}
                                    unscripted={ready?.data?.unscripted_products ?? []}
                                />
                            </div>
                            <div className="space-y-4" data-tour="wrapup-proposals">
                                {groups.map(group => {
                                    const items = group.items ?? [];
                                    const selectable = items.filter(i => !isTriaged(i));
                                    const on = selectable.filter(i => rows[i.id]?.accept).length;
                                    const all = selectable.length > 0 && on === selectable.length;
                                    const some = on > 0 && !all;
                                    return (
                                        <section
                                            key={group.kind}
                                            className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm"
                                        >
                                            <header className="flex items-center gap-3 border-b border-gray-200 bg-gray-50 px-4 py-2.5">
                                                <input
                                                    type="checkbox"
                                                    checked={all}
                                                    ref={el => {
                                                        if (el) el.indeterminate = some;
                                                    }}
                                                    disabled={locked || selectable.length === 0}
                                                    onChange={e =>
                                                        toggleGroup(group, e.target.checked)
                                                    }
                                                    aria-label={`Include every item in: ${groupHeading(
                                                        group.kind,
                                                        items.length,
                                                        session?.company_name ?? null,
                                                    )}`}
                                                    className="h-4 w-4 rounded border-gray-300 text-brand focus:ring-brand disabled:opacity-40"
                                                />
                                                <h2 className="text-sm font-semibold text-gray-900">
                                                    {groupHeading(
                                                        group.kind,
                                                        items.length,
                                                        session?.company_name ?? null,
                                                    )}
                                                </h2>
                                                {some && (
                                                    <span className="text-xs text-gray-500">
                                                        {on} of {selectable.length} ticked
                                                    </span>
                                                )}
                                            </header>
                                            <div className="divide-y divide-gray-100">
                                                {items.map(item => (
                                                    <ProposalRow
                                                        key={item.id}
                                                        item={item}
                                                        checked={rows[item.id]?.accept ?? false}
                                                        onChange={v =>
                                                            setRow(item.id, { accept: v })
                                                        }
                                                        confirmed={
                                                            rows[item.id]?.confirmed ?? false
                                                        }
                                                        onConfirm={v =>
                                                            setRow(item.id, { confirmed: v })
                                                        }
                                                        edited={rows[item.id]?.edited ?? null}
                                                        onEdit={v => setRow(item.id, { edited: v })}
                                                        result={results[item.id] ?? null}
                                                        locked={locked}
                                                    />
                                                ))}
                                            </div>
                                        </section>
                                    );
                                })}
                            </div>

                            {!complete && (
                                <div className="sticky bottom-4 z-10 mt-6 rounded-lg border border-gray-200 bg-white/95 p-4 shadow-md backdrop-blur" data-tour="wrapup-actions">
                                    {tally.heldOverwrites > 0 && (
                                        <p className="mb-3 text-xs font-medium text-amber-800">
                                            {tally.heldOverwrites}{' '}
                                            {tally.heldOverwrites === 1 ? 'row' : 'rows'} would
                                            replace a value HubSpot already holds. Confirm{' '}
                                            {tally.heldOverwrites === 1 ? 'it' : 'them'} above, or
                                            untick, before approving.
                                        </p>
                                    )}
                                    <div className="flex flex-wrap items-center gap-3">
                                        <PrimaryButton
                                            onClick={() => void submit(false)}
                                            busy={submitting}
                                            disabled={locked || tally.heldOverwrites > 0}
                                        >
                                            {approveLabel}
                                        </PrimaryButton>
                                        <SecondaryButton
                                            onClick={() => void onNothingHere()}
                                            disabled={locked}
                                        >
                                            Nothing here
                                        </SecondaryButton>
                                        <p className="text-xs text-gray-500">
                                            <span className="font-medium">Nothing here</span> is an
                                            answer, not a cancel — it logs the call as reviewed
                                            with nothing worth filing.
                                        </p>
                                    </div>
                                </div>
                            )}
                        </>
                    )}
                </>
            )}
        </PageContainer>
    );
};

export default WrapUp;
