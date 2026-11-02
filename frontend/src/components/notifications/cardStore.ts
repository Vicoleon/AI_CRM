// The "ready" toaster's cards: a tiny module store (the same shape as
// eos/Toast), kept import-free so it runs under Node's test runner.
//
// The store is module state, so it outlives the toaster, and signing out does
// not reload the tab. So it belongs to ONE person: claiming it for anyone else
// (or for nobody, on sign-out) empties it, and so does the toaster unmounting.
// The next person to sign in on the same tab never sees the last person's
// meeting on a card.

export interface CardStore<T extends { id: string }> {
    subscribe: (cb: () => void) => () => void;
    /** Stable until the cards change (what `useSyncExternalStore` needs). */
    getSnapshot: () => readonly T[];
    /** Newest on top; older cards give way past the cap. */
    show: (rows: readonly T[]) => void;
    dismiss: (id: string) => void;
    /** The cards belong to `owner` from now on (null: nobody is signed in).
     *  A different owner than before empties the store. */
    claim: (owner: string | null) => void;
    /** Empty the store and forget its owner. */
    clear: () => void;
}

export function createCardStore<T extends { id: string }>(max: number): CardStore<T> {
    let cards: readonly T[] = [];
    let owner: string | null = null;
    const listeners = new Set<() => void>();
    const emit = () => listeners.forEach(l => l());
    const empty = () => {
        if (!cards.length) return;
        cards = [];
        emit();
    };

    return {
        subscribe: cb => {
            listeners.add(cb);
            return () => {
                listeners.delete(cb);
            };
        },
        getSnapshot: () => cards,
        show: rows => {
            if (!rows.length) return;
            const ids = new Set(rows.map(r => r.id));
            cards = [...rows, ...cards.filter(c => !ids.has(c.id))].slice(0, Math.max(0, max));
            emit();
        },
        dismiss: id => {
            if (!cards.some(c => c.id === id)) return;
            cards = cards.filter(c => c.id !== id);
            emit();
        },
        claim: next => {
            if (next === owner) return;
            owner = next;
            empty();
        },
        clear: () => {
            owner = null;
            empty();
        },
    };
}
