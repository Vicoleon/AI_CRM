// The shared cache behind Productivity's Email, Calendar, Slack and Documents.
//
//   cd <repo>/frontend && node --test "src/components/briefing/**/*.test.ts"
//
// The rules worth pinning are the ones that fail quietly: a second person on
// the same tab seeing the first person's inbox, a duplicate Slack snapshot
// eating the rate limit, or a folder change answered with the old folder scope.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createCachedResource } from './cachedResource.ts';

function deferred<T>() {
    let resolve!: (v: T) => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

function harness() {
    let who = 'alice';
    let clock = 1_000;
    const calls: ReturnType<typeof deferred<string>>[] = [];
    const resource = createCachedResource<string>(
        () => {
            const d = deferred<string>();
            calls.push(d);
            return d.promise;
        },
        {
            identity: () => who,
            freshMs: 60_000,
            fallbackError: 'Failed',
            now: () => clock,
        },
    );
    return {
        resource,
        calls,
        setWho: (w: string) => { who = w; },
        advance: (ms: number) => { clock += ms; },
    };
}

describe('createCachedResource', () => {
    it('joins a load already in flight instead of fetching twice', async () => {
        const h = harness();
        const first = h.resource.load();
        const second = h.resource.load();
        assert.equal(h.calls.length, 1);
        assert.equal(h.resource.getSnapshot().fetching, true);
        h.calls[0].resolve('inbox');
        await Promise.all([first, second]);
        assert.deepEqual(h.resource.getSnapshot(), { data: 'inbox', error: null, fetching: false });
    });

    it('serves a fresh copy without refetching, and refetches a stale one', async () => {
        const h = harness();
        const load = h.resource.load();
        h.calls[0].resolve('v1');
        await load;

        h.advance(59_000);
        await h.resource.load();
        assert.equal(h.calls.length, 1);

        h.advance(2_000);
        const again = h.resource.load();
        assert.equal(h.calls.length, 2);
        // The stale copy stays on screen while the refetch runs.
        assert.equal(h.resource.getSnapshot().data, 'v1');
        assert.equal(h.resource.getSnapshot().fetching, true);
        h.calls[1].resolve('v2');
        await again;
        assert.equal(h.resource.getSnapshot().data, 'v2');
    });

    it('never hands one person another person\'s copy', async () => {
        const h = harness();
        const load = h.resource.load();
        h.calls[0].resolve('alice inbox');
        await load;

        h.setWho('bob');
        assert.equal(h.resource.getSnapshot().data, null);

        // Bob's load starts clean even though Alice's copy is still fresh.
        const bobLoad = h.resource.load();
        assert.equal(h.calls.length, 2);
        h.calls[1].resolve('bob inbox');
        await bobLoad;
        assert.equal(h.resource.getSnapshot().data, 'bob inbox');
    });

    it('drops an answer that comes back after the person changed', async () => {
        const h = harness();
        const aliceLoad = h.resource.load();
        h.setWho('bob');
        const bobLoad = h.resource.load();
        h.calls[0].resolve('alice inbox');
        await aliceLoad;
        assert.equal(h.resource.getSnapshot().data, null);
        h.calls[1].resolve('bob inbox');
        await bobLoad;
        assert.equal(h.resource.getSnapshot().data, 'bob inbox');
    });

    it('a forced load supersedes the one in flight', async () => {
        const h = harness();
        const background = h.resource.load();
        const forced = h.resource.load({ force: true });
        assert.equal(h.calls.length, 2);

        h.calls[1].resolve('new folder scope');
        await forced;
        h.calls[0].resolve('old folder scope');
        await background;
        assert.equal(h.resource.getSnapshot().data, 'new folder scope');
        assert.equal(h.resource.getSnapshot().fetching, false);
    });

    it('keeps the last good copy when a refresh fails', async () => {
        const h = harness();
        const load = h.resource.load();
        h.calls[0].resolve('inbox');
        await load;

        const refresh = h.resource.load({ force: true });
        h.calls[1].reject({ response: { data: { detail: 'Gmail is down' } } });
        await refresh;
        assert.deepEqual(h.resource.getSnapshot(), {
            data: 'inbox', error: 'Gmail is down', fetching: false,
        });
    });

    it('retries after a failure even inside the fresh window', async () => {
        const h = harness();
        const load = h.resource.load();
        h.calls[0].reject(new Error('boom'));
        await load;
        assert.equal(h.resource.getSnapshot().error, 'boom');

        void h.resource.load();
        assert.equal(h.calls.length, 2);
    });

    it('notifies subscribers on every change', async () => {
        const h = harness();
        let seen = 0;
        const unsubscribe = h.resource.subscribe(() => { seen += 1; });
        const load = h.resource.load();
        h.calls[0].resolve('inbox');
        await load;
        assert.equal(seen, 2); // fetching, then landed
        unsubscribe();
        void h.resource.load({ force: true });
        assert.equal(seen, 2);
        h.calls[1].resolve('later');
    });
});
