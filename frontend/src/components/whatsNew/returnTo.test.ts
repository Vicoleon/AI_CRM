// The return trip after sign-in: only a What's New link is ever remembered or
// followed, so it can't become a redirect to anywhere else.
//
//   cd <repo>/frontend && node --test src/components/whatsNew/returnTo.test.ts
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    RETURN_TO_KEY,
    RETURN_TO_PATTERN,
    clearReturnTo,
    isWhatsNewReturnTo,
    peekReturnTo,
    rememberReturnTo,
} from './returnTo.ts';

/** A Map-backed Storage, optionally refusing every call. */
function fakeStorage(broken = false): Storage & { map: Map<string, string> } {
    const map = new Map<string, string>();
    const guard = () => {
        if (broken) throw new Error('blocked');
    };
    return {
        map,
        get length() {
            return map.size;
        },
        clear: () => {
            guard();
            map.clear();
        },
        getItem: (k: string) => {
            guard();
            return map.has(k) ? (map.get(k) as string) : null;
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

describe('which paths are followed after sign-in', () => {
    it('is pinned to the What\'s New links and the Chrome Connect page, nothing else', () => {
        assert.equal(
            RETURN_TO_PATTERN.source,
            String.raw`^(\/whats-new\/[A-Za-z0-9][A-Za-z0-9_-]{0,63}(\?src=slack)?|\/chrome\/connect)$`,
        );
    });

    it('accepts a note, a Slack arrival and Chrome Connect', () => {
        for (const ok of ['/whats-new/12', '/whats-new/rel-2026-10-27', '/whats-new/rel_7?src=slack', '/chrome/connect']) {
            assert.equal(isWhatsNewReturnTo(ok), true, ok);
        }
    });

    it('refuses anything that could send the person elsewhere', () => {
        for (const bad of [
            '/',
            '/whats-new/',
            '/whats-new/-abc',
            `/whats-new/${'x'.repeat(65)}`,
            '/whats-new/a.b',
            '/whats-new/12?src=email',
            '/whats-new/12?src=slack&next=//evil.example',
            '/whats-new/12#x',
            '//evil.example/whats-new/12',
            'https://evil.example/whats-new/12',
            '/whats-new/12/../../admin',
            '/whats-new/review/7',
            '/admin?tab=access',
            ' /whats-new/12',
            '/chrome/connect?next=//evil.example',
            '/chrome/connect/',
            '/chrome/connectx',
            '//evil.example/chrome/connect',
        ]) {
            assert.equal(isWhatsNewReturnTo(bad), false, bad);
        }
        assert.equal(isWhatsNewReturnTo(null), false);
        assert.equal(isWhatsNewReturnTo(42), false);
    });
});

describe('remember, peek, clear', () => {
    it('remembers a What\'s New link and reads it back', () => {
        const s = fakeStorage();
        rememberReturnTo('/whats-new/12', '?src=slack', s);
        assert.equal(s.map.get(RETURN_TO_KEY), '/whats-new/12?src=slack');
        assert.equal(peekReturnTo(s), '/whats-new/12?src=slack');
        // Peeking does not consume it (the link's own route clears it).
        assert.equal(peekReturnTo(s), '/whats-new/12?src=slack');
        clearReturnTo(s);
        assert.equal(peekReturnTo(s), null);
    });

    it('ignores any other page', () => {
        const s = fakeStorage();
        rememberReturnTo('/eos/rocks', '', s);
        rememberReturnTo('/whats-new/12', '?next=/admin', s);
        assert.equal(s.map.size, 0);
        assert.equal(peekReturnTo(s), null);
    });

    it('never follows a stored value that is not a What\'s New link', () => {
        const s = fakeStorage();
        s.map.set(RETURN_TO_KEY, '//evil.example');
        assert.equal(peekReturnTo(s), null);
    });

    it('a blocked or missing store costs the trip, never an exception', () => {
        const broken = fakeStorage(true);
        assert.doesNotThrow(() => rememberReturnTo('/whats-new/1', '', broken));
        assert.equal(peekReturnTo(broken), null);
        assert.doesNotThrow(() => clearReturnTo(broken));
        assert.doesNotThrow(() => rememberReturnTo('/whats-new/1', '', null));
        assert.equal(peekReturnTo(null), null);
    });
});
