// UC-45 Customer X-Ray API client. Uses the shared axios instance (bearer token
// + request id attached by its interceptor); full /api/... paths, no baseURL.
import client from './client';

export interface ChurnFactor {
    name: string;
    value: string;
    points: number;
    weight: number;
    evidence: string;
}

export interface XraySignals {
    churn?: {
        score: number;
        band: string;
        factors: ChurnFactor[];
        rationale_md?: string;
    };
    days_since_last_touch?: number | null;
    active_products_count?: number;
    active_products?: string[];
    open_pipeline_value?: number;
    open_deals_count?: number;
    renewal_countdown_days?: number | null;
    activity_trend?: { last_90d: number; prior_90d: number; direction: string };
    client_status?: string;
    csm?: string;
    company_name?: string;
    company_domain?: string;
    email?: Record<string, unknown>;
}

export interface Opportunity {
    title: string;
    why_now: string;
    product: string;
    play: string;
    entity: string;
}

export interface NewsItem {
    date: string;
    headline: string;
    why_it_matters: string;
    url: string;
}

export interface XraySections {
    executive_summary?: string;
    relationship_timeline_highlights?: string[];
    relationship_health?: {
        narrative_md?: string;
        cadence?: string | null;
        breadth?: string | null;
        responsiveness?: string | null;
        sentiment_trend?: string | null;
    };
    commercial_health?: string;
    opportunities?: Opportunity[];
    churn_rationale_md?: string;
    recent_news?: NewsItem[];
    source_evidence?: string[];
    _as_of?: Record<string, string>;
}

export interface TimelineEvent {
    id: string;
    event_key: string;
    ts: string | null;
    source: string;
    kind: string | null;
    title: string;
    detail: string | null;
    evidence: string | null;
}

export interface XraySnapshot {
    id: string;
    company_id: string;
    company_name?: string;
    company_domain?: string;
    version: number;
    kind: string;
    status: string;
    error?: string | null;
    focus?: string | null;
    sections: XraySections;
    signals: XraySignals;
    covered_through?: string | null;
    progress?: Record<string, string>;
    partial_errors: string[];
    model_id?: string;
    created_by?: string;
    created_at?: string;
    updated_at?: string;
    timeline?: TimelineEvent[];
}

export interface SnapshotStub {
    id: string;
    kind: string;
    version: number;
    churn_band?: string;
    churn_score?: number;
    created_at?: string;
}

export interface CustomerRow {
    company_id: string;
    name: string;
    domain: string;
    client_status: string;
    csm: string;
    latest_snapshot: SnapshotStub | null;
}

export interface RunStatus {
    status: string;
    progress: Record<string, string>;
    error?: string | null;
    snapshot?: XraySnapshot;
}

export interface HistoryRow {
    id: string;
    version: number;
    kind: string;
    status: string;
    created_by?: string;
    created_at?: string;
    churn: { score?: number; band?: string };
}

interface RunBody {
    focus?: string;
    company_name?: string;
    company_domain?: string;
}

export const searchCustomers = (q: string) =>
    client
        .get<CustomerRow[]>('/api/xray/customers', { params: { q } })
        .then((r) => r.data);

export const recentXrays = () =>
    client.get<CustomerRow[]>('/api/xray/recent').then((r) => r.data);

// Fast runs inline (a few seconds); give it a generous client timeout.
export const runFast = (companyId: string, body: RunBody) =>
    client
        .post<XraySnapshot>(`/api/xray/${companyId}/fast`, body, { timeout: 120_000 })
        .then((r) => r.data);

export const runDeep = (companyId: string, body: RunBody) =>
    client
        .post<{ snapshot_id: string; status: string }>(
            `/api/xray/${companyId}/deep`,
            body,
        )
        .then((r) => r.data);

export const getRun = (snapshotId: string) =>
    client.get<RunStatus>(`/api/xray/runs/${snapshotId}`).then((r) => r.data);

export const getLatest = (companyId: string) =>
    client.get<XraySnapshot>(`/api/xray/${companyId}`).then((r) => r.data);

export const getHistory = (companyId: string) =>
    client.get<HistoryRow[]>(`/api/xray/${companyId}/history`).then((r) => r.data);

export const getTimeline = (
    companyId: string,
    params: { source?: string; limit?: number; before?: string },
) =>
    client
        .get<TimelineEvent[]>(`/api/xray/${companyId}/timeline`, { params })
        .then((r) => r.data);

/** Editable Word twin of the on-screen brief (blob → caller downloads). */
export const downloadXrayDocx = (snapshotId: string) =>
    client.get<Blob>(`/api/xray/snapshots/${snapshotId}/docx`, {
        responseType: 'blob',
    });

/** Persist the brief as a Word doc on the caller's Assets page. */
export const saveXrayToAssets = (snapshotId: string) =>
    client.post<{ doc_id: string; created: boolean; filename: string }>(
        `/api/xray/snapshots/${snapshotId}/save-to-assets`,
    );

export const CHURN_BAND_COLOR: Record<string, string> = {
    Healthy: '#15803d',
    Watch: '#b45309',
    'At-risk': '#b91c1c',
};
