// Drift tests for the sales daily rhythm (My Day's Daily rhythm section and
// the Ask Cubie rhythm prompts). The server files are read as text, the same
// way cockpit.test.ts pins the cockpit's vocabulary:
//
//   cd <repo>/frontend && node --test "src/api/salesRhythm.test.ts"
//
// Pinned here:
//
//   * The action kinds the router emits are exactly the ones the client knows,
//     and DailyRhythm.tsx handles each one case by case.
//   * Every reason code the server can send has its own sentence.
//   * Every block's starter wording (`ask_prompt`) and the end-of-day recap
//     are, verbatim, starter prompts in chatSuggestions.ts, and where the
//     checklist wording caps the list ("which 3", "find 5") the "+N more"
//     question (`more_prompt`) asks for ALL of them instead.
//   * The part of the day flips at 11:00 and 15:00.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

const resolveExtensionless: ResolveHookSync = (specifier, context, nextResolve) => {
    const parent = context.parentURL ?? '';
    const relative = /^\.{1,2}\//.test(specifier);
    if (parent.includes('/frontend/src/') && relative && !/\.[a-z]+$/i.test(specifier)) {
        return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
};

registerHooks({ resolve: resolveExtensionless });

const {
    RHYTHM_ACTION_KINDS,
    RHYTHM_UNAVAILABLE_COPY,
    RHYTHM_UNAVAILABLE_REASONS,
    askCubieHref,
    rhythmUnavailableCopy,
} = await import('./salesRhythm.ts');
const { RHYTHM_PARTS, SALES_RHYTHM_PROMPTS, rhythmAudienceFor, rhythmPartFor } = await import(
    '../pages/chatSuggestions.ts'
);

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string => fs.readFileSync(path.join(REPO, relative), 'utf8');

const ROUTER_PY = read('backend/routers/sales_rhythm.py');
const CHECKS_PY = read('rubik_core/sales/rhythm_checks.py');
const RHYTHM_TSX = read('frontend/src/pages/sales/DailyRhythm.tsx');

const unique = (values: string[]): string[] => [...new Set(values)].sort();

describe('rhythm action kinds', () => {
    const emitted = unique([...ROUTER_PY.matchAll(/\bkind="([a-z_]+)"/g)].map(m => m[1]));

    it('finds kinds in the router at all', () => {
        assert.ok(emitted.length >= 2, `only found ${emitted.length} kinds`);
    });

    it('declares exactly the kinds the router emits', () => {
        assert.deepEqual([...RHYTHM_ACTION_KINDS].sort(), emitted);
    });

    it('handles each kind case by case in DailyRhythm', () => {
        const handled = unique([...RHYTHM_TSX.matchAll(/case '([a-z_]+)':/g)].map(m => m[1]));
        assert.deepEqual(handled, emitted);
    });
});

describe('rhythm unavailable reasons', () => {
    const emitted = new Set<string>();
    for (const m of CHECKS_PY.matchAll(/^[A-Z_]+ = "([a-z_]+)"$/gm)) {
        if (/^(crm_|no_)/.test(m[1])) emitted.add(m[1]);
    }
    for (const m of ROUTER_PY.matchAll(/_empty_block\(c, "([a-z_]+)"\)/g)) emitted.add(m[1]);
    for (const m of ROUTER_PY.matchAll(/result\.reason or "([a-z_]+)"/g)) emitted.add(m[1]);

    it('finds the reasons in the server at all', () => {
        assert.ok(emitted.size >= 3, `only found ${emitted.size} reason codes`);
    });

    it('declares every code the server can send, each with a real sentence', () => {
        for (const reason of emitted) {
            assert.ok(
                (RHYTHM_UNAVAILABLE_REASONS as readonly string[]).includes(reason),
                `${reason} is not declared`,
            );
        }
        for (const reason of RHYTHM_UNAVAILABLE_REASONS) {
            assert.equal(rhythmUnavailableCopy(reason), RHYTHM_UNAVAILABLE_COPY[reason]);
        }
    });

    it('admits the gap for a code this build has never heard of', () => {
        const copy = rhythmUnavailableCopy('some_future_code');
        assert.ok(copy && copy.includes('some_future_code'));
    });

    it('treats no reason as a genuinely empty block', () => {
        assert.equal(rhythmUnavailableCopy(undefined), null);
    });
});

describe('overflow questions are the starter prompts', () => {
    const repPrompts = new Set(RHYTHM_PARTS.flatMap(p => SALES_RHYTHM_PROMPTS.rep[p]));
    const asks = [...ROUTER_PY.matchAll(/"ask_prompt": "([^"]+)"/g)].map(m => m[1]);
    const recap = /^RECAP_PROMPT = "([^"]+)"$/m.exec(ROUTER_PY);

    it('finds every block in the router', () => {
        assert.equal(asks.length, 7);
        assert.ok(recap, 'RECAP_PROMPT not found as one literal');
    });

    it('offers every overflow question and the recap as a rep prompt', () => {
        for (const ask of asks) assert.ok(repPrompts.has(ask), `missing from chatSuggestions: ${ask}`);
        assert.ok(recap && repPrompts.has(recap[1]));
    });

    it('gives every capped block an uncapped "+N more" question', () => {
        const mores = [...ROUTER_PY.matchAll(/"more_prompt": "([^"]+)"/g)].map(m => m[1]);
        const capped = asks.filter(a => /\b(3|5|10) deals\b/.test(a));
        assert.ok(capped.length >= 2, 'expected the capped checklist wordings');
        assert.equal(mores.length, capped.length);
        for (const more of mores) {
            assert.ok(/\ball\b/i.test(more), `"+N more" must ask for all: ${more}`);
            assert.ok(!/\b(3|5|10) deals\b/.test(more), `"+N more" must not cap: ${more}`);
        }
    });

    it('builds an assistant link that Chat can read back', () => {
        const href = askCubieHref(asks[0]);
        assert.ok(href.startsWith('/chat?q='));
        assert.equal(new URLSearchParams(href.split('?')[1]).get('q'), asks[0]);
    });
});

describe('the routine for the time of day', () => {
    const at = (h: number, m = 0) => new Date(2026, 9, 1, h, m);

    it('flips at 11:00 and 15:00 on the reader’s clock', () => {
        assert.equal(rhythmPartFor(at(7)), 'morning');
        assert.equal(rhythmPartFor(at(10, 59)), 'morning');
        assert.equal(rhythmPartFor(at(11)), 'midday');
        assert.equal(rhythmPartFor(at(14, 59)), 'midday');
        assert.equal(rhythmPartFor(at(15)), 'eod');
        assert.equal(rhythmPartFor(at(23)), 'eod');
    });

    it('gives a leader on the team roster the team prompts', () => {
        assert.equal(rhythmAudienceFor({ rep: true, team: true }), 'team');
        assert.equal(rhythmAudienceFor({ rep: true, team: false }), 'rep');
        assert.equal(rhythmAudienceFor({ rep: false, team: false }), null);
    });

    it('keeps every routine short enough to read at a glance', () => {
        for (const audience of ['rep', 'team'] as const) {
            for (const part of RHYTHM_PARTS) {
                const prompts = SALES_RHYTHM_PROMPTS[audience][part];
                assert.ok(prompts.length >= 2 && prompts.length <= 4, `${audience}/${part}`);
                assert.equal(new Set(prompts).size, prompts.length, `${audience}/${part} repeats`);
            }
        }
    });

    it('asks the team view for a top 10, not a rep’s top 3', () => {
        assert.ok(SALES_RHYTHM_PROMPTS.team.morning.some(p => p.includes('10 deals closing this month')));
        assert.ok(SALES_RHYTHM_PROMPTS.team.eod.some(p => p.includes('my team create or update today')));
    });
});
