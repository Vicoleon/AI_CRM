import { useEffect, useRef, useState } from 'react';

import { impersonationHeaders, isTokenValid } from '../../api/client';
import { activeToken } from '../../brand/storage';
import { pinnedWorkspaceClient } from './pinnedWorkspaceClient';
import { saveWorkspaceCrmChanges, tokenMatchesWorkspace } from './workspaceOperationsRequest';

const LIST_FIELDS = [
    'pipeline_ids',
    'closed_won_stage_ids',
    'xray_auto_renew_properties',
    'xray_account_status_properties',
    'xray_extra_deal_properties',
    'xray_extra_company_properties',
    'ccs_vendor_fields_default',
    'ccs_vendor_fields_extended',
    'sales_rhythm_late_stages',
] as const;

const TEXT_FIELDS = [
    'stage_lag_stages',
    'portal_id',
    'deal_product_property',
    'deal_turnaround_property',
    'xray_renewal_date_property',
    'xray_csm_owner_property',
    'scorecard_sync_department',
] as const;

type ListField = (typeof LIST_FIELDS)[number];
type TextField = (typeof TEXT_FIELDS)[number];
type FormField = ListField | TextField | 'scorecard_sync_metrics';
type FormState = Record<FormField, string>;
const ALL_FIELDS: readonly FormField[] = [...LIST_FIELDS, ...TEXT_FIELDS, 'scorecard_sync_metrics'];

function toForm(data: unknown): FormState {
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
        throw new Error('HubSpot settings response is invalid.');
    }
    const source = data as Record<string, unknown>;
    const form = {} as FormState;
    for (const field of LIST_FIELDS) {
        const value = source[field];
        if (!Array.isArray(value) || !value.every(item => typeof item === 'string')) {
            throw new Error('HubSpot settings response is invalid.');
        }
        form[field] = value.join('\n');
    }
    for (const field of TEXT_FIELDS) {
        const value = source[field];
        if (value != null && typeof value !== 'string') {
            throw new Error('HubSpot settings response is invalid.');
        }
        form[field] = value ?? '';
    }
    const metrics = source.scorecard_sync_metrics;
    if (!metrics || typeof metrics !== 'object' || Array.isArray(metrics) ||
        !Object.values(metrics).every(value => typeof value === 'string')) {
        throw new Error('HubSpot settings response is invalid.');
    }
    form.scorecard_sync_metrics = Object.entries(metrics)
        .map(([name, recipe]) => `${name} = ${recipe}`)
        .join('\n');
    return form;
}

function splitList(value: string): string[] {
    return value.split(/[,\n]/).map(item => item.trim()).filter(Boolean);
}

function parseMetrics(value: string): Record<string, string> {
    const metrics: Record<string, string> = {};
    for (const line of value.split('\n')) {
        if (!line.trim()) continue;
        const equals = line.indexOf('=');
        const name = line.slice(0, equals).trim();
        const recipe = line.slice(equals + 1).trim();
        if (equals < 1 || !name || !recipe) {
            throw new Error('Write each Scorecard mapping as “metric name = recipe”.');
        }
        if (Object.hasOwn(metrics, name)) {
            throw new Error(`Scorecard metric “${name}” appears more than once.`);
        }
        metrics[name] = recipe;
    }
    return metrics;
}

function toPayload(form: FormState): Record<string, string | string[] | Record<string, string>> {
    const payload: Record<string, string | string[] | Record<string, string>> = {};
    for (const field of LIST_FIELDS) payload[field] = splitList(form[field]);
    for (const field of TEXT_FIELDS) payload[field] = form[field].trim();
    payload.scorecard_sync_metrics = parseMetrics(form.scorecard_sync_metrics);
    return payload;
}

function requestError(error: unknown, fallback: string): string {
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return typeof detail === 'string' && detail.trim() ? detail : fallback;
}

export function HubSpotSettingsCard({ workspaceSlug }: { workspaceSlug: string | null }) {
    const [form, setForm] = useState<FormState | null>(null);
    const [baseline, setBaseline] = useState<FormState | null>(null);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [pipelineEditable, setPipelineEditable] = useState(false);
    const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
    const token = activeToken();
    const currentIdentity = useRef({ slug: workspaceSlug, token, epoch: 0 });
    if (currentIdentity.current.slug !== workspaceSlug || currentIdentity.current.token !== token) {
        currentIdentity.current = { slug: workspaceSlug, token, epoch: currentIdentity.current.epoch + 1 };
    }
    const loadedIdentity = useRef<{ slug: string; token: string; epoch: number } | null>(null);

    useEffect(() => {
        setForm(null);
        setBaseline(null);
        setSaving(false);
        setLoading(false);
        setPipelineEditable(false);
        setMessage(null);
        loadedIdentity.current = null;
        if (!workspaceSlug || !token || !isTokenValid(token) || !tokenMatchesWorkspace(token, workspaceSlug, import.meta.env.DEV)) return;
        let cancelled = false;
        const epoch = currentIdentity.current.epoch;
        const identity = { slug: workspaceSlug, token, headers: impersonationHeaders() };
        const request = pinnedWorkspaceClient(identity);
        const isCurrent = () => currentIdentity.current.epoch === epoch && activeToken() === token;
        setLoading(true);
        request.get('/api/users/workspace/crm-settings')
            .then(response => {
                const loaded = toForm(response.data);
                if (!cancelled && isCurrent()) {
                    setForm(loaded);
                    setBaseline(loaded);
                    setPipelineEditable(response.data?.pipeline_ids_editable === true);
                    loadedIdentity.current = { slug: workspaceSlug, token, epoch };
                }
            })
            .catch(error => {
                if (!cancelled && isCurrent()) setMessage({ kind: 'error', text: requestError(error, 'Could not load HubSpot settings.') });
            })
            .finally(() => { if (!cancelled && isCurrent()) setLoading(false); });
        return () => { cancelled = true; };
    }, [workspaceSlug, token]);

    const update = (field: FormField, value: string) => {
        setForm(current => current ? { ...current, [field]: value } : current);
        setMessage(null);
    };

    const changedFields = form && baseline ? ALL_FIELDS.filter(key => form[key] !== baseline[key]) : [];

    const save = async () => {
        if (!form || !baseline || !workspaceSlug || saving || changedFields.length === 0) return;
        const requestToken = activeToken();
        const epoch = currentIdentity.current.epoch;
        const loaded = loadedIdentity.current;
        if (!requestToken || !isTokenValid(requestToken) || !tokenMatchesWorkspace(requestToken, workspaceSlug, import.meta.env.DEV) || !loaded || loaded.slug !== workspaceSlug || loaded.token !== requestToken || loaded.epoch !== epoch) {
            setMessage({ kind: 'error', text: 'Workspace changed. Reload settings before saving.' });
            return;
        }
        const identity = { slug: workspaceSlug, token: requestToken, headers: impersonationHeaders() };
        const request = pinnedWorkspaceClient(identity);
        const isCurrent = () => currentIdentity.current.epoch === epoch && activeToken() === requestToken;
        let payload: ReturnType<typeof toPayload>;
        try {
            const allValues = toPayload(form);
            payload = Object.fromEntries(changedFields.map(key => [key, allValues[key]]));
        } catch (error) {
            setMessage({ kind: 'error', text: error instanceof Error ? error.message : 'Check the HubSpot settings.' });
            return;
        }
        setSaving(true);
        setMessage(null);
        try {
            const response = await saveWorkspaceCrmChanges((path, data) => request.patch(path, data), identity, payload, isCurrent);
            if (!response) return;
            const saved = toForm(response.data);
            if (isCurrent()) {
                setForm(saved);
                setBaseline(saved);
                setMessage({ kind: 'success', text: 'HubSpot settings saved for this workspace.' });
            }
        } catch (error) {
            if (isCurrent()) {
                setMessage({ kind: 'error', text: requestError(error, 'Could not save HubSpot settings.') });
            }
        } finally {
            if (currentIdentity.current.epoch === epoch) setSaving(false);
        }
    };

    const field = (key: FormField, label: string, help: string, multiline = false) => (
        <div className="space-y-1" key={key}>
            <label htmlFor={`hubspot-${key}`} className="block text-sm font-medium text-gray-800">{label}</label>
            {multiline ? (
                <textarea
                    id={`hubspot-${key}`}
                    value={form?.[key] ?? ''}
                    onChange={event => update(key, event.target.value)}
                    disabled={!form || saving || (key === 'pipeline_ids' && !pipelineEditable)}
                    rows={3}
                    className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20 disabled:bg-gray-50"
                />
            ) : (
                <input
                    id={`hubspot-${key}`}
                    value={form?.[key] ?? ''}
                    onChange={event => update(key, event.target.value)}
                    disabled={!form || saving || (key === 'pipeline_ids' && !pipelineEditable)}
                    className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20 disabled:bg-gray-50"
                />
            )}
            <p className="text-xs text-gray-500">{help}</p>
        </div>
    );

    return (
        <section data-tour="admin-crm-settings" className="max-w-2xl rounded-xl border border-gray-200 bg-white p-5 space-y-5">
            <div>
                <h3 className="text-lg font-semibold text-gray-900">HubSpot settings</h3>
                <p className="text-sm text-gray-500">Choose which of this workspace&apos;s deals Cubie may read and update. Connect the HubSpot account separately before using CRM features.</p>
            </div>
            {!workspaceSlug && <p className="text-sm text-amber-700">Workspace identity is unavailable. Settings cannot be edited yet.</p>}
            {loading && <p className="text-sm text-gray-500" role="status">Loading HubSpot settings…</p>}
            {message && <p role={message.kind === 'error' ? 'alert' : 'status'} className={message.kind === 'error' ? 'text-sm text-red-700' : 'text-sm text-green-700'}>{message.text}</p>}

            <div className="space-y-4">
                {field('pipeline_ids', 'Deal pipeline IDs', pipelineEditable
                    ? 'Platform support can assign the pipelines Cubie may read and update. Enter one ID per line, or separate IDs with commas.'
                    : 'Platform support assigns the pipelines Cubie may read and update. Ask them to change this access.', true)}
                <div className="grid gap-4 sm:grid-cols-2">
                    {field('portal_id', 'HubSpot portal ID', 'Used for links back to your HubSpot account. Leave blank to hide those links.')}
                    {field('closed_won_stage_ids', 'Additional won stage IDs', 'Stages your team treats as won, beyond HubSpot’s built-in won stage.', true)}
                </div>
                {field('stage_lag_stages', 'Pipeline stages in order', 'For Pipeline hygiene: stage ID, optionally followed by = and a call signal (sow_sent, moved_to_legal_review, ops_involved, pending_signature or verbal_agreement). Separate stages with commas.')}
            </div>

            <details className="rounded-lg border border-gray-200 bg-gray-50 px-4 py-3">
                <summary className="cursor-pointer text-sm font-semibold text-gray-800">Custom fields and advanced reporting</summary>
                <div className="mt-4 space-y-4">
                    <div className="grid gap-4 sm:grid-cols-2">
                        {field('deal_product_property', 'Deal product field', 'HubSpot property name for what a customer bought.')}
                        {field('deal_turnaround_property', 'Deal turnaround field', 'HubSpot property name for turnaround time.')}
                        {field('xray_renewal_date_property', 'Renewal date field', 'Customer X-Ray property name for contract renewal date.')}
                        {field('xray_csm_owner_property', 'Customer success owner field', 'Customer X-Ray property name for the account owner.')}
                    </div>
                    {field('xray_auto_renew_properties', 'Auto-renew fields', 'HubSpot property names, one per line or comma-separated.', true)}
                    {field('xray_account_status_properties', 'Account status fields', 'HubSpot property names, one per line or comma-separated.', true)}
                    {field('xray_extra_deal_properties', 'Additional deal fields', 'Customer X-Ray deal properties to include.', true)}
                    {field('xray_extra_company_properties', 'Additional company fields', 'Customer X-Ray company properties to include.', true)}
                    {field('ccs_vendor_fields_default', 'Call wrap-up vendor fields', 'Company properties a call wrap-up may propose changing.', true)}
                    {field('ccs_vendor_fields_extended', 'Additional vendor fields', 'Extra company properties allowed for extended call wrap-ups.', true)}
                    {field('sales_rhythm_late_stages', 'Late sales stages', 'Stage IDs or labels used to spot a deal waiting on the client.', true)}
                    {field('scorecard_sync_department', 'Scorecard team', 'Team whose Scorecard Cubie may update from HubSpot deals.')}
                    {field('scorecard_sync_metrics', 'Scorecard metric mappings', 'One per line: metric name = recipe. Recipes: company_won_amount, company_won_count, rep_won_amount, rep_won_count, rep_lost_count.', true)}
                </div>
            </details>

            <div className="flex justify-end">
                <button type="button" onClick={save} disabled={!form || saving || !workspaceSlug || changedFields.length === 0} className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-50">
                    {saving ? 'Saving…' : 'Save HubSpot settings'}
                </button>
            </div>
        </section>
    );
}
