import React, { useEffect, useRef, useState } from 'react';
import {
    History,
    Upload as UploadIcon,
    Download,
    RotateCcw,
    Loader2,
    X,
    CheckCircle,
    Clock,
    XCircle,
    Sparkles,
} from 'lucide-react';
import client from '../api/client';
import { formatInstant } from '../utils/datetime';

/** The `detail` string FastAPI put on an error response, if there is one. */
const errorDetail = (e: unknown): string | null => {
    const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return typeof detail === 'string' ? detail : null;
};

/** One row of a document's version history — mirrors AssetVersionInfo. */
export interface AssetVersion {
    doc_id: string;
    version_number: number;
    is_current: boolean;
    source_path?: string | null;
    original_filename?: string | null;
    size_bytes?: number | null;
    uploaded_by?: string | null;
    note?: string | null;
    change_summary?: string | null;
    restored_from?: number | null;
    created_at: string;
}

interface VersionHistoryModalProps {
    docId: string;
    filename: string;
    /** May the viewer upload / restore? (owner, admin, or an editor in scope) */
    canManage: boolean;
    /** Why uploads are blocked for this document, if they are (archived, generated, …). */
    blockedReason?: string | null;
    onClose: () => void;
    /** Fired when the current version changed (new upload or restore finished), so the
     *  Assets list can refresh the row. */
    onChanged: () => void;
}

type IngestState = 'idle' | 'uploading' | 'processing' | 'ready' | 'failed' | 'timed_out';

const POLL_INTERVAL_MS = 15_000;
const MAX_POLL_DURATION_MS = 10 * 60 * 1000;

const formatBytes = (n?: number | null) => {
    if (!n && n !== 0) return null;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
};

export const VersionHistoryModal: React.FC<VersionHistoryModalProps> = ({
    docId,
    filename,
    canManage,
    blockedReason,
    onClose,
    onChanged,
}) => {
    // null = still loading (derived spinner; no setState-in-effect needed).
    const [versions, setVersions] = useState<AssetVersion[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [file, setFile] = useState<File | null>(null);
    const [note, setNote] = useState('');
    const [ingest, setIngest] = useState<IngestState>('idle');
    const [statusMessage, setStatusMessage] = useState('');
    const [restoring, setRestoring] = useState<number | null>(null);
    const [busyDownload, setBusyDownload] = useState<number | null>(null);
    const pollRef = useRef<number | null>(null);
    const deadlineRef = useRef<number | null>(null);
    const fileInputRef = useRef<HTMLInputElement | null>(null);

    const stopPolling = () => {
        if (pollRef.current !== null) {
            window.clearInterval(pollRef.current);
            pollRef.current = null;
        }
        deadlineRef.current = null;
    };

    const loadVersions = async () => {
        try {
            const { data } = await client.get(`/api/dam/${encodeURIComponent(docId)}/versions`);
            setVersions(data);
        } catch (e: unknown) {
            setError(errorDetail(e) || 'Could not load the version history.');
            setVersions([]);
        }
    };

    useEffect(() => {
        loadVersions();
        return () => stopPolling();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [docId]);

    const checkStatus = async () => {
        try {
            const { data } = await client.get(`/api/dam/${encodeURIComponent(docId)}/status`);
            if (data.status === 'ready') {
                stopPolling();
                setIngest('ready');
                setStatusMessage('Indexed — Chat now answers from this version.');
                await loadVersions();
                onChanged();
            } else if (data.status === 'failed') {
                stopPolling();
                setIngest('failed');
                setStatusMessage(data.message || 'Indexing failed.');
                await loadVersions();
                onChanged();
            } else if (deadlineRef.current !== null && Date.now() > deadlineRef.current) {
                stopPolling();
                setIngest('timed_out');
                setStatusMessage('Still indexing — it will switch over on its own shortly.');
            }
        } catch (e) {
            console.error('Version status poll failed:', e);
        }
    };

    const startPolling = () => {
        stopPolling();
        deadlineRef.current = Date.now() + MAX_POLL_DURATION_MS;
        pollRef.current = window.setInterval(checkStatus, POLL_INTERVAL_MS);
        // First check soon — locally the ingest is usually already done.
        window.setTimeout(checkStatus, 1500);
    };

    const uploadVersion = async () => {
        if (!file) return;
        setError(null);
        setIngest('uploading');
        setStatusMessage('Uploading…');
        const fd = new FormData();
        fd.append('file', file);
        fd.append('note', note);
        try {
            await client.post(`/api/dam/${encodeURIComponent(docId)}/versions`, fd, {
                headers: { 'Content-Type': 'multipart/form-data' },
            });
            setFile(null);
            setNote('');
            if (fileInputRef.current) fileInputRef.current.value = '';
            setIngest('processing');
            setStatusMessage('Indexing the new version. Chat keeps answering from the previous one until it is ready.');
            await loadVersions();
            onChanged();
            startPolling();
        } catch (e: unknown) {
            setIngest('idle');
            setError(errorDetail(e) || 'Upload failed.');
        }
    };

    const restore = async (v: AssetVersion) => {
        if (!window.confirm(`Make version ${v.version_number} the current version again? It is re-published as a new version, so nothing in the history is lost.`)) return;
        setError(null);
        setRestoring(v.version_number);
        try {
            await client.post(`/api/dam/${encodeURIComponent(docId)}/versions/${v.version_number}/restore`, { note: '' });
            setIngest('processing');
            setStatusMessage(`Re-indexing version ${v.version_number}…`);
            await loadVersions();
            onChanged();
            startPolling();
        } catch (e: unknown) {
            setError(errorDetail(e) || 'Restore failed.');
        } finally {
            setRestoring(null);
        }
    };

    const download = async (v: AssetVersion) => {
        setBusyDownload(v.version_number);
        try {
            const res = await client.get(
                `/api/dam/${encodeURIComponent(docId)}/versions/${v.version_number}/download`,
                { responseType: 'blob' }
            );
            const url = URL.createObjectURL(res.data);
            const a = document.createElement('a');
            a.href = url;
            a.download = v.original_filename || `${filename}-v${v.version_number}`;
            a.click();
            URL.revokeObjectURL(url);
        } catch {
            setError('Download failed — the stored file for that version is unavailable.');
        } finally {
            setBusyDownload(null);
        }
    };

    const busy = ingest === 'uploading' || ingest === 'processing' || restoring !== null;
    const current = versions?.find(v => v.is_current) ?? null;
    const canUpload = canManage && !blockedReason;

    return (
        <div
            className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 backdrop-blur-sm overflow-y-auto py-10"
            onMouseDown={e => {
                if (e.target === e.currentTarget && ingest !== 'uploading') onClose();
            }}
        >
            <div className="bg-white rounded-2xl shadow-xl border border-gray-200 w-full max-w-3xl mx-4 my-auto">
                <div className="flex items-start justify-between px-6 py-4 border-b border-gray-100">
                    <div className="min-w-0">
                        <h2 className="text-xl font-bold text-gray-900 flex items-center gap-2">
                            <History size={20} className="text-brand" /> Version history
                        </h2>
                        <p className="text-sm text-gray-500 truncate" title={filename}>
                            {filename}
                            {current && (
                                <span className="ml-2 rounded bg-brand/10 px-1.5 py-0.5 text-[11px] font-semibold text-brand">
                                    v{current.version_number} is current
                                </span>
                            )}
                        </p>
                    </div>
                    <button
                        onClick={onClose}
                        disabled={ingest === 'uploading'}
                        className="p-2 rounded-lg text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors disabled:opacity-40"
                        title="Close"
                    >
                        <X size={20} />
                    </button>
                </div>

                <div className="p-6 space-y-5">
                    {/* Upload a new version */}
                    {canManage && (
                        <div className={`rounded-xl border p-4 ${blockedReason ? 'border-gray-200 bg-gray-50' : 'border-brand/20 bg-brand/[0.03]'}`}>
                            <div className="flex items-center justify-between gap-3 mb-3">
                                <div>
                                    <div className="text-sm font-semibold text-gray-900">Upload a new version</div>
                                    <div className="text-xs text-gray-500">
                                        The document keeps its place, its shares and its bookmarks — only the file changes.
                                        Chat switches to the new version once it is indexed.
                                    </div>
                                </div>
                            </div>
                            {blockedReason ? (
                                <p className="text-sm text-gray-600">{blockedReason}</p>
                            ) : (
                                <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
                                    <div className="space-y-2">
                                        <input
                                            ref={fileInputRef}
                                            id="version-file-input"
                                            type="file"
                                            accept=".pdf,.txt,.md,.docx"
                                            disabled={busy}
                                            onChange={e => {
                                                setFile(e.target.files?.[0] ?? null);
                                                setError(null);
                                                if (ingest === 'ready' || ingest === 'failed' || ingest === 'timed_out') setIngest('idle');
                                            }}
                                            className="block w-full text-sm text-gray-700 file:mr-3 file:rounded-lg file:border-0 file:bg-white file:px-3 file:py-1.5 file:text-sm file:font-semibold file:text-brand file:shadow-sm hover:file:bg-gray-50"
                                        />
                                        <input
                                            type="text"
                                            value={note}
                                            onChange={e => setNote(e.target.value)}
                                            disabled={busy}
                                            maxLength={200}
                                            placeholder="What changed? (optional — Cubie also writes a summary)"
                                            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand focus:border-brand"
                                        />
                                    </div>
                                    <button
                                        type="button"
                                        onClick={uploadVersion}
                                        disabled={!file || busy || !canUpload}
                                        className="self-start inline-flex items-center gap-2 rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-hover disabled:opacity-50 disabled:cursor-not-allowed"
                                    >
                                        {ingest === 'uploading' ? <Loader2 size={15} className="animate-spin" /> : <UploadIcon size={15} />}
                                        {ingest === 'uploading' ? 'Uploading…' : 'Upload version'}
                                    </button>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Status */}
                    {ingest === 'processing' && (
                        <div className="flex items-center gap-2 text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3 text-sm">
                            <Clock size={18} className="animate-pulse flex-shrink-0" /> {statusMessage}
                        </div>
                    )}
                    {ingest === 'ready' && (
                        <div className="flex items-center gap-2 text-green-700 bg-green-50 border border-green-200 rounded-lg p-3 text-sm">
                            <CheckCircle size={18} className="flex-shrink-0" /> {statusMessage}
                        </div>
                    )}
                    {ingest === 'timed_out' && (
                        <div className="flex items-center gap-2 text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3 text-sm">
                            <Clock size={18} className="flex-shrink-0" /> {statusMessage}
                        </div>
                    )}
                    {ingest === 'failed' && (
                        <div className="flex items-center gap-2 text-red-700 bg-red-50 border border-red-200 rounded-lg p-3 text-sm">
                            <XCircle size={18} className="flex-shrink-0" /> {statusMessage} Chat still answers from the previous version; restore it or upload the file again.
                        </div>
                    )}
                    {error && (
                        <div className="flex items-center gap-2 text-red-700 bg-red-50 border border-red-200 rounded-lg p-3 text-sm">
                            <XCircle size={18} className="flex-shrink-0" /> {error}
                        </div>
                    )}

                    {/* History */}
                    {versions === null ? (
                        <div className="py-8 text-center text-sm text-gray-500">
                            <Loader2 size={20} className="mx-auto mb-2 animate-spin text-brand" /> Loading history…
                        </div>
                    ) : versions.length === 0 ? (
                        <div className="py-8 text-center text-sm text-gray-500">No versions recorded.</div>
                    ) : (
                        <ol className="relative border-l border-gray-200 ml-3 space-y-4">
                            {versions.map(v => (
                                <li key={v.version_number} className="ml-5">
                                    <span
                                        className={`absolute -left-[9px] mt-1.5 h-4 w-4 rounded-full border-2 border-white ${v.is_current ? 'bg-brand' : 'bg-gray-300'}`}
                                    />
                                    <div className={`rounded-xl border p-3 ${v.is_current ? 'border-brand/30 bg-brand/[0.03]' : 'border-gray-200 bg-white'}`}>
                                        <div className="flex flex-wrap items-center gap-2">
                                            <span className="text-sm font-bold text-gray-900">v{v.version_number}</span>
                                            {v.is_current && (
                                                <span className="rounded bg-brand px-1.5 py-0.5 text-[10px] font-semibold text-white">Current</span>
                                            )}
                                            {v.restored_from && (
                                                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-semibold text-gray-600">
                                                    Restored from v{v.restored_from}
                                                </span>
                                            )}
                                            <span className="text-xs text-gray-500">
                                                {formatInstant(v.created_at)}
                                                {v.uploaded_by ? ` · ${v.uploaded_by}` : ''}
                                                {formatBytes(v.size_bytes) ? ` · ${formatBytes(v.size_bytes)}` : ''}
                                            </span>
                                            <div className="ml-auto flex items-center gap-1">
                                                <button
                                                    onClick={() => download(v)}
                                                    disabled={busyDownload === v.version_number || !v.source_path}
                                                    className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-2.5 py-1 text-xs font-semibold text-gray-700 hover:border-brand/40 hover:text-brand disabled:opacity-40"
                                                    title="Download this version"
                                                >
                                                    {busyDownload === v.version_number ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />} Download
                                                </button>
                                                {canUpload && !v.is_current && (
                                                    <button
                                                        onClick={() => restore(v)}
                                                        disabled={busy}
                                                        className="inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white px-2.5 py-1 text-xs font-semibold text-gray-700 hover:border-brand/40 hover:text-brand disabled:opacity-40"
                                                        title="Make this the current version again"
                                                    >
                                                        {restoring === v.version_number ? <Loader2 size={13} className="animate-spin" /> : <RotateCcw size={13} />} Restore
                                                    </button>
                                                )}
                                            </div>
                                        </div>
                                        <div className="mt-1 text-sm text-gray-800 truncate" title={v.original_filename || ''}>
                                            {v.original_filename || '—'}
                                        </div>
                                        {v.note && <div className="mt-1 text-sm text-gray-700 italic">“{v.note}”</div>}
                                        {v.change_summary ? (
                                            <div className="mt-2 flex items-start gap-1.5 rounded-lg bg-white/70 border border-gray-100 p-2 text-xs text-gray-700">
                                                <Sparkles size={13} className="mt-0.5 flex-shrink-0 text-brand" />
                                                <span><span className="font-semibold text-gray-800">What changed:</span> {v.change_summary}</span>
                                            </div>
                                        ) : v.version_number > 1 && v.is_current && ingest === 'processing' ? (
                                            <div className="mt-2 text-xs text-gray-400 italic">Cubie is writing the change summary…</div>
                                        ) : null}
                                    </div>
                                </li>
                            ))}
                        </ol>
                    )}
                </div>
            </div>
        </div>
    );
};
