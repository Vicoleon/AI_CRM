// Admin → Loose Ends: the daily Slack post's on/off switch and its channel.
//
// The post goes to #sales but ships OFF; a Cubie admin turns it on and off,
// and changes the channel, here — no env var, no redeploy. The server is
// `backend/routers/loose_ends_admin.py` (prefix /admin/loose-ends, mounted
// under /api, admin-only by role) over `rubik_core/loop_closure/digest_settings.py`;
// the contract is `docs/loose_ends/README.md` §5 "Admin". `looseEndsAdmin.test.ts`
// reads the Python and pins the paths and the field names below against it.
//
// The checks are the server's: a channel Slack knows, and the Cubie bot in it
// before the post can be switched on. A refused save is a 400 whose `detail`
// is the sentence to show as it is — `digestAdminError` passes it through.
import { PRODUCT_NAME } from '../brand/names.ts';
import client from './client';

export const DIGEST_SETTINGS_PATH = '/api/admin/loose-ends/digest';
export const DIGEST_PREVIEW_PATH = '/api/admin/loose-ends/digest/preview';

/** The last post Slack accepted (`digest.last_post`). */
export interface LooseEndsDigestLastPost {
    /** The org's business day it was for, `YYYY-MM-DD`, in the company's
     *  business time zone. */
    local_date: string;
    /** Naive UTC instant — read it with `parseInstant`. */
    posted_at: string;
    /** Open loose ends it counted, across the people the scan checked. */
    item_count: number;
    channel_name: string;
}

/** GET / PUT `/api/admin/loose-ends/digest` (`loose_ends_admin._view`). */
export interface LooseEndsDigestSettings {
    enabled: boolean;
    /** `#sales` — always with its `#`. */
    channel_name: string;
    channel_id: string | null;
    /** null until a save (or a post) has checked it. */
    bot_in_channel: boolean | null;
    /** Slack said the channel is archived (a save, or a refused post): pick another. */
    channel_archived: boolean;
    updated_by: string | null;
    updated_at: string | null;
    /** `LOOP_CLOSURE_ENABLED` on this server: with the scan off nothing posts. */
    scan_enabled: boolean;
    last_post: LooseEndsDigestLastPost | null;
}

/** POST `/api/admin/loose-ends/digest/preview` (`digest.render_today`). */
export interface LooseEndsDigestPreview {
    /** The post as it would read now; empty on a quiet day. */
    text: string;
    /** Whether the next morning's scan will post it. */
    would_post: boolean;
    /** Why it would not post; when it would, a caveat (the last post was
     *  refused and the scan will try again); null when there is nothing to add. */
    reason: string | null;
}

/** Every field the GET answers, in the order the router writes them. */
export const DIGEST_SETTINGS_FIELDS = [
    'enabled',
    'channel_name',
    'channel_id',
    'bot_in_channel',
    'channel_archived',
    'updated_by',
    'updated_at',
    'scan_enabled',
    'last_post',
] as const satisfies ReadonlyArray<keyof LooseEndsDigestSettings>;

export const DIGEST_LAST_POST_FIELDS = [
    'local_date',
    'posted_at',
    'item_count',
    'channel_name',
] as const satisfies ReadonlyArray<keyof LooseEndsDigestLastPost>;

export const DIGEST_PREVIEW_FIELDS = ['text', 'would_post', 'reason'] as const satisfies ReadonlyArray<
    keyof LooseEndsDigestPreview
>;

export async function fetchDigestSettings(): Promise<LooseEndsDigestSettings> {
    const res = await client.get<LooseEndsDigestSettings>(DIGEST_SETTINGS_PATH);
    return res.data;
}

export async function saveDigestSettings(enabled: boolean, channel: string): Promise<LooseEndsDigestSettings> {
    const res = await client.put<LooseEndsDigestSettings>(DIGEST_SETTINGS_PATH, { enabled, channel });
    return res.data;
}

export async function previewDigest(): Promise<LooseEndsDigestPreview> {
    const res = await client.post<LooseEndsDigestPreview>(DIGEST_PREVIEW_PATH);
    return res.data;
}

/** The server's sentence when there is one (a 400's `detail` is written for
 *  the admin), else the fallback. Never a stack, never an object. */
export const digestAdminError = (e: unknown, fallback: string): string => {
    const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail)) {
        const msgs = detail
            .map(d => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : ''))
            .filter(Boolean);
        if (msgs.length) return msgs.join('; ');
    }
    return fallback;
};

/** `#sales` from whatever was typed (`sales`, `#sales`, ` #Sales `); a channel
 *  id stays as it is. Empty in, empty out — the server says what is missing.
 *  The server's rule (`digest_settings._looks_like_id`): an id is typed bare,
 *  upper-case, with a digit in it — `#CUSTOMERSUCCESS` is a name. */
export const displayChannel = (typed: string): string => {
    const trimmed = typed.trim();
    const bare = trimmed.replace(/^#+/, '').trim();
    if (!bare) return '';
    const isId = !trimmed.startsWith('#') && /^[CG][A-Z0-9]{6,}$/.test(bare) && /\d/.test(bare);
    return isId ? bare : `#${bare.toLowerCase()}`;
};

type StatusFields = Pick<LooseEndsDigestSettings, 'enabled' | 'channel_name' | 'scan_enabled'> &
    Partial<Pick<LooseEndsDigestSettings, 'channel_archived'>>;

/** The one-line status at the top of the card. */
export const digestStatusLine = (s: StatusFields): string => {
    if (!s.enabled) return 'Off';
    if (!s.scan_enabled) {
        return `On for ${s.channel_name}, but the Loose Ends scan is switched off on this server, so nothing posts`;
    }
    if (s.channel_archived) {
        return `On for ${s.channel_name}, but Slack says the channel is archived, so nothing posts`;
    }
    return `On — posting to ${s.channel_name} each weekday morning after the scan`;
};

/** The green line after a save, honest about a scan that is switched off. */
export const savedMessage = (s: StatusFields): string => {
    if (!s.enabled) return `Saved. The daily post is off; nothing goes to ${s.channel_name}.`;
    if (!s.scan_enabled) {
        return `Saved. The post is on for ${s.channel_name}, but the Loose Ends scan is switched off on this server, so nothing posts until it is switched on.`;
    }
    return `Saved. The next weekday-morning scan posts to ${s.channel_name}.`;
};

/** The Save button: enabled when something changed, and also with nothing
 *  changed while the bot check is not a ✓ — every save asks Slack again, so
 *  that is how the admin re-checks after typing /invite @Cubie. */
export const saveButton = (
    dirty: boolean,
    s: Pick<LooseEndsDigestSettings, 'bot_in_channel'> & Partial<Pick<LooseEndsDigestSettings, 'channel_archived'>>,
): { enabled: boolean; label: string } => {
    if (dirty) return { enabled: true, label: 'Save' };
    const recheck = s.bot_in_channel !== true || Boolean(s.channel_archived);
    return recheck ? { enabled: true, label: 'Save and re-check' } : { enabled: false, label: 'Save' };
};

export interface BotCheck {
    /** true = ✓, false = needs the invite, null = not checked yet. */
    ok: boolean | null;
    text: string;
}

/** The fix for a missing bot, worded as the server words it
 *  (`_INVITE_FIX` in `rubik_core/loop_closure/digest_settings.py`). */
export const inviteFix = (name: string): string => `type /invite @${PRODUCT_NAME} in ${name}`;

/** Whether the Cubie bot can post in the channel, said plainly with the fix. */
export const botCheck = (
    s: Pick<LooseEndsDigestSettings, 'bot_in_channel' | 'channel_name'> &
        Partial<Pick<LooseEndsDigestSettings, 'channel_archived'>>,
): BotCheck => {
    const name = s.channel_name;
    if (s.channel_archived) {
        return { ok: false, text: `Slack says ${name} is archived — pick another channel and Save` };
    }
    if (s.bot_in_channel === true) return { ok: true, text: `${PRODUCT_NAME} is in ${name}` };
    if (s.bot_in_channel === false) {
        return {
            ok: false,
            text: `${PRODUCT_NAME} isn't in ${name} yet — ${inviteFix(name)}, then Save to re-check`,
        };
    }
    return { ok: null, text: `${PRODUCT_NAME} hasn't checked ${name} yet — Save checks it` };
};

/** "Wed, Oct 7" from the post's `YYYY-MM-DD` business day (a date, not an
 *  instant: built at local midnight so no timezone moves it a day). */
export const postDateLabel = (localDate: string): string => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(localDate);
    if (!m) return localDate;
    const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
};

/** "Last post: Wed, Oct 7 to #sales — 4 open items", or "Nothing posted yet". */
export const lastPostLine = (last: LooseEndsDigestLastPost | null): string => {
    if (!last) return 'Nothing posted yet';
    const n = last.item_count;
    return `Last post: ${postDateLabel(last.local_date)} to ${last.channel_name} — ${n} open item${n === 1 ? '' : 's'}`;
};
