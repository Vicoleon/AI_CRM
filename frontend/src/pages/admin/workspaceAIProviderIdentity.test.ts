import assert from 'node:assert/strict';
import test from 'node:test';
import { formBelongsToIdentity } from './workspaceAIProviderIdentity.ts';

test('credential form cannot submit before a workspace switch clears old fields', () => {
    const acmeForm = 0;
    const rubiksoftIdentity = 1;
    assert.equal(formBelongsToIdentity(acmeForm, rubiksoftIdentity), false);
    assert.equal(formBelongsToIdentity(rubiksoftIdentity, rubiksoftIdentity), true);
});
