// Tests for the Proposal page's origin-aware brief copy.
//
//   cd <repo>/frontend && node --test src/components/proposals/briefCopy.test.ts
//
// WHAT THESE GUARD
// ────────────────
//   * A CALL THAT IS GONE. A proposal with no call session has no call to
//     quote, so none of its brief copy may say Cubie heard one.
//   * CALL COPY DRIFTING. Proposals drafted from a call keep the sentences
//     they shipped with, word for word.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { briefCopy, proposalOrigin, type BriefCopy } from './briefCopy.ts';

const all = (c: BriefCopy): string[] => Object.values(c);

describe('proposalOrigin', () => {
    it('reads a call session as a call', () => {
        assert.equal(proposalOrigin({ call_session_id: 's1' }), 'call');
    });

    it('falls back to neutral with no call session', () => {
        assert.equal(proposalOrigin({ call_session_id: null }), 'other');
        assert.equal(proposalOrigin({}), 'other');
        assert.equal(proposalOrigin(null), 'other');
        assert.equal(proposalOrigin(undefined), 'other');
    });
});

describe('briefCopy', () => {
    it('keeps the call copy exactly as it shipped', () => {
        const c = briefCopy('call');
        assert.equal(
            c.intro,
            'What Cubie heard on the call. Fix anything wrong, fill anything missing, then confirm. The document is built only from this.',
        );
        assert.equal(c.scopeHint, 'Only what was discussed on the call. Cubie will not add to this list.');
        assert.equal(c.scopeUnconfirmed, 'Not heard on the call — added by hand or inferred');
        assert.equal(c.outcomeAdded, 'Added by you — the prospect did not say this on the call');
        assert.equal(c.pricingHint, 'Only figures that were said. The table in the document prints exactly these.');
        assert.equal(c.priceMissing, 'No price was said on the call — fill it in or remove the line');
        assert.equal(c.openQuestionsTitle, 'Not settled on the call — confirm before sending');
    });

    it('never mentions a call, or what the app heard, for a proposal with no call', () => {
        for (const line of all(briefCopy('other'))) {
            assert.doesNotMatch(line, /\bcall\b/i, line);
            assert.doesNotMatch(line, /\bheard\b|\bsaid\b|\bdiscussed\b/i, line);
        }
    });

    it('fills every sentence for every origin', () => {
        for (const origin of ['call', 'other'] as const) {
            for (const line of all(briefCopy(origin))) assert.ok(line.trim().length > 0, origin);
        }
    });
});
