/* eslint-disable react-refresh/only-export-components -- the `useWhatsNew` hook
   is intentionally colocated with its provider, like Toast and AuthContext. */
// What's New for the whole shell: one feed per page load, what this person has
// seen, and the weekly tease.
//
// Mounted in App.tsx around <Layout/> (inside ProtectedRoute and
// NotificationsProvider), so the footer button, the drawer, the Daily
// Briefing's spotlight bar and the deep-link routes all read the same feed.
// Company-wide on purpose: it does NOT ride on NotificationsContext, whose API
// sits behind the copilot. `features.whats_new` from /api/users/me already folds in the
// WHATS_NEW_ENABLED kill switch, so nothing is fetched where it is off.
//
// The feed is fetched once per page load and again whenever the full drawer
// opens. "Seen" lives in memory.ts (per person, per browser, until the walk-me
// store replaces it); the drawer's open state lives in store.ts.
//
// The weekly tease: once the first feed has arrived and the shell has had
// ~1.5 s to settle, `shouldTease` decides (once per load) whether the drawer
// may slide open by itself. A tab in the background waits until it is looked
// at (the week is not spent on a peek nobody sees); an open dialog is a "no"
// for this load. The week is recorded BEFORE it opens, so a reload mid-tease
// never teases twice. The live region below announces it, because the peek
// never takes focus.
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useLocation } from 'react-router-dom';
import { useAuth } from '../../context/AuthContext';
import { impersonationHeaders } from '../../api/client';
import { fetchWhatsNew, isWhatsNewOff } from '../../api/whatsNew';
import type { WhatsNewFeed, WhatsNewItem } from '../../api/whatsNew';
import { instantMs } from '../../utils/datetime';
import { openDialogCount } from '../eos/dialogStack';
import {
    isLiveL10Path,
    isoWeekKey,
    newestPublishedAt,
    shouldTease,
    teaseAnnouncement,
    unseenItems,
} from './logic';
import { memoryStore } from './memory';
import { closeWhatsNew, getWhatsNewDrawerState, teaseWhatsNew, useWhatsNewDrawer } from './store';

/** How long the shell gets to settle before the tease may start. */
const TEASE_SETTLE_MS = 1500;

export interface WhatsNewContextValue {
    /** /api/users/me gives this person What's New (and it is switched on). */
    enabled: boolean;
    /** The profile has loaded, so `enabled` is a verdict rather than a default. */
    profileLoaded: boolean;
    /** The last feed that loaded; null until the first one does. */
    feed: WhatsNewFeed | null;
    /** The note the last feed was asked to include (a Slack link's), or
     *  null. A linked note missing from a feed asked for it is for another
     *  team, or gone. */
    feedItemId: string | null;
    /** Why the last load failed; null while loading or after a success. */
    error: unknown;
    /** The server says What's New is switched off here, or closed to this
     *  person: every surface hides itself quietly. */
    off: boolean;
    items: WhatsNewItem[];
    /** Published since the person last looked (and within two weeks). */
    unseen: WhatsNewItem[];
    unseenIds: ReadonlySet<string>;
    /** The unseen updates the spotlight bar has not been dismissed past. */
    barItems: WhatsNewItem[];
    /** The footer and header buttons show: What's New is on for this person
     *  and the feed has loaded (an empty drawer says "No updates yet."). */
    showButton: boolean;
    /** Load the feed again (a retry after an error). */
    reload: () => void;
    /** Everything in the feed counts as seen from now on. */
    markAllSeen: () => void;
    /** Hide the spotlight bar until something newer is published. */
    dismissBar: () => void;
    /** The footer button: where focus goes back to when the drawer closes and
     *  the element that opened it is gone. */
    buttonRef: React.RefObject<HTMLButtonElement | null>;
}

const EMPTY_ITEMS: WhatsNewItem[] = [];
const NO_IDS: ReadonlySet<string> = new Set<string>();
const noop = () => {};

const WhatsNewContext = createContext<WhatsNewContextValue>({
    enabled: false,
    profileLoaded: false,
    feed: null,
    feedItemId: null,
    error: null,
    off: true,
    items: EMPTY_ITEMS,
    unseen: EMPTY_ITEMS,
    unseenIds: NO_IDS,
    barItems: EMPTY_ITEMS,
    showButton: false,
    reload: noop,
    markAllSeen: noop,
    dismissBar: noop,
    buttonRef: { current: null },
});

const statusOf = (e: unknown): number | null => {
    const status = (e as { response?: { status?: unknown } })?.response?.status;
    return typeof status === 'number' ? status : null;
};

/** An admin viewing Cubie as someone else (a person or a department). The
 *  tease never runs for them: it would record a week on the wrong person. */
const isImpersonating = (profileSaysSo: boolean): boolean => {
    if (profileSaysSo) return true;
    try {
        return Object.keys(impersonationHeaders()).length > 0;
    } catch {
        return false;
    }
};

const isAfter = (a: string | null, b: string | null): boolean =>
    (instantMs(a) ?? -Infinity) > (instantMs(b) ?? -Infinity);

interface LoadState {
    email: string | null;
    feed: WhatsNewFeed | null;
    /** The linked note this feed was asked to include. */
    itemId: string | null;
    error: unknown;
}

export const WhatsNewProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const { user, hasFeature } = useAuth();
    const email = user?.email ?? null;
    const enabled = Boolean(user) && hasFeature('whats_new');
    const impersonatedByProfile = Boolean(user?.impersonation);
    const { pathname } = useLocation();
    const drawer = useWhatsNewDrawer();
    const isFull = drawer.open && drawer.mode === 'full';

    // "now" for the two-week floor, fixed at load (the house way).
    const [now] = useState(() => new Date());
    const [load, setLoad] = useState<LoadState>({ email: null, feed: null, itemId: null, error: null });
    const [reloadTick, setReloadTick] = useState(0);
    const [announcement, setAnnouncement] = useState('');
    const buttonRef = useRef<HTMLButtonElement | null>(null);
    /** `email|reloadTick` of the last load started, so closing the drawer does
     *  not fetch again. */
    const requestedRef = useRef<string | null>(null);
    const feedRef = useRef<WhatsNewFeed | null>(null);
    const teaseDecidedRef = useRef(false);
    /** The note the open drawer is on, read when a fetch starts (a ref, so a
     *  tab switch that lets the note go does not fetch again). */
    const drawerItemRef = useRef<string | null>(null);

    // The feed belongs to whoever is signed in now.
    const feed = load.email === email ? load.feed : null;
    const feedItemId = load.email === email ? load.itemId : null;
    const error = load.email === email ? load.error : null;
    const off = !enabled || (error !== null && (isWhatsNewOff(error) || statusOf(error) === 403));

    useEffect(() => {
        feedRef.current = feed;
    }, [feed]);
    // Declared before the fetch below, so a deep link's note is in the ref
    // when the drawer's opening starts that fetch.
    useEffect(() => {
        drawerItemRef.current = drawer.open ? drawer.itemId : null;
    }, [drawer.open, drawer.itemId]);

    // Once per page load (per person), again whenever the full drawer opens,
    // and on a retry. Every setState is in the async callback behind the
    // cancelled flag (react-hooks/set-state-in-effect).
    useEffect(() => {
        if (!enabled || !email) return;
        const key = `${email}|${reloadTick}`;
        if (!isFull && requestedRef.current === key) return;
        requestedRef.current = key;
        const itemId = isFull ? drawerItemRef.current : null;
        let cancelled = false;
        let settled = false;
        fetchWhatsNew(itemId)
            .then(next => {
                settled = true;
                if (!cancelled) setLoad({ email, feed: next, itemId, error: null });
            })
            .catch(e => {
                settled = true;
                if (!cancelled) {
                    setLoad(prev => ({
                        email,
                        feed: prev.email === email ? prev.feed : null,
                        itemId: prev.email === email ? prev.itemId : null,
                        error: e,
                    }));
                }
            });
        return () => {
            cancelled = true;
            // A load thrown away before it finished does not count as done:
            // the next run (the drawer closed mid-load) must fetch again, or
            // the feed would stay empty for the rest of the page load.
            if (!settled && requestedRef.current === key) requestedRef.current = null;
        };
    }, [enabled, email, isFull, reloadTick]);

    const memory = useSyncExternalStore(memoryStore.subscribe, () => memoryStore.getSnapshot(email));

    const items = feed?.items ?? EMPTY_ITEMS;
    const unseen = useMemo(() => unseenItems(items, memory.seenThrough, now), [items, memory.seenThrough, now]);
    const unseenIds = useMemo(() => new Set(unseen.map(i => i.id)), [unseen]);
    const barItems = useMemo(
        () => unseenItems(unseen, memory.barDismissedThrough, now),
        [unseen, memory.barDismissedThrough, now],
    );

    // The weekly tease, decided once per load: after the first feed AND the
    // settle delay. Everything is read at decision time (the drawer may have
    // been opened meanwhile, the route may have changed). A hidden tab does
    // not decide: it waits until the tab is shown, then settles again.
    useEffect(() => {
        if (!feed || !email || teaseDecidedRef.current) return;
        let timer: number | undefined;
        const decide = () => {
            if (teaseDecidedRef.current) return;
            if (document.visibilityState === 'hidden') return; // wait for visibilitychange
            teaseDecidedRef.current = true;
            const at = new Date();
            const remembered = memoryStore.load(email);
            const drawerNow = getWhatsNewDrawerState();
            const count = unseenItems(feed.items, remembered.seenThrough, at).length;
            const ok = shouldTease({
                teaseEnabled: feed.tease_enabled,
                unseenCount: count,
                teasedWeek: remembered.teasedWeek,
                now: at,
                pathname: window.location.pathname,
                openedThisLoad: drawerNow.openedThisLoad,
                drawerOpen: drawerNow.open,
                impersonating: isImpersonating(impersonatedByProfile),
                viewportWidth: window.innerWidth,
                pageHidden: false,
                // A page tour on screen counts as an open dialog: its cards and the
                // peek would otherwise talk over each other on a first visit.
                dialogOpen: openDialogCount() > 0 || document.querySelector('[data-tour-overlay]') !== null,
            });
            if (!ok) return;
            // Recorded first: a reload during the peek must not tease again.
            memoryStore.save(email, { teasedWeek: isoWeekKey(at) });
            if (teaseWhatsNew()) setAnnouncement(teaseAnnouncement(count));
        };
        const onVisibility = () => {
            if (document.visibilityState !== 'visible' || teaseDecidedRef.current) return;
            window.clearTimeout(timer);
            timer = window.setTimeout(decide, TEASE_SETTLE_MS); // settle again once the tab is seen
        };
        timer = window.setTimeout(decide, Math.max(0, TEASE_SETTLE_MS - (Date.now() - now.getTime())));
        document.addEventListener('visibilitychange', onVisibility);
        return () => {
            window.clearTimeout(timer);
            document.removeEventListener('visibilitychange', onVisibility);
        };
    }, [feed, email, now, impersonatedByProfile]);

    // Nothing slides over a live L10: a peek still showing when the meeting
    // opens closes at once. (A drawer the person opened stays theirs.)
    useEffect(() => {
        const s = getWhatsNewDrawerState();
        if (isLiveL10Path(pathname) && s.open && s.mode === 'peek') closeWhatsNew();
    }, [pathname]);

    const reload = useCallback(() => setReloadTick(t => t + 1), []);

    const markAllSeen = useCallback(() => {
        const newest = newestPublishedAt(feedRef.current?.items ?? EMPTY_ITEMS);
        if (!email || !newest) return;
        if (!isAfter(newest, memoryStore.load(email).seenThrough)) return;
        memoryStore.save(email, { seenThrough: newest });
    }, [email]);

    const dismissBar = useCallback(() => {
        const newest = newestPublishedAt(feedRef.current?.items ?? EMPTY_ITEMS);
        if (!email || !newest) return;
        memoryStore.save(email, { barDismissedThrough: newest });
    }, [email]);

    const showButton = !off && feed !== null;

    const value = useMemo<WhatsNewContextValue>(
        () => ({
            enabled,
            profileLoaded: Boolean(user),
            feed,
            feedItemId,
            error,
            off,
            items,
            unseen,
            unseenIds,
            barItems,
            showButton,
            reload,
            markAllSeen,
            dismissBar,
            buttonRef,
        }),
        [
            enabled,
            user,
            feed,
            feedItemId,
            error,
            off,
            items,
            unseen,
            unseenIds,
            barItems,
            showButton,
            reload,
            markAllSeen,
            dismissBar,
        ],
    );

    return (
        <WhatsNewContext.Provider value={value}>
            {children}
            {/* Always mounted, so the tease is announced: the peek never takes
                focus, and a screen reader would otherwise miss it. */}
            <div role="status" aria-live="polite" className="sr-only">
                {announcement}
            </div>
        </WhatsNewContext.Provider>
    );
};

/** The shell's What's New state. Outside the provider it reads as switched
 *  off, so a surface rendered elsewhere simply hides. */
export const useWhatsNew = (): WhatsNewContextValue => useContext(WhatsNewContext);
