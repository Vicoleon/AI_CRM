// Contract tests for the CRM Sync client's card helpers.
//
//   cd <repo>/frontend && node --test "src/api/crmSync.test.ts"
//
// What is pinned: Approve stays on for a pending or failed card (a failed write
// is a retry), is off for anything already handled, and Edit needs fields.
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import { describe, it } from 'node:test';

// Same scoped resolver as scripts.test.ts: `./client` is extensionless.
const resolveExtensionless: ResolveHookSync = (specifier, context, nextResolve) => {
    const parent = context.parentURL ?? '';
    const relative = /^\.{1,2}\//.test(specifier);
    if (parent.includes('/frontend/src/') && relative && !/\.[a-z]+$/i.test(specifier)) {
        return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
};

registerHooks({ resolve: resolveExtensionless });

const { approveLabel, canApprove, canEdit } = await import('./crmSync.ts');

describe('canApprove', () => {
    it('keeps plain Approve / Retry', () => {
        assert.equal(approveLabel({ status: 'pending' }), 'Approve');
        assert.equal(approveLabel({ status: 'failed' }), 'Retry');
    });

    it('offers Edit only on a card with fields', () => {
        const props = { phone: { before: null, after: 'x' } };
        assert.equal(canEdit({ status: 'pending', properties: props }), true);
        assert.equal(canEdit({ status: 'pending', properties: {} }), false);
        assert.equal(canEdit({ status: 'applied', properties: props }), false);
    });

    it('stays on for a pending or failed card', () => {
        assert.equal(canApprove({ status: 'pending' }), true);
        assert.equal(canApprove({ status: 'failed' }), true);
    });

    it('is off for anything already handled', () => {
        for (const status of ['applied', 'dismissed', 'superseded', 'expired', 'applying'] as const) {
            assert.equal(canApprove({ status }), false, status);
        }
    });
});
