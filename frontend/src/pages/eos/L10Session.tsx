import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
    AlertTriangle,
    CheckCircle2,
    ChevronDown,
    ChevronRight,
    Flag,
    Users,
    BarChart3,
    Target,
    MessageSquare,
    ListTodo,
    HelpCircle,
    Info,
    ThumbsUp,
    Trash2,
    UserCog,
    ExternalLink,
    Rabbit,
    Compass,
} from 'lucide-react';
import client from '../../api/client';
import { useAuth } from '../../context/AuthContext';
import { ErrorState, LoadingState } from './_shared';
import { Modal, Field, inputCls, SubmitButton } from '../../components/eos/Modal';
import { toast } from '../../components/eos/Toast';
import { ScorecardGrid } from '../../components/eos/ScorecardGrid';
import { confirmDialog } from '../../components/eos/ConfirmDialog';
import { RabbitHoleAnimation } from '../../components/eos/RabbitHoleAnimation';
import { Tooltip } from '../../components/Tooltip';
import { useTours } from '../../tours/context';
import {
    formatPerson,
    formatScorecardGoal,
    formatScorecardValue,
    fromEditableScorecardValue,
    goalMet,
    partitionL10Inputs,
    scorecardSaveError,
    toEditableScorecardValue,
    type PersonLite,
    type ScorecardValueType,
} from './_utils';
// Every clock in this file reads a backend timestamp, and the backend sends
// naive UTC. `parseInstant` and friends are what stop the room's clocks from
// being read as local time — see `utils/datetime.ts`.
import { formatInstant, formatInstantTime, instantMs } from '../../utils/datetime';
// The in-meeting Rock Review / To-Do List / IDS segments now render the exact
// same shared Views as the standalone pages, so the UI + write logic live in
// one place. The L10 host stays the fetch/state owner and reconciles the write
// results these Views report back (suppressing the 2s live poll each time).
import { TodosView } from '../../components/eos/TodosView';
import type { Todo, TodoChange } from '../../components/eos/TodosView';
import { RocksView } from '../../components/eos/RocksView';
import type { Rock, RockChange } from '../../components/eos/RocksView';
import { IssuesView } from '../../components/eos/IssuesView';
import type { Issue, IssueChange } from '../../components/eos/IssuesView';
import type { MeetingContext } from '../../components/eos/eosViewTypes';

// ─── Types matching backend response shapes ─────────────────────────────────

interface L10SessionResp {
    id: string;
    meeting_id: string;
    actual_start: string;
    is_manager?: boolean;
    // Live cursor — present so a (re)joining client restores the real step +
    // timer anchor instead of starting at step 0 with a fresh countdown.
    current_step?: number;
    step_started_at?: string | null;
    is_paused?: boolean;
    state_version?: number;
}

// Shared live-session state polled from GET /sessions/{id}/state.
interface L10SessionStateResp {
    session_id: string;
    current_step: number;
    // Meeting-level clock anchor. Only moves when the meeting is restarted —
    // which is exactly when a tab already in the room has to re-anchor.
    actual_start: string;
    step_started_at: string | null;
    is_paused: boolean;
    state_version: number;
    manager_id: string | null;
    server_now: string;
    actual_end: string | null;
    attendance_rev: string | null;
    items_rev: string | null;
}

// Shape we care about from GET /api/eos/l10/{meeting_id}
// (only the fields the sidebar uses; the real response has more).
interface L10MeetingLite {
    id: string;
    name: string;
    // Needed to scope the rolling scorecard
    // grid + headlines lookback to this team's data.
    department_name?: string | null;
    is_exec_meeting?: boolean;
    secondary_link_label?: string | null;
    secondary_link_url?: string | null;
    // Who runs the meeting, so the survey can flag the manager's row.
    manager_id?: string | null;
    // Invitee roster (emails from the imported calendar event). Null / empty
    // for manually-scheduled meetings. Also carries the meeting's visibility
    // ACL elsewhere, so the picker uses `attendee_emails` (below) instead.
    roster_emails?: string[] | null;
    // Effective attendee pool for the Check-in picker: the invitee roster if
    // there is one, else the meeting's team (department or Exec members).
    // Empty only when the team has no members (picker then falls back to all).
    attendee_emails?: string[] | null;
    // The same pool resolved to people. Merged into the caller's own EOS
    // roster (`sessionPeople`) because the pool legitimately contains
    // attendees outside the caller's team — a cross-department leader of this
    // team, or an invited guest — who are absent from /api/eos/people.
    attendees?: PersonLite[] | null;
}

interface ScorecardMetric {
    id: string;
    name: string;
    // The goal, both as authored and as the comparator + target it falls back
    // to. get_meeting_items serialises the whole metric row, so these are
    // present — the meeting renders the same goal as the Scorecard page.
    goal_description?: string | null;
    target_value: string | null;
    comparator?: string | null;
    unit?: string | null;
    // Render hint (currency | percent | number). Needed in the meeting so
    // a percent value is shown — and typed — as a percent, not a fraction.
    value_type: ScorecardValueType | null;
    on_track: boolean;
}

// The in-meeting Rocks / To-Dos / Issues reuse the shared View types imported
// above (`Rock`, `Todo`, `Issue`). The get_meeting_items payload carries the
// enriched rocks + raw ORM to-dos/issues those types describe, so the segments
// render identically to the standalone pages.

interface MeetingItems {
    // Lightweight meeting payload so the sidebar can render the
    // configurable link slot without a second round-trip.
    meeting?: L10MeetingLite;
    metrics?: ScorecardMetric[];
    rocks?: Rock[];
    todos?: Todo[];
    issues?: Issue[];
}

interface AttendanceRow {
    id: string;
    session_id: string;
    person_id: string;
    rating: number | null;
    // Per-person end-of-meeting survey comment (rides with `rating`).
    rating_comment: string | null;
    attended: boolean;
    // Segue check-in: Personal + Professional are separate so both of a
    // person's entries persist instead of overwriting each other.
    checkin_personal_content: string | null;
    checkin_personal_rating: number | null;
    checkin_professional_content: string | null;
    checkin_professional_rating: number | null;
}

// ─── Agenda configuration ───────────────────────────────────────────────────

interface AgendaStep {
    id: string;
    label: string;
    duration: number; // minutes
    icon: React.ComponentType<{ size?: number }>;
}

const AGENDA: AgendaStep[] = [
    { id: 'checkin', label: 'Check-in', duration: 5, icon: Users },
    { id: 'scorecard', label: 'Scorecard', duration: 5, icon: BarChart3 },
    { id: 'rocks', label: 'Rock Review', duration: 5, icon: Target },
    { id: 'headlines', label: 'Headlines', duration: 5, icon: MessageSquare },
    { id: 'todos', label: 'To-Do List', duration: 5, icon: ListTodo },
    { id: 'ids', label: 'IDS', duration: 60, icon: HelpCircle },
    { id: 'conclude', label: 'Conclude', duration: 5, icon: ThumbsUp },
];

// Steps whose panel is a wide table or list rather than a form. The workspace
// caps everything else at max-w-4xl (good for reading a check-in or the closing
// notes); at that width the Scorecard grid shows about five of its thirteen
// weeks, and an IDS issue's notes are typed in a column narrower than the text
// in them. These four get the full workspace instead.
const WIDE_STEPS = new Set(['scorecard', 'rocks', 'todos', 'ids']);

const fmtTime = (seconds: number): string => {
    const abs = Math.abs(seconds);
    const m = Math.floor(abs / 60);
    const s = abs % 60;
    return `${m}:${s < 10 ? '0' : ''}${s}`;
};

// This week's Sunday (local midnight), matching the standalone
// Scorecard page's getPastWeeks so an in-meeting entry hits the same row.
const _currentWeekStart = (): Date => {
    const d = new Date();
    d.setDate(d.getDate() - d.getDay());
    d.setHours(0, 0, 0, 0);
    return d;
};

// ─── State reconcilers (shared with the standalone page hosts) ──────────────
// The shared Views report writes back as change events; the host applies them
// to its own array. Identical to the reducers in Todos.tsx / Rocks.tsx /
// Issues.tsx — in the L10 the onChange handler ALSO bumps the poll-suppression
// window (via suppressPoll) so the 2s live poll can't clobber the optimistic edit.

const applyTodoChange = (prev: Todo[], c: TodoChange): Todo[] => {
    switch (c.type) {
        case 'upsert':
            return prev.some(t => t.id === c.todo.id)
                ? prev.map(t => (t.id === c.todo.id ? c.todo : t))
                : [...prev, c.todo];
        case 'remove':
            return prev.filter(t => t.id !== c.id);
        case 'set-status':
            return prev.map(t => (t.id === c.id ? { ...t, status: c.status } : t));
    }
};

const applyIssueChange = (prev: Issue[], c: IssueChange): Issue[] => {
    switch (c.type) {
        case 'upsert':
            return prev.some(i => i.id === c.issue.id)
                ? prev.map(i => (i.id === c.issue.id ? c.issue : i))
                : [...prev, c.issue];
        case 'remove':
            return prev.filter(i => i.id !== c.id);
        case 'set':
            return prev.map(i => (i.id === c.id ? { ...i, ...c.patch } : i));
    }
};

const applyRockChange = (prev: Rock[], c: RockChange): Rock[] => {
    switch (c.type) {
        case 'upsert':
            return prev.some(r => r.id === c.rock.id)
                ? prev.map(r => (r.id === c.rock.id ? c.rock : r))
                : [...prev, c.rock];
        case 'remove':
            return prev.filter(r => r.id !== c.id);
        case 'set':
            return prev.map(r => (r.id === c.id ? { ...r, ...c.patch } : r));
    }
};

// ─── Master pace bar ─────────────────────────────────────────────────────────
// The step timer answers "how long is left in THIS segment"; a facilitator also
// needs "how much of the 90 have we burned, and will IDS get its hour?". This
// strip draws every agenda segment to scale, fills the elapsed portion, reads
// out wall-clock time left against the whole box, and flags on-pace vs over.
//
// It is purely derived — no new state, no new fetch. Everything comes from the
// same anchors the countdown already uses (session start, the current step's
// server-anchored start, and the corrected server clock), so it self-corrects
// on a (re)join and stays in lock-step with the sidebar timer. Its colours are
// the `brand` design tokens, like the rest of this file.
const TOTAL_PLANNED_SEC = AGENDA.reduce((sum, a) => sum + a.duration * 60, 0);

const MeetingPaceBar: React.FC<{
    stepIdx: number;
    // session.actual_start as epoch ms.
    actualStartMs: number;
    // The current step's server-anchored start (epoch ms), or null before the
    // first /state sync establishes it.
    stepStartedAt: number | null;
    // The corrected server clock (epoch ms), refreshed each countdown tick by
    // the parent so this component stays pure — no Date.now()/ref read here.
    serverNowMs: number;
}> = ({ stepIdx, actualStartMs, stepStartedAt, serverNowMs }) => {
    const durationSec = AGENDA[stepIdx].duration * 60;
    // Wall-time spent in the current step, derived from the SAME anchors as
    // stepIdx (the step's server-anchored start + the shared clock), NOT from
    // `timeLeft`. `timeLeft` is recomputed one render later by the countdown
    // effect, so reading it here would briefly disagree with a just-advanced
    // stepIdx and flash the fill (e.g. the 60-min IDS block painting full for a
    // frame on entry). These anchors move together, so the fill never lies.
    const rawElapsedSec =
        stepStartedAt != null
            ? Math.max(0, Math.floor((serverNowMs - stepStartedAt) / 1000))
            : 0;
    const elapsedInStep = Math.min(rawElapsedSec, durationSec);
    const overInStep = Math.max(0, rawElapsedSec - durationSec);

    const meetingElapsedSec = Math.max(0, Math.floor((serverNowMs - actualStartMs) / 1000));
    const boxRemainingSec = TOTAL_PLANNED_SEC - meetingElapsedSec;

    // Schedule variance: how the actual clock compares to the plan. Measured at
    // the checkpoint the current step began (stable within a step), plus any
    // live overrun so sitting in grace pushes us "over" in real time. Before the
    // first sync we don't know the real step start, so assume on-plan.
    const plannedBeforeSec = AGENDA.slice(0, stepIdx).reduce((s, a) => s + a.duration * 60, 0);
    const actualBeforeSec =
        stepStartedAt != null
            ? Math.max(0, Math.floor((stepStartedAt - actualStartMs) / 1000))
            : plannedBeforeSec;
    const varianceSec = actualBeforeSec - plannedBeforeSec + overInStep;
    const onPace = Math.abs(varianceSec) <= 30;
    const over = varianceSec > 30;

    const paceLabel = onPace
        ? 'On pace'
        : over
          ? `${fmtTime(varianceSec)} over`
          : `${fmtTime(varianceSec)} ahead`;

    return (
        <div className="bg-white border-b border-gray-200 px-6 py-3 flex-shrink-0" data-tour="eos-l10-pace">
            <div className="flex items-center justify-between gap-4 mb-2">
                <span className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">
                    Meeting pace
                </span>
                <div className="flex items-center gap-3">
                    <span
                        className={`inline-flex items-center gap-1.5 text-xs font-semibold px-2.5 py-1 rounded-full ${
                            onPace
                                ? 'bg-green-100 text-green-800'
                                : over
                                  ? 'bg-amber-100 text-amber-800'
                                  : 'bg-blue-100 text-blue-800'
                        }`}
                    >
                        <span
                            className={`w-1.5 h-1.5 rounded-full ${
                                onPace ? 'bg-green-500' : over ? 'bg-amber-500' : 'bg-blue-500'
                            }`}
                        />
                        {paceLabel}
                    </span>
                    <span
                        className={`text-2xl font-bold tabular-nums leading-none ${
                            boxRemainingSec < 0 ? 'text-red-600' : 'text-gray-900'
                        }`}
                    >
                        {boxRemainingSec < 0 ? `+${fmtTime(boxRemainingSec)}` : fmtTime(boxRemainingSec)}
                        <span className="text-xs font-normal text-gray-400 ml-1.5">
                            {boxRemainingSec < 0 ? 'over 90m' : 'left of 90m'}
                        </span>
                    </span>
                </div>
            </div>

            <div
                className="flex h-3 rounded-full overflow-hidden bg-gray-100 border border-gray-200"
                role="img"
                aria-label={`Meeting pace: ${AGENDA[stepIdx].label} in progress, ${
                    boxRemainingSec < 0
                        ? `${fmtTime(boxRemainingSec)} over the 90 minute box`
                        : `${fmtTime(boxRemainingSec)} left of 90 minutes`
                }, ${paceLabel}.`}
            >
                {AGENDA.map((seg, i) => {
                    const isDone = i < stepIdx;
                    const isCurrent = i === stepIdx;
                    const fillPct = isDone
                        ? 100
                        : isCurrent
                          ? Math.min(100, (elapsedInStep / durationSec) * 100)
                          : 0;
                    const overflowing = isCurrent && overInStep > 0;
                    return (
                        <div
                            key={seg.id}
                            style={{ flexGrow: seg.duration }}
                            className={`relative ${i > 0 ? 'border-l border-white' : ''}`}
                            title={`${seg.label} · ${seg.duration}m`}
                        >
                            <div
                                className={`absolute inset-y-0 left-0 ${
                                    overflowing ? 'bg-amber-500' : 'bg-brand'
                                }`}
                                style={{ width: `${fillPct}%` }}
                            />
                        </div>
                    );
                })}
            </div>

            <div className="flex mt-1.5">
                {AGENDA.map((seg, i) => (
                    <div
                        key={seg.id}
                        style={{ flexGrow: seg.duration }}
                        className={`text-[9px] uppercase tracking-wide truncate px-0.5 ${
                            i === stepIdx
                                ? 'text-brand font-bold'
                                : i < stepIdx
                                  ? 'text-gray-600'
                                  : 'text-gray-500'
                        }`}
                    >
                        {seg.label}
                    </div>
                ))}
            </div>
        </div>
    );
};

// ─── Page ───────────────────────────────────────────────────────────────────

export const L10Session: React.FC = () => {
    const { meetingId } = useParams<{ meetingId: string }>();
    const navigate = useNavigate();
    const { user } = useAuth();
    const tours = useTours();

    const [session, setSession] = useState<L10SessionResp | null>(null);
    const [isManager, setIsManager] = useState(false);
    const [stepIdx, setStepIdx] = useState(0);
    const [timeLeft, setTimeLeft] = useState(AGENDA[0].duration * 60);
    const [isGrace, setIsGrace] = useState(false);
    const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    // Timer ANCHOR (server-epoch ms): the moment the current step began.
    // Everyone derives the countdown from this so all clients agree — null
    // until the first /state sync establishes it.
    const [stepStartedAt, setStepStartedAt] = useState<number | null>(null);
    // server_now(ms) − client now(ms) at the last /state poll. Added to
    // Date.now() to read the server's clock locally, so a skewed laptop
    // clock can't desync the countdown. A ref (not state) so updating it
    // every poll doesn't tear down the 1s timer interval.
    const serverOffsetRef = useRef<number>(0);
    // Corrected server clock (epoch ms), refreshed by the 1s countdown tick so
    // the pace bar reads wall-clock elapsed without an impure render.
    const [serverNowMs, setServerNowMs] = useState<number>(() => Date.now());

    // The meeting rating is now an individual survey (per-person rows
    // on attendance), not one page-level number. `notes` stays as the
    // manager's meeting-level closing notes.
    const [notes, setNotes] = useState('');

    const [meeting, setMeeting] = useState<L10MeetingLite | null>(null);
    const [metrics, setMetrics] = useState<ScorecardMetric[]>([]);
    const [rocks, setRocks] = useState<Rock[]>([]);
    const [todos, setTodos] = useState<Todo[]>([]);
    const [issues, setIssues] = useState<Issue[]>([]);

    const [error, setError] = useState<string | null>(null);
    // Only the manager-handoff modal is hosted here now; New Rock/To-Do/Issue
    // creation + the rock detail dialog live inside the shared Views.
    const [activeModal, setActiveModal] = useState<'transfer' | null>(null);
    const [people, setPeople] = useState<PersonLite[]>([]);
    const [currentUserPersonId, setCurrentUserPersonId] = useState<string | null>(null);
    const [attendance, setAttendance] = useState<AttendanceRow[]>([]);

    // Instigator / rabbit-hole button.
    //
    // ``rabbitTick`` bumps every time the animation fires so React mounts
    // a fresh DOM node — without that, two presses in 3s wouldn't replay
    // the keyframes. ``rabbitCaller`` carries the email of whoever fired
    // the current signal so we can show "Casey called a rabbit hole"
    // alongside the visual. Empty string falls back to anonymous.
    //
    // Sync (broadcast) flow:
    //   1. Anyone presses → POST /signals { kind: "rabbit_hole" } → server row.
    //   2. Every client polls /signals?since=<lastSeen> every 2s.
    //   3. When a new row arrives, the local animation plays for everyone,
    //      keyed off the server's ``created_at`` so duplicates are skipped.
    const [rabbitTick, setRabbitTick] = useState(0);
    const [rabbitVisible, setRabbitVisible] = useState(false);
    const [rabbitCaller, setRabbitCaller] = useState<string | null>(null);
    // The latest signal timestamp we've already played. Bumped on every
    // poll so we never re-fire the same broadcast. Initialized to the
    // session's actual_start once the session loads so we don't replay
    // stale signals from a previous session of the same meeting.
    const lastRabbitSeenRef = useRef<string | null>(null);

    // Live-sync change markers. The poll compares the server's
    // attendance_rev / items_rev to the last value it saw and refetches only
    // the list that moved, so a peer's check-in or rock toggle surfaces
    // within ~2s without a manual refresh. ``firstStateSync`` suppresses the
    // refetch on the very first poll (init already loaded both lists).
    const lastAttendanceRevRef = useRef<string | null>(null);
    const lastItemsRevRef = useRef<string | null>(null);
    // The same marker as state, handed to panels that own their own fetch
    // (Headlines) so they can re-read when the server says something moved.
    const [itemsRev, setItemsRev] = useState<string | null>(null);
    const firstStateSyncRef = useRef<boolean>(true);
    // After a local optimistic edit (rock/todo/issue/metric), briefly hold off
    // the wholesale items refetch so a 2s poll can't clobber the edit with
    // pre-commit data while the PATCH is still in flight.
    const suppressItemsRefetchUntilRef = useRef<number>(0);
    // Set once the poll sees the session concluded (actual_end). Followers
    // then stop polling + freeze the clock and get a clear banner instead of
    // a silently frozen screen.
    const [sessionEnded, setSessionEnded] = useState(false);
    // Sync health. Every /state failure used to be a bare console.debug with
    // the interval still spinning, so an expired session JWT or a stalled
    // network looked EXACTLY like a healthy meeting whose leader hadn't moved
    // on — you only found out by refreshing, which is what fixed it. Count the
    // consecutive misses and say so on screen.
    const pollFailStreakRef = useRef(0);
    const [syncStale, setSyncStale] = useState(false);
    const [resyncing, setResyncing] = useState(false);
    // The manager's cursor as of the last successful poll. A follower whose own
    // stepIdx has drifted from it gets a one-click jump instead of a refresh.
    const [serverStepIdx, setServerStepIdx] = useState<number | null>(null);
    // Assigned by the live-poll effect so the Resync button can force a tick.
    const syncNowRef = useRef<(() => Promise<void>) | null>(null);

    // ── Init: start (or resume) session, then load aggregated items ─────────
    useEffect(() => {
        let cancelled = false;
        if (!meetingId) return;
        (async () => {
            try {
                const startRes = await client.post<L10SessionResp>(
                    '/api/eos/l10/sessions/start',
                    {
                        meeting_id: meetingId,
                        actual_start: new Date().toISOString(),
                    },
                );
                if (cancelled) return;
                setSession(startRes.data);
                setIsManager(Boolean(startRes.data.is_manager));
                // Restore the shared cursor on (re)join. The timer anchor is
                // left for the first /state poll to set (it carries the
                // server clock needed to correct for skew), so we briefly
                // show the full step duration rather than a wrong countdown.
                setStepIdx(
                    Math.min(
                        Math.max(startRes.data.current_step ?? 0, 0),
                        AGENDA.length - 1,
                    ),
                );

                // Fire-and-forget rollover summary (not blocking).
                client
                    .post(`/api/eos/l10/sessions/${startRes.data.id}/rollover`)
                    .catch(err => console.warn('Rollover failed (non-blocking):', err));

                const itemsRes = await client.get<MeetingItems>(
                    `/api/eos/l10/meetings/${meetingId}/items`,
                );
                if (cancelled) return;
                setMeeting(itemsRes.data.meeting ?? null);
                setMetrics(itemsRes.data.metrics ?? []);
                setRocks(itemsRes.data.rocks ?? []);
                setTodos(itemsRes.data.todos ?? []);
                setIssues(itemsRes.data.issues ?? []);

                // Preload people for in-meeting modals + the transfer modal.
                // Also pluck the current user's person row so we can exclude
                // them from the "Transfer management" candidate list.
                const pRes = await client.get<PersonLite[]>('/api/eos/people/');
                if (cancelled) return;
                setPeople(pRes.data);
                const me = user?.email
                    ? pRes.data.find(p => p.email.toLowerCase() === user.email.toLowerCase())
                    : null;
                setCurrentUserPersonId(me?.id ?? null);

                // Initial attendance load for the Check-in segment.
                const aRes = await client.get<AttendanceRow[]>(
                    `/api/eos/l10/sessions/${startRes.data.id}/attendance`,
                );
                if (!cancelled) setAttendance(aRes.data);
            } catch (err) {
                if (!cancelled) {
                    setError(err instanceof Error ? err.message : 'Failed to start session');
                }
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [meetingId, user?.email]);

    // ── Per-step countdown timer (derived from the shared anchor) ───────────
    // The countdown is NOT counted locally; it's computed each second from
    // the server-authoritative anchor: remaining = duration − (serverNow −
    // step_started_at). Every client computes the same value, so all screens
    // agree, and a (re)join or a missed poll self-corrects on the next tick
    // (no catch-up logic). serverNow = Date.now() + serverOffset corrects for
    // a skewed local clock. The interval only re-paints; it holds no state.
    useEffect(() => {
        if (timerRef.current) clearInterval(timerRef.current);
        if (sessionEnded) return; // meeting concluded — freeze the clock
        const durationSec = AGENDA[stepIdx].duration * 60;
        const compute = () => {
            // Refresh the shared server clock each tick (the pace bar reads it
            // for wall-clock elapsed against the 90-min box). Computed here, in
            // the interval, so render stays pure.
            const serverNow = Date.now() + serverOffsetRef.current;
            setServerNowMs(serverNow);
            if (stepStartedAt == null) {
                // Anchor not known yet (before the first /state poll lands) —
                // show the full step duration rather than a bogus countdown.
                setTimeLeft(durationSec);
                setIsGrace(false);
                return;
            }
            const elapsedSec = Math.floor((serverNow - stepStartedAt) / 1000);
            const remaining = durationSec - elapsedSec;
            setTimeLeft(remaining);
            setIsGrace(remaining < 0);
        };
        compute();
        timerRef.current = setInterval(compute, 1000);
        return () => {
            if (timerRef.current) clearInterval(timerRef.current);
        };
    }, [stepIdx, stepStartedAt, sessionEnded]);

    // Live poll (every 2s while the session is live): one tick that does the
    // shared-cursor sync AND the signal broadcast. Folded into a single
    // interval so we don't double the cadence during a meeting — each call is
    // a short stateless GET (the connection-budget-safe shape, never a held
    // stream). They run in independent try/catch so one failing doesn't block
    // the other.
    //
    // ``syncState`` follows the manager's step + timer anchor, recomputes our
    // own manager status from the authoritative manager_id, and refetches a
    // list only when its change-marker moved. ``pollSignals`` plays the
    // rabbit-hole; we seed ``lastRabbitSeen`` to the session start so a fresh
    // load doesn't replay old rabbits.
    useEffect(() => {
        if (!session?.id) return;
        const sessionId = session.id;
        // The meeting-clock anchor we're currently rendering. A Restart moves
        // it server-side; comparing against it is how a tab already in the room
        // notices (see ``restarted`` below).
        const knownActualStart = session.actual_start;
        if (lastRabbitSeenRef.current === null) {
            lastRabbitSeenRef.current = session.actual_start;
        }
        let cancelled = false;

        const syncState = async () => {
            try {
                const res = await client.get<L10SessionStateResp>(
                    `/api/eos/l10/sessions/${sessionId}/state`,
                );
                if (cancelled) return;
                const st = res.data;
                // A poll landed: we're in sync again.
                pollFailStreakRef.current = 0;
                setSyncStale(false);
                setServerStepIdx(
                    Math.min(Math.max(st.current_step, 0), AGENDA.length - 1),
                );

                // The meeting was concluded (possibly by someone else) — stop
                // polling, freeze the clock, and surface a banner instead of
                // leaving this tab on a frozen, runaway-grace screen.
                if (st.actual_end != null) {
                    cancelled = true;
                    window.clearInterval(handle);
                    setSessionEnded(true);
                    return;
                }

                // Re-baseline the clock each poll (ref-only — doesn't restart
                // the timer interval).
                serverOffsetRef.current =
                    (instantMs(st.server_now) ?? Date.now()) - Date.now();

                // Am I the facilitator? Derive from the authoritative
                // manager_id so a handed-off manager gains control within one
                // tick. Only override once we know our own person id.
                const amManager = Boolean(
                    st.manager_id &&
                        currentUserPersonId &&
                        st.manager_id === currentUserPersonId,
                );
                if (st.manager_id && currentUserPersonId) {
                    setIsManager(amManager);
                }

                // The meeting was restarted (by us from the Meetings page, or
                // by an admin): the server moved the meeting-level anchor, so
                // adopt it and let the whole room's clocks fall back to zero.
                const restarted = st.actual_start !== knownActualStart;
                if (restarted) {
                    setSession(prev =>
                        prev ? { ...prev, actual_start: st.actual_start } : prev,
                    );
                }

                // Followers adopt the server cursor every tick; the manager
                // is the writer, so adopts only on the first sync (to set the
                // anchor + clock baseline) and drives locally thereafter — a
                // restart is the exception, since it's a rewind the facilitator
                // must follow too.
                if (firstStateSyncRef.current || !amManager || restarted) {
                    setStepIdx(
                        Math.min(Math.max(st.current_step, 0), AGENDA.length - 1),
                    );
                    setStepStartedAt(instantMs(st.step_started_at));
                }

                // Refetch a list only when its marker moved (skip the first
                // sync — init already loaded both).
                if (firstStateSyncRef.current) {
                    lastAttendanceRevRef.current = st.attendance_rev;
                    lastItemsRevRef.current = st.items_rev;
                } else {
                    if (st.attendance_rev !== lastAttendanceRevRef.current) {
                        lastAttendanceRevRef.current = st.attendance_rev;
                        client
                            .get<AttendanceRow[]>(
                                `/api/eos/l10/sessions/${sessionId}/attendance`,
                            )
                            .then(r => {
                                if (!cancelled) setAttendance(r.data);
                            })
                            .catch(e => console.debug('Attendance refetch failed', e));
                    }
                    if (
                        st.items_rev !== lastItemsRevRef.current &&
                        meetingId &&
                        Date.now() >= suppressItemsRefetchUntilRef.current
                    ) {
                        // (When suppressed we deliberately leave lastItemsRev
                        // unchanged so the next poll after the window catches
                        // up to server truth — including our own edit.)
                        lastItemsRevRef.current = st.items_rev;
                        setItemsRev(st.items_rev);
                        client
                            .get<MeetingItems>(
                                `/api/eos/l10/meetings/${meetingId}/items`,
                            )
                            .then(r => {
                                if (cancelled) return;
                                setMetrics(r.data.metrics ?? []);
                                setRocks(r.data.rocks ?? []);
                                setTodos(r.data.todos ?? []);
                                setIssues(r.data.issues ?? []);
                            })
                            .catch(e => console.debug('Items refetch failed', e));
                    }
                }
                firstStateSyncRef.current = false;
            } catch (err) {
                console.debug('State poll failed', err);
                pollFailStreakRef.current += 1;
                // Three misses ≈ 6s. Short enough that the room notices before
                // it has argued past a whole agenda item, long enough that one
                // dropped packet doesn't cry wolf.
                if (pollFailStreakRef.current >= 3) setSyncStale(true);
            }
        };

        syncNowRef.current = syncState;

        const pollSignals = async () => {
            try {
                const since = lastRabbitSeenRef.current;
                const params = since
                    ? `?since=${encodeURIComponent(since)}`
                    : '';
                const res = await client.get<
                    {
                        id: string;
                        kind: string;
                        created_at: string;
                        created_by: string | null;
                    }[]
                >(`/api/eos/l10/sessions/${sessionId}/signals${params}`);
                if (cancelled || res.data.length === 0) return;
                // Bump the seen-cursor to the newest one we received so
                // we never re-fire the same signal on the next poll.
                lastRabbitSeenRef.current = res.data[res.data.length - 1].created_at;
                // Find a rabbit-hole signal NOT fired by us (we already
                // played it optimistically on click). Anything fired by
                // anyone else triggers a fresh animation.
                const myEmail = user?.email ?? '';
                const fromOthers = res.data.find(
                    s => s.kind === 'rabbit_hole' && s.created_by !== myEmail,
                );
                if (fromOthers) {
                    setRabbitCaller(fromOthers.created_by ?? null);
                    setRabbitVisible(true);
                    setRabbitTick(t => t + 1);
                    window.setTimeout(() => setRabbitVisible(false), 2900);
                }
            } catch (err) {
                // Network blip — swallow. The next tick retries with the
                // same ``since`` so we don't miss anything.
                console.debug('Rabbit poll failed', err);
            }
        };

        const tick = () => {
            void syncState();
            void pollSignals();
        };
        // Declare the interval handle before the first tick so syncState can
        // clear it on actual_end, then fire once immediately.
        const handle = window.setInterval(tick, 2000);
        tick();
        return () => {
            cancelled = true;
            syncNowRef.current = null;
            window.clearInterval(handle);
        };
    }, [session?.id, session?.actual_start, user?.email, currentUserPersonId, meetingId]);

    // Manual resync: force one /state tick now. Covers both failure shapes —
    // a poll that has been erroring silently, and a cursor that diverged
    // because the room forked across two session rows.
    const resyncNow = useCallback(async () => {
        if (!syncNowRef.current || resyncing) return;
        setResyncing(true);
        try {
            await syncNowRef.current();
        } finally {
            setResyncing(false);
        }
    }, [resyncing]);

    // ── Manager: drive the shared cursor ───────────────────────────────────
    // Optimistically move + re-anchor locally (the facilitator shouldn't wait
    // for the round-trip), then POST so every follower jumps on their next
    // poll. The server re-anchors to its own clock; followers adopt that, the
    // manager keeps the optimistic anchor (a <300ms difference).
    const goToStep = useCallback(
        async (idx: number) => {
            if (!session) return;
            const clamped = Math.min(Math.max(idx, 0), AGENDA.length - 1);
            setStepIdx(clamped);
            // Optimistic anchor so the facilitator's countdown moves instantly.
            setStepStartedAt(Date.now() + serverOffsetRef.current);
            try {
                const res = await client.post<L10SessionResp>(
                    `/api/eos/l10/sessions/${session.id}/state`,
                    { current_step: clamped },
                );
                // Adopt the server's exact anchor so the manager and followers
                // compute the identical countdown (kills the one-network-leg
                // drift between the optimistic anchor and the stored one).
                const anchor = instantMs(res.data?.step_started_at);
                if (anchor !== null) {
                    setStepStartedAt(anchor);
                }
            } catch (err) {
                console.warn('Step advance broadcast failed', err);
            }
        },
        [session],
    );

    // ── Toggle handlers (optimistic, mirroring the 04.3a pattern) ──────────

    const toggleMetricOnTrack = async (metricId: string, current: boolean) => {
        suppressItemsRefetchUntilRef.current = Date.now() + 2500; // protect optimistic edit
        setMetrics(prev =>
            prev.map(m => (m.id === metricId ? { ...m, on_track: !current } : m)),
        );
        try {
            await client.patch(`/api/eos/scorecard/metrics/${metricId}`, {
                on_track: !current,
            });
        } catch (err) {
            console.error('Metric toggle failed:', err);
            setMetrics(prev =>
                prev.map(m => (m.id === metricId ? { ...m, on_track: current } : m)),
            );
        }
    };

    // ── Check-in submit ────────────────────────────────────────────────────

    /** Upsert the caller's check-in entry on the current session. Backend
     * stores it on `eos_l10_attendance` so a Person who already has an
     * attendance row gets their check-in fields filled rather than a
     * second row created. The check-in fields are OPTIONAL — null
     * values cause the router to skip the field overwrite, marking
     * attendance without persisting any check-in content. */
    const submitCheckin = async (payload: {
        person_id: string;
        content: string | null;
        type: 'personal' | 'professional' | null;
        rating: number | null;
    }) => {
        if (!session) return;
        try {
            // Personal + Professional are stored in separate columns, so a
            // person can record both without one overwriting the other. Send
            // only the selected category's pair; the other category's fields
            // stay null, which the backend treats as "no change".
            const isPersonal = payload.type === 'personal';
            const isProfessional = payload.type === 'professional';
            await client.post(
                `/api/eos/l10/sessions/${session.id}/attendance`,
                {
                    session_id: session.id,
                    person_id: payload.person_id,
                    attended: true,
                    checkin_personal_content: isPersonal ? payload.content : null,
                    checkin_personal_rating: isPersonal ? payload.rating : null,
                    checkin_professional_content: isProfessional ? payload.content : null,
                    checkin_professional_rating: isProfessional ? payload.rating : null,
                },
            );
            const aRes = await client.get<AttendanceRow[]>(
                `/api/eos/l10/sessions/${session.id}/attendance`,
            );
            setAttendance(aRes.data);
        } catch (err) {
            console.error('Failed to submit check-in:', err);
            toast.error('Failed to record check-in.');
        }
    };

    // ── In-meeting create flows ────────────────────────────────────────────

    // Stamp items created in the meeting with the meeting's TEAM
    // (department or Exec) so they live on the team's list and resurface in
    // future L10s — not just keyed to this one meeting instance. The shared
    // Views consume this via `meetingContext` below.
    const meetingTeam = (): { department_name?: string | null; is_exec?: boolean } =>
        meeting?.is_exec_meeting
            ? { is_exec: true }
            : { department_name: meeting?.department_name ?? null };

    // Load-bearing: every meeting-mode onChange handler calls suppressPoll()
    // AND merges into local state, so the 2s items poll can't clobber the
    // shared View's optimistic edit while its PATCH/POST is still in flight.
    const SUPPRESS_MS = 2500;
    const suppressPoll = () => {
        suppressItemsRefetchUntilRef.current = Date.now() + SUPPRESS_MS;
    };

    // Passed to the shared Views so items they create are stamped with this
    // meeting's team + linked to this L10 (resurfacing in future meetings).
    const meetingContext: MeetingContext | null = meetingId
        ? { l10MeetingId: meetingId, team: meetingTeam() }
        : null;

    // Everyone the in-meeting pickers may offer or name: the caller's own EOS
    // roster (/api/eos/people, scoped to the caller's team) UNION this
    // meeting's resolved attendees. The union is load-bearing — an attendee
    // outside the caller's team (a cross-department director who leads it, an
    // invited guest) is absent from the roster, so before this the Check-in
    // and Transfer-management dropdowns silently dropped them and their
    // recorded check-in rendered nameless. Sorted so the merge doesn't append
    // those attendees below an otherwise unordered list.
    const sessionPeople = useMemo(() => {
        const byId = new Map(people.map(p => [p.id, p]));
        for (const a of meeting?.attendees ?? []) {
            if (!byId.has(a.id)) byId.set(a.id, a);
        }
        return [...byId.values()].sort((a, b) =>
            `${a.first_name} ${a.last_name}`.localeCompare(
                `${b.first_name} ${b.last_name}`,
            ),
        );
    }, [people, meeting?.attendees]);

    // Who the room is following. Falls back to the generic word rather than an
    // empty string — "Following · Rocks" would read as a rendering bug.
    const managerName = useMemo(() => {
        const m = meeting?.manager_id
            ? sessionPeople.find(pp => pp.id === meeting.manager_id)
            : null;
        return m ? `${m.first_name} ${m.last_name}` : 'the facilitator';
    }, [meeting?.manager_id, sessionPeople]);

    // ── Manager handoff ────────────────────────────────────────────────────

    /** Transfer management to another person mid-meeting. Backend PATCHes
     * `eos_l10_meetings.manager_id`; locally we drop our manager privileges
     * so the UI immediately switches to participant mode. The new manager
     * picks up the role on their next page load / refresh.
     */
    const transferManagement = async (newManagerId: string) => {
        if (!meetingId) return;
        try {
            await client.patch(`/api/eos/l10/${meetingId}`, {
                manager_id: newManagerId,
            });
            setIsManager(false);
            setActiveModal(null);
        } catch (err) {
            console.error('Transfer failed:', err);
            toast.error('Failed to transfer management.');
        }
    };

    // ── Individual meeting survey ────────────────────────────────────

    /** Submit (or update) the caller's OWN end-of-meeting rating + comment.
     * Stored per-person on eos_l10_attendance so each participant AND the
     * manager fills out their own; the session's average is derived from
     * these rows server-side. Re-fetches attendance so the live results
     * list reflects the submission immediately. */
    const submitSurvey = async (payload: {
        rating: number;
        comment: string | null;
    }): Promise<boolean> => {
        if (!session || !currentUserPersonId) return false;
        try {
            await client.post(`/api/eos/l10/sessions/${session.id}/attendance`, {
                session_id: session.id,
                person_id: currentUserPersonId,
                attended: true,
                rating: payload.rating,
                rating_comment: payload.comment,
            });
            const aRes = await client.get<AttendanceRow[]>(
                `/api/eos/l10/sessions/${session.id}/attendance`,
            );
            setAttendance(aRes.data);
            return true;
        } catch (err) {
            console.error('Failed to submit survey:', err);
            toast.error('Failed to submit your rating.');
            return false;
        }
    };

    // ── Session lifecycle ──────────────────────────────────────────────────

    const finishSession = async () => {
        if (!session) return;
        try {
            // No aggregate rating param — the backend derives average_rating
            // from the per-person survey rows. We still pass the
            // manager's closing notes.
            await client.post(`/api/eos/l10/sessions/${session.id}/end`, null, {
                params: { notes },
            });
            navigate('/eos/meetings');
        } catch (err) {
            console.error('Failed to end session:', err);
            toast.error('Failed to end session.');
        }
    };

    // ── Render ──────────────────────────────────────────────────────────────

    if (error) {
        return (
            <div className="max-w-3xl mx-auto p-8">
                <ErrorState message={error} />
                <button
                    type="button"
                    onClick={() => navigate('/eos/meetings')}
                    className="mt-4 px-3 py-2 bg-brand hover:bg-brand-hover text-white rounded-md text-sm font-medium"
                >
                    Back to meetings
                </button>
            </div>
        );
    }

    if (!session)
        return (
            <div className="max-w-3xl mx-auto p-8">
                <LoadingState label="Starting session…" />
                {/* Escape hatch: starting a session hits several APIs (105s axios
                    timeout), so give the user a way back to the list instead of
                    stranding them on the spinner if the start stalls. */}
                <div className="mt-4 flex justify-center">
                    <button
                        type="button"
                        onClick={() => navigate('/eos/meetings')}
                        className="px-3 py-2 bg-white border border-gray-300 hover:border-gray-400 text-gray-700 rounded-md text-sm font-medium"
                    >
                        Back to meetings
                    </button>
                </div>
            </div>
        );

    const step = AGENDA[stepIdx];
    const CurrentIcon = step.icon;

    return (
        <div className="flex flex-col h-[calc(100vh-0px)] bg-gray-50 relative overflow-hidden">
            {/* Rabbit-hole instigator visual.
                Pointer-events-none on the overlay itself, so it never blocks
                clicks on whatever is under it. The component drives a Lottie
                rabbit + Framer Motion trajectory + CSS hole. Lives in its own
                module so the inline-keyframes blob doesn't pollute this file
                and so the same component can be reused elsewhere (e.g. a
                team-wide "raise the rabbit" Slack hook later). */}
            <RabbitHoleAnimation
                visible={rabbitVisible}
                runKey={rabbitTick}
                caller={rabbitCaller}
            />

            {/* Concluded banner — shown to anyone still on the page when the
                facilitator ends the meeting, so a follower never sits on a
                frozen, runaway-grace screen. */}
            {sessionEnded && (
                <div className="absolute top-0 inset-x-0 z-50 bg-brand text-white px-4 py-3 flex items-center justify-center gap-4 shadow-md">
                    <span className="text-sm font-medium">
                        This meeting has been concluded by the facilitator.
                    </span>
                    <button
                        type="button"
                        onClick={() => navigate('/eos/meetings')}
                        className="text-xs px-3 py-1.5 bg-white/20 hover:bg-white/30 rounded font-semibold"
                    >
                        Back to meetings
                    </button>
                </div>
            )}

            {/* Sync health. Not a toast — a toast would fade while the screen
                stays wrong. It sits with the pace bar because that's where the
                room already looks, and it is the only signal that "the leader
                hasn't moved on" is actually "your screen stopped listening". */}
            {syncStale && !sessionEnded && (
                <div className="bg-amber-50 border-b border-amber-200 px-6 py-2 flex items-center justify-center gap-3 flex-shrink-0">
                    <AlertTriangle size={14} className="text-amber-600 flex-shrink-0" />
                    <span className="text-xs text-amber-800 font-medium">
                        Not syncing — this screen may be behind the room.
                    </span>
                    <button
                        type="button"
                        onClick={resyncNow}
                        disabled={resyncing}
                        className="text-xs px-2 py-1 bg-amber-600 hover:bg-amber-700 disabled:opacity-50 text-white rounded font-semibold"
                    >
                        {resyncing ? 'Reconnecting…' : 'Resync now'}
                    </button>
                </div>
            )}

            {/* ── Master pace bar: the whole 90-minute view, above
                everything so it's the first thing the room reads. ─────── */}
            <MeetingPaceBar
                stepIdx={stepIdx}
                actualStartMs={instantMs(session.actual_start) ?? Date.now()}
                stepStartedAt={stepStartedAt}
                serverNowMs={serverNowMs}
            />

            {/* Workspace row: sidebar + main sit beneath the pace bar. */}
            <div className="flex flex-1 min-h-0 overflow-hidden">
            {/* ── Sidebar: timer + agenda ─────────────────────────────── */}
            <aside className="w-72 bg-white border-r border-gray-200 flex flex-col flex-shrink-0">
                <div className="p-5 border-b border-gray-200 bg-brand text-white">
                    <div className={`text-4xl font-bold tabular-nums ${isGrace ? 'text-red-300' : ''}`}>
                        {isGrace ? `+${fmtTime(timeLeft)}` : fmtTime(timeLeft)}
                    </div>
                    <div className="text-xs opacity-80 mt-1 uppercase tracking-wider">
                        {step.label} time
                    </div>
                    {isGrace && isManager && (
                        <button
                            type="button"
                            onClick={() => goToStep(stepIdx)}
                            className="mt-3 w-full text-xs px-2 py-1.5 bg-white/20 hover:bg-white/30 rounded font-semibold"
                        >
                            Reset timer
                        </button>
                    )}
                </div>

                <ul className="flex-1 py-2 overflow-y-auto" data-tour="eos-l10-agenda">
                    {AGENDA.map((s, idx) => {
                        const Icon = s.icon;
                        const isActive = idx === stepIdx;
                        const isDone = idx < stepIdx;
                        // Follow-the-leader: only the manager moves the shared
                        // cursor. Everyone else's agenda is read-only and
                        // tracks the manager via the /state poll, so the team
                        // literally sees the same step at the same time.
                        const isClickable = isManager && idx !== stepIdx;
                        return (
                            <li key={s.id}>
                                <button
                                    type="button"
                                    onClick={() => isClickable && goToStep(idx)}
                                    disabled={!isClickable}
                                    className={`w-full flex items-center gap-3 px-5 py-3 text-sm transition-colors ${
                                        isActive
                                            ? 'bg-brand/10 text-brand font-semibold border-l-4 border-brand'
                                            : isDone
                                              ? 'text-gray-700 hover:bg-gray-50'
                                              : 'text-gray-400 cursor-default'
                                    }`}
                                >
                                    {isDone ? (
                                        <CheckCircle2 size={16} className="text-green-500" />
                                    ) : (
                                        <Icon size={16} />
                                    )}
                                    <span className="flex-1 text-left">{s.label}</span>
                                    <span className="text-xs opacity-60">{s.duration}m</span>
                                </button>
                            </li>
                        );
                    })}
                </ul>

                <div className="p-4 border-t border-gray-200 space-y-2">
                    {/* Configurable per-meeting link slot. Only rendered
                        when both label and url are populated — half-configured
                        links are intentionally hidden. Opens in a new tab. */}
                    {meeting?.secondary_link_label && meeting?.secondary_link_url && (
                        <a
                            href={meeting.secondary_link_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-white border border-gray-300 hover:border-brand hover:text-brand text-gray-700 rounded-md text-sm font-medium"
                            title={meeting.secondary_link_url}
                        >
                            <ExternalLink size={14} /> {meeting.secondary_link_label}
                        </a>
                    )}
                    {isManager && (
                        <button
                            type="button"
                            onClick={() => setActiveModal('transfer')}
                            className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-white border border-brand/30 hover:border-brand hover:bg-brand/5 text-brand rounded-md text-sm font-medium"
                            title="Hand off the manager role to someone else"
                        >
                            <UserCog size={14} /> Transfer management
                        </button>
                    )}
                    {/* Rabbit-hole instigator — visible to every
                        participant of the session (not just the manager). A
                        click POSTs to /signals so every other client picks
                        it up on the next 2s poll and plays the same hop-
                        into-hole animation. The local optimistic play
                        fires immediately so the clicker doesn't perceive
                        round-trip latency. */}
                    <button
                        type="button"
                        onClick={async () => {
                            if (!session) return;
                            // Optimistic local play — the clicker shouldn't
                            // wait for the server round-trip. Other clients
                            // pick it up via polling.
                            setRabbitCaller(user?.email ?? null);
                            setRabbitVisible(true);
                            setRabbitTick(t => t + 1);
                            window.setTimeout(() => setRabbitVisible(false), 2900);
                            try {
                                await client.post(
                                    `/api/eos/l10/sessions/${session.id}/signals`,
                                    { kind: 'rabbit_hole' },
                                );
                            } catch (err) {
                                // Broadcast failed — the local animation already
                                // ran, but the rest of the team won't see it.
                                // Don't block the UX with a modal; just log.
                                console.warn('Rabbit broadcast failed', err);
                            }
                        }}
                        className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-white border border-amber-300 hover:border-amber-500 hover:bg-amber-50 text-amber-700 rounded-md text-sm font-medium"
                        title="Off-topic? Send the rabbit — everyone in the meeting sees it"
                        data-tour="eos-l10-rabbit"
                    >
                        <Rabbit size={14} /> Rabbit hole
                    </button>
                    <button
                        type="button"
                        onClick={() => navigate('/eos/meetings')}
                        className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-white border border-gray-300 hover:border-gray-400 text-gray-700 rounded-md text-sm font-medium"
                        data-tour="eos-l10-exit"
                    >
                        <Flag size={14} /> Exit meeting
                    </button>
                    {/* The meeting hides the app's sidebar, and with it the
                        account menu's "Replay this page's tour", so the L10's
                        tour (which never starts by itself here) lives here. */}
                    {tours.available && tours.pageHasTour && (
                        <button
                            type="button"
                            onClick={tours.replay}
                            className="w-full flex items-center justify-center gap-1.5 px-3 py-1.5 text-gray-500 hover:text-gray-800 rounded-md text-xs font-medium"
                        >
                            <Compass size={13} aria-hidden="true" /> Tour of this page
                        </button>
                    )}
                </div>
            </aside>

            {/* ── Main workspace ──────────────────────────────────────── */}
            <main className="flex-1 overflow-y-auto p-8">
                <header className="mb-6 flex items-center gap-4" data-tour="eos-l10-header">
                    <div className="bg-brand/10 text-brand p-3 rounded-md">
                        <CurrentIcon size={28} />
                    </div>
                    <div>
                        <h1 className="text-2xl font-semibold text-gray-900">{step.label}</h1>
                        <p className="text-sm text-gray-500">
                            Phase focus: {step.duration} minutes
                        </p>
                    </div>
                </header>

                <div className={WIDE_STEPS.has(step.id) ? 'max-w-full' : 'max-w-4xl'}>
                    {step.id === 'checkin' && (
                        <CheckinPanel
                            people={sessionPeople}
                            rosterEmails={meeting?.attendee_emails ?? null}
                            attendance={attendance}
                            currentUserPersonId={currentUserPersonId}
                            onSubmit={submitCheckin}
                        />
                    )}
                    {step.id === 'scorecard' && (
                        <ScorecardPanel
                            metrics={metrics}
                            people={sessionPeople}
                            onToggle={toggleMetricOnTrack}
                            departmentName={meeting?.department_name ?? null}
                            isExecMeeting={Boolean(meeting?.is_exec_meeting)}
                        />
                    )}
                    {step.id === 'rocks' && (
                        <RocksView
                            mode="meeting"
                            rocks={rocks}
                            people={sessionPeople}
                            quarters={[]}
                            meetingContext={meetingContext ?? undefined}
                            onChange={(c) => {
                                suppressPoll();
                                setRocks(prev => applyRockChange(prev, c));
                            }}
                            onIssueCreated={(issue) => {
                                // A rock's "Add to IDS" spawns an issue — drop it into
                                // the IDS segment immediately (the Rock→IDS bubble).
                                suppressPoll();
                                setIssues(prev =>
                                    applyIssueChange(prev, { type: 'upsert', issue }),
                                );
                            }}
                        />
                    )}
                    {step.id === 'headlines' && (
                        <HeadlinesPanel
                            meetingId={meetingId ?? null}
                            refreshKey={itemsRev}
                            suppressPoll={suppressPoll}
                        />
                    )}
                    {step.id === 'todos' && (
                        <TodosView
                            mode="meeting"
                            todos={todos}
                            people={sessionPeople}
                            meetingContext={meetingContext ?? undefined}
                            onChange={(c) => {
                                suppressPoll();
                                setTodos(prev => applyTodoChange(prev, c));
                            }}
                        />
                    )}
                    {step.id === 'ids' && (
                        <IssuesView
                            mode="meeting"
                            issues={issues}
                            todos={todos}
                            people={sessionPeople}
                            meetingContext={meetingContext ?? undefined}
                            onIssueChange={(c) => {
                                suppressPoll();
                                setIssues(prev => applyIssueChange(prev, c));
                            }}
                            onTodoChange={(c) => {
                                // The Issue→To-Do link: a to-do spun off an issue lands
                                // in the shared todos state so it also shows in To-Dos.
                                suppressPoll();
                                setTodos(prev => applyTodoChange(prev, c));
                            }}
                        />
                    )}
                    {step.id === 'conclude' && (
                        <ConcludePanel
                            people={sessionPeople}
                            attendance={attendance}
                            currentUserPersonId={currentUserPersonId}
                            managerPersonId={meeting?.manager_id ?? null}
                            onSubmitSurvey={submitSurvey}
                            isManager={isManager}
                            notes={notes}
                            setNotes={setNotes}
                        />
                    )}
                </div>

                <footer className="mt-8 pt-6 border-t border-gray-200 flex items-center justify-between gap-4" data-tour="eos-l10-footer">
                    <div className="flex items-center gap-3 min-w-0">
                        <div className="text-sm text-gray-500 flex-shrink-0">
                            {/* Named zone: an L10 is one meeting read from
                                several timezones at once, so "11:01 AM" is
                                only unambiguous when it says whose 11:01. */}
                            Session started{' '}
                            {formatInstantTime(session.actual_start, { withZone: true })}
                        </div>
                        {!isManager && (
                            <div className="text-xs text-gray-500 italic min-w-0 truncate">
                                You can toggle scorecards / cycle rocks / check off to-dos / add issues as we discuss them — the manager moves the agenda forward.
                            </div>
                        )}
                    </div>
                    <div className="flex items-center gap-3 flex-shrink-0">
                        {!isManager && (
                            <span className="text-xs text-brand font-medium bg-brand/10 px-3 py-1.5 rounded-full whitespace-nowrap">
                                {/* Naming the section we believe the leader is on
                                    makes a diverged cursor visible. Silence read
                                    as "the leader is taking their time". */}
                                Following {managerName} ·{' '}
                                {AGENDA[serverStepIdx ?? stepIdx].label}
                            </span>
                        )}
                        {/* Only shows when our cursor actually disagrees with the
                            server's — the self-heal for a forked room or a poll
                            that missed the advance, so nobody has to refresh. */}
                        {!isManager &&
                            serverStepIdx !== null &&
                            serverStepIdx !== stepIdx && (
                                <button
                                    type="button"
                                    onClick={() => {
                                        setStepIdx(serverStepIdx);
                                        void resyncNow();
                                    }}
                                    className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-500 hover:bg-amber-600 text-white rounded-md text-xs font-semibold whitespace-nowrap"
                                >
                                    Jump to {AGENDA[serverStepIdx].label}
                                </button>
                            )}
                        {isManager && stepIdx < AGENDA.length - 1 && (
                            <button
                                type="button"
                                onClick={() => goToStep(stepIdx + 1)}
                                className="flex items-center gap-2 px-4 py-2 bg-brand hover:bg-brand-hover text-white rounded-md text-sm font-medium"
                            >
                                Next section <ChevronRight size={16} />
                            </button>
                        )}
                        {isManager && stepIdx === AGENDA.length - 1 && (
                            <button
                                type="button"
                                onClick={finishSession}
                                className="flex items-center gap-2 px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-md text-sm font-medium"
                            >
                                Finish meeting <CheckCircle2 size={16} />
                            </button>
                        )}
                    </div>
                </footer>
            </main>
            </div>

            {/* ── Manager-handoff modal ──────────────────────────────────
                New Rock / To-Do / Issue creation + the rock detail dialog now
                live inside the shared Views, so the only modal the L10 host
                still owns is the manager transfer. */}
            <Modal
                isOpen={activeModal === 'transfer'}
                onClose={() => setActiveModal(null)}
                title="Transfer management"
            >
                <TransferManagementForm
                    candidates={sessionPeople.filter(p => p.id !== currentUserPersonId)}
                    onSubmit={transferManagement}
                    onCancel={() => setActiveModal(null)}
                />
            </Modal>
        </div>
    );
};

// ─── Step panels ────────────────────────────────────────────────────────────

const Card: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-6">{children}</div>
);

const CheckinPanel: React.FC<{
    // `sessionPeople` — the caller's EOS roster UNION the meeting's resolved
    // attendees, so filtering by `rosterEmails` below can actually match an
    // attendee from outside the caller's team.
    people: PersonLite[];
    // The meeting's attendee pool (server-resolved): the invitee roster if the
    // meeting was imported from a calendar, else the meeting's team (department
    // or Exec members). The picker is scoped to these; null/empty falls back to
    // the full people list (only when the team has no members).
    rosterEmails: string[] | null;
    attendance: AttendanceRow[];
    currentUserPersonId: string | null;
    onSubmit: (payload: {
        person_id: string;
        // Null content/type/rating means "skip my check-in".
        content: string | null;
        type: 'personal' | 'professional' | null;
        rating: number | null;
    }) => Promise<void>;
}> = ({ people, rosterEmails, attendance, currentUserPersonId, onSubmit }) => {
    const [personId, setPersonId] = useState(currentUserPersonId ?? '');
    const [content, setContent] = useState('');
    const [checkType, setCheckType] = useState<'personal' | 'professional'>('professional');
    // Tri-state on purpose. The old default of 8 meant every submit carried a
    // rating nobody chose, so the only way to tell "I rated myself an 8" from
    // "I never touched this" was whether free text was typed — which is why
    // picking a 9 and clicking the button silently discarded it. '' = no rating.
    const [rating, setRating] = useState<number | ''>('');
    const [submitting, setSubmitting] = useState(false);

    // Re-sync the default attendee whenever the parent's resolved person changes.
    useEffect(() => {
        if (currentUserPersonId && !personId) setPersonId(currentUserPersonId);
    }, [currentUserPersonId, personId]);

    const peopleById = new Map(people.map(p => [p.id, p]));

    // Only the meeting's attendee pool appears in the picker (invitees, or the
    // team when there's no calendar roster). Fall back to the full list only
    // when the pool is empty so the picker is never empty.
    const rosterSet =
        rosterEmails && rosterEmails.length
            ? new Set(rosterEmails.map(e => e.trim().toLowerCase()))
            : null;
    const attendeeOptions = rosterSet
        ? people.filter(p => p.email && rosterSet.has(p.email.toLowerCase()))
        : people;

    // Each attendance row can hold both a Personal and a Professional check-in;
    // flatten into one display entry per recorded category so both show.
    const entries = attendance.flatMap(a => {
        const out: {
            key: string;
            personId: string;
            type: 'personal' | 'professional';
            content: string;
            rating: number | null;
        }[] = [];
        // Content OR rating — in EOS the number IS the check-in ("professional
        // nine, personal eight"); good news is the optional half. Keying this
        // off content alone hid a rating-only check-in that had saved fine.
        if (
            (a.checkin_personal_content || '').trim() ||
            a.checkin_personal_rating != null
        ) {
            out.push({
                key: `${a.id}-personal`,
                personId: a.person_id,
                type: 'personal',
                content: a.checkin_personal_content ?? '',
                rating: a.checkin_personal_rating,
            });
        }
        if (
            (a.checkin_professional_content || '').trim() ||
            a.checkin_professional_rating != null
        ) {
            out.push({
                key: `${a.id}-professional`,
                personId: a.person_id,
                type: 'professional',
                content: a.checkin_professional_content ?? '',
                rating: a.checkin_professional_rating,
            });
        }
        return out;
    });

    const handle = async (e: React.FormEvent) => {
        e.preventDefault();
        // The check-in is OPTIONAL — exec team explicitly does not record
        // segway content because attendees sometimes share personal info or a low
        // score they don't want persisted. We allow submitting an attendance row
        // with empty content/type/rating, which the backend treats as a no-op for
        // the check-in fields (router only overwrites when not null) while still
        // upserting the attendance record itself.
        if (!personId || submitting) return;
        setSubmitting(true);
        try {
            const trimmed = content.trim();
            const hasRating = rating !== '';
            // The two halves are independent: a rating with no good news saves
            // the rating, good news with no rating saves the text. `type` rides
            // along whenever either is present so the value lands in the right
            // column pair. Neither → every field is null and the upsert records
            // attendance only (the check-in opt-out).
            await onSubmit({
                person_id: personId,
                content: trimmed ? trimmed : null,
                type: trimmed || hasRating ? checkType : null,
                rating: hasRating ? Number(rating) : null,
            });
            setContent('');
            setRating('');
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <Card>
            <h3 className="text-lg font-semibold text-gray-900 mb-1">Good news &amp; check-in</h3>
            <p className="text-sm text-gray-500 mb-4">
                Optional. Each attendee can rate how their week is going (0–10) and share one line of good
                news — either on its own, or both. Leave both blank and click <strong>Skip</strong> to keep it
                off the record: some teams (e.g., Exec) don't persist check-in content. Anything you do enter
                is stored on the meeting record for context.
            </p>

            <form onSubmit={handle} className="grid grid-cols-1 md:grid-cols-12 gap-2 items-stretch mb-5">
                <select
                    value={personId}
                    onChange={e => setPersonId(e.target.value)}
                    className="md:col-span-3 border border-gray-200 rounded-md px-2 py-2 text-sm bg-white"
                    required
                >
                    <option value="" disabled>Select attendee…</option>
                    {attendeeOptions.map(p => (
                        <option key={p.id} value={p.id}>
                            {p.first_name} {p.last_name}
                        </option>
                    ))}
                </select>
                <input
                    value={content}
                    onChange={e => setContent(e.target.value)}
                    placeholder="Good news (optional)"
                    className="md:col-span-5 border border-gray-200 rounded-md px-3 py-2 text-sm"
                />
                <select
                    value={checkType}
                    onChange={e => setCheckType(e.target.value as 'personal' | 'professional')}
                    className="md:col-span-2 border border-gray-200 rounded-md px-2 py-2 text-sm bg-white"
                >
                    <option value="professional">Professional</option>
                    <option value="personal">Personal</option>
                </select>
                <select
                    value={rating}
                    onChange={e =>
                        setRating(e.target.value === '' ? '' : Number(e.target.value))
                    }
                    className="md:col-span-1 border border-gray-200 rounded-md px-2 py-2 text-sm bg-white"
                    title="Rating 0–10 (optional)"
                >
                    <option value="">—</option>
                    {Array.from({ length: 11 }, (_, i) => i).map(n => (
                        <option key={n} value={n}>{n}</option>
                    ))}
                </select>
                <button
                    type="submit"
                    disabled={!personId || submitting}
                    className="md:col-span-1 bg-brand hover:bg-brand-hover disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium px-2 py-2 rounded-md"
                    title={
                        content.trim() || rating !== ''
                            ? 'Save check-in'
                            : 'Mark as attended without recording a check-in'
                    }
                >
                    {content.trim() || rating !== '' ? 'Save' : 'Skip'}
                </button>
            </form>

            <div className="overflow-x-auto border border-gray-200 rounded-md">
                <table className="w-full text-sm">
                    <thead className="bg-gray-50 text-gray-500 text-xs">
                        <tr>
                            <th className="text-left px-3 py-2 font-semibold">Attendee</th>
                            <th className="text-left px-3 py-2 font-semibold">Good news</th>
                            <th className="text-left px-3 py-2 font-semibold w-32">Type</th>
                            <th className="text-center px-3 py-2 font-semibold w-16">Rating</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                        {entries.map(entry => {
                            const p = peopleById.get(entry.personId);
                            return (
                                <tr key={entry.key} className="hover:bg-gray-50/50">
                                    <td className="px-3 py-2">
                                        <div className="flex items-center gap-2 text-gray-700">
                                            <Users size={12} className="text-brand" />
                                            <span>
                                                {p ? `${p.first_name} ${p.last_name}` : '—'}
                                            </span>
                                        </div>
                                    </td>
                                    <td className="px-3 py-2 text-gray-900">{entry.content}</td>
                                    <td className="px-3 py-2">
                                        <span className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full bg-gray-100 text-gray-700">
                                            {entry.type}
                                        </span>
                                    </td>
                                    <td className="px-3 py-2 text-center font-mono">
                                        {entry.rating ?? '—'}
                                    </td>
                                </tr>
                            );
                        })}
                        {entries.length === 0 && (
                            <tr>
                                <td colSpan={4} className="px-3 py-3 text-xs text-gray-400 italic text-center">
                                    No check-ins yet — go around the room.
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
        </Card>
    );
};

// Rolling scorecard grid, as the team asked for it — a grid that
// adds a column for every week and keeps rolling. The in-meeting view now
// mirrors the /eos/scorecard page so the team can see variance over time
// during the L10 instead of bouncing between tabs.
interface ScorecardGridMetric {
    id: string;
    name: string;
    description: string | null;
    // Needed by the shared grid to cluster rows under their owner, the way the
    // page (and the spreadsheet before it) reads.
    owner_id: string | null;
    // The team's drag order within that owner's block (null = never reordered,
    // alphabetical), so the meeting reads the measurables in the page's order.
    sort_order: number | null;
    // The authored goal ("> 98%") the Scorecard page's Goal column shows, and
    // the machine goal it falls back to. Both are needed here so the meeting
    // reads the SAME goal as the page (see `formatScorecardGoal`).
    goal_description: string | null;
    target_value: string | null;
    comparator: string | null;
    unit: string | null;
    value_type: ScorecardValueType | null;
    on_track: boolean;
    entries: Record<string, string | null>;
    // Sparse — only the cells that carry an annotation.
    notes?: Record<string, string | null>;
    average_4w: number | null;
    average_13w: number | null;
}

interface ScorecardGridResp {
    weeks: string[];
    metrics: ScorecardGridMetric[];
}

// Deliberately NOT `formatInstant`: a scorecard week is a calendar date, not
// an instant. `"2026-08-30T00:00:00"` means *the week of Aug 30*, so reading
// it as UTC would render 8/29 for every user west of Greenwich and put the
// column header a week-day out of step with the entries under it.
const _formatWeekHeader = (iso: string): string => {
    try {
        return new Date(iso).toLocaleDateString(undefined, {
            month: 'numeric',
            day: 'numeric',
        });
    } catch {
        return iso;
    }
};

// Which scorecard layout the in-meeting panel shows. "grid" is the page's
// table — the exec team asked for it by name after their first L10 ("we like
// the left-nav view better"); "cards" is the original per-metric stack, which
// frames the week you're logging rather than the quarter you're reviewing.
// Remembered per user because it's a reading preference, not a team setting.
const SCORECARD_VIEW_KEY = 'eos.l10.scorecardView';
type ScorecardView = 'grid' | 'cards';

const ScorecardPanel: React.FC<{
    metrics: ScorecardMetric[];
    people: PersonLite[];
    onToggle: (id: string, current: boolean) => void;
    departmentName: string | null;
    isExecMeeting: boolean;
}> = ({ metrics, people, onToggle, departmentName, isExecMeeting }) => {
    const [view, setView] = useState<ScorecardView>(() =>
        localStorage.getItem(SCORECARD_VIEW_KEY) === 'cards' ? 'cards' : 'grid',
    );
    const chooseView = (next: ScorecardView) => {
        setView(next);
        localStorage.setItem(SCORECARD_VIEW_KEY, next);
    };
    const peopleById = useMemo(
        () => new Map(people.map(pp => [pp.id, pp])),
        [people],
    );
    const [grid, setGrid] = useState<ScorecardGridResp | null>(null);
    const [historyLoading, setHistoryLoading] = useState(true);
    const [historyError, setHistoryError] = useState<string | null>(null);
    const [savingId, setSavingId] = useState<string | null>(null);

    // The editable cell is THIS week (current Sunday), aligned with
    // the standalone Scorecard page so an in-meeting entry hits the same row.
    const weekStart = useMemo(() => _currentWeekStart(), []);
    const weekIso = useMemo(() => weekStart.toISOString().slice(0, 10), [weekStart]);

    const loadGrid = useCallback(async (): Promise<ScorecardGridResp> => {
        // Scope the grid to the meeting's TEAM. Exec/Leadership L10 →
        // is_exec metrics; department L10 → that department's metrics. (Was
        // unscoped for exec, which leaked every department's metrics in.)
        // 13 weeks — a quarter, the page's default and the window the exec
        // team actually reads. (Was 8, which also meant the two surfaces
        // disagreed about how much history "the scorecard" shows.)
        const params = new URLSearchParams({ weeks: '13' });
        if (isExecMeeting) {
            params.set('is_exec', 'true');
        } else if (departmentName) {
            params.set('department_name', departmentName);
        }
        const res = await client.get<ScorecardGridResp>(
            `/api/eos/scorecard/grid?${params.toString()}`,
        );
        return res.data;
    }, [departmentName, isExecMeeting]);

    useEffect(() => {
        let cancelled = false;
        setHistoryLoading(true);
        (async () => {
            try {
                const data = await loadGrid();
                if (!cancelled) setGrid(data);
            } catch (err) {
                if (!cancelled) {
                    setHistoryError(
                        err instanceof Error ? err.message : 'Failed to load history',
                    );
                }
            } finally {
                if (!cancelled) setHistoryLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [loadGrid]);

    // Render from the GRID — the authoritative, correctly-scoped source.
    // Previously this panel mapped over the lightweight ``metrics`` prop,
    // which is department-filtered and therefore EMPTY on exec meetings,
    // making the scorecard look "not wired" even though grid data existed.
    // Fall back to ``metrics`` only while the grid is still loading so the
    // section isn't blank on first paint.
    const rows: ScorecardGridMetric[] = grid
        ? grid.metrics
        : metrics.map(m => ({
              id: m.id,
              name: m.name,
              description: null,
              owner_id: null,
              sort_order: null,
              goal_description: m.goal_description ?? null,
              target_value: m.target_value,
              comparator: m.comparator ?? null,
              unit: m.unit ?? null,
              value_type: m.value_type,
              on_track: m.on_track,
              entries: {},
              average_4w: null,
              average_13w: null,
          }));
    const weekAxis = grid?.weeks ?? [];

    const toggleOnTrack = (gm: ScorecardGridMetric) => {
        // Optimistic flip on the grid we render from + delegate the PATCH
        // (and parent ``metrics`` sync) to the existing handler.
        setGrid(prev =>
            prev
                ? {
                      ...prev,
                      metrics: prev.metrics.map(x =>
                          x.id === gm.id ? { ...x, on_track: !x.on_track } : x,
                      ),
                  }
                : prev,
        );
        onToggle(gm.id, gm.on_track);
    };

    // `canonical` is the STORED form — percent as a fraction (0.5), currency and
    // number plain. Callers in display units convert first
    // (`fromEditableScorecardValue`); the shared grid cell already has.
    const saveWeek = async (metricId: string, canonical: string) => {
        const gm = grid?.metrics.find(x => x.id === metricId);
        const existing = gm?.entries?.[weekIso] ?? '';
        // No-op when the value is unchanged (incl. empty → still empty).
        if (canonical === (existing ?? '')) return;
        setSavingId(metricId);
        try {
            await client.post('/api/eos/scorecard/entries/upsert', {
                metric_id: metricId,
                week_start_date: weekStart.toISOString(),
                value: canonical === '' ? null : canonical,
            });
            // Refetch so rolling averages + the week axis stay correct.
            const data = await loadGrid();
            setGrid(data);
        } catch (err) {
            console.error('Failed to save scorecard value:', err);
            toast.error(scorecardSaveError(err, "Failed to save this week's value."));
        } finally {
            setSavingId(null);
        }
    };

    // Annotate this week's cell in-meeting. `upsert` matches on the calendar
    // day and only overwrites the note when it's actually sent, so this can't
    // clobber the value and `saveWeek` can't clobber the note.
    const saveWeekNote = async (metricId: string, note: string) => {
        const gm = grid?.metrics.find(x => x.id === metricId);
        const existing = gm?.notes?.[weekIso] ?? '';
        if (note.trim() === (existing ?? '').trim()) return;
        setSavingId(metricId);
        try {
            await client.post('/api/eos/scorecard/entries/upsert', {
                metric_id: metricId,
                week_start_date: weekStart.toISOString(),
                value: gm?.entries?.[weekIso] ?? null,
                note: note.trim() === '' ? null : note.trim(),
            });
            setGrid(await loadGrid());
        } catch (err) {
            console.error('Failed to save scorecard note:', err);
            toast.error(scorecardSaveError(err, 'Failed to save the cell note.'));
        } finally {
            setSavingId(null);
        }
    };

    return (
        <Card>
            <div className="flex items-start justify-between mb-1">
                <h3 className="text-lg font-semibold text-gray-900">Scorecard review</h3>
                <div className="flex items-center gap-3">
                    {grid && grid.weeks.length > 0 && (
                        <span className="text-[11px] text-gray-500">
                            Trailing {grid.weeks.length} weeks · newest first
                        </span>
                    )}
                    {/* Grid is the default: the exec team reads the scorecard
                        backwards over a quarter and asked for the page's table
                        by name. Cards stay available for the teams that prefer
                        the "log this week" framing. */}
                    <div className="flex rounded-md border border-gray-200 overflow-hidden">
                        {(['grid', 'cards'] as ScorecardView[]).map(v => (
                            <button
                                key={v}
                                type="button"
                                onClick={() => chooseView(v)}
                                className={`px-2 py-1 text-[11px] font-medium capitalize ${
                                    view === v
                                        ? 'bg-brand text-white'
                                        : 'bg-white text-gray-600 hover:bg-gray-50'
                                }`}
                            >
                                {v}
                            </button>
                        ))}
                    </div>
                </div>
            </div>
            <p className="text-xs text-gray-500 mb-4">
                Enter this week's number, flip on/off track, and read the rolling
                history — all without leaving the meeting. Past weeks are read-only
                here; correct them on the Scorecard page.
            </p>

            {rows.length === 0 ? (
                <p className="text-sm text-gray-500 italic">
                    {historyLoading
                        ? 'Loading scorecard…'
                        : 'No scorecard metrics configured yet.'}
                </p>
            ) : view === 'grid' ? (
                <div className="space-y-3">
                    <ScorecardGrid
                        metrics={rows}
                        // Noon UTC, not local midnight: the grid keys a column
                        // by `weekKey` (toISOString().slice(0,10)), and a
                        // local-midnight Date in a positive UTC offset stringifies
                        // to the PREVIOUS day — every column would then miss its
                        // own data. Noon round-trips for every real offset.
                        weeks={weekAxis.map(iso => new Date(`${iso}T12:00:00Z`))}
                        peopleById={peopleById}
                        valueAt={(metricId, week) =>
                            rows.find(r => r.id === metricId)?.entries?.[week] ?? ''
                        }
                        noteAt={(metricId, week) =>
                            rows.find(r => r.id === metricId)?.notes?.[week] ?? null
                        }
                        // The server already computed these over the full
                        // window, so don't re-derive them from the visible axis.
                        averagesFor={metricId => {
                            const r = rows.find(x => x.id === metricId);
                            return {
                                avg4: r?.average_4w ?? null,
                                avg13: r?.average_13w ?? null,
                            };
                        }}
                        // The one editable column. An L10 logs THIS week; fixing
                        // history is a Scorecard-page job with an audit trail.
                        canEditWeek={week => week === weekIso}
                        // `saveWeek`/`saveWeekNote` always write the CURRENT
                        // week, which is sound only because `canEditWeek` below
                        // permits exactly that column. Widen one and you must
                        // widen the other.
                        onSaveCell={(metricId, _weekDate, value) =>
                            saveWeek(metricId, value)
                        }
                        onSaveNote={(metricId, _weekDate, note) =>
                            saveWeekNote(metricId, note)
                        }
                        maxHeightClass="max-h-[55vh]"
                    />
                    {/* On/off-track is a judgement the team makes out loud, so it
                        stays a first-class control in both views rather than
                        hiding inside a cell. */}
                    <div className="flex flex-wrap gap-2">
                        {rows.map(gm => (
                            <button
                                key={gm.id}
                                type="button"
                                onClick={() => toggleOnTrack(gm)}
                                disabled={savingId === gm.id}
                                title="Toggle on / off track"
                                className={`text-[11px] px-2 py-1 rounded-full border font-medium disabled:opacity-50 ${
                                    gm.on_track
                                        ? 'bg-green-50 border-green-200 text-green-700'
                                        : 'bg-red-50 border-red-200 text-red-700'
                                }`}
                            >
                                {gm.on_track ? '✓' : '✕'} {gm.name}
                            </button>
                        ))}
                    </div>
                </div>
            ) : (
                <div className="space-y-3">
                    {rows.map(gm => {
                        const goal = formatScorecardGoal(gm);
                        const thisWeek = gm.entries?.[weekIso] ?? '';
                        const isPercent = gm.value_type === 'percent';
                        return (
                            <div
                                key={gm.id}
                                className="bg-gray-50 border border-gray-200 rounded-md overflow-hidden"
                            >
                                {/* Top row — name, target, current pill */}
                                <div className="flex items-center justify-between gap-3 p-3">
                                    <div className="min-w-0">
                                        {/* Same tooltip as the Scorecard page —
                                            a native `title` here was effectively
                                            invisible. */}
                                        <Tooltip
                                            content={gm.description}
                                            className="max-w-full gap-1 cursor-help"
                                        >
                                            <span className="min-w-0 truncate font-medium text-gray-900">
                                                {gm.name}
                                            </span>
                                            {gm.description ? (
                                                <Info
                                                    size={13}
                                                    className="shrink-0 text-gray-400"
                                                    aria-hidden="true"
                                                />
                                            ) : null}
                                        </Tooltip>
                                        {(goal || gm.average_4w != null) && (
                                            <p className="text-[11px] text-gray-500 mt-0.5">
                                                {/* The goal as the Scorecard
                                                    page states it — authored
                                                    text when there is any, else
                                                    comparator + target. The
                                                    averages used to be hidden
                                                    along with it on a
                                                    goal-less measurable. */}
                                                {goal && `Target: ${goal}`}
                                                {gm.average_4w != null && (
                                                    <>
                                                        {goal ? ' · ' : ''}
                                                        4w avg{' '}
                                                        <span className="font-mono">
                                                            {formatScorecardValue(
                                                                gm.average_4w,
                                                                gm.value_type,
                                                            )}
                                                        </span>
                                                    </>
                                                )}
                                                {gm.average_13w != null && (
                                                    <>
                                                        {' · '}
                                                        13w avg{' '}
                                                        <span className="font-mono">
                                                            {formatScorecardValue(
                                                                gm.average_13w,
                                                                gm.value_type,
                                                            )}
                                                        </span>
                                                    </>
                                                )}
                                            </p>
                                        )}
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => toggleOnTrack(gm)}
                                        className={`shrink-0 text-xs font-bold uppercase px-3 py-1 rounded-full ${
                                            gm.on_track
                                                ? 'bg-green-100 text-green-800'
                                                : 'bg-red-100 text-red-800'
                                        }`}
                                        title="Click to flip on/off track"
                                    >
                                        {gm.on_track ? 'On Track' : 'Off Track'}
                                    </button>
                                </div>

                                {/* This week's value — editable inline. */}
                                <div className="border-t border-gray-200 bg-white px-3 py-2 flex items-center gap-2">
                                    <label className="text-[11px] uppercase tracking-wider font-semibold text-gray-500">
                                        This week
                                    </label>
                                    <span className="relative inline-block w-24">
                                        <input
                                            // key resets the uncontrolled input when the
                                            // stored value changes (after a refetch).
                                            key={`${gm.id}-${thisWeek}`}
                                            type="text"
                                            // Typed in display units — "50" on a
                                            // percent measurable means 50%.
                                            defaultValue={toEditableScorecardValue(
                                                thisWeek,
                                                gm.value_type,
                                            )}
                                            onBlur={e =>
                                                saveWeek(
                                                    gm.id,
                                                    fromEditableScorecardValue(
                                                        e.target.value,
                                                        gm.value_type,
                                                    ),
                                                )
                                            }
                                            placeholder="—"
                                            className={`w-full text-xs font-mono tabular-nums rounded border border-gray-300 py-1 focus:border-brand focus:ring-1 focus:ring-brand outline-none ${
                                                isPercent ? 'pl-2 pr-5' : 'px-2'
                                            }`}
                                        />
                                        {isPercent && (
                                            <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[11px] text-gray-400">
                                                %
                                            </span>
                                        )}
                                    </span>
                                    {savingId === gm.id && (
                                        <span className="text-[11px] text-gray-400 italic">
                                            saving…
                                        </span>
                                    )}
                                </div>

                                {/* Mini rolling grid — read-only history. Latest
                                    week on the left to match the standalone page. */}
                                {weekAxis.length > 0 && (
                                    <div className="border-t border-gray-200 bg-white px-3 py-2 overflow-x-auto">
                                        <table className="w-full text-xs">
                                            <thead>
                                                <tr className="text-[10px] uppercase tracking-wider text-gray-400">
                                                    {weekAxis.map(w => (
                                                        <th
                                                            key={w}
                                                            className="text-center font-mono font-normal py-1 px-1.5 min-w-[42px]"
                                                            title={w}
                                                        >
                                                            {_formatWeekHeader(w)}
                                                        </th>
                                                    ))}
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {/* Cells colour through the same
                                                    `goalMet` test the Scorecard
                                                    page uses. The hardcoded
                                                    `>=` this replaced painted
                                                    every lower-is-better
                                                    measurable ("late reports
                                                    < 2%") backwards — a missed
                                                    week read green in the
                                                    meeting and red on the
                                                    page. */}
                                                <tr>
                                                    {weekAxis.map(w => {
                                                        const raw = gm.entries?.[w] ?? null;
                                                        const met = goalMet(
                                                            raw,
                                                            gm.comparator,
                                                            gm.target_value,
                                                        );
                                                        let cellCls =
                                                            'text-gray-700 border border-gray-100';
                                                        if (raw == null) {
                                                            cellCls =
                                                                'text-gray-300 border border-dashed border-gray-200';
                                                        } else if (met !== null) {
                                                            cellCls = met
                                                                ? 'text-green-700 bg-green-50 border border-green-100'
                                                                : 'text-amber-700 bg-amber-50 border border-amber-100';
                                                        }
                                                        return (
                                                            <td
                                                                key={w}
                                                                className={`text-center font-mono tabular-nums py-1 px-1.5 ${cellCls}`}
                                                            >
                                                                {formatScorecardValue(
                                                                    raw,
                                                                    gm.value_type,
                                                                ) || '—'}
                                                            </td>
                                                        );
                                                    })}
                                                </tr>
                                            </tbody>
                                        </table>
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}

            {historyError && (
                <p className="text-[11px] text-red-500 italic mt-3">
                    Rolling history unavailable: {historyError}
                </p>
            )}
        </Card>
    );
};

const _formatUpdateDate = (iso: string | null): string =>
    formatInstant(iso, { month: 'short', day: 'numeric' });

// ── Headlines + Ideas panel ──────────────────────────────────────────

type L10InputKind = 'headline' | 'idea';

interface L10Input {
    id: string;
    meeting_id: string;
    kind: L10InputKind;
    text: string;
    discussed: boolean;
    created_at: string;
    created_by: string | null;
}

/** Pre-meeting + in-meeting input for headlines and ideas.
 *
 * Anyone on the team can drop items at any point — the same endpoint
 * is shared with the Meetings page "pre-meeting prep" entry point.
 * Manager flips ``discussed`` as the team covers each item.
 */
const HeadlinesPanel: React.FC<{
    meetingId: string | null;
    // Bumps whenever the session poll sees the team's items change. Headlines
    // hang off the meeting and used to be fetched exactly once on mount, so a
    // teammate's headline never appeared mid-session — this is what tells the
    // panel to look again.
    refreshKey: string | null;
    // Hold off the parent's items refetch briefly after a local write, so a 2s
    // poll can't land mid-flight and clobber the optimistic add.
    suppressPoll: () => void;
}> = ({ meetingId, refreshKey, suppressPoll }) => {
    const [items, setItems] = useState<L10Input[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [activeKind, setActiveKind] = useState<L10InputKind>('headline');
    const [draft, setDraft] = useState('');
    const [submitting, setSubmitting] = useState(false);

    // Trailing 30-day lookback so the team can see what was raised
    // in prior sessions without bouncing to the diary. Lazy — only fires
    // when the user expands the "Recent (30 days)" section.
    const [historyOpen, setHistoryOpen] = useState(false);
    const [history, setHistory] = useState<L10Input[]>([]);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [historyError, setHistoryError] = useState<string | null>(null);
    const [historyLoaded, setHistoryLoaded] = useState(false);

    // Covered items leave the live list and collapse in here. Collapsed by
    // default — the point of checking one off is to stop looking at it — but
    // never deleted: the meeting's record of what it covered stays one click
    // away, and unchecking pulls an item back into the live list.
    const [coveredOpen, setCoveredOpen] = useState(false);

    useEffect(() => {
        if (!meetingId) return;
        let cancelled = false;
        (async () => {
            try {
                const res = await client.get<L10Input[]>(
                    `/api/eos/l10/meetings/${meetingId}/inputs`,
                );
                if (!cancelled) setItems(res.data);
            } catch (err) {
                if (!cancelled) {
                    setError(err instanceof Error ? err.message : 'Failed to load inputs');
                }
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [meetingId, refreshKey]);

    // Fetch the 30-day history exactly once, the first time the section
    // is expanded. Re-collapsing keeps the data cached so re-opening is
    // instantaneous.
    const loadHistory = async () => {
        if (!meetingId || historyLoaded || historyLoading) return;
        setHistoryLoading(true);
        setHistoryError(null);
        try {
            const res = await client.get<L10Input[]>(
                `/api/eos/l10/meetings/${meetingId}/inputs/history?days=30`,
            );
            setHistory(res.data);
            setHistoryLoaded(true);
        } catch (err) {
            setHistoryError(
                err instanceof Error ? err.message : 'Failed to load history',
            );
        } finally {
            setHistoryLoading(false);
        }
    };

    const toggleHistory = () => {
        const next = !historyOpen;
        setHistoryOpen(next);
        if (next) loadHistory();
    };

    const submit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!meetingId) return;
        const text = draft.trim();
        if (!text || submitting) return;
        setSubmitting(true);
        suppressPoll();
        try {
            const res = await client.post<L10Input>(
                `/api/eos/l10/meetings/${meetingId}/inputs`,
                { meeting_id: meetingId, kind: activeKind, text, discussed: false },
            );
            setItems(prev => [...prev, res.data]);
            setDraft('');
        } catch (err) {
            console.error('Failed to add input', err);
            toast.error('Failed to add — please retry.');
        } finally {
            setSubmitting(false);
        }
    };

    const toggleDiscussed = async (row: L10Input) => {
        try {
            suppressPoll();
            const res = await client.patch<L10Input>(
                `/api/eos/l10/inputs/${row.id}`,
                { discussed: !row.discussed },
            );
            setItems(prev => prev.map(i => (i.id === row.id ? res.data : i)));
        } catch (err) {
            console.error('Failed to toggle discussed', err);
        }
    };

    const remove = async (row: L10Input) => {
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
            suppressPoll();
            await client.delete(`/api/eos/l10/inputs/${row.id}`);
            setItems(prev => prev.filter(i => i.id !== row.id));
        } catch (err) {
            console.error('Failed to delete input', err);
        }
    };

    const headlines = items.filter(i => i.kind === 'headline');
    const ideas = items.filter(i => i.kind === 'idea');
    const active = activeKind === 'headline' ? headlines : ideas;
    // Newest first, and checked-off items out of the way (UAT feedback).
    const { pending, covered } = partitionL10Inputs(active);

    return (
        <Card>
            <h3 className="text-lg font-semibold text-gray-900 mb-1">
                Headlines &amp; parking lot
            </h3>
            {/* "Ideas" was read as a synonym for IDS, so items meant for the IDS
                list were filed here and never discussed. This tab is the parking
                lot; IDS is the agenda step named IDS. */}
            <p className="text-sm text-gray-500 mb-4">
                Anyone can add these before the L10 starts. <strong>Headlines</strong> are
                customer / employee / company news. The <strong>parking lot</strong> holds ideas
                that aren't ready to act on — convert one to a To-Do or an Issue when it is. Not
                the same as <strong>IDS</strong>: issues to solve this meeting go in the IDS step.
                Newest first — check one off as the team covers it and it moves into{' '}
                <strong>Covered this meeting</strong> below, out of the way but not gone.
            </p>

            {/* Tabs */}
            <div className="flex gap-1 mb-3 border-b border-gray-200">
                {(['headline', 'idea'] as L10InputKind[]).map(k => (
                    <button
                        key={k}
                        type="button"
                        onClick={() => setActiveKind(k)}
                        className={`px-3 py-2 text-sm font-medium -mb-px border-b-2 transition-colors ${
                            activeKind === k
                                ? 'border-brand text-brand'
                                : 'border-transparent text-gray-500 hover:text-gray-700'
                        }`}
                    >
                        {/* The count is what's still to cover, not the
                            all-time total — a tab that reads "Headlines (4)"
                            after the team covered all four is a to-do list
                            that never empties. */}
                        {k === 'headline'
                            ? `Headlines (${headlines.filter(i => !i.discussed).length})`
                            : `Parking lot (${ideas.filter(i => !i.discussed).length})`}
                    </button>
                ))}
            </div>

            {/* Compose form */}
            <form onSubmit={submit} className="flex gap-2 mb-4">
                <input
                    value={draft}
                    onChange={e => setDraft(e.target.value)}
                    placeholder={
                        activeKind === 'headline'
                            ? 'e.g. "Acme renewed for 3 years — Casey closed it Friday"'
                            : 'e.g. "Could we ship a Slack notification for off-track rocks?"'
                    }
                    className="flex-1 border border-gray-200 rounded-md px-3 py-2 text-sm"
                />
                <button
                    type="submit"
                    disabled={!draft.trim() || submitting || !meetingId}
                    className="px-3 py-2 bg-brand hover:bg-brand-hover disabled:opacity-50 text-white text-sm font-medium rounded-md"
                >
                    {submitting
                        ? 'Adding…'
                        : activeKind === 'headline'
                          ? 'Add headline'
                          : 'Park it'}
                </button>
            </form>

            {/* List — newest first, and only what's still to cover. */}
            {loading && <p className="text-xs text-gray-500 italic">Loading…</p>}
            {error && <p className="text-xs text-red-600">{error}</p>}
            {!loading && !error && pending.length === 0 && (
                <p className="text-xs text-gray-500 italic">
                    {covered.length > 0
                        ? `All ${covered.length} covered — nothing left in this list.`
                        : activeKind === 'headline'
                          ? 'No headlines yet — share short wins, complaints, or signals.'
                          : 'Parking lot is empty — drop ideas here when they aren\'t ready to be a To-Do or an Issue.'}
                </p>
            )}
            {!loading && pending.length > 0 && (
                <ul className="space-y-1.5">
                    {pending.map(row => (
                        <li
                            key={row.id}
                            className="flex items-start gap-3 p-3 rounded-md border bg-white border-gray-200"
                        >
                            <input
                                type="checkbox"
                                checked={row.discussed}
                                onChange={() => toggleDiscussed(row)}
                                className="mt-0.5 h-4 w-4 accent-brand"
                                title="Mark covered — moves it out of this list"
                            />
                            <div className="flex-1 min-w-0">
                                <p className="text-sm">{row.text}</p>
                                {row.created_by && (
                                    <p className="text-[10px] text-gray-400 mt-0.5">
                                        by {row.created_by}
                                    </p>
                                )}
                            </div>
                            <button
                                type="button"
                                onClick={() => remove(row)}
                                className="text-xs text-gray-400 hover:text-red-600"
                                title="Delete"
                            >
                                <Trash2 size={12} />
                            </button>
                        </li>
                    ))}
                </ul>
            )}

            {/* Checked off — folded away, not deleted. Unchecking here puts an
                item straight back at the top of the live list above. */}
            {!loading && covered.length > 0 && (
                <div className="mt-3">
                    <button
                        type="button"
                        onClick={() => setCoveredOpen(o => !o)}
                        aria-expanded={coveredOpen}
                        className="flex items-center gap-1.5 text-xs font-medium text-gray-500 hover:text-brand"
                    >
                        {coveredOpen ? (
                            <ChevronDown size={14} />
                        ) : (
                            <ChevronRight size={14} />
                        )}
                        Covered this meeting ({covered.length})
                    </button>
                    {coveredOpen && (
                        <ul className="mt-2 space-y-1.5">
                            {covered.map(row => (
                                <li
                                    key={row.id}
                                    className="flex items-start gap-3 p-2.5 rounded-md border bg-gray-50 border-gray-200 text-gray-500"
                                >
                                    <input
                                        type="checkbox"
                                        checked={row.discussed}
                                        onChange={() => toggleDiscussed(row)}
                                        className="mt-0.5 h-4 w-4 accent-brand"
                                        title="Not covered after all — put it back on the list"
                                    />
                                    <div className="flex-1 min-w-0">
                                        <p className="text-sm line-through">{row.text}</p>
                                        {row.created_by && (
                                            <p className="text-[10px] text-gray-400 mt-0.5">
                                                by {row.created_by}
                                            </p>
                                        )}
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => remove(row)}
                                        className="text-xs text-gray-400 hover:text-red-600"
                                        title="Delete"
                                    >
                                        <Trash2 size={12} />
                                    </button>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            )}

            {/* Trailing 30-day lookback. Read-only — teams use this
                to recall prior context ("three weeks ago we said X..."),
                not to re-litigate items. The active list above stays the
                source of truth for this session's discussion. */}
            <div className="mt-4 pt-3 border-t border-gray-100">
                <button
                    type="button"
                    onClick={toggleHistory}
                    className="w-full flex items-center justify-between text-left text-xs font-medium text-gray-600 hover:text-brand"
                >
                    <span className="flex items-center gap-1.5">
                        {historyOpen ? (
                            <ChevronDown size={14} />
                        ) : (
                            <ChevronRight size={14} />
                        )}
                        Recent {activeKind === 'headline' ? 'headlines' : 'ideas'} — last 30 days
                        {historyLoaded && (
                            <span className="text-gray-400 font-normal">
                                ({history.filter(h => h.kind === activeKind).length})
                            </span>
                        )}
                    </span>
                    {historyOpen && (
                        <span className="text-[10px] text-gray-400 italic">
                            read-only
                        </span>
                    )}
                </button>
                {historyOpen && (
                    <div className="mt-2">
                        {historyLoading && (
                            <p className="text-[11px] text-gray-400 italic">Loading…</p>
                        )}
                        {historyError && (
                            <p className="text-[11px] text-red-500">
                                {historyError}
                            </p>
                        )}
                        {historyLoaded && (() => {
                            const past = history.filter(h => h.kind === activeKind);
                            // Trim out items already shown in the active list
                            // so the "Recent" view is genuinely *additional*
                            // context, not a duplicate of the live queue.
                            const activeIds = new Set(active.map(a => a.id));
                            const trimmed = past.filter(p => !activeIds.has(p.id));
                            if (trimmed.length === 0) {
                                return (
                                    <p className="text-[11px] text-gray-400 italic">
                                        No prior {activeKind}s in the last 30 days.
                                    </p>
                                );
                            }
                            return (
                                <ul className="space-y-1">
                                    {trimmed.map(row => (
                                        <li
                                            key={row.id}
                                            className="text-xs text-gray-600 px-2.5 py-1.5 bg-gray-50 border border-gray-100 rounded-md"
                                        >
                                            <p className={row.discussed ? '' : 'font-medium'}>
                                                {row.text}
                                            </p>
                                            <p className="text-[10px] text-gray-400 mt-0.5">
                                                {row.created_by
                                                    ? `${row.created_by} · `
                                                    : ''}
                                                {_formatUpdateDate(row.created_at)}
                                                {!row.discussed && ' · not yet covered'}
                                            </p>
                                        </li>
                                    ))}
                                </ul>
                            );
                        })()}
                    </div>
                )}
            </div>
        </Card>
    );
};

// The Conclude step is now an INDIVIDUAL survey. Every attendee —
// participants AND the manager — submits their own 1–10 rating + optional
// comment, stored per-person on eos_l10_attendance. The session average is
// derived from those rows server-side; the breakdown is shown live here and
// on the dashboard / Meetings diary afterward.
const ConcludePanel: React.FC<{
    people: PersonLite[];
    attendance: AttendanceRow[];
    currentUserPersonId: string | null;
    managerPersonId: string | null;
    onSubmitSurvey: (payload: {
        rating: number;
        comment: string | null;
    }) => Promise<boolean>;
    isManager: boolean;
    notes: string;
    setNotes: (s: string) => void;
}> = ({
    people,
    attendance,
    currentUserPersonId,
    managerPersonId,
    onSubmitSurvey,
    isManager,
    notes,
    setNotes,
}) => {
    const myRow = currentUserPersonId
        ? attendance.find(a => a.person_id === currentUserPersonId)
        : undefined;
    const [rating, setRating] = useState<number>(myRow?.rating ?? 8);
    const [comment, setComment] = useState<string>(myRow?.rating_comment ?? '');
    const [submitting, setSubmitting] = useState(false);
    const submitted = myRow?.rating != null;

    const peopleById = new Map(people.map(p => [p.id, p]));
    // A "response" is anyone who submitted a rating and/or a written comment.
    const responses = attendance.filter(
        a => a.rating != null || (a.rating_comment ?? '').trim(),
    );
    const ratings = responses
        .map(a => a.rating)
        .filter((r): r is number => r != null);
    const avg = ratings.length
        ? Math.round((ratings.reduce((s, r) => s + r, 0) / ratings.length) * 10) / 10
        : null;

    const submit = async () => {
        setSubmitting(true);
        await onSubmitSurvey({ rating, comment: comment.trim() || null });
        setSubmitting(false);
    };

    return (
        <Card>
            <h3 className="text-lg font-semibold text-gray-900 mb-1">
                Meeting wrap-up · individual survey
            </h3>
            <p className="text-xs text-gray-500 mb-5">
                Everyone rates the meeting — participants and the manager each submit
                their own. The team average is calculated from these and shown on the
                dashboard.
            </p>

            {/* My survey */}
            {currentUserPersonId ? (
                <div className="rounded-md border border-gray-200 bg-gray-50 p-4 mb-6">
                    <div className="flex items-center justify-between mb-2">
                        <label className="text-sm font-medium text-gray-700">
                            Your rating (1 – 10)
                        </label>
                        {submitted && (
                            <span className="text-[11px] font-semibold text-green-700 bg-green-100 px-2 py-0.5 rounded-full">
                                Submitted ✓
                            </span>
                        )}
                    </div>
                    <input
                        type="range"
                        min={1}
                        max={10}
                        step={1}
                        value={rating}
                        onChange={e => setRating(parseInt(e.target.value, 10))}
                        className="w-full accent-brand"
                    />
                    <div className="flex justify-between text-xs text-gray-500 mt-1 mb-3">
                        <span>Poor (1)</span>
                        <span className="text-xl font-bold text-brand">{rating}</span>
                        <span>Excellent (10)</span>
                    </div>
                    <textarea
                        value={comment}
                        onChange={e => setComment(e.target.value)}
                        placeholder="What worked? What can we improve next week? (optional)"
                        className="w-full min-h-[72px] rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    />
                    <div className="flex justify-end mt-2">
                        <button
                            type="button"
                            onClick={submit}
                            disabled={submitting}
                            className="px-4 py-2 text-sm font-medium bg-brand hover:bg-brand-hover disabled:opacity-50 text-white rounded-md"
                        >
                            {submitting
                                ? 'Saving…'
                                : submitted
                                  ? 'Update my rating'
                                  : 'Submit my rating'}
                        </button>
                    </div>
                </div>
            ) : (
                <p className="text-sm text-amber-700 bg-amber-50 border border-amber-100 rounded-md px-3 py-2 mb-6">
                    We couldn't match your account to an EOS person, so your rating can't
                    be recorded. Ask an admin to confirm your user is synced.
                </p>
            )}

            {/* Live results */}
            <div className="mb-6">
                <div className="flex items-center justify-between mb-2">
                    <p className="text-[11px] uppercase tracking-wider font-semibold text-gray-500">
                        Submitted ({responses.length})
                    </p>
                    {avg != null && (
                        <span className="text-sm">
                            Team avg{' '}
                            <span className="text-lg font-bold text-brand">{avg}</span>
                            <span className="text-gray-400">/10</span>
                        </span>
                    )}
                </div>
                {responses.length === 0 ? (
                    <p className="text-sm text-gray-400 italic">
                        No ratings submitted yet.
                    </p>
                ) : (
                    <ul className="space-y-2">
                        {responses.map(a => {
                            const isMgr =
                                managerPersonId != null &&
                                a.person_id === managerPersonId;
                            return (
                                <li
                                    key={a.id}
                                    className="flex items-start justify-between gap-3 text-sm border-l-2 border-gray-200 pl-2"
                                >
                                    <div className="min-w-0">
                                        <span className="font-medium text-gray-800">
                                            {formatPerson(
                                                peopleById.get(a.person_id),
                                                'Someone',
                                            )}
                                        </span>
                                        {isMgr && (
                                            <span className="ml-2 text-[10px] uppercase tracking-wider font-semibold text-brand bg-brand/10 px-1.5 py-0.5 rounded-full">
                                                Manager
                                            </span>
                                        )}
                                        {a.rating_comment && (
                                            <p className="text-xs text-gray-600 italic mt-0.5">
                                                "{a.rating_comment}"
                                            </p>
                                        )}
                                    </div>
                                    <span className="shrink-0 font-mono tabular-nums text-gray-700">
                                        {a.rating != null ? `${a.rating}/10` : '—'}
                                    </span>
                                </li>
                            );
                        })}
                    </ul>
                )}
            </div>

            {/* Manager-only closing notes — the meeting summary (indexed for
                retrieval), separate from per-person survey comments. */}
            {isManager && (
                <div>
                    <label className="block text-sm font-medium text-gray-700 mb-2">
                        Closing notes (manager)
                    </label>
                    <textarea
                        value={notes}
                        onChange={e => setNotes(e.target.value)}
                        placeholder="Summary, decisions, and anything to carry into next week."
                        className="w-full min-h-[100px] rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    />
                    <p className="text-[11px] text-gray-400 mt-1">
                        Click “Finish &amp; rate” below to conclude — the team average is
                        recorded from everyone's ratings.
                    </p>
                </div>
            )}
        </Card>
    );
};

// ─── In-meeting forms ───────────────────────────────────────────────────────

const TransferManagementForm: React.FC<{
    candidates: PersonLite[];
    onSubmit: (newManagerId: string) => void | Promise<void>;
    onCancel: () => void;
}> = ({ candidates, onSubmit, onCancel }) => {
    const [pickedId, setPickedId] = useState(candidates[0]?.id ?? '');
    const [submitting, setSubmitting] = useState(false);

    const handle = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!pickedId) return;
        setSubmitting(true);
        try {
            await onSubmit(pickedId);
        } finally {
            setSubmitting(false);
        }
    };

    if (candidates.length === 0) {
        return (
            <div className="space-y-4">
                <p className="text-sm text-gray-600">
                    Nobody else is available to take over yet. People are synced from the
                    main system — once another teammate signs in, they'll appear here.
                </p>
                <div className="flex justify-end">
                    <button
                        type="button"
                        onClick={onCancel}
                        className="px-4 py-2 text-sm font-medium text-gray-700 hover:text-gray-900"
                    >
                        Close
                    </button>
                </div>
            </div>
        );
    }

    return (
        <form onSubmit={handle} className="space-y-4">
            <p className="text-sm text-gray-600">
                Hand off the manager role for this meeting. You'll immediately become a
                participant; the new manager picks up the role on their next page load or
                refresh.
            </p>
            <Field label="New manager">
                <select
                    required
                    value={pickedId}
                    onChange={e => setPickedId(e.target.value)}
                    className={inputCls}
                    autoFocus
                >
                    <option value="" disabled>Select a person…</option>
                    {candidates.map(p => (
                        <option key={p.id} value={p.id}>
                            {p.first_name} {p.last_name} — {p.email}
                        </option>
                    ))}
                </select>
            </Field>
            <SubmitButton disabled={submitting} onCancel={onCancel}>
                Transfer
            </SubmitButton>
        </form>
    );
};

