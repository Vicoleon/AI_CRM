// One of today's client calls, straight off the AE's calendar.
//
// THE ROW'S PRIMARY JOB IS THE COMPANY BINDING. Arming transcription is
// secondary and best-effort — a conference already running cannot be armed, a
// client-hosted space lives in another Workspace and cannot be patched at all,
// and a recurring series shares ONE space. The binding is the part that always
// works, and it is the part that lets the wrap-up write to the right CRM record
// without asking. So a row whose arming failed is STILL worth capturing, and
// the two verdicts are rendered separately on purpose.
//
// FIVE THINGS HERE ARE DELIBERATE:
//
// 1. The company is a CONFIRMABLE SUGGESTION, never a silent binding. Only
//    `match === "unique"` arrives with a `hubspot_company_id`, and only that
//    state pre-selects. Two candidates means a human taps one — the server
//    withholds the id precisely so no client can reproduce the `candidates[0]`
//    bug, and reading `candidates[0]` here would smuggle it straight back in.
// 2. Capture is DISABLED until a company is bound, with the reason on the row.
//    An unbound capture produces a wrap-up that can write nowhere, which is the
//    silent no-op this whole feature is shaped to avoid. Join and Prep stay
//    available regardless: a row is never useless for lacking a binding.
// 3. Confidential means TWO things on a calendar row, and the copy says both:
//    the call is logged and never transcribed, AND Cubie does not ask Google to
//    transcribe it at all. It cannot be undone, so it asks once before use.
// 4. `meet_uri` is nullable-safe everywhere. Never `window.open(null)`.
// 5. A row that already has a session shows Join and the wrap-up, never Capture
//    again. `POST /start` is idempotent at the DATABASE on
//    `UNIQUE(ae_email, calendar_event_id)`, so a second press could not double-
//    book — but it also would not re-arm, and offering a button that does
//    nothing visible is its own kind of lie.
// 6. THE CHECKLIST SITS ABOVE THE ARMING BADGE, and that placement is load-
//    bearing rather than aesthetic. The arming verdict CHANGES when the call is
//    captured (`captured.arming` replaces `meeting.arming`, with its own
//    paragraph appearing or vanishing), so anything rendered below it jumps down
//    the page at the exact moment the AE is joining the call and reading the
//    questions. Reading order agrees with it anyway: which client, which
//    products, what to ask, then capture.
//
// AND THE PRODUCTS THEMSELVES. The AE picks them here, before the call, and
// the pick is remembered per (AE, company) so next week's call with the same
// client defaults to the same products. Three rules the rest of this file must
// not undo:
//   * The pick is a LIST. A call that surfaces three products should produce
//     three leads, one per product, so nothing here ever collapses several
//     products into one.
//   * `scripts: null` is the server's fail-soft answer, NOT an error state. The
//     row renders exactly as it did before this feature shipped, and `start`
//     is sent WITHOUT `product_keys` — omitted and empty are different requests
//     server-side, and empty would wipe the AE's remembered pick for a client on
//     a row that never offered them a picker to answer with.
//   * Nothing about scripts can block Capture. Most products have no checklist
//     on day one; the tables ship empty on purpose.
//   * A pick's checklist comes from the DAY's catalogue, not only the row's
//     `sections`. Those hold what is remembered for this client, and a first
//     call has nothing remembered — resolved against them alone, every product
//     the AE ticked read "no approved checklist" until the call after.
import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
    Building2,
    ClipboardList,
    ExternalLink,
    Radio,
    Repeat,
    ShieldOff,
    Sparkles,
    Users,
} from 'lucide-react';
import {
    canPrepMeeting,
    checklistsFor,
    companyOptionLabel,
    meetingScripts,
    type ArmingVerdict,
    type CallsError,
    type CaptureConsent,
    type CompanyOption,
    type ScheduledMeeting,
    type ScriptSection,
} from '../../api/calls';
import { confirmDialog } from '../eos/ConfirmDialog';
import { MEET_PANEL_HINT, meetJoinUrl, type MeetHandoff } from '../../meetAddon/joinLink';
import { PrimaryButton } from '../smartTeams/shared';
import { ArmingBadge } from './ArmingBadge';
import { CompanyPicker } from './CompanyPicker';
import { PreCallPanel } from './PreCallPanel';
import { ProductPicker } from './ProductPicker';
import { companyPrompt, isImminent, startsInLabel } from './verdictCopy';

/** What `POST /api/calls/start` gave back for THIS row in this session. Held by
 *  the page rather than the row so it survives a re-sort, and so the page can
 *  count the day's outcomes. */
export interface CapturedCall {
    call_session_id: string;
    arming: ArmingVerdict;
    origin: string;
    /** What we sent, not what came back — `capture_consent` is not echoed. */
    confidential: boolean;
}

export interface MeetingRowProps {
    meeting: ScheduledMeeting;
    /** The day's `script_catalogue` — every live checklist, so a product the AE
     *  ticks shows its checklist whether or not this client remembers it.
     *  Required, not optional: a caller that forgot it would bring back the
     *  "no approved checklist" first-call bug without a type error. */
    scriptCatalogue: readonly ScriptSection[] | null;
    /** Non-null once this row has been captured during this visit. */
    captured: CapturedCall | null;
    /** This row's POST is in flight. */
    busy: boolean;
    /** Another row's POST is in flight — the lock is there to look sane, not to
     *  be safe (the endpoint is idempotent at the database). */
    locked: boolean;
    /** This row's own failed capture. Rendered ON THE ROW: a page-level banner
     *  cannot say which of eight meetings it is about. */
    error: CallsError | null;
    /** Set when this AE has Cubie in Google Meet: Join then opens Meet on their
     *  work account (where the add-on is installed) and the row says where the
     *  checklist lives once they are in the call. Null or absent leaves the row
     *  exactly as it was. */
    meetAddon?: MeetHandoff | null;
    onCapture: (
        meeting: ScheduledMeeting,
        args: {
            hubspot_company_id: string;
            capture_consent: CaptureConsent;
            /** The AE's product pick, in pick order — or UNDEFINED when this row
             *  had no picker to answer with (`scripts: null`). The page must
             *  pass it straight through: `[]` clears the remembered pick for
             *  this client server-side, `undefined` leaves last week's alone,
             *  and the two must not be conflated on the way to the wire. */
            product_keys?: string[];
        },
    ) => void;
}

// Full literal class strings — an interpolated fragment is purged by the JIT.
const ROW = 'rounded-lg border border-gray-200 bg-white p-5 shadow-sm';
const ROW_LIVE = 'rounded-lg border-2 border-brand bg-white p-5 shadow-sm';
const PROMPT_AMBER = 'rounded-md border border-amber-200 bg-amber-50 p-3';
const PROMPT_GRAY = 'rounded-md border border-gray-200 bg-gray-50 p-3';
const CONSENT_ON = 'mt-4 rounded-md border border-amber-300 bg-amber-50 p-3';
const CONSENT_OFF = 'mt-4 rounded-md border border-gray-200 bg-gray-50 p-3';
const JOIN_BTN =
    'inline-flex items-center justify-center gap-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2';
const GHOST_BTN =
    'inline-flex items-center justify-center gap-2 rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2';

interface Bound {
    id: string;
    label: string;
    domain: string | null;
}

const CONFIDENTIAL_CONSEQUENCE =
    'Cubie will log that this call happened — who, when, and against which company — and ' +
    'nothing else. It does not ask Google to transcribe the meeting, captures no transcript, ' +
    'extracts nothing and offers no wrap-up. This cannot be undone: a confidential call can ' +
    'never be un-suppressed afterwards.';

export const MeetingRow: React.FC<MeetingRowProps> = ({
    meeting,
    scriptCatalogue,
    captured,
    busy,
    locked,
    error,
    meetAddon = null,
    onCapture,
}) => {
    const navigate = useNavigate();

    // The AE's own choice for this row, when they made one. Null means "use the
    // server's suggestion, if it gave one".
    const [override, setOverride] = useState<Bound | null>(null);
    // The AE pressed "change" on a suggestion: drop back to the picker without
    // losing the suggestion, which is still what the server thinks.
    const [repicking, setRepicking] = useState(false);
    const [confidential, setConfidential] = useState(false);
    // The AE's product pick for this row. NULL means "they have not touched it",
    // which is a different fact from "they picked nothing" — the first falls
    // back to what the server remembers for this client, the second is an empty
    // list the AE chose and which clears that memory. DERIVED into `selected`
    // below rather than seeded from props in an effect: a `setState` inside an
    // effect keyed on a prop is an ERROR under `react-hooks/set-state-in-effect`,
    // and it would also stomp the AE's choice on every re-render of the day.
    const [picked, setPicked] = useState<string[] | null>(null);

    const company = meeting.company;
    // ONLY `unique` carries an id. This is the whole contract — see note 1.
    const suggested: Bound | null =
        company?.match === 'unique' && company.hubspot_company_id
            ? {
                  id: company.hubspot_company_id,
                  label: company.name || company.domain || company.hubspot_company_id,
                  domain: company.domain,
              }
            : null;
    const bound = override ?? (repicking ? null : suggested);

    const sessionId = captured?.call_session_id ?? meeting.call_session_id;
    const hasSession = Boolean(sessionId) || meeting.capture_state !== 'not_started';
    const arming = captured?.arming ?? meeting.arming;
    const wasConfidential = captured?.confidential ?? false;

    // The checklist block. `null` — the server could not read the script tables,
    // or this is an older payload — renders the row exactly as it did before
    // this feature existed. It is not a broken state and gets no error copy.
    const scripts = meetingScripts(meeting);
    const selected = picked ?? scripts?.selected_product_keys ?? [];
    // "Remembered from your last call with this client" is only TRUE while the
    // AE has neither touched the picker nor rebound the company: the memory is
    // keyed on (AE, company), so the moment they pick a different company the
    // sentence stops being about the company on the row.
    const rememberedPick = picked === null && override === null && !repicking;

    const prompt = companyPrompt(meeting);
    const countdown = startsInLabel(meeting);
    const imminent = isImminent(meeting);
    const externals = (meeting.attendees ?? []).filter(a => a.is_external && !a.is_self);
    const joinable = typeof meeting.meet_uri === 'string' && meeting.meet_uri.length > 0;
    // Google gives an add-on no way to open itself, so the hand-off is the link
    // (right account) plus one honest sentence (where to click) — never a claim
    // that the checklist will simply appear.
    const joinHref =
        joinable && meeting.meet_uri
            ? meetAddon
                ? meetJoinUrl(meeting.meet_uri, meetAddon.accountEmail)
                : meeting.meet_uri
            : null;
    const showMeetHint = Boolean(meetAddon && joinHref && meeting.capture_state !== 'wrapped');
    // `meet_meeting_code` is REQUIRED alongside `calendar_event_id` — the router
    // 422s without it, because it is the only address `spaces.get` accepts for a
    // space Cubie did not create. Falling back to the ad-hoc start instead would
    // hand the AE a SECOND Meet link for a meeting that already has one, with
    // the client sitting in the other. So the row says so and offers Join.
    const codeMissing = !meeting.meeting_code;

    const pick = (option: CompanyOption) => {
        setOverride({
            id: option.hubspot_company_id,
            label: companyOptionLabel(option),
            domain: option.domain || null,
        });
        setRepicking(false);
    };

    const capture = async () => {
        if (!bound) return;
        if (confidential) {
            const ok = await confirmDialog({
                title: 'Capture this call as confidential?',
                message: `${CONFIDENTIAL_CONSEQUENCE}\n\nThe call stays bound to ${bound.label}, so it is still on the record — only its content is never captured.`,
                confirmLabel: 'Yes, capture confidentially',
                cancelLabel: 'Back',
            });
            if (!ok) return;
        }
        onCapture(meeting, {
            hubspot_company_id: bound.id,
            capture_consent: confidential ? 'suppressed' : 'granted',
            // Omitted, NOT empty, when there was no picker: an empty list is the
            // AE deselecting every product and it CLEARS their remembered pick
            // for this client. A row that never showed them a picker has not
            // been answered and must not overwrite last week's answer.
            product_keys: scripts ? [...selected] : undefined,
        });
    };

    const openPrep = () => {
        // Same pattern as the daily briefing's calendar column, so the two
        // calendar surfaces reach the AI prep flow the same way.
        navigate(`/meeting-prep/${encodeURIComponent(meeting.google_event_id)}`, {
            state: {
                event: {
                    event_id: meeting.google_event_id,
                    title: meeting.title,
                    start: meeting.start,
                    end: meeting.end,
                    all_day: false,
                    rsvp: meeting.rsvp,
                },
            },
        });
    };

    return (
        <li className={imminent ? ROW_LIVE : ROW}>
            {/* ── when and what ─────────────────────────────────────────── */}
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="text-sm font-semibold text-gray-900">
                    {meeting.start_local || '—'}
                </span>
                {countdown && (
                    <span
                        className={
                            meeting.is_live
                                ? 'inline-flex items-center gap-1 rounded-full bg-brand px-2 py-0.5 text-[11px] font-semibold text-white'
                                : 'text-xs text-gray-500'
                        }
                    >
                        {meeting.is_live && <Radio size={11} />}
                        {countdown}
                    </span>
                )}
                {meeting.recurring_event_id && (
                    <span className="inline-flex items-center gap-1 text-[11px] text-gray-400">
                        <Repeat size={11} /> recurring
                    </span>
                )}
                {meeting.rsvp === 'needs_action' && (
                    <span className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700">
                        not answered
                    </span>
                )}
            </div>
            <p className="mt-0.5 text-base font-medium text-gray-900">{meeting.title}</p>

            {/* ── who from outside the company ──────────────────────────── */}
            {externals.length > 0 && (
                <p className="mt-1.5 flex items-start gap-1.5 text-xs text-gray-500">
                    <Users size={13} className="mt-0.5 flex-shrink-0 text-gray-400" />
                    <span className="min-w-0">
                        {externals
                            .map(a => {
                                const who = a.display_name || a.email;
                                return a.is_optional ? `${who} (optional)` : who;
                            })
                            .join(', ')}
                    </span>
                </p>
            )}

            {/* ── the company: a suggestion to confirm, never a binding ─── */}
            <div className="mt-4">
                {bound ? (
                    <div className="flex items-center gap-3 rounded-md border border-gray-300 bg-gray-50 px-3 py-2">
                        <Building2 size={16} className="flex-shrink-0 text-brand" />
                        <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm font-medium text-gray-900">
                                {bound.label}
                            </span>
                            {bound.domain && (
                                <span className="block truncate text-xs text-gray-500">
                                    {bound.domain}
                                    {!override && ' · matched from the guest’s email'}
                                </span>
                            )}
                        </span>
                        {!hasSession && (
                            <button
                                type="button"
                                onClick={() => {
                                    setOverride(null);
                                    setRepicking(true);
                                }}
                                className="flex-shrink-0 text-xs font-medium text-brand hover:underline"
                            >
                                change
                            </button>
                        )}
                    </div>
                ) : hasSession ? (
                    // A session exists and we do not know its company from this
                    // payload. Do not invent one, and do not offer a picker that
                    // cannot rebind it — say what is true.
                    <p className="text-xs text-gray-500">
                        This call is already captured. Its HubSpot binding is on the wrap-up.
                    </p>
                ) : (
                    <div className={prompt?.tone === 'gray' ? PROMPT_GRAY : PROMPT_AMBER}>
                        {prompt && (
                            <>
                                <p className="text-sm font-medium text-gray-900">
                                    {prompt.headline}
                                </p>
                                <p className="mt-1 text-xs leading-relaxed text-gray-600">
                                    {prompt.hint}
                                </p>
                            </>
                        )}

                        {/* Candidates, when HubSpot offered more than one. One
                            tap each, nothing pre-selected. */}
                        {(company?.candidates?.length ?? 0) > 0 && (
                            <ul className="mt-3 space-y-1.5">
                                {company.candidates.map(candidate => (
                                    <li key={candidate.hubspot_company_id}>
                                        <button
                                            type="button"
                                            onClick={() =>
                                                pick({
                                                    hubspot_company_id:
                                                        candidate.hubspot_company_id,
                                                    name: candidate.name,
                                                    domain: candidate.domain,
                                                    client_status: candidate.lifecyclestage,
                                                })
                                            }
                                            className="flex w-full items-center gap-2.5 rounded-md border border-gray-300 bg-white px-3 py-2 text-left transition-colors hover:border-brand hover:bg-brand-tint"
                                        >
                                            <Building2
                                                size={15}
                                                className="flex-shrink-0 text-gray-400"
                                            />
                                            <span className="min-w-0 flex-1">
                                                <span className="block truncate text-sm font-medium text-gray-900">
                                                    {companyOptionLabel(candidate)}
                                                </span>
                                                {(candidate.domain ||
                                                    candidate.lifecyclestage) && (
                                                    <span className="block truncate text-xs text-gray-500">
                                                        {[
                                                            candidate.domain,
                                                            candidate.lifecyclestage,
                                                        ]
                                                            .filter(Boolean)
                                                            .join(' · ')}
                                                    </span>
                                                )}
                                            </span>
                                        </button>
                                    </li>
                                ))}
                            </ul>
                        )}

                        <div className="mt-3">
                            <CompanyPicker
                                inputId={`ccs-company-${meeting.google_event_id}`}
                                label={
                                    (company?.candidates?.length ?? 0) > 0
                                        ? 'Not one of these? Search HubSpot'
                                        : 'Company'
                                }
                                onPick={pick}
                                onCancel={
                                    repicking && suggested
                                        ? () => setRepicking(false)
                                        : undefined
                                }
                                cancelLabel={
                                    suggested
                                        ? `Keep ${suggested.label}`
                                        : 'Cancel'
                                }
                            />
                        </div>
                    </div>
                )}
            </div>

            {/* ── what this call is about, and what it has to cover ───────
                ABOVE the arming badge on purpose — see note 6. `scripts: null`
                renders nothing at all here, which is the whole of the fail-soft
                contract: no panel, no picker, no error. */}
            {scripts && (
                <>
                    <ProductPicker
                        idPrefix={`ccs-product-${meeting.google_event_id}`}
                        available={scripts.available}
                        selected={selected}
                        onChange={setPicked}
                        frozen={hasSession}
                        pickSent={captured !== null}
                        remembered={rememberedPick}
                    />
                    <PreCallPanel
                        selected={selected}
                        sections={checklistsFor(selected, scripts.sections, scriptCatalogue)}
                        hasSession={hasSession}
                        confidential={confidential || wasConfidential}
                    />
                </>
            )}

            {/* ── transcription: honest, and never silent ────────────────── */}
            <div className="mt-3">
                <ArmingBadge verdict={arming} hasSession={hasSession} />
            </div>

            {/* ── confidential, with its consequence on the row ──────────── */}
            {!hasSession && (
                <div className={confidential ? CONSENT_ON : CONSENT_OFF}>
                    <label className="flex items-start gap-3">
                        <input
                            type="checkbox"
                            checked={confidential}
                            onChange={e => setConfidential(e.target.checked)}
                            className="mt-0.5 h-4 w-4 flex-shrink-0 rounded border-gray-300 text-brand focus:ring-brand"
                        />
                        <span>
                            <span className="flex items-center gap-1.5 text-sm font-medium text-gray-900">
                                <ShieldOff size={15} className="text-amber-600" />
                                Confidential call
                            </span>
                            <span className="mt-1 block text-xs leading-relaxed text-gray-600">
                                The call is logged against the company but never transcribed —
                                Cubie does not ask Google to transcribe it, nothing is
                                extracted, no wrap-up is offered, and it cannot be un-suppressed
                                later.
                            </span>
                        </span>
                    </label>
                </div>
            )}

            {wasConfidential && (
                <p className="mt-3 flex items-start gap-1.5 text-xs text-amber-800">
                    <ShieldOff size={13} className="mt-0.5 flex-shrink-0" />
                    Captured as confidential — logged, never transcribed, and no wrap-up will be
                    offered.
                </p>
            )}

            {/* ── what the AE can do ────────────────────────────────────── */}
            <div className="mt-4 flex flex-wrap items-center gap-2.5">
                {joinHref && (
                    <a
                        href={joinHref}
                        target="_blank"
                        rel="noopener noreferrer"
                        className={JOIN_BTN}
                    >
                        <ExternalLink size={15} /> Join
                    </a>
                )}

                {hasSession ? (
                    sessionId && (
                        <Link
                            to={`/calls/${encodeURIComponent(sessionId)}/wrapup`}
                            className={GHOST_BTN}
                        >
                            <ClipboardList size={15} />
                            {meeting.capture_state === 'wrapped' ? 'View wrap-up' : 'Wrap up'}
                        </Link>
                    )
                ) : (
                    <PrimaryButton
                        onClick={() => void capture()}
                        busy={busy}
                        disabled={busy || locked || !bound || codeMissing}
                    >
                        Capture this call
                    </PrimaryButton>
                )}

                {canPrepMeeting(meeting) && (
                    <button type="button" onClick={openPrep} className={GHOST_BTN}>
                        <Sparkles size={15} /> Prep
                    </button>
                )}
            </div>

            {showMeetHint && (
                <p className="mt-2 flex items-start gap-1.5 text-xs text-gray-500">
                    <ClipboardList size={13} className="mt-0.5 flex-shrink-0 text-brand" />
                    <span>{MEET_PANEL_HINT}</span>
                </p>
            )}

            {!hasSession && (codeMissing || !bound) && (
                <p className="mt-2 text-xs text-gray-500">
                    {codeMissing
                        ? 'Cubie cannot read a Meet meeting code from this invite, so it cannot ' +
                          'attach a capture to this calendar entry. Join with the link — and if ' +
                          'you want the call on the record, start it from “Not on your calendar?” ' +
                          'below.'
                        : 'Pick the company before capturing — that binding is what lets the ' +
                          'wrap-up write to the right HubSpot record without asking you again.'}
                </p>
            )}

            {error && (
                <p className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
                    {error.message}
                    {error.detail && error.detail !== error.message && (
                        <span className="mt-1 block text-[11px] text-red-700">
                            {error.detail}
                        </span>
                    )}
                </p>
            )}
        </li>
    );
};

export default MeetingRow;
