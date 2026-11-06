// The toaster's card store belongs to one person.
//
//   cd <repo>/frontend && node --test "src/components/notifications/**/*.test.ts"
//
// It is module state and signing out does not reload the tab, so without an
// owner the next person to sign in on the same tab was shown the last
// person's "your meeting prep is ready" card (their meeting's name, and a
// button to it) until their own first poll landed.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createCardStore } from './cardStore.ts';

const card = (id: string) => ({ id, title: `card ${id}` });

describe('createCardStore', () => {
    it('shows newest on top, without duplicates, up to the cap', () => {
        const store = createCardStore<ReturnType<typeof card>>(3);
        store.show([card('a')]);
        store.show([card('b'), card('a')]);
        assert.deepEqual(
            store.getSnapshot().map(c => c.id),
            ['b', 'a'],
        );
        store.show([card('c'), card('d')]);
        assert.deepEqual(
            store.getSnapshot().map(c => c.id),
            ['c', 'd', 'b'],
        );
    });

    it('dismisses one card, and keeps the snapshot stable when nothing changed', () => {
        const store = createCardStore<ReturnType<typeof card>>(3);
        store.show([card('a'), card('b')]);
        const before = store.getSnapshot();
        store.dismiss('nope');
        assert.equal(store.getSnapshot(), before);
        store.dismiss('a');
        assert.deepEqual(
            store.getSnapshot().map(c => c.id),
            ['b'],
        );
    });

    it('signing in as someone else empties the cards', () => {
        const store = createCardStore<ReturnType<typeof card>>(3);
        let emitted = 0;
        store.subscribe(() => {
            emitted += 1;
        });
        store.claim('a@acme.example');
        store.show([card('a-meeting')]);
        emitted = 0;
        store.claim('b@acme.example');
        assert.deepEqual(store.getSnapshot(), []);
        assert.equal(emitted, 1);
    });

    it('signing out (no owner) empties them too', () => {
        const store = createCardStore<ReturnType<typeof card>>(3);
        store.claim('a@acme.example');
        store.show([card('a-meeting')]);
        store.claim(null);
        assert.deepEqual(store.getSnapshot(), []);
    });

    it('the same person keeps their cards (a re-render, a refreshed token)', () => {
        const store = createCardStore<ReturnType<typeof card>>(3);
        store.claim('a@acme.example');
        store.show([card('x')]);
        store.claim('a@acme.example');
        assert.deepEqual(
            store.getSnapshot().map(c => c.id),
            ['x'],
        );
    });

    it('clear (the toaster unmounting, which is what signing out does) empties and forgets the owner', () => {
        const store = createCardStore<ReturnType<typeof card>>(3);
        store.claim('a@acme.example');
        store.show([card('x')]);
        store.clear();
        assert.deepEqual(store.getSnapshot(), []);
        // The next mount claims afresh, even for the same person.
        store.show([card('y')]);
        store.claim('a@acme.example');
        assert.deepEqual(store.getSnapshot(), []);
    });
});
