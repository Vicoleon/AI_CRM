// My To-Dos API client — the personal promises ledger (/api/todos).
// Uses the shared axios instance (bearer token + request id attached by its
// interceptor); full /api/... paths. Dates here are CALENDAR DAYS
// (YYYY-MM-DD in the owner's timezone) — render them with formatISODate,
// never parseInstant, or they shift a day west of Greenwich.
import client from './client';

export type TodoStatus =
    | 'suggested' | 'open' | 'slipping' | 'evidence_found'
    | 'kept' | 'dropped' | 'handed_off';

export type CounterpartyKind = 'person' | 'company' | 'deal';
export type ObjectKind =
    | 'thread' | 'document' | 'deal' | 'event' | 'transcript' | 'person' | 'company';

/** One promise. Mirrors backend `_out(row)` in backend/routers/promises.py. */
export interface Todo {
    id: string;
    title: string;
    detail: string | null;
    status: TodoStatus;
    promised_for: string | null;
    due_local: string | null;
    timezone: string;
    counterparty_kind: CounterpartyKind | null;
    counterparty_id: string | null;
    counterparty_label: string | null;
    object_kind: ObjectKind | null;
    object_id: string | null;
    object_label: string | null;
    source: string;
    source_ref: Record<string, unknown> | null;
    snooze_count: number;
    slipping_since: string | null;
    is_private: boolean;
    eos_todo_id: string | null;
    created_at: string | null;
    kept_at: string | null;
    dropped_at: string | null;
}

export interface TodoGroup {
    key: string;
    label: string;
    kind: CounterpartyKind | null;
    items: Todo[];
}

export interface EosTodoRow {
    id: string;
    title: string;
    due_local: string | null;
    team: string;
    l10_meeting_id: string | null;
    link: string;
}

export interface CommitmentRow {
    id: string;
    kind: string;
    title: string;
    state: string;
    hubspot_deal_id: string | null;
    hubspot_company_id: string | null;
    promised_at: string | null;
    breach_at: string | null;
    link: string;
}

export interface TodosPage {
    today: string;
    timezone: string;
    people_waiting: number;
    suggested: Todo[];
    confirm: Todo[];
    waiting: TodoGroup[];
    read_through: { eos_todos: EosTodoRow[]; commitments: CommitmentRow[] };
    ledger: { week_start: string; kept: number; dropped: number; open: number; slipping: number };
    recent: Todo[];
}

export interface ParsedTodo {
    title: string;
    detail: string | null;
    due_local: string | null;
    counterparty_label: string | null;
    counterparty_kind: CounterpartyKind | null;
    counterparty_id?: string | null;
    object_label: string | null;
    object_kind: ObjectKind | null;
    parser: 'llm' | 'fallback';
}

export interface CreateTodoInput {
    title: string;
    detail?: string | null;
    due_local?: string | null;
    counterparty_kind?: CounterpartyKind | null;
    counterparty_id?: string | null;
    counterparty_label?: string | null;
    object_kind?: ObjectKind | null;
    object_id?: string | null;
    object_label?: string | null;
    source?: 'page' | 'chat' | 'priority_card' | 'slack_cmd';
    source_ref?: Record<string, unknown> | null;
}

export const fetchTodos = async (): Promise<TodosPage> => {
    const { data } = await client.get<TodosPage>('/api/todos');
    return data;
};

export const parseTodo = async (text: string): Promise<ParsedTodo> => {
    const { data } = await client.post<{ parsed: ParsedTodo }>('/api/todos/parse', { text });
    return data.parsed;
};

export const createTodo = async (input: CreateTodoInput): Promise<Todo> => {
    const { data } = await client.post<{ todo: Todo }>('/api/todos', input);
    return data.todo;
};

export const addTodoFromPriority = async (
    priorityId: string,
): Promise<{ todo: Todo; created: boolean }> => {
    const { data } = await client.post<{ todo: Todo; created: boolean }>(
        '/api/todos/from-priority',
        { priority_id: priorityId },
    );
    return data;
};

export interface PatchTodoInput {
    title?: string;
    detail?: string | null;
    due_local?: string | null;
    clear_due?: boolean;
    counterparty_label?: string | null;
    clear_counterparty?: boolean;
    is_private?: boolean;
}

export const patchTodo = async (id: string, input: PatchTodoInput): Promise<Todo> => {
    const { data } = await client.patch<{ todo: Todo }>(`/api/todos/${encodeURIComponent(id)}`, input);
    return data.todo;
};

const act = async (id: string, verb: string, body?: Record<string, unknown>): Promise<Todo> => {
    const { data } = await client.post<{ todo: Todo }>(
        `/api/todos/${encodeURIComponent(id)}/${verb}`,
        body ?? {},
    );
    return data.todo;
};

export const keptTodo = (id: string) => act(id, 'kept');
export const notYetTodo = (id: string) => act(id, 'not-yet');
export const dropTodo = (id: string, reason?: string) => act(id, 'drop', { reason: reason ?? null });
export const reopenTodo = (id: string) => act(id, 'reopen');
export const acceptTodo = (id: string) => act(id, 'accept');
export const rejectTodo = (id: string) => act(id, 'reject');
export const snoozeTodo = (id: string, opts: { preset?: 'tomorrow' | '3d' | 'week'; until?: string }) =>
    act(id, 'snooze', opts);
export const promoteTodoToEos = async (id: string, department?: string) => {
    const { data } = await client.post<{ todo: Todo; eos_todo_id: string; created: boolean }>(
        `/api/todos/${encodeURIComponent(id)}/promote-eos`,
        { department: department ?? null },
    );
    return data;
};
