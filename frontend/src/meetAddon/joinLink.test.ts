// `meetJoinUrl` — open the calendar's Meet link on the AE's WORK account, where
// the Cubie add-on is installed. Landing on a personal account means Cubie is
// simply missing from Activities, with nothing on screen saying why.
//
//   cd <repo>/frontend && node --test "src/meetAddon/*.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { meetJoinUrl } from './joinLink.ts';

const AE = 'parker@acme.example';

describe('meetJoinUrl', () => {
    it('adds the work account to a plain Meet link', () => {
        const url = new URL(meetJoinUrl('https://meet.google.com/abc-defg-hij', AE));
        assert.equal(url.hostname, 'meet.google.com');
        assert.equal(url.pathname, '/abc-defg-hij');
        assert.equal(url.searchParams.get('authuser'), AE);
    });

    it('keeps the parameters the invite already carried', () => {
        const url = new URL(meetJoinUrl('https://meet.google.com/abc-defg-hij?hs=122', AE));
        assert.equal(url.searchParams.get('hs'), '122');
        assert.equal(url.searchParams.get('authuser'), AE);
    });

    it('leaves an authuser the link already chose alone', () => {
        const link = 'https://meet.google.com/abc-defg-hij?authuser=1';
        assert.equal(meetJoinUrl(link, AE), link);
    });

    it('never rewrites a link that is not Google Meet', () => {
        const zoom = 'https://zoom.us/j/123456';
        assert.equal(meetJoinUrl(zoom, AE), zoom);
        const lookalike = 'https://meet.google.com.evil.example/abc';
        assert.equal(meetJoinUrl(lookalike, AE), lookalike);
    });

    it('returns the link unchanged without an account or with a broken link', () => {
        assert.equal(meetJoinUrl('https://meet.google.com/abc-defg-hij', null), 'https://meet.google.com/abc-defg-hij');
        assert.equal(meetJoinUrl('https://meet.google.com/abc-defg-hij', '  '), 'https://meet.google.com/abc-defg-hij');
        assert.equal(meetJoinUrl('not a url', AE), 'not a url');
    });
});
