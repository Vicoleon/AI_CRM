// Regression tests for the Smart Teams interview's decisions.
//
// The repo has NO frontend component test runner (no vitest, no jest, no
// testing-library, no `test` script). Rather than add one for this file, it runs
// on Node's built-in test runner against Node's built-in TypeScript stripping,
// which is why `interviewFlow.ts` is a leaf module with type-only imports:
//
//   cd <repo>/frontend && node --test "src/components/smartTeams/**/*.test.ts"
//
// (The bare directory form works on Node 22 but not Node 24 — pass the glob.)
//
// It is also covered by `npx tsc -b`, so it cannot rot silently.
//
// What is pinned here is what the interview turns on: a resume must land on the
// item they stopped at, a cleared answer must not count as answered, and the
// outbox must never replay one person's retake over another interview's answers.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import type { AnswerValue } from '../../api/smartTeams';
import {
    PAGE_SIZE,
    answeredCount,
    clampPage,
    digitToScaleValue,
    firstUnansweredIndex,
    formatMinutes,
    isAnswered,
    keyToChoice,
    mergeOutbox,
    outboxAppliesTo,
    outboxStorageKey,
    pageCount,
    pageForIndex,
    pageSlice,
    resumePage,
    readOutbox,
    retireFromOutbox,
    saveStateLabel,
    unansweredOnPage,
    writeOutbox,
} from './interviewFlow.ts';

const ids = (n: number, prefix = 'itm'): string[] =>
    Array.from({ length: n }, (_, i) => `${prefix}_${String(i + 1).padStart(2, '0')}`);

const answerAll = (keys: string[], value: AnswerValue = 3): Record<string, AnswerValue> =>
    Object.fromEntries(keys.map(k => [k, value]));

describe('paging — a 50-item bank must never be a wall or a dead end', () => {
    it('always offers at least one page, even for an empty bank', () => {
        assert.equal(pageCount(0), 1);
        assert.equal(pageCount(-4), 1);
    });

    it('splits the Big Five bank into whole pages with a short last one', () => {
        assert.equal(pageCount(50), 5);
        assert.equal(pageCount(27), 3);
        assert.equal(pageSlice(ids(27), 3).length, 7);
    });

    it('clamps a hand-edited ?page= instead of rendering nothing', () => {
        assert.equal(clampPage('99', 27), 3);
        assert.equal(clampPage('0', 27), 1);
        assert.equal(clampPage('-3', 27), 1);
        assert.equal(clampPage('two', 27), 1);
        assert.equal(clampPage(undefined, 27), 1);
        assert.equal(clampPage('2', 27), 2);
    });

    it('maps an item index onto its 1-based page', () => {
        assert.equal(pageForIndex(0), 1);
        assert.equal(pageForIndex(PAGE_SIZE - 1), 1);
        assert.equal(pageForIndex(PAGE_SIZE), 2);
        assert.equal(pageForIndex(49), 5);
    });
});

describe('answered-ness — a cleared answer is not an answer', () => {
    it('treats null as unanswered, matching validate_answers', () => {
        // interview.validate_answers accepts null as "asked and cleared", and
        // the Python progress calculation counts only non-null values. Counting
        // it here would show a full bank the person cannot submit.
        assert.equal(isAnswered(null), false);
        assert.equal(isAnswered(undefined), false);
        assert.equal(isAnswered(0), true);
        assert.equal(isAnswered('a'), true);
    });

    it('counts a value of 0 — a scale could legitimately start there', () => {
        const items = ids(3);
        assert.equal(answeredCount(items, { [items[0]]: 0, [items[1]]: null }), 1);
    });

    it('reports the unanswered ids on a page, in order', () => {
        const items = ids(4);
        const answers: Record<string, AnswerValue> = { [items[0]]: 2, [items[2]]: null };
        assert.deepEqual(unansweredOnPage(items, answers), [items[1], items[2], items[3]]);
    });
});

describe('resume — land where they stopped, not at the top', () => {
    it('finds the first gap, not the last answer', () => {
        const items = ids(30);
        const answers = answerAll(items.slice(0, 12));
        // Someone who skipped item 5 and carried on resumes at item 5.
        answers[items[4]] = null;
        assert.equal(firstUnansweredIndex(items, answers), 4);
        assert.equal(resumePage(items, answers), 1);
    });

    it('opens the page holding the first unanswered item', () => {
        const items = ids(50);
        assert.equal(resumePage(items, answerAll(items.slice(0, 23))), 3);
    });

    it('opens a fresh bank at page 1', () => {
        assert.equal(resumePage(ids(50), {}), 1);
    });

    it('opens a finished bank on its LAST page, where Continue is', () => {
        const items = ids(50);
        assert.equal(firstUnansweredIndex(items, answerAll(items)), null);
        assert.equal(resumePage(items, answerAll(items)), 5);
    });
});

describe('keyboard — digits are the accelerator, bounds come from the section', () => {
    it('accepts a digit inside the scale and rejects one outside it', () => {
        assert.equal(digitToScaleValue('3', 1, 5), 3);
        assert.equal(digitToScaleValue('5', 1, 5), 5);
        assert.equal(digitToScaleValue('6', 1, 5), null);
        assert.equal(digitToScaleValue('0', 1, 5), null);
    });

    it('honours a scale that is not 1-5', () => {
        // The anchor wording and the bounds differ per layer; nothing here may
        // assume five points.
        assert.equal(digitToScaleValue('0', 0, 4), 0);
        assert.equal(digitToScaleValue('7', 1, 7), 7);
    });

    it('ignores letters, modifiers and chords', () => {
        assert.equal(digitToScaleValue('a', 1, 5), null);
        assert.equal(digitToScaleValue('Enter', 1, 5), null);
        assert.equal(digitToScaleValue('', 1, 5), null);
    });

    it('maps A/B and 1/2 onto a forced choice', () => {
        assert.equal(keyToChoice('a'), 'a');
        assert.equal(keyToChoice('B'), 'b');
        assert.equal(keyToChoice('1'), 'a');
        assert.equal(keyToChoice('2'), 'b');
        assert.equal(keyToChoice('3'), null);
        assert.equal(keyToChoice('Tab'), null);
    });
});

describe('formatting — round up, and never invent precision', () => {
    it('rounds minutes up so the hint under-promises', () => {
        assert.equal(formatMinutes(61), 'about 2 minutes');
        assert.equal(formatMinutes(30), 'about a minute');
        assert.equal(formatMinutes(1988), 'about 34 minutes');
    });

    it('handles zero, negatives and nonsense without printing NaN', () => {
        assert.equal(formatMinutes(0), 'less than a minute');
        assert.equal(formatMinutes(-10), 'less than a minute');
        assert.equal(formatMinutes(Number.NaN), 'less than a minute');
    });

    it('says something sane past an hour', () => {
        assert.equal(formatMinutes(3600), 'about an hour');
        assert.equal(formatMinutes(3600 + 300), 'about an hour 5 min');
    });
});

describe('save indicator — a failed save must be visible and countable', () => {
    it('names how many answers are stranded', () => {
        assert.equal(saveStateLabel('failed', 1), "1 answer hasn't saved");
        assert.equal(saveStateLabel('failed', 7), "7 answers haven't saved");
    });

    it('never claims Saved while something is pending', () => {
        assert.equal(saveStateLabel('idle', 3), 'Not saved yet');
        assert.equal(saveStateLabel('idle', 0), 'Saved');
        assert.equal(saveStateLabel('saving', 3), 'Saving…');
    });
});

describe('outbox — the crash log for a session that dies mid-bank', () => {
    it('keeps workspace and user outboxes separate in the same browser', () => {
        const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
        const values = new Map<string, string>();
        Object.defineProperty(globalThis, 'localStorage', {
            configurable: true,
            value: {
                getItem: (key: string) => values.get(key) ?? null,
                setItem: (key: string, value: string) => { values.set(key, value); },
                removeItem: (key: string) => { values.delete(key); },
            },
        });
        try {
            const alpha = { workspaceSlug: 'alpha', userEmail: 'Same@Example.com' };
            const beta = { workspaceSlug: 'beta', userEmail: 'same@example.com' };
            const other = { workspaceSlug: 'alpha', userEmail: 'other@example.com' };
            assert.notEqual(outboxStorageKey(alpha), outboxStorageKey(beta));
            assert.notEqual(outboxStorageKey(alpha), outboxStorageKey(other));
            assert.equal(outboxStorageKey(null), null);
            values.set('cubie_st_interview_outbox', JSON.stringify(mergeOutbox(null, 'old', 'big_five', { a: 1 })));
            assert.equal(readOutbox(alpha), null);
            writeOutbox(mergeOutbox(null, 'iv-alpha', 'big_five', { a: 3 }), alpha);
            assert.equal(readOutbox(beta), null);
            writeOutbox(mergeOutbox(null, 'iv-beta', 'big_five', { a: 5 }), beta);
            assert.equal(readOutbox(alpha)?.interview_id, 'iv-alpha');
            assert.equal(readOutbox(beta)?.interview_id, 'iv-beta');
            writeOutbox(null, beta);
            assert.equal(readOutbox(alpha)?.interview_id, 'iv-alpha');
            assert.equal(readOutbox(beta), null);
        } finally {
            if (previous) Object.defineProperty(globalThis, 'localStorage', previous);
            else Reflect.deleteProperty(globalThis, 'localStorage');
        }
    });

    it('accumulates answers for the same interview', () => {
        const first = mergeOutbox(null, 'iv-1', 'big_five', { a: 3 });
        const second = mergeOutbox(first, 'iv-1', 'big_five', { b: 4 });
        assert.deepEqual(second.answers, { a: 3, b: 4 });
        assert.equal(second.interview_id, 'iv-1');
    });

    it('lets a later value win for the same key', () => {
        const first = mergeOutbox(null, 'iv-1', 'big_five', { a: 3 });
        assert.deepEqual(mergeOutbox(first, 'iv-1', 'big_five', { a: 5 }).answers, { a: 5 });
    });

    it('DISCARDS an outbox from a different interview', () => {
        // A retake reuses the same item ids. Merging the old row's leftovers
        // into the new interview would import answers the person did not give
        // this time — the one bug this whole mechanism could cause.
        const stale = mergeOutbox(null, 'iv-1', 'big_five', { a: 1, b: 2 });
        const fresh = mergeOutbox(stale, 'iv-2', 'big_five', { a: 5 });
        assert.deepEqual(fresh.answers, { a: 5 });
        assert.equal(fresh.interview_id, 'iv-2');
    });

    it('moves the cursor with the answers, so a dead session resumes in place', () => {
        const first = mergeOutbox(null, 'iv-1', 'disc_likert', { a: 3 });
        assert.equal(mergeOutbox(first, 'iv-1', 'big_five', { b: 3 }).section_cursor, 'big_five');
    });

    it('retires only the keys the server confirmed', () => {
        const outbox = mergeOutbox(null, 'iv-1', 'big_five', { a: 3, b: 4, c: 5 });
        const left = retireFromOutbox(outbox, ['a', 'c']);
        assert.deepEqual(left?.answers, { b: 4 });
    });

    it('clears itself entirely once nothing is left', () => {
        // An empty outbox left on disk reads as "a save failed" next mount.
        const outbox = mergeOutbox(null, 'iv-1', 'big_five', { a: 3 });
        assert.equal(retireFromOutbox(outbox, ['a']), null);
        assert.equal(retireFromOutbox(null, ['a']), null);
    });

    it('replays only a non-empty outbox for the interview in hand', () => {
        const outbox = mergeOutbox(null, 'iv-1', 'big_five', { a: 3 });
        assert.equal(outboxAppliesTo(outbox, 'iv-1'), true);
        assert.equal(outboxAppliesTo(outbox, 'iv-2'), false);
        assert.equal(outboxAppliesTo(outbox, null), false);
        assert.equal(outboxAppliesTo(null, 'iv-1'), false);
        assert.equal(outboxAppliesTo({ ...outbox, answers: {} }, 'iv-1'), false);
    });
});
