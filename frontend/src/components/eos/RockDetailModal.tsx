import React, { useEffect, useRef, useState } from 'react';
import {
    CheckCircle2,
    Clock,
    AlertTriangle,
    AlertCircle,
    CalendarArrowUp,
} from 'lucide-react';
import client from '../../api/client';
import { errorMessage } from '../../utils/errors';
import { Modal, Field, inputCls } from './Modal';
import { toast } from './Toast';
import type { PersonLite } from '../../pages/eos/_utils';
import {
    computeMilestoneProgress,
    formatISODate,
    isMilestoneOverdue,
    todayISO,
} from '../../pages/eos/_utils';

// ─── Types shared by the Rocks page and the in-meeting L10 session ──────────

export type RockStatus = 'on_track' | 'off_track' | 'done';

export interface RockMilestone {
    // Stable server-assigned UUID for a persisted milestone row (absent on a
    // brand-new, not-yet-saved milestone). The whole-array PATCH reconciler
    // matches BY this id: an entry with a known id updates that row in place;
    // an entry without one is inserted as a fresh row. Preserving it through
    // the edit/toggle flow keeps a milestone's UUID stable across saves — which
    // its AI-Priorities card is keyed on.
    id?: string;
    description: string;
    completed: boolean;
    // Optional ISO "YYYY-MM-DD" local due date (null when unset). Progress is
    // a simple completed/total count now — the old `weight` field is retired.
    due_date?: string | null;
    // Server-assigned ordering index; carried back through PATCH so the row's
    // position is preserved rather than reshuffled.
    sort_order?: number;
}

// Weekly update history on a rock. Mirrors RockWeeklyUpdateResponse
// from the backend.
interface RockWeeklyUpdate {
    id: string;
    rock_id: string;
    status: RockStatus;
    percent_complete: number;
    comment: string | null;
    update_date: string; // YYYY-MM-DD
    created_at: string;
    created_by: string | null;
}

// Form state for the "log weekly update" form. status and
// percent_complete are REQUIRED at submission time; the empty defaults here
// are just for the controlled inputs.
interface UpdateFormState {
    status: RockStatus;
    percent_complete: string; // string-bound so the input can be blank
    comment: string;
}

const EMPTY_UPDATE_FORM: UpdateFormState = {
    status: 'on_track',
    percent_complete: '',
    comment: '',
};

type IDSPriority = 'low' | 'medium' | 'high';

const STATUS_LABELS: Record<RockStatus, string> = {
    on_track: 'On Track',
    off_track: 'Off Track',
    done: 'Done',
};

const STATUS_ICON: Record<RockStatus, React.ComponentType<{ size?: number }>> = {
    on_track: Clock,
    off_track: AlertTriangle,
    done: CheckCircle2,
};

interface RockDetailModalProps {
    isOpen: boolean;
    /** The rock being viewed. Null renders nothing (modal stays closed). */
    rockId: string | null;
    description: string;
    /** Pre-resolved owner display name — each page formats it its own way. */
    ownerLabel: string;
    status: RockStatus;
    milestones: RockMilestone[] | null;
    /** Full people roster, used to resolve weekly-update authors by email. */
    people: PersonLite[];
    /**
     * May this viewer write to the rock? The server's ``can_edit`` verdict
     * (``can_mutate``), passed down by the host list. False hides the
     * milestone toggle and the weekly-update form rather than letting someone
     * type an update the save will refuse with a 403. Defaults true.
     */
    canEdit?: boolean;
    onClose: () => void;
    /**
     * Bubble status / milestone changes up so the parent list reflects them.
     * The in-meeting caller also uses this to protect the optimistic edit
     * from the 2-second live-session poll.
     */
    onRockChanged: (
        rockId: string,
        patch: { status?: RockStatus; milestones?: RockMilestone[] },
    ) => void;
    /**
     * Fired after "Add to IDS" successfully creates an issue, carrying the
     * server's created-issue response. The in-meeting L10 caller uses it to
     * drop the new issue straight into the IDS segment; the standalone Rocks
     * page omits it (the Issues page is separate).
     */
    onIssueCreated?: (issue: unknown) => void;
    /**
     * Offers "Carry over to next quarter" for a rock still in play: the host
     * closes this dialog and opens the rock's editable next-quarter draft.
     * Only the standalone Rocks page passes it — the L10 Rock Review is
     * scoped to the current quarter, so its dialog is unchanged.
     */
    onCarryOver?: () => void;
    /** Quarter this rock was already carried over to ("Q4 2026"); shown in
     *  place of the Carry-over button, since a rock carries over once. */
    carriedToLabel?: string | null;
}

/**
 * The full rock detail dialog — clickable milestones, "Add to
 * IDS", a required-fields weekly-update form, and the update history table.
 *
 * Extracted from the standalone Rocks page so the L10 live session can render
 * the exact same view *in place* instead of navigating away to /eos/rocks
 * (which yanked the user out of the meeting). Single source of truth: both
 * the Rocks page and the in-meeting Rock Review use this component.
 */
export const RockDetailModal: React.FC<RockDetailModalProps> = ({
    isOpen,
    rockId,
    description,
    ownerLabel,
    status,
    milestones,
    people,
    canEdit = true,
    onClose,
    onRockChanged,
    onIssueCreated,
    onCarryOver,
    carriedToLabel = null,
}) => {
    // Local, authoritative-while-open copies of the mutable rock fields.
    // Seeded from props when a rock opens; NOT reseeded on prop changes so a
    // background refresh can't clobber the user's in-modal edits.
    const [liveStatus, setLiveStatus] = useState<RockStatus>(status);
    const [liveMilestones, setLiveMilestones] = useState<RockMilestone[] | null>(
        milestones,
    );

    const [updates, setUpdates] = useState<RockWeeklyUpdate[]>([]);
    const [updatesLoading, setUpdatesLoading] = useState(false);
    const [updatesError, setUpdatesError] = useState<string | null>(null);
    const [updateForm, setUpdateForm] = useState<UpdateFormState>(EMPTY_UPDATE_FORM);
    const [updateSubmitting, setUpdateSubmitting] = useState(false);
    // The update just logged — highlights its History row briefly so the
    // save is visible even when the table is off-screen.
    const [justLoggedId, setJustLoggedId] = useState<string | null>(null);
    const historyRef = useRef<HTMLDivElement>(null);

    // "Add to IDS" mini-form.
    const [idsFormOpen, setIdsFormOpen] = useState(false);
    const [idsDescription, setIdsDescription] = useState('');
    const [idsPriority, setIdsPriority] = useState<IDSPriority>('medium');
    const [idsSubmitting, setIdsSubmitting] = useState(false);
    const [idsLastSentId, setIdsLastSentId] = useState<string | null>(null);

    // Seed local state + load history whenever a (new) rock opens. Keyed on
    // rockId + isOpen only — deliberately not on status/milestones, which are
    // just the initial seed.
    useEffect(() => {
        if (!isOpen || !rockId) return;
        setLiveStatus(status);
        setLiveMilestones(milestones);
        setUpdateForm(EMPTY_UPDATE_FORM);
        setUpdatesError(null);
        setIdsFormOpen(false);
        setIdsDescription('');
        setIdsPriority('medium');
        setIdsLastSentId(null);
        setUpdatesLoading(true);
        let cancelled = false;
        (async () => {
            try {
                const res = await client.get<RockWeeklyUpdate[]>(
                    `/api/eos/rocks/${rockId}/updates`,
                );
                if (!cancelled) setUpdates(res.data);
            } catch (err) {
                if (!cancelled)
                    setUpdatesError(
                        err instanceof Error ? err.message : 'Failed to load history',
                    );
            } finally {
                if (!cancelled) setUpdatesLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [rockId, isOpen]);

    // Drop the current rock's topic onto the team's IDS list.
    const submitAddToIds = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!rockId || idsSubmitting) return;
        const desc = idsDescription.trim();
        if (!desc) return;

        setIdsSubmitting(true);
        try {
            const res = await client.post<{ id: string }>(
                `/api/eos/rocks/${rockId}/ids`,
                { description: desc, priority: idsPriority },
            );
            setIdsLastSentId(res.data.id);
            // Bubble the created issue up so an in-meeting caller can surface it
            // in the IDS segment immediately (the standalone page ignores it).
            onIssueCreated?.(res.data);
            setIdsDescription('');
            setIdsPriority('medium');
            setIdsFormOpen(false);
        } catch (err) {
            console.error('Failed to add to IDS', err);
            toast.error('Failed to add to IDS — please retry.');
        } finally {
            setIdsSubmitting(false);
        }
    };

    // Click-to-toggle a milestone. Persists the new list via PATCH /rocks/{id}
    // and pre-fills the pending weekly-update form's % so "Log update"
    // captures the new progress automatically. Optimistic — re-sync on error.
    const toggleMilestone = async (idx: number) => {
        if (!rockId || !liveMilestones) return;
        const prevMilestones = liveMilestones;
        // Spread every existing milestone so its server-assigned `id` and
        // `sort_order` ride along in the PATCH. The backend reconciler matches
        // by `id` and updates the row in place — reconstructing rows without
        // the id would delete-and-recreate them with fresh UUIDs (breaking the
        // AI-Priorities card keyed on the milestone's UUID).
        const nextMilestones = prevMilestones.map((m, i) =>
            i === idx ? { ...m, completed: !m.completed } : { ...m },
        );

        setLiveMilestones(nextMilestones);
        onRockChanged(rockId, { milestones: nextMilestones });
        const newPct = computeMilestoneProgress(nextMilestones);
        setUpdateForm(prev => ({ ...prev, percent_complete: String(newPct) }));

        try {
            await client.patch<unknown>(`/api/eos/rocks/${rockId}`, {
                milestones: nextMilestones,
            });
        } catch (err) {
            console.error('Failed to toggle milestone', err);
            setLiveMilestones(prevMilestones);
            onRockChanged(rockId, { milestones: prevMilestones });
            toast.error(errorMessage(err, 'Failed to update milestone — please retry.'));
        }
    };

    const submitWeeklyUpdate = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!rockId || updateSubmitting) return;

        // Belt-and-suspenders client-side guard; the backend enforces the same
        // rules and returns 422 — we just save a round-trip.
        const pct = Number(updateForm.percent_complete);
        if (Number.isNaN(pct) || pct < 0 || pct > 100) {
            toast.error('Percent complete must be a number between 0 and 100.');
            return;
        }

        setUpdateSubmitting(true);
        try {
            const res = await client.post<RockWeeklyUpdate>(
                `/api/eos/rocks/${rockId}/updates`,
                {
                    status: updateForm.status,
                    percent_complete: pct,
                    comment: updateForm.comment.trim() || null,
                },
            );
            // Prepend (the GET returns newest-first; keep that order).
            setUpdates(prev => [res.data, ...prev]);
            setLiveStatus(res.data.status);
            onRockChanged(rockId, { status: res.data.status });
            setUpdateForm(EMPTY_UPDATE_FORM);
            // Say it saved. Clearing the form was the only signal before, and
            // the new History row lands below the fold of the modal's own
            // scroll body — so a successful save read as a no-op. Three signals
            // now: a toast, a 2.5s highlight on the new row, and scrolling that
            // row into view.
            toast.success('Weekly update logged.');
            setJustLoggedId(res.data.id);
            window.setTimeout(() => setJustLoggedId(null), 2500);
            window.requestAnimationFrame(() => {
                historyRef.current?.scrollIntoView({
                    behavior: 'smooth',
                    block: 'nearest',
                });
            });
        } catch (err) {
            console.error('Failed to log weekly update', err);
            toast.error(
                errorMessage(
                    err,
                    'Failed to log weekly update — status and percent are required.',
                ),
            );
        } finally {
            setUpdateSubmitting(false);
        }
    };

    // Local "today" for milestone overdue colouring (recomputed each render).
    const today = todayISO();

    return (
        <Modal
            isOpen={isOpen}
            onClose={onClose}
            title={rockId ? `Rock: ${description}` : 'Rock'}
            maxWidth="max-w-4xl"
        >
            {rockId && (
                <div className="space-y-6">
                    {/* Header: current cached status + owner */}
                    <div className="flex items-center justify-between text-xs text-gray-600 pb-3 border-b border-gray-100">
                        <span>
                            Owner:{' '}
                            <span className="font-medium text-gray-800">{ownerLabel}</span>
                        </span>
                        <span className="flex items-center gap-1 font-semibold">
                            {React.createElement(STATUS_ICON[liveStatus], { size: 14 })}
                            {STATUS_LABELS[liveStatus]}
                        </span>
                    </div>

                    {/* Clickable milestones — click a row to toggle it; the
                        computed % (completed / total) pre-fills the weekly
                        update form below so logging progress is one click. */}
                    <section>
                        {liveMilestones && liveMilestones.length > 0 ? (
                            <>
                                <div className="flex items-center justify-between mb-2">
                                    <h4 className="text-sm font-semibold text-gray-900">
                                        Milestones
                                        <span className="ml-2 text-xs font-normal text-gray-500">
                                            {liveMilestones.filter(m => m.completed).length} /{' '}
                                            {liveMilestones.length} done
                                        </span>
                                    </h4>
                                    <span className="text-xs font-mono font-semibold text-brand">
                                        {computeMilestoneProgress(liveMilestones)}%
                                    </span>
                                </div>
                                {/* Progress bar — shows the completed/total % visually. */}
                                <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden mb-3">
                                    <div
                                        className="h-full bg-brand transition-all"
                                        style={{
                                            width: `${computeMilestoneProgress(liveMilestones)}%`,
                                        }}
                                    />
                                </div>
                                <ul className="space-y-1">
                                    {liveMilestones.map((m, i) => {
                                        const overdue = isMilestoneOverdue(m, today);
                                        return (
                                            <li key={i}>
                                                <button
                                                    type="button"
                                                    disabled={!canEdit}
                                                    onClick={() => toggleMilestone(i)}
                                                    className={`w-full flex items-center gap-2 text-sm text-left px-2 py-1.5 rounded-md transition-colors ${
                                                        m.completed
                                                            ? 'bg-green-50 hover:bg-green-100'
                                                            : 'hover:bg-gray-50'
                                                    } ${canEdit ? '' : 'cursor-default'}`}
                                                    title={
                                                        !canEdit
                                                            ? 'This Rock belongs to another team — you can follow it, but not change it'
                                                            : m.completed
                                                              ? 'Mark incomplete'
                                                              : 'Mark complete'
                                                    }
                                                >
                                                    {m.completed ? (
                                                        <CheckCircle2
                                                            size={16}
                                                            className="text-green-600 shrink-0"
                                                        />
                                                    ) : (
                                                        <span
                                                            className="inline-block w-4 h-4 rounded-full border-2 border-gray-300 shrink-0"
                                                            aria-hidden="true"
                                                        />
                                                    )}
                                                    <span
                                                        className={`flex-1 ${
                                                            m.completed
                                                                ? 'line-through text-gray-500'
                                                                : 'text-gray-800'
                                                        }`}
                                                    >
                                                        {m.description}
                                                    </span>
                                                    {m.due_date && (
                                                        <span
                                                            className={`text-[10px] font-medium shrink-0 ${
                                                                overdue
                                                                    ? 'text-red-600'
                                                                    : 'text-gray-400'
                                                            }`}
                                                            title={overdue ? 'Overdue' : 'Due date'}
                                                        >
                                                            {formatISODate(m.due_date)}
                                                        </span>
                                                    )}
                                                </button>
                                            </li>
                                        );
                                    })}
                                </ul>
                            </>
                        ) : (
                            <>
                                <h4 className="text-sm font-semibold text-gray-900 mb-2">
                                    Milestones
                                </h4>
                                <p className="text-xs text-gray-500 italic">
                                    No milestones recorded. Edit the rock to add them, or use
                                    <strong> Draft with AI</strong> to generate a set from a goal.
                                </p>
                            </>
                        )}
                    </section>

                    {/* Add to IDS — drop the topic onto the team's IDS list */}
                    <section className="rounded-md bg-amber-50/40 border border-amber-200 p-3">
                        <div className="flex items-start justify-between gap-3">
                            <div className="flex items-start gap-2 min-w-0">
                                <AlertCircle
                                    size={14}
                                    className="text-amber-600 mt-0.5 shrink-0"
                                />
                                <div className="text-xs text-gray-700">
                                    <strong className="text-gray-900">
                                        Going down a rabbit hole?
                                    </strong>{' '}
                                    Drop the topic onto the next L10's IDS list — keeps the rock
                                    review on time while the issue gets a dedicated slot.
                                </div>
                            </div>
                            {!idsFormOpen && (
                                <button
                                    type="button"
                                    onClick={() => setIdsFormOpen(true)}
                                    className="shrink-0 px-2.5 py-1 text-xs font-medium text-amber-700 bg-white border border-amber-300 hover:bg-amber-100 rounded-md"
                                >
                                    Add to IDS
                                </button>
                            )}
                        </div>

                        {idsFormOpen && (
                            <form onSubmit={submitAddToIds} className="space-y-2 mt-3">
                                <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                                    <textarea
                                        required
                                        value={idsDescription}
                                        onChange={e => setIdsDescription(e.target.value)}
                                        className={`${inputCls} md:col-span-2`}
                                        rows={2}
                                        placeholder='What needs to be discussed? e.g. "We need to align with marketing on launch date"'
                                        autoFocus
                                    />
                                    <div className="space-y-2">
                                        <select
                                            value={idsPriority}
                                            onChange={e =>
                                                setIdsPriority(e.target.value as IDSPriority)
                                            }
                                            className={inputCls}
                                        >
                                            <option value="low">Low priority</option>
                                            <option value="medium">Medium priority</option>
                                            <option value="high">High priority</option>
                                        </select>
                                        <div className="flex gap-2">
                                            <button
                                                type="button"
                                                onClick={() => {
                                                    setIdsFormOpen(false);
                                                    setIdsDescription('');
                                                }}
                                                className="flex-1 px-2 py-1.5 text-xs text-gray-600 bg-white border border-gray-300 hover:bg-gray-50 rounded-md"
                                            >
                                                Cancel
                                            </button>
                                            <button
                                                type="submit"
                                                disabled={!idsDescription.trim() || idsSubmitting}
                                                className="flex-1 px-2 py-1.5 text-xs font-medium text-white bg-amber-600 hover:bg-amber-700 disabled:opacity-50 rounded-md"
                                            >
                                                {idsSubmitting ? 'Sending…' : 'Send to IDS'}
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            </form>
                        )}

                        {idsLastSentId && !idsFormOpen && (
                            <div className="mt-2 text-xs text-green-700 bg-green-50 border border-green-200 rounded px-2 py-1.5 flex items-center gap-1.5">
                                <CheckCircle2 size={12} />
                                Added to IDS — it'll show up in the Issues panel for this team.
                            </div>
                        )}
                    </section>

                    {/* Quarter carry-over, beside Add to IDS: the other way to
                        move an unfinished rock along. Done rocks have nothing
                        left to carry. */}
                    {carriedToLabel ? (
                        <p className="flex items-center gap-1.5 text-xs text-gray-500">
                            <CalendarArrowUp size={12} className="shrink-0" />
                            Carried over to {carriedToLabel}
                        </p>
                    ) : (
                        onCarryOver &&
                        canEdit &&
                        liveStatus !== 'done' && (
                            <section className="rounded-md border border-gray-200 p-3">
                                <div className="flex items-start justify-between gap-3">
                                    <div className="flex items-start gap-2 min-w-0">
                                        <CalendarArrowUp
                                            size={14}
                                            className="text-gray-500 mt-0.5 shrink-0"
                                        />
                                        <div className="text-xs text-gray-700">
                                            <strong className="text-gray-900">
                                                Not finishing this quarter?
                                            </strong>{' '}
                                            Carry it into next quarter. This rock stays here as the
                                            record; you edit its next-quarter version before saving.
                                        </div>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={onCarryOver}
                                        className="shrink-0 px-2.5 py-1 text-xs font-medium text-gray-700 bg-white border border-gray-300 hover:bg-gray-50 rounded-md"
                                    >
                                        Carry over to next quarter
                                    </button>
                                </div>
                            </section>
                        )
                    )}

                    {/* Weekly-update form — required fields. Hidden for
                        viewers the write gate would refuse: the whole team can
                        READ its team's rocks, but only the owner, the creator,
                        the owner's manager and the team's leadership may log
                        progress on one. */}
                    {!canEdit ? (
                        <section>
                            <p className="text-sm text-gray-500">
                                This Rock belongs to another team, so you can follow it
                                here but not log updates on it. Ask{' '}
                                {ownerLabel || 'its owner'} to record the update, or
                                raise it as an Issue in your L10.
                            </p>
                        </section>
                    ) : (
                    <section>
                        <h4 className="text-sm font-semibold text-gray-900 mb-2">
                            Log this week's update
                        </h4>
                        <form onSubmit={submitWeeklyUpdate} className="space-y-3">
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                <Field label="Status (required)">
                                    <select
                                        required
                                        value={updateForm.status}
                                        onChange={e =>
                                            setUpdateForm({
                                                ...updateForm,
                                                status: e.target.value as RockStatus,
                                            })
                                        }
                                        className={inputCls}
                                    >
                                        <option value="on_track">On Track</option>
                                        <option value="off_track">Off Track</option>
                                        <option value="done">Done</option>
                                    </select>
                                </Field>
                                <Field label="% complete (required)">
                                    <input
                                        required
                                        type="number"
                                        min={0}
                                        max={100}
                                        value={updateForm.percent_complete}
                                        onChange={e =>
                                            setUpdateForm({
                                                ...updateForm,
                                                percent_complete: e.target.value,
                                            })
                                        }
                                        className={inputCls}
                                        placeholder="0–100"
                                    />
                                </Field>
                            </div>
                            <Field label="Comment (optional)">
                                <textarea
                                    value={updateForm.comment}
                                    onChange={e =>
                                        setUpdateForm({ ...updateForm, comment: e.target.value })
                                    }
                                    className={inputCls}
                                    rows={2}
                                    placeholder='e.g. "Backend shipped, waiting on IT for ACLs"'
                                />
                            </Field>
                            <div className="flex justify-end">
                                <button
                                    type="submit"
                                    disabled={updateSubmitting}
                                    className="px-3 py-2 bg-brand hover:bg-brand-hover disabled:opacity-50 text-white rounded-md text-sm font-medium"
                                >
                                    {updateSubmitting ? 'Saving…' : 'Log update'}
                                </button>
                            </div>
                        </form>
                    </section>
                    )}

                    {/* History */}
                    <section ref={historyRef}>
                        <h4 className="text-sm font-semibold text-gray-900 mb-2">
                            History
                            <span className="ml-2 text-xs font-normal text-gray-500">
                                {updates.length} {updates.length === 1 ? 'update' : 'updates'}
                            </span>
                        </h4>
                        {updatesLoading && (
                            <p className="text-xs text-gray-500 italic">Loading history…</p>
                        )}
                        {updatesError && <p className="text-xs text-red-600">{updatesError}</p>}
                        {!updatesLoading && !updatesError && updates.length === 0 && (
                            <p className="text-xs text-gray-500 italic">
                                No updates yet — fill the form above to log the first one.
                            </p>
                        )}
                        {!updatesLoading && updates.length > 0 && (
                            <div className="border border-gray-200 rounded-md overflow-hidden">
                                <table className="w-full text-xs">
                                    <thead className="bg-gray-50 text-gray-500 uppercase tracking-wider">
                                        <tr>
                                            <th className="text-left px-3 py-2 font-semibold w-28">
                                                Date
                                            </th>
                                            <th className="text-left px-3 py-2 font-semibold w-28">
                                                Status
                                            </th>
                                            <th className="text-left px-3 py-2 font-semibold w-16">%</th>
                                            <th className="text-left px-3 py-2 font-semibold">
                                                Comment
                                            </th>
                                            <th className="text-left px-3 py-2 font-semibold w-40">
                                                By
                                            </th>
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-gray-100">
                                        {updates.map(u => {
                                            const StatusIcon = STATUS_ICON[u.status];
                                            // Resolve the author email to a Person row from the
                                            // already-loaded people list. Fall back to the raw
                                            // email if no match — e.g. an update logged by a user
                                            // who isn't on the EOS Person roster yet.
                                            const author = u.created_by
                                                ? people.find(
                                                      p =>
                                                          p.email.toLowerCase() ===
                                                          u.created_by!.toLowerCase(),
                                                  )
                                                : null;
                                            const authorLabel = author
                                                ? `${author.first_name} ${author.last_name}`
                                                : (u.created_by ?? '—');
                                            return (
                                                <tr
                                                    key={u.id}
                                                    className={
                                                        u.id === justLoggedId
                                                            ? 'bg-green-50 transition-colors'
                                                            : 'hover:bg-gray-50/50 transition-colors'
                                                    }
                                                >
                                                    <td className="px-3 py-2 font-mono text-gray-700">
                                                        {u.update_date}
                                                    </td>
                                                    <td className="px-3 py-2">
                                                        <span className="inline-flex items-center gap-1 text-gray-700">
                                                            <StatusIcon size={11} />
                                                            {STATUS_LABELS[u.status]}
                                                        </span>
                                                    </td>
                                                    <td className="px-3 py-2 font-mono text-gray-900">
                                                        {u.percent_complete}%
                                                    </td>
                                                    <td className="px-3 py-2 text-gray-700">
                                                        {u.comment || (
                                                            <span className="italic text-gray-400">—</span>
                                                        )}
                                                    </td>
                                                    <td
                                                        className="px-3 py-2 text-gray-700 truncate"
                                                        title={u.created_by ?? ''}
                                                    >
                                                        {authorLabel}
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </section>
                </div>
            )}
        </Modal>
    );
};
