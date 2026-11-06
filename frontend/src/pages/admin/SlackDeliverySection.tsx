import React, { useEffect, useState } from 'react';
import { RefreshCw, AlertCircle, Send } from 'lucide-react';
import client from '../../api/client';

// Mirrors backend/routers/priorities_admin.py :: SlackDeliveryDept
interface SlackDeliveryDept {
    name: string;
    slack_priorities_enabled: boolean;
}

/**
 * Per-department opt-in for the morning Cubie AI-Priorities Slack DM.
 * Flips `departments.slack_priorities_enabled`. Exec-team members always receive
 * the DM regardless of department, so this only governs whole-department rollout.
 */
export const SlackDeliverySection: React.FC = () => {
    const [depts, setDepts] = useState<SlackDeliveryDept[] | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [savingDept, setSavingDept] = useState<string | null>(null);

    const fetchDepts = async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await client.get<SlackDeliveryDept[]>('/api/admin/priorities/slack-delivery');
            setDepts(res.data);
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Failed to load departments');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchDepts();
    }, []);

    const toggle = async (dept: SlackDeliveryDept) => {
        setSavingDept(dept.name);
        setError(null);
        try {
            const res = await client.put<SlackDeliveryDept>(
                '/api/admin/priorities/slack-delivery',
                { name: dept.name, enabled: !dept.slack_priorities_enabled }
            );
            setDepts(prev => (prev || []).map(d => (d.name === dept.name ? res.data : d)));
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Update failed');
        } finally {
            setSavingDept(null);
        }
    };

    return (
        <div className="bg-white border border-gray-100 rounded-xl p-5">
            <h3 className="text-sm font-semibold text-gray-900 mb-1 flex items-center gap-2">
                <Send className="h-4 w-4 text-brand" />
                Slack delivery — morning AI Priorities DM
            </h3>
            <p className="text-xs text-gray-500 mb-4 max-w-2xl">
                Turn a department on to DM its members their top AI Priorities every weekday
                morning via the Cubie Slack bot. Exec-team members always receive it, regardless
                of department. Anyone whose work email isn't in your Slack workspace is skipped
                automatically (they still see priorities on the web).
            </p>

            {error && (
                <div className="p-3 mb-3 bg-red-50 text-red-700 rounded-lg flex items-center gap-2 text-sm">
                    <AlertCircle className="h-4 w-4" /> {error}
                </div>
            )}

            {loading ? (
                <div className="flex items-center justify-center py-6">
                    <RefreshCw className="h-5 w-5 animate-spin text-gray-400" />
                </div>
            ) : !depts || depts.length === 0 ? (
                <p className="text-sm text-gray-400 py-4">No departments found.</p>
            ) : (
                <div className="divide-y divide-gray-50">
                    {depts.map(d => (
                        <div key={d.name} className="flex items-center justify-between py-2.5">
                            <span className="text-sm text-gray-900">{d.name}</span>
                            <button
                                role="switch"
                                aria-checked={d.slack_priorities_enabled}
                                disabled={savingDept === d.name}
                                onClick={() => toggle(d)}
                                title={d.slack_priorities_enabled ? 'Disable Slack delivery' : 'Enable Slack delivery'}
                                className={`relative inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${d.slack_priorities_enabled ? 'bg-brand' : 'bg-gray-300'
                                    }`}
                            >
                                <span
                                    className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${d.slack_priorities_enabled ? 'translate-x-6' : 'translate-x-1'
                                        }`}
                                />
                            </button>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
};

export default SlackDeliverySection;
