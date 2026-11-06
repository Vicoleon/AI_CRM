/**
 * Settings → Scheduled tasks.
 *
 * Deliberately NOT a create form: tasks are created by asking Cubie in Chat
 * ("every weekday at 8am send me my important emails"), which is the whole
 * feature. This tab is the management surface — see what is scheduled, when it
 * next runs, what happened last time, and pause / run / delete.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
    CalendarClock,
    Loader2,
    Pause,
    Play,
    Zap,
    Trash2,
    AlertCircle,
    CheckCircle2,
    MessageSquare,
    Mail,
    Slack as SlackIcon,
} from 'lucide-react';
import {
    deleteScheduledTask,
    fetchScheduledTasks,
    runScheduledTaskNow,
    updateScheduledTask,
    type ScheduledTask,
} from '../../api/scheduledTasks';

const STATUS_LABEL: Record<ScheduledTask['status'], string> = {
    active: 'Active',
    paused: 'Paused',
    completed: 'Done',
    error_paused: 'Paused after failures',
};

const STATUS_CLASS: Record<ScheduledTask['status'], string> = {
    active: 'bg-green-50 text-green-700 border-green-200',
    paused: 'bg-gray-100 text-gray-600 border-gray-200',
    completed: 'bg-gray-100 text-gray-600 border-gray-200',
    error_paused: 'bg-amber-50 text-amber-800 border-amber-200',
};

const RUN_LABEL: Record<string, string> = {
    queued: 'queued',
    running: 'running now',
    succeeded: 'succeeded',
    failed: 'failed',
    skipped: 'skipped',
};

const errorText = (err: unknown): string => {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
    if (detail) return detail;
    return err instanceof Error ? err.message : 'Something went wrong.';
};

const ChannelPills: React.FC<{ channels: string[] }> = ({ channels }) => (
    <span className="inline-flex items-center gap-2 text-xs text-gray-500">
        <span className="inline-flex items-center gap-1">
            <MessageSquare size={13} /> Chat
        </span>
        {channels.includes('slack') && (
            <span className="inline-flex items-center gap-1">
                <SlackIcon size={13} /> Slack
            </span>
        )}
        {channels.includes('email') && (
            <span className="inline-flex items-center gap-1">
                <Mail size={13} /> Email
            </span>
        )}
    </span>
);

export const ScheduledTasksTab: React.FC = () => {
    const [tasks, setTasks] = useState<ScheduledTask[]>([]);
    const [maxPerUser, setMaxPerUser] = useState<number | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    // The id currently being acted on, so only that row's buttons spin.
    const [busyId, setBusyId] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);

    const load = useCallback(async (): Promise<void> => {
        try {
            const data = await fetchScheduledTasks();
            setTasks(data.tasks);
            setMaxPerUser(data.max_per_user);
            setError(null);
        } catch (err) {
            setError(errorText(err));
        } finally {
            setLoading(false);
        }
    }, []);

    // Mount-only loader: every setState happens after an await, inside the
    // async callback (react-hooks/set-state-in-effect is an error here).
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const data = await fetchScheduledTasks();
                if (!cancelled) {
                    setTasks(data.tasks);
                    setMaxPerUser(data.max_per_user);
                }
            } catch (err) {
                if (!cancelled) setError(errorText(err));
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    const act = async (id: string, fn: () => Promise<unknown>, message?: string) => {
        setBusyId(id);
        setError(null);
        setNotice(null);
        try {
            await fn();
            if (message) setNotice(message);
            await load();
        } catch (err) {
            setError(errorText(err));
        } finally {
            setBusyId(null);
        }
    };

    const onToggle = (task: ScheduledTask) => {
        const next = task.status === 'active' ? 'paused' : 'active';
        return act(
            task.id,
            () => updateScheduledTask(task.id, { status: next }),
            next === 'paused'
                ? `Paused “${task.title}”. Nothing is lost — resume it any time.`
                : `Resumed “${task.title}”.`,
        );
    };

    const onRunNow = (task: ScheduledTask) =>
        act(
            task.id,
            () => runScheduledTaskNow(task.id),
            `Running “${task.title}” now. The result arrives as its own conversation in your Scheduled tasks chat folder in a minute or two.`,
        );

    const onDelete = (task: ScheduledTask) => {
        if (
            !window.confirm(
                `Delete “${task.title}”? It will never run again. Results you already received stay in your chat history.`,
            )
        ) {
            return;
        }
        return act(task.id, () => deleteScheduledTask(task.id), `Deleted “${task.title}”.`);
    };

    if (loading) {
        return (
            <div className="p-8 flex items-center gap-2 text-gray-500">
                <Loader2 className="animate-spin" size={16} /> Loading…
            </div>
        );
    }

    return (
        <div>
            <header className="mb-5">
                <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
                    <CalendarClock size={18} /> Scheduled tasks
                </h2>
                <p className="text-sm text-gray-500 mt-1">
                    Work Cubie runs for you automatically. Create one by asking in{' '}
                    <Link to="/chat" className="text-brand hover:underline">
                        Chat
                    </Link>{' '}
                    — for example, “every weekday at 8am send me my important emails”.
                </p>
            </header>

            {error && (
                <div className="mb-4 flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
                    <AlertCircle size={16} className="mt-0.5 shrink-0" />
                    <span>{error}</span>
                </div>
            )}
            {notice && (
                <div className="mb-4 flex items-start gap-2 rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-800">
                    <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
                    <span>{notice}</span>
                </div>
            )}

            {tasks.length === 0 ? (
                <div className="rounded-lg border border-dashed border-gray-300 p-8 text-center">
                    <CalendarClock size={22} className="mx-auto text-gray-400" />
                    <p className="mt-3 text-sm text-gray-600">
                        You haven’t scheduled anything yet.
                    </p>
                    <Link
                        to="/chat?q=Schedule%20this%20for%20me%3A%20every%20weekday%20at%208am%2C%20send%20me%20my%20important%20emails"
                        className="mt-4 inline-flex items-center gap-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand-hover"
                    >
                        Ask Cubie to schedule one
                    </Link>
                </div>
            ) : (
                <ul className="space-y-3">
                    {tasks.map(task => {
                        const busy = busyId === task.id;
                        const lastRun = task.last_run;
                        return (
                            <li
                                key={task.id}
                                className="rounded-lg border border-gray-200 bg-white p-4"
                            >
                                <div className="flex flex-wrap items-start justify-between gap-3">
                                    <div className="min-w-0">
                                        <div className="flex items-center gap-2">
                                            <h3 className="truncate font-medium text-gray-900">
                                                {task.title}
                                            </h3>
                                            <span
                                                className={`rounded-full border px-2 py-0.5 text-xs ${STATUS_CLASS[task.status]}`}
                                            >
                                                {STATUS_LABEL[task.status]}
                                            </span>
                                        </div>
                                        <p className="mt-1 text-sm text-gray-600">
                                            {task.schedule_text}
                                            {task.status === 'active' && task.next_run_text && (
                                                <>
                                                    {' · '}
                                                    <span className="text-gray-500">
                                                        next {task.next_run_text}
                                                    </span>
                                                </>
                                            )}
                                        </p>
                                        <p className="mt-1 line-clamp-2 text-sm text-gray-500">
                                            {task.prompt}
                                        </p>
                                        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
                                            <ChannelPills channels={task.delivery_channels} />
                                            {lastRun && (
                                                <span className="text-xs text-gray-500">
                                                    Last run {RUN_LABEL[lastRun.status] ?? lastRun.status}
                                                    {lastRun.chat_session_id && (
                                                        <>
                                                            {' · '}
                                                            <Link
                                                                to={`/chat?session=${lastRun.chat_session_id}`}
                                                                className="text-brand hover:underline"
                                                            >
                                                                open result
                                                            </Link>
                                                        </>
                                                    )}
                                                </span>
                                            )}
                                        </div>
                                        {task.paused_reason && (
                                            <p className="mt-2 text-xs text-amber-800">
                                                {task.paused_reason}
                                            </p>
                                        )}
                                        {lastRun?.status === 'failed' && lastRun.error && (
                                            <p className="mt-1 text-xs text-red-700">
                                                {lastRun.error}
                                            </p>
                                        )}
                                    </div>

                                    <div className="flex shrink-0 items-center gap-1">
                                        {task.status !== 'completed' && (
                                            <button
                                                onClick={() => onRunNow(task)}
                                                disabled={busy}
                                                title="Run now"
                                                className="rounded-md p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-800 disabled:opacity-40"
                                            >
                                                <Zap size={16} />
                                            </button>
                                        )}
                                        {task.status !== 'completed' && (
                                            <button
                                                onClick={() => onToggle(task)}
                                                disabled={busy}
                                                title={task.status === 'active' ? 'Pause' : 'Resume'}
                                                className="rounded-md p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-800 disabled:opacity-40"
                                            >
                                                {task.status === 'active' ? (
                                                    <Pause size={16} />
                                                ) : (
                                                    <Play size={16} />
                                                )}
                                            </button>
                                        )}
                                        <button
                                            onClick={() => onDelete(task)}
                                            disabled={busy}
                                            title="Delete"
                                            className="rounded-md p-2 text-gray-500 hover:bg-red-50 hover:text-red-700 disabled:opacity-40"
                                        >
                                            <Trash2 size={16} />
                                        </button>
                                        {busy && (
                                            <Loader2 size={16} className="animate-spin text-gray-400" />
                                        )}
                                    </div>
                                </div>
                            </li>
                        );
                    })}
                </ul>
            )}

            {maxPerUser !== null && tasks.length > 0 && (
                <p className="mt-4 text-xs text-gray-500">
                    {tasks.length} of {maxPerUser} scheduled tasks. Each one is a full Cubie run
                    every time it fires, so the cap keeps the cost predictable.
                </p>
            )}
        </div>
    );
};
