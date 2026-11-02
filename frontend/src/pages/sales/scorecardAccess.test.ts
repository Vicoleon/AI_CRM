import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { scorecardControls } from './scorecardAccess.ts';

const scorecard = {
    metric_name: 'Loose ends over 7 days',
    metrics_found: 1,
    people_without_metric: 2,
    can_setup: true,
};

describe('Loose Ends Team scorecard controls', () => {
    it('hides both the card and setup action when EOS is off, even with stale scorecard data', () => {
        assert.deepEqual(scorecardControls(false, scorecard), { scorecard: null, canSetup: false });
    });

    it('shows the card and eligible setup action when EOS is on', () => {
        assert.deepEqual(scorecardControls(true, scorecard), { scorecard, canSetup: true });
        assert.deepEqual(scorecardControls(true, { ...scorecard, people_without_metric: 0 }), {
            scorecard: { ...scorecard, people_without_metric: 0 },
            canSetup: false,
        });
    });

    it('keeps the card and action hidden when the server has no scorecard', () => {
        assert.deepEqual(scorecardControls(true, null), { scorecard: null, canSetup: false });
        assert.deepEqual(scorecardControls(false, undefined), { scorecard: null, canSetup: false });
    });
});
