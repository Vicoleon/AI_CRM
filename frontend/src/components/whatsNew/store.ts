// Whether the What's New drawer is open, and how: a module-level store read
// with useSyncExternalStore, the same shape as Toast and ConfirmDialog, so any
// call site (the footer button, the Daily Briefing bar, a Slack deep link, the
// weekly tease) opens it without a context or prop-drilling. One
// <WhatsNewDrawer/> (mounted once in Layout) renders it. The state survives
// route changes, which is how a deep link can open the drawer and then
// navigate home.
//
// Two modes:
//   full — the drawer someone asked for: a modal dialog with a backdrop.
//   peek — the weekly tease: it slides open by itself, non-modal, and closes
//          again unless the person reaches for it (promoteToFull).
import { useSyncExternalStore } from 'react';

/** What opened the drawer. `slack` = the `?src=slack` link in the channel post. */
export type WhatsNewFrom = 'slack' | 'button' | 'bar' | 'header';
export type WhatsNewMode = 'peek' | 'full';

export interface WhatsNewDrawerState {
    open: boolean;
    mode: WhatsNewMode;
    /** The note to scroll to and highlight (a changelog entry id). */
    itemId: string | null;
    from: WhatsNewFrom | null;
    /** A sentence to show at the top ("This update is no longer available."). */
    notice: string | null;
    /** The drawer has been shown, in either mode, during this page load. The
     *  tease never follows it: once per load at most, and never after the
     *  person has already looked. */
    openedThisLoad: boolean;
}

export interface OpenWhatsNewOptions {
    itemId?: string;
    from?: WhatsNewFrom;
    notice?: string;
}

const CLOSED: WhatsNewDrawerState = {
    open: false,
    mode: 'full',
    itemId: null,
    from: null,
    notice: null,
    openedThisLoad: false,
};

let state: WhatsNewDrawerState = CLOSED;
const listeners = new Set<() => void>();

const set = (next: WhatsNewDrawerState): void => {
    state = next;
    for (const listener of [...listeners]) listener();
};

/** Open the full drawer (or switch an open one to what was asked). */
export function openWhatsNew(opts: OpenWhatsNewOptions = {}): void {
    set({
        open: true,
        mode: 'full',
        itemId: opts.itemId ?? null,
        from: opts.from ?? null,
        notice: opts.notice ?? null,
        openedThisLoad: true,
    });
}

/** Slide the drawer open by itself (the weekly tease). A no-op when it is
 *  open or has already been shown during this page load; returns whether it
 *  opened. */
export function teaseWhatsNew(): boolean {
    if (state.open || state.openedThisLoad) return false;
    set({ ...CLOSED, open: true, mode: 'peek', openedThisLoad: true });
    return true;
}

/** The person reached for the peek (a click or key press on a control in it):
 *  it becomes the normal drawer. */
export function promoteToFull(): void {
    if (!state.open || state.mode === 'full') return;
    set({ ...state, mode: 'full' });
}

/** Close it. Everything else is kept so the closing animation shows the same
 *  content; the next open replaces it. */
export function closeWhatsNew(): void {
    if (!state.open) return;
    set({ ...state, open: false });
}

/** The state right now, for timers and handlers that must not act on what a
 *  render saw earlier. */
export const getWhatsNewDrawerState = (): WhatsNewDrawerState => state;

const subscribe = (callback: () => void): (() => void) => {
    listeners.add(callback);
    return () => {
        listeners.delete(callback);
    };
};

export function useWhatsNewDrawer(): WhatsNewDrawerState {
    return useSyncExternalStore(subscribe, getWhatsNewDrawerState, getWhatsNewDrawerState);
}
