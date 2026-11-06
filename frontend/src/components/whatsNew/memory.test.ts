// What the drawer remembers per person (the walk-me swap point): read safely,
// kept apart per person, stable for useSyncExternalStore, and live across tabs.
//
//   cd <repo>/frontend && node --test "src/components/whatsNew/memory.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { EMPTY_MEMORY, createLocalMemoryStore, memoryKey, memoryStore } from './memory.ts';

/** A Map-backed localStorage. */
class FakeStorage {
    data = new Map<string, string>();
    failReads = false;
    failWrites = false;
    getItem(key: string): string | null {
        if (this.failReads) throw new Error('SecurityError');
        return this.data.get(key) ?? null;
    }
    setItem(key: string, value: string): void {
        if (this.failWrites) throw new Error('QuotaExceededError');
        this.data.set(key, value);
    }
}

const setup = () => {
    const storage = new FakeStorage();
    const events = new EventTarget();
    const store = createLocalMemoryStore(() => storage as unknown as Storage, events);
    return { storage, events, store };
};

/** What another tab writing the key looks like here. */
const storageEvent = (key: string | null): Event => {
    const event = new Event('storage');
    Object.defineProperty(event, 'key', { value: key });
    return event;
};

describe('reading and saving', () => {
    it('is empty for someone who has never opened the drawer', () => {
        const { store } = setup();
        assert.deepEqual(store.load('ana@acme.example'), EMPTY_MEMORY);
    });

    it('keeps what was saved, merged field by field', () => {
        const { storage, store } = setup();
        store.save('ana@acme.example', { seenThrough: '2026-10-12T15:00:00' });
        const after = store.save('ana@acme.example', { teasedWeek: '2026-W42' });
        assert.deepEqual(after, {
            seenThrough: '2026-10-12T15:00:00',
            teasedWeek: '2026-W42',
            barDismissedThrough: null,
        });
        assert.deepEqual(store.load('ana@acme.example'), after);
        assert.ok(storage.data.has('whatsNew:ana@acme.example'));
    });

    it('clears a field saved as null', () => {
        const { store } = setup();
        store.save('ana@acme.example', { barDismissedThrough: '2026-10-12T15:00:00' });
        assert.equal(store.save('ana@acme.example', { barDismissedThrough: null }).barDismissedThrough, null);
    });
});

describe('per person', () => {
    it('keeps each person apart', () => {
        const { store } = setup();
        store.save('ana@acme.example', { teasedWeek: '2026-W42' });
        assert.equal(store.load('bo@acme.example').teasedWeek, null);
        store.save('bo@acme.example', { teasedWeek: '2026-W40' });
        assert.equal(store.load('ana@acme.example').teasedWeek, '2026-W42');
    });

    it('is the same person whatever the email case', () => {
        const { store } = setup();
        store.save(' Ana@ACME.example ', { teasedWeek: '2026-W42' });
        assert.equal(store.load('ana@acme.example').teasedWeek, '2026-W42');
        assert.equal(memoryKey('Ana@ACME.example'), 'whatsNew:ana@acme.example');
    });

    it('remembers nothing without an email', () => {
        const { storage, store } = setup();
        assert.deepEqual(store.save(null, { teasedWeek: '2026-W42' }), EMPTY_MEMORY);
        assert.deepEqual(store.save('  ', { teasedWeek: '2026-W42' }), EMPTY_MEMORY);
        assert.deepEqual(store.load(undefined), EMPTY_MEMORY);
        assert.equal(storage.data.size, 0);
    });
});

describe('corrupt or unavailable storage', () => {
    it('reads a corrupt value as empty instead of throwing', () => {
        const { storage, store } = setup();
        for (const raw of ['not json', '[1,2]', 'null', '42', '"text"']) {
            storage.data.set('whatsNew:ana@acme.example', raw);
            assert.deepEqual(store.load('ana@acme.example'), EMPTY_MEMORY, raw);
        }
    });

    it('keeps the good fields of a partly wrong value', () => {
        const { storage, store } = setup();
        storage.data.set('whatsNew:ana@acme.example', JSON.stringify({ seenThrough: 5, teasedWeek: '2026-W42', extra: true }));
        assert.deepEqual(store.load('ana@acme.example'), {
            seenThrough: null,
            teasedWeek: '2026-W42',
            barDismissedThrough: null,
        });
    });

    it('saves over a corrupt value', () => {
        const { storage, store } = setup();
        storage.data.set('whatsNew:ana@acme.example', '{oops');
        store.save('ana@acme.example', { teasedWeek: '2026-W42' });
        assert.equal(store.load('ana@acme.example').teasedWeek, '2026-W42');
    });

    it('reads as empty when storage throws, and keeps a save for this page load', () => {
        const { storage, store } = setup();
        storage.failReads = true;
        storage.failWrites = true;
        assert.deepEqual(store.load('ana@acme.example'), EMPTY_MEMORY);
        store.save('ana@acme.example', { seenThrough: '2026-10-12T15:00:00' });
        assert.equal(store.load('ana@acme.example').seenThrough, '2026-10-12T15:00:00');
    });

    it('stores again once storage accepts writes', () => {
        const { storage, store } = setup();
        storage.failWrites = true;
        store.save('ana@acme.example', { teasedWeek: '2026-W41' });
        storage.failWrites = false;
        store.save('ana@acme.example', { teasedWeek: '2026-W42' });
        assert.equal(JSON.parse(storage.data.get('whatsNew:ana@acme.example') ?? '{}').teasedWeek, '2026-W42');
    });

    it('survives storage that is not there at all', () => {
        const store = createLocalMemoryStore(() => {
            throw new Error('blocked');
        }, null);
        assert.deepEqual(store.load('ana@acme.example'), EMPTY_MEMORY);
        assert.equal(store.save('ana@acme.example', { teasedWeek: '2026-W42' }).teasedWeek, '2026-W42');
        // The app's own store under Node, where there is no localStorage.
        assert.deepEqual(memoryStore.load('ana@acme.example'), EMPTY_MEMORY);
    });
});

describe('useSyncExternalStore contract', () => {
    it('returns the same object until the stored value changes', () => {
        const { storage, store } = setup();
        const a = store.getSnapshot('ana@acme.example');
        assert.equal(store.getSnapshot('ana@acme.example'), a);
        store.save('ana@acme.example', { teasedWeek: '2026-W42' });
        const b = store.getSnapshot('ana@acme.example');
        assert.notEqual(b, a);
        assert.equal(store.getSnapshot('ana@acme.example'), b);
        // Saving the same values again stores the same string: same object.
        store.save('ana@acme.example', { teasedWeek: '2026-W42' });
        assert.equal(store.getSnapshot('ana@acme.example'), b);
        // Another tab changed it: a new object.
        storage.data.set('whatsNew:ana@acme.example', JSON.stringify({ teasedWeek: '2026-W43' }));
        assert.equal(store.getSnapshot('ana@acme.example').teasedWeek, '2026-W43');
    });

    it('notifies subscribers on a save, until they unsubscribe', () => {
        const { store } = setup();
        let calls = 0;
        const unsubscribe = store.subscribe(() => {
            calls += 1;
        });
        store.save('ana@acme.example', { teasedWeek: '2026-W42' });
        assert.equal(calls, 1);
        unsubscribe();
        store.save('ana@acme.example', { teasedWeek: '2026-W43' });
        assert.equal(calls, 1);
    });

    it('notifies when another tab writes a What\'s New key, and only then', () => {
        const { events, store } = setup();
        let calls = 0;
        const unsubscribe = store.subscribe(() => {
            calls += 1;
        });
        events.dispatchEvent(storageEvent('whatsNew:ana@acme.example'));
        assert.equal(calls, 1);
        events.dispatchEvent(storageEvent('token'));
        assert.equal(calls, 1);
        // The other tab cleared all of storage.
        events.dispatchEvent(storageEvent(null));
        assert.equal(calls, 2);
        unsubscribe();
        events.dispatchEvent(storageEvent('whatsNew:ana@acme.example'));
        assert.equal(calls, 2);
    });
});
