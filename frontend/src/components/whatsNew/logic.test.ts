// The What's New drawer's rules: which updates are new, when the drawer may
// slide open by itself, and the button's name.
//
//   cd <repo>/frontend && node --test "src/components/whatsNew/logic.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import type { WhatsNewItem } from '../../api/whatsNew';
import {
    TEASE_MIN_VIEWPORT,
    UNSEEN_FLOOR_DAYS,
    isLiveL10Path,
    isoWeekKey,
    newestPublishedAt,
    shouldTease,
    teaseAnnouncement,
    unseenItems,
    whatsNewButtonLabel,
} from './logic.ts';
import type { TeaseInput } from './logic.ts';

/** Only what the rules read, plus a number to name the item by. */
type TestItem = Pick<WhatsNewItem, 'published_at'> & { id: number };

const item = (over: Partial<TestItem> & { id: number }): TestItem => ({
    published_at: '2026-10-12T15:00:00',
    ...over,
});

/** A local date at noon (weeks are the reader's own). */
const local = (y: number, m: number, d: number, h = 12, min = 0): Date => new Date(y, m - 1, d, h, min);

describe('isoWeekKey', () => {
    it('names the ISO week in local time', () => {
        assert.equal(isoWeekKey(local(2026, 10, 14)), '2026-W42');
    });

    it('starts the week on Monday', () => {
        assert.equal(isoWeekKey(local(2026, 10, 18, 23, 59)), '2026-W42');
        assert.equal(isoWeekKey(local(2026, 10, 19, 0, 1)), '2026-W43');
    });

    it('puts the last days of December in next year when the week is', () => {
        assert.equal(isoWeekKey(local(2025, 12, 29)), '2026-W01');
        assert.equal(isoWeekKey(local(2024, 12, 30)), '2025-W01');
    });

    it('puts the first days of January in last year when the week is', () => {
        assert.equal(isoWeekKey(local(2026, 12, 31)), '2026-W53');
        assert.equal(isoWeekKey(local(2027, 1, 1)), '2026-W53');
        assert.equal(isoWeekKey(local(2027, 1, 3, 23, 59)), '2026-W53');
        assert.equal(isoWeekKey(local(2027, 1, 4, 0, 0)), '2027-W01');
        assert.equal(isoWeekKey(local(2021, 1, 3)), '2020-W53');
        assert.equal(isoWeekKey(local(2023, 1, 1)), '2022-W52');
    });

    it('is not moved by a daylight-saving change', () => {
        assert.equal(isoWeekKey(local(2026, 3, 8)), '2026-W10');
        assert.equal(isoWeekKey(local(2026, 3, 9)), '2026-W11');
        assert.equal(isoWeekKey(local(2026, 11, 1)), '2026-W44');
        assert.equal(isoWeekKey(local(2026, 11, 2)), '2026-W45');
    });
});

describe('unseenItems', () => {
    const now = Date.UTC(2026, 9, 14, 12, 0, 0); // 2026-10-14 12:00 UTC

    it('counts only what was published after the person last looked', () => {
        const items = [
            item({ id: 1, published_at: '2026-10-10T08:00:00' }),
            item({ id: 2, published_at: '2026-10-10T08:00:01' }),
            item({ id: 3, published_at: '2026-10-12T15:00:00.123456' }),
            item({ id: 4, published_at: '2026-10-09T23:59:59' }),
        ];
        assert.deepEqual(
            unseenItems(items, '2026-10-10T08:00:00', now).map(i => i.id),
            [2, 3],
        );
    });

    it('reads the server timestamps as UTC, not local time', () => {
        // 07:00 UTC is before a 10:00 UTC "seen" mark — read as local time in
        // the Americas it would land after it and count as new.
        const items = [item({ id: 1, published_at: '2026-10-10T07:00:00' })];
        assert.deepEqual(unseenItems(items, '2026-10-10T10:00:00Z', now), []);
        // And a mark with an offset is the instant it names (06:00 UTC).
        assert.deepEqual(
            unseenItems(items, '2026-10-10T08:00:00+02:00', now).map(i => i.id),
            [1],
        );
    });

    it('never counts anything older than two weeks', () => {
        assert.equal(UNSEEN_FLOOR_DAYS, 14);
        const items = [
            item({ id: 1, published_at: '2026-09-29T12:00:00' }), // 15 days
            item({ id: 2, published_at: '2026-09-30T12:00:01' }), // just inside 14
            item({ id: 3, published_at: '2026-10-13T09:00:00' }),
        ];
        // Never looked: the floor alone.
        assert.deepEqual(unseenItems(items, null, now).map(i => i.id), [2, 3]);
        // Last looked a month ago: still the floor, not everything since.
        assert.deepEqual(unseenItems(items, '2026-09-01T00:00:00', now).map(i => i.id), [2, 3]);
        // An unreadable mark is as if they never looked.
        assert.deepEqual(unseenItems(items, 'garbage', now).map(i => i.id), [2, 3]);
    });

    it('takes now as a Date as well as epoch ms', () => {
        const items = [item({ id: 1, published_at: '2026-10-13T09:00:00' })];
        assert.equal(unseenItems(items, null, new Date(now)).length, 1);
    });

    it('never counts an item whose time cannot be read, and keeps the order', () => {
        const items = [
            item({ id: 1, published_at: '' }),
            item({ id: 2, published_at: '2026-10-13T09:00:00' }),
            item({ id: 3, published_at: 'not a date' }),
            item({ id: 4, published_at: '2026-10-12T09:00:00' }),
        ];
        assert.deepEqual(unseenItems(items, null, now).map(i => i.id), [2, 4]);
    });
});

describe('newestPublishedAt', () => {
    it('is the newest instant, as the server sent it', () => {
        const items = [
            item({ id: 1, published_at: '2026-10-10T08:00:00' }),
            item({ id: 2, published_at: '2026-10-12T15:00:00.123456' }),
            item({ id: 3, published_at: 'not a date' }),
        ];
        assert.equal(newestPublishedAt(items), '2026-10-12T15:00:00.123456');
    });

    it('is null with nothing readable', () => {
        assert.equal(newestPublishedAt([]), null);
        assert.equal(newestPublishedAt([item({ id: 1, published_at: '' })]), null);
    });
});

describe('shouldTease', () => {
    const now = local(2026, 10, 14, 9);
    const ok: TeaseInput = {
        teaseEnabled: true,
        unseenCount: 2,
        teasedWeek: '2026-W41',
        now,
        pathname: '/',
        openedThisLoad: false,
        drawerOpen: false,
        impersonating: false,
        viewportWidth: 1280,
        pageHidden: false,
        dialogOpen: false,
    };

    it('teases the first visit of a week with something new', () => {
        assert.equal(shouldTease(ok), true);
        assert.equal(shouldTease({ ...ok, teasedWeek: null }), true);
        assert.equal(shouldTease({ ...ok, pathname: '/eos/meetings' }), true);
    });

    it('teases once a week', () => {
        assert.equal(shouldTease({ ...ok, teasedWeek: isoWeekKey(now) }), false);
        assert.equal(shouldTease({ ...ok, teasedWeek: '2026-W42' }), false);
    });

    it('needs the admin switch on and something new', () => {
        assert.equal(shouldTease({ ...ok, teaseEnabled: false }), false);
        assert.equal(shouldTease({ ...ok, unseenCount: 0 }), false);
    });

    it('never slides over the live L10', () => {
        assert.equal(shouldTease({ ...ok, pathname: '/eos/meetings/7f3c' }), false);
    });

    it('leaves a Slack or review link to open the drawer itself', () => {
        assert.equal(shouldTease({ ...ok, pathname: '/whats-new/12' }), false);
        assert.equal(shouldTease({ ...ok, pathname: '/whats-new/rel-12' }), false);
    });

    it('never follows a drawer already opened or open now', () => {
        assert.equal(shouldTease({ ...ok, openedThisLoad: true }), false);
        assert.equal(shouldTease({ ...ok, drawerOpen: true }), false);
    });

    it('never teases an admin viewing as someone else', () => {
        assert.equal(shouldTease({ ...ok, impersonating: true }), false);
    });

    it('needs a window wide enough', () => {
        assert.equal(TEASE_MIN_VIEWPORT, 768);
        assert.equal(shouldTease({ ...ok, viewportWidth: 767 }), false);
        assert.equal(shouldTease({ ...ok, viewportWidth: 768 }), true);
    });

    it('never spends the week where nobody would see it', () => {
        // A background tab: the peek would open and close unseen.
        assert.equal(shouldTease({ ...ok, pageHidden: true }), false);
        // A Modal is open: the peek would slide in under its backdrop.
        assert.equal(shouldTease({ ...ok, dialogOpen: true }), false);
    });
});

describe('isLiveL10Path', () => {
    it('is a meeting session, not the meetings list', () => {
        assert.equal(isLiveL10Path('/eos/meetings/abc-123'), true);
        assert.equal(isLiveL10Path('/eos/meetings'), false);
        assert.equal(isLiveL10Path('/eos/meetings/'), false);
        assert.equal(isLiveL10Path('/eos/meetings/abc/notes'), false);
        assert.equal(isLiveL10Path('/eos/rocks'), false);
    });
});

describe('copy', () => {
    it('announces the tease since the last visit, not "this week"', () => {
        assert.equal(teaseAnnouncement(1), "What's new: 1 update since your last visit");
        assert.equal(teaseAnnouncement(3), "What's new: 3 updates since your last visit");
    });

    it('names the button from the count of new updates', () => {
        assert.equal(whatsNewButtonLabel(0), "What's new");
        assert.equal(whatsNewButtonLabel(3), "What's new, 3 new");
    });
});
