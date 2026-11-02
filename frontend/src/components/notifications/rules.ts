// The rules behind the notification bell and the "ready" toasts, kept pure so
// they run under Node's test runner.
//
// * Which notifications pop a toast: only the ones a person is WAITING for
//   (a meeting prep built in the background, or one that failed), only when
//   they are new since this tab first looked, and never on the page the
//   toast would send them to — that page catches up with them itself.
// * Which meeting-prep notifications the meeting's own page must catch up
//   with, and when.
// * Matching a notification's link to the route on screen, with URL-encoded
//   event ids (Google event ids can carry "_", "@" and friends, and the
//   server's `quote(…, safe="")` and the browser's `encodeURIComponent` do
//   not encode exactly the same characters).
// * Relative time for the bell's list.
//
// Import-free apart from `datetime.ts` (with its extension) so it runs under
// Node's test runner; the notification type is a type-only import.
import type { AppNotification } from '../../api/notifications';
import { instantMs } from '../../utils/datetime.ts';

export const MEETING_PREP_READY = 'meeting_prep_ready';
export const MEETING_PREP_FAILED = 'meeting_prep_failed';

/** Kinds a person is waiting on, so they pop a toast wherever they are. */
export const TOAST_KINDS: ReadonlySet<string> = new Set([MEETING_PREP_READY, MEETING_PREP_FAILED]);

export const isMeetingPrepKind = (kind: string | null | undefined): boolean =>
    kind === MEETING_PREP_READY || kind === MEETING_PREP_FAILED;

const safeDecode = (s: string): string => {
    try {
        return decodeURIComponent(s);
    } catch {
        return s;
    }
};

/** The in-app path a link opens, decoded, with no query, hash or trailing
 *  slash — or null for no link / another site. */
export function linkPath(link: string | null | undefined): string | null {
    if (!link) return null;
    let raw = link.trim();
    if (!raw) return null;
    if (/^[a-z][a-z0-9+.-]*:/i.test(raw) || raw.startsWith('//')) {
        // An absolute URL: only this app's own origin counts as a route.
        try {
            const origin = typeof window !== 'undefined' ? window.location.origin : null;
            const url = new URL(raw);
            if (!origin || url.origin !== origin) return null;
            raw = url.pathname;
        } catch {
            return null;
        }
    }
    const cut = raw.search(/[?#]/);
    let path = cut >= 0 ? raw.slice(0, cut) : raw;
    if (!path.startsWith('/')) path = `/${path}`;
    if (path.length > 1) path = path.replace(/\/+$/, '');
    return safeDecode(path) || '/';
}

/** Is `link` the page at `pathname`? Compares decoded paths, so an event id
 *  encoded by the server and the same id encoded by the browser match. */
export function isCurrentRoute(link: string | null | undefined, pathname: string | null | undefined): boolean {
    const a = linkPath(link);
    const b = linkPath(pathname);
    return !!a && !!b && a === b;
}

const PREP_PREFIX = '/meeting-prep/';

/** The meeting a `/meeting-prep/<event id>` link (or route) is about, decoded;
 *  null for anything else. */
export function meetingPrepEventId(link: string | null | undefined): string | null {
    const path = linkPath(link);
    if (!path || !path.startsWith(PREP_PREFIX)) return null;
    const id = path.slice(PREP_PREFIX.length);
    return id || null;
}

/**
 * Which unread notifications to toast now, and the updated "seen" set.
 *
 * `seen === null` is the first look for this person in this tab: everything
 * already waiting is remembered and NOTHING toasts — the bell lists it, and a
 * burst of old toasts on every page load would be noise. After that, a row
 * toasts once, the first time it appears, if it is a kind someone waits for
 * and its link is not the page already on screen.
 *
 * A row for the page on screen is NOT remembered: that page catches up with it
 * and marks it read (`prepNoticeStep`), which takes it out of `unread`. If the
 * reader leaves before that happens, it is still unread and still new to this
 * tab, so it toasts on the next page instead of being lost. Every other row is
 * remembered whether it toasted or not.
 *
 * Returned newest first, like the feed.
 */
export function pickToasts(
    unread: readonly AppNotification[],
    seen: ReadonlySet<string> | null,
    pathname: string | null | undefined,
): { toast: AppNotification[]; seen: Set<string> } {
    const next = new Set(seen ?? []);
    const toast: AppNotification[] = [];
    for (const n of unread) {
        if (next.has(n.id)) continue;
        if (seen === null || !TOAST_KINDS.has(n.kind)) {
            next.add(n.id);
            continue;
        }
        if (n.link && isCurrentRoute(n.link, pathname)) continue;
        next.add(n.id);
        toast.push(n);
    }
    return { toast, seen: next };
}

/** This meeting's meeting-prep notifications that are still unread. On the
 *  meeting's own page these are the ones the page did NOT acknowledge (an
 *  acknowledged row never reaches `unread`): a build it was not watching —
 *  another tab's, or one that settled while the reader was elsewhere. */
export function unackedPrepNotices(
    unread: readonly AppNotification[],
    eventId: string | null | undefined,
): AppNotification[] {
    if (!eventId) return [];
    return unread.filter(n => isMeetingPrepKind(n.kind) && meetingPrepEventId(n.link) === eventId);
}

/**
 * What the meeting-prep page does about its own unacknowledged notifications:
 *
 * * `none` — there are none.
 * * `wait` — the page is following a build of this meeting (its status poll
 *   settles it and acknowledges the notification), or is still opening (the
 *   answer acknowledges what it shows). Left unread meanwhile, so if the
 *   reader leaves first it still toasts on the next page.
 * * `refresh` — nobody here is going to: re-read the versions (and show or
 *   offer one newer than the one on screen), and mark them read.
 */
export type PrepNoticeStep = 'none' | 'wait' | 'refresh';

export function prepNoticeStep(
    notices: readonly unknown[],
    page: { following: boolean; opening: boolean },
): PrepNoticeStep {
    if (!notices.length) return 'none';
    return page.following || page.opening ? 'wait' : 'refresh';
}

/** "The page has shown this meeting's build result": which notifications that
 *  covers. A meeting-prep notification for this event created no later than
 *  `cutoffMs` (server time); `null` matches any. */
export interface PrepAck {
    eventId: string;
    cutoffMs: number | null;
}

export function ackMatches(ack: PrepAck, n: Pick<AppNotification, 'kind' | 'link' | 'created_at'>): boolean {
    if (!isMeetingPrepKind(n.kind)) return false;
    if (meetingPrepEventId(n.link) !== ack.eventId) return false;
    if (ack.cutoffMs === null) return true;
    const at = instantMs(n.created_at);
    return at !== null && at <= ack.cutoffMs;
}

/** The unread badge: nothing at zero, "99+" past 99. */
export function badgeLabel(count: number): string | null {
    if (!count || count < 0) return null;
    return count > 99 ? '99+' : String(count);
}

/** "just now", "5 min ago", "3 h ago", "yesterday", "4 days ago", then a date. */
export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string {
    const at = instantMs(iso);
    if (at === null) return '';
    const sec = Math.max(0, Math.round((now - at) / 1000));
    if (sec < 45) return 'just now';
    const min = Math.round(sec / 60);
    if (min < 60) return `${min} min ago`;
    const hours = Math.round(min / 60);
    if (hours < 24) return `${hours} h ago`;
    const days = Math.floor(hours / 24);
    if (days < 2) return 'yesterday';
    if (days < 7) return `${days} days ago`;
    try {
        return new Date(at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    } catch {
        return new Date(at).toLocaleDateString();
    }
}
