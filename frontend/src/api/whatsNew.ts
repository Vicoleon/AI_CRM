// What's New: the drawer's feed of Cubie release notes, and the opened count.
//
// Spec §4.6.1: RubikSoft writes each release note once (the Cubie changelog)
// and every workspace reads it. Nobody here drafts, approves or edits a note:
// the drawer only reads, filtered on the server to the features this person
// can use. Until the changelog has notes the feed is empty and the drawer says
// "No updates yet."
//
// The server is `backend/routers/whats_new.py` (prefix /whats-new, mounted
// under /api OUTSIDE the copilot gate, so EOS-only people get the drawer too)
// over `rubik_core/whats_new/` (how it works: `docs/whats_new/README.md`).
// `whatsNew.test.ts` pins the paths and field names below against the router.
//
// Every timestamp is naive UTC — read it with `parseInstant`.
import client from './client';
import { errorDetail } from '../utils/errors';

// ─── wire types ──────────────────────────────────────────────────────────────

/** How a person reached a note: the `?src=slack` link in the morning post,
 *  or the drawer's "Try it" link. One count per person per note per source. */
export type ClickSource = 'slack' | 'try_it';

/** One release note, as the drawer shows it. */
export interface WhatsNewItem {
    /** The changelog entry's id: a short token (`rel-2026-10-27`). */
    id: string;
    title: string;
    /** A sentence or two of plain language. */
    body: string;
    /** A page inside the app ("/eos/rocks") for "Try it"; null = no link. The
     *  server drops any link that would leave the app. */
    path: string | null;
    /** Label of the feature the note is about ("For <label> users"); null when
     *  it is for everyone. */
    feature_label: string | null;
    /** Naive UTC instant. */
    published_at: string;
}

/** GET `/api/whats-new`. */
export interface WhatsNewFeed {
    /** Published notes the reader may see, newest first, at most 60. */
    items: WhatsNewItem[];
    /** Admin switch: the drawer may slide open once a week by itself. */
    tease_enabled: boolean;
}

// Every field each answer carries. `whatsNew.test.ts` checks the lists are
// complete against the interfaces above and that the server writes each name.

export const WHATS_NEW_FEED_FIELDS = ['items', 'tease_enabled'] as const satisfies ReadonlyArray<keyof WhatsNewFeed>;

export const WHATS_NEW_ITEM_FIELDS = [
    'id',
    'title',
    'body',
    'path',
    'feature_label',
    'published_at',
] as const satisfies ReadonlyArray<keyof WhatsNewItem>;

// ─── paths ───────────────────────────────────────────────────────────────────

/** Every path the client calls, in one place (pinned by whatsNew.test.ts). */
export const WHATS_NEW_PATHS = {
    feed: '/api/whats-new',
    open: (id: string): string => `/api/whats-new/${encodeURIComponent(id)}/open`,
} as const;

/** A changelog entry id: what a `/whats-new/<id>` link may carry. Mirrors
 *  `is_entry_id` in `rubik_core/whats_new/changelog.py`. */
export const ENTRY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

export const isEntryId = (value: unknown): value is string => typeof value === 'string' && ENTRY_ID_PATTERN.test(value);

// ─── calls ───────────────────────────────────────────────────────────────────

/** The feed. `itemId` (the note a Slack link opened the drawer on) asks the
 *  server to add that note when it is older than the newest 60 but this
 *  person may still see it. */
export async function fetchWhatsNew(itemId?: string | null): Promise<WhatsNewFeed> {
    const url = itemId ? `${WHATS_NEW_PATHS.feed}?item=${encodeURIComponent(itemId)}` : WHATS_NEW_PATHS.feed;
    const res = await client.get<WhatsNewFeed>(url);
    return res.data;
}

/** Count one open. The server stamps the person and department and dedups,
 *  so calling it twice (React StrictMode in dev) still counts once. */
export async function recordOpen(id: string, source: ClickSource): Promise<void> {
    await client.post(WHATS_NEW_PATHS.open(id), { source });
}

// ─── errors ──────────────────────────────────────────────────────────────────

/** The kill switch's 404 (`WHATS_NEW_ENABLED` off on this server). */
export const NOT_SWITCHED_ON = "What's New is not switched on.";
/** A note 404: never published, withdrawn, or for another team. */
export const NO_LONGER_AVAILABLE = 'This update is no longer available.';

const statusOf = (e: unknown): number | null => {
    const status = (e as { response?: { status?: unknown } })?.response?.status;
    return typeof status === 'number' ? status : null;
};

/** Whether a failure means the whole feature is off here: the kill switch's
 *  404, or a server without the router at all (FastAPI's bare "Not Found").
 *  The shell hides What's New quietly on this; it is not an error. */
export const isWhatsNewOff = (e: unknown): boolean =>
    statusOf(e) === 404 && errorDetail(e) !== NO_LONGER_AVAILABLE;

/** The sentence to show for a failed call. A 404 is either the note ("This
 *  update is no longer available.") or the feature switched off; any other
 *  server sentence passes through; anything without one gets the fallback. */
export const whatsNewError = (e: unknown, fallback: string): string => {
    if (statusOf(e) === 404) return isWhatsNewOff(e) ? NOT_SWITCHED_ON : NO_LONGER_AVAILABLE;
    return errorDetail(e) ?? fallback;
};
