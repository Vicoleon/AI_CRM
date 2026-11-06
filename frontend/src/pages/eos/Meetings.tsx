import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    Calendar,
    Clock,
    Users,
    Play,
    Plus,
    RefreshCw,
    NotebookPen,
    Trash2,
    History,
    RotateCcw,
    Edit2,
    Save,
    X,
    CalendarDays,
    CalendarOff,
    Check,
    ExternalLink,
    ChevronDown,
    ChevronRight,
    Sparkles,
    ListTodo,
    Flag,
    UserCheck,
} from 'lucide-react';
import client from '../../api/client';
import { useAuth } from '../../context/AuthContext';
import { parseInstant } from '../../utils/datetime';
import {
    EmptyState,
    ErrorState,
    EosPageHeader,
    LoadingState,
    PageContainer,
    TeamFilter,
    OwnerChip,
} from './_shared';
import {
    canRestartMeeting,
    EXEC_SCOPE_LABEL,
    formatPerson,
    partitionL10Inputs,
    type PersonLite,
} from './_utils';
import { toast } from '../../components/eos/Toast';
import { confirmDialog } from '../../components/eos/ConfirmDialog';
import { Modal, Field, inputCls, SubmitButton } from '../../components/eos/Modal';

// GET /api/eos/l10/{id}/manager-availability — "can the facilitator still be
// reached to hand this over?" plus what the caller is allowed to do about it.
interface ManagerAvailability {
    available: boolean;
    // "available" | "no_manager" | "offboarded" | "on_pto"
    reason: string;
    // Human sentence explaining the state — shown verbatim, since each reason
    // implies a different next step.
    detail: string;
    manager_name: string | null;
    manager_email: string | null;
    out_from: string | null;
    out_until: string | null;
    returns_on: string | null;
    // Non-null when the caller can already edit the meeting outright
    // ("manager" | "leadership" | "admin" | "org_exec" | "exec_team").
    your_authority: string | null;
    // True only when the break-glass path is open: no standing authority, an
    // EOS profile to be made manager with, and an unreachable manager.
    can_take_over: boolean;
    your_person_id: string | null;
}

interface L10Meeting {
    id: string;
    name: string;
    department_name: string | null;
    manager_id: string | null;
    day_of_week: number | null; // 0=Mon .. 6=Sun per backend convention
    start_time: string | null; // "HH:MM"
    duration_minutes: number;
    is_exec_meeting?: boolean;
    // Configurable link slot — populated together or both null.
    secondary_link_label?: string | null;
    secondary_link_url?: string | null;
    // Set when the meeting was imported from a Google
    // Calendar event. Frontend uses it to show an "Imported from
    // Google Calendar" pill on the card.
    google_event_id?: string | null;
    // Calendar date of the occurrence most recently imported/synced
    // (e.g. "2026-07-01"). Informational "last synced" metadata — the
    // schedule line shows the COMPUTED next occurrence instead.
    last_imported_date?: string | null;
    roster_emails?: string[] | null;
    // Derived by the list endpoint: id of an in-progress session, if any.
    // When set, the meeting is live and can be rejoined regardless of weekday.
    live_session_id?: string | null;
    // Last reconciliation against Google Calendar, stored on the row.
    // 'active' | 'cancelled', or null when nobody has been able to check.
    calendar_state?: string | null;
    next_occurrence_at?: string | null;
    calendar_checked_at?: string | null;
}

// What Google Calendar says about one imported meeting RIGHT NOW, from
// GET /api/eos/l10/calendar-status. Keyed by meeting id in `calendarStatus`.
//
// `state` of 'unknown' means we could not ask — Calendar is unconfigured, the
// API errored, or the meeting is past the server's per-request lookup cap. It
// must render as "we don't know", never as a cancellation: a Google blip must
// not tell every team their L10 was called off.
interface L10CalendarStatus {
    meeting_id: string;
    state: 'active' | 'cancelled' | 'unknown';
    next_occurrence_at?: string | null;
    checked_at?: string | null;
    persisted: boolean;
    reason?: string | null;
    // Stable discriminator for a 'cancelled' state. Branch on this, never on
    // `reason` — that one is prose meant for a human, and rewording it must
    // not change what the card says.
    kind?: 'event_deleted' | 'nothing_scheduled' | null;
}

// An "L10"-titled event on the caller's Google Calendar, as normalized
// by GET /api/eos/l10/calendar-upcoming. Declared at module scope so the
// strip helpers below can reference it.
interface IngestedL10Event {
    google_event_id: string | null;
    // Series id shared across every occurrence of a recurring event. An
    // imported meeting is keyed on this, so it's how we match a future
    // occurrence back to the meeting it should refresh. Null for one-offs.
    recurring_event_id: string | null;
    summary: string;
    start: string | null;
    end: string | null;
    location: string | null;
    hangout_link: string | null;
    attendees: string[];
    organizer_email: string | null;
}

// Sentinel value used by the Scope dropdown to mean "the cross-department
// Exec leadership team." Regular department names live alongside it in the
// same <select>, with "" reserved for "no scope".
const EXEC_SCOPE = '__exec__';

interface FormState {
    name: string;
    // For the Exec choice this holds EXEC_SCOPE; otherwise it's a real
    // department name (or "" for none, when an admin picks no scope).
    scope: string;
    manager_id: string;
    day_of_week: number;
    start_time: string;
    duration_minutes: number;
    // Configurable per-meeting link slot for transient initiatives
    // (e.g. "What Matters Most" tracker). Empty strings → null on the wire.
    secondary_link_label: string;
    secondary_link_url: string;
}

const EMPTY_FORM: FormState = {
    name: '',
    scope: '',
    manager_id: '',
    day_of_week: 0, // Monday
    start_time: '09:00',
    duration_minutes: 90,
    secondary_link_label: '',
    secondary_link_url: '',
};

const DAY_LABELS = [
    'Monday',
    'Tuesday',
    'Wednesday',
    'Thursday',
    'Friday',
    'Saturday',
    'Sunday',
];

const dayLabel = (idx: number | null) =>
    idx === null || idx < 0 || idx > 6 ? 'TBD' : DAY_LABELS[idx];

// Parses a "YYYY-MM-DD" wire date as a local date (avoids the UTC-midnight
// off-by-one that `new Date("YYYY-MM-DD")` can produce west of UTC).
const formatImportedDate = (iso: string) => {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
    });
};

// The concrete date of the meeting's NEXT occurrence, computed from its
// weekly pattern — today while the meeting is still upcoming/underway on its
// scheduled day, otherwise the next matching weekday. Computed (not stored)
// so the card advances by itself when a week passes: nobody should have to
// re-import a meeting just to move its date forward.
const nextOccurrenceDate = (m: L10Meeting): Date | null => {
    if (m.day_of_week === null || m.day_of_week < 0 || m.day_of_week > 6) {
        return null;
    }
    const now = new Date();
    const todayDow = (now.getDay() + 6) % 7; // JS 0=Sun → backend 0=Mon
    let delta = (m.day_of_week - todayDow + 7) % 7;
    if (delta === 0) {
        // Scheduled day: it's still "today's meeting" until it has ended.
        const [h, mi] = (m.start_time ?? '23:59').split(':').map(Number);
        const end = new Date(now);
        end.setHours(h, mi + (m.duration_minutes || 0), 0, 0);
        if (now > end) delta = 7;
    }
    const d = new Date(now);
    d.setDate(now.getDate() + delta);
    return d;
};

// True when Google Calendar has told us this meeting's series is cancelled —
// either live (`status`) or from the last reconciliation stored on the row.
//
// Only an explicit 'cancelled' counts. 'unknown' and a null `calendar_state`
// both mean "nobody could ask", and are treated as not-cancelled so a Calendar
// outage — or a manually-scheduled L10 that has no calendar at all — never
// reads as a cancellation.
const isCalendarCancelled = (
    m: L10Meeting,
    status?: L10CalendarStatus,
): boolean =>
    status ? status.state === 'cancelled' : m.calendar_state === 'cancelled';

// The next occurrence GOOGLE actually lists, if we have it. Beats the weekly
// pattern for a moved meeting, a skipped week or a changed time — none of
// which `day_of_week` + `start_time` can express.
const calendarNextDate = (
    m: L10Meeting,
    status?: L10CalendarStatus,
): Date | null => {
    const raw = status?.next_occurrence_at ?? m.next_occurrence_at ?? null;
    const d = parseInstant(raw);
    // A reconciled date that has already ENDED is stale, not "next". One that
    // started but is still running is today's meeting — keep showing it.
    const endMs = d ? d.getTime() + (m.duration_minutes || 0) * 60_000 : 0;
    return d && endMs > Date.now() ? d : null;
};

// A cancelled series comes in two flavours, and they read very differently to
// whoever is looking at the card. The GATING is identical — there is no next
// meeting either way — so the backend keeps one state and tells them apart in
// `reason`; only the wording changes here.
const wasDeleted = (status?: L10CalendarStatus): boolean =>
    status?.kind === 'event_deleted';

const cancellationLabel = (status?: L10CalendarStatus): string =>
    wasDeleted(status) ? 'Cancelled on calendar' : 'Nothing scheduled';

const cancellationReason = (status?: L10CalendarStatus): string =>
    status?.reason
        ? `Google Calendar says ${status.reason}`
        : 'Google Calendar has no upcoming occurrence of this event';

// Schedule line for a meeting card/row: the full date of the next
// occurrence ("Next: Wednesday, Jul 8, 2026 at 11:00") so it's always
// unambiguous WHICH meeting the card is about.
//
// Prefers the calendar's own answer. When the series has been cancelled we say
// so instead of rolling the weekly pattern forward into a date that will never
// happen — otherwise a cancelled L10 keeps advertising "Next: Wednesday,
// Sep 16" week after week.
const scheduleLabel = (m: L10Meeting, status?: L10CalendarStatus): string => {
    if (isCalendarCancelled(m, status)) {
        return wasDeleted(status)
            ? 'No upcoming occurrence — cancelled on Google Calendar'
            : 'No upcoming occurrence on Google Calendar';
    }
    const fromCalendar = calendarNextDate(m, status);
    const d = fromCalendar ?? nextOccurrenceDate(m);
    if (!d) return 'Schedule TBD';
    const dateStr = d.toLocaleDateString(undefined, {
        weekday: 'long',
        month: 'short',
        day: 'numeric',
        year: 'numeric',
    });
    const timeStr = fromCalendar
        ? ` at ${fromCalendar.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`
        : m.start_time
          ? ` at ${m.start_time}`
          : '';
    return `Next: ${dateStr}${timeStr}`;
};

// True when an ingested calendar event belongs to a series that is already
// imported as a meeting. Matches the stored id exactly, or as an
// instance-id prefix (`<series>_<timestamp>` — meetings imported before
// series-dedup). Drives the strip's "In Cubie / Sync" state so users aren't
// prompted to Import something that's already here.
const eventAlreadyImported = (
    ev: IngestedL10Event,
    meetings: L10Meeting[],
): boolean => {
    const series = ev.recurring_event_id ?? ev.google_event_id;
    if (!series) return false;
    return meetings.some(
        m =>
            !!m.google_event_id &&
            (m.google_event_id === series ||
                m.google_event_id.startsWith(`${series}_`) ||
                m.google_event_id === ev.google_event_id),
    );
};

// Today's weekday in the backend's 0=Mon..6=Sun convention (JS getDay is
// 0=Sun..6=Sat).
const todayWeekdayMondayBased = (): number => (new Date().getDay() + 6) % 7;

// A recurring L10 is "this week's" — and therefore startable — only on its
// scheduled weekday. Every other day the Start button is disabled and shows
// when it next opens, so it's unambiguous which meeting is live today and a
// future week's slot can't be started early.
const isStartableToday = (m: L10Meeting, status?: L10CalendarStatus): boolean =>
    !isCalendarCancelled(m, status) &&
    m.day_of_week !== null &&
    m.day_of_week === todayWeekdayMondayBased();

// Label for the disabled Start button on non-scheduled days.
const startsLabel = (m: L10Meeting, status?: L10CalendarStatus): string => {
    if (isCalendarCancelled(m, status)) return 'Not on the calendar';
    return m.day_of_week === null || m.day_of_week < 0 || m.day_of_week > 6
        ? 'Schedule TBD'
        : `Starts ${dayLabel(m.day_of_week)}${m.start_time ? ` at ${m.start_time}` : ''}`;
};

// Up to two initials from a display name (or an email local-part fallback),
// splitting on spaces and the separators common in email handles.
const avatarInitials = (name: string): string =>
    name
        .trim()
        .split(/[\s._-]+/)
        .filter(Boolean)
        .slice(0, 2)
        .map(w => w[0])
        .join('')
        .toUpperCase() || '?';

interface MeetingActionProps {
    meeting: L10Meeting;
    // Resolved facilitator name + attendee display names (from the people
    // list), so the card previews the room without another fetch. Optional so
    // the card still renders before people load.
    managerName?: string | null;
    attendees?: string[];
    onEdit: (m: L10Meeting) => void;
    onDiary: (m: L10Meeting) => void;
    onPrep: (m: L10Meeting) => void;
    onStart: (id: string) => void;
    onDelete: (m: L10Meeting) => void;
    deletingId: string | null;
    // Restart (people on the `l10_meeting_restart` feature who can run the
    // meeting): reset the live session to the top with a fresh timer, then
    // drop into it. Only wired up when the meeting is live — see `restartable`.
    onRestart?: (m: L10Meeting) => void;
    restartable?: boolean;
    restartingId?: string | null;
    // Calendar-imported meetings only: re-pull time/attendees from the
    // source Google Calendar series. `syncing` flags the in-flight state.
    onSync?: (m: L10Meeting) => void;
    syncing?: boolean;
    // Live answer from GET /api/eos/l10/calendar-status, when it has arrived.
    // Undefined means "not checked yet" — the card falls back to whatever the
    // row already carries, and never invents a cancellation.
    calendarStatus?: L10CalendarStatus;
    // "Manager away?" — checks whether the facilitator can still be reached and,
    // when they can't, offers to hand the meeting to the caller. On-demand
    // (never per-row on load) because the check can hit the manager's calendar.
    onTakeOver?: (m: L10Meeting) => void;
    checkingHandoffId?: string | null;
}

// Full-detail card — used for the primary (user's own) L10.
const MeetingCard: React.FC<MeetingActionProps> = ({
    meeting,
    managerName,
    attendees,
    onEdit,
    onDiary,
    onPrep,
    onStart,
    onDelete,
    deletingId,
    onRestart,
    restartable,
    restartingId,
    onSync,
    syncing,
    calendarStatus,
    onTakeOver,
    checkingHandoffId,
}) => (
    <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5">
        <div className="flex items-start justify-between gap-4 mb-3">
            <div className="flex items-start gap-3 min-w-0">
                <div className="bg-brand/10 text-brand p-2.5 rounded-md shrink-0">
                    <Calendar size={20} />
                </div>
                <div className="min-w-0">
                    <h3 className="text-lg font-semibold text-gray-900 truncate">
                        {meeting.name}
                    </h3>
                    <p className="text-xs text-gray-500 mt-0.5">
                        {meeting.is_exec_meeting
                            ? EXEC_SCOPE_LABEL
                            : meeting.department_name || 'No team'}
                    </p>
                </div>
            </div>
            <div className="flex items-center gap-1.5 shrink-0">
                {meeting.is_exec_meeting && (
                    <span className="text-[10px] font-bold uppercase tracking-wider bg-brand text-white px-2 py-0.5 rounded-full">
                        Exec
                    </span>
                )}
                {onTakeOver && (
                    <button
                        type="button"
                        onClick={() => onTakeOver(meeting)}
                        disabled={checkingHandoffId === meeting.id}
                        className="p-1 text-gray-400 hover:text-brand hover:bg-brand/5 rounded disabled:opacity-50 disabled:cursor-not-allowed"
                        title="Manager away? Take over this meeting"
                    >
                        <UserCheck size={14} />
                    </button>
                )}
                <button
                    type="button"
                    onClick={() => onEdit(meeting)}
                    className="p-1 text-gray-400 hover:text-brand hover:bg-brand/5 rounded"
                    title="Edit meeting — manager, schedule, scope"
                >
                    <Edit2 size={14} />
                </button>
                <button
                    type="button"
                    onClick={() => onDelete(meeting)}
                    disabled={deletingId === meeting.id}
                    className="p-1 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded disabled:opacity-50 disabled:cursor-not-allowed"
                    title="Delete meeting (manager / admin only)"
                >
                    <Trash2 size={14} />
                </button>
            </div>
        </div>

        {/* Facilitator + attendee presence — a pre-flight read of the room. */}
        {(managerName || (attendees && attendees.length > 0)) && (
            <div className="flex items-center gap-x-3 gap-y-2 flex-wrap mb-3">
                {managerName && (
                    <span className="inline-flex items-center gap-1.5">
                        <span className="text-[11px] uppercase tracking-wide text-gray-400">
                            Led by
                        </span>
                        <OwnerChip name={managerName} size="sm" />
                    </span>
                )}
                {attendees && attendees.length > 0 && (
                    <div
                        className="flex items-center"
                        title={`${attendees.length} attendee${attendees.length === 1 ? '' : 's'}`}
                    >
                        {attendees.slice(0, 5).map((name, i) => (
                            <span
                                key={`${name}-${i}`}
                                title={name}
                                className="w-6 h-6 rounded-full bg-brand-tint text-brand text-[9px] font-bold grid place-items-center border-2 border-white first:ml-0 -ml-2"
                            >
                                {avatarInitials(name)}
                            </span>
                        ))}
                        {attendees.length > 5 && (
                            <span className="w-6 h-6 rounded-full bg-gray-100 text-gray-500 text-[9px] font-bold grid place-items-center border-2 border-white -ml-2">
                                +{attendees.length - 5}
                            </span>
                        )}
                    </div>
                )}
            </div>
        )}

        {meeting.google_event_id && (
            <div className="mb-3 flex items-center gap-2">
                <p
                    className="text-[10px] uppercase tracking-wider text-gray-400 inline-flex items-center gap-1"
                    title={
                        meeting.last_imported_date
                            ? `Last synced from Google Calendar on ${formatImportedDate(meeting.last_imported_date)}`
                            : undefined
                    }
                >
                    <CalendarDays size={10} />
                    Imported from Google Calendar
                </p>
                {isCalendarCancelled(meeting, calendarStatus) && (
                    <span
                        className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider bg-amber-50 text-amber-700 border border-amber-200 px-2 py-0.5 rounded-full"
                        title={`${cancellationReason(calendarStatus)}, so Cubie has stopped counting the weekly slot forward. Put the event back on the calendar and hit Sync to bring it back, or delete this meeting when the team is done with it.`}
                    >
                        <CalendarOff size={10} />
                        {cancellationLabel(calendarStatus)}
                    </span>
                )}
                {onSync && (
                    <button
                        type="button"
                        onClick={() => onSync(meeting)}
                        disabled={syncing}
                        className="inline-flex items-center gap-1 text-[10px] font-medium text-brand hover:text-brand-hover hover:underline disabled:opacity-50"
                        title="Pull the latest time and attendees from the Google Calendar event"
                    >
                        <RefreshCw size={10} className={syncing ? 'animate-spin' : ''} />
                        {syncing ? 'Syncing…' : 'Sync'}
                    </button>
                )}
            </div>
        )}

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-sm text-gray-500 mb-4">
            <span className="inline-flex items-center gap-1.5">
                <Clock size={14} />
                {/* The next occurrence — Google's own answer when we have it,
                    the stored weekly pattern otherwise, and an explicit "not
                    happening" when the calendar series was cancelled. */}
                {scheduleLabel(meeting, calendarStatus)}
            </span>
            <span className="inline-flex items-center gap-1.5">
                <Users size={14} />
                {meeting.duration_minutes} minutes
            </span>
            {meeting.secondary_link_label && meeting.secondary_link_url && (
                <a
                    href={meeting.secondary_link_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 text-brand hover:text-brand-hover hover:underline"
                    title={meeting.secondary_link_url}
                >
                    <ExternalLink size={14} />
                    {meeting.secondary_link_label}
                </a>
            )}
        </div>

        <div className="flex gap-2">
            <button
                type="button"
                onClick={() => onDiary(meeting)}
                className="flex items-center justify-center gap-1.5 flex-1 px-3 py-2 bg-white border border-gray-300 hover:border-brand hover:text-brand text-gray-700 rounded-md text-sm font-medium transition-colors"
                title="Browse past sessions of this L10 (read-only for team, editable for the manager)"
            >
                <History size={14} />
                History
            </button>
            <button
                type="button"
                onClick={() => onPrep(meeting)}
                className="flex items-center justify-center gap-1.5 flex-1 px-3 py-2 bg-white border border-gray-300 hover:border-brand hover:text-brand text-gray-700 rounded-md text-sm font-medium transition-colors"
                title="Add headlines and park ideas before the session starts — they show up in the meeting's Headlines section"
            >
                <NotebookPen size={14} />
                Add headlines
            </button>
            {meeting.live_session_id ? (
                <>
                    {restartable && onRestart && (
                        <button
                            type="button"
                            onClick={() => onRestart(meeting)}
                            disabled={restartingId === meeting.id}
                            className="flex items-center justify-center gap-1.5 px-3 py-2 bg-white border border-brand/30 hover:border-brand hover:bg-brand/5 text-brand rounded-md text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                            title="Restart — reset this meeting to the top of the agenda with a fresh timer"
                        >
                            <RotateCcw
                                size={14}
                                className={restartingId === meeting.id ? 'animate-spin' : ''}
                            />
                            Restart
                        </button>
                    )}
                    <button
                        type="button"
                        onClick={() => onStart(meeting.id)}
                        className="flex items-center justify-center gap-2 flex-1 px-3 py-2 bg-green-600 hover:bg-green-700 text-white rounded-md text-sm font-medium transition-colors"
                        title="This L10 is in progress — rejoin the live session"
                    >
                        <Play size={14} fill="currentColor" />
                        Rejoin
                    </button>
                </>
            ) : isStartableToday(meeting, calendarStatus) ? (
                <button
                    type="button"
                    onClick={() => onStart(meeting.id)}
                    className="flex items-center justify-center gap-2 flex-1 px-3 py-2 bg-brand hover:bg-brand-hover text-white rounded-md text-sm font-medium transition-colors"
                >
                    <Play size={14} fill="currentColor" />
                    Start
                </button>
            ) : (
                <button
                    type="button"
                    disabled
                    title={
                        isCalendarCancelled(meeting, calendarStatus)
                            ? "This L10's Google Calendar event was cancelled, so there's no occurrence to start. Put it back on the calendar and hit Sync, or delete the meeting."
                            : `This L10 can only be started on its scheduled day (${dayLabel(meeting.day_of_week)}).`
                    }
                    className="flex items-center justify-center gap-2 flex-1 px-3 py-2 bg-gray-100 text-gray-400 border border-gray-200 rounded-md text-sm font-medium cursor-not-allowed"
                >
                    <Clock size={14} />
                    {startsLabel(meeting, calendarStatus)}
                </button>
            )}
        </div>
    </div>
);

// Per-person meeting-survey breakdown for a finished session, shown
// in the past-meetings diary. Lazily fetches /sessions/{id}/survey on expand.
interface SurveyResult {
    person_id: string;
    person_name: string | null;
    is_manager: boolean;
    rating: number | null;
    comment: string | null;
}
interface SurveyResp {
    session_id: string;
    average_rating: number | null;
    submitted_count: number;
    results: SurveyResult[];
}

// Cubie AI recap of a finished session — lazily generated on expand and
// cached server-side, mirroring the lazy SessionSurveyBreakdown pattern below.
const SessionRecap: React.FC<{ sessionId: string }> = ({ sessionId }) => {
    const [open, setOpen] = useState(false);
    const [recap, setRecap] = useState<string | null>(null);
    const [loaded, setLoaded] = useState(false);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const load = async (regenerate: boolean) => {
        setLoading(true);
        setError(null);
        try {
            const res = regenerate
                ? await client.post<{ recap: string | null }>(
                      `/api/eos/l10/sessions/${sessionId}/recap/regenerate`,
                  )
                : await client.get<{ recap: string | null }>(
                      `/api/eos/l10/sessions/${sessionId}/recap`,
                  );
            setRecap(res.data.recap);
            setLoaded(true);
        } catch (err) {
            const code = (err as { response?: { status?: number } }).response?.status;
            if (regenerate) {
                // Keep the already-loaded recap on screen; surface the failure as
                // a toast rather than blanking known-good content.
                toast.error(
                    code === 403
                        ? "Only the meeting's manager (or an admin) can regenerate the recap."
                        : 'Cubie could not regenerate the recap — the previous one is still shown.',
                );
            } else {
                setError('Cubie could not generate a recap for this session.');
            }
        } finally {
            setLoading(false);
        }
    };

    const toggle = () => {
        const next = !open;
        setOpen(next);
        if (next && !loaded && !loading) void load(false);
    };

    return (
        <div className="mt-3 border-t border-gray-100 pt-2.5">
            <button
                type="button"
                onClick={toggle}
                aria-expanded={open}
                className="w-full flex items-center gap-1.5 text-xs font-semibold text-brand hover:text-brand-hover"
            >
                <Sparkles size={13} />
                Cubie recap
                {open ? (
                    <ChevronDown size={13} className="ml-auto" />
                ) : (
                    <ChevronRight size={13} className="ml-auto" />
                )}
            </button>
            {open && (
                <div className="mt-2">
                    {loading && (
                        <p className="text-xs text-gray-400 italic">
                            Cubie is writing the recap…
                        </p>
                    )}
                    {error && !loading && <p className="text-xs text-red-600">{error}</p>}
                    {loaded &&
                        !loading &&
                        !error &&
                        (recap ? (
                            <>
                                <p className="text-sm text-gray-700 whitespace-pre-line leading-relaxed rounded-md border border-brand/20 bg-brand/5 p-3">
                                    {recap}
                                </p>
                                <button
                                    type="button"
                                    onClick={() => void load(true)}
                                    className="mt-2 inline-flex items-center gap-1 text-[11px] font-medium text-gray-500 hover:text-brand"
                                    title="Ask Cubie to write a fresh recap (manager / admin)"
                                >
                                    <RefreshCw size={11} />
                                    Regenerate
                                </button>
                            </>
                        ) : (
                            <p className="text-xs text-gray-400 italic">
                                No recap available — the in-meeting AI is turned off, or there
                                wasn't enough recorded to summarize.
                            </p>
                        ))}
                </div>
            )}
        </div>
    );
};

const SessionSurveyBreakdown: React.FC<{ sessionId: string }> = ({ sessionId }) => {
    const [open, setOpen] = useState(false);
    const [data, setData] = useState<SurveyResp | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const toggle = async () => {
        const next = !open;
        setOpen(next);
        if (next && data === null && !loading) {
            setLoading(true);
            try {
                const res = await client.get<SurveyResp>(
                    `/api/eos/l10/sessions/${sessionId}/survey`,
                );
                setData(res.data);
            } catch (err) {
                setError(err instanceof Error ? err.message : 'Failed to load survey');
            } finally {
                setLoading(false);
            }
        }
    };

    return (
        <div className="mt-2 border-t border-gray-100 pt-2">
            <button
                type="button"
                onClick={toggle}
                aria-expanded={open}
                className="text-[11px] font-medium text-gray-500 hover:text-brand"
            >
                {open ? 'Hide individual survey' : 'View individual survey'}
            </button>
            {open && (
                <div className="mt-2">
                    {loading && (
                        <p className="text-[11px] text-gray-400 italic">Loading…</p>
                    )}
                    {error && <p className="text-[11px] text-red-500 italic">{error}</p>}
                    {data && data.results.length === 0 && (
                        <p className="text-[11px] text-gray-400 italic">
                            No individual ratings were submitted for this meeting.
                        </p>
                    )}
                    {data && data.results.length > 0 && (
                        <>
                            <p className="text-[11px] text-gray-500 mb-1">
                                {data.submitted_count} submitted · avg{' '}
                                <span className="font-semibold text-brand">
                                    {data.average_rating ?? '—'}
                                </span>
                                /10
                            </p>
                            <ul className="space-y-1">
                                {data.results.map(r => (
                                    <li
                                        key={r.person_id}
                                        className="flex items-start justify-between gap-3 text-xs border-l-2 border-gray-200 pl-2"
                                    >
                                        <div className="min-w-0">
                                            <span className="font-medium text-gray-800">
                                                {r.person_name ?? 'Someone'}
                                            </span>
                                            {r.is_manager && (
                                                <span className="ml-1.5 text-[9px] uppercase tracking-wider font-semibold text-brand bg-brand/10 px-1 py-0.5 rounded-full">
                                                    Manager
                                                </span>
                                            )}
                                            {r.comment && (
                                                <p className="text-gray-600 italic mt-0.5">
                                                    "{r.comment}"
                                                </p>
                                            )}
                                        </div>
                                        <span className="shrink-0 font-mono tabular-nums text-gray-700">
                                            {r.rating != null ? `${r.rating}/10` : '—'}
                                        </span>
                                    </li>
                                ))}
                            </ul>
                        </>
                    )}
                </div>
            )}
        </div>
    );
};

export const Meetings: React.FC = () => {
    const { isAdmin, isExec, isManager, managedDepartments, user, hasFeature } = useAuth();
    const navigate = useNavigate();
    const ownDepartment = user?.department ?? '';
    // Team Managers (non-exec) can schedule / edit their own team's L10s too
    // — not just admins and execs.
    const canCreate = isAdmin || isExec || isManager;
    // Restart a live L10: the server's verdict for `l10_meeting_restart`
    // (Admin → Feature Access; admins only by default).
    const canUseRestart = hasFeature('l10_meeting_restart');
    // Default the Scope picker to the most useful option for the caller:
    // Exec users tend to schedule the Exec L10 first; a Team Manager lands
    // on the first team they lead; everyone else on their own department.
    const defaultScope = isExec
        ? EXEC_SCOPE
        : (ownDepartment || managedDepartments[0] || '');

    const [meetings, setMeetings] = useState<L10Meeting[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    // Tracks the meeting whose Delete request is in flight so the trash
    // button can show a disabled state on that card only (not across the
    // whole list).
    const [deletingId, setDeletingId] = useState<string | null>(null);
    // Meeting whose manager-availability check is in flight (the check can hit
    // the manager's calendar, so the button disables while it runs).
    const [checkingHandoffId, setCheckingHandoffId] = useState<string | null>(null);

    // Tracks the meeting whose Restart request is in flight (spinner on that
    // row/card only).
    const [restartingId, setRestartingId] = useState<string | null>(null);

    // Admin-only dropdown data, fetched lazily when the modal opens for the
    // first time so non-admins don't burn a request.
    const [, setDepartments] = useState<string[]>([]);
    const [people, setPeople] = useState<PersonLite[]>([]);
    const [dropdownsLoaded, setDropdownsLoaded] = useState(false);

    // Lookup maps so each card can resolve manager_id -> name and
    // roster email -> display name from the eagerly-loaded people list.
    const peopleById = useMemo(
        () => new Map(people.map(p => [p.id, p])),
        [people],
    );
    const peopleByEmail = useMemo(
        () => new Map(people.map(p => [p.email.toLowerCase(), p])),
        [people],
    );

    const [modalOpen, setModalOpen] = useState(false);
    const [form, setForm] = useState<FormState>(EMPTY_FORM);
    const [submitting, setSubmitting] = useState(false);
    // When non-null, the New Meeting modal switches to edit mode — name
    // becomes "Edit Meeting", Save sends PATCH instead of POST, and the
    // form is pre-filled from the existing meeting (including manager,
    // scope, schedule, link slot). Crucial for transferring the manager
    // role on a calendar-imported meeting whose organizer can't attend.
    const [editingMeeting, setEditingMeeting] = useState<L10Meeting | null>(null);

    // Pre-meeting prep modal — headlines + ideas for an upcoming L10
    type L10InputKind = 'headline' | 'idea';
    interface L10Input {
        id: string;
        meeting_id: string;
        kind: L10InputKind;
        text: string;
        discussed: boolean;
        created_by: string | null;
        created_at: string;
    }
    const [prepOpen, setPrepOpen] = useState(false);
    const [prepMeeting, setPrepMeeting] = useState<L10Meeting | null>(null);
    const [prepItems, setPrepItems] = useState<L10Input[]>([]);
    const [prepLoading, setPrepLoading] = useState(false);
    const [prepKind, setPrepKind] = useState<L10InputKind>('headline');
    const [prepDraft, setPrepDraft] = useState('');
    const [prepSubmitting, setPrepSubmitting] = useState(false);
    // The visible tab's queue, ordered the way the in-meeting Headlines list
    // orders it: still-to-cover newest first, already-handled after them.
    // Someone adding prep five minutes before the L10 should see their own
    // item without scrolling past last week's.
    const orderedPrepItems = useMemo(() => {
        const { pending, covered } = partitionL10Inputs(
            prepItems.filter(i => i.kind === prepKind),
        );
        return [...pending, ...covered];
    }, [prepItems, prepKind]);
    // FT: convert a prep idea into an IDS Issue or a To-Do. `convertRowId` is
    // the idea whose convert panel is open; `convertBusyId` the one whose
    // request is in flight; `convertOwnerId` the To-Do owner ('' = the caller).
    const [convertRowId, setConvertRowId] = useState<string | null>(null);
    const [convertBusyId, setConvertBusyId] = useState<string | null>(null);
    const [convertOwnerId, setConvertOwnerId] = useState<string>('');

    // Past-meetings diary — modal listing prior closed sessions
    // for a given L10. Manager can inline-edit the notes of any session.
    interface L10Session {
        id: string;
        meeting_id: string;
        actual_start: string;
        actual_end: string | null;
        average_rating: number | null;
        notes: string | null;
        is_manager?: boolean;
        created_by?: string | null;
    }
    const [diaryOpen, setDiaryOpen] = useState(false);
    const [diaryMeeting, setDiaryMeeting] = useState<L10Meeting | null>(null);
    const [diarySessions, setDiarySessions] = useState<L10Session[]>([]);
    const [diaryLoading, setDiaryLoading] = useState(false);
    const [diaryError, setDiaryError] = useState<string | null>(null);
    const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
    const [editingNotes, setEditingNotes] = useState('');
    const [editingSubmitting, setEditingSubmitting] = useState(false);

    // Calendar ingest — the next 5 events on the caller's calendar
    // whose title contains "L10". Surfaces above the manual meetings
    // grid so the team can see their actual upcoming sessions without
    // having to re-schedule them inside Cubie. (Shape: IngestedL10Event,
    // declared at module scope.)
    const [calendarEvents, setCalendarEvents] = useState<IngestedL10Event[]>([]);
    const [calendarAvailable, setCalendarAvailable] = useState<boolean>(true);
    const [calendarLoading, setCalendarLoading] = useState<boolean>(true);
    // Per-event-id flags so the Import button shows "Importing…" only on
    // the row the user clicked, not across the whole strip.
    const [importingEventId, setImportingEventId] = useState<string | null>(null);

    // What Google says about each imported meeting's series right now, keyed by
    // meeting id. Fetched SEPARATELY from the meetings list (one Google
    // round-trip per imported meeting) so the board paints immediately and
    // then corrects itself, instead of blocking behind a third party.
    const [calendarStatus, setCalendarStatus] = useState<
        Record<string, L10CalendarStatus>
    >({});

    // Promote a calendar event to a real L10Meeting via
    // POST /api/eos/l10/calendar-import. Server resolves organizer +
    // attendees to Person rows (creating placeholder rows for unknown
    // emails) and stamps the meeting with google_event_id so re-imports
    // upsert instead of duplicating.
    const importByEventId = async (eventId: string) => {
        setImportingEventId(eventId);
        try {
            await client.post<L10Meeting>('/api/eos/l10/calendar-import', {
                google_event_id: eventId,
            });
            // The backend upserts (and may consolidate duplicate rows), so
            // refetch the list rather than merging locally — deleted
            // duplicates must drop out and the survivor must refresh.
            // Silent refetch (no loading flag) so the page doesn't flash.
            const listed = await client.get<L10Meeting[]>('/api/eos/l10/');
            setMeetings(listed.data);
            // The event stays in the strip — it now renders as
            // "In Cubie ✓ / Sync" instead of Import, which teaches the link
            // between the calendar event and the meeting card.
        } catch (err) {
            console.error('Calendar import failed', err);
            const code = (err as { response?: { status?: number } }).response?.status;
            if (code === 503) {
                toast.error(
                    "Google Calendar isn't configured on this instance. The event can't be imported automatically — create the meeting manually with the New Meeting button.",
                );
            } else {
                toast.error('Failed to import this calendar event. Please retry.');
            }
        } finally {
            setImportingEventId(null);
        }
    };

    const importCalendarEvent = (ev: IngestedL10Event) => {
        if (!ev.google_event_id) return;
        void importByEventId(ev.google_event_id);
    };

    // Card-level "Sync": re-import using the stored series id, pulling the
    // latest time/attendees from Google Calendar onto the existing meeting.
    const syncMeetingFromCalendar = (m: L10Meeting) => {
        if (!m.google_event_id) return;
        void importByEventId(m.google_event_id);
    };

    const fetchMeetings = async () => {
        try {
            setError(null);
            setLoading(true);
            const res = await client.get<L10Meeting[]>('/api/eos/l10/');
            setMeetings(res.data);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Unknown error');
        } finally {
            setLoading(false);
        }
    };

    // (Re)load the calendar-ingested upcoming L10s. Shared by the
    // mount effect and the Refresh button. Failures are soft — the manual
    // grid still renders if Calendar is down / unconfigured.
    const fetchCalendarUpcoming = async () => {
        try {
            setCalendarLoading(true);
            const res = await client.get<{
                events: IngestedL10Event[];
                calendar_available: boolean;
            }>('/api/eos/l10/calendar-upcoming', { params: { limit: 5 } });
            setCalendarEvents(res.data.events ?? []);
            setCalendarAvailable(Boolean(res.data.calendar_available));
        } catch (err) {
            console.warn('Calendar ingest failed (non-blocking)', err);
            setCalendarAvailable(false);
        } finally {
            setCalendarLoading(false);
        }
    };

    // Reconcile each imported meeting against its live Google Calendar series.
    // Non-blocking and failure-tolerant by design: if this never returns, the
    // board still renders from the stored schedule. The one thing it must not
    // do is turn an outage into a cancellation, which is why an 'unknown' state
    // is stored as-is and read as "we don't know" (see isCalendarCancelled).
    const fetchCalendarStatus = async () => {
        try {
            const res = await client.get<{
                calendar_available: boolean;
                statuses: L10CalendarStatus[];
            }>('/api/eos/l10/calendar-status');
            const byId: Record<string, L10CalendarStatus> = {};
            for (const st of res.data.statuses ?? []) byId[st.meeting_id] = st;
            setCalendarStatus(byId);
        } catch (err) {
            console.warn('Calendar status check failed (non-blocking)', err);
        }
    };

    // Refresh button — reload the meetings grid, the calendar strip, and the
    // per-meeting calendar reconciliation.
    const refreshAll = () => {
        void fetchMeetings();
        void fetchCalendarUpcoming();
        void fetchCalendarStatus();
    };

    // Multi-team scope filter — replaces the old "show every department"
    // dropdown. Backend filters scopes to:
    //   admin → all departments + Exec team
    //   exec  → Exec + own dept + managed_departments
    //   user  → own dept only
    // When only one scope is returned, the frontend hides the dropdown
    // entirely (regular users don't see clutter they can't act on).
    interface MeetingScope {
        kind: 'exec' | 'department';
        label: string;
        value: string; // EXEC_SCOPE sentinel or raw department name
        // False for a team the caller only reaches by sitting on its L10
        // roster. Those are FILING scopes (Issues / To-Dos / Rocks pick them
        // up) but not scheduling ones — the backend's
        // `_require_department_scope` 403s a create/edit for a team you don't
        // own or manage, so this dropdown must filter them out rather than
        // offer a save that cannot succeed. Optional: older responses omit it.
        can_schedule?: boolean;
    }
    const [scopes, setScopes] = useState<MeetingScope[]>([]);
    const [, setScopesLoaded] = useState(false);
    // Team filter over the meeting cards: 'all' | '__exec__' | department.
    // Filtering the meetings also scopes their prep (headlines & ideas).
    const [teamFilter, setTeamFilter] = useState<string>('all');

    // Options for the view filter (TeamFilter) — derived from the meetings
    // the backend actually returned, NOT from the create/edit `scopes` (which
    // for an admin list every department, including teams they have no visible
    // meeting in). GET /api/eos/l10/ now returns only meetings the caller
    // belongs to, so the filter should offer exactly those teams. TeamFilter
    // hides itself when fewer than two scopes exist, so single-team users see
    // no dropdown. Shape matches TeamScope (structurally identical).
    const meetingFilterScopes = useMemo<MeetingScope[]>(() => {
        const derived: MeetingScope[] = [];
        if (meetings.some(m => m.is_exec_meeting)) {
            derived.push({ kind: 'exec', label: EXEC_SCOPE_LABEL, value: EXEC_SCOPE });
        }
        const seen = new Set<string>();
        for (const m of meetings) {
            if (m.is_exec_meeting) continue;
            const name = m.department_name?.trim();
            if (!name) continue;
            const key = name.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            derived.push({ kind: 'department', label: name, value: name });
        }
        return derived;
    }, [meetings]);

    const ensureDropdowns = async (): Promise<MeetingScope[]> => {
        if (dropdownsLoaded) return scopes;
        try {
            // /api/eos/me/meeting-scopes replaces the raw departments list for
            // the scope picker. People are already preloaded once on mount (for
            // the cards' facilitator + attendees), so we don't refetch them here.
            const sRes = await client.get<{ scopes: MeetingScope[] }>(
                '/api/eos/me/meeting-scopes',
            );
            // Roster-only scopes can't be scheduled into — drop them here so
            // the create/edit Scope dropdown only ever offers saveable teams.
            const scopeList = (sRes.data.scopes ?? []).filter(
                s => s.can_schedule !== false,
            );
            setScopes(scopeList);
            // Keep the legacy `departments` state populated for
            // backward-compat (other parts of the form / non-admin
            // labels read from it) — derive it from the scope list.
            setDepartments(
                scopeList
                    .filter(s => s.kind === 'department')
                    .map(s => s.value),
            );
            setScopesLoaded(true);
            setDropdownsLoaded(true);
            // Return the freshly-fetched list because React state
            // updates above won't be visible to the *current* tick of
            // the caller's `await` continuation.
            return scopeList;
        } catch (err) {
            console.error('Failed to preload dropdowns', err);
            return scopes;
        }
    };

    useEffect(() => {
        let cancelled = false;
        (async () => {
            if (!cancelled) await fetchMeetings();
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    // Fetch the calendar-ingested L10s in parallel on mount. Failures
    // are soft — the manual meetings grid still renders even if Calendar is
    // down or unconfigured (e.g. local dev without GWS keys).
    useEffect(() => {
        void fetchCalendarUpcoming();
    }, []);

    // Reconcile the imported meetings against Google on mount, in parallel with
    // everything else. Mount-only, so it never re-fires on a state change.
    useEffect(() => {
        void fetchCalendarStatus();
    }, []);

    // Preload the people list on mount so cards can show the facilitator
    // and attendee avatars (previously fetched only when a modal opened). A
    // direct fetch (rather than ensureDropdowns) keeps this effect free of an
    // unstable function dependency; the modal's own lazy load still runs.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const res = await client.get<PersonLite[]>('/api/eos/people/');
                if (!cancelled) setPeople(res.data);
            } catch (err) {
                console.error('Failed to preload people for cards', err);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    const openNew = async () => {
        setEditingMeeting(null);
        // Fire ensureDropdowns first so we can pick a sensible default
        // scope from the user's actual allowed list — beats stale
        // assumptions about defaultScope when the user's been added to
        // managed_departments since their last reload.
        const loadedScopes = await ensureDropdowns();
        // Default to Exec team for exec users; otherwise the first
        // department they can schedule for. Falls back to defaultScope
        // (own department) when ensureDropdowns failed silently.
        const firstScope =
            loadedScopes.find(s => s.kind === 'exec')?.value
            ?? loadedScopes.find(s => s.kind === 'department')?.value
            ?? defaultScope;
        setForm({ ...EMPTY_FORM, scope: firstScope });
        setModalOpen(true);
    };

    // Open the meeting modal in EDIT mode pre-filled from an existing
    // meeting. The crucial case is a calendar-imported meeting whose
    // organizer can't always run that day's L10 — the manager picker
    // here lets anyone reassign without going back to Google Calendar.
    const openEditMeeting = async (meeting: L10Meeting) => {
        // Always load the people list first so the manager dropdown
        // can render the existing selection (the dropdown options are
        // built from `people`, which is empty for non-admins until
        // ensureDropdowns runs).
        await ensureDropdowns();
        // Map the stored scope (department_name + is_exec_meeting) back
        // into the same single Scope select used by the create form.
        const scope = meeting.is_exec_meeting
            ? EXEC_SCOPE
            : meeting.department_name ?? '';
        setEditingMeeting(meeting);
        setForm({
            name: meeting.name,
            scope,
            manager_id: meeting.manager_id ?? '',
            day_of_week: meeting.day_of_week ?? 0,
            start_time: meeting.start_time ?? '09:00',
            duration_minutes: meeting.duration_minutes,
            secondary_link_label: meeting.secondary_link_label ?? '',
            secondary_link_url: meeting.secondary_link_url ?? '',
        });
        setModalOpen(true);
    };

    const onSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setSubmitting(true);
        try {
            const isExecScope = form.scope === EXEC_SCOPE;
            const payload: Record<string, unknown> = {
                name: form.name,
                day_of_week: form.day_of_week,
                start_time: form.start_time,
                duration_minutes: form.duration_minutes,
                is_exec_meeting: isExecScope,
            };
            // Department only goes on the wire for non-exec scopes. When the
            // Exec team is selected, the meeting is cross-cutting and the
            // department association is intentionally left null. In edit
            // mode we send null explicitly so flipping from Dept → Exec
            // actually clears the previous department.
            if (isExecScope) {
                if (editingMeeting) payload.department_name = null;
            } else if (form.scope) {
                payload.department_name = form.scope;
            } else if (editingMeeting) {
                payload.department_name = null;
            }
            // Manager: PATCH semantics — null clears the manager.
            payload.manager_id = form.manager_id || null;
            // Only send link fields when both are filled. The frontend
            // refuses to render a half-configured link (label without URL or
            // vice versa) so don't persist that state in the first place.
            // In edit mode, send nulls to clear a previously-set link.
            const linkLabel = form.secondary_link_label.trim();
            const linkUrl = form.secondary_link_url.trim();
            if (linkLabel && linkUrl) {
                payload.secondary_link_label = linkLabel;
                payload.secondary_link_url = linkUrl;
            } else if (editingMeeting) {
                payload.secondary_link_label = null;
                payload.secondary_link_url = null;
            }

            if (editingMeeting) {
                const res = await client.patch<L10Meeting>(
                    `/api/eos/l10/${editingMeeting.id}`,
                    payload,
                );
                setMeetings(prev =>
                    prev.map(m => (m.id === editingMeeting.id ? res.data : m)),
                );
            } else {
                const res = await client.post<L10Meeting>('/api/eos/l10/', payload);
                setMeetings(prev => [...prev, res.data]);
            }
            setModalOpen(false);
            setEditingMeeting(null);
        } catch (err) {
            console.error('Failed to save meeting', err);
            const code = (err as { response?: { status?: number } }).response?.status;
            if (code === 403) {
                toast.error(
                    "Only this meeting's manager, your team's leader, or an admin can edit it. If the manager is away, use the take-over button (the person icon) instead.",
                );
            } else {
                toast.error(editingMeeting ? 'Failed to save changes.' : 'Failed to create meeting.');
            }
        } finally {
            setSubmitting(false);
        }
    };

    // "Manager away?" — the escape hatch for a team whose facilitator left for
    // vacation without handing the L10 over. Before this, only that person (or
    // an admin, who can't even SEE other teams' meetings) could re-point it, so
    // the team was locked out of its own L10 until they came back.
    //
    // Two steps on purpose. The availability check runs first so the user gets a
    // straight answer instead of a 403: they may already be able to just edit the
    // meeting, or the manager may be perfectly reachable, and both are worth
    // saying plainly. Only when the manager is genuinely unreachable do we offer
    // the takeover — and the server re-verifies all of it on the write.
    const takeOverMeeting = async (meeting: L10Meeting) => {
        setCheckingHandoffId(meeting.id);
        try {
            const { data } = await client.get<ManagerAvailability>(
                `/api/eos/l10/${meeting.id}/manager-availability`,
            );

            if (data.your_authority) {
                toast.success(
                    `You already manage this meeting${
                        data.your_authority === 'manager' ? '' : " as your team's leader"
                    } — use Edit to change the facilitator.`,
                );
                return;
            }
            if (!data.can_take_over) {
                // Either the manager is available, or the caller has no EOS
                // profile to be made manager with. `detail` says which.
                toast.error(data.detail);
                return;
            }

            if (
                !(await confirmDialog({
                    title: `Take over ${meeting.name}?`,
                    message: `${data.detail}\n\nYou'll become the manager of this meeting, so you can start it and drive the agenda. ${
                        data.manager_name ?? 'The previous manager'
                    } can take it back at any time, and the change is logged.`,
                    confirmLabel: 'Take over',
                }))
            ) {
                return;
            }

            const { data: updated } = await client.post<L10Meeting>(
                `/api/eos/l10/${meeting.id}/transfer-manager`,
                {
                    new_manager_id: data.your_person_id,
                    note: `takeover — ${data.reason}`,
                },
            );
            setMeetings(prev => prev.map(m => (m.id === updated.id ? updated : m)));
            toast.success(`You now manage ${meeting.name}.`);
        } catch (err) {
            console.error('Failed to take over meeting', err);
            const res = (err as { response?: { status?: number; data?: { detail?: string } } })
                .response;
            // The server's 403/404 detail is the specific reason (manager is
            // back, no EOS profile, not your meeting) — show it rather than a
            // generic failure, since each one has a different next step.
            toast.error(res?.data?.detail ?? 'Could not take over this meeting.');
        } finally {
            setCheckingHandoffId(null);
        }
    };

    const startSession = (meetingId: string) => {
        // Gate: a meeting is only startable on its scheduled weekday (so only
        // this week's L10 is live, not a future week's slot). The disabled
        // buttons already prevent this; this is a safety net for any other
        // call path.
        const m = meetings.find(x => x.id === meetingId);
        // A live session can always be rejoined; otherwise only on the
        // scheduled weekday.
        if (m && !m.live_session_id && !isStartableToday(m, calendarStatus[meetingId]))
            return;
        // The meeting's own page, which hosts the session runner.
        navigate(`/eos/meetings/${meetingId}`);
    };

    // Restart a live meeting: reset the session to the top of the agenda with
    // a fresh timer (server-side), then drop into the freshly-reset meeting.
    // Offered only to people the server puts on `l10_meeting_restart`, never
    // on the Exec team's L10 (canRestartMeeting); the server still enforces
    // the real feature + manager/admin/creator gate.
    const restartMeeting = async (meeting: L10Meeting) => {
        if (!meeting.live_session_id) return;
        if (
            !(await confirmDialog({
                title: 'Restart meeting?',
                message: `Everyone currently in "${meeting.name}" jumps back to the top of the agenda with a fresh timer. Notes and check-ins already captured are kept.`,
                confirmLabel: 'Restart',
            }))
        ) {
            return;
        }
        setRestartingId(meeting.id);
        try {
            await client.post(
                `/api/eos/l10/sessions/${meeting.live_session_id}/restart`,
            );
            navigate(`/eos/meetings/${meeting.id}`);
        } catch (err) {
            console.error('Failed to restart meeting', err);
            const code = (err as { response?: { status?: number } }).response?.status;
            if (code === 403) {
                toast.error(
                    "Only the meeting's manager (or an admin) can restart it, never the Exec team's L10, and only once an admin has turned Restart on for you.",
                );
            } else if (code === 409) {
                toast.error(
                    'That meeting has already been concluded — start a fresh one instead.',
                );
                void fetchMeetings();
            } else {
                toast.error('Failed to restart the meeting. Please retry.');
            }
        } finally {
            setRestartingId(null);
        }
    };

    // Soft-delete a meeting via DELETE /api/eos/l10/{id}. Backend
    // returns 204 on success, 403 when the caller isn't the manager
    // (or an admin), 404 if it was already deleted. We optimistically
    // drop the row from local state on 2xx; on failure we leave the
    // list intact and surface the reason.
    const deleteMeeting = async (meeting: L10Meeting) => {
        const label = meeting.is_exec_meeting
            ? 'the Exec L10'
            : meeting.department_name
                ? `the ${meeting.department_name} L10`
                : 'this meeting';
        if (
            !(await confirmDialog({
                title: `Delete ${label}?`,
                message: `"${meeting.name}" will disappear from this page. Past sessions and their notes stay accessible by direct link, but the recurring slot will be removed.`,
                confirmLabel: 'Delete',
                destructive: true,
            }))
        ) {
            return;
        }
        setDeletingId(meeting.id);
        try {
            await client.delete(`/api/eos/l10/${meeting.id}`);
            setMeetings(prev => prev.filter(m => m.id !== meeting.id));
        } catch (err) {
            console.error('Failed to delete meeting', err);
            const code = (err as { response?: { status?: number } }).response?.status;
            if (code === 403) {
                toast.error(
                    "Only this meeting's manager, your team's leader, or an admin can delete it. If the manager is away, take the meeting over first (the person icon).",
                );
            } else if (code === 404) {
                // Already gone — sync local state so the row disappears.
                setMeetings(prev => prev.filter(m => m.id !== meeting.id));
            } else {
                toast.error('Failed to delete meeting. Please retry.');
            }
        } finally {
            setDeletingId(null);
        }
    };

    // ── pre-meeting prep ────────────────────────────────────────────

    const openPrep = async (meeting: L10Meeting) => {
        setPrepMeeting(meeting);
        setPrepOpen(true);
        setPrepDraft('');
        setPrepKind('headline');
        setConvertRowId(null);
        setConvertOwnerId('');
        // Load the people list (for the idea→To-Do owner picker) lazily.
        // Soft-fails for non-admins — the convert then defaults the owner to
        // the caller server-side, so the feature still works without a picker.
        void ensureDropdowns();
        setPrepLoading(true);
        try {
            const res = await client.get<L10Input[]>(
                `/api/eos/l10/meetings/${meeting.id}/inputs`,
            );
            setPrepItems(res.data);
        } catch (err) {
            console.error('Failed to load prep inputs', err);
            setPrepItems([]);
        } finally {
            setPrepLoading(false);
        }
    };

    const closePrep = () => {
        setPrepOpen(false);
        setPrepMeeting(null);
        setPrepItems([]);
    };

    const submitPrep = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!prepMeeting) return;
        const text = prepDraft.trim();
        if (!text || prepSubmitting) return;
        setPrepSubmitting(true);
        try {
            const res = await client.post<L10Input>(
                `/api/eos/l10/meetings/${prepMeeting.id}/inputs`,
                { meeting_id: prepMeeting.id, kind: prepKind, text, discussed: false },
            );
            setPrepItems(prev => [...prev, res.data]);
            setPrepDraft('');
        } catch (err) {
            console.error('Failed to add prep input', err);
            toast.error('Failed to add — please retry.');
        } finally {
            setPrepSubmitting(false);
        }
    };

    const removePrepItem = async (row: L10Input) => {
        if (
            !(await confirmDialog({
                title: `Delete ${row.kind}?`,
                message: `"${row.text}"`,
                confirmLabel: 'Delete',
                destructive: true,
            }))
        )
            return;
        try {
            await client.delete(`/api/eos/l10/inputs/${row.id}`);
            setPrepItems(prev => prev.filter(i => i.id !== row.id));
        } catch (err) {
            console.error('Failed to delete prep input', err);
        }
    };

    // Promote a parking-lot idea into a real IDS Issue or a To-Do. Team scope
    // (department / exec) and the l10 back-pointer are inherited server-side
    // from the meeting; the idea is kept but flagged handled so it drops off
    // the live queue. For a To-Do, `ownerId` '' means "assign to me" (the
    // backend resolves the caller's own Person).
    const convertPrepIdea = async (
        row: L10Input,
        target: 'issue' | 'todo',
        ownerId?: string,
    ) => {
        if (convertBusyId) return;
        setConvertBusyId(row.id);
        try {
            const res = await client.post<{ input: L10Input }>(
                `/api/eos/l10/inputs/${row.id}/convert`,
                {
                    target,
                    owner_id:
                        target === 'todo' ? ownerId || undefined : undefined,
                    mark_discussed: true,
                },
            );
            // Reflect the source idea's updated `discussed` flag in place.
            setPrepItems(prev =>
                prev.map(i => (i.id === row.id ? res.data.input : i)),
            );
            setConvertRowId(null);
        } catch (err) {
            console.error('Failed to convert idea', err);
            const code = (err as { response?: { status?: number } }).response
                ?.status;
            if (code === 403) {
                toast.error(
                    'You don’t have permission to add items to this team’s IDS / To-Dos.',
                );
            } else {
                toast.error('Could not convert this idea — please retry.');
            }
        } finally {
            setConvertBusyId(null);
        }
    };

    // ── past-meetings diary ────────────────────────────────────────

    const openDiary = async (meeting: L10Meeting) => {
        setDiaryMeeting(meeting);
        setDiaryOpen(true);
        setDiarySessions([]);
        setDiaryError(null);
        setEditingSessionId(null);
        setDiaryLoading(true);
        try {
            const res = await client.get<L10Session[]>(
                '/api/eos/l10/sessions',
                { params: { meeting_id: meeting.id, finished_only: true, limit: 50 } },
            );
            setDiarySessions(res.data);
        } catch (err) {
            setDiaryError(err instanceof Error ? err.message : 'Failed to load history');
        } finally {
            setDiaryLoading(false);
        }
    };

    const closeDiary = () => {
        setDiaryOpen(false);
        setDiaryMeeting(null);
        setDiarySessions([]);
        setDiaryError(null);
        setEditingSessionId(null);
    };

    const startEditingNotes = (session: L10Session) => {
        setEditingSessionId(session.id);
        setEditingNotes(session.notes ?? '');
    };

    const cancelEditingNotes = () => {
        setEditingSessionId(null);
        setEditingNotes('');
    };

    const saveEditedNotes = async (session: L10Session) => {
        if (editingSubmitting) return;
        setEditingSubmitting(true);
        try {
            // The backend's PATCH /sessions/{id} accepts the same shape as
            // POST sessions/start (L10SessionCreate). meeting_id +
            // actual_start are required by the schema even though the
            // server ignores them for updates — we echo the session's
            // existing values to keep the payload valid.
            const res = await client.patch<L10Session>(
                `/api/eos/l10/sessions/${session.id}`,
                {
                    meeting_id: session.meeting_id,
                    actual_start: session.actual_start,
                    notes: editingNotes,
                },
            );
            setDiarySessions(prev =>
                prev.map(s => (s.id === session.id ? { ...s, notes: res.data.notes } : s)),
            );
            setEditingSessionId(null);
        } catch (err) {
            console.error('Failed to save session notes', err);
            const code = (err as { response?: { status?: number } }).response?.status;
            if (code === 403) {
                toast.error("Only the meeting's manager (or an admin) can edit past notes.");
            } else {
                toast.error('Failed to save notes — please retry.');
            }
        } finally {
            setEditingSubmitting(false);
        }
    };

    return (
        <PageContainer>
            <EosPageHeader
                icon={<Calendar size={20} />}
                title="L10 Meetings"
                subtitle="Weekly pulse of the organization"
                right={
                    <div className="flex items-center gap-2">
                        <TeamFilter
                            scopes={meetingFilterScopes}
                            value={teamFilter}
                            onChange={setTeamFilter}
                        />
                        <button
                            type="button"
                            onClick={refreshAll}
                            className="flex items-center gap-2 px-3 py-2 bg-white border border-gray-300 hover:border-brand hover:text-brand text-gray-700 rounded-md text-sm font-medium transition-colors"
                        >
                            <RefreshCw size={16} /> Refresh
                        </button>
                        {canCreate && (
                            <button
                                type="button"
                                onClick={openNew}
                                className="flex items-center gap-2 px-3 py-2 bg-brand hover:bg-brand-hover text-white rounded-md text-sm font-medium transition-colors"
                            >
                                <Plus size={16} /> New Meeting
                            </button>
                        )}
                    </div>
                }
            />

            {/* Calendar-ingested upcoming L10s. Renders above the
                manual grid so the team sees their actual upcoming
                meetings without having to schedule them inside Cubie.
                Hides itself entirely when calendar isn't available or
                there's nothing matching "L10" in the title. */}
            {!calendarLoading && calendarAvailable && calendarEvents.length > 0 && (
                <div className="mb-6 bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden" data-tour="eos-meetings-calendar">
                    <div className="px-5 py-3 border-b border-gray-100 flex items-center justify-between">
                        <div className="flex items-center gap-2">
                            <CalendarDays size={16} className="text-brand" />
                            <h2 className="text-sm font-semibold text-gray-900">
                                Next L10s on your calendar
                            </h2>
                            <span className="text-xs text-gray-400">
                                {calendarEvents.length} upcoming
                            </span>
                        </div>
                        <span className="text-[10px] uppercase tracking-wider text-gray-400">
                            Ingested from Google Calendar
                        </span>
                    </div>
                    <ul className="divide-y divide-gray-100">
                        {calendarEvents.map(ev => {
                            const start = ev.start ? new Date(ev.start) : null;
                            const startLabel = start
                                ? start.toLocaleString(undefined, {
                                      weekday: 'short',
                                      month: 'short',
                                      day: 'numeric',
                                      hour: 'numeric',
                                      minute: '2-digit',
                                  })
                                : 'TBD';
                            return (
                                <li
                                    key={ev.google_event_id ?? ev.summary + (ev.start ?? '')}
                                    className="px-5 py-3 flex items-center justify-between gap-3 hover:bg-gray-50/50"
                                >
                                    <div className="min-w-0">
                                        <div className="text-sm font-medium text-gray-900 truncate">
                                            {ev.summary}
                                        </div>
                                        <div className="text-xs text-gray-500 flex items-center gap-3 mt-0.5 flex-wrap">
                                            <span className="inline-flex items-center gap-1">
                                                <Clock size={11} />
                                                {startLabel}
                                            </span>
                                            {ev.attendees.length > 0 && (
                                                <span className="inline-flex items-center gap-1">
                                                    <Users size={11} />
                                                    {ev.attendees.length} attendee
                                                    {ev.attendees.length === 1 ? '' : 's'}
                                                </span>
                                            )}
                                            {ev.location && (
                                                <span className="truncate max-w-xs">
                                                    📍 {ev.location}
                                                </span>
                                            )}
                                        </div>
                                    </div>
                                    <div className="shrink-0 flex items-center gap-2">
                                        {/* Already-imported series: show the
                                            link instead of tempting a second
                                            Import — the meeting card below is
                                            the source of truth; Sync just
                                            refreshes it from the calendar. */}
                                        {ev.google_event_id &&
                                            (eventAlreadyImported(ev, meetings) ? (
                                                <>
                                                    <span
                                                        className="flex items-center gap-1 px-2 py-1 text-xs text-green-700 bg-green-50 border border-green-200 rounded-md"
                                                        title="This meeting is already in Cubie — see its card below"
                                                    >
                                                        <Check size={11} />
                                                        In Cubie
                                                    </span>
                                                    <button
                                                        type="button"
                                                        onClick={() => importCalendarEvent(ev)}
                                                        disabled={
                                                            importingEventId === ev.google_event_id
                                                        }
                                                        className="flex items-center gap-1 px-2.5 py-1 text-xs text-brand bg-white border border-brand/30 hover:bg-brand/5 disabled:opacity-50 rounded-md"
                                                        title="Refresh the meeting's time and attendees from this calendar event"
                                                    >
                                                        <RefreshCw
                                                            size={11}
                                                            className={
                                                                importingEventId === ev.google_event_id
                                                                    ? 'animate-spin'
                                                                    : ''
                                                            }
                                                        />
                                                        {importingEventId === ev.google_event_id
                                                            ? 'Syncing…'
                                                            : 'Sync'}
                                                    </button>
                                                </>
                                            ) : (
                                                <button
                                                    type="button"
                                                    onClick={() => importCalendarEvent(ev)}
                                                    disabled={
                                                        importingEventId === ev.google_event_id
                                                    }
                                                    className="flex items-center gap-1 px-2.5 py-1 text-xs text-white bg-brand hover:bg-brand-hover disabled:opacity-50 rounded-md"
                                                    title="Bring this meeting into Cubie so you can start sessions, log history, and track roster access"
                                                >
                                                    <Plus size={11} />
                                                    {importingEventId === ev.google_event_id
                                                        ? 'Importing…'
                                                        : 'Import'}
                                                </button>
                                            ))}
                                        {ev.hangout_link && (
                                            <a
                                                href={ev.hangout_link}
                                                target="_blank"
                                                rel="noopener noreferrer"
                                                className="flex items-center gap-1 px-2.5 py-1 text-xs text-brand bg-white border border-brand/30 hover:bg-brand/5 rounded-md"
                                                title="Open the Google Meet link"
                                            >
                                                <ExternalLink size={11} /> Join
                                            </a>
                                        )}
                                    </div>
                                </li>
                            );
                        })}
                    </ul>
                </div>
            )}

            {loading && <LoadingState label="Loading meetings…" />}
            {error && <ErrorState message={error} />}

            {!loading && !error && meetings.length === 0 && (
                <EmptyState
                    title="No L10 meetings yet."
                    hint={
                        canCreate
                            ? 'Click "New Meeting" above to schedule the first one.'
                            : 'Ask an admin (or someone on the Exec team) to create the first L10 meeting.'
                    }
                />
            )}

            {!loading && !error && meetings.length > 0 && (() => {
                const teamMeetings = meetings.filter(m => {
                    if (teamFilter === 'all') return true;
                    if (teamFilter === EXEC_SCOPE) return !!m.is_exec_meeting;
                    return (
                        !m.is_exec_meeting &&
                        (m.department_name ?? '').toLowerCase() ===
                            teamFilter.toLowerCase()
                    );
                });
                if (teamMeetings.length === 0) {
                    return <EmptyState title="No meetings for this team." />;
                }
                // Every meeting the page receives is one the caller may open
                // — the backend (GET /api/eos/l10/) returns other teams' L10s
                // only to the org-wide Exec, never to admins — so render them
                // all as full cards. There is no "Other L10 meetings" section
                // anymore. The "My L10s" header is only meaningful when the
                // caller sits on more than one.
                return (
                    <div className="space-y-4" data-tour="eos-meetings-list">
                        {teamMeetings.length > 1 && (
                            <div className="flex items-center gap-2 text-sm font-semibold text-gray-700">
                                <Calendar size={16} className="text-brand" />
                                My L10s
                                <span className="text-xs text-gray-400 font-normal">
                                    ({teamMeetings.length}) · your team and any
                                    other L10s you sit on
                                </span>
                            </div>
                        )}
                        {teamMeetings.map(pm => {
                            // Resolve facilitator + attendees from the
                            // eagerly-loaded people list (fall back to the email
                            // local-part when someone is outside the caller's scope).
                            const mp = pm.manager_id
                                ? peopleById.get(pm.manager_id)
                                : undefined;
                            const managerName = mp ? formatPerson(mp) : null;
                            const attendees = (pm.roster_emails ?? []).map(e => {
                                const p = peopleByEmail.get(e.toLowerCase());
                                return p ? formatPerson(p) : e.split('@')[0];
                            });
                            return (
                            <MeetingCard
                                key={pm.id}
                                meeting={pm}
                                managerName={managerName}
                                attendees={attendees}
                                onEdit={openEditMeeting}
                                onDiary={openDiary}
                                onPrep={openPrep}
                                onStart={startSession}
                                onDelete={deleteMeeting}
                                deletingId={deletingId}
                                onRestart={restartMeeting}
                                restartable={canRestartMeeting(pm, {
                                    mayRunMeetings: canCreate,
                                    restartFeature: canUseRestart,
                                })}
                                restartingId={restartingId}
                                onSync={syncMeetingFromCalendar}
                                syncing={
                                    !!pm.google_event_id &&
                                    importingEventId === pm.google_event_id
                                }
                                calendarStatus={calendarStatus[pm.id]}
                                onTakeOver={takeOverMeeting}
                                checkingHandoffId={checkingHandoffId}
                            />
                            );
                        })}
                    </div>
                );
            })()}

            <Modal
                isOpen={modalOpen}
                onClose={() => {
                    setModalOpen(false);
                    setEditingMeeting(null);
                }}
                title={
                    editingMeeting
                        ? `Edit Meeting — ${editingMeeting.name}`
                        : 'New L10 Meeting'
                }
            >
                <form onSubmit={onSubmit} className="space-y-4">
                    <Field label="Meeting title">
                        <input
                            required
                            type="text"
                            value={form.name}
                            onChange={e => setForm({ ...form, name: e.target.value })}
                            className={inputCls}
                            placeholder='e.g. "Sales Team L10"'
                            autoFocus
                        />
                    </Field>

                    <div className="grid grid-cols-2 gap-3">
                        {/* Scope picker — driven by /api/eos/me/meeting-scopes.
                            Hidden completely when only one scope is available
                            (regular user with a single department) since
                            picking is meaningless. Exec / admin users see the
                            real dropdown. */}
                        {scopes.length <= 1 ? (
                            // Hidden but still render the disabled value so
                            // form layout doesn't shift in edit mode; we keep
                            // form.scope synced via the openNew/openEditMeeting
                            // setters.
                            <div className="text-xs text-gray-500 self-center">
                                <span className="font-medium text-gray-700">
                                    {scopes[0]?.label ?? ownDepartment ?? '—'}
                                </span>
                                <div className="text-[10px] text-gray-400 mt-0.5">
                                    Access via calendar invite roster
                                </div>
                            </div>
                        ) : (
                            <Field label="Team">
                                <select
                                    required
                                    value={form.scope}
                                    onChange={e => setForm({ ...form, scope: e.target.value })}
                                    className={inputCls}
                                >
                                    {isAdmin && <option value="">— None —</option>}
                                    {scopes.map(s => (
                                        <option key={s.value} value={s.value}>
                                            {s.label}
                                        </option>
                                    ))}
                                </select>
                            </Field>
                        )}
                        <Field label="Manager (optional)">
                            <select
                                value={form.manager_id}
                                onChange={e => setForm({ ...form, manager_id: e.target.value })}
                                className={inputCls}
                            >
                                <option value="">— None —</option>
                                {people.map(p => (
                                    <option key={p.id} value={p.id}>
                                        {p.first_name} {p.last_name}
                                    </option>
                                ))}
                            </select>
                        </Field>
                    </div>

                    <div className="grid grid-cols-3 gap-3">
                        <Field label="Day">
                            <select
                                value={form.day_of_week}
                                onChange={e =>
                                    setForm({ ...form, day_of_week: parseInt(e.target.value, 10) })
                                }
                                className={inputCls}
                            >
                                {DAY_LABELS.map((label, idx) => (
                                    <option key={idx} value={idx}>
                                        {label}
                                    </option>
                                ))}
                            </select>
                        </Field>
                        <Field label="Start">
                            <input
                                required
                                type="time"
                                value={form.start_time}
                                onChange={e => setForm({ ...form, start_time: e.target.value })}
                                className={inputCls}
                            />
                        </Field>
                        <Field label="Duration (min)">
                            <input
                                required
                                type="number"
                                min={15}
                                step={15}
                                value={form.duration_minutes}
                                onChange={e =>
                                    setForm({
                                        ...form,
                                        duration_minutes: parseInt(e.target.value, 10) || 90,
                                    })
                                }
                                className={inputCls}
                            />
                        </Field>
                    </div>

                    {/* Configurable link slot for transient initiatives.
                        Both fields optional; the meeting only renders the link
                        when label AND url are non-empty. */}
                    <details className="border border-gray-200 rounded-md">
                        <summary className="cursor-pointer px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50">
                            Optional: pin a tracker link (e.g. "What Matters Most")
                        </summary>
                        <div className="grid grid-cols-2 gap-3 p-3 border-t border-gray-200">
                            <Field label="Link label">
                                <input
                                    type="text"
                                    value={form.secondary_link_label}
                                    onChange={e =>
                                        setForm({ ...form, secondary_link_label: e.target.value })
                                    }
                                    className={inputCls}
                                    placeholder='e.g. "What Matters Most 2026"'
                                />
                            </Field>
                            <Field label="Link URL">
                                <input
                                    type="url"
                                    value={form.secondary_link_url}
                                    onChange={e =>
                                        setForm({ ...form, secondary_link_url: e.target.value })
                                    }
                                    className={inputCls}
                                    placeholder="https://docs.example.com/wmm"
                                />
                            </Field>
                        </div>
                    </details>

                    <SubmitButton
                        disabled={submitting || !form.name.trim()}
                        onCancel={() => {
                            setModalOpen(false);
                            setEditingMeeting(null);
                        }}
                    >
                        {editingMeeting ? 'Save changes' : 'Create meeting'}
                    </SubmitButton>
                </form>
            </Modal>

            {/* Pre-meeting prep — headlines + ideas */}
            <Modal
                isOpen={prepOpen}
                onClose={closePrep}
                title={
                    prepMeeting
                        ? `Prep — ${prepMeeting.name}`
                        : 'Pre-meeting prep'
                }
                maxWidth="max-w-3xl"
            >
                {prepMeeting && (
                    <div className="space-y-4">
                        <p className="text-sm text-gray-500">
                            Drop headlines (wins, complaints, signals) and parking-lot ideas
                            here before the L10 starts. Anyone on the team can add — they show
                            up in the Headlines section when the session opens. Ready to act on
                            one? Use <span className="font-medium">Convert</span> to turn it
                            into an IDS Issue or an owned To-Do.
                        </p>

                        {/* Kind tabs */}
                        <div className="flex gap-1 border-b border-gray-200">
                            {(['headline', 'idea'] as L10InputKind[]).map(k => {
                                const count = prepItems.filter(i => i.kind === k).length;
                                return (
                                    <button
                                        key={k}
                                        type="button"
                                        onClick={() => setPrepKind(k)}
                                        className={`px-3 py-2 text-sm font-medium -mb-px border-b-2 transition-colors ${
                                            prepKind === k
                                                ? 'border-brand text-brand'
                                                : 'border-transparent text-gray-500 hover:text-gray-700'
                                        }`}
                                    >
                                        {k === 'headline'
                                            ? `Headlines (${count})`
                                            : `Parking lot (${count})`}
                                    </button>
                                );
                            })}
                        </div>

                        {/* Compose */}
                        <form onSubmit={submitPrep} className="flex gap-2">
                            <input
                                value={prepDraft}
                                onChange={e => setPrepDraft(e.target.value)}
                                placeholder={
                                    prepKind === 'headline'
                                        ? 'e.g. "Acme renewed — closed Friday"'
                                        : 'e.g. "Notify Slack on off-track rocks?"'
                                }
                                className={inputCls}
                                autoFocus
                            />
                            <button
                                type="submit"
                                disabled={!prepDraft.trim() || prepSubmitting}
                                className="px-3 py-2 bg-brand hover:bg-brand-hover disabled:opacity-50 text-white text-sm font-medium rounded-md whitespace-nowrap"
                            >
                                Add
                            </button>
                        </form>

                        {/* List */}
                        {prepLoading && (
                            <p className="text-xs text-gray-500 italic">Loading…</p>
                        )}
                        {!prepLoading && (
                            <ul className="space-y-1.5 max-h-72 overflow-y-auto">
                                {/* Ordered like the in-meeting Headlines list: still to cover
                                    first (newest at the top), anything already handled after. */}
                                {orderedPrepItems.map(row => (
                                    <li
                                        key={row.id}
                                        className={`rounded-md border bg-white ${
                                            row.discussed
                                                ? 'border-gray-100 opacity-70'
                                                : 'border-gray-200'
                                        }`}
                                    >
                                        <div className="flex items-start gap-3 p-2.5">
                                            <div className="flex-1 min-w-0">
                                                <p className="text-sm text-gray-900">{row.text}</p>
                                                <div className="flex items-center gap-2 mt-0.5">
                                                    {row.created_by && (
                                                        <span className="text-[10px] text-gray-400">
                                                            by {row.created_by}
                                                        </span>
                                                    )}
                                                    {row.discussed && (
                                                        <span className="text-[10px] text-green-600 inline-flex items-center gap-0.5">
                                                            <Check size={10} /> handled
                                                        </span>
                                                    )}
                                                </div>
                                            </div>
                                            <div className="flex items-center gap-2 shrink-0">
                                                {prepKind === 'idea' && !row.discussed && (
                                                    <button
                                                        type="button"
                                                        onClick={() => {
                                                            // Reset the owner each time a panel
                                                            // opens so idea B never inherits the
                                                            // owner picked for idea A.
                                                            setConvertOwnerId('');
                                                            setConvertRowId(id =>
                                                                id === row.id ? null : row.id,
                                                            );
                                                        }}
                                                        className="text-xs text-gray-500 hover:text-brand inline-flex items-center gap-1"
                                                        title="Convert to IDS Issue or To-Do"
                                                    >
                                                        <ChevronDown
                                                            size={12}
                                                            className={
                                                                convertRowId === row.id
                                                                    ? 'rotate-180 transition-transform'
                                                                    : 'transition-transform'
                                                            }
                                                        />
                                                        Convert
                                                    </button>
                                                )}
                                                <button
                                                    type="button"
                                                    onClick={() => removePrepItem(row)}
                                                    className="text-xs text-gray-400 hover:text-red-600"
                                                    title="Delete"
                                                >
                                                    <Trash2 size={12} />
                                                </button>
                                            </div>
                                        </div>
                                        {prepKind === 'idea' &&
                                            convertRowId === row.id && (
                                                <div className="px-2.5 pb-2.5 pt-1 border-t border-gray-100 flex flex-wrap items-center gap-2">
                                                    <span className="text-[11px] text-gray-500">
                                                        Turn this idea into:
                                                    </span>
                                                    <button
                                                        type="button"
                                                        disabled={convertBusyId === row.id}
                                                        onClick={() =>
                                                            convertPrepIdea(row, 'issue')
                                                        }
                                                        className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-gray-300 hover:border-brand hover:text-brand disabled:opacity-50"
                                                    >
                                                        <Flag size={12} /> IDS Issue
                                                    </button>
                                                    <div className="inline-flex items-center gap-1">
                                                        {people.length > 0 && (
                                                            <select
                                                                value={convertOwnerId}
                                                                onChange={e =>
                                                                    setConvertOwnerId(
                                                                        e.target.value,
                                                                    )
                                                                }
                                                                className="text-xs border border-gray-300 rounded-md px-1.5 py-1 max-w-[9rem]"
                                                                title="To-Do owner"
                                                            >
                                                                <option value="">Me</option>
                                                                {people.map(p => (
                                                                    <option
                                                                        key={p.id}
                                                                        value={p.id}
                                                                    >
                                                                        {formatPerson(p)}
                                                                    </option>
                                                                ))}
                                                            </select>
                                                        )}
                                                        <button
                                                            type="button"
                                                            disabled={convertBusyId === row.id}
                                                            onClick={() =>
                                                                convertPrepIdea(
                                                                    row,
                                                                    'todo',
                                                                    convertOwnerId,
                                                                )
                                                            }
                                                            className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-gray-300 hover:border-brand hover:text-brand disabled:opacity-50"
                                                        >
                                                            <ListTodo size={12} /> To-Do
                                                        </button>
                                                    </div>
                                                </div>
                                            )}
                                    </li>
                                ))}
                                {orderedPrepItems.length === 0 && (
                                    <li className="text-xs text-gray-500 italic py-4 text-center">
                                        {prepKind === 'headline'
                                            ? 'No headlines queued — add the first one above.'
                                            : 'No ideas queued — add the first one above.'}
                                    </li>
                                )}
                            </ul>
                        )}
                    </div>
                )}
            </Modal>

            {/* Past-meetings diary — running history of finished L10s. */}
            <Modal
                isOpen={diaryOpen}
                onClose={closeDiary}
                title={
                    diaryMeeting
                        ? `History — ${diaryMeeting.name}`
                        : 'Past meetings'
                }
                maxWidth="max-w-4xl"
            >
                {diaryMeeting && (
                    <div className="space-y-4">
                        <p className="text-sm text-gray-500">
                            Past sessions newest-first. Team members can read; the meeting's
                            manager can edit notes after the fact for clarity or corrections.
                        </p>

                        {diaryLoading && (
                            <p className="text-xs text-gray-500 italic">Loading…</p>
                        )}
                        {diaryError && (
                            <p className="text-xs text-red-600">{diaryError}</p>
                        )}
                        {!diaryLoading && !diaryError && diarySessions.length === 0 && (
                            <p className="text-xs text-gray-500 italic">
                                No finished sessions yet. After a session is wrapped up via
                                <strong> Finish &amp; rate</strong>, it'll show up here.
                            </p>
                        )}

                        {!diaryLoading && diarySessions.length > 0 && (
                            <ul className="space-y-3">
                                {diarySessions.map(session => {
                                    const start = parseInstant(session.actual_start);
                                    const end = parseInstant(session.actual_end);
                                    const durationMin =
                                        start !== null && end !== null
                                            ? Math.max(
                                                  1,
                                                  Math.round(
                                                      (end.getTime() - start.getTime()) / 60000,
                                                  ),
                                              )
                                            : null;
                                    const isEditing = editingSessionId === session.id;
                                    return (
                                        <li
                                            key={session.id}
                                            className="border border-gray-200 rounded-md p-3 bg-white"
                                        >
                                            <div className="flex items-center justify-between mb-2 gap-3">
                                                <div className="text-sm text-gray-900 font-medium">
                                                    {start
                                                        ? start.toLocaleString(undefined, {
                                                              dateStyle: 'medium',
                                                              timeStyle: 'short',
                                                          })
                                                        : '—'}
                                                </div>
                                                <div className="flex items-center gap-3 text-xs text-gray-500">
                                                    {durationMin != null && (
                                                        <span>{durationMin} min</span>
                                                    )}
                                                    {session.average_rating != null && (
                                                        <span className="font-mono">
                                                            ★ {session.average_rating}/10
                                                        </span>
                                                    )}
                                                </div>
                                            </div>

                                            {!isEditing && (
                                                <div className="flex items-start justify-between gap-3">
                                                    <div className="flex-1 min-w-0 text-xs text-gray-700 whitespace-pre-wrap">
                                                        {session.notes?.trim() ? (
                                                            session.notes
                                                        ) : (
                                                            <span className="italic text-gray-400">
                                                                No notes recorded for this session.
                                                            </span>
                                                        )}
                                                    </div>
                                                    <button
                                                        type="button"
                                                        onClick={() => startEditingNotes(session)}
                                                        className="shrink-0 text-xs text-gray-500 hover:text-brand flex items-center gap-1"
                                                        title="Edit (manager only — backend returns 403 otherwise)"
                                                    >
                                                        <Edit2 size={12} /> Edit
                                                    </button>
                                                </div>
                                            )}

                                            {isEditing && (
                                                <div className="space-y-2">
                                                    <textarea
                                                        value={editingNotes}
                                                        onChange={e =>
                                                            setEditingNotes(e.target.value)
                                                        }
                                                        className={inputCls}
                                                        rows={4}
                                                        autoFocus
                                                        placeholder="Notes for this session…"
                                                    />
                                                    <div className="flex justify-end gap-2">
                                                        <button
                                                            type="button"
                                                            onClick={cancelEditingNotes}
                                                            className="px-2 py-1 text-xs text-gray-600 bg-white border border-gray-300 hover:bg-gray-50 rounded-md flex items-center gap-1"
                                                        >
                                                            <X size={12} /> Cancel
                                                        </button>
                                                        <button
                                                            type="button"
                                                            disabled={editingSubmitting}
                                                            onClick={() => saveEditedNotes(session)}
                                                            className="px-2 py-1 text-xs text-white bg-brand hover:bg-brand-hover disabled:opacity-50 rounded-md flex items-center gap-1"
                                                        >
                                                            <Save size={12} />
                                                            {editingSubmitting ? 'Saving…' : 'Save'}
                                                        </button>
                                                    </div>
                                                </div>
                                            )}

                                            {/* Individual meeting-survey breakdown. */}
                                            <SessionRecap sessionId={session.id} />
                                            <SessionSurveyBreakdown sessionId={session.id} />
                                        </li>
                                    );
                                })}
                            </ul>
                        )}
                    </div>
                )}
            </Modal>
        </PageContainer>
    );
};
