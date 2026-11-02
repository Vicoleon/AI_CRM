// Tests for the Rock carry-over helpers.
//
// Runs on Node's built-in test runner + TypeScript stripping, like
// `noteText.test.ts` — `rockCarryOver.ts` is a leaf module with no imports so
// it loads without a bundler:
//
//   cd <repo>/frontend && node --test "src/components/eos/**/*.test.ts"
//
// What's pinned here is where a carried rock lands by default (the contract's
// 2026-10-09 examples), the Q4 → Q1 year rollover, reading a naive server
// datetime without a timezone shift, and which milestones travel with it.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    carryOverMilestones,
    clearDueDatesBefore,
    compareQuarters,
    defaultCarryTarget,
    nextQuarter,
    quarterLabel,
    quarterOfDate,
    quarterOfISO,
    quarterStartISO,
} from './rockCarryOver.ts';

// Local-time constructor on purpose: "today" is the viewer's calendar day.
const OCT_9_2026 = new Date(2026, 9, 9);

describe('quarterOfISO', () => {
    it('reads the quarter off the string, not through a Date', () => {
        // The naive datetime the API sends for Quarter.start_date. Through
        // `new Date()` a browser east of UTC could read July 1 as June 30.
        assert.deepEqual(quarterOfISO('2026-07-01T00:00:00'), { year: 2026, quarter: 3 });
        assert.deepEqual(quarterOfISO('2026-10-01'), { year: 2026, quarter: 4 });
    });

    it('maps each month boundary to its quarter', () => {
        const cases: [string, number][] = [
            ['2026-01-01', 1],
            ['2026-03-31', 1],
            ['2026-04-01', 2],
            ['2026-06-30', 2],
            ['2026-09-30', 3],
            ['2026-12-31', 4],
        ];
        for (const [iso, quarter] of cases) {
            assert.equal(quarterOfISO(iso)?.quarter, quarter, iso);
        }
    });

    it('returns null for anything that is not a date', () => {
        for (const bad of [null, undefined, '', 'Q3 2026', '2026-13-01']) {
            assert.equal(quarterOfISO(bad), null, String(bad));
        }
    });
});

describe('quarterOfDate', () => {
    it('uses the local calendar day', () => {
        assert.deepEqual(quarterOfDate(OCT_9_2026), { year: 2026, quarter: 4 });
        assert.deepEqual(quarterOfDate(new Date(2027, 0, 1)), { year: 2027, quarter: 1 });
    });
});

describe('nextQuarter', () => {
    it('steps within the year', () => {
        assert.deepEqual(nextQuarter({ year: 2026, quarter: 3 }), { year: 2026, quarter: 4 });
    });

    it('rolls Q4 over into Q1 of the next year', () => {
        assert.deepEqual(nextQuarter({ year: 2026, quarter: 4 }), { year: 2027, quarter: 1 });
    });
});

describe('compareQuarters', () => {
    it('orders by year first, then quarter', () => {
        assert.ok(compareQuarters({ year: 2026, quarter: 4 }, { year: 2027, quarter: 1 }) < 0);
        assert.ok(compareQuarters({ year: 2027, quarter: 1 }, { year: 2026, quarter: 4 }) > 0);
        assert.ok(compareQuarters({ year: 2026, quarter: 2 }, { year: 2026, quarter: 3 }) < 0);
        assert.equal(compareQuarters({ year: 2026, quarter: 3 }, { year: 2026, quarter: 3 }), 0);
    });
});

describe('quarterLabel / quarterStartISO', () => {
    it('labels like the server names the quarters it creates', () => {
        assert.equal(quarterLabel({ year: 2026, quarter: 4 }), 'Q4 2026');
    });

    it('gives each quarter its first day, zero-padded', () => {
        assert.equal(quarterStartISO({ year: 2026, quarter: 1 }), '2026-01-01');
        assert.equal(quarterStartISO({ year: 2026, quarter: 2 }), '2026-04-01');
        assert.equal(quarterStartISO({ year: 2026, quarter: 3 }), '2026-07-01');
        assert.equal(quarterStartISO({ year: 2026, quarter: 4 }), '2026-10-01');
    });
});

describe('defaultCarryTarget', () => {
    // The contract's examples, with today = 2026-10-09 (Q4 2026).
    it('carries last quarter\'s rock into the current quarter', () => {
        assert.deepEqual(defaultCarryTarget('2026-07-01T00:00:00', OCT_9_2026), {
            year: 2026,
            quarter: 4,
        });
    });

    it('carries a current-quarter rock into next quarter, across the year end', () => {
        assert.deepEqual(defaultCarryTarget('2026-10-01T00:00:00', OCT_9_2026), {
            year: 2027,
            quarter: 1,
        });
    });

    it('never lands an old rock in a quarter that has already ended', () => {
        // Q2 2026 → Q4 2026, not Q3 2026.
        assert.deepEqual(defaultCarryTarget('2026-04-01T00:00:00', OCT_9_2026), {
            year: 2026,
            quarter: 4,
        });
    });

    it('carries a future-quarter rock into the quarter after it', () => {
        assert.deepEqual(defaultCarryTarget('2027-01-01', OCT_9_2026), {
            year: 2027,
            quarter: 2,
        });
    });

    it('falls back to the quarter after today when the source is unknown', () => {
        assert.deepEqual(defaultCarryTarget(null, OCT_9_2026), { year: 2027, quarter: 1 });
        assert.deepEqual(defaultCarryTarget(undefined, new Date(2026, 4, 15)), {
            year: 2026,
            quarter: 3,
        });
    });
});

describe('carryOverMilestones', () => {
    const TARGET = '2026-10-01';

    it('takes only the unfinished milestones, as brand-new rows', () => {
        const carried = carryOverMilestones(
            [
                { id: 'a', sort_order: 0, description: 'Spec signed', completed: true },
                { id: 'b', sort_order: 1, description: 'Pilot with Sales', completed: false },
            ] as { id: string; sort_order: number; description: string; completed: boolean }[],
            TARGET,
        );
        assert.deepEqual(carried, [
            { description: 'Pilot with Sales', completed: false, due_date: null },
        ]);
        // No id / sort_order: the server must insert, never re-attach.
        assert.equal('id' in carried[0], false);
        assert.equal('sort_order' in carried[0], false);
    });

    it('keeps a due date on or after the target quarter\'s first day', () => {
        const carried = carryOverMilestones(
            [
                { description: 'On the first day', completed: false, due_date: '2026-10-01' },
                { description: 'Later', completed: false, due_date: '2026-11-15' },
            ],
            TARGET,
        );
        assert.deepEqual(
            carried.map(m => m.due_date),
            ['2026-10-01', '2026-11-15'],
        );
    });

    it('clears a due date from the old quarter', () => {
        const carried = carryOverMilestones(
            [
                { description: 'Old quarter', completed: false, due_date: '2026-09-30' },
                { description: 'No date', completed: false, due_date: null },
                { description: 'Missing date', completed: false },
            ],
            TARGET,
        );
        assert.deepEqual(
            carried.map(m => m.due_date),
            [null, null, null],
        );
    });

    it('compares across the year end', () => {
        const carried = carryOverMilestones(
            [
                { description: 'December', completed: false, due_date: '2026-12-15' },
                { description: 'February', completed: false, due_date: '2027-02-01' },
            ],
            quarterStartISO({ year: 2027, quarter: 1 }),
        );
        assert.deepEqual(
            carried.map(m => m.due_date),
            [null, '2027-02-01'],
        );
    });

    it('handles a rock with no milestones', () => {
        assert.deepEqual(carryOverMilestones(null, TARGET), []);
        assert.deepEqual(carryOverMilestones(undefined, TARGET), []);
        assert.deepEqual(
            carryOverMilestones([{ description: 'Done', completed: true }], TARGET),
            [],
        );
    });
});

describe('clearDueDatesBefore', () => {
    it('clears dates before the newly picked quarter and keeps the rest', () => {
        // Carried into Q4 2026 first, then the user picks Q1 2027 instead.
        const draft = [
            { description: 'November', completed: false, due_date: '2026-11-20' },
            { description: 'January', completed: false, due_date: '2027-01-15' },
            { description: 'Undated', completed: false, due_date: null },
        ];
        const moved = clearDueDatesBefore(draft, quarterStartISO({ year: 2027, quarter: 1 }));
        assert.deepEqual(
            moved.map(m => [m.description, m.due_date]),
            [
                ['November', null],
                ['January', '2027-01-15'],
                ['Undated', null],
            ],
        );
    });

    it('keeps a date on the quarter\'s first day, and never mutates its input', () => {
        const draft = [{ description: 'Kick-off', completed: false, due_date: '2027-01-01' }];
        const moved = clearDueDatesBefore(draft, '2027-01-01');
        assert.equal(moved[0].due_date, '2027-01-01');
        const cleared = clearDueDatesBefore(draft, '2027-04-01');
        assert.equal(cleared[0].due_date, null);
        assert.equal(draft[0].due_date, '2027-01-01');
    });
});
