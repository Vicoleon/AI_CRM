import React, { useMemo } from 'react';
import { Calendar, CheckSquare, Square } from 'lucide-react';
import { Modal } from './Modal';
import { formatInstant } from '../../utils/datetime';
import { StatusBadge, ExecBadge, OwnerChip } from '../../pages/eos/_shared';
import {
    EXEC_SCOPE_LABEL,
    formatPerson,
    type PersonLite,
} from '../../pages/eos/_utils';
import type { Issue } from './IssuesView';
import type { Todo } from './TodosView';

// Date-only stamp with the year (issues can sit open across months). Mirrors
// the Issues grid's `_fmtCreated`.
const fmtDate = (iso: string | null | undefined): string =>
    formatInstant(iso, { month: 'short', day: 'numeric', year: 'numeric' });

const PRIORITY_META: Record<
    Issue['priority'],
    { label: string; tone: 'red' | 'amber' | 'gray' }
> = {
    high: { label: 'High', tone: 'red' },
    medium: { label: 'Medium', tone: 'amber' },
    low: { label: 'Low', tone: 'gray' },
};

const STATUS_META: Record<
    Issue['status'],
    { label: string; tone: 'blue' | 'green' | 'gray' }
> = {
    open: { label: 'Open', tone: 'blue' },
    solved: { label: 'Solved', tone: 'green' },
    archived: { label: 'Archived', tone: 'gray' },
};

interface IssueDetailModalProps {
    /** The issue to show. Null keeps the modal closed. */
    issue: Issue | null;
    people: PersonLite[];
    /** Child to-dos spun off this issue (rendered read-only for context). */
    childTodos: Todo[];
    onClose: () => void;
}

/**
 * Read-only "full issue" dialog. The IDS grid shows only each issue's title;
 * clicking a row opens this to reveal the full description plus all metadata
 * (priority, status, owner, team, who raised it, dates) and any child to-dos.
 *
 * Purely presentational — no writes. Editing stays on the row's pencil action
 * (the create/edit modal in {@link IssuesView}); this is the "view details"
 * surface, shared by the standalone Issues page and the in-meeting L10 IDS
 * segment.
 */
export const IssueDetailModal: React.FC<IssueDetailModalProps> = ({
    issue,
    people,
    childTodos,
    onClose,
}) => {
    const peopleById = useMemo(() => new Map(people.map(p => [p.id, p])), [people]);
    const peopleByEmail = useMemo(
        () => new Map(people.map(p => [p.email.toLowerCase(), p])),
        [people],
    );

    const owner = issue?.owner_id ? peopleById.get(issue.owner_id) : undefined;
    const priority = issue ? PRIORITY_META[issue.priority] : null;
    const status = issue ? STATUS_META[issue.status] : null;
    const team = issue
        ? issue.is_exec
            ? EXEC_SCOPE_LABEL
            : issue.department_name
        : null;

    return (
        <Modal
            isOpen={issue !== null}
            onClose={onClose}
            title={issue ? issue.title || 'Issue' : 'Issue'}
            maxWidth="max-w-4xl"
        >
            {issue && (
                <div className="space-y-5">
                    {/* Badges: priority + status + optional exec tag. */}
                    <div className="flex flex-wrap items-center gap-2">
                        {priority && (
                            <StatusBadge value={`${priority.label} priority`} tone={priority.tone} />
                        )}
                        {status && <StatusBadge value={status.label} tone={status.tone} />}
                        {issue.is_exec && <ExecBadge />}
                    </div>

                    {/* Full description — preserves the author's line breaks. */}
                    <section>
                        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1.5">
                            Description
                        </h4>
                        {issue.description?.trim() ? (
                            <p className="text-sm text-gray-800 whitespace-pre-wrap break-words">
                                {issue.description}
                            </p>
                        ) : (
                            <p className="text-sm text-gray-400 italic">No description.</p>
                        )}
                    </section>

                    {/* Metadata grid. */}
                    <section className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm border-t border-gray-100 pt-4">
                        <div>
                            <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">
                                Owner
                            </div>
                            {owner ? (
                                <OwnerChip name={formatPerson(owner)} />
                            ) : (
                                <span className="text-gray-400">Unassigned</span>
                            )}
                        </div>
                        <div>
                            <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">
                                Team
                            </div>
                            <span className="text-gray-800">{team || '—'}</span>
                        </div>
                        <div>
                            <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">
                                Raised by
                            </div>
                            <span className="text-gray-800">
                                {issue.created_by
                                    ? formatPerson(
                                          peopleByEmail.get(issue.created_by.toLowerCase()),
                                          issue.created_by,
                                      )
                                    : '—'}
                            </span>
                        </div>
                        <div>
                            <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">
                                Raised on
                            </div>
                            <span className="text-gray-800">{fmtDate(issue.created_at)}</span>
                        </div>
                        {issue.solved_at && (
                            <div>
                                <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">
                                    Solved on
                                </div>
                                <span className="text-green-600 font-medium">
                                    {fmtDate(issue.solved_at)}
                                </span>
                            </div>
                        )}
                    </section>

                    {/* Child to-dos spun off this issue — read-only. */}
                    {childTodos.length > 0 && (
                        <section className="border-t border-gray-100 pt-4">
                            <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">
                                Linked to-dos ({childTodos.length})
                            </h4>
                            <ul className="space-y-2">
                                {childTodos.map(todo => (
                                    <li key={todo.id} className="flex items-center gap-3 text-sm">
                                        {todo.status === 'done' ? (
                                            <CheckSquare
                                                size={15}
                                                className="text-green-500 flex-shrink-0"
                                            />
                                        ) : (
                                            <Square
                                                size={15}
                                                className="text-gray-300 flex-shrink-0"
                                            />
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
                        </section>
                    )}
                </div>
            )}
        </Modal>
    );
};
