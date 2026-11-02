import { useEffect, useRef, useState } from 'react';
import { impersonationHeaders, isTokenValid } from '../../api/client';
import { activeToken } from '../../brand/storage';
import { pinnedWorkspaceClient } from './pinnedWorkspaceClient';
import { saveWorkspaceChanges, tokenMatchesWorkspace } from './workspaceOperationsRequest';

const TEXT_FIELDS = [
    'loop_closure_scorecard_metric',
    'ingestion_council_department',
    'deep_prep_sender_name',
    'deep_prep_sender_mailbox',
    'l10_sender_mailbox',
    'l10_start_after',
] as const;
const LIST_FIELDS = ['ingestion_council_recipients', 'insights_excluded_emails'] as const;
type TextField = (typeof TEXT_FIELDS)[number];
type ListField = (typeof LIST_FIELDS)[number];
type Field = TextField | ListField;
type Form = Record<Field, string> & { time_zone: string };
const FIELDS: readonly Field[] = [...TEXT_FIELDS, ...LIST_FIELDS];

function operationsForm(raw: unknown): Omit<Form, 'time_zone'> {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
        throw new Error('Workspace operations response is invalid.');
    }
    const values = raw as Record<string, unknown>;
    const form = {} as Omit<Form, 'time_zone'>;
    for (const key of TEXT_FIELDS) {
        if (values[key] != null && typeof values[key] !== 'string') {
            throw new Error('Workspace operations response is invalid.');
        }
        form[key] = (values[key] as string | null) ?? '';
    }
    for (const key of LIST_FIELDS) {
        const list = values[key];
        if (!Array.isArray(list) || !list.every(item => typeof item === 'string')) {
            throw new Error('Workspace operations response is invalid.');
        }
        form[key] = list.join('\n');
    }
    return form;
}

function timeZoneForm(raw: unknown): string {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw) ||
        typeof (raw as { time_zone?: unknown }).time_zone !== 'string') {
        throw new Error('Workspace time zone response is invalid.');
    }
    return (raw as { time_zone: string }).time_zone;
}

function errorText(error: unknown, fallback: string): string {
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return typeof detail === 'string' && detail.trim() ? detail : fallback;
}

function splitEmails(value: string): string[] {
    return value.split(/[,\n]/).map(item => item.trim()).filter(Boolean);
}

export function WorkspaceOperationsCard({ workspaceSlug }: { workspaceSlug: string | null }) {
    const [form, setForm] = useState<Form | null>(null);
    const [baseline, setBaseline] = useState<Form | null>(null);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
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
        setMessage(null);
        setSaving(false);
        setLoading(false);
        loadedIdentity.current = null;
        if (!workspaceSlug || !token || !isTokenValid(token) || !tokenMatchesWorkspace(token, workspaceSlug, import.meta.env.DEV)) return;
        let cancelled = false;
        const epoch = currentIdentity.current.epoch;
        const identity = { slug: workspaceSlug, token, headers: impersonationHeaders() };
        const request = pinnedWorkspaceClient(identity);
        const isCurrent = () => currentIdentity.current.epoch === epoch && activeToken() === token;
        setLoading(true);
        Promise.all([
            request.get('/api/users/workspace/operations'),
            request.get('/api/users/workspace/time-zone'),
        ]).then(([operations, time]) => {
            const loaded: Form = { ...operationsForm(operations.data), time_zone: timeZoneForm(time.data) };
            if (!cancelled && isCurrent()) {
                setForm(loaded);
                setBaseline(loaded);
                loadedIdentity.current = { slug: workspaceSlug, token, epoch };
            }
        }).catch(error => {
            if (!cancelled && isCurrent()) setMessage({ kind: 'error', text: errorText(error, 'Could not load workspace operations.') });
        }).finally(() => { if (!cancelled && isCurrent()) setLoading(false); });
        return () => { cancelled = true; };
    }, [workspaceSlug, token]);

    const update = (key: keyof Form, value: string) => {
        setForm(previous => previous ? { ...previous, [key]: value } : previous);
        setMessage(null);
    };

    const changed = form && baseline ? FIELDS.filter(key => form[key] !== baseline[key]) : [];
    const timeChanged = !!(form && baseline && form.time_zone !== baseline.time_zone);

    const save = async () => {
        if (!form || !baseline || !workspaceSlug || saving || (!changed.length && !timeChanged)) return;
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
        const payload: Record<string, string | string[] | null> = {};
        for (const key of changed) {
            payload[key] = key === 'ingestion_council_recipients' || key === 'insights_excluded_emails'
                ? splitEmails(form[key])
                : key === 'l10_start_after' ? form[key].trim() || null : form[key].trim();
        }
        setSaving(true);
        setMessage(null);
        try {
            let nextOperations = baseline;
            let nextTime = baseline.time_zone;
            const result = await saveWorkspaceChanges(
                (path, data) => request.patch(path, data),
                identity,
                changed.length ? payload : null,
                timeChanged ? form.time_zone.trim() : null,
                isCurrent,
            );
            if (result.cancelled) return;
            if (result.operations !== null) nextOperations = { ...operationsForm(result.operations), time_zone: baseline.time_zone };
            if (result.timeZone !== null) nextTime = timeZoneForm(result.timeZone);
            if (isCurrent()) {
                const saved = { ...nextOperations, time_zone: nextTime };
                setForm(saved);
                setBaseline(saved);
                setMessage({ kind: 'success', text: 'Workspace operations saved.' });
            }
        } catch (error) {
            if (isCurrent()) {
                setMessage({ kind: 'error', text: errorText(error, 'Could not save workspace operations.') });
            }
        } finally {
            if (currentIdentity.current.epoch === epoch) setSaving(false);
        }
    };

    const input = (key: keyof Form, label: string, help: string, multiline = false) => (
        <div key={key} className="space-y-1">
            <label htmlFor={`workspace-operations-${key}`} className="block text-sm font-medium text-gray-800">{label}</label>
            {multiline ? (
                <textarea id={`workspace-operations-${key}`} value={form?.[key] ?? ''} onChange={event => update(key, event.target.value)}
                    disabled={!form || saving} rows={3}
                    className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20 disabled:bg-gray-50" />
            ) : (
                <input id={`workspace-operations-${key}`} value={form?.[key] ?? ''} onChange={event => update(key, event.target.value)}
                    disabled={!form || saving}
                    className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20 disabled:bg-gray-50" />
            )}
            <p className="text-xs text-gray-500">{help}</p>
        </div>
    );

    return (
        <section data-tour="admin-workspace-operations" className="max-w-2xl rounded-xl border border-gray-200 bg-white p-5 space-y-5">
            <div>
                <h3 className="text-lg font-semibold text-gray-900">Workspace operations</h3>
                <p className="text-sm text-gray-500">Set the local clock, reporting audience and sender details for this company.</p>
            </div>
            {!workspaceSlug && <p className="text-sm text-amber-700">Workspace identity is unavailable. Operations cannot be edited yet.</p>}
            {loading && <p className="text-sm text-gray-500" role="status">Loading workspace operations…</p>}
            {message && <p role={message.kind === 'error' ? 'alert' : 'status'} className={message.kind === 'error' ? 'text-sm text-red-700' : 'text-sm text-green-700'}>{message.text}</p>}
            <div className="grid gap-4 sm:grid-cols-2">
                {input('time_zone', 'Business time zone', 'IANA name, for example America/Costa_Rica. Used for business days and scheduled reports.')}
                {input('loop_closure_scorecard_metric', 'Loose Ends Scorecard metric', 'Exact measurable name to update. Leave blank to stop Scorecard updates.')}
                {input('ingestion_council_department', 'Ingestion health team', 'Department whose members get ingestion health reports when no recipients are listed.')}
                {input('ingestion_council_recipients', 'Ingestion health recipients', 'Workspace user email addresses, one per line.', true)}
            </div>
            <details className="rounded-lg border border-gray-200 bg-gray-50 px-4 py-3">
                <summary className="cursor-pointer text-sm font-semibold text-gray-800">Delivery and Insights settings</summary>
                <div className="mt-4 grid gap-4 sm:grid-cols-2">
                    {input('insights_excluded_emails', 'Exclude from Insights counts', 'Workspace user email addresses, one per line.', true)}
                    {input('deep_prep_sender_name', 'Deep Prep sender name', 'Name shown on Deep Prep emails.')}
                    {input('deep_prep_sender_mailbox', 'Deep Prep sender mailbox', 'Verified workspace mailbox used to send Deep Prep.')}
                    {input('l10_sender_mailbox', 'L10 sender mailbox', 'Verified workspace mailbox used to send L10 recaps.')}
                    {input('l10_start_after', 'L10 recap start date', 'ISO date or datetime. No recaps are sent until this is set.')}
                </div>
            </details>
            <div className="flex justify-end">
                <button type="button" onClick={save} disabled={!form || saving || !workspaceSlug || (!changed.length && !timeChanged)}
                    className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-50">
                    {saving ? 'Saving…' : 'Save workspace operations'}
                </button>
            </div>
        </section>
    );
}
