/* eslint-disable react-refresh/only-export-components -- `useNotifications`
   is colocated with its provider on purpose. */
/**
 * Unread in-app notifications, polled once for the whole app.
 *
 * Mounted above Layout so the left nav, the notification bell, the "ready"
 * toasts and the Chat sidebar all read the SAME poll — several consumers with
 * several timers would multiply the request rate and let the bell and the
 * dots disagree about what is unread.
 *
 * Cadence: one timer, every 60s, plus a poll on returning to the tab. When a
 * page has just started something the person is waiting on (a meeting prep
 * built in the background), it calls `expectSoon()` and the same timer runs
 * every 8s until the expected notification arrives or the window ends, then
 * drops back to 60s — so "your meeting prep is ready" lands within seconds
 * without every tab polling fast all day.
 *
 * Read state is optimistic: a row marked read here disappears at once, and a
 * poll that was already in flight cannot bring it back (`seenIds`). If the
 * server refuses the read, the suppression is lifted so the next poll shows
 * the row again rather than hiding something still unread server-side.
 * Reads are always BY ID — "Mark all as read" too — so a row that landed after
 * the last poll (a meeting prep someone is waiting for) is never marked read
 * unseen.
 *
 * Only people with the full app poll: the notifications API is mounted behind
 * `require_full_app_access` (backend/main.py), so for anyone else every poll
 * would be a 403 and the feed is simply empty.
 */
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { fetchNotifications, markNotificationRead, sessionIdFromLink } from '../api/notifications';
import type { AppNotification } from '../api/notifications';
import { ackMatches, type PrepAck } from '../components/notifications/rules';
import { instantMs } from '../utils/datetime';
import { useAuth } from './AuthContext';

/** Backend transport is polling-only, so this is the feed's base cadence. */
const POLL_MS = 60_000;
/** While a page is expecting a notification. */
const FAST_POLL_MS = 8_000;
/** How long `expectSoon` keeps the fast cadence by default. */
const EXPECT_WINDOW_MS = 5 * 60_000;
/** A build's notification is written the moment the build settles (same
 *  worker, seconds apart, both on the server's clock); an acknowledgement
 *  covers notifications up to this long after it. Kept short on purpose: the
 *  NEXT build of the same meeting (a Regenerate clicked right after) takes far
 *  longer than this, so its notification is never swallowed. */
const ACK_SLACK_MS = 20_000;
/** How long an acknowledgement keeps catching a notification that is still
 *  on its way. */
const ACK_TTL_MS = 3 * 60_000;
const SCHEDULED_RESULT = 'scheduled_task_result';
const EMPTY: AppNotification[] = [];

interface Expectation {
    until: number;
    /** Ids already in the feed when it started — those never count as "arrived". */
    known: Set<string>;
    /** Rows written before this (epoch ms) never count either: an old unread
     *  row the first poll had not fetched yet is not the one awaited. */
    since: number;
    match: (n: AppNotification) => boolean;
}

/** How far before `expectSoon` a row may be dated and still count as the
 *  awaited arrival — browser and server clocks are never quite in step. */
const EXPECT_CLOCK_SLACK_MS = 2 * 60_000;

const arrived = (x: Expectation, r: AppNotification): boolean =>
    !x.known.has(r.id) && (instantMs(r.created_at) ?? Infinity) >= x.since && x.match(r);

interface ActiveAck extends PrepAck {
    expires: number;
}

interface NotificationsValue {
    /** Every unread row, newest first. */
    unread: AppNotification[];
    /** The first poll for this sign-in has landed, so a row that appears from
     *  now on is NEW (the toaster needs to tell that from "already waiting"). */
    ready: boolean;
    /** Chat session id → the unread notification ids pointing at it. */
    unreadBySession: Record<string, string[]>;
    /** Unread scheduled-task results that opened a conversation. */
    chatUnreadCount: number;
    /**
     * Unread scheduled-task notices with no conversation to open — today
     * that is the "task paused itself" warning, which lives in Settings.
     */
    settingsUnreadCount: number;
    /** Mark every unread notification for one conversation read. */
    markSessionSeen: (sessionId: string) => Promise<void>;
    /** Mark one notification read. */
    markRead: (id: string) => Promise<void>;
    /** Mark these notifications read — the rows the bell showed. By id on
     *  purpose: one that arrived after the last poll stays unread. */
    markAllRead: (ids: readonly string[]) => Promise<void>;
    /**
     * Something the person is waiting on was just started: poll every 8s for
     * `windowMs` (default 5 min), or until a NEW unread notification that
     * `match`es (default: any) arrives. Returns a function that ends this wait
     * early — for a page that saw the result itself. Stable across renders.
     */
    expectSoon: (windowMs?: number, match?: (n: AppNotification) => boolean) => () => void;
    /**
     * The meeting-prep page has shown this meeting's build result: mark its
     * meeting-prep notifications read — the ones already here, and for a few
     * minutes the one still on its way — up to `upTo` (the server's
     * `finished_at` / `created_at`) plus a little slack. Stable across renders.
     */
    acknowledgeMeetingPrep: (eventId: string, upTo?: string | null) => void;
    refresh: () => Promise<void>;
}

const NotificationsContext = createContext<NotificationsValue>({
    unread: [],
    ready: false,
    unreadBySession: {},
    chatUnreadCount: 0,
    settingsUnreadCount: 0,
    markSessionSeen: async () => {},
    markRead: async () => {},
    markAllRead: async () => {},
    expectSoon: () => () => {},
    acknowledgeMeetingPrep: () => {},
    refresh: async () => {},
});

export const NotificationsProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const auth = useAuth();
    // The sign-in this feed polls for: none without the full app (the API
    // would answer 403), nor while the profile that says so is loading.
    const token = auth.canAccessFullApp ? auth.token : null;
    /** The feed, stamped with the sign-in it belongs to — signing out (or in
     *  as someone else) shows nothing until that person's first poll lands,
     *  without an effect having to clear it. */
    const [feed, setFeed] = useState<{ forToken: string; rows: AppNotification[] } | null>(null);
    // Guards the optimistic clears: an in-flight poll that started BEFORE the
    // read landed would otherwise restore the row.
    const seenIds = useRef<Set<string>>(new Set());
    /** The raw rows of the last poll (before local suppression). */
    const lastRows = useRef<AppNotification[]>([]);
    const expectations = useRef<Expectation[]>([]);
    const acks = useRef<ActiveAck[]>([]);
    /** Only the newest poll may write the feed. */
    const loadSeq = useRef(0);
    /** Only the newest scheduling decision may arm the timer. */
    const runSeq = useRef(0);
    const timer = useRef<number | undefined>(undefined);
    /** Re-arms the one timer on a new delay (set by the polling effect). */
    const rearm = useRef<(delay: number) => void>(() => {});

    /** Ids to mark read on the server, suppressed locally until it answers. */
    const markIds = useCallback(async (ids: string[]) => {
        await Promise.all(
            ids.map(async id => {
                try {
                    await markNotificationRead(id);
                } catch {
                    // Lift the local suppression so the next poll can show the
                    // row again. Keeping it would hide a notification that is
                    // still unread server-side until the tab is reloaded.
                    seenIds.current.delete(id);
                }
            }),
        );
    }, []);

    const load = useCallback(async () => {
        if (!token) return;
        const forToken = token;
        const seq = ++loadSeq.current;
        try {
            const rows = await fetchNotifications({ unread: true, limit: 100 });
            if (seq !== loadSeq.current) return;
            const now = Date.now();
            lastRows.current = rows;

            // A page already showed what these say: mark them read.
            acks.current = acks.current.filter(a => a.expires > now);
            const acked = rows.filter(
                r => !seenIds.current.has(r.id) && acks.current.some(a => ackMatches(a, r)),
            );
            acked.forEach(r => seenIds.current.add(r.id));
            if (acked.length) void markIds(acked.map(r => r.id));

            const visible = rows.filter(r => !seenIds.current.has(r.id));

            // An expected notification arrived (or the wait ran out). Only an
            // UNREAD arrival counts: one a page already acknowledged belongs
            // to a build it saw finish — often the previous build of the very
            // meeting a new wait is for — and must not end that new wait.
            expectations.current = expectations.current.filter(
                x => x.until > now && !visible.some(r => arrived(x, r)),
            );

            setFeed({ forToken, rows: visible });
        } catch {
            // A courtesy channel, never a contract — a failed poll leaves the
            // last known state alone rather than blanking the indicators.
        }
    }, [token, markIds]);

    useEffect(() => {
        if (!token) return;
        let alive = true;
        const delay = () => {
            const now = Date.now();
            expectations.current = expectations.current.filter(x => x.until > now);
            return expectations.current.length ? FAST_POLL_MS : POLL_MS;
        };
        const arm = (ms: number) => {
            window.clearTimeout(timer.current);
            timer.current = window.setTimeout(() => void run(), ms);
        };
        // ONE timer: each run re-arms it when its poll lands, unless a newer
        // run (focus, a re-arm) has taken over in the meantime.
        const run = async () => {
            const mine = ++runSeq.current;
            window.clearTimeout(timer.current);
            await load();
            if (!alive || mine !== runSeq.current) return;
            arm(delay());
        };
        rearm.current = (ms: number) => {
            if (!alive) return;
            ++runSeq.current;
            arm(ms);
        };
        void run();
        // Coming back to the tab is the moment a result is most likely to be
        // waiting, and it costs one request.
        const onFocus = () => {
            void run();
        };
        window.addEventListener('focus', onFocus);
        return () => {
            alive = false;
            window.clearTimeout(timer.current);
            rearm.current = () => {};
            window.removeEventListener('focus', onFocus);
        };
    }, [token, load]);

    const expectSoon = useCallback((windowMs: number = EXPECT_WINDOW_MS, match?: (n: AppNotification) => boolean) => {
        const now = Date.now();
        const wasFast = expectations.current.some(x => x.until > now);
        const mine: Expectation = {
            until: now + Math.max(0, windowMs),
            known: new Set(lastRows.current.map(r => r.id)),
            since: now - EXPECT_CLOCK_SLACK_MS,
            match: match ?? (() => true),
        };
        expectations.current.push(mine);
        if (!wasFast) rearm.current(FAST_POLL_MS);
        // Ending a wait needs no re-arm: the next poll picks the slow cadence
        // once nothing is expected.
        return () => {
            expectations.current = expectations.current.filter(x => x !== mine);
        };
    }, []);

    const acknowledgeMeetingPrep = useCallback(
        (eventId: string, upTo?: string | null) => {
            if (!eventId) return;
            const now = Date.now();
            const ack: ActiveAck = {
                eventId,
                cutoffMs: (instantMs(upTo) ?? now) + ACK_SLACK_MS,
                expires: now + ACK_TTL_MS,
            };
            acks.current.push(ack);
            const hit = lastRows.current.filter(r => !seenIds.current.has(r.id) && ackMatches(ack, r));
            if (!hit.length) return;
            const ids = hit.map(r => r.id);
            ids.forEach(id => seenIds.current.add(id));
            setFeed(f => (f ? { ...f, rows: f.rows.filter(r => !ids.includes(r.id)) } : f));
            void markIds(ids);
        },
        [markIds],
    );

    const unread = feed && token && feed.forToken === token ? feed.rows : EMPTY;
    const ready = !!feed && !!token && feed.forToken === token;

    const markRead = useCallback(
        async (id: string) => {
            if (!id) return;
            seenIds.current.add(id);
            setFeed(f => (f ? { ...f, rows: f.rows.filter(r => r.id !== id) } : f));
            await markIds([id]);
        },
        [markIds],
    );

    const markAllRead = useCallback(
        async (shown: readonly string[]) => {
            const ids = [...new Set(shown)].filter(id => id && !seenIds.current.has(id));
            if (!ids.length) return;
            ids.forEach(id => seenIds.current.add(id));
            setFeed(f => (f ? { ...f, rows: f.rows.filter(r => !ids.includes(r.id)) } : f));
            await markIds(ids);
        },
        [markIds],
    );

    const unreadBySession = useMemo(() => {
        const map: Record<string, string[]> = {};
        for (const n of unread) {
            const sid = sessionIdFromLink(n.link);
            if (!sid) continue;
            (map[sid] ||= []).push(n.id);
        }
        return map;
    }, [unread]);

    const scheduled = useMemo(() => unread.filter(n => n.kind === SCHEDULED_RESULT), [unread]);

    const chatUnreadCount = useMemo(
        () => scheduled.filter(n => sessionIdFromLink(n.link)).length,
        [scheduled],
    );
    const settingsUnreadCount = useMemo(
        () => scheduled.filter(n => !sessionIdFromLink(n.link)).length,
        [scheduled],
    );

    const markSessionSeen = useCallback(async (sessionId: string) => {
        const ids = unreadBySession[sessionId];
        if (!ids?.length) return;
        ids.forEach(id => seenIds.current.add(id));
        setFeed(f => (f ? { ...f, rows: f.rows.filter(n => !ids.includes(n.id)) } : f));
        await markIds(ids);
    }, [unreadBySession, markIds]);

    const value = useMemo(
        () => ({
            unread,
            ready,
            unreadBySession,
            chatUnreadCount,
            settingsUnreadCount,
            markSessionSeen,
            markRead,
            markAllRead,
            expectSoon,
            acknowledgeMeetingPrep,
            refresh: load,
        }),
        [
            unread,
            ready,
            unreadBySession,
            chatUnreadCount,
            settingsUnreadCount,
            markSessionSeen,
            markRead,
            markAllRead,
            expectSoon,
            acknowledgeMeetingPrep,
            load,
        ],
    );

    return <NotificationsContext.Provider value={value}>{children}</NotificationsContext.Provider>;
};

export const useNotifications = (): NotificationsValue => useContext(NotificationsContext);
