// UC-43 Documents tab — recent Drive activity for the current user.
// UC-44 adds:
//   - "Folders" button in the header opens a picker. Selections persist
//     via PUT /api/drive/preferences and scope future loads of
//     /api/drive/recent-activity to the chosen folders.
//   - Per-row "Add to Knowledge" button opens a modal that pushes the
//     Drive file into the same RAG corpus /api/ingest feeds.
//
// Backend wraps UC-29's GoogleDriveClient.list_recent_files /
// list_files_in_folders primitives.
import React, { useEffect, useState } from 'react';
import {
    FileBox,
    ExternalLink,
    Users,
    User as UserIcon,
    FolderCog,
    Plus,
    Check,
} from 'lucide-react';
import client from '../../api/client';
import { useAuth } from '../../context/AuthContext';
import { parseInstant } from '../../utils/datetime';
import type { DriveActivityItem } from './types';
import { driveActivityResource, useCachedResource } from './productivityCache';
import { DriveFolderPickerModal, type DriveFolderRef } from './DriveFolderPickerModal';
import { AddToAssetsModal } from './AddToAssetsModal';


const MIME_LABEL: Record<string, string> = {
    'application/vnd.google-apps.document': 'Doc',
    'application/vnd.google-apps.spreadsheet': 'Sheet',
    'application/vnd.google-apps.presentation': 'Slides',
    'application/vnd.google-apps.form': 'Form',
    'application/vnd.google-apps.drawing': 'Drawing',
    'application/vnd.google-apps.folder': 'Folder',
    'application/pdf': 'PDF',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'Word',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'Excel',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'PPT',
    'text/csv': 'CSV',
    'text/plain': 'Text',
    'image/png': 'Image',
    'image/jpeg': 'Image',
};


function mimeLabel(mime: string): string {
    return MIME_LABEL[mime] || 'File';
}


function relativeTime(iso?: string): string {
    const at = parseInstant(iso);
    if (!at) return '';
    const diffMs = Date.now() - at.getTime();
    const minutes = Math.floor(diffMs / 60_000);
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days}d ago`;
    return at.toLocaleDateString();
}


export const DocumentsColumn: React.FC = () => {
    // Same assets:create gate the ingest endpoint enforces — without this the
    // row button invites a click that can only ever come back 403.
    const { canUploadDocuments } = useAuth();
    // The recent-activity list comes from the shared Productivity cache, which
    // Home warms in the background, so this tab usually paints at once.
    const { data: activity, loading, error, refresh } = useCachedResource(driveActivityResource);
    const items = activity ? activity.items || [] : null;
    // A folder change re-scopes the list; show the skeleton, not the old scope.
    const [rescoping, setRescoping] = useState(false);

    // UC-44: folder picker state
    const [folders, setFolders] = useState<DriveFolderRef[]>([]);
    const [foldersLoaded, setFoldersLoaded] = useState(false);
    const [pickerOpen, setPickerOpen] = useState(false);

    // UC-44: add-to-assets modal state
    const [addTarget, setAddTarget] = useState<DriveActivityItem | null>(null);
    const [ingestedIds, setIngestedIds] = useState<Set<string>>(new Set());

    // Toast-ish banner shown briefly after a successful ingestion.
    const [toast, setToast] = useState<string | null>(null);
    useEffect(() => {
        if (!toast) return;
        const t = setTimeout(() => setToast(null), 3000);
        return () => clearTimeout(t);
    }, [toast]);

    // Load saved folders on mount (the activity list loads via the cache).
    useEffect(() => {
        client
            .get<{ folders: DriveFolderRef[] }>('/api/drive/preferences')
            .then(res => setFolders(res.data.folders || []))
            .catch(() => setFolders([]))
            .finally(() => setFoldersLoaded(true));
    }, []);

    // When folders change (after the picker saves), refetch activity.
    const handleFolderSave = (next: DriveFolderRef[]) => {
        setFolders(next);
        setRescoping(true);
        void refresh().finally(() => setRescoping(false));
    };

    return (
        <div>
            {/* Folder scope header */}
            <div className="px-4 py-3 border-b border-gray-100 bg-gray-50/60 flex items-center justify-between gap-3">
                <div className="text-xs text-gray-600 flex-1 min-w-0">
                    {!foldersLoaded ? (
                        <span className="text-gray-400">Loading folder scope…</span>
                    ) : folders.length === 0 ? (
                        <span>
                            <span className="text-gray-500">
                                Showing recent activity across all of Drive.
                            </span>{' '}
                            <button
                                onClick={() => setPickerOpen(true)}
                                className="text-brand hover:underline font-medium"
                            >
                                Pick folders to narrow this.
                            </button>
                        </span>
                    ) : (
                        <span className="inline-flex items-center gap-1 flex-wrap">
                            <span className="text-gray-500">Showing files from:</span>
                            {folders.slice(0, 3).map(f => (
                                <span
                                    key={f.id}
                                    className="text-[11px] px-2 py-0.5 rounded-full bg-brand/10 text-brand"
                                >
                                    {f.name}
                                </span>
                            ))}
                            {folders.length > 3 && (
                                <span className="text-[11px] text-gray-400">
                                    +{folders.length - 3} more
                                </span>
                            )}
                        </span>
                    )}
                </div>
                <button
                    onClick={() => setPickerOpen(true)}
                    className="text-xs font-medium text-gray-600 hover:text-brand inline-flex items-center gap-1 shrink-0"
                >
                    <FolderCog size={14} />
                    Folders
                </button>
            </div>

            {toast && (
                <div className="m-4 p-2 rounded bg-green-50 border border-green-200 text-xs text-green-800 flex items-center gap-2">
                    <Check size={14} />
                    {toast}
                </div>
            )}

            {/* List */}
            {loading || rescoping ? (
                <div className="animate-pulse space-y-3 p-4">
                    {Array.from({ length: 6 }).map((_, i) => (
                        <div
                            key={i}
                            className={`h-3 bg-gray-200 rounded ${
                                i === 0 ? 'w-3/4' : i % 2 === 0 ? 'w-full' : 'w-5/6'
                            }`}
                        />
                    ))}
                </div>
            ) : error && !items ? (
                <div className="flex flex-col items-center justify-center py-12 text-gray-400">
                    <FileBox size={28} className="mb-2 opacity-50" />
                    <p className="text-sm text-red-600">{error}</p>
                </div>
            ) : !items || items.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-12 text-gray-400">
                    <FileBox size={28} className="mb-2 opacity-50" />
                    <p className="text-sm">
                        {folders.length === 0
                            ? 'No Drive activity in the last 7 days.'
                            : 'No recent activity in the selected folders.'}
                    </p>
                </div>
            ) : (
                <div className="divide-y divide-gray-100">
                    {items.map(file => {
                        const editorName =
                            file.last_modifying_user_name ||
                            file.last_modifying_user_email?.split('@')[0] ||
                            null;
                        const isMe = file.is_owned_by_me;
                        const alreadyIngested = ingestedIds.has(file.id);
                        return (
                            <div
                                key={file.id}
                                className="flex items-start gap-3 px-4 py-3 hover:bg-gray-50 transition-colors"
                            >
                                <FileBox
                                    size={18}
                                    className="mt-0.5 text-gray-400 shrink-0"
                                />
                                <a
                                    href={file.web_view_link || '#'}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="flex-1 min-w-0"
                                >
                                    <div className="flex items-center gap-2">
                                        <span className="text-sm font-medium text-gray-900 truncate">
                                            {file.name}
                                        </span>
                                        <span className="text-[10px] uppercase font-bold tracking-wider px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-600 shrink-0">
                                            {mimeLabel(file.mime_type)}
                                        </span>
                                        {file.is_shared && (
                                            <Users
                                                size={12}
                                                className="text-gray-400 shrink-0"
                                                aria-label="Shared"
                                            />
                                        )}
                                    </div>
                                    <div className="text-xs text-gray-500 mt-0.5 truncate">
                                        {isMe ? (
                                            <span className="inline-flex items-center gap-1">
                                                <UserIcon size={11} />
                                                You
                                            </span>
                                        ) : editorName ? (
                                            <span>{editorName}</span>
                                        ) : null}
                                        {(editorName || isMe) && (
                                            <span className="text-gray-300 mx-1">·</span>
                                        )}
                                        <span>
                                            edited {relativeTime(file.modified_time)}
                                        </span>
                                    </div>
                                </a>

                                {/* Add-to-Knowledge button */}
                                <button
                                    onClick={() => setAddTarget(file)}
                                    disabled={alreadyIngested || !canUploadDocuments}
                                    title={
                                        alreadyIngested
                                            ? 'Already added to Knowledge'
                                            : !canUploadDocuments
                                              ? 'You don’t have permission to add documents to the knowledge base. Ask an admin to give your account upload access.'
                                              : 'Add this document to the knowledge base'
                                    }
                                    className={`text-xs inline-flex items-center gap-1 shrink-0 px-2 py-1 rounded transition-colors ${
                                        alreadyIngested
                                            ? 'text-green-600 bg-green-50 cursor-default'
                                            : !canUploadDocuments
                                              ? 'text-gray-300 cursor-not-allowed'
                                              : 'text-gray-500 hover:text-brand hover:bg-brand/5'
                                    }`}
                                >
                                    {alreadyIngested ? (
                                        <>
                                            <Check size={12} />
                                            Added
                                        </>
                                    ) : (
                                        <>
                                            <Plus size={12} />
                                            Knowledge
                                        </>
                                    )}
                                </button>

                                {file.web_view_link && (
                                    <a
                                        href={file.web_view_link}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="mt-1 text-gray-300 hover:text-gray-500 shrink-0"
                                        title="Open in Drive"
                                    >
                                        <ExternalLink size={14} />
                                    </a>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}

            {/* Modals */}
            <DriveFolderPickerModal
                open={pickerOpen}
                initialSelection={folders}
                onClose={() => setPickerOpen(false)}
                onSave={handleFolderSave}
            />
            <AddToAssetsModal
                open={addTarget !== null}
                fileId={addTarget?.id || ''}
                fileName={addTarget?.name || ''}
                onClose={() => setAddTarget(null)}
                onSuccess={() => {
                    if (addTarget) {
                        setIngestedIds(prev => {
                            const next = new Set(prev);
                            next.add(addTarget.id);
                            return next;
                        });
                        setToast(
                            `"${addTarget.name}" added to Knowledge.`,
                        );
                    }
                }}
            />
        </div>
    );
};
