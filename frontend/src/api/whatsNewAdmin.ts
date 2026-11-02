// Admin → What's New (pages/admin/WhatsNewTab.tsx): the switch and Slack
// channel of the weekday morning post of new Cubie release notes, and the
// weekly tease. Spec §4.6.1: each workspace opts into the digest; the notes
// themselves come from RubikSoft and nobody here approves them.
//
// The server is `backend/routers/whats_new_admin.py` (prefix
// /admin/whats-new, mounted under /api, `admin_required` on every endpoint)
// over `rubik_core/whats_new/settings.py`. A refused save is a 400 whose
// `detail` is the sentence to show as it is ("Invite Cubie to #announcements
// first: type /invite @Cubie in that channel.") — `whatsNewSettingsError`
// passes it through.
import client from './client';
import { errorDetail } from '../utils/errors';

export const WHATS_NEW_SETTINGS_PATH = '/api/admin/whats-new/settings';

/** GET / PUT `/api/admin/whats-new/settings`. */
export interface WhatsNewSettings {
    slack_enabled: boolean;
    /** `#announcements` — always with its `#`. */
    channel_name: string;
    channel_id: string | null;
    /** null until a save has asked Slack. */
    bot_in_channel: boolean | null;
    channel_archived: boolean;
    /** The drawer slides open by itself the first time someone opens Cubie
     *  each week. */
    tease_enabled: boolean;
    updated_by: string | null;
    /** Naive UTC instant. */
    updated_at: string | null;
}

/** PUT body. */
export interface WhatsNewSettingsInput {
    slack_enabled: boolean;
    /** As typed: `#announcements`, `announcements` or an id. */
    channel: string;
    tease_enabled: boolean;
}

/** Every field the GET answers, in the order the router writes them. */
export const WHATS_NEW_SETTINGS_FIELDS = [
    'slack_enabled',
    'channel_name',
    'channel_id',
    'bot_in_channel',
    'channel_archived',
    'tease_enabled',
    'updated_by',
    'updated_at',
] as const satisfies ReadonlyArray<keyof WhatsNewSettings>;

export async function fetchWhatsNewSettings(): Promise<WhatsNewSettings> {
    const res = await client.get<WhatsNewSettings>(WHATS_NEW_SETTINGS_PATH);
    return res.data;
}

export async function saveWhatsNewSettings(input: WhatsNewSettingsInput): Promise<WhatsNewSettings> {
    const res = await client.put<WhatsNewSettings>(WHATS_NEW_SETTINGS_PATH, input);
    return res.data;
}

/** The server's sentence when there is one (a 400's `detail` is written for
 *  the admin, a 403 says the tab is for admins), else the fallback. */
export const whatsNewSettingsError = (e: unknown, fallback: string): string => errorDetail(e) ?? fallback;
