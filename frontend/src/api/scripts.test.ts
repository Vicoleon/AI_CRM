// Contract tests for the Call Scripts authoring client.
//
//   cd <repo>/frontend && node --test "src/api/**/*.test.ts"
//
// (The bare directory form works on Node 22 but not on Node 24, which tries to
// load the path as a module — pass the glob. `npx tsc -b` covers this file too,
// through tsconfig.test.json, so it cannot rot silently.)
//
// WHY THIS CLIENT NEEDS TESTS OF THIS SHAPE
// ─────────────────────────────────────────
// The script editor is the feature's promise: Harper or Indigo adds a
// product's checklist through a form, with no deploy. Everything that can go
// wrong on it goes wrong QUIETLY, which is the same failure mode the whole
// feature exists to remove:
//
//   * A KEY RULE DRIFTING. `product_key` and `fact_key` are the join keys the
//     rest of the feature matches on, exactly and never fuzzily. If the form's
//     rule is LOOSER than the router's, the author gets a 400 they can read — an
//     annoyance. If it is TIGHTER, the form refuses a key the server would have
//     taken, and the author quietly works around it with a different key that
//     then never matches anything.
//   * THE TWO 404s COLLAPSING. `_require_ccs_enabled` ("the feature is off") and
//     a missing script ("that one does not exist") arrive as the same status with
//     a bare-string detail. Rendering them as one sentence sends an author to an
//     administrator over a typo, or the reverse.
//   * `scriptState` GETTING THE TRUTH TABLE WRONG. Three columns answer one
//     question — is this what an AE sees on a live client call — and an editor
//     that says "Approved" over a version the panel does not render is a lie the
//     author has no way to check.
//   * THE EDITOR SENDING `items: []` FOR A CHECKLIST IT COULD NOT READ. There is
//     no `GET /api/scripts/{id}`, so the screen often does not hold a version's
//     questions; the router treats an OMITTED `items` as "leave them alone" and
//     `[]` as "empty it". One wrong line silently wipes a checklist nobody on the
//     screen could see.
//
// Each is re-derived FROM the other side — the Python source, the page's own
// source — rather than restated as a constant this file could get equally wrong.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

// `api/scripts.ts` imports `./client`, extensionless, the way every module in
// this app does — a specifier Vite resolves and bare Node does not. Teach the
// runner Vite's rule, and only for our own files: an unscoped hook also rewrites
// the CJS `require`s inside node_modules and breaks axios's dependency tree.
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
    MAX_HELP_CHARS,
    MAX_KEY_CHARS,
    MAX_NAME_CHARS,
    MAX_NOTES_CHARS,
    MAX_PROMPT_CHARS,
    MAX_SCRIPT_ITEMS,
    MAX_TARGET_CHARS,
    SCRIPT_KEY_RE,
    SCRIPT_TARGET_RE,
    foldKey,
    groupByProduct,
    isAuthorDenied,
    isConflict,
    isForbidden,
    isNotEnabled,
    isScriptNotFound,
    keyProblem,
    savingForks,
    scriptState,
    scriptsError,
    suggestKey,
    targetProblem,
} = await import('./scripts.ts');
type ScriptSummary = import('./scripts.ts').ScriptSummary;

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string => fs.readFileSync(path.join(REPO, relative), 'utf8');

/** The source with its PROSE removed — whole-line `//` comments and `/* … *\/`
 *  blocks. The editor's own header explains the `items: []` trap by name, so a
 *  ban on the CODE has to ignore the documentation of the thing it protects. */
const codeOnly = (source: string): string =>
    source
        .replace(/\/\*[\s\S]*?\*\//g, ' ')
        .split('\n')
        .filter(line => !/^\s*\/\//.test(line))
        .join('\n');

const ROUTER_PY = read('backend/routers/scripts.py');
const CALLS_PY = read('backend/routers/calls.py');
const EDITOR_TSX = codeOnly(read('frontend/src/pages/scripts/ScriptEditor.tsx'));

/** The body of a python `NAME = re.compile(r"…")`, read rather than restated. */
const pyRegex = (source: string, name: string): string => {
    const match = new RegExp(`${name} = re\\.compile\\(r"([^"]+)"\\)`).exec(source);
    assert.ok(match, `could not find ${name} in scripts.py`);
    return match[1];
};

/** A python `NAME = <int>` constant. */
const pyInt = (source: string, name: string): number => {
    const match = new RegExp(`^${name} = (\\d+)$`, 'm').exec(source);
    assert.ok(match, `could not find ${name} in scripts.py`);
    return Number(match[1]);
};

/** `max_length=<int>` on a named pydantic field, e.g. `prompt_text: str = Field(min_length=1, max_length=1000)`. */
const pyFieldMax = (source: string, field: string): number => {
    const match = new RegExp(`${field}: [^\\n]*max_length=(\\d+)`).exec(source);
    assert.ok(match, `could not find max_length for ${field} in scripts.py`);
    return Number(match[1]);
};

const summary = (over: Partial<ScriptSummary> = {}): ScriptSummary => ({
    script_id: 's1',
    product_key: 'implementation_plan',
    name: 'Implementation Plan',
    version: 1,
    hubspot_product_id: null,
    is_active: true,
    is_live: false,
    approved_by: null,
    approved_at: null,
    source: 'manual',
    notes: null,
    item_count: 3,
    required_item_count: 2,
    created_at: null,
    updated_at: null,
    ...over,
});

const rejection = (status: number, detail: unknown): unknown => ({ response: { status, data: { detail } } });

describe('the key rules are the router’s, character for character', () => {
    it('mirrors _KEY_RE', () => {
        // Tighter than the server would be worse than looser: the author works
        // around a refusal with a key that then matches nothing.
        assert.equal(SCRIPT_KEY_RE.source, pyRegex(ROUTER_PY, '_KEY_RE'));
    });

    it('mirrors _TARGET_RE', () => {
        assert.equal(SCRIPT_TARGET_RE.source, pyRegex(ROUTER_PY, '_TARGET_RE'));
    });

    it('mirrors the item cap and every field length the router enforces', () => {
        assert.equal(MAX_SCRIPT_ITEMS, pyInt(ROUTER_PY, '_MAX_ITEMS'));
        assert.equal(MAX_KEY_CHARS, pyFieldMax(ROUTER_PY, 'fact_key'));
        assert.equal(MAX_PROMPT_CHARS, pyFieldMax(ROUTER_PY, 'prompt_text'));
        assert.equal(MAX_HELP_CHARS, pyFieldMax(ROUTER_PY, 'help_text'));
        assert.equal(MAX_TARGET_CHARS, pyFieldMax(ROUTER_PY, 'hubspot_target'));
        assert.equal(MAX_NOTES_CHARS, pyFieldMax(ROUTER_PY, 'notes'));
        // `name` is declared on two models; both carry the same cap and the
        // regex finds the first, which is the one a create is checked against.
        assert.equal(MAX_NAME_CHARS, pyFieldMax(ROUTER_PY, 'name'));
    });

    it('folds a key the way the router does, and no further', () => {
        assert.equal(foldKey('  Implementation_Plan  '), 'implementation_plan');
        // NOT slugified: normalizing differently from the writer would stop
        // matching rows a human can see in the table.
        assert.equal(foldKey('implementation plan'), 'implementation plan');
    });

    it('refuses a key with a space, and its suggestion is itself a legal key', () => {
        const problem = keyProblem('Implementation Plan', 'Product key');
        assert.ok(problem, 'a key with a space must be refused');
        assert.match(problem, /no spaces/);
        assert.ok(SCRIPT_KEY_RE.test(suggestKey('Implementation Plan')));
    });

    it('suggests a legal key from an ordinary question, apostrophes included', () => {
        for (const question of [
            'How many hours per week?',
            "What's the rate?",
            'When would this commence — and who signs it off, roughly speaking, in the end?',
        ]) {
            const key = suggestKey(question);
            assert.ok(key, `no suggestion for ${question}`);
            assert.equal(keyProblem(key, 'Fact key'), null, `suggested an illegal key for ${question}: ${key}`);
        }
    });

    it('treats an empty HubSpot target as complete, not as a gap', () => {
        // Some facts feed the SOW rather than the CRM — hours per week, a
        // rate — and there may be no property that holds them.
        assert.equal(targetProblem(''), null);
        assert.equal(targetProblem('  '), null);
        assert.equal(targetProblem('company.industry'), null);
        assert.equal(targetProblem('industry'), null);
        assert.ok(targetProblem('company industry'));
    });
});

describe('the two 404s and the two 403s stay apart', () => {
    it('reads the flag-off sentence out of calls.py rather than restating it', () => {
        // The router imports `_require_ccs_enabled` from calls.py so there is one
        // flag and one message; this test reads that one message.
        const match = /detail="(Sales call capture is not enabled[^"]*)"/.exec(CALLS_PY);
        assert.ok(match, 'could not find the CCS flag-off detail in calls.py');
        const err = scriptsError(rejection(404, match[1]));
        assert.ok(isNotEnabled(err));
        assert.ok(!isScriptNotFound(err));
    });

    it('reads the missing-script sentence out of scripts.py', () => {
        assert.match(ROUTER_PY, /detail="No such call script\."/);
        const err = scriptsError(rejection(404, 'No such call script.'));
        assert.ok(isScriptNotFound(err));
        assert.ok(!isNotEnabled(err));
        // The router's own words are the honest answer, so they are shown.
        assert.equal(err.message, 'No such call script.');
    });

    it('treats “no approved script for this product” as a miss, not as a flag', () => {
        const err = scriptsError(rejection(404, "No approved call script for 'implementation_plan'."));
        assert.ok(isScriptNotFound(err));
    });

    it('tells the authoring refusal from the audience refusal', () => {
        // `_AUTHOR_DENIED` is read from the router: the client discriminates on
        // its opening words, so a rewrite there must fail here rather than
        // silently start rendering "this surface is not yours" at an author.
        const match = /_AUTHOR_DENIED = \(\s*"([^"]+)"/.exec(ROUTER_PY);
        assert.ok(match, 'could not find _AUTHOR_DENIED in scripts.py');
        const authoring = scriptsError(rejection(403, match[1]));
        assert.ok(isAuthorDenied(authoring), `_AUTHOR_DENIED no longer matches the client: ${match[1]}`);
        assert.ok(!isForbidden(authoring));

        const audience = scriptsError(rejection(403, 'Sales Call Capture is not turned on for you. Ask your admin.'));
        assert.ok(isForbidden(audience));
        assert.ok(!isAuthorDenied(audience));
    });

    it('keeps a version race recoverable rather than fatal', () => {
        const err = scriptsError(rejection(409, 'Someone else saved a new version of this script a moment ago.'));
        assert.ok(isConflict(err));
        assert.match(err.message, /Someone else saved/);
    });

    it('survives FastAPI’s 422 array without showing “undefined”', () => {
        const err = scriptsError(rejection(422, [{ msg: 'ensure this value has at most 50 items' }]));
        assert.equal(err.code, 'invalid_request');
        assert.match(err.detail ?? '', /at most 50 items/);
    });
});

describe('scriptState answers the one question an AE’s screen asks', () => {
    it('calls the newest approved, active version Approved', () => {
        assert.equal(scriptState(summary({ approved_by: 'harper@acme.example', is_live: true })), 'approved');
    });

    it('calls an unapproved version a Draft, however active it is', () => {
        // `source="mined"` rows land written by a machine with is_active already
        // true. Approval, not activity, is what makes a script live.
        assert.equal(scriptState(summary({ source: 'mined', is_active: true, approved_by: null })), 'draft');
    });

    it('calls an approved version that lost to a newer one Superseded', () => {
        assert.equal(scriptState(summary({ approved_by: 'harper@acme.example', is_live: false })), 'superseded');
    });

    it('calls anything switched off Retired, approved or not', () => {
        assert.equal(scriptState(summary({ is_active: false })), 'retired');
        assert.equal(scriptState(summary({ is_active: false, approved_by: 'harper@acme.example' })), 'retired');
    });

    it('warns that a save forks whenever a version is approved — live or not', () => {
        // A superseded version forks too: some finished call ran under it, and
        // its wrap-up recomputes the gap list from its items.
        assert.ok(savingForks(summary({ approved_by: 'harper@acme.example', is_live: true })));
        assert.ok(savingForks(summary({ approved_by: 'harper@acme.example', is_live: false })));
        assert.ok(!savingForks(summary({ approved_by: null })));
    });
});

describe('the catalogue groups by product without inventing an order', () => {
    it('keeps the server’s product order and puts the newest version first', () => {
        const groups = groupByProduct([
            summary({ script_id: 'b2', product_key: 'b', version: 2, name: 'B two' }),
            summary({ script_id: 'b1', product_key: 'b', version: 1, name: 'B one' }),
            summary({ script_id: 'a1', product_key: 'a', version: 1, name: 'A one' }),
        ]);
        assert.deepEqual(
            groups.map(g => g.product_key),
            ['b', 'a'],
        );
        assert.deepEqual(groups[0].versions.map(v => v.version), [2, 1]);
        // The name an author recognises is the newest version's.
        assert.equal(groups[0].name, 'B two');
    });

    it('reports no live version rather than guessing one', () => {
        // The normal early state — the tables ship empty — and it means AEs see
        // NO checklist for that product, which the screen has to be able to say.
        const [group] = groupByProduct([summary({ approved_by: null, is_live: false })]);
        assert.equal(group.live, null);

        const [withLive] = groupByProduct([summary({ approved_by: 'x@acme.example', is_live: true })]);
        assert.equal(withLive.live?.script_id, 's1');
    });
});

describe('the editor never empties a checklist it could not read', () => {
    it('sends `items` on a save only when it holds them', () => {
        // There is no `GET /api/scripts/{id}`, so `form.items` is null whenever
        // the screen did not itself write or fetch this version's questions. The
        // router keeps the current checklist for an OMITTED `items` and empties
        // it for `[]`, so this guard is the whole difference between a safe
        // rename and a silent wipe.
        assert.match(EDITOR_TSX, /if \(form\.items\) body\.items = toInput\(form\.items\);/);
    });

    it('never hands the update body an unconditional empty list', () => {
        assert.doesNotMatch(EDITOR_TSX, /body\.items = toInput\(form\.items \?\? \[\]\)/);
    });

    it('states the fork consequence on the save button, not in a tooltip', () => {
        // Self-serve survives only if the person clicking understands
        // what the click does. `title=` is where consequences go to be missed.
        assert.match(EDITOR_TSX, /Save as v\$\{nextVersion\}/);
        assert.match(EDITOR_TSX, /Approve — put this in front of AEs on live calls/);
    });
});
