// The shared stack of open modal dialogs (Modal, ConfirmDialog, the What's
// New drawer's full mode): only the topmost one answers Escape and Tab.
//
//   cd <repo>/frontend && node --test "src/components/eos/dialogStack.test.ts"
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { describe, it } from 'node:test';

import { isTopmost, newDialogId, openDialogCount, pop, push } from './dialogStack.ts';

describe('dialogStack', () => {
    it('makes the last dialog opened the topmost', () => {
        const modal = newDialogId();
        const confirm = newDialogId();
        assert.notEqual(modal, confirm);
        assert.equal(isTopmost(modal), false);
        push(modal);
        assert.equal(isTopmost(modal), true);
        push(confirm);
        assert.equal(isTopmost(modal), false);
        assert.equal(isTopmost(confirm), true);
        pop(confirm);
        assert.equal(isTopmost(modal), true);
        pop(modal);
        assert.equal(openDialogCount(), 0);
    });

    it('lets a dialog underneath close first', () => {
        push('whats-new');
        const modal = newDialogId();
        push(modal);
        pop('whats-new');
        assert.equal(isTopmost(modal), true);
        assert.equal(openDialogCount(), 1);
        pop(modal);
    });

    it('ignores closing a dialog that never opened', () => {
        pop('nothing');
        assert.equal(openDialogCount(), 0);
        assert.equal(isTopmost('nothing'), false);
    });

    it('is the stack Modal uses', () => {
        const modal = fs.readFileSync(new URL('./Modal.tsx', import.meta.url), 'utf8');
        assert.match(modal, /from '\.\/dialogStack'/);
        assert.match(modal, /pushDialog\(id\)/);
        assert.match(modal, /popDialog\(id\)/);
        assert.doesNotMatch(modal, /_modalStack/);
    });
});
