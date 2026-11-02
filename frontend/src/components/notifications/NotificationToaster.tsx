// "Your meeting prep is ready" — wherever the person is in Cubie.
//
// A meeting prep builds in the background so nobody has to sit on the page
// while it does. When it lands (or fails), the server writes a notification;
// this shows it as a card in the top-right corner of whatever page is open,
// with a button straight to the briefing. Mounted ONCE, in Layout.
//
// Which rows toast lives in `rules.ts` (`pickToasts`): only kinds a person is
// waiting for, only rows that are new since this tab first looked (so the
// unread backlog never bursts on a page load), and never on the page the card
// would open — that page catches up with it and marks it read itself (if the
// reader leaves first, it still toasts on the next page). Dismissing a card
// leaves the notification unread: it is still in the bell until it is opened.
//
// The cards live in a tiny module store (`cardStore.ts`): the feed changes in
// an effect, and the store is what that effect syncs into. The store belongs
// to the person signed in — a sign-out or a different person empties it.
import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { AlertTriangle, ArrowRight, CheckCircle2, X } from 'lucide-react';

import type { AppNotification } from '../../api/notifications';
import { useAuth } from '../../context/AuthContext';
import { useNotifications } from '../../context/NotificationsContext';
import { createCardStore } from './cardStore';
import { MEETING_PREP_FAILED, pickToasts } from './rules';

/** How long a card stays when nobody is pointing at it. */
const AUTO_HIDE_MS = 12_000;
/** The most cards on screen at once; older ones give way. */
const MAX_CARDS = 3;

const cards = createCardStore<AppNotification>(MAX_CARDS);
const dismissCard = cards.dismiss;

const ToastCard: React.FC<{ n: AppNotification; onOpen: (n: AppNotification) => void }> = ({ n, onOpen }) => {
    // Hover or keyboard focus holds the card; leaving starts the clock again.
    const [held, setHeld] = useState(false);
    useEffect(() => {
        if (held) return;
        const t = window.setTimeout(() => dismissCard(n.id), AUTO_HIDE_MS);
        return () => window.clearTimeout(t);
    }, [held, n.id]);

    const failed = n.kind === MEETING_PREP_FAILED || n.severity !== 'info';
    const Icon = failed ? AlertTriangle : CheckCircle2;

    return (
        <div
            onMouseEnter={() => setHeld(true)}
            onMouseLeave={() => setHeld(false)}
            onFocus={() => setHeld(true)}
            onBlur={e => {
                if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setHeld(false);
            }}
            className="pointer-events-auto relative w-[22rem] max-w-[calc(100vw-2rem)] overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl shadow-slate-900/10"
        >
            <div
                className={`absolute inset-y-0 left-0 w-1 ${failed ? 'bg-amber-500' : 'bg-brand'}`}
                aria-hidden="true"
            />
            <div className="flex items-start gap-3 py-3.5 pl-5 pr-3">
                <div
                    className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full ${
                        failed ? 'bg-amber-50 text-amber-600' : 'bg-brand/10 text-brand'
                    }`}
                >
                    <Icon size={16} aria-hidden="true" />
                </div>
                <div className="min-w-0 flex-1">
                    <p className="text-sm font-bold text-slate-900">{n.title}</p>
                    {n.body && <p className="mt-0.5 text-[13px] leading-snug text-slate-600">{n.body}</p>}
                    {n.link && (
                        <button
                            type="button"
                            onClick={() => onOpen(n)}
                            className="mt-2.5 inline-flex items-center gap-1.5 rounded-lg bg-brand px-3 py-1.5 text-xs font-bold text-white transition-colors hover:bg-brand-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-1"
                        >
                            Open briefing
                            <ArrowRight size={13} aria-hidden="true" />
                        </button>
                    )}
                </div>
                <button
                    type="button"
                    onClick={() => dismissCard(n.id)}
                    aria-label="Dismiss"
                    title="Dismiss — it stays in your notifications"
                    className="shrink-0 rounded-md p-1 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                >
                    <X size={14} />
                </button>
            </div>
        </div>
    );
};

/** Renders inside Layout's fixed top-right stack (beside the transcript
 *  sync indicator), so the two never overlap. */
export const NotificationToaster: React.FC = () => {
    const { unread, ready, markRead } = useNotifications();
    const { user } = useAuth();
    const { pathname } = useLocation();
    const navigate = useNavigate();
    const visible = useSyncExternalStore(cards.subscribe, cards.getSnapshot, cards.getSnapshot);
    /** Ids this tab has already looked at; null until the first poll lands. */
    const seen = useRef<Set<string> | null>(null);

    // The cards are this person's: signing in as someone else empties them
    // (before anything below can show more), and so does unmounting — which
    // is what signing out does — so no card outlives its reader.
    const owner = user?.email?.toLowerCase() ?? null;
    useEffect(() => {
        cards.claim(owner);
    }, [owner]);
    useEffect(() => () => cards.clear(), []);

    useEffect(() => {
        if (!ready) {
            // Signed out (or not loaded yet): the next person's first poll
            // seeds afresh, so their backlog never toasts either.
            seen.current = null;
            return;
        }
        const { toast, seen: next } = pickToasts(unread, seen.current, pathname);
        seen.current = next;
        if (toast.length) cards.show(toast);
    }, [unread, ready, pathname]);

    // A card whose notification was read elsewhere (the bell, the page it
    // links to, another tab) has nothing left to say.
    useEffect(() => {
        if (!ready) return;
        const live = new Set(unread.map(n => n.id));
        for (const c of cards.getSnapshot()) if (!live.has(c.id)) dismissCard(c.id);
    }, [unread, ready]);

    const open = (n: AppNotification) => {
        dismissCard(n.id);
        void markRead(n.id);
        if (n.link) navigate(n.link);
    };

    return (
        <div role="region" aria-label="New notifications" aria-live="polite" className="flex flex-col items-end gap-3">
            {visible.map(n => (
                <ToastCard key={n.id} n={n} onOpen={open} />
            ))}
        </div>
    );
};

export default NotificationToaster;
