import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
    AlertCircle,
    CheckCircle2,
    Plus,
    Edit2,
    Trash2,
    MessageSquare,
    ListPlus,
    CheckSquare,
    Square,
    Calendar,
    ChevronDown,
    ChevronRight,
    Search,
} from 'lucide-react';
import client from '../../api/client';
import { errorMessage } from '../../utils/errors';
import { formatInstant, instantMs, parseInstant } from '../../utils/datetime';
import {
    EmptyState,
    ErrorState,
    EosPageHeader,
    ExecBadge,
    KpiCard,
    KpiStrip,
    LoadingState,
    OwnerChip,
    PageContainer,
    StatusBadge,
    TeamFilter,
} from '../../pages/eos/_shared';
import {
    scopeToTeam,
    formatPerson,
    matchesTeamFilter,
    EXEC_SCOPE_LABEL,
    TODO_MAX_LEN,
    type TeamScope,
    type PersonLite,
    useTeamFilter,
} from '../../pages/eos/_utils';
import { Modal, Field, inputCls, SubmitButton } from './Modal';
import { IssueDetailModal } from './IssueDetailModal';
import { NotesThread } from './NotesThread';
import { useAuth } from '../../context/AuthContext';
import type { MeetingContext } from './eosViewTypes';
// The child to-dos an Issue manages share the To-Dos page's shape + reconciler,
// so the created to-do can flow into the host's todos state (and, in the L10,
// surface in the To-Do segment — this is the Issue→To-Do link).
import type { Todo, TodoChange } from './TodosView';

type IssuePriority = 'low' | 'medium' | 'high';
type IssueStatus = 'open' | 'solved' | 'archived';

export interface Issue {
    id: string;
    // Short headline shown on the IDS grid. The server always populates it
    // (deriving from the description when a title isn't supplied), but callers
    // still fall back to the description defensively.
    title: string;
    description: string;
    priority: IssuePriority;
    status: IssueStatus;
    owner_id: string | null;
    department_name: string | null;
    is_exec: boolean; // Exec/Leadership-team issue (exec-only, server-enforced)
    l10_meeting_id: string | null;
    created_by: string | null; // Who created it (delete-permission check)
    created_at: string; // when the issue was raised — shown on the row
    solved_at: string | null; // when it was solved — set once the issue closes
}

/**
 * Reconciliation events the host applies to its own `issues` state. In the L10
 * host this ALSO bumps the poll-suppression window so the 2s live poll doesn't
 * clobber the optimistic edit. `set` carries a partial patch (used for the
 * optimistic solve/reopen toggle).
 */
export type IssueChange =
    | { type: 'upsert'; issue: Issue }
    | { type: 'remove'; id: string }
    | { type: 'set'; id: string; patch: Partial<Issue> };

// Open issues are grouped into fixed priority buckets (High, Medium, Low),
// mirroring the To-Dos page's due-date buckets. Each carries its own uppercase
// sub-header colour.
const PRIORITY_BUCKETS: { key: IssuePriority; label: string; headerCls: string }[] = [
    { key: 'high', label: 'High', headerCls: 'text-red-700' },
    { key: 'medium', label: 'Medium', headerCls: 'text-amber-700' },
    { key: 'low', label: 'Low', headerCls: 'text-gray-600' },
];

const DAY_MS = 24 * 60 * 60 * 1000;

// Lookback windows offered on the Solved drawer. Leaders asked for "issues solved
// in the last 30-60-90 days" after the Headlines step's own last-30-days list,
// so the drawer reads the same way: a trailing window, newest first, read-only
// context rather than a second work list.
const SOLVED_WINDOWS = [30, 60, 90] as const;
type SolvedWindow = (typeof SOLVED_WINDOWS)[number] | 'all';
// What a meeting preloads: the widest window the drawer offers, so switching
// 30 → 60 → 90 is instant and the KPI tiles (7-day + 90-day) are fully fed by
// one request. The standalone page already holds every status, so it filters
// what it has and keeps an "All" option.
const SOLVED_HISTORY_DAYS = 90;

// Compact "when was this raised / solved" stamp. Date-only (the user asked for
// a *date*) with the year, since issues can sit open across months and a bare
// "Jul 8" would be ambiguous.
const _fmtCreated = (iso: string): string =>
    formatInstant(iso, { month: 'short', day: 'numeric', year: 'numeric' }, iso);

interface FormState {
    title: string;
    description: string;
    priority: IssuePriority;
    status: IssueStatus;
    team: string; // Meeting-scope value (department name or __exec__)
}

const EMPTY_FORM: FormState = {
    title: '',
    description: '',
    priority: 'medium',
    status: 'open',
    team: '',
};

// "Turn this issue into a to-do" mini-form.
interface TodoFormState {
    description: string;
    owner_email: string; // '' → let the server default to the issue owner
    due_date: string; // YYYY-MM-DD or ''
}

const EMPTY_TODO_FORM: TodoFormState = { description: '', owner_email: '', due_date: '' };

interface IssuesViewProps {
    /** 'page' renders full-page chrome (PageContainer + header + team filter);
     * 'meeting' renders a compact header for embedding in an L10 segment. */
    mode: 'page' | 'meeting';
    issues: Issue[];
    /** Every child to-do the host knows about (the To-Dos page shape). Grouped
     * by `source_issue_id` into per-issue dependency chips + child lists. */
    todos: Todo[];
    people: PersonLite[];
    /** Team scopes for the page filter + the create-form team select. Omit in
     * meeting mode (the team is locked to the meeting). */
    teamScopes?: TeamScope[];
    /** Present only in meeting mode: stamps created issues with the meeting's
     * team + links them to this L10 so they resurface in future meetings. */
    meetingContext?: MeetingContext;
    loading?: boolean;
    error?: string | null;
    /** Host reconciles its own `issues` state (and, in the L10, suppresses the
     * live poll) in response to these events. */
    onIssueChange: (change: IssueChange) => void;
    /** Fired when a child to-do is created for an issue so the host adds it to
     * its `todos` state — in the L10 this makes the to-do appear in the To-Do
     * segment. This is the Issue→To-Do link. */
    onTodoChange: (change: TodoChange) => void;
}

/**
 * The shared Issues (IDS) view — the single source of truth for the standalone
 * Issues page and the in-meeting L10 "IDS" segment. Fully controlled: the host
 * owns fetching + the `issues` array AND the `todos` array (child to-dos); this
 * component owns rendering, derivations, ephemeral UI state, all modals, and the
 * write calls — reporting results back via `onIssueChange` / `onTodoChange`.
 */
export const IssuesView: React.FC<IssuesViewProps> = ({
    mode,
    issues,
    todos,
    people,
    teamScopes = [],
    meetingContext,
    loading = false,
    error = null,
    onIssueChange,
    onTodoChange,
}) => {
    const { isAdmin, isExec, managesDept, user } = useAuth();
    // You can delete your own issues; admins / exec can delete any.
    // A Team Manager can delete any issue on a team they lead.
    const canDelete = (issue: Issue): boolean =>
        isAdmin ||
        isExec ||
        managesDept(issue.department_name) ||
        (!!issue.created_by &&
            !!user?.email &&
            issue.created_by.toLowerCase() === user.email.toLowerCase());

    const isMeeting = mode === 'meeting';

    const [modalOpen, setModalOpen] = useState(false);
    const [editing, setEditing] = useState<Issue | null>(null);
    const [form, setForm] = useState<FormState>(EMPTY_FORM);
    const [submitting, setSubmitting] = useState(false);
    // Read-only "full issue" modal opened by clicking a row (the grid shows
    // only the title). We store the id (not a snapshot) so the modal reflects
    // the LIVE issue — in the L10 the 2s poll continuously replaces `issues`,
    // and a frozen snapshot would drift from the (live) linked-to-dos section.
    // Resolving from `issues` each render also auto-closes the modal if the
    // issue is deleted out from under it. Null = closed.
    const [detailIssueId, setDetailIssueId] = useState<string | null>(null);
    // Team filter for the IDS list: 'all' | '__exec__' | department name.
    const [teamFilter, setTeamFilter] = useTeamFilter(teamScopes, 'eos.teamFilter.issues');
    // Which issue's notes thread is expanded.
    const [notesOpenId, setNotesOpenId] = useState<string | null>(null);

    // Scrum-style child to-dos: which issue's child-to-do list is expanded.
    const [expandedTodosId, setExpandedTodosId] = useState<string | null>(null);
    // Inline explanation when the server refuses to solve an issue (HTTP 409:
    // it still has open child to-dos the caller might not be able to see).
    const [solveError, setSolveError] = useState<{ id: string; msg: string } | null>(null);

    // Collapsed-by-default closed sections (mirrors the To-Dos "Completed" card).
    const [showSolved, setShowSolved] = useState(false);
    const [showArchived, setShowArchived] = useState(false);

    // Substring search over title + description. There was no search anywhere
    // in EOS, which in the exec L10 meant "we talked about this last week"
    // ended with the facilitator leaving the meeting, opening Issues, expanding
    // Solved, and reading down the list.
    const [search, setSearch] = useState('');
    // In a meeting `issues` is open-only by construction — GET
    // /l10/meetings/{id}/items filters to IssueStatus.OPEN, and it should stay
    // that way (the IDS list is today's work, not the archive). Two separate
    // reads fill that gap, both team-scoped by the server:
    //
    //  1. `recentSolved` — the team's last SOLVED_HISTORY_DAYS of solved
    //     issues, loaded with the step. This is what makes "Solved this week"
    //     and "Solve rate 90d" true in the room: before it, an issue solved
    //     mid-meeting left the client on the next items poll, so the tile read
    //     0 right after a team solved three.
    //     It also backs the Solved drawer's 30/60/90-day lookback.
    //  2. `searchHistory` — every closed issue the caller may read, all-time,
    //     fetched once the first time someone searches, so "we solved this in
    //     the spring" is findable without leaving the room.
    const [recentSolved, setRecentSolved] = useState<Issue[] | null>(null);
    const [recentSolvedError, setRecentSolvedError] = useState<string | null>(null);
    // Bumped by any write that can change what's solved, to re-pull (1).
    const [historyReload, setHistoryReload] = useState(0);
    const [searchHistory, setSearchHistory] = useState<Issue[] | null>(null);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [historyError, setHistoryError] = useState<string | null>(null);
    // Which trailing window the Solved drawer lists. A meeting defaults to the
    // nearest one (last 30 days = "what have we been closing lately"); the page
    // defaults to All so the full archive it already holds stays visible.
    const [solvedWindow, setSolvedWindow] = useState<SolvedWindow>(
        mode === 'meeting' ? 30 : 'all',
    );

    // "Turn issue into a to-do" modal — the issue being acted on (null = closed).
    const [todoModalIssue, setTodoModalIssue] = useState<Issue | null>(null);
    const [todoForm, setTodoForm] = useState<TodoFormState>(EMPTY_TODO_FORM);
    const [todoSubmitting, setTodoSubmitting] = useState(false);

    // Owner lookups: by id for issue/to-do owners, by (lower-cased) email for the
    // issue's `created_by` stamp.
    const peopleById = useMemo(() => new Map(people.map(p => [p.id, p])), [people]);
    const peopleByEmail = useMemo(
        () => new Map(people.map(p => [p.email.toLowerCase(), p])),
        [people],
    );

    // Fetch-once guard as a REF, not state. Deriving it from `historyLoading`
    // put that state in the dep array, so flipping it to true re-ran the effect,
    // whose cleanup cancelled the in-flight request — leaving `historyLoading`
    // stuck true and the history never loaded.
    const historyRequestedRef = useRef(false);
    const searchIsActive = search.trim().length > 0;
    useEffect(() => {
        if (!isMeeting || !searchIsActive) return;
        if (historyRequestedRef.current) return;
        historyRequestedRef.current = true;
        let cancelled = false;
        setHistoryLoading(true);
        setHistoryError(null);
        (async () => {
            try {
                // No status param needed — this endpoint returns every status
                // the caller may read, scoped by team.
                const params = new URLSearchParams();
                if (meetingContext?.team.is_exec) params.set('is_exec', 'true');
                else if (meetingContext?.team.department_name) {
                    params.set('department_name', meetingContext.team.department_name);
                }
                const qs = params.toString();
                const res = await client.get<Issue[]>(
                    `/api/eos/issues/${qs ? `?${qs}` : ''}`,
                );
                if (!cancelled) {
                    setSearchHistory(res.data.filter(i => i.status !== 'open'));
                }
            } catch (err) {
                if (!cancelled) {
                    // Let the next keystroke retry rather than wedging on a
                    // single network blip.
                    historyRequestedRef.current = false;
                    setHistoryError(
                        err instanceof Error ? err.message : 'Failed to load past issues',
                    );
                }
            } finally {
                if (!cancelled) setHistoryLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [isMeeting, searchIsActive, meetingContext]);

    // The meeting's solved-history preload. Keyed on the meeting ID, never on
    // `meetingContext` — the L10 host rebuilds that object on every render and
    // the step re-renders on a 2s poll, so depending on the object itself would
    // refetch in a loop.
    const l10MeetingId = meetingContext?.l10MeetingId ?? null;
    useEffect(() => {
        if (!isMeeting || !l10MeetingId) return;
        let cancelled = false;
        (async () => {
            try {
                const res = await client.get<Issue[]>(
                    `/api/eos/l10/meetings/${l10MeetingId}/issues/history?days=${SOLVED_HISTORY_DAYS}`,
                );
                if (cancelled) return;
                setRecentSolved(res.data);
                setRecentSolvedError(null);
            } catch (err) {
                if (cancelled) return;
                // Non-fatal: the open list — the meeting's actual work — is
                // unaffected, so this reads as a note under the tiles rather
                // than an error state thrown over the whole IDS step.
                setRecentSolvedError(
                    err instanceof Error ? err.message : 'Failed to load solved issues',
                );
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [isMeeting, l10MeetingId, historyReload]);

    // In a meeting the solved rows come from that fetch rather than from the
    // host's `issues`, so every write that can change what's solved re-pulls
    // it — else the issue you just solved, reopened, edited or deleted keeps
    // its stale shape in the drawer for the rest of the meeting.
    const refreshSolvedHistory = () => {
        if (isMeeting) setHistoryReload(n => n + 1);
    };

    // Everything below the KPI strip + list respects the team filter. In a
    // meeting the host's `issues` holds only the OPEN list, so the fetched
    // solved rows are folded in here — that one merge is what makes the tiles
    // count solves, gives the Solved drawer something to show, and lets a
    // search reach back past this week.
    const scopedIssues = useMemo(() => {
        if (!isMeeting) return issues;
        const extra = [...(recentSolved ?? []), ...(searchHistory ?? [])];
        if (extra.length === 0) return issues;
        const byId = new Map<string, Issue>();
        for (const i of extra) byId.set(i.id, i);
        // `issues` wins every collision: it carries this meeting's optimistic
        // edits, and a just-reopened issue is stale in the fetched history.
        for (const i of issues) byId.set(i.id, i);
        return [...byId.values()];
    }, [isMeeting, issues, recentSolved, searchHistory]);

    const teamFiltered = useMemo(
        () => scopedIssues.filter(i => matchesTeamFilter(teamFilter, i)),
        [scopedIssues, teamFilter],
    );

    const searchFiltered = useMemo(() => {
        const q = search.trim().toLowerCase();
        if (!q) return teamFiltered;
        return teamFiltered.filter(
            i =>
                (i.title ?? '').toLowerCase().includes(q) ||
                (i.description ?? '').toLowerCase().includes(q),
        );
    }, [teamFiltered, search]);

    const openIssues = useMemo(
        () => teamFiltered.filter(i => i.status === 'open'),
        [teamFiltered],
    );
    // Newest-solved first.
    const solvedIssues = useMemo(
        () =>
            teamFiltered
                .filter(i => i.status === 'solved')
                .sort((a, b) => {
                    const at = a.solved_at ? new Date(a.solved_at).getTime() : 0;
                    const bt = b.solved_at ? new Date(b.solved_at).getTime() : 0;
                    return bt - at;
                }),
        [teamFiltered],
    );
    const archivedIssues = useMemo(
        () =>
            teamFiltered
                .filter(i => i.status === 'archived')
                .sort(
                    (a, b) =>
                        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
                ),
        [teamFiltered],
    );

    // What the LISTS render. Separate from the three memos above, which the KPI
    // strip reads: a search narrows what you're looking at, it must not restate
    // the team's open count as "1".
    const searchActive = searchIsActive;
    // The Solved drawer is collapsed by default but forced open by a search.
    const solvedOpen = showSolved || searchActive;
    const matchIds = useMemo(
        () => new Set(searchFiltered.map(i => i.id)),
        [searchFiltered],
    );
    const displayOpen = useMemo(
        () => (searchActive ? openIssues.filter(i => matchIds.has(i.id)) : openIssues),
        [searchActive, openIssues, matchIds],
    );
    // Solved rows come from `searchFiltered` rather than `solvedIssues` so a
    // search can surface a match the lookback window would otherwise hide.
    // Searching is the "find it wherever it is" path — dropping a match
    // because it was solved 100 days ago is the dead end search exists to fix.
    const displaySolved = useMemo(() => {
        if (searchActive) {
            return searchFiltered
                .filter(i => i.status === 'solved')
                .sort((a, b) => {
                    const at = a.solved_at ? new Date(a.solved_at).getTime() : 0;
                    const bt = b.solved_at ? new Date(b.solved_at).getTime() : 0;
                    return bt - at;
                });
        }
        if (solvedWindow === 'all') return solvedIssues;
        const cutoff = Date.now() - solvedWindow * DAY_MS;
        return solvedIssues.filter(i => {
            const t = instantMs(i.solved_at);
            return t !== null && t >= cutoff;
        });
    }, [searchActive, searchFiltered, solvedIssues, solvedWindow]);

    // A meeting can only offer windows it has actually loaded (the preload is
    // SOLVED_HISTORY_DAYS wide); the page holds every solved issue already, so
    // it keeps an All option and defaults to it.
    const solvedWindowOptions: SolvedWindow[] = isMeeting
        ? [...SOLVED_WINDOWS]
        : [...SOLVED_WINDOWS, 'all'];
    const displayArchived = useMemo(
        () =>
            searchActive
                ? searchFiltered.filter(i => i.status === 'archived')
                : archivedIssues,
        [searchActive, searchFiltered, archivedIssues],
    );

    // Split the open issues into fixed priority buckets, each sorted oldest-open
    // first so the longest-standing issues read at the top.
    const openBuckets = useMemo(() => {
        const groups: Record<IssuePriority, Issue[]> = { high: [], medium: [], low: [] };
        for (const i of displayOpen) groups[i.priority].push(i);
        const byCreatedAsc = (a: Issue, b: Issue) =>
            new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
        groups.high.sort(byCreatedAsc);
        groups.medium.sort(byCreatedAsc);
        groups.low.sort(byCreatedAsc);
        return groups;
    }, [displayOpen]);

    // KPI summary strip — current values only over the team-filtered issues (no
    // historical baseline, so no trend arrows/deltas).
    const kpis = useMemo(() => {
        const now = Date.now();
        const midnight = new Date();
        midnight.setHours(0, 0, 0, 0);
        const solvedWeekCutoff = midnight.getTime() - 6 * DAY_MS; // today + prior 6 days = a 7-day window
        const cohort90Cutoff = now - 90 * DAY_MS;

        const openCount = openIssues.length;

        const solvedThisWeek = teamFiltered.filter(i => {
            if (!i.solved_at) return false;
            const d = parseInstant(i.solved_at);
            if (!d) return false;
            d.setHours(0, 0, 0, 0);
            return d.getTime() >= solvedWeekCutoff;
        }).length;

        const cohort90 = teamFiltered.filter(i => {
            const t = instantMs(i.created_at);
            return t !== null && t >= cohort90Cutoff;
        });
        const solvedInCohort = cohort90.filter(i => i.status === 'solved').length;
        const solveRate90 =
            cohort90.length === 0 ? null : Math.round((solvedInCohort / cohort90.length) * 100);

        const ages = openIssues
            .map(i => instantMs(i.created_at))
            .filter((t): t is number => t !== null)
            .map(t => (now - t) / DAY_MS);
        const avgDaysOpen =
            ages.length === 0 ? null : ages.reduce((a, b) => a + b, 0) / ages.length;

        return { openCount, solvedThisWeek, solveRate90, avgDaysOpen };
    }, [teamFiltered, openIssues]);

    // Group child to-dos under their parent issue so each row can show a
    // dependency chip + an expandable list.
    const childTodosByIssue = useMemo(() => {
        const map = new Map<string, Todo[]>();
        for (const t of todos) {
            if (!t.source_issue_id) continue;
            const arr = map.get(t.source_issue_id);
            if (arr) arr.push(t);
            else map.set(t.source_issue_id, [t]);
        }
        return map;
    }, [todos]);

    const openNew = () => {
        setEditing(null);
        // Default to the caller's primary team scope.
        setForm({ ...EMPTY_FORM, team: teamScopes[0]?.value ?? '' });
        setModalOpen(true);
    };

    const openEdit = (issue: Issue) => {
        setEditing(issue);
        setForm({
            title: issue.title ?? '',
            description: issue.description,
            priority: issue.priority,
            status: issue.status,
            team: '', // not editable after create
        });
        setModalOpen(true);
    };

    const closeModal = () => {
        setModalOpen(false);
        setEditing(null);
    };

    const onSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setSubmitting(true);
        try {
            if (editing) {
                const res = await client.patch<Issue>(`/api/eos/issues/${editing.id}`, form);
                onIssueChange({ type: 'upsert', issue: res.data });
                refreshSolvedHistory();
            } else {
                // Server defaults status=open on create; we send it explicitly anyway.
                const { title, description, priority, status } = form;
                const payload: Record<string, unknown> = { title, description, priority, status };
                if (meetingContext) {
                    // Meeting mode: stamp the meeting's team + link to this L10 so
                    // the issue resurfaces in the team's future meetings.
                    Object.assign(payload, meetingContext.team);
                    payload.l10_meeting_id = meetingContext.l10MeetingId;
                } else {
                    // Tag the issue with its team so it surfaces in that team's L10.
                    Object.assign(payload, scopeToTeam(form.team));
                }
                const res = await client.post<Issue>('/api/eos/issues/', payload);
                onIssueChange({ type: 'upsert', issue: res.data });
            }
            closeModal();
        } catch (err) {
            console.error('Failed to save issue', err);
            alert('Failed to save Issue.');
        } finally {
            setSubmitting(false);
        }
    };

    const toggleStatus = async (issue: Issue) => {
        const newStatus: IssueStatus = issue.status === 'solved' ? 'open' : 'solved';
        // Clear any stale "can't solve yet" message for this issue.
        setSolveError(prev => (prev?.id === issue.id ? null : prev));
        onIssueChange({ type: 'set', id: issue.id, patch: { status: newStatus } });
        try {
            await client.patch(`/api/eos/issues/${issue.id}`, { status: newStatus });
            // A meeting's open list drops the row on the next items poll, so
            // without this the issue just solved would vanish from the room
            // entirely instead of landing in the Solved drawer (and the tile
            // would stay at 0). Reopening needs it too: the fetched history
            // still holds the row as solved until it's re-pulled.
            refreshSolvedHistory();
        } catch (err) {
            console.error('Status toggle failed', err);
            // Revert the optimistic flip.
            onIssueChange({ type: 'set', id: issue.id, patch: { status: issue.status } });
            // The server returns 409 with a { detail } message when the issue
            // still has open child to-dos (which the caller may not even be able
            // to see). Surface that inline instead of throwing / failing silently.
            const status = (err as { response?: { status?: number } })?.response?.status;
            const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data
                ?.detail;
            if (status === 409 && typeof detail === 'string') {
                setSolveError({ id: issue.id, msg: detail });
            }
        }
    };

    const onDelete = async (issue: Issue) => {
        if (!confirm(`Delete issue: "${issue.title || issue.description}"?`)) return;
        try {
            await client.delete(`/api/eos/issues/${issue.id}`);
            onIssueChange({ type: 'remove', id: issue.id });
            refreshSolvedHistory();
        } catch (err) {
            console.error('Delete failed', err);
            alert('Failed to delete Issue.');
        }
    };

    // ─── Scrum-style "turn issue into a to-do" ──────────────────────────────
    const openTodoModal = (issue: Issue) => {
        // Prefill the description from the issue and default the owner to the
        // issue's owner when we can resolve them (else leave it on the server
        // default). Both stay editable.
        const owner = issue.owner_id ? peopleById.get(issue.owner_id) : undefined;
        setTodoForm({
            // Trim to the To-Do cap: an issue body is routinely a paragraph, and
            // the to-do row renders on a line or two.
            description: issue.description.slice(0, TODO_MAX_LEN),
            owner_email: owner?.email ?? '',
            due_date: '',
        });
        setTodoModalIssue(issue);
    };

    const closeTodoModal = () => {
        setTodoModalIssue(null);
        setTodoForm(EMPTY_TODO_FORM);
    };

    const onSubmitTodo = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!todoModalIssue) return;
        setTodoSubmitting(true);
        try {
            const payload: Record<string, unknown> = {
                description: todoForm.description,
                due_date: todoForm.due_date ? new Date(todoForm.due_date).toISOString() : undefined,
            };
            // Omit owner_email when blank so the server applies its default
            // (the issue's owner, then the caller). The server copies the team
            // from the parent issue, so no team stamping is needed here.
            if (todoForm.owner_email) payload.owner_email = todoForm.owner_email;
            const res = await client.post<Todo>(
                `/api/eos/issues/${todoModalIssue.id}/todos`,
                payload,
            );
            // Report the created child to-do so the host adds it to its `todos`
            // state (the chip + child list pick it up; in the L10 it appears in
            // the To-Do segment). Expand this issue so it's visible.
            onTodoChange({ type: 'upsert', todo: res.data });
            setExpandedTodosId(todoModalIssue.id);
            closeTodoModal();
        } catch (err) {
            console.error('Failed to create child to-do', err);
            alert(errorMessage(err, 'Failed to add To-Do. Please try again.'));
        } finally {
            setTodoSubmitting(false);
        }
    };

    // Render a ` · `-separated meta line from a list of nodes (skips separators
    // around omitted pieces).
    const metaRow = (parts: React.ReactNode[]) =>
        parts.map((node, idx) => (
            <React.Fragment key={idx}>
                {idx > 0 && <span className="text-gray-300">·</span>}
                {node}
            </React.Fragment>
        ));

    // The expandable panels (409 banner, read-only child to-dos, notes thread)
    // shared by every row, rendered below the row inside its <li>.
    const rowPanels = (issue: Issue, childTodos: Todo[], todosExpanded: boolean) => (
        <>
            {/* Inline reason the issue can't be solved yet (server 409). */}
            {solveError?.id === issue.id && (
                <div className="border-t border-red-100 px-6 py-2 bg-red-50 text-xs text-red-700 flex items-start gap-2">
                    <AlertCircle size={14} className="flex-shrink-0 mt-0.5" />
                    <span>{solveError.msg}</span>
                </div>
            )}
            {/* Scrum child-to-do list (read-only; completion is managed on the
                To-Dos page / in the L10). Toggled by the dependency chip. */}
            {todosExpanded && childTodos.length > 0 && (
                <div className="border-t border-gray-100 px-6 py-3 bg-gray-50/60">
                    <ul className="space-y-2">
                        {childTodos.map(todo => (
                            <li key={todo.id} className="flex items-center gap-3 text-sm">
                                {todo.status === 'done' ? (
                                    <CheckSquare size={15} className="text-green-500 flex-shrink-0" />
                                ) : (
                                    <Square size={15} className="text-gray-300 flex-shrink-0" />
                                )}
                                <span
                                    className={`flex-1 ${
                                        todo.status === 'done'
                                            ? 'text-gray-400 line-through'
                                            : 'text-gray-700'
                                    }`}
                                >
                                    {todo.description}
                                </span>
                                <span className="text-xs text-gray-500">
                                    {formatPerson(peopleById.get(todo.owner_id))}
                                </span>
                                {todo.due_date && (
                                    <span className="flex items-center gap-1 text-xs text-gray-500">
                                        <Calendar size={12} />
                                        {new Date(todo.due_date).toLocaleDateString()}
                                    </span>
                                )}
                            </li>
                        ))}
                    </ul>
                </div>
            )}
            {/* Discussion / resolution notes thread. */}
            {notesOpenId === issue.id && (
                <div className="border-t border-gray-100 px-6 py-3 bg-gray-50/60">
                    <NotesThread
                        basePath={`/api/eos/issues/${issue.id}/notes`}
                        emptyHint="No notes yet. Capture the discussion / resolution here before solving."
                    />
                </div>
            )}
        </>
    );

    // Hover-only action cluster (notes / edit / delete), optionally with the
    // "turn into a to-do" action for open rows.
    const hoverActions = (issue: Issue, withAddTodo: boolean) => (
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity flex-shrink-0">
            <button
                type="button"
                onClick={() => setNotesOpenId(notesOpenId === issue.id ? null : issue.id)}
                className={`p-1.5 rounded hover:bg-gray-100 ${
                    notesOpenId === issue.id
                        ? 'text-brand'
                        : 'text-gray-400 hover:text-gray-700'
                }`}
                aria-label="Notes"
                title="Discussion / resolution notes"
            >
                <MessageSquare size={15} />
            </button>
            {withAddTodo && (
                <button
                    type="button"
                    onClick={() => openTodoModal(issue)}
                    className="p-1.5 rounded hover:bg-gray-100 text-gray-400 hover:text-brand"
                    aria-label="Add to-do"
                    title="Add a to-do for this issue"
                >
                    <ListPlus size={15} />
                </button>
            )}
            <button
                type="button"
                onClick={() => openEdit(issue)}
                className="p-1.5 text-gray-400 hover:text-gray-700 hover:bg-gray-100 rounded"
                aria-label="Edit"
            >
                <Edit2 size={14} />
            </button>
            {canDelete(issue) && (
                <button
                    type="button"
                    onClick={() => onDelete(issue)}
                    className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded"
                    aria-label="Delete"
                >
                    <Trash2 size={14} />
                </button>
            )}
        </div>
    );

    // The single dependency chip — amber while any linked to-do is open, green
    // once they're all done. Clicking it expands the read-only child list. Shown
    // on every row that has child to-dos, regardless of status, so a solved /
    // archived issue's linked to-dos stay viewable inline (as before the redesign).
    const dependencyChip = (issue: Issue, childTodos: Todo[], todosExpanded: boolean) => {
        if (childTodos.length === 0) return null;
        const openChildCount = childTodos.filter(t => t.status !== 'done').length;
        return (
            <button
                type="button"
                onClick={() => setExpandedTodosId(todosExpanded ? null : issue.id)}
                className="flex-shrink-0 rounded-full hover:opacity-80 transition-opacity focus:outline-none focus:ring-2 focus:ring-brand/30"
                aria-expanded={todosExpanded}
                aria-label="Toggle linked to-dos"
                title={todosExpanded ? 'Hide linked to-dos' : 'Show linked to-dos'}
            >
                <StatusBadge
                    value={
                        openChildCount > 0
                            ? `${openChildCount} of ${childTodos.length} to-dos open`
                            : 'to-dos done — ready'
                    }
                    tone={openChildCount > 0 ? 'amber' : 'green'}
                />
            </button>
        );
    };

    const renderOpenRow = (issue: Issue) => {
        const childTodos = childTodosByIssue.get(issue.id) ?? [];
        const openChildCount = childTodos.filter(t => t.status !== 'done').length;
        // An open issue can't be solved while any child to-do is still open.
        const solveBlocked = openChildCount > 0;
        const todosExpanded = expandedTodosId === issue.id;
        const owner = issue.owner_id ? peopleById.get(issue.owner_id) : undefined;

        const meta: React.ReactNode[] = [];
        if (issue.created_by) {
            meta.push(
                <span>
                    Raised by{' '}
                    {formatPerson(
                        peopleByEmail.get(issue.created_by.toLowerCase()),
                        issue.created_by,
                    )}
                </span>,
            );
        }
        meta.push(<span>{_fmtCreated(issue.created_at)}</span>);
        const team = issue.is_exec ? EXEC_SCOPE_LABEL : issue.department_name;
        if (team) meta.push(<span>{team}</span>);

        return (
            <li key={issue.id}>
                <div className="px-6 py-3.5 flex items-center gap-4 group">
                    {/* Solve circle (like the To-Dos checkbox). Disabled + grayed
                        while child to-dos are still open. */}
                    <button
                        type="button"
                        onClick={() => toggleStatus(issue)}
                        disabled={solveBlocked}
                        className="flex-shrink-0 rounded-full disabled:cursor-not-allowed"
                        aria-label="Mark solved"
                        title={solveBlocked ? 'Finish the linked to-dos first.' : 'Mark solved'}
                    >
                        <span
                            className={`block w-[18px] h-[18px] rounded-full border-2 transition-colors ${
                                solveBlocked
                                    ? 'border-gray-200'
                                    : 'border-gray-300 group-hover:border-brand'
                            }`}
                        />
                    </button>

                    <div className="flex-1 min-w-0">
                        <button
                            type="button"
                            onClick={() => setDetailIssueId(issue.id)}
                            className="flex items-center gap-2 min-w-0 max-w-full text-left rounded focus:outline-none focus:ring-2 focus:ring-brand/30 group/title"
                            title="View issue details"
                        >
                            {issue.is_exec && <ExecBadge />}
                            <span className="text-sm font-medium text-gray-900 truncate group-hover/title:text-brand group-hover/title:underline">
                                {issue.title || issue.description}
                            </span>
                        </button>
                        <div className="text-xs text-gray-400 mt-1 flex items-center flex-wrap gap-x-1.5 gap-y-1">
                            {metaRow(meta)}
                        </div>
                    </div>

                    {dependencyChip(issue, childTodos, todosExpanded)}

                    {owner && (
                        <span className="flex-shrink-0">
                            <OwnerChip name={formatPerson(owner)} />
                        </span>
                    )}

                    {hoverActions(issue, true)}
                </div>
                {rowPanels(issue, childTodos, todosExpanded)}
            </li>
        );
    };

    // Solved / archived rows — the collapsed-section pattern from the To-Dos
    // "Completed" list: line-through description + a muted meta line.
    const renderClosedRow = (issue: Issue, kind: 'solved' | 'archived') => {
        const owner = issue.owner_id ? peopleById.get(issue.owner_id) : undefined;
        const team = issue.is_exec ? EXEC_SCOPE_LABEL : issue.department_name;
        const childTodos = childTodosByIssue.get(issue.id) ?? [];
        const todosExpanded = expandedTodosId === issue.id;

        // Keep the same meta contract as open rows: solved/created stamp, who
        // raised it, and the team — so no information disappears once closed.
        const meta: React.ReactNode[] = [];
        if (kind === 'solved' && issue.solved_at) {
            meta.push(
                <span className="text-green-600 font-medium">
                    Solved {_fmtCreated(issue.solved_at)}
                </span>,
            );
        }
        if (kind === 'archived') {
            meta.push(<span>Created {_fmtCreated(issue.created_at)}</span>);
        }
        if (issue.created_by) {
            meta.push(
                <span>
                    Raised by{' '}
                    {formatPerson(
                        peopleByEmail.get(issue.created_by.toLowerCase()),
                        issue.created_by,
                    )}
                </span>,
            );
        }
        if (team) meta.push(<span>{team}</span>);

        return (
            <li key={issue.id}>
                <div className="px-6 py-3 flex items-center gap-4 group">
                    {kind === 'solved' ? (
                        <button
                            type="button"
                            onClick={() => toggleStatus(issue)}
                            className="text-green-500 hover:text-green-700 transition-colors flex-shrink-0"
                            aria-label="Reopen"
                            title="Reopen this issue"
                        >
                            <CheckCircle2 size={18} />
                        </button>
                    ) : (
                        // Archived issues can still be re-solved by clicking the
                        // circle (toggleStatus flips a non-solved issue to solved),
                        // matching the pre-redesign behavior where every row's
                        // status control was live.
                        <button
                            type="button"
                            onClick={() => toggleStatus(issue)}
                            className="flex-shrink-0 rounded-full"
                            aria-label="Mark solved"
                            title="Mark this archived issue solved"
                        >
                            <span className="block w-[18px] h-[18px] rounded-full border-2 border-gray-300 group-hover:border-brand transition-colors" />
                        </button>
                    )}

                    <div className="flex-1 min-w-0">
                        <button
                            type="button"
                            onClick={() => setDetailIssueId(issue.id)}
                            className="flex items-center gap-2 min-w-0 max-w-full text-left rounded focus:outline-none focus:ring-2 focus:ring-brand/30 group/title"
                            title="View issue details"
                        >
                            {issue.is_exec && <ExecBadge />}
                            <span className="text-sm text-gray-400 line-through truncate group-hover/title:text-gray-600 group-hover/title:underline">
                                {issue.title || issue.description}
                            </span>
                        </button>
                        <div className="text-xs text-gray-400 mt-1 flex items-center flex-wrap gap-x-1.5 gap-y-1">
                            {metaRow(meta)}
                        </div>
                    </div>

                    {dependencyChip(issue, childTodos, todosExpanded)}

                    {owner && (
                        <span className="flex-shrink-0">
                            <OwnerChip name={formatPerson(owner)} />
                        </span>
                    )}

                    {hoverActions(issue, false)}
                </div>
                {rowPanels(issue, childTodos, todosExpanded)}
            </li>
        );
    };

    const newButton = (
        <button
            type="button"
            onClick={openNew}
            className="flex items-center gap-2 px-3 py-2 bg-brand hover:bg-brand-hover text-white rounded-md text-sm font-medium transition-colors"
        >
            <Plus size={16} /> New Issue
        </button>
    );

    const searchBox = (
        <div className="mb-4" data-tour="eos-issues-search">
            <div className="relative">
                <Search
                    size={14}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none"
                />
                <input
                    type="search"
                    value={search}
                    onChange={e => setSearch(e.target.value)}
                    placeholder={
                        isMeeting
                            ? 'Search issues — including ones already solved'
                            : 'Search issues…'
                    }
                    className="w-full border border-gray-200 rounded-md pl-9 pr-3 py-2 text-sm"
                    aria-label="Search issues"
                />
            </div>
            {isMeeting && searchActive && (
                <p className="mt-1 text-xs text-gray-500">
                    {historyLoading
                        ? 'Loading solved issues…'
                        : historyError
                          ? historyError
                          : `Searching open issues${
                                searchHistory ? ' and this team’s solved history' : ''
                            }.`}
                </p>
            )}
        </div>
    );

    const body = (
        <>
            {loading && <LoadingState label="Loading issues…" />}
            {error && <ErrorState message={error} />}

            {!loading && !error && (scopedIssues.length > 0 || searchActive) && searchBox}

            {!loading && !error && searchActive && searchFiltered.length === 0 && (
                <EmptyState
                    title={`Nothing matches “${search.trim()}”.`}
                    hint={
                        isMeeting
                            ? 'Open issues and this team’s solved history were both searched.'
                            : 'Try fewer words, or clear the search.'
                    }
                />
            )}

            {!loading && !error && !searchActive && teamFiltered.length === 0 && (
                <EmptyState
                    title={
                        teamFilter === 'all'
                            ? 'No issues yet.'
                            : 'No issues for this team.'
                    }
                    hint='Click "New Issue" above to add one.'
                />
            )}

            {!loading && !error && teamFiltered.length > 0 && (
                <>
                    <KpiStrip cols={4}>
                        <KpiCard
                            label="Open issues"
                            value={kpis.openCount}
                            hint="Issues currently open (this team's view)."
                        />
                        <KpiCard
                            label="Solved this week"
                            value={kpis.solvedThisWeek}
                            hint="Issues solved in the last 7 days."
                        />
                        <KpiCard
                            label="Solve rate 90d"
                            value={kpis.solveRate90 === null ? '—' : `${kpis.solveRate90}%`}
                            hint="Share of issues raised in the last 90 days that are now solved"
                        />
                        <KpiCard
                            label="Avg days open"
                            value={
                                kpis.avgDaysOpen === null ? '—' : kpis.avgDaysOpen.toFixed(1)
                            }
                            hint="Average age of the open issues"
                        />
                    </KpiStrip>

                    {isMeeting && recentSolvedError && (
                        <p className="-mt-2 mb-4 text-xs text-amber-700">
                            Couldn’t load this team’s solved issues, so the tiles
                            above count only what’s still open. {recentSolvedError}
                        </p>
                    )}

                    <div className="space-y-6">
                        {/* Open — split into fixed priority buckets (High/Medium/Low),
                            oldest-open first within each. */}
                        <section className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden" data-tour="eos-issues-open">
                            <div className="px-6 py-3 bg-gray-50 border-b border-gray-200 flex items-center gap-2">
                                <h2 className="text-base font-semibold text-gray-900">Open</h2>
                                <span className="text-xs text-gray-400">
                                    {displayOpen.length}
                                </span>
                            </div>
                            {displayOpen.length === 0 ? (
                                <p className="px-6 py-8 text-center text-sm text-gray-500">
                                    {searchActive
                                        ? 'No open issues match — check Solved below.'
                                        : 'No open issues. Nice.'}
                                </p>
                            ) : (
                                PRIORITY_BUCKETS.map(bucket => {
                                    const items = openBuckets[bucket.key];
                                    if (items.length === 0) return null;
                                    return (
                                        <div key={bucket.key}>
                                            <div className="px-6 py-2 bg-gray-50/60 border-b border-gray-100 flex items-center gap-2">
                                                <h3
                                                    className={`text-xs font-semibold uppercase tracking-wide ${bucket.headerCls}`}
                                                >
                                                    {bucket.label}
                                                </h3>
                                                <span className="text-xs text-gray-400">
                                                    {items.length}
                                                </span>
                                            </div>
                                            <ul className="divide-y divide-gray-100">
                                                {items.map(issue => renderOpenRow(issue))}
                                            </ul>
                                        </div>
                                    );
                                })
                            )}
                        </section>

                        {/* Solved — collapsed by default, auto-expanded while a
                            search is running (the whole point of searching here
                            is to find one). The chips pick the lookback window:
                            what did we close in the last 30 / 60 / 90 days. The
                            section renders off `solvedIssues` rather than the
                            windowed list so the chips stay reachable when the
                            nearest window is empty. */}
                        {solvedIssues.length > 0 && (
                            <section className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden opacity-90" data-tour="eos-issues-solved">
                                <div
                                    className={`px-6 py-3 bg-gray-50 flex items-center gap-x-3 gap-y-2 flex-wrap ${
                                        solvedOpen ? 'border-b border-gray-200' : ''
                                    }`}
                                >
                                    <button
                                        type="button"
                                        onClick={() => setShowSolved(v => !v)}
                                        className="flex items-center gap-2 text-left hover:opacity-70 transition-opacity"
                                        aria-expanded={solvedOpen}
                                    >
                                        {solvedOpen ? (
                                            <ChevronDown size={16} className="text-gray-500" />
                                        ) : (
                                            <ChevronRight size={16} className="text-gray-500" />
                                        )}
                                        <h2 className="text-base font-semibold text-gray-900">
                                            Solved ({displaySolved.length})
                                        </h2>
                                        {!searchActive && solvedWindow !== 'all' && (
                                            <span className="text-xs text-gray-500">
                                                last {solvedWindow} days
                                            </span>
                                        )}
                                    </button>
                                    {/* Hidden mid-search: the list isn't windowed
                                        then, so a highlighted chip would lie. */}
                                    {!searchActive && (
                                        <div
                                            className="ml-auto flex items-center gap-1"
                                            role="group"
                                            aria-label="Solved lookback window"
                                        >
                                            {solvedWindowOptions.map(w => {
                                                const active = solvedWindow === w;
                                                return (
                                                    <button
                                                        key={w}
                                                        type="button"
                                                        onClick={() => {
                                                            setSolvedWindow(w);
                                                            // Picking a window means
                                                            // "show me" — open the
                                                            // drawer if it's shut.
                                                            setShowSolved(true);
                                                        }}
                                                        aria-pressed={active}
                                                        aria-label={
                                                            w === 'all'
                                                                ? 'All solved issues'
                                                                : `Solved in the last ${w} days`
                                                        }
                                                        className={`px-2.5 py-0.5 rounded-full text-xs font-medium border transition-colors ${
                                                            active
                                                                ? 'bg-brand text-white border-brand'
                                                                : 'bg-white text-gray-600 border-gray-200 hover:border-brand hover:text-brand'
                                                        }`}
                                                    >
                                                        {w === 'all' ? 'All' : `${w}d`}
                                                    </button>
                                                );
                                            })}
                                        </div>
                                    )}
                                </div>
                                {solvedOpen &&
                                    (displaySolved.length === 0 ? (
                                        <p className="px-6 py-8 text-center text-sm text-gray-500">
                                            {searchActive
                                                ? 'No solved issues match — try fewer words.'
                                                : `Nothing solved in the last ${solvedWindow} days.`}
                                        </p>
                                    ) : (
                                        <ul className="divide-y divide-gray-100">
                                            {displaySolved.map(issue =>
                                                renderClosedRow(issue, 'solved'),
                                            )}
                                        </ul>
                                    ))}
                            </section>
                        )}

                        {/* Archived — collapsed by default; only when any exist so no
                            issue silently disappears. */}
                        {displayArchived.length > 0 && (
                            <section className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden opacity-90">
                                <button
                                    type="button"
                                    onClick={() => setShowArchived(v => !v)}
                                    className={`w-full px-6 py-3 bg-gray-50 flex items-center gap-2 text-left hover:bg-gray-100 transition-colors ${
                                        showArchived || searchActive
                                            ? 'border-b border-gray-200'
                                            : ''
                                    }`}
                                    aria-expanded={showArchived || searchActive}
                                >
                                    {/* Auto-expanded while searching: a match
                                        sitting behind a collapsed toggle is the
                                        exact thing the search box exists to
                                        stop happening. */}
                                    {showArchived || searchActive ? (
                                        <ChevronDown size={16} className="text-gray-500" />
                                    ) : (
                                        <ChevronRight size={16} className="text-gray-500" />
                                    )}
                                    <h2 className="text-base font-semibold text-gray-900">
                                        Archived ({displayArchived.length})
                                    </h2>
                                </button>
                                {(showArchived || searchActive) && (
                                    <ul className="divide-y divide-gray-100">
                                        {displayArchived.map(issue =>
                                            renderClosedRow(issue, 'archived'),
                                        )}
                                    </ul>
                                )}
                            </section>
                        )}
                    </div>
                </>
            )}
        </>
    );

    const modal = (
        <Modal
            isOpen={modalOpen}
            onClose={closeModal}
            title={editing ? 'Edit Issue' : 'New Issue'}
        >
            <form onSubmit={onSubmit} className="space-y-4">
                <Field label="Title">
                    <input
                        required
                        type="text"
                        value={form.title}
                        onChange={e => setForm({ ...form, title: e.target.value })}
                        className={inputCls}
                        placeholder="Short headline — shows on the list"
                        maxLength={120}
                        autoFocus
                    />
                </Field>
                <Field label="Description">
                    <textarea
                        required
                        value={form.description}
                        onChange={e => setForm({ ...form, description: e.target.value })}
                        className={`${inputCls} min-h-[100px]`}
                        placeholder="Add the full detail — shown when the issue is opened…"
                    />
                </Field>
                <Field label="Priority">
                    <select
                        value={form.priority}
                        onChange={e => setForm({ ...form, priority: e.target.value as IssuePriority })}
                        className={inputCls}
                    >
                        <option value="low">Low</option>
                        <option value="medium">Medium</option>
                        <option value="high">High</option>
                    </select>
                </Field>
                {/* Which team's L10 this issue belongs to. Hidden when
                    the caller has a single scope (it's used silently) and in
                    meeting mode (the team is locked to the meeting). */}
                {!editing && !meetingContext && teamScopes.length > 1 && (
                    <Field label="Team (shows in this team's L10)">
                        <select
                            value={form.team}
                            onChange={e => setForm({ ...form, team: e.target.value })}
                            className={inputCls}
                        >
                            {teamScopes.map(s => (
                                <option key={s.value} value={s.value}>
                                    {s.label}
                                </option>
                            ))}
                        </select>
                    </Field>
                )}
                {editing && (
                    <Field label="Status">
                        <select
                            value={form.status}
                            onChange={e => setForm({ ...form, status: e.target.value as IssueStatus })}
                            className={inputCls}
                        >
                            <option value="open">Open</option>
                            <option value="solved">Solved</option>
                            <option value="archived">Archived</option>
                        </select>
                    </Field>
                )}
                <SubmitButton disabled={submitting} onCancel={closeModal}>
                    {editing ? 'Save changes' : 'Create issue'}
                </SubmitButton>
            </form>
        </Modal>
    );

    // Scrum: "turn issue into a to-do" — creates a child to-do linked to the
    // issue via POST /api/eos/issues/{id}/todos.
    const todoModal = (
        <Modal
            isOpen={todoModalIssue !== null}
            onClose={closeTodoModal}
            title="Add to-do for this issue"
        >
            <form onSubmit={onSubmitTodo} className="space-y-4">
                <Field label="To-do">
                    <input
                        required
                        type="text"
                        maxLength={TODO_MAX_LEN}
                        value={todoForm.description}
                        onChange={e => setTodoForm({ ...todoForm, description: e.target.value })}
                        className={inputCls}
                        autoFocus
                    />
                </Field>
                <Field label="Owner">
                    <select
                        value={todoForm.owner_email}
                        onChange={e => setTodoForm({ ...todoForm, owner_email: e.target.value })}
                        className={inputCls}
                    >
                        <option value="">Issue owner (default)</option>
                        {people.map(p => (
                            <option key={p.id} value={p.email}>
                                {p.first_name} {p.last_name} — {p.email}
                            </option>
                        ))}
                    </select>
                </Field>
                <Field label="Due date">
                    <input
                        type="date"
                        value={todoForm.due_date}
                        onChange={e => setTodoForm({ ...todoForm, due_date: e.target.value })}
                        className={inputCls}
                    />
                </Field>
                <SubmitButton disabled={todoSubmitting} onCancel={closeTodoModal}>
                    Add to-do
                </SubmitButton>
            </form>
        </Modal>
    );

    // Read-only "full issue" dialog opened from a row's title. Resolved each
    // render against `scopedIssues` — the live `issues` prop plus whatever the
    // meeting fetched — so the L10 poll's updates flow through (and the modal
    // closes itself if the issue is deleted), AND a solved row still opens: in
    // a meeting those rows exist only in the fetched history, so resolving
    // against `issues` alone opened an empty dialog. `scopedIssues` keeps the
    // live row on any collision, so nothing goes stale.
    const detailIssue = detailIssueId
        ? (scopedIssues.find(i => i.id === detailIssueId) ?? null)
        : null;
    const detailModal = (
        <IssueDetailModal
            issue={detailIssue}
            people={people}
            childTodos={detailIssue ? (childTodosByIssue.get(detailIssue.id) ?? []) : []}
            onClose={() => setDetailIssueId(null)}
        />
    );

    if (isMeeting) {
        return (
            <div>
                <div className="flex items-center justify-between mb-4">
                    <div>
                        {/* Named for the agenda step that hosts it. The step is
                            called IDS and the panel used to be called Issues,
                            which is how items intended for IDS ended up parked
                            under the Headlines tab instead. */}
                        <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
                            <AlertCircle size={18} /> IDS — Issues
                        </h2>
                        <p className="text-sm text-gray-500">Identify, Discuss, Solve</p>
                    </div>
                    {newButton}
                </div>
                {body}
                {modal}
                {todoModal}
                {detailModal}
            </div>
        );
    }

    return (
        <PageContainer>
            <EosPageHeader
                icon={<AlertCircle size={20} />}
                title="Issues"
                subtitle="Identify, Discuss, Solve"
                right={
                    <>
                        <TeamFilter
                            scopes={teamScopes}
                            value={teamFilter}
                            onChange={setTeamFilter}
                        />
                        {newButton}
                    </>
                }
            />
            {body}
            {modal}
            {todoModal}
            {detailModal}
        </PageContainer>
    );
};
