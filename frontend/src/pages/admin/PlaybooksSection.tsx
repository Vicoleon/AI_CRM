import React, { useEffect, useMemo, useState } from 'react';
import {
    AlertCircle, BookText, CheckCircle2, Eye, Pencil, Plus, RefreshCw,
    Save, Trash2, X,
} from 'lucide-react';
import client from '../../api/client';

// ─── Types ───────────────────────────────────────────────────────────────────

interface PlaybookRow {
    id: string;
    name: string;
    department: string | null;
    body?: string;
    body_preview?: string;
    is_active: boolean;
    sort_order: number;
    created_by: string;
    updated_by: string;
    created_at: string;
    updated_at: string;
}

interface EffectivePlaybook {
    department: string | null;
    playbooks: { id: string; name: string; sort_order: number; body: string }[];
    concatenated_text: string;
}

interface PlaybookDraft {
    id?: string;
    name: string;
    department: string | null;
    body: string;
    is_active: boolean;
    sort_order: number;
}

// ─── Component ───────────────────────────────────────────────────────────────

export const PlaybooksSection: React.FC = () => {
    const [rows, setRows] = useState<PlaybookRow[]>([]);
    const [departments, setDepartments] = useState<string[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState<string | null>(null);
    const [filterDept, setFilterDept] = useState<string | 'all'>('all');
    const [showInactive, setShowInactive] = useState(true);
    const [editing, setEditing] = useState<PlaybookDraft | null>(null);
    const [preview, setPreview] = useState<EffectivePlaybook | null>(null);
    const [saving, setSaving] = useState(false);

    const fetchAll = async () => {
        setLoading(true);
        setError(null);
        try {
            const [pbRes, deptRes] = await Promise.all([
                client.get<PlaybookRow[]>('/api/admin/priorities/playbooks', {
                    params: { include_inactive: true },
                }),
                client.get<string[]>('/api/departments/'),
            ]);
            setRows(pbRes.data);
            setDepartments(deptRes.data || []);
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Failed to load');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => { fetchAll(); }, []);

    const filteredRows = useMemo(() => {
        return rows.filter(r => {
            if (!showInactive && !r.is_active) return false;
            if (filterDept === 'all') return true;
            if (filterDept === '__default__') return r.department == null;
            return r.department === filterDept;
        });
    }, [rows, filterDept, showInactive]);

    const startNew = () => setEditing({
        name: '', department: null, body: '',
        is_active: true, sort_order: 100,
    });

    const startEdit = async (row: PlaybookRow) => {
        // Need the full body, not the preview.
        try {
            const res = await client.get<PlaybookRow>(`/api/admin/priorities/playbooks/${row.id}`);
            const full = res.data;
            setEditing({
                id: full.id,
                name: full.name,
                department: full.department,
                body: full.body || '',
                is_active: full.is_active,
                sort_order: full.sort_order,
            });
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Failed to load playbook');
        }
    };

    const save = async () => {
        if (!editing) return;
        setSaving(true);
        setError(null);
        setSuccess(null);
        try {
            const payload: any = {
                name: editing.name,
                department: editing.department,
                body: editing.body,
                is_active: editing.is_active,
                sort_order: editing.sort_order,
            };
            if (editing.id) {
                await client.put(`/api/admin/priorities/playbooks/${editing.id}`, payload);
                setSuccess('Playbook updated.');
            } else {
                await client.post('/api/admin/priorities/playbooks', payload);
                setSuccess('Playbook created.');
            }
            setEditing(null);
            await fetchAll();
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Save failed');
        } finally {
            setSaving(false);
        }
    };

    const remove = async (row: PlaybookRow) => {
        if (!confirm(`Delete "${row.name}"? This cannot be undone.`)) return;
        try {
            await client.delete(`/api/admin/priorities/playbooks/${row.id}`);
            await fetchAll();
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Delete failed');
        }
    };

    const showPreview = async (dept: string | null) => {
        try {
            const res = await client.get<EffectivePlaybook>(
                '/api/admin/priorities/playbooks/effective',
                { params: dept !== null ? { department: dept } : {} },
            );
            setPreview(res.data);
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Preview failed');
        }
    };

    // ── render ────────────────────────────────────────────────────────────

    return (
        <div className="bg-white border border-gray-100 rounded-xl p-5">
            <div className="flex items-start justify-between gap-4 mb-4">
                <div>
                    <h3 className="text-sm font-semibold text-gray-900 flex items-center gap-2">
                        <BookText className="h-4 w-4 text-amber-500" />
                        Department playbooks
                    </h3>
                    <p className="text-xs text-gray-500 mt-1 max-w-2xl">
                        Free-form markdown the decision-maker reads to learn
                        "what counts as high-value" for a department.
                        Multiple active playbooks per department are
                        concatenated by <code>sort_order</code>. Rows with
                        no department are defaults that apply to everyone.
                    </p>
                </div>
                <button
                    onClick={startNew}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-brand text-white text-xs font-medium rounded-md hover:bg-brand-hover"
                >
                    <Plus className="h-3.5 w-3.5" /> New playbook
                </button>
            </div>

            {/* Filters */}
            <div className="flex items-center gap-3 mb-3">
                <select
                    value={filterDept}
                    onChange={e => setFilterDept(e.target.value as any)}
                    className="px-2 py-1 text-xs border border-gray-200 rounded-md"
                >
                    <option value="all">All departments</option>
                    <option value="__default__">Default (no department)</option>
                    {departments.map(d => <option key={d} value={d}>{d}</option>)}
                </select>
                <label className="flex items-center gap-1.5 text-xs text-gray-600">
                    <input
                        type="checkbox"
                        checked={showInactive}
                        onChange={e => setShowInactive(e.target.checked)}
                    />
                    Show inactive
                </label>
                {filterDept !== 'all' && (
                    <button
                        onClick={() => showPreview(filterDept === '__default__' ? null : filterDept)}
                        className="ml-auto inline-flex items-center gap-1 px-2 py-1 text-xs text-amber-700 hover:bg-amber-50 rounded"
                    >
                        <Eye className="h-3.5 w-3.5" />
                        Preview decision-maker injection
                    </button>
                )}
            </div>

            {/* Alerts */}
            {error && (
                <div className="p-2.5 mb-3 bg-red-50 text-red-700 rounded-md flex items-center gap-2 text-xs">
                    <AlertCircle className="h-4 w-4" /> {error}
                </div>
            )}
            {success && (
                <div className="p-2.5 mb-3 bg-green-50 text-green-700 rounded-md flex items-center gap-2 text-xs">
                    <CheckCircle2 className="h-4 w-4" /> {success}
                </div>
            )}

            {/* List */}
            {loading ? (
                <div className="flex items-center justify-center py-8">
                    <RefreshCw className="h-5 w-5 animate-spin text-gray-400" />
                </div>
            ) : (
                <div className="space-y-2">
                    {filteredRows.length === 0 && (
                        <div className="text-xs text-gray-400 italic py-6 text-center">
                            No playbooks match the current filter.
                        </div>
                    )}
                    {filteredRows.map(row => (
                        <div
                            key={row.id}
                            className={`border rounded-lg px-3 py-2 flex items-start justify-between gap-3 ${
                                row.is_active ? 'border-gray-200 bg-white' : 'border-gray-100 bg-gray-50'
                            }`}
                        >
                            <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2">
                                    <span className="text-sm font-medium text-gray-900">{row.name}</span>
                                    {!row.is_active && (
                                        <span className="text-[10px] uppercase tracking-tight text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">
                                            inactive
                                        </span>
                                    )}
                                </div>
                                <div className="text-[11px] text-gray-500 mt-0.5">
                                    {row.department
                                        ? <>Department: <b>{row.department}</b></>
                                        : <>Default (all departments)</>
                                    } · Order <b>{row.sort_order}</b> · Updated {row.updated_at?.slice(0, 10)} by {row.updated_by}
                                </div>
                                {row.body_preview && (
                                    <div className="text-[11px] text-gray-400 mt-1 line-clamp-2 italic">
                                        {row.body_preview}
                                    </div>
                                )}
                            </div>
                            <div className="flex items-center gap-1 flex-shrink-0">
                                <button
                                    onClick={() => startEdit(row)}
                                    className="p-1.5 text-gray-500 hover:text-amber-700 hover:bg-amber-50 rounded"
                                    title="Edit"
                                >
                                    <Pencil className="h-3.5 w-3.5" />
                                </button>
                                <button
                                    onClick={() => remove(row)}
                                    className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded"
                                    title="Delete"
                                >
                                    <Trash2 className="h-3.5 w-3.5" />
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            {/* Edit modal */}
            {editing && (
                <div className="fixed inset-0 bg-black/30 z-50 flex items-center justify-center p-4">
                    <div className="bg-white rounded-2xl shadow-xl max-w-3xl w-full max-h-[90vh] flex flex-col">
                        <div className="px-5 py-3 border-b border-gray-100 flex items-center justify-between">
                            <h4 className="text-sm font-semibold">
                                {editing.id ? 'Edit playbook' : 'New playbook'}
                            </h4>
                            <button onClick={() => setEditing(null)} className="p-1 text-gray-400 hover:text-gray-600">
                                <X className="h-4 w-4" />
                            </button>
                        </div>
                        <div className="px-5 py-4 space-y-3 overflow-auto">
                            <div className="grid grid-cols-2 gap-3">
                                <div>
                                    <label className="block text-xs font-medium text-gray-700 mb-1">Name</label>
                                    <input
                                        type="text"
                                        value={editing.name}
                                        onChange={e => setEditing({ ...editing, name: e.target.value })}
                                        className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md"
                                        placeholder="e.g. Ops — Vendor management"
                                    />
                                </div>
                                <div>
                                    <label className="block text-xs font-medium text-gray-700 mb-1">Department</label>
                                    <select
                                        value={editing.department ?? '__default__'}
                                        onChange={e => setEditing({
                                            ...editing,
                                            department: e.target.value === '__default__' ? null : e.target.value,
                                        })}
                                        className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md"
                                    >
                                        <option value="__default__">Default (every department)</option>
                                        {departments.map(d => <option key={d} value={d}>{d}</option>)}
                                    </select>
                                </div>
                            </div>
                            <div className="grid grid-cols-3 gap-3">
                                <div>
                                    <label className="block text-xs font-medium text-gray-700 mb-1">Sort order</label>
                                    <input
                                        type="number" min={0} max={1000} step={10}
                                        value={editing.sort_order}
                                        onChange={e => setEditing({ ...editing, sort_order: parseInt(e.target.value, 10) || 100 })}
                                        className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md"
                                    />
                                </div>
                                <div>
                                    <label className="block text-xs font-medium text-gray-700 mb-1">Status</label>
                                    <label className="flex items-center gap-1.5 text-sm text-gray-700 py-1.5">
                                        <input
                                            type="checkbox"
                                            checked={editing.is_active}
                                            onChange={e => setEditing({ ...editing, is_active: e.target.checked })}
                                        />
                                        Active
                                    </label>
                                </div>
                            </div>
                            <div>
                                <label className="block text-xs font-medium text-gray-700 mb-1">Body (markdown)</label>
                                <textarea
                                    value={editing.body}
                                    onChange={e => setEditing({ ...editing, body: e.target.value })}
                                    rows={20}
                                    className="w-full px-2 py-1.5 text-xs font-mono border border-gray-200 rounded-md"
                                    placeholder="# High-value entities&#10;- ..."
                                />
                            </div>
                        </div>
                        <div className="px-5 py-3 border-t border-gray-100 flex items-center justify-end gap-2">
                            <button
                                onClick={() => setEditing(null)}
                                className="px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 rounded-md"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={save}
                                disabled={saving || !editing.name.trim()}
                                className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-brand text-white text-xs font-medium rounded-md hover:bg-brand-hover disabled:opacity-50"
                            >
                                {saving ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                                Save
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Preview modal */}
            {preview && (
                <div className="fixed inset-0 bg-black/30 z-50 flex items-center justify-center p-4">
                    <div className="bg-white rounded-2xl shadow-xl max-w-3xl w-full max-h-[90vh] flex flex-col">
                        <div className="px-5 py-3 border-b border-gray-100 flex items-center justify-between">
                            <h4 className="text-sm font-semibold">
                                Decision-maker injection — {preview.department || 'default'}
                            </h4>
                            <button onClick={() => setPreview(null)} className="p-1 text-gray-400 hover:text-gray-600">
                                <X className="h-4 w-4" />
                            </button>
                        </div>
                        <div className="px-5 py-4 overflow-auto">
                            <div className="text-[11px] text-gray-500 mb-3">
                                {preview.playbooks.length} active playbook
                                {preview.playbooks.length === 1 ? '' : 's'} concatenated below in
                                the order shown.
                            </div>
                            <pre className="text-xs font-mono text-gray-800 bg-gray-50 border border-gray-100 rounded-md p-3 whitespace-pre-wrap">
                                {preview.concatenated_text || '(no active playbooks for this scope)'}
                            </pre>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default PlaybooksSection;
