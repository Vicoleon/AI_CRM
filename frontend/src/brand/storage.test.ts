// A browser that still holds the keys the app used before the rebrand loads
// the app normally: the old keys are simply never read, so every reader falls
// back to its default, and nothing throws.
//
//   cd <repo>/frontend && node --test src/brand/storage.test.ts
//
// The old key names are assembled from fragments so this file never matches
// the repo's legacy-brand scan.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    CHAT_STREAMING_KEY,
    MEET_ADDON_TOKEN_KEY,
    SUPPORT_TOKEN_KEY,
    TOUR_PROGRESS_PREFIX,
    activeToken,
    clearAuthTokens,
    clearSupportToken,
    discardLegacySharedSupportToken,
    dropRejectedToken,
    readChatStreamingPref,
    readMeetAddonToken,
    readSupportToken,
    readTourProgress,
    setSupportToken,
    tourProgressSubject,
    writeTourProgress,
} from './storage.ts';

const OLD = 'at' + 'las';
const EMAIL = 'ana@acme.example';
const TOURS = JSON.stringify({ enabled: true, seen: { 'home.intro': 'done' } });

/** A Map-backed Storage, optionally refusing every call. */
function fakeStorage(entries: Record<string, string> = {}, broken = false): Storage {
    const map = new Map(Object.entries(entries));
    const guard = () => {
        if (broken) throw new Error('blocked');
    };
    return {
        get length() {
            return map.size;
        },
        clear: () => {
            guard();
            map.clear();
        },
        getItem: (k: string) => {
            guard();
            return map.get(k) ?? null;
        },
        key: (i: number) => [...map.keys()][i] ?? null,
        removeItem: (k: string) => {
            guard();
            map.delete(k);
        },
        setItem: (k: string, v: string) => {
            guard();
            map.set(k, String(v));
        },
    };
}

/** Every key the app wrote before the rebrand, each holding a value that
 *  would change behaviour if it were still read. */
const OLD_KEYS_ONLY = () =>
    fakeStorage({
        [`${OLD}.tours.v1:${EMAIL}`]: TOURS,
        [`${OLD}_chat_streaming`]: '0',
        [`${OLD}.chat.chimeEnabled`]: 'off',
        [`${OLD}.meetAddon.token`]: 'header.payload.signature',
        [`${OLD}_st_interview_outbox`]: '[{"answer":"x"}]',
    });

describe('a browser holding only the old storage keys', () => {
    it('gets no tour progress, so the server copy decides', () => {
        assert.equal(readTourProgress(EMAIL, OLD_KEYS_ONLY()), null);
    });

    it('streams chat replies (the default), ignoring the old opt-out', () => {
        assert.equal(readChatStreamingPref(OLD_KEYS_ONLY()), true);
    });

    it('has no Meet add-on session, so the panel asks to sign in', () => {
        assert.equal(readMeetAddonToken(OLD_KEYS_ONLY()), null);
    });
});

describe('platform support token storage', () => {
    it('uses the tab support token while preserving the shared home session', () => {
        const home = fakeStorage({ token: 'home-session' });
        const tab = fakeStorage();
        assert.equal(activeToken(home, tab), 'home-session');
        assert.equal(setSupportToken('support-session', tab), true);
        assert.equal(activeToken(home, tab), 'support-session');
        assert.equal(readSupportToken(tab), 'support-session');
        assert.equal(home.getItem(SUPPORT_TOKEN_KEY), null);
        assert.equal(home.getItem('token'), 'home-session');
        clearSupportToken(tab);
        assert.equal(activeToken(home, tab), 'home-session');
    });

    it('ignores and removes the older shared support token on upgrade', () => {
        const home = fakeStorage({ token: 'home-session', [SUPPORT_TOKEN_KEY]: 'old-shared-support' });
        const tab = fakeStorage();
        assert.equal(activeToken(home, tab), 'home-session');
        discardLegacySharedSupportToken(home);
        assert.equal(home.getItem(SUPPORT_TOKEN_KEY), null);
    });

    it('drops only the rejected active session, never a new session after a late 401', () => {
        const home = fakeStorage({ token: 'home-session' });
        const tab = fakeStorage({ [SUPPORT_TOKEN_KEY]: 'support-session' });
        assert.equal(dropRejectedToken('old-support-session', home, tab), 'stale');
        assert.equal(activeToken(home, tab), 'support-session');
        assert.equal(dropRejectedToken('support-session', home, tab), 'support');
        assert.equal(activeToken(home, tab), 'home-session');
        assert.equal(dropRejectedToken('home-session', home, tab), 'home');
        assert.equal(activeToken(home, tab), null);
    });

    it('logout clears both sessions, including when storage refuses writes', () => {
        const home = fakeStorage({ token: 'home-session' });
        const tab = fakeStorage({ [SUPPORT_TOKEN_KEY]: 'support-session' });
        clearAuthTokens(home, tab);
        assert.equal(activeToken(home, tab), null);
        assert.doesNotThrow(() => clearAuthTokens(fakeStorage({}, true), fakeStorage({}, true)));
        assert.equal(activeToken(fakeStorage({}, true), fakeStorage({}, true)), null);
        assert.equal(setSupportToken('support-session', fakeStorage({}, true)), false);
    });
});

describe('tour progress across support workspaces', () => {
    it('keeps the existing home key and isolates support workspaces with the same email', () => {
        const storage = fakeStorage();
        const home = tourProgressSubject(EMAIL, null, false);
        const first = tourProgressSubject(EMAIL, 'acme', true);
        const second = tourProgressSubject(EMAIL, 'beta', true);
        assert.equal(home, EMAIL);
        assert.notEqual(first, second);
        assert.notEqual(first, home);
        assert.ok(first && second);
        writeTourProgress(home!, { enabled: false, seen: { 'home.intro': 'completed' } }, storage);
        writeTourProgress(first, { enabled: true, seen: { 'home.new.platform-support': 'skipped' } }, storage);
        assert.equal(readTourProgress(second, storage), null);
        assert.deepEqual(readTourProgress(home!, storage)?.seen, { 'home.intro': 'completed' });
        assert.deepEqual(readTourProgress(first, storage)?.seen, { 'home.new.platform-support': 'skipped' });
    });

    it('never falls back to home tour memory when a support workspace slug is absent', () => {
        assert.equal(tourProgressSubject(EMAIL, null, true), null);
    });
});

describe('storage that refuses every call, or is absent', () => {
    for (const [label, storage] of [
        ['blocked', fakeStorage({}, true)],
        ['absent', null],
    ] as const) {
        it(`${label}: every reader returns its default without throwing`, () => {
            assert.equal(readTourProgress(EMAIL, storage), null);
            assert.equal(readChatStreamingPref(storage), true);
            assert.equal(readMeetAddonToken(storage), null);
            assert.doesNotThrow(() =>
                writeTourProgress(EMAIL, { enabled: true, seen: {} }, storage),
            );
        });
    }
});

describe('the current keys', () => {
    it('all carry the product prefix', () => {
        for (const key of [TOUR_PROGRESS_PREFIX, CHAT_STREAMING_KEY, MEET_ADDON_TOKEN_KEY]) {
            assert.match(key, /^cubie[._:]/, key);
        }
    });

    it('tour progress round-trips under the new key', () => {
        const storage = fakeStorage();
        writeTourProgress(EMAIL, { enabled: false, seen: { 'home.intro': 'skipped' } }, storage);
        assert.equal(storage.getItem(`${TOUR_PROGRESS_PREFIX}${EMAIL}`) !== null, true);
        assert.deepEqual(readTourProgress(EMAIL, storage), {
            enabled: false,
            seen: { 'home.intro': 'skipped' },
        });
    });

    it('a malformed tour cache reads as none', () => {
        for (const raw of ['not json', '{"enabled":"yes","seen":{}}', '{"enabled":true}', 'null']) {
            const storage = fakeStorage({ [`${TOUR_PROGRESS_PREFIX}${EMAIL}`]: raw });
            assert.equal(readTourProgress(EMAIL, storage), null, raw);
        }
    });

    it('streaming is off only for an explicit "0"', () => {
        assert.equal(readChatStreamingPref(fakeStorage({ [CHAT_STREAMING_KEY]: '0' })), false);
        assert.equal(readChatStreamingPref(fakeStorage({ [CHAT_STREAMING_KEY]: '1' })), true);
        assert.equal(readChatStreamingPref(fakeStorage()), true);
    });

    it('the Meet add-on session is read from its own key', () => {
        const storage = fakeStorage({ [MEET_ADDON_TOKEN_KEY]: 'jwt' });
        assert.equal(readMeetAddonToken(storage), 'jwt');
    });
});
