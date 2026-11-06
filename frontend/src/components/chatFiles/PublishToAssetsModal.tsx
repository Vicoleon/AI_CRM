// "Publish to Assets" for a private Ask Cubie file: the same choices as Drive's
// "Add to Knowledge" (components/briefing/AddToAssetsModal.tsx), plus a
// description and a plain statement of who will see the copy. Publishing
// copies the file into the shared Library; the private file stays private.
import React, { useEffect, useState } from 'react';
import { AlertCircle, Share2, X } from 'lucide-react';
import client from '../../api/client';
import { publishChatFile } from '../../api/chatFiles';
import type { ChatFile } from '../../api/chatFiles';

interface Props {
    file: ChatFile;
    onClose: () => void;
    onPublished: (result: { doc_id: string; filename: string; rendition: boolean }) => void;
}

export const PublishToAssetsModal: React.FC<Props> = ({ file, onClose, onPublished }) => {
    const [topics, setTopics] = useState<string[]>([]);
    const [departments, setDepartments] = useState<string[]>([]);
    const [topic, setTopic] = useState('');
    const [department, setDepartment] = useState('');
    const [isCompanyWide, setIsCompanyWide] = useState(false);
    const [description, setDescription] = useState(file.summary || '');
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
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
            .catch(() => setError("Topics and departments couldn't be loaded. Close this and try again."));
    }, []);

    const alreadyIn = file.published?.[0];
    const audience = isCompanyWide ? 'everyone at your company' : department ? `the ${department} department` : 'that department';

    const handleSubmit = async () => {
        if (!topic || !department) {
            setError('Pick a topic and a department.');
            return;
        }
        setSubmitting(true);
        setError(null);
        try {
            const result = await publishChatFile(file.id, {
                topic,
                department,
                is_company_wide: isCompanyWide,
                description,
            });
            onPublished(result);
            onClose();
        } catch (e: unknown) {
            const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
            setError(typeof detail === 'string' && detail ? detail : "Cubie couldn't publish that file. Try again.");
        } finally {
            setSubmitting(false);
        }
    };

    const fieldClass =
        'w-full px-3 py-2 text-sm rounded-lg border border-gray-200 focus:outline-none focus:border-brand focus:ring-1 focus:ring-brand';

    return (
        <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
            <div
                className="bg-white rounded-2xl shadow-xl w-full max-w-md overflow-hidden"
                onClick={e => e.stopPropagation()}
                role="dialog"
                aria-modal="true"
                aria-labelledby="publish-title"
            >
                <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between">
                    <div className="min-w-0">
                        <h2 id="publish-title" className="text-lg font-semibold text-gray-900 flex items-center gap-2">
                            <Share2 size={18} /> Publish to Assets
                        </h2>
                        <p className="text-xs text-gray-500 mt-0.5 truncate max-w-[320px]" title={file.filename}>
                            {file.filename}
                        </p>
                    </div>
                    <button onClick={onClose} className="text-gray-400 hover:text-gray-600" aria-label="Close">
                        <X size={20} />
                    </button>
                </div>

                <div className="px-5 py-4 space-y-3">
                    {error && (
                        <div className="p-2 rounded bg-red-50 border border-red-200 text-xs text-red-700 flex items-center gap-2">
                            <AlertCircle size={14} className="flex-shrink-0" /> {error}
                        </div>
                    )}
                    {alreadyIn && (
                        <div className="p-2 rounded bg-amber-50 border border-amber-200 text-xs text-amber-800">
                            Already in Assets as "{alreadyIn.filename}". Publishing again adds a second copy.
                        </div>
                    )}

                    <label className="block">
                        <span className="block text-xs font-medium text-gray-700 mb-1">Topic</span>
                        <select value={topic} onChange={e => setTopic(e.target.value)} className={fieldClass}>
                            {topics.length === 0 ? (
                                <option value="">(no topics found)</option>
                            ) : (
                                topics.map(t => <option key={t} value={t}>{t}</option>)
                            )}
                        </select>
                    </label>

                    <label className="block">
                        <span className="block text-xs font-medium text-gray-700 mb-1">Department</span>
                        <select value={department} onChange={e => setDepartment(e.target.value)} className={fieldClass}>
                            {departments.length === 0 ? (
                                <option value="">(no departments found)</option>
                            ) : (
                                departments.map(d => <option key={d} value={d}>{d}</option>)
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

                    <label className="block">
                        <span className="block text-xs font-medium text-gray-700 mb-1">Description</span>
                        <textarea
                            value={description}
                            onChange={e => setDescription(e.target.value)}
                            rows={3}
                            className={fieldClass}
                        />
                    </label>

                    <p className="text-xs text-gray-600 leading-relaxed">
                        This puts a copy in Assets for <span className="font-medium">{audience}</span>, and Cubie
                        can then use it to answer their questions. Your private file stays private; deleting it
                        later does not remove the copy from Assets.
                        {file.publishes_as_original === false && (
                            <> Assets can't search a {file.kind_label} directly, so it gets a Word document
                            with {file.kind === 'image' ? 'the picture and ' : file.kind === 'pdf' ? 'its pages and ' : ''}
                            what Cubie read from it.</>
                        )}
                    </p>
                </div>

                <div className="px-5 py-3 border-t border-gray-100 flex items-center justify-end gap-2 bg-gray-50">
                    <button onClick={onClose} disabled={submitting} className="px-3 py-1.5 text-sm text-gray-600 hover:text-gray-800">
                        Cancel
                    </button>
                    <button
                        onClick={handleSubmit}
                        disabled={submitting || !topic || !department}
                        className="px-4 py-1.5 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50 inline-flex items-center gap-1.5"
                    >
                        {submitting ? 'Publishing…' : (<><Share2 size={14} /> Publish</>)}
                    </button>
                </div>
            </div>
        </div>
    );
};
