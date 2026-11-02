// The What's New drawer's open/peek state. The store is module-level (one per
// page load), so these steps run in order over the same store, like a page
// load would.
//
//   cd <repo>/frontend && node --test "src/components/whatsNew/store.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    closeWhatsNew,
    getWhatsNewDrawerState,
    openWhatsNew,
    promoteToFull,
    teaseWhatsNew,
} from './store.ts';

describe('one page load', () => {
    it('starts closed, never opened', () => {
        const s = getWhatsNewDrawerState();
        assert.equal(s.open, false);
        assert.equal(s.openedThisLoad, false);
    });

    it('teases once: a peek, not the full drawer', () => {
        assert.equal(teaseWhatsNew(), true);
        const s = getWhatsNewDrawerState();
        assert.equal(s.open, true);
        assert.equal(s.mode, 'peek');
        assert.equal(s.openedThisLoad, true);
        assert.equal(teaseWhatsNew(), false);
    });

    it('becomes the full drawer when the person reaches for it', () => {
        promoteToFull();
        assert.equal(getWhatsNewDrawerState().mode, 'full');
        assert.equal(getWhatsNewDrawerState().open, true);
    });

    it('closes, keeping its content for the closing animation', () => {
        closeWhatsNew();
        const s = getWhatsNewDrawerState();
        assert.equal(s.open, false);
        assert.equal(s.mode, 'full');
    });

    it('never teases again in the same page load', () => {
        assert.equal(teaseWhatsNew(), false);
        assert.equal(getWhatsNewDrawerState().open, false);
    });

    it('opens on a note and a source, with a notice', () => {
        openWhatsNew({ itemId: 'rel-7', from: 'slack', notice: 'This update is no longer available.' });
        assert.deepEqual(getWhatsNewDrawerState(), {
            open: true,
            mode: 'full',
            itemId: 'rel-7',
            from: 'slack',
            notice: 'This update is no longer available.',
            openedThisLoad: true,
        });
    });

    it('replaces the last open entirely on the next open', () => {
        openWhatsNew({ from: 'button' });
        const s = getWhatsNewDrawerState();
        assert.equal(s.itemId, null);
        assert.equal(s.notice, null);
        assert.equal(s.from, 'button');
    });

    it('does not tease over a drawer that is open', () => {
        assert.equal(teaseWhatsNew(), false);
        assert.equal(getWhatsNewDrawerState().mode, 'full');
        promoteToFull();
        assert.equal(getWhatsNewDrawerState().mode, 'full');
        closeWhatsNew();
    });
});
