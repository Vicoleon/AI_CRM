// Contract tests for the Cubie Sales Cockpit client.
//
//   cd <repo>/frontend && node --test "src/api/**/*.test.ts"
//
// (The bare directory form works on Node 22 but not on Node 24, which tries to
// load the path as a module — pass the glob. `npx tsc -b` covers this file too,
// through tsconfig.test.json, so it cannot rot silently.)
//
// WHY THESE TESTS READ PYTHON AND JSX AS TEXT
// ───────────────────────────────────────────
// Everything this file pins is a WIRE STRING shared with a server that TypeScript
// cannot see. The failures they exist to catch are all of one shape: the client
// and the router disagree about a literal, nothing fails to compile, no request
// errors, and the UI quietly says something false. Three had already shipped:
//
//   * `wrapupRoute` built `/calls/wrapup?session=<id>`, which `App.tsx` does not
//     register. An AE opened My Day, clicked the single primary action on the
//     block the spec puts FIRST because it decays, and landed on a blank page.
//     The same shape is interpolated into the HubSpot call record by
//     `backend/routers/calls.py::_WRAPUP_PATH`, so the deep link sitting inside
//     the client's CRM was dead too.
//   * Six of the eight action `kind` strings were invented rather than read off
//     the router, so six of My Day's eight buttons fell through to the disabled
//     "Cubie does not know how to do that" fallback.
//   * `crm_read_failed` was missing from the `unavailable_reason` set, so
//     "HubSpot could not be read" rendered as "you are caught up".
//
// A unit test over the client alone would have passed in all three cases. So
// these re-derive the truth FROM the other side — the router source, the route
// table — rather than restating a constant this file could also get wrong.
// Reading source text is the crude part; being unable to lie about the server is
// the point.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

// `api/cockpit.ts` imports `./client`, extensionless, the way every module in
// this app does — a specifier Vite resolves and bare Node does not. Rather than
// reshape the source for the test runner, teach the runner Vite's rule, and only
// for our own files: an unscoped hook also rewrites the CJS `require`s inside
// node_modules and breaks axios's dependency tree.
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
    BLOCK_UNAVAILABLE_COPY,
    BLOCK_UNAVAILABLE_REASONS,
    COCKPIT_ACTION_KINDS,
    blockUnavailableCopy,
    isBlockUnavailableReason,
    isCockpitActionKind,
    wrapupRoute,
} = await import('./cockpit.ts');

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string =>
    fs.readFileSync(path.join(REPO, relative), 'utf8');

const COCKPIT_PY = read('backend/routers/cockpit.py');
const APP_TSX = read('frontend/src/App.tsx');
const MY_DAY_TSX = read('frontend/src/pages/sales/MyDay.tsx');
const CALLS_PY = read('backend/routers/calls.py');

const unique = (values: string[]): string[] => [...new Set(values)].sort();

// ─── the wrap-up route ───────────────────────────────────────────────────────

describe('wrapupRoute', () => {
    it('builds the path shape App.tsx registers, not a query string', () => {
        assert.equal(wrapupRoute('sess-123'), '/calls/sess-123/wrapup');
    });

    it('percent-encodes the session id', () => {
        // Ids are hex today, so nothing needs escaping — which is exactly when
        // an unescaped interpolation survives review and then breaks on the
        // first id that is not.
        assert.equal(wrapupRoute('a b/c'), '/calls/a%20b%2Fc/wrapup');
    });

    it('matches a route App.tsx actually registers', () => {
        // The regression, stated as the router table sees it. `/calls/wrapup?
        // session=<id>` matches NOTHING here, and react-router renders a blank
        // page for a path it cannot match rather than raising anything.
        const registered = [...APP_TSX.matchAll(/path="([^"]*)"/g)]
            .map(m => m[1])
            .filter(p => p.startsWith('calls'));
        assert.ok(registered.length > 0, 'no /calls routes found in App.tsx');

        const pathname = wrapupRoute('sess-123').split('?')[0].replace(/^\//, '');
        const matches = registered.filter(route =>
            new RegExp(`^${route.replace(/:[A-Za-z0-9_]+/g, '[^/]+')}$`).test(pathname),
        );
        assert.deepEqual(matches, ['calls/:sessionId/wrapup']);
    });

    it('agrees with the path the backend puts inside the HubSpot call record', () => {
        // `_WRAPUP_PATH` is interpolated onto FRONTEND_URL and written into the
        // CRM. If these two ever diverge, the in-app button and the link in
        // HubSpot cannot both work.
        const declared = /_WRAPUP_PATH = "([^"]+)"/.exec(CALLS_PY);
        assert.ok(declared, '_WRAPUP_PATH not found in backend/routers/calls.py');
        assert.equal(
            declared[1].replace('{call_session_id}', 'sess-123'),
            wrapupRoute('sess-123'),
        );
    });
});

// ─── the action vocabulary ───────────────────────────────────────────────────

describe('action kinds', () => {
    // Every `Action(..., kind="…")` the router constructs. A literal here is the
    // only thing that reaches the client, so the set is closed and derivable.
    const emitted = unique([...COCKPIT_PY.matchAll(/\bkind="([a-z_]+)"/g)].map(m => m[1]));

    it('declares exactly the kinds the router emits', () => {
        assert.deepEqual([...COCKPIT_ACTION_KINDS].sort(), emitted);
    });

    it('is handled case-by-case in My Day', () => {
        const handled = unique([...MY_DAY_TSX.matchAll(/case '([a-z_]+)':/g)].map(m => m[1]));
        assert.deepEqual(handled, emitted);
    });

    it('degrades an unknown kind instead of narrowing it away', () => {
        // `Action.kind` is a bare `str` server-side, so a ninth kind is a
        // runtime event and not a compile error. The guard has to stay in front
        // of the switch, and it has to say no.
        assert.equal(isCockpitActionKind('review_wrapup'), true);
        assert.equal(isCockpitActionKind('open_wrapup'), false);
        assert.equal(isCockpitActionKind(undefined), false);
    });
});

// ─── "you have nothing" versus "Cubie could not look" ────────────────────────

describe('block unavailable reasons', () => {
    // Every code a `Block(...)` can carry: the literals the router constructs
    // one with, plus the non-null values `_Crm.reason` can hold.
    const emitted = new Set<string>();
    for (const line of COCKPIT_PY.split('\n')) {
        for (const m of line.matchAll(/unavailable_reason=(?:[\w.]+ or )?"([a-z_]+)"/g))
            emitted.add(m[1]);
        for (const m of line.matchAll(/(?:^|[^\w])reason(?:: str \| None)? = "([a-z_]+)"/g))
            emitted.add(m[1]);
        for (const m of line.matchAll(/_Crm\(reason="([a-z_]+)"/g)) emitted.add(m[1]);
    }

    it('finds the reasons in the router at all', () => {
        // A guard on the extraction itself: a regex that silently matches
        // nothing would make every assertion below vacuously true.
        assert.ok(emitted.size >= 4, `only found ${emitted.size} reason codes`);
    });

    it('declares every code the router can send', () => {
        const missing = [...emitted].filter(r => !isBlockUnavailableReason(r));
        assert.deepEqual(missing, []);
    });

    it('gives every declared code its own sentence', () => {
        for (const reason of BLOCK_UNAVAILABLE_REASONS) {
            const copy = blockUnavailableCopy(reason);
            assert.equal(copy, BLOCK_UNAVAILABLE_COPY[reason]);
            assert.ok(copy && copy.length > 20, `${reason} has no real copy`);
        }
    });

    it('keeps crm_read_failed declared even while the router folds it away', () => {
        // `_no_contact_block` reports a failed stale-deal search as
        // `crm_unavailable` today. The code stays declared so an older backend —
        // or a revert — degrades to an honest sentence rather than to a clean
        // slate. This is the finding-17 pin: it must not resolve to the generic
        // fallback.
        assert.equal(isBlockUnavailableReason('crm_read_failed'), true);
        const copy = blockUnavailableCopy('crm_read_failed');
        assert.ok(copy && !copy.includes('Cubie could not load this block'));
        assert.ok(copy && /not\s+finish|unavailable/i.test(copy));
    });

    it('admits the gap for a code this build has never heard of', () => {
        // The other half of the same rule: an unknown code must produce a
        // sentence that says Cubie could not look, never an empty state.
        const copy = blockUnavailableCopy('some_future_code');
        assert.ok(copy && copy.includes('some_future_code'));
        assert.ok(copy && copy.includes('does not mean there is nothing outstanding'));
    });

    it('says nothing when the block is genuinely just empty', () => {
        assert.equal(blockUnavailableCopy(null), null);
        assert.equal(blockUnavailableCopy(undefined), null);
        assert.equal(blockUnavailableCopy(''), null);
    });
});
