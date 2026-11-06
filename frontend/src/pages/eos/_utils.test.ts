// Tests for the pure EOS helpers that carry a UAT decision in them.
//
// Runs on Node's built-in test runner + TypeScript stripping — `_utils.ts` has
// no imports, so it loads without a bundler:
//
//   cd <repo>/frontend && node --test "src/pages/eos/**/*.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { canRestartMeeting, partitionL10Inputs } from './_utils.ts';

const row = (id: string, created_at: string, discussed = false) => ({
    id,
    created_at,
    discussed,
});

describe('partitionL10Inputs', () => {
    it('puts the newest headline at the top', () => {
        const items = [
            row('oldest', '2026-09-01T09:00:00Z'),
            row('middle', '2026-09-01T09:30:00Z'),
            row('newest', '2026-09-01T09:45:00Z'),
        ];
        assert.deepEqual(
            partitionL10Inputs(items).pending.map(i => i.id),
            ['newest', 'middle', 'oldest'],
        );
    });

    it('lifts a checked-off item out of the live list', () => {
        const items = [
            row('covered', '2026-09-01T09:00:00Z', true),
            row('open', '2026-09-01T08:00:00Z'),
        ];
        const { pending, covered } = partitionL10Inputs(items);
        assert.deepEqual(pending.map(i => i.id), ['open']);
        assert.deepEqual(covered.map(i => i.id), ['covered']);
    });

    it('keeps covered items newest-first too', () => {
        const items = [
            row('a', '2026-09-01T08:00:00Z', true),
            row('b', '2026-09-01T10:00:00Z', true),
        ];
        assert.deepEqual(
            partitionL10Inputs(items).covered.map(i => i.id),
            ['b', 'a'],
        );
    });

    it('never loses a row', () => {
        const items = [
            row('a', '2026-09-01T08:00:00Z'),
            row('b', '2026-09-01T09:00:00Z', true),
            row('c', '2026-09-01T10:00:00Z'),
        ];
        const { pending, covered } = partitionL10Inputs(items);
        assert.equal(pending.length + covered.length, items.length);
    });

    it('does not mutate the caller\'s array', () => {
        const items = [
            row('a', '2026-09-01T08:00:00Z'),
            row('b', '2026-09-01T10:00:00Z'),
        ];
        partitionL10Inputs(items);
        assert.deepEqual(items.map(i => i.id), ['a', 'b']);
    });

    it('keeps rows with an unusable timestamp in their original order', () => {
        const items = [
            row('first', 'not-a-date'),
            row('second', 'also-not-a-date'),
        ];
        assert.deepEqual(
            partitionL10Inputs(items).pending.map(i => i.id),
            ['first', 'second'],
        );
    });

    it('handles an empty queue', () => {
        assert.deepEqual(partitionL10Inputs([]), { pending: [], covered: [] });
    });
});

describe('canRestartMeeting', () => {
    const team = { is_exec_meeting: false };
    const exec = { is_exec_meeting: true };

    it('offers Restart to someone with the feature who can run the meeting', () => {
        assert.equal(canRestartMeeting(team, { mayRunMeetings: true, restartFeature: true }), true);
    });

    it('judges the person, not the meeting\'s team', () => {
        // Any team's meeting: there is no list of teams to be on any more.
        assert.equal(
            canRestartMeeting({ is_exec_meeting: false, department_name: 'Sales' }, {
                mayRunMeetings: true,
                restartFeature: true,
            }),
            true,
        );
    });

    it('hides it without the feature', () => {
        assert.equal(canRestartMeeting(team, { mayRunMeetings: true, restartFeature: false }), false);
    });

    it('hides it from someone who cannot run meetings', () => {
        assert.equal(canRestartMeeting(team, { mayRunMeetings: false, restartFeature: true }), false);
    });

    it('never offers it on the Exec team\'s L10', () => {
        assert.equal(canRestartMeeting(exec, { mayRunMeetings: true, restartFeature: true }), false);
    });
});
