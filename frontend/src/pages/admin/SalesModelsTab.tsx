import React, { useEffect, useMemo, useState } from 'react';
import { Save, RefreshCw, AlertCircle, CheckCircle2, RotateCcw } from 'lucide-react';
import client from '../../api/client';

// ─── Types matching backend/routers/sales_admin.py ───────────────────────────

interface SalesRoleSpec {
    key: string;
    label: string;
    group: string;
    description: string;
    tier: string;
    default_model: string;
    models: string[];
    /** Why this job offers fewer models than the full list, if it does. */
    choices_note?: string;
}

interface SalesCatalog {
    roles: SalesRoleSpec[];
    all_models: string[];
}

interface SalesConfig {
    schema_version: number;
    roles: Record<string, { model_id: string }>;
}

/** FastAPI puts the reason in `detail`; fall back to the transport error. */
const errorMessage = (e: unknown, fallback: string): string => {
    const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (e instanceof Error && e.message) return e.message;
    return fallback;
};

// ─── Component ───────────────────────────────────────────────────────────────

export const SalesModelsTab: React.FC = () => {
    const [config, setConfig] = useState<SalesConfig | null>(null);
    const [catalog, setCatalog] = useState<SalesCatalog | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState<string | null>(null);

    const fetchData = async () => {
        setLoading(true);
        setError(null);
        try {
            const [cfgRes, catRes] = await Promise.all([
                client.get<SalesConfig>('/api/admin/sales/config'),
                client.get<SalesCatalog>('/api/admin/sales/catalog'),
            ]);
            setConfig(cfgRes.data);
            setCatalog(catRes.data);
        } catch (e: unknown) {
            setError(errorMessage(e, 'Failed to load sales model config'));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchData();
    }, []);

    // Roles arrive flat and ordered; the backend's `group` is what sections them,
    // so adding a role server-side needs no change here.
    const groups = useMemo(() => {
        if (!catalog) return [];
        const order: string[] = [];
        const byGroup = new Map<string, SalesRoleSpec[]>();
        for (const role of catalog.roles) {
            if (!byGroup.has(role.group)) {
                byGroup.set(role.group, []);
                order.push(role.group);
            }
            byGroup.get(role.group)!.push(role);
        }
        return order.map(title => ({ title, roles: byGroup.get(title)! }));
    }, [catalog]);

    const setRoleModel = (key: string, modelId: string) => {
        if (!config) return;
        setConfig({
            ...config,
            roles: { ...config.roles, [key]: { ...config.roles[key], model_id: modelId } },
        });
    };

    const handleSave = async () => {
        if (!config) return;
        setSaving(true);
        setError(null);
        setSuccess(null);
        try {
            const res = await client.put<SalesConfig>('/api/admin/sales/config', {
                roles: config.roles,
            });
            setConfig(res.data);
            setSuccess('Saved — the next proposal, call wrap-up or report uses it. No redeploy.');
        } catch (e: unknown) {
            setError(errorMessage(e, 'Failed to save'));
        } finally {
            setSaving(false);
        }
    };

    const handleResetDefaults = () => {
        if (!config || !catalog) return;
        const roles: SalesConfig['roles'] = {};
        for (const role of catalog.roles) roles[role.key] = { model_id: role.default_model };
        setConfig({ ...config, roles });
        setSuccess(null);
    };

    if (loading) {
        return <div className="p-6 text-gray-500">Loading sales model settings…</div>;
    }
    if (!config || !catalog) {
        return (
            <div className="p-6 text-red-600 flex items-center gap-2">
                <AlertCircle className="h-5 w-5" /> {error || 'Could not load sales model config.'}
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <div>
                <h3 className="text-lg font-semibold text-gray-900">Sales models</h3>
                <p className="text-sm text-gray-500 mt-1">
                    Which Gemini model powers each sales feature, and the Deep Prep brief
                    every team gets. Only GA models with
                    provisioned quota are listed — preview models are excluded because they
                    hit rate limits under load. Changes apply to the next run, no redeploy.
                </p>
                <p className="text-sm text-gray-500 mt-2">
                    Each job is set separately on purpose: the steps that read one source at a
                    time are cheap and run many times per report, while the steps that write
                    something a client reads are worth a stronger model. Moving a job down a
                    tier is also the fastest lever if a model starts rate-limiting.
                </p>
            </div>

            {error && (
                <div className="rounded-md bg-red-50 p-3 text-sm text-red-700 flex items-center gap-2">
                    <AlertCircle className="h-4 w-4" /> {error}
                </div>
            )}
            {success && (
                <div className="rounded-md bg-green-50 p-3 text-sm text-green-700 flex items-center gap-2">
                    <CheckCircle2 className="h-4 w-4" /> {success}
                </div>
            )}

            {groups.map(group => (
                <div key={group.title} className="border border-gray-200 rounded-lg overflow-hidden">
                    <div className="bg-gray-50 px-4 py-2 border-b border-gray-200">
                        <h4 className="text-sm font-semibold text-gray-700">{group.title}</h4>
                    </div>
                    <div className="divide-y divide-gray-100">
                        {group.roles.map(role => {
                            const current = config.roles[role.key]?.model_id || role.default_model;
                            const changed = current !== role.default_model;
                            return (
                                <div
                                    key={role.key}
                                    className="px-4 py-3 flex flex-col md:flex-row md:items-start gap-3"
                                >
                                    <div className="md:w-1/2">
                                        <div className="flex items-center gap-2">
                                            <span className="text-sm font-medium text-gray-900">
                                                {role.label}
                                            </span>
                                            {changed && (
                                                <span className="text-[10px] uppercase tracking-wide bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded">
                                                    changed
                                                </span>
                                            )}
                                        </div>
                                        <p className="text-xs text-gray-500 mt-1">{role.description}</p>
                                    </div>
                                    <div className="md:w-1/2">
                                        <select
                                            value={current}
                                            onChange={e => setRoleModel(role.key, e.target.value)}
                                            className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm"
                                        >
                                            {/* Keep the stored value selectable even if it has
                                                since left the catalog, so opening this page
                                                never silently rewrites it. */}
                                            {!role.models.includes(current) && (
                                                <option value={current}>{current} (current)</option>
                                            )}
                                            {role.models.map(m => (
                                                <option key={m} value={m}>
                                                    {m}
                                                    {m === role.default_model ? ' (default)' : ''}
                                                </option>
                                            ))}
                                        </select>
                                        <p className="text-xs text-gray-400 mt-1">
                                            Default: <code>{role.default_model}</code>
                                            {role.choices_note && ` · ${role.choices_note}`}
                                        </p>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                </div>
            ))}

            <div className="flex items-center gap-3">
                <button
                    onClick={handleSave}
                    disabled={saving}
                    className="flex items-center gap-2 bg-brand text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-brand-hover disabled:opacity-50"
                >
                    <Save className="h-4 w-4" /> {saving ? 'Saving…' : 'Save'}
                </button>
                <button
                    onClick={fetchData}
                    className="flex items-center gap-2 border border-gray-200 px-4 py-2 rounded-lg text-sm font-medium text-gray-700 hover:bg-gray-50"
                >
                    <RefreshCw className="h-4 w-4" /> Refresh
                </button>
                <button
                    onClick={handleResetDefaults}
                    className="flex items-center gap-2 border border-gray-200 px-4 py-2 rounded-lg text-sm font-medium text-gray-700 hover:bg-gray-50"
                >
                    <RotateCcw className="h-4 w-4" /> Reset to defaults
                </button>
            </div>
        </div>
    );
};
