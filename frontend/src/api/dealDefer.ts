// Defer a deal — set it aside until a follow-up date, with a note.
//
// THE ROUTER IS THE CONTRACT: every field here was read out of
// `backend/routers/deal_defer.py`. A deferred deal drops off the rep's own
// lists (My Day's deal blocks, the Daily rhythm checks, the deal cards on the
// Daily Briefing) until its follow-up date, when it comes back by itself. The
// manager views keep listing it with owner, date and note. Each defer also
// writes one follow-up task on the deal in HubSpot.
//
// Rows that may be deferred carry a `defer` handle (`DeferTarget`) from the
// server; the client never decides on its own that a row is a deal.
import client from './client';

/** The longest a deal may be deferred — `MAX_DEFER_DAYS` in
 *  `rubik_core/sales/deal_defer.py`. The server enforces it; the date picker
 *  uses it so the rep cannot pick a date that will be refused. */
export const MAX_DEFER_DAYS = 180;

/** `NOTE_MAX_CHARS` in the same module. */
export const NOTE_MAX_CHARS = 1000;

/** The deal a row is about, when the reader may defer it. */
export interface DeferTarget {
    deal_id: string;
    deal_name?: string | null;
}

/** Whether the HubSpot follow-up task landed. */
export type DeferTaskStatus = 'pending' | 'written' | 'failed';

export interface DeferredDeal {
    deal_id: string;
    deal_name?: string | null;
    /** YYYY-MM-DD — the day the deal comes back on its own. */
    follow_up_on: string;
    note: string;
    owner_email: string;
    owner_name?: string | null;
    deferred_by: string;
    /** YYYY-MM-DD. */
    deferred_on: string;
    hubspot_task_status: DeferTaskStatus | string;
    /** Why the task did not land, in words a rep can act on. */
    hubspot_task_error?: string | null;
    hubspot_url?: string | null;
}

export interface DeferRequest {
    /** YYYY-MM-DD, after today and at most `MAX_DEFER_DAYS` out. */
    follow_up_on: string;
    /** Required: why the deal is on hold and what happens next. */
    note: string;
    deal_name?: string | null;
}

export interface DeferResponse {
    defer: DeferredDeal;
    outcome: 'created' | 'replaced' | 'unchanged' | string;
    hubspot_task: {
        /** Only `written` and `already_written` mean the task is on the deal.
         *  `in_progress`: another request is creating it right now.
         *  `unconfirmed`: Cubie could not tell whether it landed. */
        status: 'written' | 'already_written' | 'write_failed' | 'in_progress' | 'unconfirmed' | string;
        message?: string | null;
    };
}

export async function deferDeal(dealId: string, body: DeferRequest): Promise<DeferResponse> {
    const { data } = await client.post<DeferResponse>(
        `/api/sales/deals/${encodeURIComponent(dealId)}/defer`,
        body,
    );
    return data;
}

export async function undeferDeal(dealId: string): Promise<{ deal_id: string; message: string }> {
    const { data } = await client.post<{ deal_id: string; message: string }>(
        `/api/sales/deals/${encodeURIComponent(dealId)}/undefer`,
    );
    return data;
}

export async function fetchDeferredDeals(): Promise<DeferredDeal[]> {
    const { data } = await client.get<{ items: DeferredDeal[] }>('/api/sales/deals/deferred');
    return data.items ?? [];
}

/** What the rep is told after pressing Defer. A defer always lands in Cubie;
 *  the HubSpot task is the part that can fail, and the toast has to say which. */
export function deferOutcomeMessage(res: DeferResponse): { ok: boolean; text: string } {
    const back = res.defer.follow_up_on;
    const task = res.hubspot_task;
    if (task.status === 'write_failed') {
        return {
            ok: false,
            text: `Deferred until ${back}, but the HubSpot follow-up task was not created: ${task.message ?? 'HubSpot refused it.'} Retry it from Deferred deals on My Day.`,
        };
    }
    if (task.status !== 'written' && task.status !== 'already_written') {
        // in_progress / unconfirmed / a status this build does not know: the
        // defer stands, the task is not confirmed — never claim it landed.
        return {
            ok: false,
            text: `Deferred until ${back}. ${task.message ?? 'The HubSpot follow-up task is not confirmed yet.'}`,
        };
    }
    // A new date or note opens a new defer with its own task; the earlier
    // task is not removed from HubSpot, and the rep should know.
    const earlier =
        res.outcome === 'replaced'
            ? ' Your earlier follow-up task is still in HubSpot; close it there if you no longer need it.'
            : '';
    return {
        ok: true,
        text: `Deferred until ${back}. A follow-up task for that day is on the deal in HubSpot.${earlier}`,
    };
}

/** YYYY-MM-DD for a local calendar day, `days` from today. Local on purpose:
 *  a follow-up date is a day on the rep's calendar, not a UTC instant. */
export function localDay(days: number, from: Date = new Date()): string {
    const d = new Date(from.getFullYear(), from.getMonth(), from.getDate() + days);
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${d.getFullYear()}-${mm}-${dd}`;
}
