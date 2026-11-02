import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { loadCurrentProfile } from './profileRequest.ts';

function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (reason: unknown) => void;
    const promise = new Promise<T>((yes, no) => {
        resolve = yes;
        reject = no;
    });
    return { promise, resolve, reject };
}

describe('profile requests across session changes', () => {
    it('discards a late home profile when a support session becomes active', async () => {
        const request = deferred<{ workspace: string }>();
        let active = 'home-token';
        let epoch = 1;
        const applied: string[] = [];
        let settled = 0;
        const pending = loadCurrentProfile({
            token: 'home-token',
            load: () => request.promise,
            isCurrentGeneration: () => epoch === 1,
            activeToken: () => active,
            onProfile: profile => applied.push(profile.workspace),
            onUnauthorized: () => assert.fail('stale request must not log out'),
            onError: () => assert.fail('stale request must not report an error'),
            onSettled: () => { settled += 1; },
        });

        active = 'support-token';
        epoch += 1;
        request.resolve({ workspace: 'home' });
        await pending;

        assert.deepEqual(applied, []);
        assert.equal(settled, 0);
    });

    it('discards an old response even after returning to the same home token', async () => {
        const request = deferred<{ workspace: string }>();
        let epoch = 1;
        const applied: string[] = [];
        const pending = loadCurrentProfile({
            token: 'home-token',
            load: () => request.promise,
            isCurrentGeneration: () => epoch === 1,
            activeToken: () => 'home-token',
            onProfile: profile => applied.push(profile.workspace),
            onUnauthorized: () => assert.fail('stale request must not log out'),
            onError: () => assert.fail('stale request must not report an error'),
            onSettled: () => assert.fail('stale request must not complete the new load'),
        });

        epoch += 2; // entered support, then returned home
        request.resolve({ workspace: 'old home profile' });
        await pending;

        assert.deepEqual(applied, []);
    });

    it('still handles a 401 for the current support request after its token is removed', async () => {
        const request = deferred<{ workspace: string }>();
        let active = 'support-token';
        let unauthorized = 0;
        const pending = loadCurrentProfile({
            token: 'support-token',
            load: () => request.promise,
            isCurrentGeneration: () => true,
            activeToken: () => active,
            onProfile: () => assert.fail('401 has no profile'),
            onUnauthorized: () => { unauthorized += 1; },
            onError: () => assert.fail('401 must restore the home session'),
            onSettled: () => assert.fail('support load must not complete the home load'),
        });

        active = 'home-token'; // the Axios 401 interceptor clears support first
        request.reject({ response: { status: 401 } });
        await pending;

        assert.equal(unauthorized, 1);
    });
});
