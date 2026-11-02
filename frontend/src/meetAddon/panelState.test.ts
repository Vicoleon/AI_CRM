// Contract tests for the Meet side panel's pure logic.
//
//   cd <repo>/frontend && node --test "src/meetAddon/*.test.ts"
//
// The panel is read by an AE in the middle of a client call, so every failure
// worth testing here is a QUIET one:
//
//   * "WE COULD NOT LOOK" TURNING INTO "THERE IS NOTHING". A payload missing
//     `calendar_available` or `catalogue_available` must read as unavailable,
//     never as the reassuring default.
//   * AN UNCERTAIN COMPANY GETTING A NAME. Only a confirmed or suggested company
//     is ever named; a normalizer that passed a name through would put the
//     wrong client's name over a live call.
//   * PRODUCTS MERGING OR REORDERING. One section per product, server order
//     first, the AE's additions after — and an addition never duplicates.
//   * LAST WEEK'S TICKS ON THIS WEEK'S CALL. A recurring series reuses one Meet
//     code, so the tick storage key carries the day.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    addProduct,
    classifyFailure,
    EMPTY_LOCAL,
    localStateKey,
    normalizeContext,
    panelProducts,
    parseLocalState,
    pickableSections,
    removeProduct,
    sectionProgress,
    tickKey,
    toggleTick,
} from './panelState.ts';

const IMPLEMENTATION = {
    script_id: 's-impl',
    product_key: 'implementation',
    name: 'Implementation',
    items: [
        { fact_key: 'accounts', prompt_text: 'How many accounts?', required: true, help_text: null },
        { fact_key: 'hours', prompt_text: 'Hours per account?', required: true, help_text: 'Ask per account' },
        { fact_key: 'rate', prompt_text: 'What rate?', required: false, help_text: null },
    ],
};
const SUPPORT_PLAN = {
    script_id: 's-over',
    product_key: 'support_plan',
    name: 'Support Plan',
    items: [{ fact_key: 'licenses', prompt_text: 'How many licenses?', required: true, help_text: null }],
};

function payload(over: Record<string, unknown> = {}) {
    return {
        meeting_code: 'abc-defg-hij',
        match: 'calendar',
        calendar_available: true,
        calls_sharing_code: 1,
        call: {
            title: 'Acme — discovery',
            start_local: 'Wednesday Sep 30, 10:00 AM CDT',
            is_live: true,
            call_session_id: 'sess-1',
            capture_state: 'session_exists',
            confidential: false,
        },
        company: { state: 'confirmed', hubspot_company_id: 'co-42', name: 'Acme', domain: 'acmecorp.example' },
        products_source: 'call',
        selected: [{ product_key: 'implementation', name: 'Implementation', has_checklist: true }],
        catalogue_available: true,
        catalogue: [IMPLEMENTATION, SUPPORT_PLAN],
        ...over,
    };
}

describe('normalizeContext', () => {
    it('keeps a well-formed payload as it came', () => {
        const ctx = normalizeContext(payload());
        assert.equal(ctx.match, 'calendar');
        assert.equal(ctx.call?.call_session_id, 'sess-1');
        assert.equal(ctx.company?.name, 'Acme');
        assert.deepEqual(ctx.catalogue.map(s => s.product_key), ['implementation', 'support_plan']);
    });

    it('reads a missing availability flag as UNAVAILABLE, never as fine', () => {
        const raw = payload();
        delete (raw as Record<string, unknown>).calendar_available;
        delete (raw as Record<string, unknown>).catalogue_available;
        const ctx = normalizeContext(raw);
        assert.equal(ctx.calendar_available, false);
        assert.equal(ctx.catalogue_available, false);
    });

    it('never names an uncertain company, even if a name arrives', () => {
        const ctx = normalizeContext(
            payload({ company: { state: 'uncertain', hubspot_company_id: 'co-1', name: 'Acme A', reason: 'ambiguous' } }),
        );
        assert.equal(ctx.company?.state, 'uncertain');
        assert.equal(ctx.company?.name, null);
        assert.equal(ctx.company?.hubspot_company_id, null);
        assert.equal(ctx.company?.reason, 'ambiguous');
    });

    it('treats an unknown company state as uncertain', () => {
        const ctx = normalizeContext(payload({ company: { state: 'guessed', name: 'Acme' } }));
        assert.equal(ctx.company?.state, 'uncertain');
        assert.equal(ctx.company?.name, null);
    });

    it('defaults an item to required, and drops items with no fact key', () => {
        const ctx = normalizeContext(
            payload({
                catalogue: [
                    {
                        product_key: 'x',
                        name: 'X',
                        items: [{ fact_key: 'a', prompt_text: 'A?' }, { prompt_text: 'orphan' }, 'junk'],
                    },
                ],
            }),
        );
        assert.equal(ctx.catalogue[0].items.length, 1);
        assert.equal(ctx.catalogue[0].items[0].required, true);
    });

    it('dedupes selected products and keeps their order', () => {
        const ctx = normalizeContext(
            payload({
                selected: [
                    { product_key: 'b', name: 'B' },
                    { product_key: 'a', name: 'A' },
                    { product_key: 'b', name: 'B again' },
                ],
            }),
        );
        assert.deepEqual(ctx.selected.map(s => s.product_key), ['b', 'a']);
    });

    it('refuses something that is not the add-on payload at all', () => {
        assert.throws(() => normalizeContext(null));
        assert.throws(() => normalizeContext({ match: 'calendar' }));
    });
});

describe('classifyFailure', () => {
    it('keeps the four different answers apart', () => {
        assert.deepEqual(classifyFailure(401), { kind: 'signed_out' });
        assert.equal(classifyFailure(403, 'Not for you.').kind, 'not_for_you');
        assert.deepEqual(classifyFailure(404), { kind: 'switched_off' });
        assert.deepEqual(classifyFailure(422), { kind: 'bad_code' });
        assert.equal(classifyFailure(500).kind, 'error');
        assert.equal(classifyFailure(502).kind, 'error');
    });

    it('carries the server sentence on a 403', () => {
        const failure = classifyFailure(403, 'Cubie in Google Meet is not enabled for your account yet.');
        assert.equal(failure.kind, 'not_for_you');
        assert.ok('message' in failure && failure.message.includes('not enabled'));
    });
});

describe('panelProducts / pickableSections', () => {
    it('renders server products first, then additions, one section each', () => {
        const ctx = normalizeContext(payload());
        const products = panelProducts(ctx, ['support_plan', 'implementation']);
        assert.deepEqual(
            products.map(p => [p.product_key, p.origin]),
            [
                ['implementation', 'call'],
                ['support_plan', 'added'],
            ],
        );
    });

    it('labels a remembered pick as remembered, not as attached to the call', () => {
        const ctx = normalizeContext(payload({ products_source: 'remembered' }));
        assert.equal(panelProducts(ctx, [])[0].origin, 'remembered');
    });

    it('keeps a picked product with no checklist, with no section', () => {
        const ctx = normalizeContext(
            payload({ selected: [{ product_key: 'vendor_mgmt', name: 'Vendor mgmt', has_checklist: false }] }),
        );
        const [product] = panelProducts(ctx, []);
        assert.equal(product.product_key, 'vendor_mgmt');
        assert.equal(product.section, null);
    });

    it('drops an addition whose checklist is no longer live', () => {
        const ctx = normalizeContext(payload());
        assert.deepEqual(
            panelProducts(ctx, ['retired_product']).map(p => p.product_key),
            ['implementation'],
        );
    });

    it('offers only the checklists not already on screen', () => {
        const ctx = normalizeContext(payload());
        assert.deepEqual(pickableSections(ctx, []).map(s => s.product_key), ['support_plan']);
        assert.deepEqual(pickableSections(ctx, ['support_plan']), []);
    });
});

describe('the AE’s own ticks', () => {
    it('keys the ticks by meeting code AND day', () => {
        assert.notEqual(
            localStateKey('abc-defg-hij', '2026-09-30'),
            localStateKey('abc-defg-hij', '2026-10-07'),
        );
    });

    it('parses anything unreadable as an empty panel', () => {
        assert.deepEqual(parseLocalState(null), EMPTY_LOCAL);
        assert.deepEqual(parseLocalState('{not json'), EMPTY_LOCAL);
        assert.deepEqual(parseLocalState('[1,2]'), EMPTY_LOCAL);
        assert.deepEqual(parseLocalState('{"ticks":[1,"a","a"],"added":"x"}'), { ticks: ['a'], added: [] });
    });

    it('toggles a tick on and off', () => {
        const key = tickKey('implementation', 'accounts');
        const on = toggleTick(EMPTY_LOCAL, key);
        assert.deepEqual(on.ticks, [key]);
        assert.deepEqual(toggleTick(on, key).ticks, []);
    });

    it('removing an added product also forgets its ticks', () => {
        let state = addProduct(EMPTY_LOCAL, 'support_plan');
        state = addProduct(state, 'support_plan');
        assert.deepEqual(state.added, ['support_plan']);
        state = toggleTick(state, tickKey('support_plan', 'licenses'));
        state = toggleTick(state, tickKey('implementation', 'accounts'));
        state = removeProduct(state, 'support_plan');
        assert.deepEqual(state, { ticks: [tickKey('implementation', 'accounts')], added: [] });
    });

    it('counts required questions still to ask, not optional ones', () => {
        const none = sectionProgress(IMPLEMENTATION, []);
        assert.deepEqual(none, { asked: 0, total: 3, requiredLeft: 2 });
        const some = sectionProgress(IMPLEMENTATION, [tickKey('implementation', 'accounts'), tickKey('implementation', 'rate')]);
        assert.deepEqual(some, { asked: 2, total: 3, requiredLeft: 1 });
    });

    it('never counts another product’s tick against this one', () => {
        assert.equal(sectionProgress(IMPLEMENTATION, [tickKey('support_plan', 'accounts')]).asked, 0);
    });
});
