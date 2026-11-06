// Regression tests for the Chat empty-state starter prompts.
//
// Runs on Node's built-in test runner against Node's built-in TypeScript
// stripping (`npm test`; there is no frontend test framework),
// which is why chatSuggestions.ts is a leaf module with type-only imports:
//
//   cd <repo>/frontend && node --test "src/pages/chatSuggestions.test.ts"
//
// What is pinned here:
//
//   * The per-feature cap. "Up to 5 per feature" is the rule that keeps one
//     area from swamping the tour as the catalogue grows.
//   * The gate is fail-closed. A prompt for a feature the reader cannot reach
//     sends them into a 403 or an "I can't do that", so an absent verdict must
//     hide the group — never show it.
//   * Breadth before depth. Six cards must be six different parts of Cubie
//     while six groups are still available; a picker that took the easy path
//     and shuffled a flat list would pass everything else here and fail this.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    MAX_PROMPTS_PER_GROUP,
    SUGGESTION_CATALOG,
    VISIBLE_PROMPT_COUNT,
    pickSuggestedPrompts,
    seededRng,
    type SuggestionGate,
    type SuggestionGroup,
} from './chatSuggestions.ts';

const allowAll = () => true;
const gated = (...keys: SuggestionGate[]) => (k: SuggestionGate) => keys.includes(k);

describe('the catalogue', () => {
    it('gives every feature at least one and at most five prompts', () => {
        for (const group of SUGGESTION_CATALOG) {
            assert.ok(group.prompts.length > 0, `${group.id} has no prompts`);
            assert.ok(
                group.prompts.length <= MAX_PROMPTS_PER_GROUP,
                `${group.id} has ${group.prompts.length} prompts, over the cap of ${MAX_PROMPTS_PER_GROUP}`,
            );
        }
    });

    it('has unique group ids and no repeated prompt anywhere', () => {
        const ids = SUGGESTION_CATALOG.map(g => g.id);
        assert.equal(new Set(ids).size, ids.length, 'duplicate group id');

        const texts = SUGGESTION_CATALOG.flatMap(g => g.prompts);
        const seen = new Set<string>();
        for (const text of texts) {
            assert.ok(!seen.has(text), `prompt appears twice: ${text}`);
            seen.add(text);
        }
    });

    it('phrases every prompt as a complete, ready-to-send question', () => {
        // Clicking a card sends it immediately — there is no edit step — so a
        // fragment or a placeholder ("ask about [topic]") goes straight to the
        // model as-is.
        for (const group of SUGGESTION_CATALOG) {
            for (const text of group.prompts) {
                assert.equal(text.trim(), text, `untrimmed prompt in ${group.id}: ${text}`);
                assert.ok(text.length > 12, `prompt too short to be a question in ${group.id}: ${text}`);
                assert.match(text, /[.?]$/, `prompt is not a full sentence in ${group.id}: ${text}`);
                assert.doesNotMatch(text, /[[<{]/, `prompt has a placeholder in ${group.id}: ${text}`);
            }
        }
    });

    it('offers enough ungated breadth to fill the empty state', () => {
        // A brand-new user with no extra features must still get a full,
        // varied set of cards — otherwise the tour is half a screen of blanks.
        const ungated = SUGGESTION_CATALOG.filter(g => !g.requires);
        assert.ok(
            ungated.length >= VISIBLE_PROMPT_COUNT,
            `only ${ungated.length} ungated groups for ${VISIBLE_PROMPT_COUNT} cards`,
        );
    });
});

describe('pickSuggestedPrompts', () => {
    it('returns the requested number of prompts', () => {
        const picked = pickSuggestedPrompts({ canUse: allowAll, rng: seededRng(1) });
        assert.equal(picked.length, VISIBLE_PROMPT_COUNT);
    });

    it('never repeats a prompt in one draw', () => {
        for (let seed = 1; seed <= 25; seed++) {
            const texts = pickSuggestedPrompts({
                count: 8,
                canUse: allowAll,
                rng: seededRng(seed),
            }).map(p => p.text);
            assert.equal(new Set(texts).size, texts.length, `duplicate at seed ${seed}`);
        }
    });

    it('spreads across features before taking a second from any one', () => {
        for (let seed = 1; seed <= 25; seed++) {
            const picked = pickSuggestedPrompts({ canUse: allowAll, rng: seededRng(seed) });
            const groups = new Set(picked.map(p => p.groupId));
            assert.equal(
                groups.size,
                picked.length,
                `seed ${seed} drew two prompts from one feature: ${picked.map(p => p.groupId).join(', ')}`,
            );
        }
    });

    it('draws a different set across reloads', () => {
        // The whole point of the change: the same four cards forever taught
        // people that Cubie does four things.
        const first = pickSuggestedPrompts({ canUse: allowAll, rng: seededRng(1) });
        const second = pickSuggestedPrompts({ canUse: allowAll, rng: seededRng(99) });
        assert.notDeepEqual(
            first.map(p => p.text),
            second.map(p => p.text),
        );
    });

    it('hides a gated feature the reader cannot reach', () => {
        const picked = pickSuggestedPrompts({
            count: 100,
            canUse: gated('scheduled_tasks'),
            rng: seededRng(7),
        });
        const groups = new Set(picked.map(p => p.groupId));
        assert.ok(groups.has('scheduled-tasks'), 'granted feature was left out');
        assert.ok(!groups.has('smart-teams'), 'suggested a feature the reader cannot reach');
    });

    it('shows only ungated groups when no verdicts have arrived', () => {
        // AuthContext answers false for every key while /api/users/me is in
        // flight. A missing verdict must hide a surface, never reveal one.
        const picked = pickSuggestedPrompts({ count: 100, rng: seededRng(3) });
        const gatedIds = new Set(SUGGESTION_CATALOG.filter(g => g.requires).map(g => g.id));
        for (const p of picked) {
            assert.ok(!gatedIds.has(p.groupId), `leaked gated group ${p.groupId}`);
        }
    });

    it('falls back to a second prompt per group rather than returning short', () => {
        const catalog: SuggestionGroup[] = [
            { id: 'a', icon: 'email', prompts: ['One question here?', 'Two question here?'] },
            { id: 'b', icon: 'crm', prompts: ['Three question here?', 'Four question here?'] },
        ];
        const picked = pickSuggestedPrompts({ count: 4, catalog, rng: seededRng(5) });
        assert.equal(picked.length, 4);
        assert.equal(new Set(picked.map(p => p.text)).size, 4);
        // Round-robin: both groups appear before either repeats.
        assert.deepEqual(new Set(picked.slice(0, 2).map(p => p.groupId)), new Set(['a', 'b']));
    });

    it('returns everything available rather than padding when the catalogue runs out', () => {
        const catalog: SuggestionGroup[] = [
            { id: 'a', icon: 'email', prompts: ['Only one question here?'] },
        ];
        const picked = pickSuggestedPrompts({ count: 6, catalog, rng: seededRng(2) });
        assert.equal(picked.length, 1);
    });

    it('leaves the module catalogue in its declared order', () => {
        // shuffled() copies; a picker that sorted in place would scramble the
        // shared const for every later caller in the same page load.
        const before = SUGGESTION_CATALOG.map(g => g.id);
        pickSuggestedPrompts({ canUse: allowAll, rng: seededRng(11) });
        assert.deepEqual(SUGGESTION_CATALOG.map(g => g.id), before);
    });

    it('handles a zero or negative count without throwing', () => {
        assert.deepEqual(pickSuggestedPrompts({ count: 0, canUse: allowAll }), []);
        assert.deepEqual(pickSuggestedPrompts({ count: -3, canUse: allowAll }), []);
    });
});

describe('seededRng', () => {
    it('gives the same draw for the same seed and a different one for the next', () => {
        // The component holds a seed in state precisely so a re-render does
        // not re-deal the cards. If equal seeds ever stopped agreeing, the
        // prompts would shuffle under the reader's cursor.
        const draw = (seed: number) =>
            pickSuggestedPrompts({ canUse: allowAll, rng: seededRng(seed) }).map(p => p.text);

        assert.deepEqual(draw(4242), draw(4242));
        assert.notDeepEqual(draw(4242), draw(4243));
    });

    it('still shuffles when handed the degenerate seed 0', () => {
        // 0 is xorshift's fixed point; unguarded it returns 0 forever, which
        // is a Fisher-Yates that reverses the array the same way every time.
        const rng = seededRng(0);
        const values = Array.from({ length: 5 }, () => rng());
        assert.equal(new Set(values).size, values.length, 'seed 0 produced a constant stream');
        for (const v of values) {
            assert.ok(v >= 0 && v < 1, `out of range: ${v}`);
        }
    });

    it('stays inside [0, 1) across many draws', () => {
        const rng = seededRng(20260910);
        for (let i = 0; i < 2000; i++) {
            const v = rng();
            assert.ok(v >= 0 && v < 1, `out of range at draw ${i}: ${v}`);
        }
    });
});
