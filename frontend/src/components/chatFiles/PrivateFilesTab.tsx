// Assets → My private files: the files this person attached in Ask Cubie.
// Only ever their own (the server filters on the caller); nobody else, admins
// included, has a view of them.
import React, { useCallback, useEffect, useState } from 'react';
import { Download, FileBox, Loader2, Lock, Paperclip, Share2, Trash2 } from 'lucide-react';
import { deleteChatFile, downloadChatFile, formatBytes, listChatFiles } from '../../api/chatFiles';
import type { ChatFile } from '../../api/chatFiles';
import { formatInstantDate } from '../../utils/datetime';
import { KindIcon, WhereBadge } from './AttachmentChips';
import { PublishToAssetsModal } from './PublishToAssetsModal';

/** Why the publish button is off: the same permission as any Assets upload. */
const PUBLISH_BLOCKED =
    'Publishing to Assets needs permission to add documents to the knowledge base. Ask an admin to give your account upload access.';

export const PrivateFilesTab: React.FC<{ onCount?: (n: number) => void }> = ({ onCount }) => {
    const [files, setFiles] = useState<ChatFile[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [busyId, setBusyId] = useState<string | null>(null);
    const [canPublish, setCanPublish] = useState(false);
    const [publishing, setPublishing] = useState<ChatFile | null>(null);
    const [notice, setNotice] = useState<string | null>(null);

    const load = useCallback(async () => {
        try {
            const data = await listChatFiles();
            setFiles(data.files);
            setCanPublish(!!data.can_publish);
            setError(null);
            onCount?.(data.files.length);
        } catch {
            setError("Your files couldn't be loaded. Try again in a moment.");
            setFiles([]);
        }
    }, [onCount]);

    useEffect(() => { void load(); }, [load]);

    const remove = async (f: ChatFile) => {
        if (!window.confirm(
            `Delete "${f.filename}"? Cubie will no longer find it from your conversations. `
            + 'A conversation you attached it to keeps what Cubie read there until you delete that conversation.',
        )) return;
        setBusyId(f.id);
        try {
            await deleteChatFile(f.id);
            setFiles(prev => {
                const next = (prev || []).filter(x => x.id !== f.id);
                onCount?.(next.length);
                return next;
            });
        } catch {
            setError(`"${f.filename}" couldn't be deleted. Try again.`);
        } finally {
            setBusyId(null);
        }
    };

    const download = async (f: ChatFile) => {
        setBusyId(f.id);
        try {
            await downloadChatFile(f);
        } catch {
            setError(`"${f.filename}" couldn't be downloaded. Try again.`);
        } finally {
            setBusyId(null);
        }
    };

    return (
        <div>
            <div className="mb-4 flex items-start gap-2 rounded-lg border border-gray-200 bg-white px-4 py-3 text-sm text-gray-600">
                <Lock size={16} className="mt-0.5 flex-shrink-0 text-brand" />
                <p>
                    Files you attached in Ask Cubie. <span className="font-medium">Only you can see them</span>:
                    not your team, not your manager, not an admin. Files in your private library can be found
                    from any of your conversations; files marked "This chat only" are used in the
                    conversations you attached them to.
                </p>
            </div>
            {error && <p className="mb-3 text-sm text-red-700">{error}</p>}
            {notice && <p className="mb-3 text-sm text-emerald-700">{notice}</p>}
            {files === null ? (
                <div className="text-center py-12">
                    <Loader2 className="mx-auto animate-spin text-brand" size={28} />
                    <p className="mt-4 text-gray-500">Loading your files...</p>
                </div>
            ) : files.length === 0 ? (
                <div className="text-center py-12 bg-white rounded-xl border border-gray-200">
                    <Paperclip size={48} className="mx-auto mb-4 text-gray-300" />
                    <p className="text-gray-500">No private files yet</p>
                    <p className="text-gray-400 text-sm mt-1">
                        Use the paperclip in Ask Cubie to attach a document, spreadsheet, deck or picture.
                    </p>
                </div>
            ) : (
                <ul className="bg-white border border-gray-100 rounded-lg divide-y divide-gray-100">
                    {files.map(f => (
                        <li key={f.id} className="px-4 py-3 flex items-start gap-3">
                            <div className="mt-0.5 text-brand">
                                <KindIcon kind={f.kind} size={18} />
                            </div>
                            <div className="min-w-0 flex-1">
                                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                                    <span className="font-medium text-gray-900 truncate" title={f.filename}>{f.filename}</span>
                                    <WhereBadge file={f} />
                                </div>
                                <div className="text-xs text-gray-500 mt-0.5">
                                    {f.kind_label}
                                    {f.detail ? ` · ${f.detail}` : ''} · {formatBytes(f.size_bytes)}
                                    {f.created_at ? ` · added ${formatInstantDate(f.created_at)}` : ''}
                                </div>
                                {f.summary && <p className="text-sm text-gray-600 mt-1">{f.summary}</p>}
                                {f.published && f.published.length > 0 && (
                                    <p className="text-xs text-gray-600 mt-1 flex items-center gap-1">
                                        <FileBox size={12} className="text-brand" />
                                        In Assets as "{f.published[0].filename}"
                                        {f.published[0].is_company_wide
                                            ? ', company-wide'
                                            : f.published[0].department ? `, ${f.published[0].department}` : ''}
                                        {f.published[0].asset_status === 'archived' ? ' (archived)' : ''}
                                    </p>
                                )}
                            </div>
                            <div className="flex items-center gap-1 flex-shrink-0">
                                <button
                                    type="button"
                                    onClick={() => { setNotice(null); setPublishing(f); }}
                                    disabled={busyId === f.id || !canPublish}
                                    className="p-2 rounded-lg text-gray-500 hover:text-brand hover:bg-brand/10 disabled:opacity-40 disabled:cursor-not-allowed"
                                    title={canPublish ? 'Publish a copy to Assets' : PUBLISH_BLOCKED}
                                    aria-label={`Publish ${f.filename} to Assets`}
                                >
                                    <Share2 size={16} />
                                </button>
                                <button
                                    type="button"
                                    onClick={() => download(f)}
                                    disabled={busyId === f.id}
                                    className="p-2 rounded-lg text-gray-500 hover:text-brand hover:bg-brand/10 disabled:opacity-50"
                                    title="Download"
                                    aria-label={`Download ${f.filename}`}
                                >
                                    <Download size={16} />
                                </button>
                                <button
                                    type="button"
                                    onClick={() => remove(f)}
                                    disabled={busyId === f.id}
                                    className="p-2 rounded-lg text-gray-500 hover:text-red-700 hover:bg-red-50 disabled:opacity-50"
                                    title="Delete"
                                    aria-label={`Delete ${f.filename}`}
                                >
                                    <Trash2 size={16} />
                                </button>
                            </div>
                        </li>
                    ))}
                </ul>
            )}
            {publishing && (
                <PublishToAssetsModal
                    file={publishing}
                    onClose={() => setPublishing(null)}
                    onPublished={result => {
                        setNotice(
                            `Published "${result.filename}" to Assets. It is being indexed now; `
                            + 'it shows under Documents in a minute or two.',
                        );
                        void load();
                    }}
                />
            )}
        </div>
    );
};
