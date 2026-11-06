// Which notifications toast, and matching their links to the route on screen.
//
//   cd <repo>/frontend && node --test "src/components/notifications/**/*.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import type { AppNotification } from '../../api/notifications';
import {
    MEETING_PREP_FAILED,
    MEETING_PREP_READY,
    ackMatches,
    badgeLabel,
    isCurrentRoute,
    linkPath,
    meetingPrepEventId,
    pickToasts,
    prepNoticeStep,
    relativeTime,
    unackedPrepNotices,
} from './rules.ts';

const note = (over: Partial<AppNotification> & { id: string }): AppNotification => ({
    kind: MEETING_PREP_READY,
    title: 'Your meeting prep is ready',
    body: 'Quarterly review — v2 is ready to review.',
    severity: 'info',
    link: '/meeting-prep/evt-1',
    created_at: '2026-10-02T15:00:00',
    read_at: null,
    ...over,
});

// What the server writes: urllib.parse.quote(event_id, safe="").
const pyQuote = (s: string) =>
    encodeURIComponent(s).replace(/[!'()*]/g, c => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);

describe('linkPath', () => {
    it('drops the query, hash and trailing slash, and decodes', () => {
        assert.equal(linkPath('/chat?session=abc'), '/chat');
        assert.equal(linkPath('/meeting-prep/abc%40google.com#top'), '/meeting-prep/abc@google.com');
        assert.equal(linkPath('/settings/'), '/settings');
        assert.equal(linkPath('/'), '/');
    });

    it('is null for nothing, and for another site', () => {
        assert.equal(linkPath(null), null);
        assert.equal(linkPath(''), null);
        assert.equal(linkPath('https://example.com/meeting-prep/x'), null);
    });

    it('keeps a malformed escape rather than throwing', () => {
        assert.equal(linkPath('/meeting-prep/100%'), '/meeting-prep/100%');
    });
});

describe('isCurrentRoute', () => {
    it('matches an event id the server and the browser encoded differently', () => {
        const id = "abc_20261002T150000Z(it's)*!";
        const serverLink = `/meeting-prep/${pyQuote(id)}`;
        const browserPath = `/meeting-prep/${encodeURIComponent(id)}`;
        assert.notEqual(serverLink, browserPath);
        assert.ok(isCurrentRoute(serverLink, browserPath));
    });

    it('matches when the browser left a character unencoded', () => {
        assert.ok(isCurrentRoute('/meeting-prep/abc%40x.example', '/meeting-prep/abc@x.example'));
    });

    it('a different meeting, or another page, is not the current route', () => {
        assert.ok(!isCurrentRoute('/meeting-prep/evt-1', '/meeting-prep/evt-2'));
        assert.ok(!isCurrentRoute('/meeting-prep/evt-1', '/productivity'));
        assert.ok(!isCurrentRoute(null, '/meeting-prep/evt-1'));
    });

    it('ignores the query on either side', () => {
        assert.ok(isCurrentRoute('/chat?session=a', '/chat'));
    });
});

describe('meetingPrepEventId', () => {
    it('reads the decoded event id', () => {
        assert.equal(meetingPrepEventId('/meeting-prep/abc%40google.com'), 'abc@google.com');
        assert.equal(meetingPrepEventId(`/meeting-prep/${pyQuote('a/b')}`), 'a/b');
    });

    it('is null for any other link', () => {
        assert.equal(meetingPrepEventId('/chat?session=x'), null);
        assert.equal(meetingPrepEventId('/meeting-prep/'), null);
        assert.equal(meetingPrepEventId(null), null);
    });
});

describe('pickToasts', () => {
    it('the first look toasts nothing — rows already waiting are remembered', () => {
        const rows = [note({ id: 'a' }), note({ id: 'b', kind: MEETING_PREP_FAILED })];
        const { toast, seen } = pickToasts(rows, null, '/');
        assert.deepEqual(toast, []);
        assert.deepEqual([...seen].sort(), ['a', 'b']);
    });

    it('a new meeting-prep notification toasts once', () => {
        const first = pickToasts([note({ id: 'a' })], null, '/');
        const rows = [note({ id: 'b' }), note({ id: 'a' })];
        const second = pickToasts(rows, first.seen, '/productivity');
        assert.deepEqual(
            second.toast.map(n => n.id),
            ['b'],
        );
        const third = pickToasts(rows, second.seen, '/productivity');
        assert.deepEqual(third.toast, []);
    });

    it('failures toast too', () => {
        const { toast } = pickToasts([note({ id: 'f', kind: MEETING_PREP_FAILED })], new Set(), '/');
        assert.equal(toast.length, 1);
    });

    it('other kinds never toast (the bell and the nav dots carry them)', () => {
        const { toast, seen } = pickToasts(
            [note({ id: 's', kind: 'scheduled_task_result', link: '/chat?session=1' })],
            new Set(),
            '/',
        );
        assert.deepEqual(toast, []);
        assert.ok(seen.has('s'));
    });

    it('no toast on the page it links to — that page catches up with it', () => {
        const id = 'abc_20261002T150000Z';
        const row = note({ id: 'n1', link: `/meeting-prep/${pyQuote(id)}` });
        const onPage = pickToasts([row], new Set(), `/meeting-prep/${encodeURIComponent(id)}`);
        assert.deepEqual(onPage.toast, []);
        // Not remembered while it is the page's to handle…
        assert.ok(!onPage.seen.has('n1'));
        // …and still not toasted on the next poll on that page.
        assert.deepEqual(pickToasts([row], onPage.seen, `/meeting-prep/${encodeURIComponent(id)}`).toast, []);
    });

    it('left that page while it was still unread: it toasts on the next page, once', () => {
        const row = note({ id: 'n1', link: '/meeting-prep/evt-1' });
        const onPage = pickToasts([row], new Set(), '/meeting-prep/evt-1');
        const away = pickToasts([row], onPage.seen, '/productivity');
        assert.deepEqual(
            away.toast.map(n => n.id),
            ['n1'],
        );
        assert.ok(away.seen.has('n1'));
        assert.deepEqual(pickToasts([row], away.seen, '/').toast, []);
    });

    it('the page marked it read: it is gone from unread and never toasts', () => {
        const row = note({ id: 'n1', link: '/meeting-prep/evt-1' });
        const onPage = pickToasts([row], new Set(), '/meeting-prep/evt-1');
        assert.deepEqual(pickToasts([], onPage.seen, '/productivity').toast, []);
    });

    it('the first look still remembers a row for the page on screen (the backlog never toasts)', () => {
        const row = note({ id: 'n1', link: '/meeting-prep/evt-1' });
        const first = pickToasts([row], null, '/meeting-prep/evt-1');
        assert.ok(first.seen.has('n1'));
        assert.deepEqual(pickToasts([row], first.seen, '/').toast, []);
    });

    it('a notification for another meeting still toasts on a meeting-prep page', () => {
        const { toast } = pickToasts([note({ id: 'n', link: '/meeting-prep/evt-2' })], new Set(), '/meeting-prep/evt-1');
        assert.equal(toast.length, 1);
    });

    it('keeps the feed order (newest first)', () => {
        const rows = [note({ id: 'new' }), note({ id: 'old' })];
        assert.deepEqual(
            pickToasts(rows, new Set(), '/').toast.map(n => n.id),
            ['new', 'old'],
        );
    });
});

describe('unackedPrepNotices', () => {
    it('this meeting’s meeting-prep rows only, encoded ids included', () => {
        const rows = [
            note({ id: 'mine', link: '/meeting-prep/a%40b' }),
            note({ id: 'failed', kind: MEETING_PREP_FAILED, link: '/meeting-prep/a@b' }),
            note({ id: 'other', link: '/meeting-prep/evt-2' }),
            note({ id: 'task', kind: 'scheduled_task_result', link: '/meeting-prep/a@b' }),
        ];
        assert.deepEqual(
            unackedPrepNotices(rows, 'a@b').map(n => n.id),
            ['mine', 'failed'],
        );
    });

    it('no meeting, no rows', () => {
        assert.deepEqual(unackedPrepNotices([note({ id: 'a' })], ''), []);
        assert.deepEqual(unackedPrepNotices([note({ id: 'a' })], null), []);
    });
});

describe('prepNoticeStep', () => {
    const row = [note({ id: 'a' })];

    it('nothing to catch up with', () => {
        assert.equal(prepNoticeStep([], { following: false, opening: false }), 'none');
    });

    it('a notification nobody on the page acknowledged: refresh and mark it read', () => {
        assert.equal(prepNoticeStep(row, { following: false, opening: false }), 'refresh');
    });

    it('waits while the page follows a build of this meeting, or is still opening', () => {
        assert.equal(prepNoticeStep(row, { following: true, opening: false }), 'wait');
        assert.equal(prepNoticeStep(row, { following: false, opening: true }), 'wait');
    });
});

describe('ackMatches', () => {
    const cutoff = Date.parse('2026-10-02T15:01:30Z') + 20_000;

    it('covers this meeting’s notifications up to the cutoff', () => {
        const ack = { eventId: 'evt-1', cutoffMs: cutoff };
        assert.ok(ackMatches(ack, note({ id: 'a', created_at: '2026-10-02T15:01:31' })));
        assert.ok(ackMatches(ack, note({ id: 'a2', created_at: '2026-10-02T15:01:50' })));
        assert.ok(ackMatches(ack, note({ id: 'b', kind: MEETING_PREP_FAILED, created_at: '2026-10-02T14:00:00' })));
    });

    it('not a later build’s notification, another meeting, or another kind', () => {
        const ack = { eventId: 'evt-1', cutoffMs: cutoff };
        assert.ok(!ackMatches(ack, note({ id: 'a', created_at: '2026-10-02T15:02:30' })));
        assert.ok(!ackMatches(ack, note({ id: 'b', link: '/meeting-prep/evt-2' })));
        assert.ok(!ackMatches(ack, note({ id: 'c', kind: 'scheduled_task_result' })));
    });

    it('matches encoded event ids', () => {
        const ack = { eventId: 'a@b', cutoffMs: null };
        assert.ok(ackMatches(ack, note({ id: 'a', link: '/meeting-prep/a%40b' })));
    });
});

describe('badgeLabel', () => {
    it('is empty at zero and caps at 99+', () => {
        assert.equal(badgeLabel(0), null);
        assert.equal(badgeLabel(3), '3');
        assert.equal(badgeLabel(99), '99');
        assert.equal(badgeLabel(100), '99+');
    });
});

describe('relativeTime', () => {
    const now = Date.parse('2026-10-02T15:00:00Z');

    it('reads the naive-UTC timestamps the API sends', () => {
        assert.equal(relativeTime('2026-10-02T14:59:50', now), 'just now');
        assert.equal(relativeTime('2026-10-02T14:55:00', now), '5 min ago');
        assert.equal(relativeTime('2026-10-02T12:00:00', now), '3 h ago');
        assert.equal(relativeTime('2026-10-01T13:00:00', now), 'yesterday');
        assert.equal(relativeTime('2026-09-28T15:00:00', now), '4 days ago');
    });

    it('older than a week is a date', () => {
        const out = relativeTime('2026-09-01T15:00:00', now);
        assert.ok(out && !/ago/.test(out), out);
    });

    it('a timestamp slightly in the future (clock skew) is just now', () => {
        assert.equal(relativeTime('2026-10-02T15:00:20', now), 'just now');
    });

    it('nothing usable is empty', () => {
        assert.equal(relativeTime(null, now), '');
        assert.equal(relativeTime('nope', now), '');
    });
});
