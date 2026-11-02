// Admin → Folders: curate the shared Drive folders shown per department
// on the Productivity "Team Folders" tab. Modeled on PlaybooksSection.
import React, { useEffect, useMemo, useState } from 'react';
import {
    AlertCircle, CheckCircle2, FolderTree, Pencil, Plus, RefreshCw, Save, Trash2, X,
} from 'lucide-react';
import client from '../../api/client';

interface FolderRow {
    id: string;
    department: string | null;
    folder_id: string;
    folder_name: string;
    web_view_link: string | null;
    sort_order: number;
    is_active: boolean;
    updated_by: string;
    updated_at: string;
}

interface FolderDraft {
    id?: string;
    department: string | null;
    folder_id: string;       // bare id OR a pasted Drive URL
    folder_name: string;
    web_view_link: string;
    sort_order: number;
    is_active: boolean;
}

export const DepartmentFoldersSection: React.FC = () => {
    const [rows, setRows] = useState<FolderRow[]>([]);
    const [departments, setDepartments] = useState<string[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState<string | null>(null);
    const [filterDept, setFilterDept] = useState<string | 'all'>('all');
    const [showInactive, setShowInactive] = useState(true);
    const [editing, setEditing] = useState<FolderDraft | null>(null);
    const [saving, setSaving] = useState(false);

    const fetchAll = async () => {
        setLoading(true);
        setError(null);
        try {
            const [fRes, deptRes] = await Promise.all([
                client.get<FolderRow[]>('/api/department-folders/admin', {
                    params: { include_inactive: true },
                }),
                client.get<string[]>('/api/departments/'),
            ]);
            setRows(fRes.data);
            setDepartments(deptRes.data || []);
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Failed to load');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => { fetchAll(); }, []);

    const filteredRows = useMemo(() => rows.filter(r => {
        if (!showInactive && !r.is_active) return false;
        if (filterDept === 'all') return true;
        if (filterDept === '__company__') return r.department == null;
        return r.department === filterDept;
    }), [rows, filterDept, showInactive]);

    const startNew = () => setEditing({
        department: null, folder_id: '', folder_name: '',
        web_view_link: '', sort_order: 100, is_active: true,
    });

    const startEdit = (row: FolderRow) => setEditing({
        id: row.id,
        department: row.department,
        folder_id: row.folder_id,
        folder_name: row.folder_name,
        web_view_link: row.web_view_link || '',
        sort_order: row.sort_order,
        is_active: row.is_active,
    });

    const save = async () => {
        if (!editing) return;
        setSaving(true);
        setError(null);
        setSuccess(null);
        try {
            const payload: any = {
                department: editing.department,
                folder_id: editing.folder_id,
                folder_name: editing.folder_name,
                web_view_link: editing.web_view_link || null,
                sort_order: editing.sort_order,
                is_active: editing.is_active,
            };
            if (editing.id) {
                await client.put(`/api/department-folders/admin/${editing.id}`, payload);
                setSuccess('Folder updated.');
            } else {
                await client.post('/api/department-folders/admin', payload);
                setSuccess('Folder added.');
            }
            setEditing(null);
            await fetchAll();
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Save failed');
        } finally {
            setSaving(false);
        }
    };

    const remove = async (row: FolderRow) => {
        if (!confirm(`Remove "${row.folder_name}"? This cannot be undone.`)) return;
        try {
            await client.delete(`/api/department-folders/admin/${row.id}`);
            await fetchAll();
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Delete failed');
        }
    };

    return (
        <div className="bg-white border border-gray-100 rounded-xl p-5">
            <div className="flex items-start justify-between gap-4 mb-4">
                <div>
                    <h3 className="text-sm font-semibold text-gray-900 flex items-center gap-2">
                        <FolderTree className="h-4 w-4 text-brand" />
                        Department folders
                    </h3>
                    <p className="text-xs text-gray-500 mt-1 max-w-2xl">
                        Shared Drive folders shown on the Productivity → Team Folders tab
                        for each department. Paste a Drive folder link or id. Rows with no
                        department are <b>company-wide</b> and show for everyone.
                    </p>
                </div>
                <button
                    onClick={startNew}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-brand text-white text-xs font-medium rounded-md hover:bg-brand-hover"
                >
                    <Plus className="h-3.5 w-3.5" /> Add folder
                </button>
            </div>

            <div className="flex items-center gap-3 mb-3">
                <select
                    value={filterDept}
                    onChange={e => setFilterDept(e.target.value as any)}
                    className="px-2 py-1 text-xs border border-gray-200 rounded-md"
                >
                    <option value="all">All departments</option>
                    <option value="__company__">Company-wide (no department)</option>
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
            </div>

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

            {loading ? (
                <div className="flex items-center justify-center py-8">
                    <RefreshCw className="h-5 w-5 animate-spin text-gray-400" />
                </div>
            ) : (
                <div className="space-y-2">
                    {filteredRows.length === 0 && (
                        <div className="text-xs text-gray-400 italic py-6 text-center">
                            No folders match the current filter.
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
                                    <span className="text-sm font-medium text-gray-900 truncate">{row.folder_name}</span>
                                    {!row.is_active && (
                                        <span className="text-[10px] uppercase tracking-tight text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">
                                            inactive
                                        </span>
                                    )}
                                </div>
                                <div className="text-[11px] text-gray-500 mt-0.5">
                                    {row.department
                                        ? <>Department: <b>{row.department}</b></>
                                        : <>Company-wide</>
                                    } · Order <b>{row.sort_order}</b> · <span className="font-mono">{row.folder_id}</span>
                                </div>
                            </div>
                            <div className="flex items-center gap-1 flex-shrink-0">
                                <button
                                    onClick={() => startEdit(row)}
                                    className="p-1.5 text-gray-500 hover:text-brand hover:bg-rose-50 rounded"
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

            {editing && (
                <div className="fixed inset-0 bg-black/30 z-50 flex items-center justify-center p-4">
                    <div className="bg-white rounded-2xl shadow-xl max-w-xl w-full">
                        <div className="px-5 py-3 border-b border-gray-100 flex items-center justify-between">
                            <h4 className="text-sm font-semibold">
                                {editing.id ? 'Edit folder' : 'Add folder'}
                            </h4>
                            <button onClick={() => setEditing(null)} className="p-1 text-gray-400 hover:text-gray-600">
                                <X className="h-4 w-4" />
                            </button>
                        </div>
                        <div className="px-5 py-4 space-y-3">
                            <div>
                                <label className="block text-xs font-medium text-gray-700 mb-1">Label</label>
                                <input
                                    type="text"
                                    value={editing.folder_name}
                                    onChange={e => setEditing({ ...editing, folder_name: e.target.value })}
                                    className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md"
                                    placeholder="e.g. Sales — Active Deals"
                                />
                            </div>
                            <div>
                                <label className="block text-xs font-medium text-gray-700 mb-1">
                                    Drive folder link or id
                                </label>
                                <input
                                    type="text"
                                    value={editing.folder_id}
                                    onChange={e => setEditing({ ...editing, folder_id: e.target.value })}
                                    className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md font-mono"
                                    placeholder="https://drive.google.com/drive/folders/…"
                                />
                            </div>
                            <div className="grid grid-cols-2 gap-3">
                                <div>
                                    <label className="block text-xs font-medium text-gray-700 mb-1">Department</label>
                                    <select
                                        value={editing.department ?? '__company__'}
                                        onChange={e => setEditing({
                                            ...editing,
                                            department: e.target.value === '__company__' ? null : e.target.value,
                                        })}
                                        className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md"
                                    >
                                        <option value="__company__">Company-wide (everyone)</option>
                                        {departments.map(d => <option key={d} value={d}>{d}</option>)}
                                    </select>
                                </div>
                                <div>
                                    <label className="block text-xs font-medium text-gray-700 mb-1">Sort order</label>
                                    <input
                                        type="number" min={0} max={1000} step={10}
                                        value={editing.sort_order}
                                        onChange={e => setEditing({ ...editing, sort_order: parseInt(e.target.value, 10) || 100 })}
                                        className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-md"
                                    />
                                </div>
                            </div>
                            <label className="flex items-center gap-1.5 text-sm text-gray-700">
                                <input
                                    type="checkbox"
                                    checked={editing.is_active}
                                    onChange={e => setEditing({ ...editing, is_active: e.target.checked })}
                                />
                                Active
                            </label>
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
                                disabled={saving || !editing.folder_name.trim() || !editing.folder_id.trim()}
                                className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-brand text-white text-xs font-medium rounded-md hover:bg-brand-hover disabled:opacity-50"
                            >
                                {saving ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                                Save
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default DepartmentFoldersSection;
