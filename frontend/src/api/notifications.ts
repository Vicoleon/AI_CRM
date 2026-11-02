// In-app notification feed (the `notifications` table). Uses the shared axios
// instance; full /api/... paths.
//
// Backend is polling-only by design: there is no push channel (SSE is
// deferred), so callers poll. `kind` is an open string on purpose: a producer
// can ship a new kind before this client knows about it, and an unfamiliar
// row must still render rather than break the feed.
import client from './client';

export interface AppNotification {
    id: string;
    kind: string;
    title: string;
    body: string;
    severity: 'info' | 'warn' | 'urgent';
    /** In-app destination, e.g. "/chat?session=<uuid>". */
    link: string | null;
    created_at: string;
    read_at: string | null;
}

export const fetchNotifications = async (
    opts: { unread?: boolean; limit?: number } = {},
): Promise<AppNotification[]> => {
    const { data } = await client.get<AppNotification[]>('/api/notifications/', {
        params: { unread: opts.unread ?? false, limit: opts.limit ?? 20 },
    });
    return data;
};

export const markNotificationRead = async (id: string): Promise<void> => {
    await client.post(`/api/notifications/${encodeURIComponent(id)}/read`);
};

/**
 * The chat session a notification points at, or null when it links elsewhere.
 *
 * Scheduled-task results link to their result conversation; the "task paused"
 * notice has no conversation and links to Settings instead, so callers must
 * handle null rather than assume every row is a chat.
 */
export const sessionIdFromLink = (link: string | null): string | null => {
    if (!link) return null;
    const q = link.indexOf('?');
    if (q < 0 || !link.slice(0, q).endsWith('/chat')) return null;
    return new URLSearchParams(link.slice(q + 1)).get('session');
};
