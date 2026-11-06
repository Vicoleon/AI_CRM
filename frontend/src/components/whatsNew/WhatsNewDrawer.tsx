// The What's New drawer: a right-hand panel over whatever page is open, mounted
// once in Layout beside <ToastHost/> and portalled to <body> (the sidebar clips
// its content). Opened from anywhere through store.ts.
//
// Two modes (store.ts):
//   full — someone asked for it: a modal dialog (backdrop, focus trap, Escape,
//          focus restored on close). It joins the shared dialog stack
//          (eos/dialogStack) so only the topmost dialog takes Escape and Tab.
//   peek — the weekly tease: no backdrop, no focus steal, no scroll lock, and
//          it never joins the dialog stack. It closes itself after ~5 s;
//          pointing at it, tabbing into it or the browser tab going to the
//          background pauses that, coming back restarts a short timer, Escape
//          closes it while focus is inside it. Clicking any control in it
//          (other than Close) turns it into the full drawer.
//
// What it shows: the Cubie changelog's release notes this person may see,
// newest first (the server filters them by feature). With none, "No updates
// yet." Nothing here drafts, approves or edits a note.
//
// "Seen": closing the FULL drawer marks everything seen, but only if the list
// was actually shown during that open; the tease never marks anything.
//
// Overlays: z-[150] sits above toasts (100), the bell popover (120) and the
// voice panel (130), and below Modal (200) and the radio player (9999, bottom
// right — hence the footer's right padding). The drawer never calls toast():
// results show in its own status line.
import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import { AnimatePresence, MotionConfig, motion } from 'framer-motion';
import { ArrowRight, CheckCheck, CheckCircle2, Info, Link2, Loader2, Sparkles, X } from 'lucide-react';
import { NOT_SWITCHED_ON, recordOpen, whatsNewError } from '../../api/whatsNew';
import type { WhatsNewItem } from '../../api/whatsNew';
import { formatInstant } from '../../utils/datetime';
import { isTopmost as isTopmostDialog, newDialogId, pop as popDialog, push as pushDialog } from '../eos/dialogStack';
import { closeWhatsNew, getWhatsNewDrawerState, promoteToFull, useWhatsNewDrawer } from './store';
import type { WhatsNewDrawerState } from './store';
import { useWhatsNew } from './WhatsNewProvider';
import type { WhatsNewContextValue } from './WhatsNewProvider';

/** The peek closes itself after this long, unless someone reaches for it. */
const PEEK_MS = 5000;
/** …and this long after the pointer or focus leaves it. */
const PEEK_RESUME_MS = 2000;

const FOCUSABLE =
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

const SUBTITLE = "Cubie's release notes, written by RubikSoft. You only see updates about features you can use.";
const FOR_ANOTHER_TEAM = 'This update is for another team.';

/** The DOM id of a note's card, for the Slack link's scroll-into-view. */
const itemDomId = (id: string): string => `whats-new-item-${id}`;

/** The feed has loaded and the feature is on: the list can render. */
const feedReady = (wn: WhatsNewContextValue): boolean => wn.feed !== null && !wn.off;

export const WhatsNewDrawer: React.FC = () => {
    const drawer = useWhatsNewDrawer();
    const wn = useWhatsNew();
    const { markAllSeen } = wn;
    const isFull = drawer.open && drawer.mode === 'full';
    const newsShown = drawer.open && isFull && feedReady(wn);

    // Whether the list has been on screen in the full drawer during this
    // open. Kept in a ref (written in an effect, read when closing) so closing
    // from a timer or a key handler sees the same answer as a click.
    const newsShownRef = useRef(false);
    useEffect(() => {
        if (!drawer.open) {
            newsShownRef.current = false;
            return;
        }
        if (newsShown) newsShownRef.current = true;
    }, [drawer.open, newsShown]);

    const requestClose = useCallback(() => {
        const s = getWhatsNewDrawerState();
        if (s.open && s.mode === 'full' && newsShownRef.current) markAllSeen();
        closeWhatsNew();
    }, [markAllSeen]);

    if (typeof document === 'undefined') return null;

    return createPortal(
        <MotionConfig reducedMotion="user">
            <AnimatePresence>
                {isFull && (
                    <motion.div
                        key="whats-new-backdrop"
                        className="fixed inset-0 z-[150] bg-gray-900/30"
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        exit={{ opacity: 0 }}
                        transition={{ duration: 0.15 }}
                        onMouseDown={e => {
                            if (e.target === e.currentTarget) requestClose();
                        }}
                        aria-hidden="true"
                    />
                )}
                {drawer.open && <DrawerPanel key="whats-new-panel" onRequestClose={requestClose} />}
            </AnimatePresence>
        </MotionConfig>,
        document.body,
    );
};

const DrawerPanel: React.FC<{ onRequestClose: () => void }> = ({ onRequestClose }) => {
    // Read live from the store (not from props): during the exit animation this
    // panel stays mounted, and its focus trap must let go the moment it closes.
    const drawer = useWhatsNewDrawer();
    const wn = useWhatsNew();
    const { buttonRef } = wn;
    const headingId = useId();
    const panelRef = useRef<HTMLDivElement>(null);
    const closeRef = useRef<HTMLButtonElement>(null);
    /** Where focus goes back to on close. */
    const restoreRef = useRef<HTMLElement | null>(null);
    const onRequestCloseRef = useRef(onRequestClose);
    useEffect(() => {
        onRequestCloseRef.current = onRequestClose;
    }, [onRequestClose]);

    const isFull = drawer.open && drawer.mode === 'full';
    const isPeek = drawer.open && drawer.mode === 'peek';

    /** Focus back to whatever opened the drawer, or the footer button. */
    const restoreFocus = useCallback(() => {
        const target = restoreRef.current;
        const usable =
            target && target !== document.body && document.contains(target) && !panelRef.current?.contains(target);
        (usable ? target : buttonRef.current)?.focus();
    }, [buttonRef]);

    // Remember the opener when the drawer opens (declared before the focus
    // effect below, which moves focus away from it).
    useEffect(() => {
        if (!drawer.open) return;
        const active = document.activeElement as HTMLElement | null;
        restoreRef.current = active && !panelRef.current?.contains(active) ? active : null;
    }, [drawer.open]);

    // Full mode: a modal dialog. Joins the shared stack so only the topmost
    // dialog answers Escape and traps Tab.
    useEffect(() => {
        if (!isFull) return;
        const id = newDialogId();
        pushDialog(id);
        const onKey = (e: KeyboardEvent) => {
            if (!isTopmostDialog(id)) return;
            if (e.key === 'Escape') {
                e.preventDefault();
                onRequestCloseRef.current();
                return;
            }
            if (e.key !== 'Tab') return;
            const panel = panelRef.current;
            if (!panel) return;
            const nodes = panel.querySelectorAll<HTMLElement>(FOCUSABLE);
            if (nodes.length === 0) {
                e.preventDefault();
                panel.focus();
                return;
            }
            const first = nodes[0];
            const last = nodes[nodes.length - 1];
            const active = document.activeElement;
            if (e.shiftKey && (active === first || !panel.contains(active))) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && (active === last || !panel.contains(active))) {
                e.preventDefault();
                first.focus();
            }
        };
        const prevOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        document.addEventListener('keydown', onKey);
        // Into the dialog once the portal has mounted, unless focus is already
        // inside (a peek someone clicked into).
        const focusTimer = window.setTimeout(() => {
            const panel = panelRef.current;
            if (!panel || panel.contains(document.activeElement)) return;
            (closeRef.current ?? panel).focus();
        }, 0);
        return () => {
            window.clearTimeout(focusTimer);
            document.body.style.overflow = prevOverflow;
            document.removeEventListener('keydown', onKey);
            popDialog(id);
            restoreFocus();
        };
    }, [isFull, restoreFocus]);

    // Peek mode: closes itself, pauses while someone points at it or tabs into
    // it, and Escape closes it only while focus is inside.
    useEffect(() => {
        if (!isPeek) return;
        const panel = panelRef.current;
        if (!panel) return;
        let hovered = false;
        let focused = false;
        let hidden = document.visibilityState === 'hidden';
        const closeIfStillPeeking = () => {
            const s = getWhatsNewDrawerState();
            if (s.open && s.mode === 'peek') closeWhatsNew();
        };
        let timer = window.setTimeout(closeIfStillPeeking, PEEK_MS);
        const pause = () => window.clearTimeout(timer);
        const resume = () => {
            if (hovered || focused || hidden) return;
            window.clearTimeout(timer);
            timer = window.setTimeout(closeIfStillPeeking, PEEK_RESUME_MS);
        };
        const onEnter = () => {
            hovered = true;
            pause();
        };
        const onLeave = () => {
            hovered = false;
            resume();
        };
        const onFocusIn = () => {
            focused = true;
            pause();
        };
        const onFocusOut = (e: FocusEvent) => {
            if (panel.contains(e.relatedTarget as Node | null)) return;
            focused = false;
            resume();
        };
        const onKey = (e: KeyboardEvent) => {
            if (e.key !== 'Escape' || !panel.contains(document.activeElement)) return;
            e.preventDefault();
            closeIfStillPeeking();
            restoreFocus();
        };
        // A peek never closes while nobody can see it: a hidden tab pauses
        // it, and coming back restarts the short timer.
        const onVisibility = () => {
            hidden = document.visibilityState === 'hidden';
            if (hidden) pause();
            else resume();
        };
        if (hidden) pause();
        panel.addEventListener('pointerenter', onEnter);
        panel.addEventListener('pointerleave', onLeave);
        panel.addEventListener('focusin', onFocusIn);
        panel.addEventListener('focusout', onFocusOut);
        document.addEventListener('keydown', onKey);
        document.addEventListener('visibilitychange', onVisibility);
        return () => {
            window.clearTimeout(timer);
            panel.removeEventListener('pointerenter', onEnter);
            panel.removeEventListener('pointerleave', onLeave);
            panel.removeEventListener('focusin', onFocusIn);
            panel.removeEventListener('focusout', onFocusOut);
            document.removeEventListener('keydown', onKey);
            document.removeEventListener('visibilitychange', onVisibility);
        };
    }, [isPeek, restoreFocus]);

    // Reaching for the peek — a click on any control in it except Close —
    // makes it the normal drawer. Capture phase, so it happens before the
    // control's own handler (a "Try it").
    const onClickCapture = (e: React.MouseEvent) => {
        if (getWhatsNewDrawerState().mode !== 'peek') return;
        const control = (e.target as HTMLElement).closest('button, a, input, select, textarea');
        if (!control || control.hasAttribute('data-whats-new-close')) return;
        promoteToFull();
    };

    const closeFromButton = () => {
        if (getWhatsNewDrawerState().mode === 'peek') {
            const hadFocus = panelRef.current?.contains(document.activeElement) ?? false;
            closeWhatsNew();
            if (hadFocus) restoreFocus();
            return;
        }
        onRequestClose();
    };

    const ready = feedReady(wn);
    // A sentence above the list: the link's own (a bad link), or a note this
    // reader can't see. "Another team" only once a feed asked for that very
    // note has come back without it (the server adds an older note the reader
    // may see), never from a feed that simply did not reach back that far.
    const notice =
        drawer.notice ??
        (ready &&
        drawer.itemId !== null &&
        wn.feedItemId === drawer.itemId &&
        !wn.items.some(i => i.id === drawer.itemId)
            ? FOR_ANOTHER_TEAM
            : null);

    return (
        <motion.div
            ref={panelRef}
            role={isFull ? 'dialog' : 'complementary'}
            aria-modal={isFull ? true : undefined}
            aria-labelledby={isFull ? headingId : undefined}
            aria-label={isFull ? undefined : "What's new"}
            tabIndex={-1}
            onClickCapture={onClickCapture}
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ duration: 0.22, ease: 'easeOut' }}
            className="fixed inset-y-0 right-0 z-[150] flex w-full max-w-md flex-col border-l border-gray-200 bg-white text-gray-900 shadow-2xl shadow-gray-900/20 outline-none"
        >
            <div className="flex flex-col gap-1.5 px-5 pb-3 pt-5">
                <div className="flex items-center gap-2.5">
                    <Sparkles size={20} className="flex-shrink-0 text-brand" aria-hidden="true" />
                    <h2 id={headingId} className="flex-1 text-lg font-bold text-gray-900">
                        What&apos;s new
                    </h2>
                    <button
                        ref={closeRef}
                        type="button"
                        onClick={closeFromButton}
                        data-whats-new-close=""
                        aria-label="Close What's new"
                        className="-my-2 -mr-2 inline-flex h-11 w-11 items-center justify-center rounded-lg text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                    >
                        <X size={18} aria-hidden="true" />
                    </button>
                </div>
                <p className="text-[13px] leading-[19px] text-gray-600">{SUBTITLE}</p>
            </div>

            {notice && (
                <p className="mx-5 mt-3 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[13px] text-amber-900">
                    <Info size={15} className="mt-0.5 flex-shrink-0" aria-hidden="true" />
                    {notice}
                </p>
            )}

            {!ready ? <FeedState wn={wn} /> : <NewsList drawer={drawer} wn={wn} onNavigate={onRequestClose} />}
        </motion.div>
    );
};

/** Before the feed is there: loading, switched off, or a failure to retry. */
const FeedState: React.FC<{ wn: WhatsNewContextValue }> = ({ wn }) => {
    if (wn.profileLoaded && !wn.enabled) {
        return <StateMessage>{NOT_SWITCHED_ON}</StateMessage>;
    }
    if (wn.error !== null) {
        return (
            <StateMessage>
                {whatsNewError(wn.error, "Cubie couldn't load what's new. Try again in a moment.")}
                {!wn.off && (
                    <button
                        type="button"
                        onClick={wn.reload}
                        className="mt-3 inline-flex min-h-[44px] items-center gap-2 rounded-lg border border-gray-200 bg-white px-4 text-sm font-medium text-gray-900 shadow-sm hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                    >
                        Try again
                    </button>
                )}
            </StateMessage>
        );
    }
    return (
        <StateMessage>
            <Loader2 size={18} className="mb-2 animate-spin text-gray-400 motion-reduce:animate-none" aria-hidden="true" />
            Loading what&apos;s new…
        </StateMessage>
    );
};

const StateMessage: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <div className="flex flex-1 flex-col items-center justify-center px-8 py-12 text-center text-sm text-gray-600">
        {children}
    </div>
);

const NewsList: React.FC<{
    drawer: WhatsNewDrawerState;
    wn: WhatsNewContextValue;
    /** A "Try it" link was followed: close the drawer. */
    onNavigate: () => void;
}> = ({ drawer, wn, onNavigate }) => {
    const [status, setStatus] = useState('');
    /** "Mark all as seen" replaced its own button: focus goes to the line
     *  that took its place, not to <body>. */
    const focusCaughtUpRef = useRef(false);
    const highlightId = drawer.itemId;
    const fromSlack = drawer.from === 'slack';

    // Scroll the deep-linked note into view once it has rendered (once per
    // note: a refetch must not yank the list back).
    const scrolledRef = useRef<string | null>(null);
    useEffect(() => {
        if (highlightId === null || scrolledRef.current === highlightId) return;
        const el = document.getElementById(itemDomId(highlightId));
        if (!el) return;
        scrolledRef.current = highlightId;
        el.scrollIntoView({ block: 'center' });
    }, [highlightId, wn.items]);

    const onTry = (item: WhatsNewItem) => {
        // Counted once per person per note; a failure never blocks the link.
        void recordOpen(item.id, 'try_it').catch(() => {});
    };

    const markAll = () => {
        focusCaughtUpRef.current = true;
        wn.markAllSeen();
        setStatus('Everything here is marked as seen.');
    };

    if (wn.items.length === 0) {
        return <StateMessage>No updates yet.</StateMessage>;
    }

    return (
        <div className="flex min-h-0 flex-1 flex-col">
            <div className="flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto px-5 pb-4 pt-3">
                {wn.items.map(item => (
                    <NewsCard
                        key={item.id}
                        item={item}
                        unseen={wn.unseenIds.has(item.id)}
                        highlighted={item.id === highlightId}
                        fromSlack={fromSlack && item.id === highlightId}
                        onTry={onTry}
                        onNavigate={onNavigate}
                    />
                ))}
            </div>

            {/* Right padding keeps the radio player (fixed, bottom right, above
                everything) off the buttons. */}
            <div className="flex flex-col gap-2 border-t border-gray-100 py-3 pl-5 pr-20">
                {wn.unseen.length > 0 ? (
                    <button
                        type="button"
                        onClick={markAll}
                        className="inline-flex min-h-[44px] items-center justify-center gap-2 rounded-lg border border-gray-200 bg-white text-sm font-medium text-gray-900 shadow-sm transition-colors hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                    >
                        <CheckCheck size={16} aria-hidden="true" />
                        Mark all as seen
                    </button>
                ) : (
                    <p
                        ref={el => {
                            // Takes focus only when it replaced the focused
                            // "Mark all as seen" button.
                            if (el && focusCaughtUpRef.current) {
                                focusCaughtUpRef.current = false;
                                el.focus();
                            }
                        }}
                        tabIndex={-1}
                        className="flex min-h-[44px] items-center justify-center gap-2 text-sm font-medium text-emerald-700 outline-none"
                    >
                        <CheckCircle2 size={16} aria-hidden="true" />
                        You&apos;re all caught up.
                    </p>
                )}
                <p role="status" className="text-center text-xs text-gray-600 empty:hidden">
                    {status}
                </p>
            </div>
        </div>
    );
};

const NewsCard: React.FC<{
    item: WhatsNewItem;
    unseen: boolean;
    highlighted: boolean;
    fromSlack: boolean;
    onTry: (item: WhatsNewItem) => void;
    onNavigate: () => void;
}> = ({ item, unseen, highlighted, fromSlack, onTry, onNavigate }) => {
    const date = formatInstant(item.published_at, { month: 'short', day: 'numeric' }, '');
    return (
        <article
            id={itemDomId(item.id)}
            className={`flex flex-col gap-1.5 rounded-xl border px-3.5 py-3 ${
                highlighted ? 'border-brand bg-brand-tint' : 'border-gray-200 bg-white'
            }`}
        >
            {fromSlack && (
                <span className="inline-flex items-center gap-1.5 self-start rounded-full border border-brand/30 bg-white px-2.5 py-0.5 text-xs font-semibold text-brand">
                    <Link2 size={13} aria-hidden="true" />
                    You came here from Slack
                </span>
            )}
            <div className="flex items-start gap-2">
                {unseen && (
                    <>
                        <span className="mt-1.5 h-2 w-2 flex-shrink-0 rounded-full bg-brand" title="New since you last looked" aria-hidden="true" />
                        <span className="sr-only">New:</span>
                    </>
                )}
                <h3 className="text-sm font-semibold leading-5 text-gray-900">{item.title}</h3>
            </div>
            {item.body && <p className="text-[13px] leading-[19px] text-gray-600">{item.body}</p>}
            <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                <span className="inline-flex flex-wrap items-center gap-2">
                    {date && <span className="text-xs text-gray-500">{date}</span>}
                    {item.feature_label && (
                        <span className="rounded bg-gray-100 px-1.5 text-[11px] leading-4 text-gray-600">
                            For {item.feature_label} users
                        </span>
                    )}
                </span>
                {item.path && (
                    <Link
                        to={item.path}
                        onClick={() => {
                            onTry(item);
                            onNavigate();
                        }}
                        className="inline-flex items-center gap-1 rounded py-1.5 text-[13px] font-semibold text-brand hover:text-brand-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                    >
                        Try it
                        <ArrowRight size={14} aria-hidden="true" />
                    </Link>
                )}
            </div>
        </article>
    );
};
