// "Not on your calendar?" — the ad-hoc start, kept as an escape hatch.
//
// This used to be the whole screen. It is now the exception: an AE who is about
// to dial a client that has no calendar invite, where Cubie has to create the
// Meet space itself. That is the ONE path where `MeetSpaceWriteDisabled` is a
// 503 rather than a warning — here the Meet write IS the feature, because
// without a space there is no call to join.
//
// THREE THINGS SURVIVE FROM THE OLD SCREEN UNCHANGED:
//
// 1. The confidential toggle states its consequence on the row, asks once
//    before use, and says out loud that it cannot be undone. A `suppressed`
//    session writes no transcript entries, no DAM asset, no participants row
//    and runs no extraction.
// 2. `meet_uri` can be NULL and it is handled, not assumed — Meet returns no
//    `meetingUri` for a space it could not fully create, and the 60-second
//    de-duplication reply hands back an existing session whose own uri may be
//    null. `hasMeetSpace` is the only gate between the response and
//    `window.open`.
// 3. The 60-second idempotency is a BACKSTOP against a double-click burning a
//    second `spaces.create` against a quota of ten per minute per user. The
//    in-flight lock below is there to look sane, not to be safe.
//
// WHAT CHANGED: the typeahead. It points at `GET /api/calls/companies`, behind
// this feature's own `require_ccs`, instead of `/api/xray/customers` behind
// Customer X-Ray's own gate. So there is no longer a second audience
// question to ask before rendering a search box, and `canAccessXray` gates
// nothing on this screen.
import React, { useState } from 'react';
import {
    Building2,
    CheckCircle2,
    ChevronDown,
    ChevronRight,
    ExternalLink,
    PhoneCall,
    ShieldOff,
    TriangleAlert,
} from 'lucide-react';
import {
    callsError,
    companyOptionLabel,
    hasMeetSpace,
    isMeetDisabled,
    startCall,
    type CallsError,
    type CompanyOption,
    type StartCallResponse,
} from '../../api/calls';
import { confirmDialog } from '../eos/ConfirmDialog';
import { MEET_PANEL_HINT, meetJoinUrl, type MeetHandoff } from '../../meetAddon/joinLink';
import { Field, inputCls } from '../eos/Modal';
import { toast } from '../eos/Toast';
import { ErrorState } from '../../pages/eos/_shared';
import { PrimaryButton, SecondaryButton } from '../smartTeams/shared';
import { ArmingBadge } from './ArmingBadge';
import { CompanyPicker } from './CompanyPicker';

// Full literal class strings — an interpolated fragment is purged by the JIT.
const PANEL = 'rounded-lg border border-gray-200 bg-white shadow-sm';
const CONSENT_ON = 'rounded-lg border border-amber-300 bg-amber-50 p-4';
const CONSENT_OFF = 'rounded-lg border border-gray-200 bg-gray-50 p-4';
const MEET_LINK =
    'inline-flex items-center justify-center gap-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2';

export interface AdHocCallFormProps {
    /** Same hand-off as a calendar row's Join — see `MeetingRow`. */
    meetAddon?: MeetHandoff | null;
}

export const AdHocCallForm: React.FC<AdHocCallFormProps> = ({ meetAddon = null }) => {
    const openHref = (uri: string): string =>
        meetAddon ? meetJoinUrl(uri, meetAddon.accountEmail) : uri;
    const [open, setOpen] = useState(false);

    const [picked, setPicked] = useState<CompanyOption | null>(null);
    const [contactId, setContactId] = useState('');
    const [dealId, setDealId] = useState('');
    const [confidential, setConfidential] = useState(false);

    const [starting, setStarting] = useState(false);
    const [started, setStarted] = useState<StartCallResponse | null>(null);
    const [err, setErr] = useState<CallsError | null>(null);

    const companyLabel = picked ? companyOptionLabel(picked) : '';

    const reset = () => {
        setStarted(null);
        setErr(null);
        setPicked(null);
        setContactId('');
        setDealId('');
        setConfidential(false);
    };

    const onStart = async () => {
        if (!picked) {
            toast.error('Pick the HubSpot company this call is with first.');
            return;
        }
        if (confidential) {
            const ok = await confirmDialog({
                title: 'Start this call as confidential?',
                message:
                    'Cubie will log that the call happened — who, when, and against which ' +
                    'company. Nothing else.\n\n' +
                    'No transcript is captured, nothing is extracted, and no wrap-up will be ' +
                    'offered. This cannot be undone: a confidential call can never be ' +
                    'un-suppressed afterwards.',
                confirmLabel: 'Yes, start confidentially',
                cancelLabel: 'Back',
            });
            if (!ok) return;
        }

        setStarting(true);
        try {
            // No `calendar_event_id`, so the router takes the `origin="cubie"`
            // path and creates the space itself.
            const res = await startCall({
                hubspot_company_id: picked.hubspot_company_id,
                hubspot_contact_id: contactId.trim() || null,
                hubspot_deal_id: dealId.trim() || null,
                capture_consent: confidential ? 'suppressed' : 'granted',
            });
            setErr(null);
            setStarted(res);
            if (hasMeetSpace(res) && res.meet_uri) {
                const win = window.open(openHref(res.meet_uri), '_blank', 'noopener,noreferrer');
                if (!win) {
                    toast.info('Your browser blocked the pop-up — use "Open Meet" below.');
                }
            }
        } catch (e) {
            const error = callsError(e, 'The call could not be started.');
            setErr(error);
            // A Meet-writes 503 is a normal state with its own panel below; a
            // red toast on top of it says "broken" about a switch.
            if (!isMeetDisabled(error)) toast.error(error.message);
        } finally {
            setStarting(false);
        }
    };

    return (
        <section className="mt-8">
            <button
                type="button"
                data-tour="calls-start-adhoc"
                onClick={() => setOpen(v => !v)}
                aria-expanded={open}
                className="flex w-full items-center gap-2 rounded-lg border border-gray-200 bg-white px-4 py-3 text-left text-sm font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50"
            >
                {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                <PhoneCall size={15} className="text-brand" />
                Not on your calendar?
                <span className="ml-1 font-normal text-gray-500">
                    Start a client call Cubie creates the Meet space for.
                </span>
            </button>

            {open && (
                <div className={`${PANEL} mt-3 p-6`}>
                    {started ? (
                        <div>
                            <div className="flex items-start gap-3">
                                <CheckCircle2
                                    size={20}
                                    className="mt-0.5 flex-shrink-0 text-green-600"
                                />
                                <div className="min-w-0">
                                    <p className="text-base font-semibold text-gray-900">
                                        Cubie has the call on the record.
                                    </p>
                                    <p className="mt-1 text-sm text-gray-600">
                                        {confidential
                                            ? 'This call is logged against the company, and nothing from it will be transcribed, extracted or written to HubSpot.'
                                            : 'When the call ends, the wrap-up appears at the top of My Day — usually within fifteen minutes.'}
                                    </p>
                                </div>
                            </div>

                            <dl className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-3">
                                <div>
                                    <dt className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                                        Company
                                    </dt>
                                    <dd className="mt-0.5 truncate text-sm text-gray-900">
                                        {companyLabel || '—'}
                                    </dd>
                                </div>
                                <div>
                                    <dt className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                                        Meeting code
                                    </dt>
                                    <dd className="mt-0.5 truncate text-sm text-gray-900">
                                        {started.meeting_code || '—'}
                                    </dd>
                                </div>
                                <div>
                                    <dt className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                                        Transcription
                                    </dt>
                                    <dd className="mt-1">
                                        {/* The server's own verdict, not a
                                            re-derivation from `confidential`. */}
                                        <ArmingBadge
                                            verdict={started.arming}
                                            hasSession
                                        />
                                    </dd>
                                </div>
                            </dl>

                            {!hasMeetSpace(started) && (
                                <div className="mt-5 flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4">
                                    <TriangleAlert
                                        size={18}
                                        className="mt-0.5 flex-shrink-0 text-amber-600"
                                    />
                                    <p className="text-xs text-amber-900">
                                        Cubie could not create the Meet space, so this call will
                                        not be transcribed. Start the meeting the way you
                                        normally would — the call is still logged against{' '}
                                        <span className="font-medium">
                                            {companyLabel || 'the company'}
                                        </span>
                                        .
                                    </p>
                                </div>
                            )}

                            <div className="mt-6 flex flex-wrap items-center gap-3">
                                {hasMeetSpace(started) && started.meet_uri && (
                                    <a
                                        href={openHref(started.meet_uri)}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className={MEET_LINK}
                                    >
                                        <ExternalLink size={15} /> Open Meet
                                    </a>
                                )}
                                <SecondaryButton onClick={reset}>
                                    Start another call
                                </SecondaryButton>
                            </div>
                            {meetAddon && hasMeetSpace(started) && started.meet_uri && (
                                <p className="mt-3 text-xs text-gray-500">{MEET_PANEL_HINT}</p>
                            )}
                        </div>
                    ) : (
                        <div className="space-y-5">
                            {/* HTTP 503 — `MeetSpaceWriteDisabled`, raised before
                                any credential request. With both Meet write
                                scopes granted on the domain-wide-delegation
                                client id, this means the
                                `CCS_MEET_ENABLED` switch is off or the
                                credentials would not load. Still an operator
                                action with a way forward for the AE, never a red
                                failure — and the router's own sentence is kept
                                underneath for whoever can fix it. */}
                            {err && isMeetDisabled(err) && (
                                <div className="rounded-lg border border-amber-300 bg-amber-50 p-4">
                                    <div className="flex items-start gap-3">
                                        <TriangleAlert
                                            size={18}
                                            className="mt-0.5 flex-shrink-0 text-amber-600"
                                        />
                                        <div className="min-w-0">
                                            <p className="text-sm font-medium text-amber-900">
                                                Cubie cannot create a transcribed call right now.
                                            </p>
                                            <p className="mt-1 text-xs text-amber-900">
                                                {err.message}
                                            </p>
                                            {err.detail && (
                                                <p className="mt-2 text-[11px] leading-relaxed text-amber-800">
                                                    <span className="font-semibold uppercase tracking-wide">
                                                        For an administrator:
                                                    </span>{' '}
                                                    {err.detail}
                                                </p>
                                            )}
                                        </div>
                                    </div>
                                </div>
                            )}

                            {err && !isMeetDisabled(err) && <ErrorState message={err.message} />}

                            {picked ? (
                                <div>
                                    <span className="mb-1.5 block text-sm font-medium text-gray-700">
                                        Company
                                    </span>
                                    <div className="flex items-center gap-3 rounded-md border border-gray-300 bg-gray-50 px-3 py-2">
                                        <Building2
                                            size={16}
                                            className="flex-shrink-0 text-brand"
                                        />
                                        <span className="min-w-0 flex-1 truncate text-sm font-medium text-gray-900">
                                            {companyLabel}
                                        </span>
                                        <button
                                            type="button"
                                            onClick={() => setPicked(null)}
                                            className="flex-shrink-0 text-xs font-medium text-brand hover:underline"
                                        >
                                            change
                                        </button>
                                    </div>
                                </div>
                            ) : (
                                <CompanyPicker
                                    inputId="ccs-adhoc-company"
                                    onPick={setPicked}
                                />
                            )}

                            {/* optional bindings — no contact/deal search exists
                                in Cubie today */}
                            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                                <Field label="Contact ID (optional)">
                                    <input
                                        type="text"
                                        value={contactId}
                                        onChange={e => setContactId(e.target.value)}
                                        placeholder="HubSpot contact ID"
                                        className={inputCls}
                                    />
                                </Field>
                                <Field label="Deal ID (optional)">
                                    <input
                                        type="text"
                                        value={dealId}
                                        onChange={e => setDealId(e.target.value)}
                                        placeholder="HubSpot deal ID"
                                        className={inputCls}
                                    />
                                </Field>
                            </div>

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
                                            The call is logged, but never transcribed — no
                                            transcript, no extraction, no wrap-up — and it cannot
                                            be un-suppressed later.
                                        </span>
                                    </span>
                                </label>
                            </div>

                            <div className="flex items-center gap-3 pt-1">
                                <PrimaryButton
                                    onClick={() => void onStart()}
                                    busy={starting}
                                    disabled={starting || !picked}
                                >
                                    Start call
                                </PrimaryButton>
                                <p className="text-xs text-gray-500">
                                    Opens Google Meet in a new tab.
                                </p>
                            </div>
                        </div>
                    )}
                </div>
            )}
        </section>
    );
};

export default AdHocCallForm;
