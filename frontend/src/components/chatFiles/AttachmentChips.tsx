// Attachment chips for Ask Cubie: the files waiting in the chat box, and the
// files a sent message carried. Every file is private to the person who
// attached it (see rubik_core/chat_files/__init__.py).
import React from 'react';
import { AlertCircle, FileSpreadsheet, FileText, Image as ImageIcon, Loader2, Lock, MessageSquare, Presentation, X } from 'lucide-react';
import type { ChatAttachmentRef, ChatFile, ChatFileKind } from '../../api/chatFiles';
import { formatBytes, kindOfName } from '../../api/chatFiles';

/** A file in the chat box, from the moment it is picked until it is sent. */
export interface PendingAttachment {
    localId: string;
    name: string;
    size: number;
    status: 'reading' | 'ready' | 'error';
    file?: ChatFile;
    error?: string;
    controller?: AbortController;
}

export const KindIcon: React.FC<{ kind: ChatFileKind; size?: number; className?: string }> = ({ kind, size = 14, className }) => {
    if (kind === 'image') return <ImageIcon size={size} className={className} />;
    if (kind === 'spreadsheet') return <FileSpreadsheet size={size} className={className} />;
    if (kind === 'presentation') return <Presentation size={size} className={className} />;
    return <FileText size={size} className={className} />;
};

/** Where a file's reading lives, in the words the person sees. */
export const WhereBadge: React.FC<{ file: ChatFile }> = ({ file }) =>
    file.index_status === 'private_library' ? (
        <span
            className="inline-flex items-center gap-1 text-[10px] font-semibold text-emerald-700"
            title="In your private library: only you can see it, and Cubie can find it in later conversations too."
        >
            <Lock size={10} /> Private library
        </span>
    ) : file.index_status === 'chat_only' ? (
        <span
            className="inline-flex items-center gap-1 text-[10px] font-semibold text-amber-700"
            title="Only you can see it. Cubie uses it in the conversations you attach it to, but can't find it from other conversations."
        >
            <MessageSquare size={10} /> This chat only
        </span>
    ) : null;

export const ComposerAttachments: React.FC<{
    items: PendingAttachment[];
    onRemove: (localId: string) => void;
}> = ({ items, onRemove }) => {
    if (items.length === 0) return null;
    return (
        <div className="flex flex-wrap gap-2 mb-2" aria-label="Attached files">
            {items.map(item => {
                const kind = item.file?.kind ?? kindOfName(item.name);
                const tone =
                    item.status === 'error'
                        ? 'border-red-200 bg-red-50'
                        : 'border-gray-200 bg-gray-50';
                return (
                    <div
                        key={item.localId}
                        className={`flex items-start gap-2 max-w-[320px] rounded-lg border ${tone} px-2.5 py-1.5`}
                    >
                        <div className="mt-0.5 text-brand flex-shrink-0">
                            {item.status === 'reading' ? (
                                <Loader2 size={14} className="animate-spin" />
                            ) : item.status === 'error' ? (
                                <AlertCircle size={14} className="text-red-600" />
                            ) : (
                                <KindIcon kind={kind} />
                            )}
                        </div>
                        <div className="min-w-0 flex-1">
                            <div className="text-xs font-medium text-gray-800 truncate" title={item.name}>
                                {item.name}
                            </div>
                            <div className="text-[10px] text-gray-500 flex items-center gap-2">
                                {item.status === 'reading' && <span>Reading…</span>}
                                {item.status === 'error' && (
                                    <span className="text-red-700 whitespace-normal">{item.error}</span>
                                )}
                                {item.status === 'ready' && item.file && (
                                    <>
                                        <span>{item.file.detail || formatBytes(item.file.size_bytes)}</span>
                                        <WhereBadge file={item.file} />
                                    </>
                                )}
                            </div>
                        </div>
                        <button
                            type="button"
                            onClick={() => onRemove(item.localId)}
                            className="p-0.5 rounded text-gray-400 hover:text-gray-700 hover:bg-gray-200 flex-shrink-0"
                            aria-label={`Remove ${item.name}`}
                            title="Remove"
                        >
                            <X size={12} />
                        </button>
                    </div>
                );
            })}
        </div>
    );
};

/** The files a sent message carried, under its bubble text. */
export const MessageAttachments: React.FC<{ files: ChatAttachmentRef[]; hasText: boolean }> = ({ files, hasText }) => (
    <div className={`flex flex-wrap gap-1.5 ${hasText ? 'mt-2' : ''}`}>
        {files.map(f => (
            <span
                key={f.id}
                className="inline-flex items-center gap-1.5 rounded-md bg-white/15 px-2 py-1 text-xs max-w-[240px]"
                title={`${f.filename} (private to you)`}
            >
                <KindIcon kind={f.kind} size={12} />
                <span className="truncate">{f.filename}</span>
            </span>
        ))}
    </div>
);
