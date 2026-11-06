import React, { useEffect, useState } from 'react';
import { Save, RefreshCw, AlertCircle, CheckCircle2 } from 'lucide-react';
import client from '../../api/client';

// ─── Types matching backend/routers/chat_admin.py ────────────────────────────

interface ChatConfig {
    schema_version: number;
    model_id: string;
}

interface ChatCatalog {
    models: string[];
    default_model: string;
}

// ─── Component ───────────────────────────────────────────────────────────────

export const ChatModelTab: React.FC = () => {
    const [config, setConfig] = useState<ChatConfig | null>(null);
    const [catalog, setCatalog] = useState<ChatCatalog | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState<string | null>(null);

    const fetchData = async () => {
        setLoading(true);
        setError(null);
        try {
            const [cfgRes, catRes] = await Promise.all([
                client.get<ChatConfig>('/api/admin/chat/config'),
                client.get<ChatCatalog>('/api/admin/chat/catalog'),
            ]);
            setConfig(cfgRes.data);
            setCatalog(catRes.data);
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Failed to load chat config');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchData();
    }, []);

    const handleSave = async () => {
        if (!config) return;
        setSaving(true);
        setError(null);
        setSuccess(null);
        try {
            const res = await client.put<ChatConfig>('/api/admin/chat/config', {
                model_id: config.model_id,
            });
            setConfig(res.data);
            setSuccess('Chat model saved — new chats use it immediately, no redeploy.');
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Failed to save');
        } finally {
            setSaving(false);
        }
    };

    if (loading) {
        return <div className="p-6 text-gray-500">Loading chat model settings…</div>;
    }
    if (!config || !catalog) {
        return (
            <div className="p-6 text-red-600 flex items-center gap-2">
                <AlertCircle className="h-5 w-5" /> {error || 'Could not load chat config.'}
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <div>
                <h3 className="text-lg font-semibold text-gray-900">Chat model</h3>
                <p className="text-sm text-gray-500 mt-1">
                    The Gemini model powering Cubie chat. Only GA models with provisioned
                    quota are listed — preview models are excluded because they hit rate
                    limits under load. Changes take effect on the next chat, no redeploy.
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

            <div className="border border-gray-200 rounded-lg p-4 max-w-xl">
                <label className="block text-sm font-medium text-gray-700 mb-1">
                    Coordinator model
                </label>
                <select
                    value={config.model_id}
                    onChange={(e) => setConfig({ ...config, model_id: e.target.value })}
                    className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm"
                >
                    {/* Keep the current value selectable even if it's not in the catalog. */}
                    {!catalog.models.includes(config.model_id) && (
                        <option value={config.model_id}>{config.model_id} (current)</option>
                    )}
                    {catalog.models.map((m) => (
                        <option key={m} value={m}>
                            {m}
                            {m === catalog.default_model ? ' (default)' : ''}
                        </option>
                    ))}
                </select>
                <p className="text-xs text-gray-400 mt-2">
                    Default (from config): <code>{catalog.default_model}</code>.{' '}
                    <code>gemini-flash-latest</code> auto-follows Google&apos;s newest GA Flash.
                </p>
            </div>

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
            </div>
        </div>
    );
};
