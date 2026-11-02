// Scheduled tasks API client. Uses the shared axios instance (bearer token +
// request id attached by its interceptor); full /api/... paths.
//
// There is no create() here on purpose: tasks are created by asking Cubie in
// Chat, which is the feature. This module is the management surface.
import client from './client';

/** One attempt at a scheduled task. Mirrors backend `RunOut`. */
export interface ScheduledTaskRun {
    id: string;
    status: 'queued' | 'running' | 'succeeded' | 'failed' | 'skipped';
    trigger: 'schedule' | 'manual';
    scheduled_for: string | null;
    started_at: string | null;
    finished_at: string | null;
    /** The chat conversation holding the result — open it at /chat?session=<id>. */
    chat_session_id: string | null;
    /** Per-channel outcome, e.g. { slack: 'sent', email: 'disabled' }. */
    delivery: Record<string, string> | null;
    error: string | null;
    created_at: string;
}

/** One scheduled task. Mirrors backend `TaskOut`. */
export interface ScheduledTask {
    id: string;
    title: string;
    prompt: string;
    schedule_kind: 'once' | 'daily' | 'weekdays' | 'weekly' | 'monthly';
    /** Plain English, rendered as-is — never re-derived from the parts here. */
    schedule_text: string;
    timezone: string;
    time_local: string | null;
    days_of_week: number[] | null;
    day_of_month: number | null;
    status: 'active' | 'paused' | 'completed' | 'error_paused';
    paused_reason: string | null;
    next_run_at: string | null;
    /** Next run in the task's own timezone, e.g. "Tue Sep 9, 8:00 AM". */
    next_run_text: string;
    delivery_channels: string[];
    last_run_at: string | null;
    last_run_status: string | null;
    run_count: number;
    consecutive_failures: number;
    created_at: string;
    last_run: ScheduledTaskRun | null;
}

export interface ScheduledTaskList {
    tasks: ScheduledTask[];
    max_per_user: number;
}

export interface ScheduledTaskPatch {
    title?: string;
    prompt?: string;
    status?: 'active' | 'paused';
    schedule_kind?: 'daily' | 'weekdays' | 'weekly' | 'monthly';
    time_local?: string;
    days_of_week?: number[];
    day_of_month?: number;
    timezone?: string;
    deliver_by_slack?: boolean;
    deliver_by_email?: boolean;
}

export const fetchScheduledTasks = async (): Promise<ScheduledTaskList> => {
    const { data } = await client.get<ScheduledTaskList>('/api/scheduled-tasks');
    return data;
};

export const fetchScheduledTaskRuns = async (id: string): Promise<ScheduledTaskRun[]> => {
    const { data } = await client.get<ScheduledTaskRun[]>(
        `/api/scheduled-tasks/${encodeURIComponent(id)}/runs`,
    );
    return data;
};

export const updateScheduledTask = async (
    id: string,
    body: ScheduledTaskPatch,
): Promise<ScheduledTask> => {
    const { data } = await client.patch<ScheduledTask>(
        `/api/scheduled-tasks/${encodeURIComponent(id)}`,
        body,
    );
    return data;
};

/** Fire a task now, leaving its schedule alone. 409 when one is in flight. */
export const runScheduledTaskNow = async (id: string): Promise<ScheduledTaskRun> => {
    const { data } = await client.post<ScheduledTaskRun>(
        `/api/scheduled-tasks/${encodeURIComponent(id)}/run-now`,
    );
    return data;
};

export const deleteScheduledTask = async (id: string): Promise<void> => {
    await client.delete(`/api/scheduled-tasks/${encodeURIComponent(id)}`);
};
