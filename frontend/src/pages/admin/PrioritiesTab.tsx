import React, { useEffect, useMemo, useState } from 'react';
import { Save, RefreshCw, AlertCircle, CheckCircle2, Sparkles, Key } from 'lucide-react';
import client from '../../api/client';
import { PlaybooksSection } from './PlaybooksSection';
import { SlackDeliverySection } from './SlackDeliverySection';

// ─── Types matching backend/routers/priorities_admin.py ──────────────────────

interface AgentSettings {
    provider: string;
    model_id: string;
    temperature: number;
}

interface ProviderSettings {
    enabled: boolean;
    api_key_configured?: boolean;
    api_key?: string;  // write-only
}

interface PolicySettings {
    revision_threshold: number;
    max_revisions: number;
}

interface PrioritiesConfig {
    schema_version: number;
    agents: Record<string, AgentSettings>;
    policy: PolicySettings;
    providers: Record<string, ProviderSettings>;
}

interface PrioritiesCatalog {
    agent_roles: string[];
    channel_agent_roles: string[];
    pipeline_agent_roles: string[];
    supported_providers: string[];
    provider_model_catalog: Record<string, string[]>;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

const ROLE_LABELS: Record<string, string> = {
    gmail_agent: 'Gmail channel',
    calendar_agent: 'Calendar channel',
    drive_agent: 'Drive channel',
    meet_agent: 'Meet channel',
    summarizer: 'Summarizer',
    decision_maker: 'Decision-maker',
    critic: 'Critic',
};

const ROLE_BLURBS: Record<string, string> = {
    gmail_agent: 'Reads unread/recent threads, flags items needing attention.',
    calendar_agent: 'Reviews upcoming events; flags externals and prep gaps.',
    drive_agent: 'Surfaces recent Drive activity tied to upcoming meetings.',
    meet_agent: 'Looks at recent Meet transcripts; pulls open commitments.',
    summarizer: 'Merges channel digests into one cross-channel narrative.',
    decision_maker: 'Picks top priorities with reasoning + suggested actions.',
    critic: 'Scores 0-10; a low score sends the list back for revision, then re-scores it.',
};

const PROVIDER_LABELS: Record<string, string> = {
    vertex: 'Google Vertex AI (default)',
    anthropic: 'Anthropic Claude',
    openai: 'OpenAI',
    deepseek: 'DeepSeek',
};

// ─── Component ───────────────────────────────────────────────────────────────

export const PrioritiesTab: React.FC = () => {
    const [config, setConfig] = useState<PrioritiesConfig | null>(null);
    const [catalog, setCatalog] = useState<PrioritiesCatalog | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState<string | null>(null);
    const [apiKeyDrafts, setApiKeyDrafts] = useState<Record<string, string>>({});

    const fetchData = async () => {
        setLoading(true);
        setError(null);
        try {
            const [cfgRes, catRes] = await Promise.all([
                client.get<PrioritiesConfig>('/api/admin/priorities/config'),
                client.get<PrioritiesCatalog>('/api/admin/priorities/catalog'),
            ]);
            setConfig(cfgRes.data);
            setCatalog(catRes.data);
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Failed to load config');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchData();
    }, []);

    const updateAgent = (role: string, patch: Partial<AgentSettings>) => {
        if (!config) return;
        setConfig({
            ...config,
            agents: {
                ...config.agents,
                [role]: { ...config.agents[role], ...patch },
            },
        });
    };

    const updatePolicy = (patch: Partial<PolicySettings>) => {
        if (!config) return;
        setConfig({ ...config, policy: { ...config.policy, ...patch } });
    };

    const updateProvider = (provider: string, patch: Partial<ProviderSettings>) => {
        if (!config) return;
        setConfig({
            ...config,
            providers: {
                ...config.providers,
                [provider]: { ...config.providers[provider], ...patch },
            },
        });
    };

    const handleSave = async () => {
        if (!config) return;
        setSaving(true);
        setError(null);
        setSuccess(null);
        try {
            // Bake any pending API-key drafts into the payload (write-only).
            const providers: Record<string, ProviderSettings> = {};
            for (const [provider, entry] of Object.entries(config.providers)) {
                const draft = apiKeyDrafts[provider];
                providers[provider] = { ...entry };
                if (draft && draft.trim()) {
                    providers[provider].api_key = draft.trim();
                }
            }
            const payload = { ...config, providers };
            const res = await client.put<PrioritiesConfig>(
                '/api/admin/priorities/config', payload
            );
            setConfig(res.data);
            setApiKeyDrafts({});
            setSuccess('Settings saved. New runs will use the updated config immediately.');
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Save failed');
        } finally {
            setSaving(false);
        }
    };

    const channelRoles = catalog?.channel_agent_roles || [];
    const pipelineRoles = catalog?.pipeline_agent_roles || [];

    const modelOptionsFor = (provider: string): string[] =>
        catalog?.provider_model_catalog[provider] || [];

    const dirty = useMemo(
        () => Object.values(apiKeyDrafts).some(v => v && v.trim()),
        [apiKeyDrafts]
    );

    if (loading) {
        return (
            <div className="flex items-center justify-center py-12">
                <RefreshCw className="h-6 w-6 animate-spin text-gray-400" />
            </div>
        );
    }

    if (!config || !catalog) {
        return (
            <div className="p-4 bg-red-50 text-red-700 rounded-lg flex items-center gap-2">
                <AlertCircle className="h-5 w-5" />
                <span>{error || 'Could not load AI Priorities config'}</span>
            </div>
        );
    }

    return (
        <div className="space-y-8">
            {/* Header */}
            <div className="flex items-start justify-between gap-4">
                <div>
                    <h2 className="text-xl font-semibold text-gray-900 flex items-center gap-2">
                        <Sparkles className="h-5 w-5 text-amber-500" />
                        AI Priorities — Multi-Agent Pipeline
                    </h2>
                    <p className="text-sm text-gray-500 mt-1 max-w-2xl">
                        Tune each agent's model, temperature, and revision policy.
                        Changes apply to the very next pipeline run — no restart needed.
                    </p>
                </div>
                <button
                    onClick={handleSave}
                    disabled={saving}
                    className="inline-flex items-center gap-2 px-4 py-2 bg-brand text-white text-sm font-medium rounded-lg hover:bg-brand-hover disabled:opacity-50 transition-colors"
                >
                    {saving ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                    Save changes
                </button>
            </div>

            {/* Alerts */}
            {error && (
                <div className="p-3 bg-red-50 text-red-700 rounded-lg flex items-center gap-2 text-sm">
                    <AlertCircle className="h-4 w-4" /> {error}
                </div>
            )}
            {success && (
                <div className="p-3 bg-green-50 text-green-700 rounded-lg flex items-center gap-2 text-sm">
                    <CheckCircle2 className="h-4 w-4" /> {success}
                </div>
            )}

            {/* Agent roles — channels */}
            <Section
                title="Channel agents"
                subtitle="One agent per data source. Each runs in parallel during the pipeline's fan-out phase."
                roles={channelRoles}
                config={config}
                catalog={catalog}
                onChange={updateAgent}
                roleLabels={ROLE_LABELS}
                roleBlurbs={ROLE_BLURBS}
                modelOptionsFor={modelOptionsFor}
            />

            {/* Agent roles — pipeline */}
            <Section
                title="Pipeline agents"
                subtitle="Sequential stages after channels finish. Critic gates the decision-maker with a bounded revision loop."
                roles={pipelineRoles}
                config={config}
                catalog={catalog}
                onChange={updateAgent}
                roleLabels={ROLE_LABELS}
                roleBlurbs={ROLE_BLURBS}
                modelOptionsFor={modelOptionsFor}
            />

            {/* Policy */}
            <div className="bg-white border border-gray-100 rounded-xl p-5">
                <h3 className="text-sm font-semibold text-gray-900 mb-1">Revision policy</h3>
                <p className="text-xs text-gray-500 mb-4">
                    If the critic scores below the threshold, the decision-maker re-runs with the critic's feedback in its prompt and the critic re-scores the result — up to <code>max revisions</code> rounds, stopping early once the score clears the threshold or a round fails to improve it.
                </p>
                <div className="grid grid-cols-2 gap-4">
                    <Field label={`Revision threshold (${config.policy.revision_threshold.toFixed(1)} / 10)`}>
                        <input
                            type="range" min={0} max={10} step={0.5}
                            value={config.policy.revision_threshold}
                            onChange={e => updatePolicy({ revision_threshold: parseFloat(e.target.value) })}
                            className="w-full"
                        />
                    </Field>
                    <Field label={`Max revisions (${config.policy.max_revisions})`}>
                        <input
                            type="range" min={0} max={3} step={1}
                            value={config.policy.max_revisions}
                            onChange={e => updatePolicy({ max_revisions: parseInt(e.target.value, 10) })}
                            className="w-full"
                        />
                    </Field>
                </div>
            </div>

            {/* Providers */}
            <div className="bg-white border border-gray-100 rounded-xl p-5">
                <h3 className="text-sm font-semibold text-gray-900 mb-1 flex items-center gap-2">
                    <Key className="h-4 w-4" />
                    Provider API keys
                </h3>
                <p className="text-xs text-gray-500 mb-4">
                    Vertex AI is wired by default via the service account. Anthropic / OpenAI / DeepSeek require an API key — pasting one here registers it (the raw value is never echoed back).
                </p>
                <div className="space-y-3">
                    {catalog.supported_providers.map(provider => {
                        const entry = config.providers[provider] || { enabled: provider === 'vertex' };
                        const draft = apiKeyDrafts[provider] || '';
                        const configured = entry.api_key_configured || provider === 'vertex';
                        return (
                            <div key={provider} className="flex items-center gap-3 py-2 border-b border-gray-50 last:border-b-0">
                                <div className="w-40 flex-shrink-0">
                                    <div className="text-sm font-medium text-gray-900">
                                        {PROVIDER_LABELS[provider] || provider}
                                    </div>
                                    <div className="text-[11px] text-gray-500 mt-0.5">
                                        {configured ? (
                                            <span className="inline-flex items-center gap-1 text-green-600">
                                                <CheckCircle2 className="h-3 w-3" /> Configured
                                            </span>
                                        ) : (
                                            <span className="text-gray-400">Not configured</span>
                                        )}
                                    </div>
                                </div>
                                <input
                                    type="password"
                                    placeholder={provider === 'vertex' ? 'Managed by service account' : 'Paste API key to update'}
                                    disabled={provider === 'vertex'}
                                    value={draft}
                                    onChange={e =>
                                        setApiKeyDrafts({ ...apiKeyDrafts, [provider]: e.target.value })
                                    }
                                    className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-md disabled:bg-gray-50 disabled:text-gray-400"
                                />
                                <label className="flex items-center gap-1.5 text-xs text-gray-600">
                                    <input
                                        type="checkbox"
                                        checked={entry.enabled || false}
                                        disabled={provider === 'vertex'}
                                        onChange={e => updateProvider(provider, { enabled: e.target.checked })}
                                    />
                                    Enabled
                                </label>
                            </div>
                        );
                    })}
                </div>
                {dirty && (
                    <p className="text-[11px] text-amber-700 mt-3">
                        Unsaved API key changes — click <b>Save changes</b> to persist.
                    </p>
                )}
            </div>

            {/* Per-department Slack AI-Priorities delivery opt-in */}
            <SlackDeliverySection />

            {/* UC-23: Department playbooks (admin-managed, multi-per-dept) */}
            <PlaybooksSection />
        </div>
    );
};

// ─── Subcomponents ───────────────────────────────────────────────────────────

interface SectionProps {
    title: string;
    subtitle: string;
    roles: string[];
    config: PrioritiesConfig;
    catalog: PrioritiesCatalog;
    onChange: (role: string, patch: Partial<AgentSettings>) => void;
    roleLabels: Record<string, string>;
    roleBlurbs: Record<string, string>;
    modelOptionsFor: (provider: string) => string[];
}

const Section: React.FC<SectionProps> = ({
    title, subtitle, roles, config, catalog, onChange, roleLabels, roleBlurbs, modelOptionsFor,
}) => (
    <div className="bg-white border border-gray-100 rounded-xl p-5">
        <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        <p className="text-xs text-gray-500 mb-4">{subtitle}</p>
        <div className="space-y-3">
            {roles.map(role => {
                const agent = config.agents[role] || { provider: 'vertex', model_id: 'gemini-flash', temperature: 0.3 };
                const models = modelOptionsFor(agent.provider);
                return (
                    <div key={role} className="grid grid-cols-12 gap-3 items-center py-2 border-b border-gray-50 last:border-b-0">
                        <div className="col-span-4">
                            <div className="text-sm font-medium text-gray-900">{roleLabels[role] || role}</div>
                            <div className="text-[11px] text-gray-500 mt-0.5">{roleBlurbs[role] || ''}</div>
                        </div>
                        <div className="col-span-3">
                            <select
                                value={agent.provider}
                                onChange={e => onChange(role, { provider: e.target.value })}
                                className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md"
                            >
                                {catalog.supported_providers.map(p => (
                                    <option key={p} value={p}>{p}</option>
                                ))}
                            </select>
                        </div>
                        <div className="col-span-3">
                            <select
                                value={agent.model_id}
                                onChange={e => onChange(role, { model_id: e.target.value })}
                                className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md"
                            >
                                {models.includes(agent.model_id) ? null : (
                                    <option value={agent.model_id}>{agent.model_id} (custom)</option>
                                )}
                                {models.map(m => (
                                    <option key={m} value={m}>{m}</option>
                                ))}
                            </select>
                        </div>
                        <div className="col-span-2">
                            <input
                                type="number" min={0} max={2} step={0.05}
                                value={agent.temperature}
                                onChange={e => onChange(role, { temperature: parseFloat(e.target.value) })}
                                className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md"
                            />
                            <div className="text-[10px] text-gray-400 text-center mt-0.5">temperature</div>
                        </div>
                    </div>
                );
            })}
        </div>
    </div>
);

const Field: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
    <div>
        <label className="block text-xs font-medium text-gray-700 mb-1">{label}</label>
        {children}
    </div>
);

export default PrioritiesTab;
