// Admin → Feature Access API client. Uses the shared axios instance (bearer
// token + request id attached by its interceptor); full /api/... paths.
import client from './client';

/** The editable half of a feature's roster. Mirrors backend `PolicyIn`. */
export interface FeaturePolicy {
    // Department slugs as stored; the picker sends labels and the server
    // slugifies, so a label from Admin → Departments is always accepted.
    departments: string[];
    extra_emails: string[];
    exec_allowed: boolean;
    admin_allowed: boolean;
    everyone: boolean;
}

export interface FeatureMember {
    email: string;
    name: string;
    department?: string | null;
    /** The door that admits them: admin | exec | named | everyone | department | managed team. */
    via: string;
}

export interface FeatureAccessEntry {
    key: string;
    label: string;
    description: string;
    surface: string;
    group: string;
    notes: string;
    membership: 'primary_or_managed' | 'managed_only';
    /** Env kill switch, read-only. null when the feature has none. */
    kill_switch: { name: string; enabled: boolean } | null;
    /** The roster the feature ships with (what "Reset to default" restores). */
    default: FeaturePolicy;
    /** The roster in force. */
    policy: FeaturePolicy;
    customized: boolean;
    note: string | null;
    updated_by: string | null;
    updated_at: string | null;
    members: FeatureMember[];
}

export interface FeatureAccessList {
    features: FeatureAccessEntry[];
    departments: string[];
}

export interface PolicyInput extends FeaturePolicy {
    note?: string;
}

export const fetchFeatureAccess = async (): Promise<FeatureAccessList> => {
    const { data } = await client.get<FeatureAccessList>('/api/admin/feature-access');
    return data;
};

export const previewFeaturePolicy = async (
    key: string,
    body: PolicyInput,
): Promise<{ key: string; policy: FeaturePolicy; members: FeatureMember[] }> => {
    const { data } = await client.post(`/api/admin/feature-access/${encodeURIComponent(key)}/preview`, body);
    return data;
};

export const saveFeaturePolicy = async (key: string, body: PolicyInput): Promise<FeatureAccessEntry> => {
    const { data } = await client.put<FeatureAccessEntry>(`/api/admin/feature-access/${encodeURIComponent(key)}`, body);
    return data;
};

export const resetFeaturePolicy = async (key: string): Promise<FeatureAccessEntry> => {
    const { data } = await client.delete<FeatureAccessEntry>(`/api/admin/feature-access/${encodeURIComponent(key)}`);
    return data;
};
