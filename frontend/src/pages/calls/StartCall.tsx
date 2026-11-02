// Today's client calls, from the AE's own calendar.
//
// WHAT THIS SCREEN IS FOR, AND WHAT IT IS NOT FOR. Its PRIMARY job is to bind
// each meeting to the right HubSpot company up front, and to hand the AE the
// calendar's own Meet link. Arming transcription is SECONDARY and best-effort,
// because three facts make arm-everything impossible:
//
//   * a conference already in progress cannot be armed — there is no API for it;
//   * a client-hosted Meet space lives in another Workspace and cannot be
//     patched at all;
//   * a recurring series reuses ONE space, so arming this week's occurrence arms
//     every future one.
//
// A row whose arming fails is therefore still valuable: the company binding is
// what lets the wrap-up write to the right CRM record without asking. So the two
// verdicts are rendered separately, and an unarmable row must never look broken
// and must never look armed.
//
// THE PROJECT'S OWN RULE, WHICH OUTRANKS CONVENIENCE EVERYWHERE HERE: a silent
// no-op is the worst outcome. Every verdict is computed server-side and arrives
// with a reason; nothing on this page defaults to "fine".
//
// FIVE THINGS HERE ARE DELIBERATE:
//
// 1. THE GATE IS ONE QUESTION NOW, NOT TWO — and that is a fix, not a
//    simplification. It used to ask `canAccessCcs` ("may this person use Sales
//    Call Capture at all?") AND `canAccessXray` ("may this person call the
//    company-search endpoint?"), because the only company typeahead in Cubie was
//    `/api/xray/customers`, behind Customer X-Ray's (then Systems-only) gate. An
//    earlier version of this file conflated the two and silently degraded the
//    screen to manual entry for every AE outside that narrow audience — most of
//    Sales, i.e. the people the screen exists for, got its worst version and
//    nothing errored. `GET /api/calls/companies` now runs the same
//    `search_companies` behind `require_ccs`, so `canAccessXray` gates nothing
//    here and the dependency is gone rather than left dead. Whether the search
//    is callable is discovered FROM the search, inside `CompanyPicker`, and
//    still reported as a fact about the SEARCH BOX — never about the account.
// 2. THREE EMPTY STATES, THREE SENTENCES, and they are never interchangeable:
//      (a) `CCS_ENABLED` is off — every /api/calls endpoint 404s. "Not enabled
//          yet", and nothing about the account is wrong.
//      (b) `calendar_available: false` — "Cubie could not read your calendar."
//          A Calendar outage must NEVER read as "you have no calls": that is an
//          answer an AE believes and acts on by not capturing a real call.
//      (c) `calendar_available: true` with no rows — "No client calls on your
//          calendar today", and this one names the filter out loud (Meet link +
//          at least one external guest + not declined) so an AE whose day is
//          internal-only recognises their own day in it rather than doubting
//          the screen. See `NOTHING_TODAY_HINT` for why the internal-only case
//          shares this sentence instead of getting a fourth.
// 3. THE TENANT-WIDE ARMING ANSWER IS STATED ONCE. `arming_unsupported` (the
//    Workspace edition/licence answer) and `write_disabled` (Cubie's own
//    switch) are identical on every row; repeated per row, one account-level
//    fact reads as thirty problems. Rows keep their badge, lose the paragraph.
// 4. `loading` IS DERIVED from a settled result keyed by the load it answered,
//    never stored. A `setLoading(true)` inside an effect keyed on a param is an
//    ERROR under `react-hooks/set-state-in-effect`.
// 5. The in-flight lock on Capture is there to LOOK sane, not to be safe:
//    `POST /start` with a `calendar_event_id` is idempotent at the DATABASE on
//    `UNIQUE(ae_email, calendar_event_id)` and returns the first session rather
//    than 500ing on the race.
import React, { useEffect, useState } from 'react';
import { CalendarDays, CloudOff, PhoneCall, RefreshCw, TriangleAlert } from 'lucide-react';
import {
    callsError,
    fetchScheduledMeetings,
    isForbidden,
    isNotEnabled,
    startCall,
    tenantWideArming,
    type CallsError,
    type CaptureConsent,
    type ScheduledDay,
    type ScheduledMeeting,
} from '../../api/calls';
import client from '../../api/client';
import { BackButton } from '../../components/BackButton';
import { toast } from '../../components/eos/Toast';
import { AdHocCallForm, MeetingRow, type CapturedCall } from '../../components/calls';
import { armingCopy } from '../../components/calls/verdictCopy';
import { useAuth } from '../../context/AuthContext';
import { EmptyState, EosPageHeader, ErrorState, LoadingState, PageContainer } from '../eos/_shared';

// Full literal class strings — an interpolated fragment is purged by the JIT.
const NOTICE_AMBER = 'mb-6 rounded-lg border border-amber-300 bg-amber-50 p-4';
const NOTICE_GRAY = 'mb-6 rounded-lg border border-gray-200 bg-gray-50 p-4';
const OUTAGE_PANEL = 'rounded-lg border border-amber-300 bg-amber-50 p-8 text-center';

/** The empty-day hint, and the one honest compromise on this screen.
 *
 *  The envelope carries no count of the events that were FILTERED OUT, so an
 *  empty list cannot be split into "your calendar is empty" and "you have
 *  meetings, but they are all internal". Claiming to know which would be an
 *  invention — exactly the kind this feature exists to avoid — so the sentence
 *  names the filter instead and lets the AE recognise their own day in it. */
const NOTHING_TODAY_HINT =
    'Cubie lists a meeting here when it has a Google Meet link, at least one guest from ' +
    'outside your company, and you have not declined it. So an empty list can also mean today is ' +
    'internal-only, or that a client call has no Meet link on the invite. If you are about ' +
    'to dial a client anyway, use “Not on your calendar?” below.';

/** One settled load, keyed by the attempt it answered — see note 4. */
interface Loaded {
    key: number;
    day: ScheduledDay | null;
    error: CallsError | null;
}

export const StartCall: React.FC = () => {
    // The screen's own audience, and the ONLY gate it asks about — see note 1.
    // `canAccessCcs` mirrors backend `auth.user_can_access_ccs`: this surface's
    // own rule, not the cockpit's. They resolve to the same audience today
    // because both read the same Feature Access roster, but CCS is the
    // narrower one in intent — it creates a recording-adjacent artifact of a
    // client conversation — so gating on the cockpit's boolean would silently
    // widen this screen the moment the two rules diverge.
    const { canAccessCcs, hasFeature, user } = useAuth();

    // Cubie in Google Meet: its own roster (`sales_meet_addon`) AND its own
    // switch (`MEET_ADDON_ENABLED`), which the client cannot see. Same rule as
    // the probes in Layout.tsx: only people the roster admits pay a request,
    // and any failure (404 switched off, 403, network) leaves the hand-off out,
    // so nobody is told to open a panel that would say "not switched on yet".
    const onMeetAddonRoster = hasFeature('sales_meet_addon');
    const [meetAddonLive, setMeetAddonLive] = useState(false);
    useEffect(() => {
        if (!onMeetAddonRoster) return;
        let cancelled = false;
        client
            .get('/api/meet-addon/status')
            .then(() => {
                if (!cancelled) setMeetAddonLive(true);
            })
            .catch(() => {
                /* the hand-off simply stays out */
            });
        return () => {
            cancelled = true;
        };
    }, [onMeetAddonRoster]);
    const meetAddon =
        onMeetAddonRoster && meetAddonLive ? { accountEmail: user?.email ?? null } : null;

    const [reloadKey, setReloadKey] = useState(0);
    const [loaded, setLoaded] = useState<Loaded | null>(null);

    // Per-row capture state. Held here rather than in the row so an outcome
    // survives a re-sort, and so the page can reason about the day as a whole.
    const [captured, setCaptured] = useState<Record<string, CapturedCall>>({});
    const [rowErrors, setRowErrors] = useState<Record<string, CallsError>>({});
    const [startingId, setStartingId] = useState<string | null>(null);

    const settled = loaded && loaded.key === reloadKey ? loaded : null;
    const loading = settled === null; // derived, never stored
    const day = settled?.day ?? null;
    const err = settled?.error ?? null;

    useEffect(() => {
        let cancelled = false;
        fetchScheduledMeetings()
            .then(result => {
                if (!cancelled) setLoaded({ key: reloadKey, day: result, error: null });
            })
            .catch(e => {
                if (cancelled) return;
                setLoaded({
                    key: reloadKey,
                    day: null,
                    error: callsError(e, 'Your calendar could not be read.'),
                });
            });
        return () => {
            cancelled = true;
        };
    }, [reloadKey]);

    // ─── the gate: may this person use this screen at all? ───────────────────
    //
    // After the hooks, never before: an early return above them would make the
    // hook order conditional.
    //
    // A `false` here is "not this person", NEVER "the feature is off": the
    // `CCS_ENABLED` flag is a server setting the client never receives and
    // every /api/calls endpoint 404s while it is off — that is the
    // `isNotEnabled` EmptyState further down, which says something different on
    // purpose. No BackButton to My Day either: someone who fails this gate
    // cannot open that page, so offering it would strand them one click further
    // along.
    if (!canAccessCcs) {
        return (
            <PageContainer>
                <EosPageHeader
                    icon={<PhoneCall size={20} />}
                    title="Today's client calls"
                    right={<BackButton fallback="/" label="Home" />}
                />
                <EmptyState
                    title="Sales Call Capture is not enabled for your account."
                    hint="Capturing a client call is part of the Sales Cockpit, which an admin opens to teams in Admin → Feature Access. Nothing is wrong with your account — if you take client calls and this should include you, ask a Cubie administrator."
                />
            </PageContainer>
        );
    }

    const meetings = day?.meetings ?? [];
    // One catalogue for the whole day, handed to every row: it does not vary by
    // row, and it is what lets a first pick show its checklist.
    const scriptCatalogue = day?.script_catalogue ?? null;
    const tenantWide = tenantWideArming(meetings);
    const tenantWideCopy = tenantWide ? armingCopy(tenantWide, { hasSession: false }) : null;

    const onCapture = async (
        meeting: ScheduledMeeting,
        args: {
            hubspot_company_id: string;
            capture_consent: CaptureConsent;
            product_keys?: string[];
        },
    ) => {
        const eventId = meeting.google_event_id;
        if (startingId) return; // note 5 — cosmetic, not the safety net
        setStartingId(eventId);
        setRowErrors(prev => {
            if (!prev[eventId]) return prev;
            const next = { ...prev };
            delete next[eventId];
            return next;
        });
        try {
            const res = await startCall({
                hubspot_company_id: args.hubspot_company_id,
                // WITH a calendar_event_id the router creates NO Meet space: the
                // calendar already made one, and the code is the only address
                // `spaces.get` accepts for a space Cubie did not create.
                calendar_event_id: eventId,
                meet_meeting_code: meeting.meeting_code,
                meet_uri: meeting.meet_uri,
                capture_consent: args.capture_consent,
                // Passed STRAIGHT THROUGH, never coalesced. `[]` is the AE
                // deselecting every product, which clears their remembered pick
                // for this client; `undefined` is a row that had no picker
                // (`scripts: null`) and is dropped from the JSON body entirely,
                // leaving last week's pick alone. `?? []` here would silently
                // turn the second case into the first.
                product_keys: args.product_keys,
            });
            setCaptured(prev => ({
                ...prev,
                [eventId]: {
                    call_session_id: res.call_session_id,
                    arming: res.arming,
                    origin: res.origin,
                    confidential: args.capture_consent === 'suppressed',
                },
            }));
            // The toast reports what actually happened, which is not always the
            // same as "done". A best-effort arming that failed is a SUCCESS for
            // the binding and must not be announced as a failure — nor as a
            // transcript that is coming.
            if (args.capture_consent === 'suppressed') {
                toast.info('Captured as confidential — logged, never transcribed.');
            } else if (res.arming?.state === 'armed') {
                toast.success('Captured. Transcription is on for this meeting.');
            } else {
                toast.info(
                    'Captured and bound to the company. Transcription could not be armed — the row says why.',
                );
            }
        } catch (e) {
            const error = callsError(e, 'The call could not be captured.');
            setRowErrors(prev => ({ ...prev, [eventId]: error }));
            // A flag-off 404 has its own panel; a red toast on top of it says
            // "broken" about something that is merely not switched on.
            if (!isNotEnabled(error)) toast.error(error.message);
        } finally {
            setStartingId(null);
        }
    };

    const subtitle = day
        ? `${day.local_date}${day.timezone ? ` · ${day.timezone}` : ''}`
        : undefined;

    return (
        <PageContainer>
            <EosPageHeader
                icon={<CalendarDays size={20} />}
                title="Today's client calls"
                subtitle={subtitle}
                right={
                    <>
                        <button
                            type="button"
                            onClick={() => setReloadKey(k => k + 1)}
                            disabled={loading}
                            className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                            <RefreshCw size={13} /> Refresh
                        </button>
                        <BackButton fallback="/sales" label="My Day" />
                    </>
                }
            />

            {/* The feature flag is off. Nothing about the account is wrong and
                no retry will help — a different sentence from the gate above. */}
            {err && isNotEnabled(err) ? (
                <EmptyState
                    title="Call capture is not enabled yet."
                    hint="Nothing is wrong with your account. Until it is switched on, run client calls the way you do today."
                />
            ) : err && isForbidden(err) ? (
                <EmptyState title="This is not yours to open." hint={err.message} />
            ) : (
                <>
                    {/* The ad-hoc form stays reachable through a hard failure of
                        /scheduled on purpose: the AE may be about to dial a
                        client right now, and the two paths do not share a
                        dependency. It is withheld only under the two states
                        where every /api/calls endpoint would refuse anyway. */}
                    {err ? (
                        <div className="space-y-4">
                            <ErrorState message={err.message} />
                            <button
                                type="button"
                                onClick={() => setReloadKey(k => k + 1)}
                                className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50"
                            >
                                <RefreshCw size={13} /> Try again
                            </button>
                        </div>
                    ) : loading ? (
                        <LoadingState label="Reading your calendar…" />
                    ) : (
                        <>
                            {/* Note 3 — the account-level answer, said once. */}
                            {tenantWide && tenantWideCopy && (
                                <div
                                    className={
                                        tenantWideCopy.tone === 'amber' ? NOTICE_AMBER : NOTICE_GRAY
                                    }
                                >
                                    <div className="flex items-start gap-3">
                                        <TriangleAlert
                                            size={18}
                                            className="mt-0.5 flex-shrink-0 text-amber-600"
                                        />
                                        <div className="min-w-0">
                                            <p className="text-sm font-medium text-amber-900">
                                                Cubie cannot turn transcription on for any meeting today.
                                            </p>
                                            {tenantWide.detail && (
                                                <p className="mt-1 text-xs leading-relaxed text-amber-900">
                                                    {tenantWide.detail}
                                                </p>
                                            )}
                                            <p className="mt-2 text-xs leading-relaxed text-amber-900">
                                                Capturing a call is still worth doing: it binds the
                                                meeting to its HubSpot company, which is what lets the
                                                wrap-up file against the right record.
                                            </p>
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* EMPTY STATE 1 — the Calendar outage. This is NOT an empty
                                day, and saying so is the whole point: "you have no
                                calls" is an answer an AE believes and acts on. */}
                            {!day?.calendar_available ? (
                                <div className={OUTAGE_PANEL}>
                                    <CloudOff size={22} className="mx-auto text-amber-600" />
                                    <p className="mt-3 text-base font-medium text-amber-900">
                                        Cubie could not read your calendar.
                                    </p>
                                    <p className="mx-auto mt-2 max-w-xl text-sm leading-relaxed text-amber-900">
                                        This is not an empty day — the calendar lookup itself failed, so
                                        Cubie cannot tell you which client calls you have. Try again in a
                                        moment. If you are about to join a client call now, use “Not on
                                        your calendar?” below: the call still gets logged and bound to
                                        its company.
                                    </p>
                                    <button
                                        type="button"
                                        onClick={() => setReloadKey(k => k + 1)}
                                        className="mt-4 inline-flex items-center gap-1.5 rounded-md border border-amber-400 bg-white px-3 py-1.5 text-xs font-medium text-amber-900 shadow-sm transition-colors hover:bg-amber-100"
                                    >
                                        <RefreshCw size={13} /> Try again
                                    </button>
                                </div>
                            ) : meetings.length === 0 ? (
                                /* EMPTY STATE 2 — a real answer: the calendar was read
                                   and holds no capturable client call. */
                                <EmptyState
                                    title="No client calls on your calendar today."
                                    hint={NOTHING_TODAY_HINT}
                                />
                            ) : (
                                <ul className="space-y-4" data-tour="calls-start-list">
                                    {meetings.map(meeting => (
                                        <MeetingRow
                                            key={meeting.google_event_id}
                                            meeting={meeting}
                                            scriptCatalogue={scriptCatalogue}
                                            captured={captured[meeting.google_event_id] ?? null}
                                            busy={startingId === meeting.google_event_id}
                                            locked={
                                                startingId !== null &&
                                                startingId !== meeting.google_event_id
                                            }
                                            error={rowErrors[meeting.google_event_id] ?? null}
                                            meetAddon={meetAddon}
                                            onCapture={(m, args) => void onCapture(m, args)}
                                        />
                                    ))}
                                </ul>
                            )}
                        </>
                    )}

                    <AdHocCallForm meetAddon={meetAddon} />
                </>
            )}
        </PageContainer>
    );
};

export default StartCall;
