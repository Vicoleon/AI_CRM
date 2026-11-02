import React, { useState, useEffect } from 'react';
import { Plus, Tag, Trash2 } from 'lucide-react';
import client from '../../api/client';
import { motion, AnimatePresence } from 'framer-motion';

/**
 * Drive-label → importance-priority catalog, rendered as the admin-only
 * "Drive Labels" tab. Each row maps a Google Drive label value (e.g.
 * "Important", "Cubie", "Project") to a 1.0–2.0 priority weight. The
 * AI-Priorities Drive signal reads this map to decide which recently-updated
 * labeled Drive files surface as high-importance priorities.
 */
interface LabelPriority {
    id: string;
    label_key: string | null;
    label_value: string;
    priority: number;
}

export const LabelsTab: React.FC = () => {
    const [labels, setLabels] = useState<LabelPriority[]>([]);
    const [newValue, setNewValue] = useState('');
    const [newKey, setNewKey] = useState('');
    const [newPriority, setNewPriority] = useState(1.5);
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        fetchLabels();
    }, []);

    const fetchLabels = async () => {
        try {
            const { data } = await client.get('/api/labels/');
            setLabels(data);
        } catch (err) {
            console.error('Failed to fetch labels:', err);
        }
    };

    const handleAdd = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!newValue.trim()) return;
        setLoading(true);
        try {
            await client.post('/api/labels/', {
                label_value: newValue.trim(),
                label_key: newKey.trim() || null,
                priority: newPriority,
            });
            setNewValue('');
            setNewKey('');
            setNewPriority(1.5);
            fetchLabels();
        } catch (err) {
            console.error('Failed to add label:', err);
        } finally {
            setLoading(false);
        }
    };

    const handlePriorityChange = async (id: string, priority: number) => {
        // Optimistic local update; persist on commit.
        setLabels((prev) => prev.map((l) => (l.id === id ? { ...l, priority } : l)));
    };

    const handlePriorityCommit = async (id: string, priority: number) => {
        const clamped = Math.max(1.0, Math.min(2.0, priority));
        try {
            await client.patch(`/api/labels/${encodeURIComponent(id)}`, { priority: clamped });
            fetchLabels();
        } catch (err) {
            console.error('Failed to update label priority:', err);
            fetchLabels();
        }
    };

    const handleRemove = async (id: string) => {
        try {
            await client.delete(`/api/labels/${encodeURIComponent(id)}`);
            fetchLabels();
        } catch (err) {
            console.error('Failed to remove label:', err);
        }
    };

    const severityLabel = (p: number) =>
        p >= 1.4 ? 'High' : p >= 1.1 ? 'Medium' : 'Low';

    return (
        <div>
            <p className="text-[#64748b] text-[15px] mb-6">
                Map Google Drive labels to an importance weight (1.0–2.0). When a teammate
                recently updates a Drive file carrying one of these labels, it surfaces as a
                priority — higher weight, higher importance.
            </p>

            {/* Add Label Form */}
            <div className="bg-white rounded-[12px] border border-gray-100 p-6 mb-8 w-fit shadow-[0_2px_8px_rgb(0,0,0,0.02)]">
                <form onSubmit={handleAdd} className="flex gap-4 items-end flex-wrap">
                    <div className="flex flex-col">
                        <label className="text-[12px] font-semibold text-gray-500 mb-1">Label value</label>
                        <input
                            type="text"
                            value={newValue}
                            onChange={(e) => setNewValue(e.target.value)}
                            placeholder="e.g. Important"
                            className="w-[220px] border border-gray-200 rounded-[6px] px-4 py-2.5 text-[14px] text-gray-700 placeholder-gray-400 focus:outline-none focus:border-brand/50 focus:ring-2 focus:ring-brand/10 transition-all font-medium"
                        />
                    </div>
                    <div className="flex flex-col">
                        <label className="text-[12px] font-semibold text-gray-500 mb-1">Label name (optional)</label>
                        <input
                            type="text"
                            value={newKey}
                            onChange={(e) => setNewKey(e.target.value)}
                            placeholder="e.g. sensitivity"
                            className="w-[200px] border border-gray-200 rounded-[6px] px-4 py-2.5 text-[14px] text-gray-700 placeholder-gray-400 focus:outline-none focus:border-brand/50 focus:ring-2 focus:ring-brand/10 transition-all font-medium"
                        />
                    </div>
                    <div className="flex flex-col">
                        <label className="text-[12px] font-semibold text-gray-500 mb-1">Priority (1.0–2.0)</label>
                        <input
                            type="number"
                            min={1.0}
                            max={2.0}
                            step={0.1}
                            value={newPriority}
                            onChange={(e) => setNewPriority(parseFloat(e.target.value))}
                            className="w-[120px] border border-gray-200 rounded-[6px] px-4 py-2.5 text-[14px] text-gray-700 focus:outline-none focus:border-brand/50 focus:ring-2 focus:ring-brand/10 transition-all font-medium"
                        />
                    </div>
                    <button
                        type="submit"
                        disabled={loading || !newValue.trim()}
                        className="bg-brand hover:bg-brand-hover text-white text-[14px] font-medium px-5 py-2.5 rounded-[6px] transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                    >
                        <Plus strokeWidth={2.5} size={16} />
                        Add Label
                    </button>
                </form>
            </div>

            {/* Labels Table */}
            <div className="overflow-x-auto bg-white border border-gray-100 rounded-lg">
                <table className="w-full text-left">
                    <thead>
                        <tr className="text-gray-500 text-sm border-b border-gray-100 bg-gray-50/50">
                            <th className="py-3 px-4 font-semibold">Label</th>
                            <th className="py-3 px-4 font-semibold w-[200px]">Priority</th>
                            <th className="py-3 px-4 font-semibold text-center w-[100px]">Action</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-50">
                        <AnimatePresence>
                            {labels.map((label) => (
                                <motion.tr
                                    key={label.id}
                                    initial={{ opacity: 0, y: -10 }}
                                    animate={{ opacity: 1, y: 0 }}
                                    exit={{ opacity: 0, y: 10 }}
                                    className="group hover:bg-gray-50/50 transition-colors"
                                >
                                    <td className="py-3 px-4">
                                        <div className="flex items-center gap-3">
                                            <div className="h-9 w-9 bg-brand-tint rounded-full flex items-center justify-center text-brand text-sm font-bold shadow-sm">
                                                <Tag size={16} strokeWidth={2.5} />
                                            </div>
                                            <div className="flex flex-col">
                                                <span className="font-semibold text-sm text-gray-900">{label.label_value}</span>
                                                {label.label_key && (
                                                    <span className="text-xs text-gray-400">{label.label_key}</span>
                                                )}
                                            </div>
                                        </div>
                                    </td>
                                    <td className="py-3 px-4">
                                        <div className="flex items-center gap-2">
                                            <input
                                                type="number"
                                                min={1.0}
                                                max={2.0}
                                                step={0.1}
                                                value={label.priority}
                                                onChange={(e) => handlePriorityChange(label.id, parseFloat(e.target.value))}
                                                onBlur={(e) => handlePriorityCommit(label.id, parseFloat(e.target.value))}
                                                className="w-[80px] border border-gray-200 rounded-[6px] px-2 py-1 text-[13px] text-gray-700 focus:outline-none focus:border-brand/50"
                                            />
                                            <span className="text-xs font-medium text-gray-500">{severityLabel(label.priority)}</span>
                                        </div>
                                    </td>
                                    <td className="py-3 px-4 text-center">
                                        <button
                                            onClick={() => handleRemove(label.id)}
                                            className="text-gray-400 hover:text-red-600 transition-colors p-1.5 rounded hover:bg-red-50"
                                            title="Delete Label"
                                        >
                                            <Trash2 className="h-4 w-4" />
                                        </button>
                                    </td>
                                </motion.tr>
                            ))}
                        </AnimatePresence>
                    </tbody>
                </table>
            </div>

            {labels.length === 0 && (
                <div className="text-center py-12 text-gray-500 bg-white rounded-xl border border-gray-100 shadow-sm mt-8">
                    <Tag size={40} className="mx-auto mb-3 text-gray-300" strokeWidth={1.5} />
                    <p className="text-[15px]">No labels yet. Add your first Drive label above.</p>
                </div>
            )}
        </div>
    );
};
