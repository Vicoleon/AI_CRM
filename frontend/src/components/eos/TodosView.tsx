import React, { useEffect, useMemo, useState } from 'react';
import {
    ListTodo,
    Calendar,
    CheckSquare,
    Square,
    Plus,
    Edit2,
    Trash2,
    ChevronDown,
    ChevronRight,
    GitBranch,
    MessageSquare,
} from 'lucide-react';
import client from '../../api/client';
import { errorMessage } from '../../utils/errors';
import {
    EmptyState,
    ErrorState,
    EosPageHeader,
    ExecBadge,
    KpiCard,
    KpiStrip,
    LoadingState,
    PageContainer,
    StatusBadge,
    TeamFilter,
} from '../../pages/eos/_shared';
import {
    TODO_MAX_LEN,
    formatPerson,
    matchesTeamFilter,
    scopeToTeam,
    useTeamFilter,
} from '../../pages/eos/_utils';
import { ClampedText } from '../ClampedText';
import type { PersonLite, TeamScope } from '../../pages/eos/_utils';
import { Modal, Field, inputCls, SubmitButton } from './Modal';
import { NotesThread } from './NotesThread';
import { useAuth } from '../../context/AuthContext';
import type { MeetingContext } from './eosViewTypes';

export type TodoStatus = 'pending' | 'done';

export interface Todo {
    id: string;
    description: string;
    due_date: string | null;
    status: TodoStatus;
    owner_id: string;
    source_issue_id: string | null; // set when spun off from an Issue (scrum story→task)
    department_name: string | null; // Team scope (for manager delete-permission)
    is_exec: boolean; // Exec/Leadership-team to-do (exec-only, server-enforced)
    created_by: string | null; // Who created it (delete-permission check)
}

/** Reconciliation events the host applies to its own `todos` state. In the L10
 * host this ALSO bumps the poll-suppression window so the 2s live poll doesn't
 * clobber the optimistic edit. */
export type TodoChange =
    | { type: 'upsert'; todo: Todo }
    | { type: 'remove'; id: string }
    | { type: 'set-status'; id: string; status: TodoStatus };

interface FormState {
    description: string;
    due_date: string; // YYYY-MM-DD or ''
    owner_email: string;
    team: string; // Meeting-scope value (department name or __exec__)
}

const EMPTY_FORM: FormState = { description: '', due_date: '', owner_email: '', team: '' };

const compareDueDate = (a: Todo, b: Todo) => {
    const at = a.due_date ? new Date(a.due_date).getTime() : Number.POSITIVE_INFINITY;
    const bt = b.due_date ? new Date(b.due_date).getTime() : Number.POSITIVE_INFINITY;
    return at - bt;
};

// Classic EOS "To-Do List": one list ordered by due date, split into
// due-date buckets so overdue work reads first. "today" is local midnight and
// each todo is bucketed by its LOCAL calendar day (matching how the row shows
// the date via toLocaleDateString), so a bucket never disagrees with the
// visible due date across timezones.
type BucketKey = 'overdue' | 'week' | 'later' | 'none';

const BUCKET_ORDER: BucketKey[] = ['overdue', 'week', 'later', 'none'];

const BUCKET_META: Record<BucketKey, { label: string; headerCls: string; dueCls: string }> = {
    overdue: { label: 'Overdue', headerCls: 'text-red-700', dueCls: 'text-red-600 font-medium' },
    week: { label: 'This week', headerCls: 'text-amber-700', dueCls: 'text-amber-600' },
    later: { label: 'Later', headerCls: 'text-gray-600', dueCls: 'text-gray-500' },
    none: { label: 'No due date', headerCls: 'text-gray-500', dueCls: 'text-gray-400' },
};

/** Local-midnight timestamp for a todo's due day (clock time ignored).
 *
 *  `new Date`, deliberately — NOT `parseInstant`. A due date is a CALENDAR
 *  DATE that happens to be stored as a midnight timestamp: the picker writes
 *  `new Date('2026-07-13').toISOString()`, so the row comes back as
 *  `2026-07-13T00:00:00` and has to be read as LOCAL midnight to stay Jul 13.
 *  Reading it as UTC would show the 12th to everyone west of Greenwich. */
const dueDayMs = (iso: string): number => {
    const d = new Date(iso);
    d.setHours(0, 0, 0, 0);
    return d.getTime();
};

/** How the due date renders on a row — the actual date, or a muted placeholder. */
const dueLabel = (t: Todo): string =>
    t.due_date ? new Date(t.due_date).toLocaleDateString() : 'No due date';

interface TodosViewProps {
    /** 'page' renders full-page chrome (PageContainer + header + team filter);
     * 'meeting' renders a compact header for embedding in an L10 segment. */
    mode: 'page' | 'meeting';
    todos: Todo[];
    people: PersonLite[];
    /** Team scopes for the page filter + the create-form team select. Omit in
     * meeting mode (the team is locked to the meeting). */
    teamScopes?: TeamScope[];
    /** Present only in meeting mode: stamps created to-dos with the meeting's
     * team + links them to this L10 so they resurface in future meetings. */
    meetingContext?: MeetingContext;
    loading?: boolean;
    error?: string | null;
    /** Host reconciles its own `todos` state (and, in the L10, suppresses the
     * live poll) in response to these events. */
    onChange: (change: TodoChange) => void;
}

/**
 * The shared To-Dos view — the single source of truth for the standalone
 * To-Dos page and the in-meeting L10 "To-Do List" segment. Fully controlled:
 * the host owns fetching + the `todos` array; this component owns rendering,
 * derivations, the create/edit modal, and the write calls, reporting results
 * back via `onChange`.
 */
export const TodosView: React.FC<TodosViewProps> = ({
    mode,
    todos,
    people,
    teamScopes = [],
    meetingContext,
    loading = false,
    error = null,
    onChange,
}) => {
    const { isAdmin, isExec, managesDept, user } = useAuth();
    // You can delete your own to-dos; admins / exec can delete any.
    // A Team Manager can delete any to-do on a team they lead.
    const canDelete = (todo: Todo): boolean =>
        isAdmin ||
        isExec ||
        managesDept(todo.department_name) ||
        (!!todo.created_by &&
            !!user?.email &&
            todo.created_by.toLowerCase() === user.email.toLowerCase());

    const isMeeting = mode === 'meeting';

    const [modalOpen, setModalOpen] = useState(false);
    const [editing, setEditing] = useState<Todo | null>(null);
    const [form, setForm] = useState<FormState>(EMPTY_FORM);
    const [submitting, setSubmitting] = useState(false);
    // Team filter for the list: 'all' | '__exec__' | department name. Meeting
    // mode leaves it on 'all' (data is already scoped to the meeting's team).
    const [teamFilter, setTeamFilter] = useTeamFilter(teamScopes, 'eos.teamFilter.todos');
    const [showCompleted, setShowCompleted] = useState(false);
    // Which to-do's notes thread is expanded, and how many notes each to-do
    // has. The counts come from one batched call so a row can advertise its
    // thread ("2 notes") without a fetch per row; the thread itself reports
    // back through `onCountChange`, keeping the chip honest after an add.
    const [notesOpenId, setNotesOpenId] = useState<string | null>(null);
    const [noteCounts, setNoteCounts] = useState<Record<string, number>>({});

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const res = await client.get<Record<string, number>>(
                    '/api/eos/todos/note-counts',
                );
                if (!cancelled) setNoteCounts(res.data ?? {});
            } catch {
                // Counts are decorative — the thread is the source of truth, so
                // a failure here just means no chips until a row is opened.
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    const peopleById = useMemo(() => new Map(people.map(p => [p.id, p])), [people]);

    const teamFiltered = useMemo(
        () => todos.filter(t => matchesTeamFilter(teamFilter, t)),
        [todos, teamFilter],
    );
    const pending = useMemo(
        () => teamFiltered.filter(t => t.status === 'pending').sort(compareDueDate),
        [teamFiltered],
    );
    const done = useMemo(
        () => teamFiltered.filter(t => t.status === 'done').sort(compareDueDate),
        [teamFiltered],
    );

    // Group the (already due-date-sorted) open to-dos into ordered buckets;
    // iterating the sorted list keeps each bucket ascending by due date.
    const openBuckets = useMemo(() => {
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const todayMs = today.getTime();
        const weekEndMs = todayMs + 7 * 24 * 60 * 60 * 1000; // today .. +7 days
        const groups: Record<BucketKey, Todo[]> = { overdue: [], week: [], later: [], none: [] };
        for (const t of pending) {
            if (!t.due_date) {
                groups.none.push(t);
                continue;
            }
            const ms = dueDayMs(t.due_date);
            if (ms < todayMs) groups.overdue.push(t);
            else if (ms <= weekEndMs) groups.week.push(t);
            else groups.later.push(t);
        }
        return groups;
    }, [pending]);

    const openNew = () => {
        setEditing(null);
        setForm({
            ...EMPTY_FORM,
            owner_email: people[0]?.email ?? '',
            team: teamScopes[0]?.value ?? '',
        });
        setModalOpen(true);
    };

    const openEdit = (todo: Todo) => {
        setEditing(todo);
        const owner = peopleById.get(todo.owner_id);
        setForm({
            description: todo.description,
            due_date: todo.due_date ? todo.due_date.split('T')[0] : '',
            owner_email: owner?.email ?? '',
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
            const payload: Record<string, unknown> = {
                description: form.description,
                due_date: form.due_date ? new Date(form.due_date).toISOString() : null,
            };

            if (editing) {
                const owner = people.find(p => p.email === form.owner_email);
                if (owner) payload.owner_id = owner.id;
                const res = await client.patch<Todo>(`/api/eos/todos/${editing.id}`, payload);
                onChange({ type: 'upsert', todo: res.data });
            } else {
                payload.owner_email = form.owner_email;
                if (meetingContext) {
                    // Stamp the meeting's team + link to this L10 so the
                    // to-do resurfaces in the team's future meetings.
                    Object.assign(payload, meetingContext.team);
                    payload.l10_meeting_id = meetingContext.l10MeetingId;
                } else {
                    // Tag the to-do with its team so it shows in that team's L10.
                    Object.assign(payload, scopeToTeam(form.team));
                }
                const res = await client.post<Todo>('/api/eos/todos/', payload);
                onChange({ type: 'upsert', todo: res.data });
            }
            closeModal();
        } catch (err) {
            console.error('Failed to save todo', err);
            alert(errorMessage(err, 'Failed to save To-Do. Please try again.'));
        } finally {
            setSubmitting(false);
        }
    };

    /**
     * Toggle pending ↔ done with optimistic UI: flip via `onChange` first,
     * revert if the PATCH fails. Mirrors the app's other inline-toggle interactions.
     */
    const toggleStatus = async (todo: Todo) => {
        const newStatus: TodoStatus = todo.status === 'done' ? 'pending' : 'done';
        onChange({ type: 'set-status', id: todo.id, status: newStatus });
        try {
            await client.patch(`/api/eos/todos/${todo.id}`, { status: newStatus });
        } catch (err) {
            console.error('Status toggle failed', err);
            onChange({ type: 'set-status', id: todo.id, status: todo.status });
        }
    };

    const onDelete = async (todo: Todo) => {
        if (!confirm(`Delete to-do: "${todo.description}"?`)) return;
        try {
            await client.delete(`/api/eos/todos/${todo.id}`);
            onChange({ type: 'remove', id: todo.id });
        } catch (err) {
            console.error('Delete failed', err);
            alert('Failed to delete To-Do.');
        }
    };

    const toggleNotes = (id: string) => setNotesOpenId(prev => (prev === id ? null : id));

    /** Meta-line chip advertising an existing notes thread; opens/closes it.
     * Only rendered when the to-do actually has notes, so a bare row stays
     * clean — adding the first note goes through the hover action below. */
    const notesChip = (todo: Todo) => {
        const count = noteCounts[todo.id] ?? 0;
        if (count === 0) return null;
        return (
            <button
                type="button"
                onClick={() => toggleNotes(todo.id)}
                className={`flex items-center gap-1 transition-colors ${
                    notesOpenId === todo.id
                        ? 'text-brand font-medium'
                        : 'text-gray-500 hover:text-brand'
                }`}
                aria-expanded={notesOpenId === todo.id}
                title="Progress notes"
            >
                <MessageSquare size={12} />
                {count} {count === 1 ? 'note' : 'notes'}
            </button>
        );
    };

    /** Row actions fade in on hover — but stay put while this row's notes are
     * open, so the toggle that collapses the thread doesn't vanish under the
     * cursor when the mouse leaves the row. */
    const rowActionsCls = (todo: Todo) =>
        `flex items-center gap-1 transition-opacity ${
            notesOpenId === todo.id
                ? 'opacity-100'
                : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'
        }`;

    /** Hover action that opens the thread — the way a first note gets added. */
    const notesButton = (todo: Todo) => (
        <button
            type="button"
            onClick={() => toggleNotes(todo.id)}
            className={`p-1.5 rounded hover:bg-gray-100 ${
                notesOpenId === todo.id ? 'text-brand' : 'text-gray-400 hover:text-gray-700'
            }`}
            aria-label="Notes"
            title="Progress notes"
        >
            <MessageSquare size={14} />
        </button>
    );

    /** The expanded thread, rendered under its row. */
    const notesPanel = (todo: Todo) =>
        notesOpenId === todo.id && (
            <div className="border-t border-gray-100 px-6 py-3 bg-gray-50/60">
                <NotesThread
                    basePath={`/api/eos/todos/${todo.id}/notes`}
                    emptyHint="No notes yet. Capture progress or what's blocking this to-do."
                    placeholder="Add a progress note…"
                    onCountChange={n =>
                        setNoteCounts(prev => (prev[todo.id] === n ? prev : { ...prev, [todo.id]: n }))
                    }
                />
            </div>
        );

    const newButton = (
        <button
            type="button"
            onClick={openNew}
            className="flex items-center gap-2 px-3 py-2 bg-brand hover:bg-brand-hover text-white rounded-md text-sm font-medium transition-colors"
            data-tour="eos-todos-new"
        >
            <Plus size={16} /> New To-Do
        </button>
    );

    const body = (
        <>
            {loading && <LoadingState label="Loading to-dos…" />}
            {error && <ErrorState message={error} />}

            {!loading && !error && todos.length === 0 && (
                <EmptyState
                    title="No to-dos yet."
                    hint='Click "New To-Do" above to add one.'
                />
            )}

            {!loading && !error && todos.length > 0 && (
                <div className="space-y-6">
                    {/* KPI summary — current values only (no trend deltas), computed
                        over the team-filtered to-dos so all three EOS pages open
                        with the same rhythm. */}
                    <KpiStrip cols={4}>
                        <KpiCard
                            label="Open"
                            value={pending.length}
                            hint="Open to-dos in view"
                        />
                        <KpiCard
                            label="Due this week"
                            value={openBuckets.week.length}
                            hint="Open to-dos due within the next 7 days"
                        />
                        <KpiCard
                            label="Overdue"
                            value={openBuckets.overdue.length}
                            valueClass={openBuckets.overdue.length > 0 ? 'text-red-700' : undefined}
                            hint="Open to-dos past their due date"
                        />
                        <KpiCard
                            label="Completed"
                            value={done.length}
                            hint="Completed to-dos in view"
                        />
                    </KpiStrip>

                    {/* Open to-dos — one list ordered by due date, split into
                        due-date buckets so overdue work reads first (classic EOS). */}
                    <section className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden" data-tour="eos-todos-open">
                        <div className="px-6 py-3 bg-gray-50 border-b border-gray-200">
                            <h2 className="text-base font-semibold text-gray-900">Open</h2>
                        </div>
                        {pending.length === 0 ? (
                            <p className="px-6 py-8 text-center text-sm text-gray-500">
                                No open to-dos. Nice.
                            </p>
                        ) : (
                            BUCKET_ORDER.map(key => {
                                const items = openBuckets[key];
                                if (items.length === 0) return null;
                                const meta = BUCKET_META[key];
                                return (
                                    <div key={key}>
                                        <div className="px-6 py-2 bg-gray-50/60 border-b border-gray-100 flex items-center gap-2">
                                            <h3
                                                className={`text-xs font-semibold uppercase tracking-wide ${meta.headerCls}`}
                                            >
                                                {meta.label}
                                            </h3>
                                            <span className="text-xs text-gray-400">{items.length}</span>
                                        </div>
                                        <ul className="divide-y divide-gray-100">
                                            {items.map(todo => (
                                                <li key={todo.id} className="group">
                                                    <div className="px-6 py-3.5 flex items-center gap-4">
                                                        <button
                                                            type="button"
                                                            onClick={() => toggleStatus(todo)}
                                                            className="text-gray-300 hover:text-brand transition-colors flex-shrink-0"
                                                            aria-label="Mark done"
                                                        >
                                                            <Square size={18} />
                                                        </button>
                                                        <div className="flex-1 min-w-0">
                                                            <p className="text-sm font-medium text-gray-900 flex items-start gap-2">
                                                                {todo.is_exec && <ExecBadge />}
                                                                {/* ClampedText, not `truncate`:
                                                                    it allows two lines and attaches
                                                                    a tooltip only when the text is
                                                                    actually cut, so a pre-cap to-do
                                                                    is still readable. */}
                                                                <ClampedText
                                                                    text={todo.description}
                                                                    lines={2}
                                                                    className="min-w-0"
                                                                />
                                                            </p>
                                                            <div className="flex items-center flex-wrap gap-x-4 gap-y-1 mt-1 text-xs">
                                                                {!todo.is_exec && todo.department_name && (
                                                                    <span className="text-gray-400">
                                                                        {todo.department_name}
                                                                    </span>
                                                                )}
                                                                <span className="text-gray-500">
                                                                    Owner:{' '}
                                                                    <span className="text-brand font-medium">
                                                                        {formatPerson(peopleById.get(todo.owner_id))}
                                                                    </span>
                                                                </span>
                                                                <span className={`flex items-center gap-1 ${meta.dueCls}`}>
                                                                    {todo.due_date && <Calendar size={12} />}
                                                                    {dueLabel(todo)}
                                                                </span>
                                                                {todo.source_issue_id && (
                                                                    <span className="flex items-center gap-1 text-gray-400">
                                                                        <GitBranch size={12} /> from issue
                                                                    </span>
                                                                )}
                                                                {key === 'overdue' && (
                                                                    <StatusBadge value="Overdue" tone="red" />
                                                                )}
                                                                {notesChip(todo)}
                                                            </div>
                                                        </div>
                                                        <div className={rowActionsCls(todo)}>
                                                            {notesButton(todo)}
                                                            <button
                                                                type="button"
                                                                onClick={() => openEdit(todo)}
                                                                className="p-1.5 text-gray-400 hover:text-gray-700 hover:bg-gray-100 rounded"
                                                                aria-label="Edit"
                                                            >
                                                                <Edit2 size={14} />
                                                            </button>
                                                            {canDelete(todo) && (
                                                                <button
                                                                    type="button"
                                                                    onClick={() => onDelete(todo)}
                                                                    className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded"
                                                                    aria-label="Delete"
                                                                >
                                                                    <Trash2 size={14} />
                                                                </button>
                                                            )}
                                                        </div>
                                                    </div>
                                                    {notesPanel(todo)}
                                                </li>
                                            ))}
                                        </ul>
                                    </div>
                                );
                            })
                        )}
                    </section>

                    {/* Completed — collapsible, collapsed by default. */}
                    {done.length > 0 && (
                        <section className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden opacity-90">
                            <button
                                type="button"
                                onClick={() => setShowCompleted(v => !v)}
                                className={`w-full px-6 py-3 bg-gray-50 flex items-center gap-2 text-left hover:bg-gray-100 transition-colors ${
                                    showCompleted ? 'border-b border-gray-200' : ''
                                }`}
                                aria-expanded={showCompleted}
                            >
                                {showCompleted ? (
                                    <ChevronDown size={16} className="text-gray-500" />
                                ) : (
                                    <ChevronRight size={16} className="text-gray-500" />
                                )}
                                <h2 className="text-base font-semibold text-gray-900">
                                    Completed ({done.length})
                                </h2>
                            </button>
                            {showCompleted && (
                                <ul className="divide-y divide-gray-100">
                                    {done.map(todo => (
                                        <li key={todo.id} className="group">
                                            <div className="px-6 py-3 flex items-center gap-4">
                                                <button
                                                    type="button"
                                                    onClick={() => toggleStatus(todo)}
                                                    className="text-green-500 hover:text-green-700 transition-colors flex-shrink-0"
                                                    aria-label="Mark pending"
                                                >
                                                    <CheckSquare size={18} />
                                                </button>
                                                <div className="flex-1 min-w-0">
                                                    <p className="text-sm text-gray-400 line-through">
                                                        <ClampedText text={todo.description} lines={2} />
                                                    </p>
                                                    <div className="flex items-center flex-wrap gap-x-4 gap-y-1 mt-1 text-xs text-gray-400">
                                                        <span className="flex items-center gap-1">
                                                            {todo.due_date && <Calendar size={12} />}
                                                            {dueLabel(todo)}
                                                        </span>
                                                        {todo.source_issue_id && (
                                                            <span className="flex items-center gap-1">
                                                                <GitBranch size={12} /> from issue
                                                            </span>
                                                        )}
                                                        {notesChip(todo)}
                                                    </div>
                                                </div>
                                                <div className={rowActionsCls(todo)}>
                                                    {notesButton(todo)}
                                                    {canDelete(todo) && (
                                                        <button
                                                            type="button"
                                                            onClick={() => onDelete(todo)}
                                                            className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded"
                                                            aria-label="Delete"
                                                        >
                                                            <Trash2 size={14} />
                                                        </button>
                                                    )}
                                                </div>
                                            </div>
                                            {notesPanel(todo)}
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </section>
                    )}
                </div>
            )}
        </>
    );

    const modal = (
        <Modal
            isOpen={modalOpen}
            onClose={closeModal}
            title={editing ? 'Edit To-Do' : 'New To-Do'}
        >
            <form onSubmit={onSubmit} className="space-y-4">
                {/* Labelled "To-do", not "Description": a To-Do has exactly one
                    text field, and calling it a description invited paragraphs
                    into a single-line list row. Capped — see TODO_MAX_LEN. */}
                <Field label="To-do">
                    <input
                        required
                        type="text"
                        maxLength={TODO_MAX_LEN}
                        value={form.description}
                        onChange={e => setForm({ ...form, description: e.target.value })}
                        className={inputCls}
                        placeholder="One small task, e.g. “Send the kickoff deck to the client”"
                        autoFocus
                    />
                    <p className="mt-1 text-xs text-gray-400">
                        {form.description.length}/{TODO_MAX_LEN} — longer than a line? Make it
                        an Issue instead.
                    </p>
                </Field>
                <Field label="Due date">
                    <input
                        type="date"
                        value={form.due_date}
                        onChange={e => setForm({ ...form, due_date: e.target.value })}
                        className={inputCls}
                    />
                </Field>
                <Field label="Owner">
                    <select
                        required
                        value={form.owner_email}
                        onChange={e => setForm({ ...form, owner_email: e.target.value })}
                        className={inputCls}
                    >
                        <option value="" disabled>Select owner…</option>
                        {people.map(p => (
                            <option key={p.id} value={p.email}>
                                {p.first_name} {p.last_name} — {p.email}
                            </option>
                        ))}
                    </select>
                </Field>
                {/* Which team's L10 this to-do belongs to. Meeting mode
                    locks the team, so the picker only shows on the page. */}
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
                <SubmitButton disabled={submitting} onCancel={closeModal}>
                    {editing ? 'Save changes' : 'Create to-do'}
                </SubmitButton>
            </form>
        </Modal>
    );

    if (isMeeting) {
        return (
            <div>
                <div className="flex items-center justify-between mb-4">
                    <div>
                        <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
                            <ListTodo size={18} /> To-Dos
                        </h2>
                        <p className="text-sm text-gray-500">Action items (7-day tasks)</p>
                    </div>
                    {newButton}
                </div>
                {body}
                {modal}
            </div>
        );
    }

    return (
        <PageContainer>
            <EosPageHeader
                icon={<ListTodo size={20} />}
                title="To-Dos"
                subtitle="Action items (7-day tasks)"
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
        </PageContainer>
    );
};
