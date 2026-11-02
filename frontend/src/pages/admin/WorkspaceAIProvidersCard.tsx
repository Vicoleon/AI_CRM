import { useEffect, useRef, useState } from 'react';
import { impersonationHeaders, isTokenValid } from '../../api/client';
import { activeToken } from '../../brand/storage';
import { pinnedWorkspaceClient } from './pinnedWorkspaceClient';
import { tokenMatchesWorkspace } from './workspaceOperationsRequest';
import { formBelongsToIdentity } from './workspaceAIProviderIdentity';
import { compatibleRouteProvider } from './workspaceAIProviderSelection';

type ProviderStatus = {
    connected: boolean;
    credential_configured: boolean;
    project?: string | null;
    region?: string | null;
    service_identity?: string | null;
};
type Settings = {
    catalog: Record<string, Record<string, string[]>>;
    workloads: Record<string, string>;
    providers: Record<string, ProviderStatus>;
    routes: Record<string, { provider: string; model: string }>;
};
type EmbeddingIndex = {
    storage: string;
    client_embeddings_available: boolean;
    corpora?: { key: string; label: string }[];
    selected_corpus?: string;
    active: { provider: string; model: string; dimension: number; version: string };
    candidate: { provider: string; model: string; dimension: number; version: string } | null;
};

const LABELS: Record<string, string> = {
    anthropic: 'Claude (Anthropic)', deepseek: 'DeepSeek', openai: 'OpenAI',
    gemini: 'Gemini API', meta: 'Muse (Meta Model API)',
    vertex_client: 'Your Vertex AI project', vertex_managed: 'Cubie-managed Vertex AI',
    ai_priorities: 'AI Priorities', l10_summary_email: 'L10 recap emails',
    insights_summary: 'Insights view summaries', crm_sync: 'CRM Sync extraction',
    crm_retrieval_plan: 'Live CRM retrieval planning',
    call_capture_extraction: 'Call wrap-up extraction',
    proposal_generator: 'Proposal drafting',
    xray_json: 'Customer X-Ray analysis',
    l10_recap: 'L10 meeting recap',
    loop_closure: 'Loose Ends scan and reply drafts',
    xray_grounding: 'Customer X-Ray web research',
    eos_rock_drafts: 'EOS Rock drafts',
    transcript_email_draft: 'Transcript follow-up email draft',
    slack_summaries: 'Slack summaries',
    smart_teams_conversation: 'Smart Teams interview',
    people_persona: 'People profiles',
    people_web_search: 'People public web research',
    open_loops: 'What am I waiting on',
    gmail_summaries: 'Gmail summaries',
    email_importance: 'Email importance triage',
    signals_judge: 'Sales signals',
    dam_description: 'Document descriptions',
    dam_version_summary: 'Document change summaries',
    meeting_skills_extract: 'Meeting skills extraction',
    chat_file_text: 'Chat file reading: text',
    chat_file_vision: 'Chat file reading: images and PDFs',
    voice_transcription: 'Voice transcription',
    voice_command_audio: 'Voice commands: spoken',
    voice_command_text: 'Voice commands: typed',
    meeting_prep_internal: 'Meeting Prep: internal brief',
    meeting_prep_external: 'Meeting Prep: external research',
    meeting_prep_rewrite: 'Meeting Prep: internal rewrite',
    meeting_prep_rewrite_grounded: 'Meeting Prep: external rewrite',
    deep_prep_council: 'Deep Prep council',
    chat_adk: 'Ask Cubie chat',
    chat_web_search: 'Chat public web research',
    chat_impersonation: 'Support chat',
    sales_email_agent: 'Sales email assistant',
    nightly_pipeline: 'Overnight pipeline',
    embedding_index: 'Knowledge search embeddings',
};

function detail(error: unknown): string {
    const value = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return typeof value === 'string' ? value : 'The request failed. Please try again.';
}

export function WorkspaceAIProvidersCard({ workspaceSlug }: { workspaceSlug: string | null }) {
    const [settings, setSettings] = useState<Settings | null>(null);
    const [embeddingIndex, setEmbeddingIndex] = useState<EmbeddingIndex | null>(null);
    const [embeddingCorpus, setEmbeddingCorpus] = useState('default');
    const embeddingCorpusRef = useRef('default');
    const [provider, setProvider] = useState('anthropic');
    const [apiKey, setApiKey] = useState('');
    const [project, setProject] = useState('');
    const [region, setRegion] = useState('');
    const [serviceIdentity, setServiceIdentity] = useState('');
    const [feature, setFeature] = useState('l10_summary_email');
    const [routeProvider, setRouteProvider] = useState('vertex_managed');
    const [routeModel, setRouteModel] = useState('');
    const [embeddingProvider, setEmbeddingProvider] = useState('vertex_managed');
    const [embeddingModel, setEmbeddingModel] = useState('');
    const [busy, setBusy] = useState(false);
    const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
    const token = activeToken();
    const identity = useRef({ workspaceSlug, token, epoch: 0 });
    if (identity.current.workspaceSlug !== workspaceSlug || identity.current.token !== token) {
        identity.current = { workspaceSlug, token, epoch: identity.current.epoch + 1 };
    }
    const [formEpoch, setFormEpoch] = useState(identity.current.epoch);

    useEffect(() => {
        setSettings(null);
        setEmbeddingIndex(null);
        setEmbeddingCorpus('default');
        embeddingCorpusRef.current = 'default';
        setApiKey('');
        setProject('');
        setRegion('');
        setServiceIdentity('');
        setMessage(null);
        setBusy(false);
        setFormEpoch(identity.current.epoch);
        if (!workspaceSlug || !token || !isTokenValid(token) || !tokenMatchesWorkspace(token, workspaceSlug, import.meta.env.DEV)) return;
        const epoch = identity.current.epoch;
        let cancelled = false;
        const request = pinnedWorkspaceClient({ slug: workspaceSlug, token, headers: impersonationHeaders() });
        request.get<Settings>('/api/admin/workspace/ai').then(response => {
            if (!cancelled && identity.current.epoch === epoch && activeToken() === token) {
                setSettings(response.data);
                setServiceIdentity(response.data.providers.vertex_client?.service_identity ?? '');
            }
        }).catch(error => {
            if (!cancelled && identity.current.epoch === epoch) setMessage({ kind: 'error', text: detail(error) });
        });
        request.get<EmbeddingIndex>('/api/admin/workspace/ai/embedding-index').then(response => {
            if (!cancelled && identity.current.epoch === epoch && activeToken() === token && embeddingCorpusRef.current === 'default') setEmbeddingIndex(response.data);
        }).catch(error => {
            if (!cancelled && identity.current.epoch === epoch) setMessage({ kind: 'error', text: detail(error) });
        });
        return () => { cancelled = true; };
    }, [workspaceSlug, token]);

    const requestForCurrentIdentity = () => {
        const currentToken = activeToken();
        if (!formBelongsToIdentity(formEpoch, identity.current.epoch) || !workspaceSlug || !currentToken || !isTokenValid(currentToken) || !tokenMatchesWorkspace(currentToken, workspaceSlug, import.meta.env.DEV) || currentToken !== token) {
            throw new Error('Workspace changed. Reload before saving.');
        }
        return { request: pinnedWorkspaceClient({ slug: workspaceSlug, token: currentToken, headers: impersonationHeaders() }), epoch: identity.current.epoch };
    };
    const isCurrent = (epoch: number) => identity.current.epoch === epoch && activeToken() === token;
    const refresh = async (request: ReturnType<typeof pinnedWorkspaceClient>, epoch: number) => {
        const corpus = embeddingCorpusRef.current;
        const [settingsResponse, indexResponse] = await Promise.all([
            request.get<Settings>('/api/admin/workspace/ai'),
            request.get<EmbeddingIndex>(`/api/admin/workspace/ai/embedding-index?corpus=${encodeURIComponent(corpus)}`),
        ]);
        if (isCurrent(epoch)) {
            setSettings(settingsResponse.data);
            if (embeddingCorpusRef.current === corpus) setEmbeddingIndex(indexResponse.data);
        }
    };
    const chooseEmbeddingCorpus = async (corpus: string) => {
        embeddingCorpusRef.current = corpus;
        setEmbeddingCorpus(corpus);
        setEmbeddingIndex(null);
        setMessage(null);
        try {
            const current = requestForCurrentIdentity();
            const response = await current.request.get<EmbeddingIndex>(`/api/admin/workspace/ai/embedding-index?corpus=${encodeURIComponent(corpus)}`);
            if (isCurrent(current.epoch) && embeddingCorpusRef.current === corpus) setEmbeddingIndex(response.data);
        } catch (error) {
            if (embeddingCorpusRef.current === corpus) setMessage({ kind: 'error', text: detail(error) });
        }
    };
    const run = async (work: (request: ReturnType<typeof pinnedWorkspaceClient>) => Promise<void>, success: string, clearKey = false) => {
        if (busy || !formBelongsToIdentity(formEpoch, identity.current.epoch)) return;
        let epoch = identity.current.epoch;
        setBusy(true);
        setMessage(null);
        try {
            const current = requestForCurrentIdentity();
            epoch = current.epoch;
            await work(current.request);
            if (clearKey && isCurrent(epoch)) setApiKey('');
            await refresh(current.request, epoch);
            if (isCurrent(epoch)) {
                setMessage({ kind: 'success', text: success });
            }
        } catch (error) {
            if (isCurrent(epoch)) setMessage({ kind: 'error', text: detail(error) });
        } finally {
            if (isCurrent(epoch)) setBusy(false);
        }
    };

    const keyProviders = Object.keys(settings?.catalog ?? {}).filter(name => name !== 'vertex_managed');
    const routeProviders = Object.entries(settings?.catalog ?? {}).filter(([, models]) =>
        Object.values(models).some(capabilities => capabilities.includes(settings?.workloads[feature] ?? '')),
    );
    const selectedProvider = compatibleRouteProvider(settings?.catalog ?? {}, settings?.workloads[feature] ?? '', routeProvider);
    const models = Object.entries(settings?.catalog[selectedProvider] ?? {}).filter(([, capabilities]) =>
        capabilities.includes(settings?.workloads[feature] ?? ''),
    ).map(([name]) => name);
    const selectedModel = models.includes(routeModel) ? routeModel : (models[0] ?? '');
    const embeddingCatalog = Object.fromEntries(Object.entries(settings?.catalog ?? {}).filter(([name, availableModels]) =>
        Object.values(availableModels).some(capabilities => capabilities.includes('embedding')) &&
        (embeddingIndex?.storage !== 'cloud_sql_pgvector' || name === 'openai' || name === 'gemini')));
    const embeddingProviders = Object.entries(embeddingCatalog);
    const selectedEmbeddingProvider = compatibleRouteProvider(embeddingCatalog, 'embedding', embeddingProvider);
    const embeddingModels = Object.entries(embeddingCatalog[selectedEmbeddingProvider] ?? {})
        .filter(([, capabilities]) => capabilities.includes('embedding'))
        .map(([name]) => name);
    const selectedEmbeddingModel = embeddingModels.includes(embeddingModel) ? embeddingModel : (embeddingModels[0] ?? '');
    const status = settings?.providers[provider];

    return <section data-tour="admin-workspace-ai-providers" className="max-w-2xl rounded-xl border border-gray-200 bg-white p-5 space-y-5">
        <div>
            <h3 className="text-lg font-semibold text-gray-900">AI provider credentials</h3>
            <p className="text-sm text-gray-600">Use your own account for supported workloads. Keys stay on the server and are never shown again. Each provider bills its account owner directly.</p>
        </div>
        {message && <p role="status" className={message.kind === 'error' ? 'text-sm text-red-700' : 'text-sm text-green-700'}>{message.text}</p>}
        {!settings ? <p className="text-sm text-gray-500">Loading provider settings…</p> : <>
            <div className="space-y-2">
                <label htmlFor="workspace-ai-provider" className="block text-sm font-medium">Provider</label>
                <select id="workspace-ai-provider" disabled={busy} value={provider} onChange={event => { setProvider(event.target.value); setApiKey(''); setMessage(null); }} className="w-full rounded-lg border border-gray-300 p-2 text-sm">
                    {keyProviders.map(name => <option key={name} value={name}>{LABELS[name] ?? name}</option>)}
                </select>
                <p className="text-xs text-gray-600">{status?.connected ? 'Connected' : 'Not connected'} · {status?.credential_configured ? 'Credential configured' : 'No credential configured'}</p>
            </div>
            {provider === 'vertex_client' ? <div className="grid gap-2 sm:grid-cols-2">
                <input aria-label="Vertex project ID" disabled={busy} placeholder="Project ID" value={project} onChange={event => setProject(event.target.value)} className="rounded-lg border border-gray-300 p-2 text-sm" />
                <input aria-label="Vertex region" disabled={busy} placeholder="Region" value={region} onChange={event => setRegion(event.target.value)} className="rounded-lg border border-gray-300 p-2 text-sm" />
                <input aria-label="Vertex service identity" readOnly placeholder="This workspace's service account email" value={serviceIdentity} className="sm:col-span-2 rounded-lg border border-gray-300 bg-gray-50 p-2 text-sm" />
                <p className="sm:col-span-2 text-xs text-gray-600">Grant this workspace’s provisioned service account access to your Vertex project. Other workspaces’ identities and private key files are not accepted.</p>
            </div> : <input type="password" disabled={busy} autoComplete="off" aria-label={`${LABELS[provider] ?? provider} API key`} placeholder="Paste a new API key" value={apiKey} onChange={event => setApiKey(event.target.value)} className="w-full rounded-lg border border-gray-300 p-2 text-sm" />}
            <div className="flex flex-wrap gap-2">
                <button disabled={busy || !formBelongsToIdentity(formEpoch, identity.current.epoch) || (provider === 'vertex_client' ? !project || !region || !serviceIdentity : !apiKey)} onClick={() => run(async request => {
                    await request.put(`/api/admin/workspace/ai/providers/${provider}`, provider === 'vertex_client'
                        ? { project, region, service_identity: serviceIdentity } : { api_key: apiKey });
                }, 'Provider saved. Test a model before selecting it.', true)} className="rounded-lg bg-brand px-3 py-2 text-sm font-medium text-white disabled:opacity-50">Save credential</button>
                <button disabled={busy || !formBelongsToIdentity(formEpoch, identity.current.epoch) || !status?.connected} onClick={() => run(async request => { await request.delete(`/api/admin/workspace/ai/providers/${provider}`); }, 'Provider disconnected.')} className="rounded-lg border border-gray-300 px-3 py-2 text-sm disabled:opacity-50">Disconnect</button>
            </div>
            <div className="border-t border-gray-200 pt-4 space-y-3">
                <h4 className="font-medium text-gray-900">Choose a provider for each workload</h4>
                <p className="text-xs text-gray-600">Only compatible models appear. A selected client provider never falls back to Cubie’s Vertex account if its key or quota fails.</p>
                <div className="grid gap-2 sm:grid-cols-3">
                    <select aria-label="AI workload" disabled={busy} value={feature} onChange={event => { setFeature(event.target.value); setRouteModel(''); }} className="rounded-lg border border-gray-300 p-2 text-sm">
                        {Object.keys(settings.workloads).filter(name => name !== 'embedding_index').map(name => <option key={name} value={name}>{LABELS[name] ?? name}</option>)}
                    </select>
                    <select aria-label="Workload provider" disabled={busy} value={selectedProvider} onChange={event => { setRouteProvider(event.target.value); setRouteModel(''); }} className="rounded-lg border border-gray-300 p-2 text-sm">
                        {routeProviders.map(([name]) => <option key={name} value={name}>{LABELS[name] ?? name}</option>)}
                    </select>
                    <select aria-label="Workload model" disabled={busy} value={selectedModel} onChange={event => setRouteModel(event.target.value)} className="rounded-lg border border-gray-300 p-2 text-sm">
                        {models.map(name => <option key={name} value={name}>{name}</option>)}
                    </select>
                </div>
                <p className="text-xs text-gray-600">Current: {settings.routes[feature] ? `${LABELS[settings.routes[feature].provider] ?? settings.routes[feature].provider} · ${settings.routes[feature].model}` : 'Cubie-managed Vertex AI'}</p>
                <button disabled={busy || !formBelongsToIdentity(formEpoch, identity.current.epoch) || !selectedModel} onClick={() => run(async request => {
                    await request.put(`/api/admin/workspace/ai/routes/${feature}`, { provider: selectedProvider, model: selectedModel });
                }, 'Workload route saved.')} className="rounded-lg bg-brand px-3 py-2 text-sm font-medium text-white disabled:opacity-50">Test and use this model</button>
            </div>
            <div data-tour="admin-embedding-index" className="border-t border-gray-200 pt-4 space-y-3">
                <h4 className="font-medium text-gray-900">Knowledge search embeddings</h4>
                <p className="text-xs text-gray-600">Changing an embedding model rebuilds the search index first. Search keeps using the current index until you activate the verified rebuild.</p>
                {embeddingIndex ? <>
                    {embeddingIndex.corpora && embeddingIndex.corpora.length > 0 && <label className="block text-xs text-gray-600">Knowledge collection
                        <select aria-label="Knowledge collection" disabled={busy} value={embeddingCorpus === 'default' ? embeddingIndex.selected_corpus : embeddingCorpus} onChange={event => { void chooseEmbeddingCorpus(event.target.value); }} className="mt-1 block w-full rounded-lg border border-gray-300 p-2 text-sm">
                            {embeddingIndex.corpora.map(item => <option key={item.key} value={item.key}>{item.label}</option>)}
                        </select>
                    </label>}
                    {embeddingIndex.corpora && embeddingIndex.corpora.length === 0 && <p className="text-xs text-amber-700">No knowledge collection is provisioned for this workspace yet.</p>}
                    <p className="text-xs text-gray-600">Active: {LABELS[embeddingIndex.active.provider] ?? embeddingIndex.active.provider} · {embeddingIndex.active.model} · {embeddingIndex.active.dimension} dimensions</p>
                    {!embeddingIndex.client_embeddings_available ? <p className="text-xs text-amber-700">This workspace’s search storage cannot use client-owned embeddings yet.</p> : <>
                        <div className="grid gap-2 sm:grid-cols-2">
                            <select aria-label="Embedding provider" disabled={busy} value={selectedEmbeddingProvider} onChange={event => { setEmbeddingProvider(event.target.value); setEmbeddingModel(''); }} className="rounded-lg border border-gray-300 p-2 text-sm">
                                {embeddingProviders.map(([name]) => <option key={name} value={name}>{LABELS[name] ?? name}</option>)}
                            </select>
                            <select aria-label="Embedding model" disabled={busy} value={selectedEmbeddingModel} onChange={event => setEmbeddingModel(event.target.value)} className="rounded-lg border border-gray-300 p-2 text-sm">
                                {embeddingModels.map(name => <option key={name} value={name}>{name}</option>)}
                            </select>
                        </div>
                        <button disabled={busy || !selectedEmbeddingModel || embeddingIndex.corpora?.length === 0} onClick={() => run(async request => {
                            await request.post('/api/admin/workspace/ai/embedding-index/prepare', { provider: selectedEmbeddingProvider, model: selectedEmbeddingModel, corpus: embeddingCorpusRef.current === 'default' ? embeddingIndex.selected_corpus ?? 'default' : embeddingCorpusRef.current });
                        }, 'Embedding index rebuilt. Review and activate it.')} className="rounded-lg border border-gray-300 px-3 py-2 text-sm disabled:opacity-50">Rebuild index</button>
                        {embeddingIndex.candidate && <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 space-y-2">
                            <p className="text-xs text-blue-900">Ready: {LABELS[embeddingIndex.candidate.provider] ?? embeddingIndex.candidate.provider} · {embeddingIndex.candidate.model} · {embeddingIndex.candidate.dimension} dimensions</p>
                            <button disabled={busy} onClick={() => run(async request => {
                                await request.post('/api/admin/workspace/ai/embedding-index/activate', { version: embeddingIndex.candidate?.version, corpus: embeddingCorpusRef.current === 'default' ? embeddingIndex.selected_corpus ?? 'default' : embeddingCorpusRef.current });
                            }, 'Embedding index activated.')} className="rounded-lg bg-brand px-3 py-2 text-sm font-medium text-white disabled:opacity-50">Activate rebuilt index</button>
                        </div>}
                    </>}
                </> : <p className="text-xs text-gray-500">Loading index status…</p>}
            </div>
        </>}
    </section>;
}
