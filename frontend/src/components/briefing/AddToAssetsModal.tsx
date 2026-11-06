// UC-44 — "Add to Assets" modal.
// Triggered by the per-row button on the Documents tab. Asks the user
// to pick a topic + department + company-wide flag, then POSTs to
// /api/drive/{file_id}/add-to-assets which downloads the file bytes
// from Drive and hands them to the same IngestionProcessor that
// /api/ingest already uses.
import React, { useEffect, useState } from 'react';
import { X, Check, Plus, AlertCircle } from 'lucide-react';
import client from '../../api/client';


interface Props {
    open: boolean;
    fileId: string;
    fileName: string;
    onClose: () => void;
    onSuccess: (docId: string) => void;
}


export const AddToAssetsModal: React.FC<Props> = ({
    open,
    fileId,
    fileName,
    onClose,
    onSuccess,
}) => {
    const [topics, setTopics] = useState<string[]>([]);
    const [departments, setDepartments] = useState<string[]>([]);
    const [topic, setTopic] = useState('');
    const [department, setDepartment] = useState('');
    const [isCompanyWide, setIsCompanyWide] = useState(false);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (!open) return;
        // Load picklists on open.
        Promise.all([
            client.get<string[]>('/api/topics/'),
            client.get<string[]>('/api/departments/'),
        ])
            .then(([t, d]) => {
                setTopics(t.data || []);
                setDepartments(d.data || []);
                setTopic(prev => prev || (t.data && t.data[0]) || '');
                setDepartment(prev => prev || (d.data && d.data[0]) || '');
            })
            .catch(e => {
                setError(
                    e?.response?.data?.detail ||
                        e.message ||
                        'Failed to load topics / departments'
                );
            });
    }, [open]);

    const handleSubmit = async () => {
        if (!topic || !department) {
            setError('Topic and Department are required.');
            return;
        }
        setSubmitting(true);
        setError(null);
        try {
            const res = await client.post<{ doc_id: string }>(
                `/api/drive/${encodeURIComponent(fileId)}/add-to-assets`,
                {
                    topic,
                    department,
                    is_company_wide: isCompanyWide,
                },
            );
            onSuccess(res.data.doc_id);
            onClose();
        } catch (e: any) {
            setError(
                e?.response?.data?.detail ||
                    e.message ||
                    'Failed to add to Assets'
            );
        } finally {
            setSubmitting(false);
        }
    };

    if (!open) return null;

    return (
        <div
            className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
            onClick={onClose}
        >
            <div
                className="bg-white rounded-2xl shadow-xl w-full max-w-md overflow-hidden"
                onClick={e => e.stopPropagation()}
            >
                <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between">
                    <div>
                        <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
                            <Plus size={18} />
                            Add to Knowledge
                        </h2>
                        <p className="text-xs text-gray-500 mt-0.5 truncate max-w-[300px]">
                            {fileName}
                        </p>
                    </div>
                    <button
                        onClick={onClose}
                        className="text-gray-400 hover:text-gray-600 transition-colors"
                    >
                        <X size={20} />
                    </button>
                </div>

                <div className="px-5 py-4 space-y-3">
                    {error && (
                        <div className="p-2 rounded bg-red-50 border border-red-200 text-xs text-red-700 flex items-center gap-2">
                            <AlertCircle size={14} />
                            {error}
                        </div>
                    )}

                    <label className="block">
                        <span className="block text-xs font-medium text-gray-700 mb-1">
                            Topic
                        </span>
                        <select
                            value={topic}
                            onChange={e => setTopic(e.target.value)}
                            className="w-full px-3 py-2 text-sm rounded-lg border border-gray-200 focus:outline-none focus:border-brand focus:ring-1 focus:ring-brand"
                        >
                            {topics.length === 0 ? (
                                <option value="">(no topics found)</option>
                            ) : (
                                topics.map(t => (
                                    <option key={t} value={t}>
                                        {t}
                                    </option>
                                ))
                            )}
                        </select>
                    </label>

                    <label className="block">
                        <span className="block text-xs font-medium text-gray-700 mb-1">
                            Department
                        </span>
                        <select
                            value={department}
                            onChange={e => setDepartment(e.target.value)}
                            className="w-full px-3 py-2 text-sm rounded-lg border border-gray-200 focus:outline-none focus:border-brand focus:ring-1 focus:ring-brand"
                        >
                            {departments.length === 0 ? (
                                <option value="">(no departments found)</option>
                            ) : (
                                departments.map(d => (
                                    <option key={d} value={d}>
                                        {d}
                                    </option>
                                ))
                            )}
                        </select>
                    </label>

                    <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                        <input
                            type="checkbox"
                            checked={isCompanyWide}
                            onChange={e => setIsCompanyWide(e.target.checked)}
                            className="rounded border-gray-300 text-brand focus:ring-brand"
                        />
                        Company-wide (visible to all departments)
                    </label>
                </div>

                <div className="px-5 py-3 border-t border-gray-100 flex items-center justify-end gap-2 bg-gray-50">
                    <button
                        onClick={onClose}
                        disabled={submitting}
                        className="px-3 py-1.5 text-sm text-gray-600 hover:text-gray-800"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={handleSubmit}
                        disabled={submitting || !topic || !department}
                        className="px-4 py-1.5 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50 inline-flex items-center gap-1.5"
                    >
                        {submitting ? (
                            <>Adding…</>
                        ) : (
                            <>
                                <Check size={14} />
                                Add
                            </>
                        )}
                    </button>
                </div>
            </div>
        </div>
    );
};
