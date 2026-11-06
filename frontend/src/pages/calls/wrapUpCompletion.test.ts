import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { isWrapUpComplete } from './wrapUpCompletion.ts';

const wrapped = { status: 'wrapped', nothing_here: true } as const;

describe('call wrap-up completion', () => {
    it('keeps a wrapped call open while late analysis needs a retry', () => {
        assert.equal(isWrapUpComplete({
            session: wrapped,
            selectable: 0,
            analysisUnavailable: true,
            outcomeRecorded: false,
            unresolved: 0,
        }), false);
    });

    it('recognizes the original completion after late analysis finishes empty', () => {
        assert.equal(isWrapUpComplete({
            session: wrapped,
            selectable: 0,
            analysisUnavailable: false,
            outcomeRecorded: false,
            unresolved: 0,
        }), true);
    });

    it('keeps a failed HubSpot write open for another decision', () => {
        assert.equal(isWrapUpComplete({
            session: { status: 'wrapped', nothing_here: false },
            selectable: 1,
            analysisUnavailable: false,
            outcomeRecorded: false,
            unresolved: 1,
        }), false);
    });
});
