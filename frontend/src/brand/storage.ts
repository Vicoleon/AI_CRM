// Browser-storage keys under the product's own prefix, and the readers whose
// default matters when a key is missing.
//
// A browser may still hold keys written under the name the app had before the
// rebrand. Those are never read: each reader below looks only at its current
// key and falls back to its default, so an old key is simply ignored. Every
// access is in try/catch, because a private window or a blocked third-party
// iframe (the Meet side panel) can refuse storage outright.
//
// Import-free at runtime so it runs under Node's test runner (`storage.test.ts`).
import type { TourState } from '../api/tours';

/** Followed by the person's email: their last copy of which tours they saw. */
export const TOUR_PROGRESS_PREFIX = 'cubie.tours.v1:';
/** "0" turns streamed chat replies off in this browser; anything else is on. */
export const CHAT_STREAMING_KEY = 'cubie_chat_streaming';
/** The Meet side panel's own session JWT (see meetAddon/addonApi.ts). */
export const MEET_ADDON_TOKEN_KEY = 'cubie.meetAddon.token';
/** A short platform support session in sessionStorage; home stays in localStorage. */
export const SUPPORT_TOKEN_KEY = 'cubie.support.token';

/** Keep the old email-only home key for existing users, while support tours
 *  fall back only to a cache for their target workspace. Without a workspace
 *  slug, there is no safe support cache key. */
export function tourProgressSubject(email: string, workspaceSlug: string | null | undefined, inSupport: boolean): string | null {
    const person = email.trim().toLowerCase();
    if (!person) return null;
    if (!inSupport) return person;
    const workspace = workspaceSlug?.trim().toLowerCase();
    return workspace ? `${person}:support:${workspace}` : null;
}

const localStore = (): Storage | null => {
    try {
        return globalThis.localStorage ?? null;
    } catch {
        return null;
    }
};

const sessionStore = (): Storage | null => {
    try {
        return globalThis.sessionStorage ?? null;
    } catch {
        return null;
    }
};

function read(key: string, storage: Storage | null): string | null {
    try {
        return storage?.getItem(key) ?? null;
    } catch {
        return null;
    }
}

/** The tour memory this browser kept for `email`, or null when there is none
 *  or it is unreadable (the server copy then decides). */
export function readTourProgress(email: string, storage: Storage | null = localStore()): TourState | null {
    const raw = read(TOUR_PROGRESS_PREFIX + email, storage);
    if (!raw) return null;
    try {
        const parsed = JSON.parse(raw) as TourState | null;
        return typeof parsed?.enabled === 'boolean' && parsed.seen && typeof parsed.seen === 'object'
            ? parsed
            : null;
    } catch {
        return null;
    }
}

export function writeTourProgress(email: string, state: TourState, storage: Storage | null = localStore()): void {
    try {
        storage?.setItem(TOUR_PROGRESS_PREFIX + email, JSON.stringify(state));
    } catch {
        // Private window or storage full: the server copy is the real one.
    }
}

/** Streaming is the default; only an explicit "0" in this browser opts out. */
export function readChatStreamingPref(storage: Storage | null = localStore()): boolean {
    return read(CHAT_STREAMING_KEY, storage) !== '0';
}

/** The raw stored Meet side-panel session, or null. Validity is the caller's
 *  check (meetAddon/addonApi.ts). */
export function readMeetAddonToken(storage: Storage | null = localStore()): string | null {
    return read(MEET_ADDON_TOKEN_KEY, storage);
}

export function readSupportToken(storage: Storage | null = sessionStore()): string | null {
    return read(SUPPORT_TOKEN_KEY, storage);
}

/** One token source for axios, streamed Chat and the auth shell. */
export function activeToken(homeStorage: Storage | null = localStore(), supportStorage: Storage | null = sessionStore()): string | null {
    return readSupportToken(supportStorage) || read('token', homeStorage);
}

export function setSupportToken(token: string, storage: Storage | null = sessionStore()): boolean {
    try {
        if (!storage || !token) return false;
        storage?.setItem(SUPPORT_TOKEN_KEY, token);
        return true;
    } catch {
        // A support session cannot proceed if requests cannot read its token.
        return false;
    }
}

export function clearSupportToken(storage: Storage | null = sessionStore()): void {
    try {
        storage?.removeItem(SUPPORT_TOKEN_KEY);
    } catch {
        // Storage may be blocked by the browser.
    }
}

export function clearAuthTokens(homeStorage: Storage | null = localStore(), supportStorage: Storage | null = sessionStore()): void {
    clearSupportToken(supportStorage);
    try {
        homeStorage?.removeItem('token');
    } catch {
        // Storage may be blocked by the browser.
    }
}

/** A late 401 from an old request must not erase a newer session. */
export function dropRejectedToken(rejected: string, homeStorage: Storage | null = localStore(), supportStorage: Storage | null = sessionStore()): 'support' | 'home' | 'stale' {
    if (rejected && readSupportToken(supportStorage) === rejected) {
        clearSupportToken(supportStorage);
        return 'support';
    }
    if (rejected && read('token', homeStorage) === rejected) {
        try {
            homeStorage?.removeItem('token');
        } catch {
            // Storage may be blocked by the browser.
        }
        return 'home';
    }
    return 'stale';
}

/** Remove a token written to shared storage by the first support-session
 *  implementation. It must never become another tab's active identity. */
export function discardLegacySharedSupportToken(homeStorage: Storage | null = localStore()): void {
    try {
        homeStorage?.removeItem(SUPPORT_TOKEN_KEY);
    } catch {
        // Storage may be blocked by the browser.
    }
}
