// The rules behind the What's New drawer, kept pure so they run under Node's
// test runner (`logic.test.ts`).
//
// * Which updates count as new: published after the person last looked, and
//   never older than two weeks (someone back from a month away sees a short
//   list, not everything since they left).
// * When the drawer may slide open by itself: once per ISO week, only with
//   something new, never on the live L10, never over a drawer or dialog
//   already open, never in a tab nobody is looking at, never while an admin is
//   viewing as someone else, never on a narrow window.
// * The button's accessible name.
//
// Which notes a person may see (the feature they are about) is decided on the
// server, so nothing here filters by feature.
//
// Import-free apart from `datetime.ts` (with its extension) so bare Node can
// load it; the API types are a type-only import.
import type { WhatsNewItem } from '../../api/whatsNew';
import { instantMs } from '../../utils/datetime.ts';

/** Nothing older than this counts as new, whatever the person last saw. */
export const UNSEEN_FLOOR_DAYS = 14;
/** The tease needs room: below this width the drawer would cover the page. */
export const TEASE_MIN_VIEWPORT = 768;

const DAY_MS = 24 * 60 * 60 * 1000;

/** The live L10 session, where the shell hides the nav (Layout's
 *  `isNavHidden`). The Exec Team runs its L10 here every Tuesday morning:
 *  nothing may slide over it. */
export const isLiveL10Path = (pathname: string): boolean => /^\/eos\/meetings\/[^/]+$/.test(pathname);

/**
 * The local-time ISO 8601 week a moment falls in, as `2026-W42`.
 *
 * Weeks start on Monday; week 1 is the one holding the year's first Thursday,
 * so the last days of December can belong to next year's week 1 and the first
 * days of January to last year's week 52 or 53. Local time on purpose: "the
 * first time you open Cubie this week" is the reader's own Monday.
 */
export function isoWeekKey(date: Date): string {
    // The Thursday of this week decides which year the week belongs to.
    const thursday = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    thursday.setDate(thursday.getDate() - ((thursday.getDay() + 6) % 7) + 3);
    const year = thursday.getFullYear();
    // Week 1 holds January 4th; find its Thursday the same way.
    const firstThursday = new Date(year, 0, 4);
    firstThursday.setDate(firstThursday.getDate() - ((firstThursday.getDay() + 6) % 7) + 3);
    // Rounded: a daylight-saving change makes a "week" 167 or 169 hours.
    const week = 1 + Math.round((thursday.getTime() - firstThursday.getTime()) / (7 * DAY_MS));
    return `${year}-W${String(week).padStart(2, '0')}`;
}

const nowMs = (now: Date | number): number => (typeof now === 'number' ? now : now.getTime());

/**
 * The updates the person has not seen: published after `seenThrough` (the
 * newest `published_at` they had when they last closed the drawer) and within
 * the last two weeks. `published_at` and `seenThrough` are the server's naive
 * UTC instants. An item whose time cannot be read is never "new". Order kept.
 */
export function unseenItems<T extends Pick<WhatsNewItem, 'published_at'>>(
    items: readonly T[],
    seenThrough: string | null,
    now: Date | number,
): T[] {
    const floor = nowMs(now) - UNSEEN_FLOOR_DAYS * DAY_MS;
    const seen = instantMs(seenThrough);
    const after = seen === null ? floor : Math.max(seen, floor);
    return items.filter(item => {
        const at = instantMs(item.published_at);
        return at !== null && at > after;
    });
}

/** The newest `published_at` among the items, as the server sent it (the
 *  value `seenThrough` and `barDismissedThrough` store); null when none. */
export function newestPublishedAt(items: readonly Pick<WhatsNewItem, 'published_at'>[]): string | null {
    let newest: string | null = null;
    let newestMs = -Infinity;
    for (const item of items) {
        const at = instantMs(item.published_at);
        if (at !== null && at > newestMs) {
            newest = item.published_at;
            newestMs = at;
        }
    }
    return newest;
}

export interface TeaseInput {
    /** Admin switch (`tease_enabled` from the feed). */
    teaseEnabled: boolean;
    /** `unseenItems(...).length`. */
    unseenCount: number;
    /** The ISO week of the last tease, from the per-person memory. */
    teasedWeek: string | null;
    now: Date;
    pathname: string;
    /** The drawer was already shown during this page load (store.ts). */
    openedThisLoad: boolean;
    drawerOpen: boolean;
    /** An admin viewing Cubie as someone else. */
    impersonating: boolean;
    viewportWidth: number;
    /** The browser tab is in the background: a tease nobody sees would use
     *  up the week. */
    pageHidden: boolean;
    /** A modal dialog (Modal, a confirm) is open: the peek would slide in
     *  under its backdrop. */
    dialogOpen: boolean;
}

/**
 * Whether the drawer may slide open by itself now. The caller decides WHEN to
 * ask (once the first feed has loaded and the shell has settled), writes
 * `teasedWeek` before opening, and passes the drawer state as it is at that
 * moment, not as it was at mount.
 */
export function shouldTease(input: TeaseInput): boolean {
    return (
        input.teaseEnabled &&
        input.unseenCount > 0 &&
        input.teasedWeek !== isoWeekKey(input.now) &&
        !isLiveL10Path(input.pathname) &&
        // A Slack or review link opens the drawer itself.
        !input.pathname.startsWith('/whats-new/') &&
        !input.openedThisLoad &&
        !input.drawerOpen &&
        !input.impersonating &&
        input.viewportWidth >= TEASE_MIN_VIEWPORT &&
        !input.pageHidden &&
        !input.dialogOpen
    );
}

const plural = (n: number, one: string, many: string): string => `${n} ${n === 1 ? one : many}`;

/** The live-region sentence when the drawer slides open by itself. */
export const teaseAnnouncement = (unseenCount: number): string =>
    `What's new: ${plural(unseenCount, 'update', 'updates')} since your last visit`;

/** The footer button's accessible name and tooltip: "What's new, 3 new". */
export function whatsNewButtonLabel(unseenCount: number): string {
    return unseenCount > 0 ? `What's new, ${unseenCount} new` : "What's new";
}
