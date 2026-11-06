// A small stale-while-revalidate store for one API read, shared by every screen
// that shows it. Kept free of the axios client and browser globals so it runs
// under Node's test runner; productivityCache.ts wires up the real reads.
//
//   * One copy per signed-in identity. A copy fetched for someone else (before
//     a logout or an impersonation switch) is never handed out.
//   * A load while one is already running joins it instead of starting another.
//     Slack's unread snapshot can take a couple of hundred rate-limited calls,
//     so a duplicate is both slow and costly. A forced load (Refresh, a
//     Documents folder change) starts a new one and the older answer is
//     dropped: it may predate whatever the user just changed.
//   * A copy younger than `freshMs` is served as is. An older one is still
//     shown at once and quietly refetched.
//   * Memory only. Nothing reaches browser storage, and a reload starts clean.

export interface CachedState<T> {
    data: T | null;
    error: string | null;
    fetching: boolean;
}

export interface CachedResource<T> {
    subscribe: (listener: () => void) => () => void;
    getSnapshot: () => CachedState<T>;
    load: (opts?: { force?: boolean }) => Promise<void>;
}

export interface CachedResourceOptions {
    // Who the copy belongs to. Changes on logout or impersonation.
    identity: () => string;
    freshMs: number;
    fallbackError: string;
    now?: () => number;
}

const EMPTY: CachedState<never> = { data: null, error: null, fetching: false };

export function errorMessage(err: unknown, fallback: string): string {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
    const message = err instanceof Error ? err.message : undefined;
    return detail || message || fallback;
}

export function createCachedResource<T>(
    fetcher: () => Promise<T>,
    { identity, freshMs, fallbackError, now = Date.now }: CachedResourceOptions,
): CachedResource<T> {
    let owner = '';
    let state: CachedState<T> = EMPTY;
    let fetchedAt = 0;
    let inflight: Promise<void> | null = null;
    // Bumped by every fetch and every identity change; only the latest lands.
    let generation = 0;
    const listeners = new Set<() => void>();

    const set = (next: CachedState<T>) => {
        state = next;
        listeners.forEach(l => l());
    };

    return {
        subscribe(listener) {
            listeners.add(listener);
            return () => {
                listeners.delete(listener);
            };
        },
        getSnapshot: () => (owner === identity() ? state : EMPTY),
        load({ force = false } = {}) {
            const who = identity();
            if (who !== owner) {
                owner = who;
                state = EMPTY;
                fetchedAt = 0;
                inflight = null;
                generation += 1;
            }
            if (inflight && !force) return inflight;
            if (!force && state.data !== null && now() - fetchedAt < freshMs) {
                return Promise.resolve();
            }
            const mine = ++generation;
            set({ ...state, error: null, fetching: true });
            const run: Promise<void> = fetcher()
                .then(
                    data => {
                        if (generation !== mine) return;
                        fetchedAt = now();
                        set({ data, error: null, fetching: false });
                    },
                    err => {
                        if (generation !== mine) return;
                        // Keep the last good copy on screen; the error rides along.
                        set({ data: state.data, error: errorMessage(err, fallbackError), fetching: false });
                    },
                )
                .finally(() => {
                    if (inflight === run) inflight = null;
                });
            inflight = run;
            return run;
        },
    };
}
