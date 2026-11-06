import React, { useState, useEffect, useRef } from 'react';
import {
    Upload as UploadIcon,
    CheckCircle,
    XCircle,
    Loader,
    Clock,
    AlertTriangle,
    Sparkles,
    X,
} from 'lucide-react';
import client from '../api/client';
import { formatInstant } from '../utils/datetime';
import { motion, AnimatePresence } from 'framer-motion';

type ProcessingStatus = 'idle' | 'uploading' | 'processing' | 'ready' | 'failed' | 'timed_out';

interface DuplicateInfo {
    existing_doc_id: string;
    existing_filename: string | null;
    existing_uploaded_at: string | null;
    existing_owner: string | null;
}

interface UploadModalProps {
    open: boolean;
    onClose: () => void;
    /** Called when an upload is accepted and again when it's ready, so the
     *  parent (Assets) can refresh the list. */
    onUploaded: () => void;
}

const POLL_INTERVAL_MS = 30_000;
// Upper bound on how long we'll wait for Vertex RAG to finish indexing before we
// stop polling and tell the user to check back later.
const MAX_POLL_DURATION_MS = 10 * 60 * 1000;

export const UploadModal: React.FC<UploadModalProps> = ({ open, onClose, onUploaded }) => {
    const [file, setFile] = useState<File | null>(null);
    const [topic, setTopic] = useState('');
    const [topics, setTopics] = useState<string[]>([]);
    const [department, setDepartment] = useState('');
    const [departments, setDepartments] = useState<string[]>([]);
    const [description, setDescription] = useState('');
    const [tags, setTags] = useState<string[]>([]);
    const [tagDraft, setTagDraft] = useState('');
    const [isCompanyWide, setIsCompanyWide] = useState(false);
    const [error, setError] = useState('');
    const [aiLoading, setAiLoading] = useState(false);

    const [processingStatus, setProcessingStatus] = useState<ProcessingStatus>('idle');
    const [statusMessage, setStatusMessage] = useState('');
    const [trackedDocId, setTrackedDocId] = useState<string | null>(null);
    const [duplicateInfo, setDuplicateInfo] = useState<DuplicateInfo | null>(null);

    const pollIntervalRef = useRef<number | null>(null);
    const pollDeadlineRef = useRef<number | null>(null);

    useEffect(() => {
        if (open) {
            fetchTopics();
            fetchDepartments();
        }
        return () => stopPolling();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [open]);

    const fetchTopics = async () => {
        try {
            const { data } = await client.get('/api/topics/');
            setTopics(data);
            if (data.length > 0) setTopic((prev) => prev || data[0]);
        } catch (err) {
            console.error('Failed to fetch topics:', err);
        }
    };

    const fetchDepartments = async () => {
        try {
            const { data } = await client.get('/api/departments/');
            setDepartments(data);
            if (data.length > 0) setDepartment((prev) => prev || data[0]);
        } catch (err) {
            console.error('Failed to fetch departments:', err);
        }
    };

    const stopPolling = () => {
        if (pollIntervalRef.current !== null) {
            window.clearInterval(pollIntervalRef.current);
            pollIntervalRef.current = null;
        }
        pollDeadlineRef.current = null;
    };

    const resetForm = () => {
        setFile(null);
        setDescription('');
        setTags([]);
        setTagDraft('');
        setIsCompanyWide(false);
        setError('');
        setDuplicateInfo(null);
        setProcessingStatus('idle');
        setStatusMessage('');
        setTrackedDocId(null);
        stopPolling();
    };

    const handleClose = () => {
        resetForm();
        onClose();
    };

    const checkStatus = async (docId: string) => {
        try {
            const { data } = await client.get(`/api/dam/${docId}/status`);
            const next = data.status as ProcessingStatus;
            setProcessingStatus(next);
            setStatusMessage(data.message || '');
            if (next === 'ready') {
                stopPolling();
                onUploaded();
            } else if (next === 'failed') {
                stopPolling();
            } else if (
                pollDeadlineRef.current !== null &&
                Date.now() > pollDeadlineRef.current
            ) {
                stopPolling();
                // Leave the busy state so the modal can be closed; the row is
                // already in the Assets list and will flip to ready on its own.
                setProcessingStatus('timed_out');
                setStatusMessage(
                    'Still processing — this is taking longer than expected. It will appear in the list shortly.'
                );
            }
        } catch (err) {
            console.error('Status poll failed:', err);
        }
    };

    const startPolling = (docId: string) => {
        stopPolling();
        pollDeadlineRef.current = Date.now() + MAX_POLL_DURATION_MS;
        pollIntervalRef.current = window.setInterval(() => {
            checkStatus(docId);
        }, POLL_INTERVAL_MS);
    };

    const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        if (e.target.files && e.target.files[0]) {
            setFile(e.target.files[0]);
            setProcessingStatus('idle');
            setStatusMessage('');
            setTrackedDocId(null);
            setError('');
            setDuplicateInfo(null);
            stopPolling();
        }
    };

    const addTag = (raw: string) => {
        const t = raw.trim();
        if (t && !tags.includes(t)) setTags((prev) => [...prev, t]);
        setTagDraft('');
    };

    const handleTagKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
        if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            addTag(tagDraft);
        } else if (e.key === 'Backspace' && !tagDraft && tags.length) {
            setTags((prev) => prev.slice(0, -1));
        }
    };

    const generateDescription = async () => {
        if (!file) {
            setError('Choose a file first, then generate a description.');
            return;
        }
        setAiLoading(true);
        setError('');
        try {
            const fd = new FormData();
            fd.append('file', file);
            const { data } = await client.post('/api/dam/describe', fd, {
                headers: { 'Content-Type': 'multipart/form-data' },
            });
            setDescription(data.description || '');
        } catch (err: any) {
            const detail = err?.response?.data?.detail;
            setError(typeof detail === 'string' ? detail : 'Could not generate a description.');
        } finally {
            setAiLoading(false);
        }
    };

    /**
     * Core upload routine.
     *  - force=true        → "Keep both": bypass dedup, co-exist with the existing doc.
     *  - replaceDocId set  → "Replace": ingest new, then purge the named old doc.
     */
    const performUpload = async (opts: { force?: boolean; replaceDocId?: string } = {}) => {
        if (!file || !topic || !department) {
            setError('Please fill in all required fields');
            return;
        }

        setError('');
        setDuplicateInfo(null);
        setProcessingStatus('uploading');
        setStatusMessage('Uploading…');

        const formData = new FormData();
        formData.append('file', file);
        formData.append('topic', topic);
        formData.append('department', department);
        formData.append('description', description);
        formData.append('tags', tags.join(','));
        formData.append('is_company_wide', String(isCompanyWide));
        if (opts.force) formData.append('force', 'true');
        if (opts.replaceDocId) formData.append('replace_doc_id', opts.replaceDocId);

        try {
            const { data } = await client.post('/api/dam/upload', formData, {
                headers: { 'Content-Type': 'multipart/form-data' },
            });

            setTrackedDocId(data.doc_id);
            setProcessingStatus('processing');
            setStatusMessage(data.message || 'Processing… your file is being indexed.');
            startPolling(data.doc_id);
            onUploaded(); // surface the processing row immediately
        } catch (err: any) {
            const detail = err.response?.data?.detail;
            // 409 with structured detail = duplicate. Offer Replace / Keep both.
            if (err.response?.status === 409 && typeof detail === 'object' && detail.can_force) {
                setProcessingStatus('idle');
                setDuplicateInfo({
                    existing_doc_id: detail.existing_doc_id,
                    existing_filename: detail.existing_filename ?? null,
                    existing_uploaded_at: detail.existing_uploaded_at ?? null,
                    existing_owner: detail.existing_owner ?? null,
                });
                return;
            }
            setProcessingStatus('failed');
            setError(typeof detail === 'string' ? detail : 'Upload failed');
        }
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        await performUpload();
    };

    const isBusy = processingStatus === 'uploading' || processingStatus === 'processing';

    if (!open) return null;

    return (
        <AnimatePresence>
            <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 backdrop-blur-sm overflow-y-auto py-10"
                onMouseDown={(e) => {
                    if (e.target === e.currentTarget && !isBusy) handleClose();
                }}
            >
                <motion.div
                    initial={{ opacity: 0, y: 20, scale: 0.98 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: 20, scale: 0.98 }}
                    className="bg-white rounded-2xl shadow-xl border border-gray-200 w-full max-w-2xl mx-4 my-auto"
                >
                    <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
                        <div>
                            <h2 className="text-xl font-bold text-gray-900">Upload a document</h2>
                            <p className="text-sm text-gray-500">
                                Adds the document to the knowledge base — it's answerable in Chat right away.
                            </p>
                        </div>
                        <button
                            onClick={handleClose}
                            disabled={isBusy}
                            className="p-2 rounded-lg text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors disabled:opacity-40"
                            title="Close"
                        >
                            <X size={20} />
                        </button>
                    </div>

                    <form onSubmit={handleSubmit} className="p-6 space-y-5">
                        {/* File Input */}
                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-2">
                                Document File <span className="text-brand">*</span>
                            </label>
                            <div className="border-2 border-dashed border-gray-300 rounded-lg p-6 text-center hover:border-brand/50 transition-colors">
                                <input
                                    id="upload-modal-file-input"
                                    type="file"
                                    onChange={handleFileChange}
                                    accept=".pdf,.txt,.md,.docx"
                                    className="hidden"
                                />
                                <label htmlFor="upload-modal-file-input" className="cursor-pointer">
                                    <UploadIcon className="mx-auto h-10 w-10 text-gray-400" />
                                    <p className="mt-2 text-sm text-gray-600">
                                        {file ? file.name : 'Click to upload or drag and drop'}
                                    </p>
                                    <p className="text-xs text-gray-500 mt-1">PDF, DOCX, TXT, or MD files</p>
                                </label>
                            </div>
                        </div>

                        <div className="grid grid-cols-2 gap-4">
                            {/* Topic */}
                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-2">
                                    Topic <span className="text-brand">*</span>
                                </label>
                                <select
                                    value={topic}
                                    onChange={(e) => setTopic(e.target.value)}
                                    className="w-full border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-brand focus:border-brand"
                                    required
                                >
                                    {topics.map((t) => (
                                        <option key={t} value={t}>{t}</option>
                                    ))}
                                </select>
                            </div>

                            {/* Department */}
                            <div>
                                <label className="block text-sm font-medium text-gray-700 mb-2">
                                    Department <span className="text-brand">*</span>
                                </label>
                                <select
                                    value={department}
                                    onChange={(e) => setDepartment(e.target.value)}
                                    className="w-full border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-brand focus:border-brand"
                                    required
                                >
                                    {departments.map((d) => (
                                        <option key={d} value={d}>{d}</option>
                                    ))}
                                </select>
                            </div>
                        </div>

                        {/* Description + AI generate */}
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <label className="block text-sm font-medium text-gray-700">
                                    Description <span className="text-gray-400 font-normal">(optional)</span>
                                </label>
                                <button
                                    type="button"
                                    onClick={generateDescription}
                                    disabled={!file || aiLoading}
                                    className="inline-flex items-center gap-1.5 text-xs font-semibold text-brand hover:text-brand-hover disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                                    title={!file ? 'Choose a file first' : 'Generate a summary from the file'}
                                >
                                    {aiLoading ? (
                                        <Loader size={14} className="animate-spin" />
                                    ) : (
                                        <Sparkles size={14} />
                                    )}
                                    {aiLoading ? 'Generating…' : 'Generate with AI'}
                                </button>
                            </div>
                            <textarea
                                value={description}
                                onChange={(e) => setDescription(e.target.value)}
                                rows={3}
                                placeholder="A short summary of what this document is and what it answers. Leave blank to auto-generate on upload."
                                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand focus:border-brand resize-none"
                            />
                        </div>

                        {/* Tags */}
                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-2">
                                Tags <span className="text-gray-400 font-normal">(optional)</span>
                            </label>
                            <div className="flex flex-wrap items-center gap-2 border border-gray-300 rounded-lg px-3 py-2 focus-within:ring-2 focus-within:ring-brand focus-within:border-brand">
                                {tags.map((t) => (
                                    <span
                                        key={t}
                                        className="inline-flex items-center gap-1 bg-brand-tint text-brand text-xs font-semibold px-2 py-1 rounded-md"
                                    >
                                        {t}
                                        <button
                                            type="button"
                                            onClick={() => setTags((prev) => prev.filter((x) => x !== t))}
                                            className="hover:text-brand-hover"
                                        >
                                            <X size={12} />
                                        </button>
                                    </span>
                                ))}
                                <input
                                    type="text"
                                    value={tagDraft}
                                    onChange={(e) => setTagDraft(e.target.value)}
                                    onKeyDown={handleTagKeyDown}
                                    onBlur={() => tagDraft && addTag(tagDraft)}
                                    placeholder={tags.length ? '' : 'Type a tag and press Enter'}
                                    className="flex-1 min-w-[120px] text-sm outline-none bg-transparent"
                                />
                            </div>
                        </div>

                        {/* Company Wide Toggle */}
                        <div className="flex items-center">
                            <input
                                id="upload-modal-company-wide"
                                type="checkbox"
                                checked={isCompanyWide}
                                onChange={(e) => setIsCompanyWide(e.target.checked)}
                                className="h-4 w-4 bg-white checked:bg-brand text-brand focus:ring-brand border-gray-300 rounded"
                            />
                            <label htmlFor="upload-modal-company-wide" className="ml-2 block text-sm text-gray-900">
                                Mark as Company-Wide (accessible by all departments)
                            </label>
                        </div>

                        {/* Duplicate-detected — Replace / Keep both / Cancel */}
                        {duplicateInfo && (
                            <motion.div
                                initial={{ opacity: 0 }}
                                animate={{ opacity: 1 }}
                                className="bg-amber-50 border border-amber-300 rounded-lg p-4"
                            >
                                <div className="flex items-start gap-3">
                                    <AlertTriangle size={22} className="text-amber-600 flex-shrink-0 mt-0.5" />
                                    <div className="flex-1">
                                        <div className="font-medium text-amber-900">
                                            This file is already in the knowledge base
                                        </div>
                                        <ul className="text-sm text-amber-900 mt-2 space-y-0.5">
                                            {duplicateInfo.existing_filename && (
                                                <li>
                                                    <span className="text-amber-800/70">Filename:</span>{' '}
                                                    <span className="font-mono">{duplicateInfo.existing_filename}</span>
                                                </li>
                                            )}
                                            {duplicateInfo.existing_owner && (
                                                <li>
                                                    <span className="text-amber-800/70">Owner:</span>{' '}
                                                    {duplicateInfo.existing_owner}
                                                </li>
                                            )}
                                            {duplicateInfo.existing_uploaded_at && (
                                                <li>
                                                    <span className="text-amber-800/70">Uploaded:</span>{' '}
                                                    {formatInstant(duplicateInfo.existing_uploaded_at)}
                                                </li>
                                            )}
                                        </ul>
                                        <p className="text-sm text-amber-800/80 mt-3">
                                            Replace the existing copy, or keep both as separate documents?
                                        </p>
                                        <div className="flex gap-2 mt-3">
                                            <button
                                                type="button"
                                                onClick={() => performUpload({ replaceDocId: duplicateInfo.existing_doc_id })}
                                                className="bg-brand hover:bg-brand-hover text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
                                            >
                                                Replace existing
                                            </button>
                                            <button
                                                type="button"
                                                onClick={() => performUpload({ force: true })}
                                                className="bg-amber-600 hover:bg-amber-700 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
                                            >
                                                Keep both
                                            </button>
                                            <button
                                                type="button"
                                                onClick={() => setDuplicateInfo(null)}
                                                className="bg-white hover:bg-gray-50 text-gray-700 text-sm font-medium px-4 py-2 rounded-lg border border-gray-300 transition-colors"
                                            >
                                                Cancel
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            </motion.div>
                        )}

                        {/* Status banners */}
                        {processingStatus === 'processing' && (
                            <div className="flex items-center gap-2 text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-4">
                                <Clock size={20} className="animate-pulse" />
                                <div className="flex-1">
                                    <div className="font-medium">Processing…</div>
                                    <div className="text-sm text-amber-800/80">
                                        {statusMessage || 'Indexing in progress. We check every 30 seconds.'}
                                    </div>
                                    {trackedDocId && (
                                        <div className="text-xs text-amber-700/70 mt-1 font-mono">doc_id: {trackedDocId}</div>
                                    )}
                                </div>
                            </div>
                        )}

                        {processingStatus === 'ready' && (
                            <div className="flex items-center gap-2 text-green-600 bg-green-50 border border-green-200 rounded-lg p-4">
                                <CheckCircle size={20} />
                                <span>{statusMessage || 'Ready to query.'}</span>
                            </div>
                        )}

                        {processingStatus === 'timed_out' && (
                            <div className="flex items-center gap-2 text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-4">
                                <Clock size={20} />
                                <span>{statusMessage || 'Still processing — it will appear in the list shortly.'}</span>
                            </div>
                        )}

                        {processingStatus === 'failed' && !error && (
                            <div className="flex items-center gap-2 text-red-600 bg-red-50 border border-red-200 rounded-lg p-4">
                                <XCircle size={20} />
                                <span>{statusMessage || 'Indexing failed.'}</span>
                            </div>
                        )}

                        {error && (
                            <div className="flex items-center gap-2 text-red-600 bg-red-50 border border-red-200 rounded-lg p-4">
                                <XCircle size={20} />
                                <span>{error}</span>
                            </div>
                        )}

                        {/* Footer actions */}
                        <div className="flex justify-end gap-3 pt-2">
                            {processingStatus === 'ready' || processingStatus === 'timed_out' ? (
                                <button
                                    type="button"
                                    onClick={handleClose}
                                    className="bg-brand hover:bg-brand-hover text-white font-medium px-5 py-2.5 rounded-lg transition-colors"
                                >
                                    Done
                                </button>
                            ) : (
                                <>
                                    <button
                                        type="button"
                                        onClick={handleClose}
                                        disabled={isBusy}
                                        className="bg-white hover:bg-gray-50 text-gray-700 font-medium px-5 py-2.5 rounded-lg border border-gray-300 transition-colors disabled:opacity-50"
                                    >
                                        Cancel
                                    </button>
                                    <button
                                        type="submit"
                                        disabled={isBusy || !file || !!duplicateInfo}
                                        className="bg-brand hover:bg-brand-hover text-white font-medium px-5 py-2.5 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                                    >
                                        {processingStatus === 'uploading' && <Loader className="animate-spin" size={18} />}
                                        {processingStatus === 'processing' && <Loader className="animate-spin" size={18} />}
                                        {(processingStatus === 'idle' || processingStatus === 'failed') && <UploadIcon size={18} />}
                                        {processingStatus === 'uploading'
                                            ? 'Uploading…'
                                            : processingStatus === 'processing'
                                              ? 'Processing…'
                                              : 'Upload and Ingest'}
                                    </button>
                                </>
                            )}
                        </div>
                    </form>
                </motion.div>
            </motion.div>
        </AnimatePresence>
    );
};
