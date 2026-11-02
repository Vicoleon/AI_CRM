// UC-44 — Drive folder picker modal.
// Lets the user search their Drive folders and pick which ones the
// Documents tab should surface. Selections are saved via PUT
// /api/drive/preferences and bound to the user (per-user
// WorkspaceSetting on the backend).
import React, { useEffect, useState } from 'react';
import { X, Search, Folder, Check } from 'lucide-react';
import client from '../../api/client';


export interface DriveFolderRef {
    id: string;
    name: string;
    web_view_link?: string;
}


interface Props {
    open: boolean;
    initialSelection: DriveFolderRef[];
    onClose: () => void;
    onSave: (selection: DriveFolderRef[]) => void;
}


export const DriveFolderPickerModal: React.FC<Props> = ({
    open,
    initialSelection,
    onClose,
    onSave,
}) => {
    const [query, setQuery] = useState('');
    const [results, setResults] = useState<DriveFolderRef[]>([]);
    const [selected, setSelected] = useState<DriveFolderRef[]>(initialSelection);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Reset to the parent's selection whenever the modal reopens.
    useEffect(() => {
        if (open) {
            setSelected(initialSelection);
            setQuery('');
        }
    }, [open, initialSelection]);

    // Debounced search.
    useEffect(() => {
        if (!open) return;
        let cancelled = false;
        const t = setTimeout(() => {
            setLoading(true);
            setError(null);
            client
                .get<{ items: DriveFolderRef[] }>('/api/drive/folders', {
                    params: { q: query, max_results: 50 },
                })
                .then(res => {
                    if (cancelled) return;
                    setResults(res.data.items || []);
                })
                .catch(e => {
                    if (cancelled) return;
                    setError(
                        e?.response?.data?.detail ||
                            e.message ||
                            'Failed to search folders'
                    );
                })
                .finally(() => {
                    if (!cancelled) setLoading(false);
                });
        }, 200);
        return () => {
            cancelled = true;
            clearTimeout(t);
        };
    }, [open, query]);

    const isSelected = (id: string) => selected.some(s => s.id === id);

    const toggle = (folder: DriveFolderRef) => {
        setSelected(prev =>
            prev.some(s => s.id === folder.id)
                ? prev.filter(s => s.id !== folder.id)
                : [...prev, folder],
        );
    };

    const handleSave = async () => {
        setSaving(true);
        setError(null);
        try {
            await client.put('/api/drive/preferences', { folders: selected });
            onSave(selected);
            onClose();
        } catch (e: any) {
            setError(
                e?.response?.data?.detail || e.message || 'Failed to save'
            );
        } finally {
            setSaving(false);
        }
    };

    if (!open) return null;

    return (
        <div
            className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
            onClick={onClose}
        >
            <div
                className="bg-white rounded-2xl shadow-xl w-full max-w-lg max-h-[80vh] flex flex-col overflow-hidden"
                onClick={e => e.stopPropagation()}
            >
                <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between">
                    <div>
                        <h2 className="text-lg font-semibold text-gray-900">
                            Pick Drive folders
                        </h2>
                        <p className="text-xs text-gray-500 mt-0.5">
                            Showing your My Drive folders. Search to find
                            nested ones — the Documents tab will only surface
                            files in the folders you pick.
                        </p>
                    </div>
                    <button
                        onClick={onClose}
                        className="text-gray-400 hover:text-gray-600 transition-colors"
                    >
                        <X size={20} />
                    </button>
                </div>

                <div className="px-5 pt-3 pb-2">
                    <div className="relative">
                        <Search
                            size={16}
                            className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
                        />
                        <input
                            autoFocus
                            value={query}
                            onChange={e => setQuery(e.target.value)}
                            placeholder="Search by folder name…"
                            className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-gray-200 focus:outline-none focus:border-brand focus:ring-1 focus:ring-brand"
                        />
                    </div>
                </div>

                {selected.length > 0 && (
                    <div className="px-5 pb-2 flex flex-wrap gap-1.5">
                        {selected.map(f => (
                            <span
                                key={f.id}
                                className="text-[11px] px-2 py-0.5 rounded-full bg-brand/10 text-brand inline-flex items-center gap-1"
                            >
                                {f.name}
                                <button
                                    onClick={() => toggle(f)}
                                    className="hover:bg-brand/20 rounded-full"
                                >
                                    <X size={10} />
                                </button>
                            </span>
                        ))}
                    </div>
                )}

                <div className="flex-1 overflow-auto">
                    {error && (
                        <div className="m-4 p-2 rounded bg-red-50 border border-red-200 text-xs text-red-700">
                            {error}
                        </div>
                    )}
                    {loading ? (
                        <div className="p-4 text-sm text-gray-400">Searching…</div>
                    ) : results.length === 0 ? (
                        <div className="p-6 text-sm text-gray-400 text-center">
                            {query
                                ? 'No folders match that search.'
                                : 'No My Drive folders found.'}
                        </div>
                    ) : (
                        <ul className="divide-y divide-gray-100">
                            {results.map(f => {
                                const sel = isSelected(f.id);
                                return (
                                    <li
                                        key={f.id}
                                        className="px-5 py-2 flex items-center gap-3 hover:bg-gray-50 cursor-pointer"
                                        onClick={() => toggle(f)}
                                    >
                                        <Folder
                                            size={16}
                                            className="text-gray-400 shrink-0"
                                        />
                                        <span className="text-sm text-gray-800 flex-1 truncate">
                                            {f.name}
                                        </span>
                                        {sel && (
                                            <Check
                                                size={16}
                                                className="text-brand shrink-0"
                                            />
                                        )}
                                    </li>
                                );
                            })}
                        </ul>
                    )}
                </div>

                <div className="px-5 py-3 border-t border-gray-100 flex items-center justify-between bg-gray-50">
                    <span className="text-xs text-gray-500">
                        {selected.length === 0
                            ? 'No folders selected — Documents will show all of Drive.'
                            : `${selected.length} folder${selected.length === 1 ? '' : 's'} selected.`}
                    </span>
                    <div className="flex items-center gap-2">
                        <button
                            onClick={onClose}
                            disabled={saving}
                            className="px-3 py-1.5 text-sm text-gray-600 hover:text-gray-800"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={handleSave}
                            disabled={saving}
                            className="px-4 py-1.5 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50"
                        >
                            {saving ? 'Saving…' : 'Save'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
};
