import React, { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { FileBox, Search, Archive, Trash2, ArrowUpDown, Mic, Users, Upload as UploadIcon, Tags, Download, Share2, Mail, X, Loader2, History, Lock } from 'lucide-react';
import client from '../api/client';
import { formatInstantDate } from '../utils/datetime';
import { motion } from 'framer-motion';
import { useAuth } from '../context/AuthContext';
import { UploadModal } from '../components/UploadModal';
import { VersionHistoryModal } from '../components/VersionHistoryModal';
import { TopicsTab } from '../components/TopicsTab';
import { PrivateFilesTab } from '../components/chatFiles/PrivateFilesTab';
import { listChatFiles } from '../api/chatFiles';
import { companyNameOrDefault } from '../api/company';

interface Asset {
    doc_id: string;
    owner: string;
    approver?: string;
    status: string;
    topic?: string;
    description?: string;
    tags?: string[];
    original_filename?: string;
    created_at: string;
    updated_at: string;
    department?: string;
    allowed_emails?: string[];
    source_path?: string;
    processing?: boolean;
    /** Which revision the row describes. 1 until a new version is uploaded. */
    current_version?: number;
}

interface TranscriptRow {
    doc_id: string;
    original_filename: string | null;
    owner: string;
    status: 'Indexed' | 'Pending' | 'Deleted' | 'Failed';
    is_owner: boolean;
    participant_count: number;
    created_at: string;
    updated_at: string;
}

type SortField = 'original_filename' | 'owner' | 'department' | 'topic' | 'status' | 'created_at';
type TabKey = 'documents' | 'transcripts' | 'topics' | 'private';

export const Assets: React.FC = () => {
    const { user, isAdmin, canUploadDocuments, hasFeature } = useAuth();
    const companyName = companyNameOrDefault(user?.workspace?.company_name);
    // Why the upload controls are disabled — shown on hover and under the
    // empty-state button, so the user knows what to ask for instead of
    // filling in the whole form and getting a 403 on submit.
    const uploadBlockedReason =
        'You don’t have permission to add documents to the knowledge base. Ask an admin to give your account upload access.';
    const [searchParams, setSearchParams] = useSearchParams();
    // Deep link: /assets?tab=topics (admin Topics nav item) / ?tab=transcripts.
    const initialTab = searchParams.get('tab');
    const [activeTab, setActiveTab] = useState<TabKey>(
        initialTab === 'transcripts'
            ? 'transcripts'
            : initialTab === 'topics'
              ? 'topics'
              : initialTab === 'private'
                ? 'private'
                : 'documents'
    );
    // My private files (attached in Ask Cubie). Shown to anyone on the roster,
    // and to anyone who still has files there after leaving it, so they can
    // always download or delete their own.
    const [privateCount, setPrivateCount] = useState<number | null>(null);
    const showPrivateTab = hasFeature('chat_file_upload') || (privateCount ?? 0) > 0;
    const [assets, setAssets] = useState<Record<string, Asset>>({});
    const [transcripts, setTranscripts] = useState<TranscriptRow[]>([]);
    const [transcriptsLoading, setTranscriptsLoading] = useState(false);
    const [searchTerm, setSearchTerm] = useState('');
    // Default to live docs; archived rows are retained records, shown on demand.
    const [statusFilter, setStatusFilter] = useState<'active' | 'archived' | 'all'>('active');
    const [loading, setLoading] = useState(true);
    const [loadingMore, setLoadingMore] = useState(false);
    const [sortField, setSortField] = useState<SortField>('created_at');
    const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc');
    const [showUpload, setShowUpload] = useState(false);

    // Pagination state
    const [total, setTotal] = useState(0);
    const [hasMore, setHasMore] = useState(false);
    const [totalIsExact, setTotalIsExact] = useState(true);
    const [nextCursor, setNextCursor] = useState<string | null>(null);
    const PAGE_SIZE = 50;

    useEffect(() => {
        fetchAssets();
        fetchTranscripts();
        listChatFiles()
            .then(data => setPrivateCount(data.files.length))
            .catch(() => setPrivateCount(0));
    }, []);

    // The URL is the source of truth for the active tab: keep them in sync on
    // same-route navigation (the component isn't remounted when only the query
    // string changes, e.g. the admin "Topics" sidebar link, or clicking the
    // "Assets" link while on Topics). Absent/unknown ?tab= resets to Documents.
    useEffect(() => {
        const t = searchParams.get('tab');
        setActiveTab(t === 'topics' || t === 'transcripts' || t === 'private' ? t : 'documents');
    }, [searchParams]);

    // Tab clicks write the URL; the effect above reconciles activeTab + the
    // sidebar highlight stays correct.
    const selectTab = (tab: TabKey) => {
        if (tab === 'documents') setSearchParams({});
        else setSearchParams({ tab });
    };

    const fetchTranscripts = async () => {
        try {
            setTranscriptsLoading(true);
            const { data } = await client.get('/api/transcripts/');
            setTranscripts(data.transcripts || []);
        } catch (err) {
            console.error('Failed to fetch transcripts:', err);
        } finally {
            setTranscriptsLoading(false);
        }
    };

    const handleDeleteTranscript = async (docId: string) => {
        if (!window.confirm('Delete this transcript? It will be removed from the search index for everyone.')) {
            return;
        }
        try {
            await client.delete(`/api/transcripts/${docId}`);
            setTranscripts(prev => prev.map(t =>
                t.doc_id === docId ? { ...t, status: 'Deleted' as const } : t
            ));
        } catch (err: any) {
            const detail = err?.response?.data?.detail || 'Failed to delete transcript';
            alert(detail);
        }
    };

    const fetchAssets = async (append: boolean = false, cursor: string | null = null) => {
        try {
            if (append) setLoadingMore(true);
            else setLoading(true);

            const cursorQuery = cursor === null ? '' : `&cursor=${encodeURIComponent(cursor)}`;
            const { data } = await client.get(`/api/assets/?limit=${PAGE_SIZE}${cursorQuery}`);

            // The API now returns { assets: {...}, total: X }
            const newAssets = data.assets || {};
            const totalCount = data.total || 0;

            if (append) {
                setAssets(prev => ({ ...prev, ...newAssets }));
            } else {
                setAssets(newAssets);
            }

            setTotal(totalCount);
            setHasMore(data.has_more === true);
            setTotalIsExact(data.total_is_exact !== false);
            setNextCursor(typeof data.next_cursor === 'string' ? data.next_cursor : null);
        } catch (err) {
            console.error('Failed to fetch assets:', err);
        } finally {
            setLoading(false);
            setLoadingMore(false);
        }
    };

    const handleLoadMore = () => {
        fetchAssets(true, nextCursor);
    };

    // Bulk archive/delete drive the Gmail-style selection toolbar — confirm once,
    // then apply to every eligible selected row (skipping any the user can't manage).
    const bulkArchive = async (list: Asset[]) => {
        const targets = list.filter(a => a.status !== 'archived' && canManage(a));
        if (!targets.length) return;
        if (!window.confirm(`Archive ${targets.length} document${targets.length > 1 ? 's' : ''}? They'll be removed from Chat answers but kept as records.`)) return;
        for (const a of targets) {
            try {
                await client.post(`/api/assets/${encodeURIComponent(a.doc_id)}/archive`);
                setAssets(prev => ({ ...prev, [a.doc_id]: { ...prev[a.doc_id], status: 'archived' } }));
            } catch (err) {
                console.error(`Failed to archive asset ${a.doc_id}:`, err);
            }
        }
        clearSelection();
    };

    const bulkDelete = async (list: Asset[]) => {
        const targets = list.filter(a => isAdmin || a.owner === user?.email);
        if (!targets.length) return;
        if (!window.confirm(`Delete ${targets.length} document${targets.length > 1 ? 's' : ''}? This also removes ${targets.length > 1 ? 'them' : 'it'} from the search index.`)) return;
        for (const a of targets) {
            try {
                await client.delete(`/api/assets/${encodeURIComponent(a.doc_id)}`);
                setAssets(prev => {
                    const next = { ...prev };
                    delete next[a.doc_id];
                    return next;
                });
            } catch (err) {
                console.error(`Failed to delete asset ${a.doc_id}:`, err);
            }
        }
        clearSelection();
    };

    // ── Share / download / email-draft (Cubie-generated docs & any stored file) ──
    const [shareDoc, setShareDoc] = useState<{ docId: string; filename: string } | null>(null);
    const [shareEmails, setShareEmails] = useState('');
    const [shareBusy, setShareBusy] = useState(false);
    const [shareError, setShareError] = useState<string | null>(null);
    const [shareResult, setShareResult] = useState<string | null>(null);
    const [draftBusy, setDraftBusy] = useState<string | null>(null);
    const [draftUrl, setDraftUrl] = useState<string | null>(null);

    // Version history: one document at a time; the modal owns its own
    // upload / restore flow and asks us to refresh the row when it changes.
    const [versionsDoc, setVersionsDoc] = useState<Asset | null>(null);
    const versionBlockedReason = (a: Asset): string | null => {
        if (a.status === 'archived') return 'This document is archived. Re-upload it to bring it back before adding a version.';
        if (a.tags?.includes('cubie-generated')) return 'Cubie-generated briefs can’t take new versions — generate a fresh brief instead.';
        if (a.topic === 'Meeting Transcripts') return 'Meeting transcripts are synced from the recording and can’t be versioned.';
        return null;
    };

    // Gmail-style row selection → a top action toolbar (replaces per-row hover icons).
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const clearSelection = () => setSelected(new Set());
    const toggleSelect = (docId: string) =>
        setSelected(prev => {
            const next = new Set(prev);
            next.has(docId) ? next.delete(docId) : next.add(docId);
            return next;
        });

    const handleDownload = async (doc_id: string, filename?: string) => {
        try {
            const res = await client.get(`/api/assets/${encodeURIComponent(doc_id)}/download`, { responseType: 'blob' });
            const url = URL.createObjectURL(res.data);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename || 'document';
            a.click();
            URL.revokeObjectURL(url);
        } catch {
            alert('Download failed — no stored file is available for this document.');
        }
    };

    const handleEmailDraft = async (doc_id: string) => {
        setDraftBusy(doc_id);
        setDraftUrl(null);
        try {
            const res = await client.post(`/api/assets/${encodeURIComponent(doc_id)}/email-draft`, {});
            const url = res.data?.gmail_draft_url;
            if (url) {
                // Cubie never sends — it drafts in YOUR Gmail; you review and hit send.
                setDraftUrl(url);
                window.open(url, '_blank', 'noopener,noreferrer');
            }
        } catch (e: any) {
            alert(e?.response?.data?.detail || 'Could not create the Gmail draft.');
        } finally {
            setDraftBusy(null);
        }
    };

    const openShare = (doc_id: string, filename?: string) => {
        setShareEmails('');
        setShareError(null);
        setShareResult(null);
        setShareDoc({ docId: doc_id, filename: filename || doc_id });
    };

    const submitShare = async () => {
        if (!shareDoc) return;
        const emails = shareEmails.split(/[,;\s]+/).map(e => e.trim()).filter(Boolean);
        if (!emails.length) { setShareError(`Enter at least one work email at ${companyName}.`); return; }
        setShareBusy(true);
        setShareError(null);
        try {
            const res = await client.post(`/api/assets/${encodeURIComponent(shareDoc.docId)}/share`, { emails });
            const added: string[] = res.data?.shared_with || [];
            setShareResult(added.length
                ? `Shared with ${added.join(', ')} — they'll see it on their Assets page and get a notification.`
                : 'They already had access.');
            if (added.length) {
                setAssets(prev => ({
                    ...prev,
                    [shareDoc.docId]: {
                        ...prev[shareDoc.docId],
                        allowed_emails: [...(prev[shareDoc.docId]?.allowed_emails || []), ...added],
                    },
                }));
            }
        } catch (e: any) {
            setShareError(e?.response?.data?.detail || 'Share failed — please try again.');
        } finally {
            setShareBusy(false);
        }
    };

    const filteredAssets = Object.entries(assets).filter(([_, asset]) => {
        // Frontend secondary filter (backend already isolates by dept, but safety first)
        const isMeetingTranscript = asset.topic === 'Meeting Transcripts';
        const isVisible = isAdmin ||
                          asset.owner === user?.email ||
                          (!isMeetingTranscript && asset.department === user?.department) ||
                          (asset.allowed_emails && asset.allowed_emails.includes(user?.email || ''));

        if (!isVisible) return false;

        const matchesSearch =
            asset.doc_id.toLowerCase().includes(searchTerm.toLowerCase()) ||
            (asset.original_filename && asset.original_filename.toLowerCase().includes(searchTerm.toLowerCase())) ||
            asset.owner.toLowerCase().includes(searchTerm.toLowerCase()) ||
            (asset.topic && asset.topic.toLowerCase().includes(searchTerm.toLowerCase())) ||
            (asset.description && asset.description.toLowerCase().includes(searchTerm.toLowerCase())) ||
            (asset.tags && asset.tags.some(t => t.toLowerCase().includes(searchTerm.toLowerCase())));

        const matchesStatus =
            statusFilter === 'all' ||
            (statusFilter === 'active' && asset.status !== 'archived') ||
            (statusFilter === 'archived' && asset.status === 'archived');

        return matchesSearch && matchesStatus;
    });

    const sortedAssets = [...filteredAssets].sort((a, b) => {
        const assetA = a[1];
        const assetB = b[1];

        let valA: any = assetA[sortField] || '';
        let valB: any = assetB[sortField] || '';

        if (sortField === 'original_filename') {
            valA = assetA.original_filename?.toLowerCase() || a[0].toLowerCase();
            valB = assetB.original_filename?.toLowerCase() || b[0].toLowerCase();
        } else if (typeof valA === 'string') {
            valA = valA.toLowerCase();
            valB = valB.toLowerCase();
        }

        if (valA < valB) return sortDirection === 'asc' ? -1 : 1;
        if (valA > valB) return sortDirection === 'asc' ? 1 : -1;
        return 0;
    });

    const handleSort = (field: SortField) => {
        if (sortField === field) {
            setSortDirection(prev => prev === 'asc' ? 'desc' : 'asc');
        } else {
            setSortField(field);
            setSortDirection('asc');
        }
    };

    const canManage = (asset: Asset) =>
        isAdmin || asset.owner === user?.email || !!user?.roles.includes('editor');

    const getStatusColor = (status: string) => {
        switch (status) {
            case 'approved': return 'bg-green-100 text-green-700';
            case 'archived': return 'bg-gray-200 text-gray-500';
            case 'failed': return 'bg-red-100 text-red-700';
            default: return 'bg-gray-100 text-gray-700';
        }
    };

    const statusLabel = (status: string) => (status === 'approved' ? 'active' : status.replace('_', ' '));

    // Derived selection state for the action toolbar. Single-doc actions (share,
    // email draft) enable only at exactly one selection; download/archive/delete
    // apply across the selection (each skips rows the user can't act on).
    const selectedAssets = [...selected].map(id => assets[id]).filter(Boolean) as Asset[];
    const selCount = selectedAssets.length;
    const singleSel = selCount === 1 ? selectedAssets[0] : null;
    const canDownloadSel = selectedAssets.some(a => !!a.source_path);
    const canShareSel = !!singleSel && singleSel.status !== 'archived' && (isAdmin || singleSel.owner === user?.email);
    const canEmailSel = !!singleSel && !!singleSel.source_path && singleSel.status !== 'archived';
    const canArchiveSel = selectedAssets.some(a => a.status !== 'archived' && canManage(a));
    const canDeleteSel = selectedAssets.some(a => isAdmin || a.owner === user?.email);
    const allVisibleSelected = sortedAssets.length > 0 && sortedAssets.every(([id]) => selected.has(id));
    const toggleSelectAll = () =>
        setSelected(allVisibleSelected ? new Set() : new Set(sortedAssets.map(([id]) => id)));

    const transcriptStatusColor = (s: string) => {
        switch (s) {
            case 'Indexed': return 'bg-green-100 text-green-700';
            case 'Pending': return 'bg-yellow-100 text-yellow-700';
            case 'Deleted': return 'bg-gray-200 text-gray-500';
            case 'Failed': return 'bg-red-100 text-red-700';
            default: return 'bg-gray-100 text-gray-700';
        }
    };

    return (
        <div className="p-8 max-w-7xl">
            <div data-tour="assets-header" className="mb-6 flex items-start justify-between gap-4">
                <div>
                    <h1 className="text-[32px] font-bold text-[#1a202c] mb-1">Assets</h1>
                    <p className="text-[#64748b] text-[15px]">Documents and meeting transcripts in the knowledge base</p>
                </div>
                <button
                    data-tour="assets-upload"
                    onClick={() => setShowUpload(true)}
                    disabled={!canUploadDocuments}
                    title={canUploadDocuments ? undefined : uploadBlockedReason}
                    className="bg-brand hover:bg-brand-hover text-white text-sm font-semibold px-5 py-2.5 rounded-lg transition-colors flex items-center gap-2 shadow-sm flex-shrink-0 disabled:bg-gray-300 disabled:cursor-not-allowed disabled:hover:bg-gray-300"
                >
                    <UploadIcon size={16} strokeWidth={2.5} /> Upload a document
                </button>
            </div>

            <div data-tour="assets-tabs" className="mb-6 border-b border-gray-200 flex gap-6">
                <button
                    onClick={() => selectTab('documents')}
                    className={`pb-3 px-1 text-sm font-semibold transition-colors flex items-center gap-2 ${activeTab === 'documents' ? 'text-brand border-b-2 border-brand' : 'text-gray-500 hover:text-gray-700'}`}
                >
                    <FileBox size={16} /> Documents
                </button>
                <button
                    onClick={() => selectTab('transcripts')}
                    className={`pb-3 px-1 text-sm font-semibold transition-colors flex items-center gap-2 ${activeTab === 'transcripts' ? 'text-brand border-b-2 border-brand' : 'text-gray-500 hover:text-gray-700'}`}
                >
                    <Mic size={16} /> Transcripts
                    {transcripts.length > 0 && (
                        <span className="ml-1 px-2 py-0.5 text-xs font-semibold bg-gray-100 text-gray-600 rounded-full">
                            {transcripts.length}
                        </span>
                    )}
                </button>
                {showPrivateTab && (
                    <button
                        data-tour="assets-private-tab"
                        onClick={() => selectTab('private')}
                        className={`pb-3 px-1 text-sm font-semibold transition-colors flex items-center gap-2 ${activeTab === 'private' ? 'text-brand border-b-2 border-brand' : 'text-gray-500 hover:text-gray-700'}`}
                        title="Files you attached in Ask Cubie. Only you can see them."
                    >
                        <Lock size={16} /> My private files
                        {(privateCount ?? 0) > 0 && (
                            <span className="ml-1 px-2 py-0.5 text-xs font-semibold bg-gray-100 text-gray-600 rounded-full">
                                {privateCount}
                            </span>
                        )}
                    </button>
                )}
                {isAdmin && (
                    <button
                        onClick={() => selectTab('topics')}
                        className={`pb-3 px-1 text-sm font-semibold transition-colors flex items-center gap-2 ${activeTab === 'topics' ? 'text-brand border-b-2 border-brand' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        <Tags size={16} /> Topics
                    </button>
                )}
            </div>

            {activeTab === 'topics' && isAdmin ? (
                <TopicsTab />
            ) : activeTab === 'private' && showPrivateTab ? (
                <PrivateFilesTab onCount={setPrivateCount} />
            ) : activeTab === 'transcripts' ? (
                <div>
                    {transcriptsLoading ? (
                        <div className="text-center py-12">
                            <div className="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-brand border-r-transparent"></div>
                            <p className="mt-4 text-gray-500">Loading transcripts...</p>
                        </div>
                    ) : transcripts.length === 0 ? (
                        <div className="text-center py-12 bg-white rounded-xl border border-gray-200">
                            <Mic size={48} className="mx-auto mb-4 text-gray-300" />
                            <p className="text-gray-500">No transcripts yet</p>
                            <p className="text-gray-400 text-sm mt-1">Meeting transcripts ingest automatically every 15 minutes.</p>
                        </div>
                    ) : (
                        <div className="bg-white border border-gray-100 rounded-lg max-h-[60vh] overflow-y-auto">
                            <table className="w-full text-left relative">
                                <thead className="sticky top-0 bg-gray-50/95 backdrop-blur z-10 shadow-sm">
                                    <tr className="text-gray-500 text-sm border-b border-gray-100">
                                        <th className="py-3 px-4 font-semibold">Meeting</th>
                                        <th className="py-3 px-4 font-semibold w-1/6">Host</th>
                                        <th className="py-3 px-4 font-semibold w-32">Participants</th>
                                        <th className="py-3 px-4 font-semibold w-32">Status</th>
                                        <th className="py-3 px-4 font-semibold w-32">Created</th>
                                        <th className="py-3 px-4 font-semibold text-center w-24">Actions</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-gray-50">
                                    {transcripts.map(t => (
                                        <motion.tr
                                            key={t.doc_id}
                                            initial={{ opacity: 0 }}
                                            animate={{ opacity: 1 }}
                                            className="group hover:bg-gray-50/50 transition-colors"
                                        >
                                            <td className="py-3 px-4">
                                                <div className="flex items-center gap-3">
                                                    <div className="h-9 w-9 bg-brand-tint rounded-full flex items-center justify-center text-brand shadow-sm">
                                                        <Mic size={16} strokeWidth={2.5} />
                                                    </div>
                                                    <span className="font-semibold text-sm text-gray-900 truncate max-w-[300px]" title={t.original_filename || t.doc_id}>
                                                        {t.original_filename || t.doc_id.substring(0, 16) + '...'}
                                                    </span>
                                                </div>
                                            </td>
                                            <td className="py-3 px-4 text-sm text-gray-700 truncate max-w-[150px]" title={t.owner}>
                                                {t.is_owner ? 'You' : t.owner}
                                            </td>
                                            <td className="py-3 px-4">
                                                <span className="inline-flex items-center gap-1 text-sm text-gray-600">
                                                    <Users size={14} /> {t.participant_count}
                                                </span>
                                            </td>
                                            <td className="py-3 px-4">
                                                <span className={`px-2 py-1 text-xs font-semibold rounded-md flex w-fit ${transcriptStatusColor(t.status)}`}>
                                                    {t.status}
                                                </span>
                                            </td>
                                            <td className="py-3 px-4 text-sm text-gray-500">
                                                {formatInstantDate(t.created_at)}
                                            </td>
                                            <td className="py-3 px-4 text-center">
                                                <div className="flex justify-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                                    {t.status !== 'Deleted' && (t.is_owner || isAdmin) && (
                                                        <button
                                                            onClick={() => handleDeleteTranscript(t.doc_id)}
                                                            className="p-1.5 rounded text-gray-400 hover:text-red-600 hover:bg-red-50 transition-colors"
                                                            title="Delete transcript"
                                                        >
                                                            <Trash2 size={16} />
                                                        </button>
                                                    )}
                                                </div>
                                            </td>
                                        </motion.tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>
            ) : (
            <>
            {/* Filters */}
            <div data-tour="assets-search" className="bg-white rounded-[12px] border border-gray-100 p-5 mb-8 shadow-[0_2px_8px_rgb(0,0,0,0.02)]">
                <div className="flex gap-4">
                    {/* Search */}
                    <div className="flex-1 relative">
                        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-400" size={18} strokeWidth={2.5} />
                        <input
                            type="text"
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            placeholder="Search by file name, owner, topic, tag, or description..."
                            className="w-full pl-10 border border-gray-200 rounded-[6px] px-4 py-2.5 text-[14px] text-gray-700 placeholder-gray-400 focus:outline-none focus:border-brand/50 focus:ring-2 focus:ring-brand/10 transition-all font-medium"
                        />
                    </div>

                    {/* Status Filter */}
                    <select
                        value={statusFilter}
                        onChange={(e) => setStatusFilter(e.target.value as 'active' | 'archived' | 'all')}
                        className="border border-gray-200 rounded-[6px] px-4 py-2.5 text-[14px] text-gray-700 focus:outline-none focus:border-brand/50 focus:ring-2 focus:ring-brand/10 bg-white font-medium cursor-pointer"
                    >
                        <option value="active">Active</option>
                        <option value="archived">Archived</option>
                        <option value="all">All</option>
                    </select>
                </div>
            </div>

            {/* Selection action toolbar — Gmail-style: appears when rows are picked */}
            {selCount > 0 && (
                <div className="sticky top-0 z-20 mb-3 flex flex-wrap items-center gap-2 rounded-[10px] border border-brand/20 bg-brand/[0.04] px-4 py-2.5 shadow-sm">
                    <button onClick={clearSelection} className="mr-1 rounded p-1 text-gray-500 hover:bg-gray-200/60 hover:text-gray-800" title="Clear selection">
                        <X size={16} />
                    </button>
                    <span className="mr-2 text-sm font-semibold text-gray-700">{selCount} selected</span>
                    {canDownloadSel && (
                        <button
                            onClick={() => selectedAssets.forEach(a => a.source_path && handleDownload(a.doc_id, a.original_filename))}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-semibold text-gray-700 hover:border-brand/40 hover:text-brand"
                        >
                            <Download size={15} /> Download
                        </button>
                    )}
                    <button
                        onClick={() => singleSel && openShare(singleSel.doc_id, singleSel.original_filename)}
                        disabled={!canShareSel}
                        title={selCount > 1 ? 'Select a single document to share' : 'Share with a teammate (they get access + a notification)'}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-semibold text-gray-700 hover:border-brand/40 hover:text-brand disabled:cursor-not-allowed disabled:opacity-40"
                    >
                        <Share2 size={15} /> Share
                    </button>
                    <button
                        onClick={() => singleSel && handleEmailDraft(singleSel.doc_id)}
                        disabled={!canEmailSel || (!!singleSel && draftBusy === singleSel.doc_id)}
                        title={selCount > 1 ? 'Select a single document to draft an email' : 'Draft a Gmail with this doc attached (Cubie never sends — you review it)'}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-semibold text-gray-700 hover:border-brand/40 hover:text-brand disabled:cursor-not-allowed disabled:opacity-40"
                    >
                        {singleSel && draftBusy === singleSel.doc_id ? <Loader2 size={15} className="animate-spin" /> : <Mail size={15} />} Email draft
                    </button>
                    <button
                        onClick={() => singleSel && setVersionsDoc(singleSel)}
                        disabled={!singleSel}
                        title={selCount > 1 ? 'Select a single document to see its versions' : 'Version history — upload a new version, download or restore an old one'}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-semibold text-gray-700 hover:border-brand/40 hover:text-brand disabled:cursor-not-allowed disabled:opacity-40"
                    >
                        <History size={15} /> Versions
                        {singleSel && (singleSel.current_version ?? 1) > 1 && (
                            <span className="rounded bg-brand/10 px-1.5 py-0.5 text-[10px] font-semibold text-brand">v{singleSel.current_version}</span>
                        )}
                    </button>
                    {canArchiveSel && (
                        <button
                            onClick={() => bulkArchive(selectedAssets)}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-semibold text-gray-700 hover:border-brand/40 hover:text-brand"
                        >
                            <Archive size={15} /> Archive
                        </button>
                    )}
                    {canDeleteSel && (
                        <button
                            onClick={() => bulkDelete(selectedAssets)}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm font-semibold text-gray-700 hover:border-red-300 hover:text-red-600"
                        >
                            <Trash2 size={15} /> Delete
                        </button>
                    )}
                </div>
            )}

            {/* Assets Table */}
            {loading ? (
                <div className="text-center py-12">
                    <div className="inline-block h-8 w-8 animate-spin rounded-full border-4 border-solid border-brand border-r-transparent"></div>
                    <p className="mt-4 text-gray-500">Loading assets...</p>
                </div>
            ) : filteredAssets.length === 0 ? (
                <div className="text-center py-12 bg-white rounded-xl border border-gray-200">
                    <FileBox size={48} className="mx-auto mb-4 text-gray-300" />
                    <p className="text-gray-500">{hasMore ? 'More documents may be available' : 'No documents found'}</p>
                    {!hasMore && (canUploadDocuments ? (
                        <button
                            onClick={() => setShowUpload(true)}
                            className="mt-4 text-brand hover:text-brand-hover text-sm font-semibold inline-flex items-center gap-1.5"
                        >
                            <UploadIcon size={15} /> Upload a document
                        </button>
                    ) : (
                        <p className="mt-4 mx-auto max-w-md text-sm text-gray-500">
                            {uploadBlockedReason}
                        </p>
                    ))}
                </div>
            ) : (
                <div data-tour="assets-list" className="bg-white border border-gray-100 rounded-lg max-h-[60vh] overflow-y-auto">
                    <table className="w-full text-left relative">
                        <thead className="sticky top-0 bg-gray-50/95 backdrop-blur z-10 shadow-sm">
                            <tr className="text-gray-500 text-sm border-b border-gray-100">
                                <th className="py-3 pl-4 pr-1 w-10">
                                    <input
                                        type="checkbox"
                                        checked={allVisibleSelected}
                                        onChange={toggleSelectAll}
                                        className="h-4 w-4 cursor-pointer accent-brand"
                                        title="Select all"
                                    />
                                </th>
                                <th className="py-3 px-4 font-semibold w-1/3 cursor-pointer hover:bg-gray-100/50 transition-colors" onClick={() => handleSort('original_filename')}><div className="flex items-center gap-1.5">File Name <ArrowUpDown size={14} className={sortField === 'original_filename' ? 'text-brand' : 'text-gray-400 opacity-50'}/></div></th>
                                <th className="py-3 px-4 font-semibold w-1/6 cursor-pointer hover:bg-gray-100/50 transition-colors" onClick={() => handleSort('owner')}><div className="flex items-center gap-1.5">Owner <ArrowUpDown size={14} className={sortField === 'owner' ? 'text-brand' : 'text-gray-400 opacity-50'}/></div></th>
                                <th className="py-3 px-4 font-semibold w-32 cursor-pointer hover:bg-gray-100/50 transition-colors" onClick={() => handleSort('department')}><div className="flex items-center gap-1.5">Department <ArrowUpDown size={14} className={sortField === 'department' ? 'text-brand' : 'text-gray-400 opacity-50'}/></div></th>
                                <th className="py-3 px-4 font-semibold w-1/6 cursor-pointer hover:bg-gray-100/50 transition-colors" onClick={() => handleSort('topic')}><div className="flex items-center gap-1.5">Topic <ArrowUpDown size={14} className={sortField === 'topic' ? 'text-brand' : 'text-gray-400 opacity-50'}/></div></th>
                                <th className="py-3 px-4 font-semibold w-32 cursor-pointer hover:bg-gray-100/50 transition-colors" onClick={() => handleSort('status')}><div className="flex items-center gap-1.5">Status <ArrowUpDown size={14} className={sortField === 'status' ? 'text-brand' : 'text-gray-400 opacity-50'}/></div></th>
                                <th className="py-3 px-4 font-semibold w-32 cursor-pointer hover:bg-gray-100/50 transition-colors" onClick={() => handleSort('created_at')}><div className="flex items-center gap-1.5">Created <ArrowUpDown size={14} className={sortField === 'created_at' ? 'text-brand' : 'text-gray-400 opacity-50'}/></div></th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-50">
                            {sortedAssets.map(([doc_id, asset]) => (
                                <motion.tr
                                    key={doc_id}
                                    initial={{ opacity: 0 }}
                                    animate={{ opacity: 1 }}
                                    className={`group transition-colors ${selected.has(doc_id) ? 'bg-brand/[0.06]' : 'hover:bg-gray-50/50'}`}
                                >
                                    <td className="py-3 pl-4 pr-1 w-10">
                                        <input
                                            type="checkbox"
                                            checked={selected.has(doc_id)}
                                            onChange={() => toggleSelect(doc_id)}
                                            className="h-4 w-4 cursor-pointer accent-brand"
                                        />
                                    </td>
                                    <td className="py-3 px-4">
                                        <div className="flex items-start gap-3">
                                            <div className="h-9 w-9 bg-brand-tint rounded-full flex items-center justify-center text-brand text-sm font-bold shadow-sm flex-shrink-0 mt-0.5">
                                                <FileBox size={16} strokeWidth={2.5} />
                                            </div>
                                            <div className="flex flex-col max-w-[320px] xl:max-w-[420px]">
                                                <span className="font-semibold text-sm text-gray-900 truncate flex items-center gap-1.5" title={asset.original_filename || doc_id}>
                                                    <span className="truncate">{asset.original_filename || doc_id.substring(0, 16) + '...'}</span>
                                                    {(asset.current_version ?? 1) > 1 && (
                                                        <button
                                                            type="button"
                                                            onClick={(e) => { e.stopPropagation(); setVersionsDoc(asset); }}
                                                            className="flex-shrink-0 rounded bg-brand/10 px-1.5 py-0.5 text-[10px] font-semibold text-brand hover:bg-brand/20"
                                                            title={`Version ${asset.current_version} — click for history`}
                                                        >
                                                            v{asset.current_version}
                                                        </button>
                                                    )}
                                                </span>
                                                {asset.description && (
                                                    <span className="text-xs text-gray-500 line-clamp-2 mt-0.5" title={asset.description}>
                                                        {asset.description}
                                                    </span>
                                                )}
                                                {asset.owner !== user?.email && asset.allowed_emails?.includes(user?.email || '') && (
                                                    <span className="mt-1 w-fit rounded bg-brand/10 px-1.5 py-0.5 text-[10px] font-semibold text-brand">
                                                        Shared with you
                                                    </span>
                                                )}
                                                {asset.tags && asset.tags.length > 0 && (
                                                    <div className="flex flex-wrap gap-1 mt-1">
                                                        {asset.tags.slice(0, 4).map(t => (
                                                            <span key={t} className="bg-gray-100 text-gray-600 text-[10px] font-semibold px-1.5 py-0.5 rounded">
                                                                {t}
                                                            </span>
                                                        ))}
                                                        {asset.tags.length > 4 && (
                                                            <span className="text-[10px] text-gray-400 font-semibold px-1">+{asset.tags.length - 4}</span>
                                                        )}
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    </td>
                                    <td className="py-3 px-4">
                                        <span className="text-sm text-gray-700 truncate block max-w-[150px]" title={asset.owner}>
                                            {asset.owner}
                                        </span>
                                    </td>
                                    <td className="py-3 px-4">
                                        <span className="text-sm text-gray-700 truncate block max-w-[120px]" title={asset.department || 'N/A'}>
                                            {asset.department || 'N/A'}
                                        </span>
                                    </td>
                                    <td className="py-3 px-4">
                                        <span className="text-sm text-gray-700 truncate block max-w-[120px]" title={asset.topic || 'N/A'}>
                                            {asset.topic || 'N/A'}
                                        </span>
                                    </td>
                                    <td className="py-3 px-4">
                                        <span className={`px-2 py-1 text-xs font-semibold rounded-md flex w-fit ${getStatusColor(asset.status)}`}>
                                            {statusLabel(asset.status)}
                                        </span>
                                    </td>
                                    <td className="py-3 px-4">
                                        <span className="text-sm text-gray-500">
                                            {formatInstantDate(asset.created_at)}
                                        </span>
                                    </td>
                                </motion.tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            {/* Pagination / Load More */}
            {!loading && hasMore && (
                <div className="mt-8 flex justify-center">
                    <button
                        onClick={handleLoadMore}
                        disabled={loadingMore}
                        className="px-6 py-2.5 bg-white border border-gray-200 rounded-lg text-sm font-semibold text-gray-700 hover:bg-gray-50 hover:border-brand/30 transition-all flex items-center gap-2 shadow-sm disabled:opacity-50"
                    >
                        {loadingMore ? (
                            <div className="h-4 w-4 animate-spin rounded-full border-2 border-solid border-brand border-r-transparent"></div>
                        ) : null}
                        {loadingMore ? 'Loading more...' : 'Load More Assets'}
                    </button>
                </div>
            )}

            {/* Stats */}
            <div data-tour="assets-pagination" className="mt-6 text-sm text-gray-500 flex justify-between items-center">
                <span>{totalIsExact
                    ? `Showing ${Object.keys(assets).length} of ${total} total assets`
                    : `Showing ${Object.keys(assets).length} assets; more may be available`}</span>
                {searchTerm && (
                    <span className="text-brand font-medium">
                        * Filters applied to current view ({filteredAssets.length} matches)
                    </span>
                )}
            </div>
            </>
            )}

            <UploadModal
                open={showUpload}
                onClose={() => setShowUpload(false)}
                onUploaded={() => fetchAssets()}
            />

            {/* Gmail-draft notice — Cubie drafts, the human sends. */}
            {draftUrl && (
                <div className="fixed bottom-6 right-6 z-50 flex items-center gap-3 rounded-xl border border-brand/20 bg-white px-4 py-3 shadow-lg">
                    <Mail size={16} className="text-brand" />
                    <span className="text-sm text-gray-700">
                        Draft created in your Gmail —{' '}
                        <a href={draftUrl} target="_blank" rel="noopener noreferrer" className="font-semibold text-brand underline">
                            open it to review &amp; send
                        </a>
                    </span>
                    <button onClick={() => setDraftUrl(null)} className="text-gray-400 hover:text-gray-600" title="Dismiss">
                        <X size={15} />
                    </button>
                </div>
            )}

            {/* Share-with-teammate modal */}
            {shareDoc && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={() => !shareBusy && setShareDoc(null)}>
                    <div className="w-full max-w-md rounded-2xl bg-white p-6 shadow-xl" onClick={e => e.stopPropagation()}>
                        <div className="mb-1 flex items-center justify-between">
                            <h3 className="text-lg font-bold text-gray-900">Share document</h3>
                            <button onClick={() => setShareDoc(null)} className="text-gray-400 hover:text-gray-600" title="Close">
                                <X size={18} />
                            </button>
                        </div>
                        <p className="mb-4 truncate text-sm text-gray-500" title={shareDoc.filename}>{shareDoc.filename}</p>
                        <label className="mb-1 block text-xs font-semibold text-gray-600">
                            Teammate email(s) — work addresses at {companyName} only, comma-separated
                        </label>
                        <input
                            type="text"
                            value={shareEmails}
                            onChange={e => setShareEmails(e.target.value)}
                            placeholder="name@company.com, colleague@company.com"
                            className="mb-3 w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-brand focus:ring-2 focus:ring-brand/10"
                            autoFocus
                        />
                        <p className="mb-4 text-xs text-gray-500">
                            They'll see it on their Assets page, get an in-app notification, and an email with the Cubie link.
                        </p>
                        {shareError && <p className="mb-3 text-sm font-medium text-red-600">{shareError}</p>}
                        {shareResult && <p className="mb-3 text-sm font-medium text-green-700">{shareResult}</p>}
                        <div className="flex justify-end gap-2">
                            <button
                                onClick={() => setShareDoc(null)}
                                className="rounded-lg px-4 py-2 text-sm font-semibold text-gray-600 hover:bg-gray-100"
                            >
                                {shareResult ? 'Done' : 'Cancel'}
                            </button>
                            <button
                                onClick={submitShare}
                                disabled={shareBusy}
                                className="flex items-center gap-2 rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-hover disabled:opacity-50"
                            >
                                {shareBusy ? <Loader2 size={14} className="animate-spin" /> : <Share2 size={14} />}
                                Share
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {versionsDoc && (
                <VersionHistoryModal
                    docId={versionsDoc.doc_id}
                    filename={versionsDoc.original_filename || versionsDoc.doc_id}
                    canManage={canManage(versionsDoc)}
                    blockedReason={versionBlockedReason(versionsDoc)}
                    onClose={() => setVersionsDoc(null)}
                    onChanged={() => fetchAssets()}
                />
            )}
        </div>
    );
};
