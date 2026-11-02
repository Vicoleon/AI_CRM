// Files in Ask Cubie (/api/chat/files). Mirrors backend/routers/chat_files.py.
// Every file is private to the person who attached it: these calls only ever
// see the caller's own files.
import client from './client';

export type ChatFileKind = 'pdf' | 'word' | 'spreadsheet' | 'presentation' | 'text' | 'image';

/** Where the reading lives. `private_library`: later conversations can find it.
 *  `chat_only`: it is used in the chats it is attached to. */
export type ChatFileIndexStatus = 'pending' | 'private_library' | 'chat_only';

export interface ChatFile {
    id: string;
    filename: string;
    kind: ChatFileKind;
    kind_label: string;
    mime_type: string | null;
    size_bytes: number;
    detail: string | null;
    summary: string;
    index_status: ChatFileIndexStatus;
    text_truncated: boolean;
    session_id: string | null;
    created_at: string | null;
    /** On an attach: the same file was attached before, and that reading came back. */
    reused?: boolean;
    /** Publishing it puts the file itself in Assets (true), or a Word copy of
     *  what Cubie read (a picture, a scan, a spreadsheet). */
    publishes_as_original?: boolean;
    /** Copies of this file published to Assets that are still there (newest first). */
    published?: PublishedCopy[];
}

/** A shared copy of a private file in Assets (the private file stays private). */
export interface PublishedCopy {
    doc_id: string;
    filename: string;
    department: string | null;
    is_company_wide: boolean;
    /** Assets got a Word copy of what Cubie read (a picture, a scan, a spreadsheet). */
    rendition: boolean;
    asset_status: string;
    published_at: string | null;
}

/** What a sent message stores about each of its files (chat_messages.metadata_json). */
export interface ChatAttachmentRef {
    id: string;
    filename: string;
    kind: ChatFileKind;
    size_bytes: number;
}

/** What the file picker offers. Kept in step with SUFFIXES in
 *  rubik_core/chat_files/extract.py; the server is the real check. */
export const ACCEPTED_FILE_TYPES = [
    '.pdf', '.docx', '.xlsx', '.xlsm', '.csv', '.tsv', '.pptx', '.txt', '.md', '.json',
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.heic', '.heif',
].join(',');

export const MAX_FILE_BYTES = 20_000_000;
export const MAX_FILES_PER_MESSAGE = 5;

export async function attachFile(
    file: File,
    sessionId: string | null,
    signal?: AbortSignal,
): Promise<ChatFile> {
    const form = new FormData();
    form.append('file', file, file.name);
    if (sessionId) form.append('session_id', sessionId);
    const { data } = await client.post<ChatFile>('/api/chat/files', form, {
        signal,
        // A scanned PDF is read page by page by the model; this bounds a hang.
        timeout: 180_000,
    });
    return data;
}

export async function listChatFiles(): Promise<{ files: ChatFile[]; can_attach: boolean; can_publish: boolean }> {
    const { data } = await client.get('/api/chat/files');
    return data;
}

export interface PublishRequest {
    topic: string;
    department: string;
    is_company_wide: boolean;
    /** Blank = the summary Cubie wrote when the file was attached. */
    description: string;
}

/** Copy a private file into Assets. Its own Assets upload permission applies. */
export async function publishChatFile(
    id: string,
    body: PublishRequest,
): Promise<{ doc_id: string; filename: string; rendition: boolean; status: string }> {
    const { data } = await client.post(`/api/chat/files/${encodeURIComponent(id)}/publish`, body, {
        // A scan's pages are rendered into the Word copy before the upload.
        timeout: 120_000,
    });
    return data;
}

export async function deleteChatFile(id: string): Promise<void> {
    await client.delete(`/api/chat/files/${encodeURIComponent(id)}`);
}

/** Fetch with the session's auth, then hand the browser a download. */
export async function downloadChatFile(file: Pick<ChatFile, 'id' | 'filename'>): Promise<void> {
    const res = await client.get(`/api/chat/files/${encodeURIComponent(file.id)}/download`, {
        responseType: 'blob',
    });
    const url = URL.createObjectURL(res.data as Blob);
    try {
        const a = document.createElement('a');
        a.href = url;
        a.download = file.filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
    } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
}

/** A refusal from the server is a plain sentence; show it as is. */
export function uploadErrorMessage(err: unknown): string {
    const e = err as { response?: { status?: number; data?: { detail?: unknown } }; code?: string };
    const detail = e?.response?.data?.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (e?.response?.status === 413) return 'That file is too large. Attach a smaller one.';
    if (e?.code === 'ECONNABORTED') return 'Reading that file took too long. Try again.';
    return "Cubie couldn't read that file. Try again.";
}

/** The kind a file name says it is, for its chip while it is being read. */
export function kindOfName(name: string): ChatFileKind {
    const ext = name.toLowerCase().split('.').pop() || '';
    if (ext === 'pdf') return 'pdf';
    if (ext === 'docx') return 'word';
    if (['xlsx', 'xlsm', 'csv', 'tsv'].includes(ext)) return 'spreadsheet';
    if (ext === 'pptx') return 'presentation';
    if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'heic', 'heif'].includes(ext)) return 'image';
    return 'text';
}

export function formatBytes(n: number): string {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
