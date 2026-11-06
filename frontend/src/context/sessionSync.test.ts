import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { activeToken, readSupportToken, setSupportToken } from '../brand/storage.ts';
import { completeSupportEntry } from './supportEntry.ts';
import { syncHomeTokenStorageEvent } from './sessionSync.ts';

function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>(yes => { resolve = yes; });
    return { promise, resolve };
}

function fakeStorage(entries: Record<string, string> = {}): Storage {
    const map = new Map(Object.entries(entries));
    return {
        get length() { return map.size; },
        clear: () => map.clear(),
        getItem: key => map.get(key) ?? null,
        key: index => [...map.keys()][index] ?? null,
        removeItem: key => { map.delete(key); },
        setItem: (key, value) => { map.set(key, String(value)); },
    };
}

describe('two-tab auth identity', () => {
    it('keeps support in Tab B while Tab A stays home, then syncs home login and logout', () => {
        const home = fakeStorage({ token: 'home-one' });
        const tabA = fakeStorage();
        const tabB = fakeStorage();
        const a = { token: activeToken(home, tabA), profile: 'home-one', epoch: 0 };
        const b = { token: activeToken(home, tabB), profile: 'home-one', epoch: 0 };
        const switchA = (token: string | null) => { a.token = token; a.profile = ''; a.epoch += 1; };
        const switchB = (token: string | null) => { b.token = token; b.profile = ''; b.epoch += 1; };
        const event = { key: 'token', storageArea: home };

        assert.equal(setSupportToken('support-b', tabB), true);
        b.token = activeToken(home, tabB);
        b.profile = 'support-workspace';
        assert.equal(a.token, 'home-one');
        assert.equal(a.profile, 'home-one');
        assert.equal(activeToken(home, tabA), 'home-one');
        assert.equal(activeToken(home, tabB), 'support-b');

        // Tab A signs in again. Its own AuthContext switches directly; only
        // Tab B receives a browser storage event.
        home.setItem('token', 'home-two');
        switchA(activeToken(home, tabA));
        assert.equal(syncHomeTokenStorageEvent(event, home, tabB, b.token, () => activeToken(home, tabB), switchB), true);
        assert.deepEqual(a, { token: 'home-two', profile: '', epoch: 1 });
        assert.deepEqual(b, { token: 'home-two', profile: '', epoch: 1 });
        assert.equal(readSupportToken(tabB), null);
        assert.equal(activeToken(home, tabA), a.token);
        assert.equal(activeToken(home, tabB), b.token);

        a.profile = 'home-two';
        b.profile = 'home-two';
        home.removeItem('token');
        switchA(activeToken(home, tabA));
        assert.equal(syncHomeTokenStorageEvent(event, home, tabB, b.token, () => activeToken(home, tabB), switchB), true);
        assert.deepEqual(a, { token: null, profile: '', epoch: 2 });
        assert.deepEqual(b, { token: null, profile: '', epoch: 2 });
    });

    it('ignores unrelated storage events', () => {
        const home = fakeStorage({ token: 'home-one' });
        const tab = fakeStorage();
        let switches = 0;
        assert.equal(syncHomeTokenStorageEvent(
            { key: 'some-other-key', storageArea: home }, home, tab, 'home-one',
            () => activeToken(home, tab), () => { switches += 1; },
        ), false);
        assert.equal(switches, 0);
    });

    for (const nextHomeToken of [null, 'home-two']) {
        it(`rejects a late /enter after another tab ${nextHomeToken ? 'changes' : 'logs out of'} the home session`, async () => {
            const home = fakeStorage({ token: 'home-one' });
            const tab = fakeStorage();
            const event = { key: 'token', storageArea: home };
            const response = deferred<{ token: string }>();
            const revoked: string[] = [];
            let currentToken: string | null = 'home-one';
            let profile = 'home-one';
            let epoch = 1;
            const startToken = currentToken;
            const startEpoch = epoch;

            const pending = completeSupportEntry({
                result: response.promise,
                isCurrentHomeSession: () => epoch === startEpoch
                    && currentToken === startToken
                    && activeToken(home, tab) === startToken,
                isTokenValid: () => true,
                storeToken: token => setSupportToken(token, tab),
                activate: token => { currentToken = token; profile = 'support-workspace'; epoch += 1; },
                revoke: async token => { revoked.push(token); },
            });

            if (nextHomeToken) home.setItem('token', nextHomeToken);
            else home.removeItem('token');
            assert.equal(syncHomeTokenStorageEvent(
                event, home, tab, currentToken, () => activeToken(home, tab),
                token => { currentToken = token; profile = ''; epoch += 1; },
            ), true);
            response.resolve({ token: 'issued-for-old-actor' });
            await assert.rejects(pending, /home session changed/i);

            assert.equal(currentToken, nextHomeToken);
            assert.equal(profile, '');
            assert.equal(activeToken(home, tab), nextHomeToken);
            assert.equal(readSupportToken(tab), null);
            assert.deepEqual(revoked, ['issued-for-old-actor']);
        });
    }
});
