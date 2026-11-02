// UC-46 CRM Sync API client. Uses the shared axios instance (bearer token +
// request id attached by its interceptor); full /api/... paths, no baseURL.
import client from './client';

export type ChangeType = 'create_contact' | 'update_contact' | 'create_deal' | 'update_deal';

export type SuggestionStatus =
    | 'pending'
    | 'applying'
    | 'applied'
    | 'failed'
    | 'dismissed'
    | 'superseded'
    | 'expired';

// `before` is what HubSpot holds right now (null for a create); `after` is
// what Cubie proposes. Both are filled server-side.
export interface PropertyDiff {
    before: string | null;
    after: string;
}

export interface CrmSuggestion {
    id: string;
    run_id: string;
    change_type: ChangeType;
    hubspot_object_type: string;
    hubspot_object_id: string | null;
    status: SuggestionStatus;
    confidence: number | null;
    error: string | null;
    properties: Record<string, PropertyDiff>;
    contact_email: string | null;
    rationale: string | null;
    evidence: {
        quote?: string;
        speaker?: string | null;
        truncated?: boolean;
    };
    meeting: { doc_id: string | null; title: string | null; date: string | null };
    applied_at: string | null;
    created_at: string | null;
}

export interface SuggestionsResponse {
    suggestions: CrmSuggestion[];
    // Null when this workspace's HubSpot portal id is unset — callers hide deep links
    // rather than render a URL that 404s.
    hubspot_portal_id: string | null;
}

export interface ScanRun {
    run_id: string;
    status: 'pending' | 'ready' | 'failed';
    error: string | null;
    stats: {
        transcripts_scanned?: number;
        external_meetings?: number;
        suggestions_created?: number;
        duplicates_skipped?: number;
        validation_rejected?: number;
    };
    queued_at: string | null;
    finished_at: string | null;
}

// Approve is offered on every pending or failed card; a failed write is a retry.
export function canApprove(s: Pick<CrmSuggestion, 'status'>): boolean {
    return s.status === 'pending' || s.status === 'failed';
}

// The approve button's words.
export function approveLabel(s: Pick<CrmSuggestion, 'status'>): string {
    return s.status === 'failed' ? 'Retry' : 'Approve';
}

// Edit needs a card that can still be approved and has fields to change.
export function canEdit(s: Pick<CrmSuggestion, 'status' | 'properties'>): boolean {
    return canApprove(s) && Object.keys(s.properties).length > 0;
}

export async function startScan(sinceDays?: number): Promise<{ run_id: string }> {
    const { data } = await client.post('/api/crm-sync/scan', {
        since_days: sinceDays ?? null,
    });
    return data;
}

export async function getRun(runId: string): Promise<ScanRun> {
    const { data } = await client.get(`/api/crm-sync/runs/${runId}`);
    return data;
}

export async function listSuggestions(status = 'pending'): Promise<SuggestionsResponse> {
    const { data } = await client.get('/api/crm-sync/suggestions', {
        params: { status },
    });
    return data;
}

export async function approveSuggestion(
    id: string,
    editedAfter?: Record<string, string>,
): Promise<CrmSuggestion> {
    const { data } = await client.post(`/api/crm-sync/suggestions/${id}/approve`, {
        edited_after: editedAfter ?? null,
    });
    return data;
}

export async function dismissSuggestion(
    id: string,
    reason?: string,
): Promise<CrmSuggestion> {
    const { data } = await client.post(`/api/crm-sync/suggestions/${id}/dismiss`, {
        reason: reason ?? null,
    });
    return data;
}

// HubSpot record URLs. 0-1 = contacts, 0-3 = deals.
export function hubspotUrl(
    portalId: string | null,
    objectType: string,
    objectId: string | null,
): string | null {
    if (!portalId || !objectId) return null;
    const typeId = objectType === 'deal' ? '0-3' : '0-1';
    return `https://app.hubspot.com/contacts/${portalId}/record/${typeId}/${objectId}`;
}
