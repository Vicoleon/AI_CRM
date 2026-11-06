// What the drawer remembers per person: what they have seen, the week it last
// slid open by itself, and how far the Daily Briefing's spotlight bar was
// dismissed.
//
// ── THE WALK-ME SWAP POINT ───────────────────────────────────────────────────
// Per-person "seen" state belongs to the walk-me
// feature (table `user_tour_progress`, API `/api/tours`). Until the drawer
// moves onto it, this file keeps it in the browser's localStorage, under
// `whatsNew:<email>`, so it is per browser as well as per person. To move it,
// replace `createLocalMemoryStore` with a store over `/api/tours` that keeps
// this same interface — ONLY THIS FILE CHANGES. Nothing on the server holds it
// today.
// ─────────────────────────────────────────────────────────────────────────────
//
// It is a subscribable store so components read it with useSyncExternalStore
// (no setState in an effect, and a profile that loads late just re-reads):
// `getSnapshot` returns the same object until the stored value changes, a
// `save` notifies every subscriber, and so does another tab writing the same
// key (the window `storage` event). Every storage access is in try/catch: a
// private window, blocked site data or a full quota must never break the page.
// When storage refuses a write, the value is kept in memory for this page
// load, so closing the drawer still clears the badge.
//
// Import-free so it runs under Node's test runner (`memory.test.ts`).

export interface WhatsNewMemory {
    /** The newest `published_at` the person had when they last closed the
     *  drawer having seen the news (naive UTC instant, as the server sent it). */
    seenThrough: string | null;
    /** The ISO week (`2026-W42`) the drawer last slid open by itself. */
    teasedWeek: string | null;
    /** The newest `published_at` when the spotlight bar was last dismissed. */
    barDismissedThrough: string | null;
}

export interface WhatsNewMemoryStore {
    /** The person's memory now. A missing email reads as empty. */
    load(email: string | null | undefined): WhatsNewMemory;
    /** Merge `patch` in, store it, notify subscribers; returns the new value.
     *  A no-op without an email. */
    save(email: string | null | undefined, patch: Partial<WhatsNewMemory>): WhatsNewMemory;
    /** For useSyncExternalStore: called on every change, here or in another tab. */
    subscribe(callback: () => void): () => void;
    /** For useSyncExternalStore: the same object until the stored value changes. */
    getSnapshot(email: string | null | undefined): WhatsNewMemory;
}

/** The part of `window` the store listens on (the `storage` event). */
type StorageEvents = Pick<EventTarget, 'addEventListener' | 'removeEventListener'>;

export const MEMORY_KEY_PREFIX = 'whatsNew:';

export const EMPTY_MEMORY: WhatsNewMemory = Object.freeze({
    seenThrough: null,
    teasedWeek: null,
    barDismissedThrough: null,
});

const FIELDS = ['seenThrough', 'teasedWeek', 'barDismissedThrough'] as const satisfies ReadonlyArray<
    keyof WhatsNewMemory
>;

const normalEmail = (email: string | null | undefined): string => (email ?? '').trim().toLowerCase();

export const memoryKey = (email: string): string => `${MEMORY_KEY_PREFIX}${normalEmail(email)}`;

/** A stored value as a memory; anything unreadable (not JSON, not an object,
 *  a field of the wrong type) reads as empty rather than throwing. */
function parse(raw: string | null): WhatsNewMemory {
    if (raw === null) return EMPTY_MEMORY;
    let value: unknown;
    try {
        value = JSON.parse(raw);
    } catch {
        return EMPTY_MEMORY;
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) return EMPTY_MEMORY;
    const record = value as Record<string, unknown>;
    const field = (name: keyof WhatsNewMemory): string | null => {
        const v = record[name];
        return typeof v === 'string' && v ? v : null;
    };
    return {
        seenThrough: field('seenThrough'),
        teasedWeek: field('teasedWeek'),
        barDismissedThrough: field('barDismissedThrough'),
    };
}

const defaultStorage = (): Storage | null => globalThis.localStorage ?? null;

const defaultEvents = (): StorageEvents | null =>
    typeof window !== 'undefined' && typeof window.addEventListener === 'function' ? window : null;

export function createLocalMemoryStore(
    getStorage: () => Storage | null = defaultStorage,
    events: StorageEvents | null = defaultEvents(),
): WhatsNewMemoryStore {
    const listeners = new Set<() => void>();
    // Last snapshot per email, keyed by the raw string it was parsed from.
    const snapshots = new Map<string, { raw: string | null; value: WhatsNewMemory }>();
    // Values storage refused to keep (newer than what storage holds).
    const unsaved = new Map<string, string>();

    const storage = (): Storage | null => {
        try {
            return getStorage();
        } catch {
            return null;
        }
    };

    const readRaw = (email: string): string | null => {
        const pending = unsaved.get(email);
        if (pending !== undefined) return pending;
        try {
            return storage()?.getItem(memoryKey(email)) ?? null;
        } catch {
            return null;
        }
    };

    const writeRaw = (email: string, raw: string): void => {
        try {
            const s = storage();
            if (!s) throw new Error('no storage');
            s.setItem(memoryKey(email), raw);
            unsaved.delete(email);
        } catch {
            unsaved.set(email, raw);
        }
    };

    const emit = (): void => {
        for (const listener of [...listeners]) listener();
    };

    const onStorage = (event: Event): void => {
        const key = (event as StorageEvent).key;
        // `key` is null when another tab cleared the whole storage.
        if (key === null || key === undefined || key.startsWith(MEMORY_KEY_PREFIX)) emit();
    };

    const getSnapshot = (email: string | null | undefined): WhatsNewMemory => {
        const who = normalEmail(email);
        if (!who) return EMPTY_MEMORY;
        const raw = readRaw(who);
        const cached = snapshots.get(who);
        if (cached && cached.raw === raw) return cached.value;
        const value = parse(raw);
        snapshots.set(who, { raw, value });
        return value;
    };

    return {
        load: getSnapshot,
        getSnapshot,
        save(email, patch) {
            const who = normalEmail(email);
            if (!who) return EMPTY_MEMORY;
            const next: WhatsNewMemory = { ...getSnapshot(who) };
            for (const name of FIELDS) {
                if (name in patch) next[name] = patch[name] ?? null;
            }
            writeRaw(who, JSON.stringify(next));
            emit();
            return getSnapshot(who);
        },
        subscribe(callback) {
            listeners.add(callback);
            if (listeners.size === 1) events?.addEventListener('storage', onStorage);
            return () => {
                listeners.delete(callback);
                if (listeners.size === 0) events?.removeEventListener('storage', onStorage);
            };
        },
    };
}

/** The app's one store. */
export const memoryStore: WhatsNewMemoryStore = createLocalMemoryStore();
