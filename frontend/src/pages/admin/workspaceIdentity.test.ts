import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { parseWorkspaceIdentity } from './workspaceIdentity.ts';

const validIdentity = {
    primary_domain: 'example.com',
    domains: ['example.com', 'example.org'],
    modules: { core: true, eos: false, sales_coach: true },
};

describe('workspace identity response', () => {
    it('keeps the complete server verdict for the read-only Admin card', () => {
        assert.deepEqual(parseWorkspaceIdentity(validIdentity), validIdentity);
    });

    it('uses the unavailable state for older or malformed responses', () => {
        for (const response of [
            null,
            {},
            { ...validIdentity, domains: null },
            { ...validIdentity, domains: ['example.com', null] },
            { ...validIdentity, modules: null },
            { ...validIdentity, modules: { core: true, eos: false } },
            { ...validIdentity, modules: { ...validIdentity.modules, eos: 'false' } },
        ]) {
            assert.equal(parseWorkspaceIdentity(response), null);
        }
    });
});
