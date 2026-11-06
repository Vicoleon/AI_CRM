// Slack inbox snapshot used by the Homefeed "Today" strip (mention/channel
// counts) and the Productivity → Slack tab (full lists). Independent of
// useBriefing so Slack's per-conversation latency never blocks the rest of the
// page — the strip and tab just render the counts when they arrive. UC-15.
// Both read one cached copy (productivityCache.ts).
import { slackActivityResource, useCachedResource } from './productivityCache';

export interface SlackActivityChannel {
    id: string;
    name?: string | null;
    is_private: boolean;
    unread_count: number;
    app_link?: string | null;
}

export interface SlackActivityMention {
    channel_id: string;
    channel_name?: string | null;
    user_name?: string | null;
    text: string;
    ts: string;
    permalink?: string | null;
    app_link?: string | null;
}

export interface SlackActivityDM {
    channel_id: string;
    counterparty_name: string;
    unread_count: number;
    is_group: boolean;
    app_link?: string | null;
    ts: string;
    preview: string;
}

export interface SlackActivity {
    is_connected: boolean;
    is_configured: boolean;
    channel_count: number;
    unread_message_count: number;
    mention_count: number;
    dm_count: number;
    channels: SlackActivityChannel[];
    mentions: SlackActivityMention[];
    dms: SlackActivityDM[];
    error?: string | null;
}

export interface UseSlackActivityResult {
    activity: SlackActivity | null;
    loading: boolean;
    refreshing: boolean;
    error: string | null;
    refresh: () => Promise<void>;
}

export function useSlackActivity(opts?: { load?: boolean }): UseSlackActivityResult {
    const { data, loading, refreshing, error, refresh } = useCachedResource(slackActivityResource, opts);
    return { activity: data, loading, refreshing, error, refresh };
}
