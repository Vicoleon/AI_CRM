import assert from 'node:assert/strict';
import test from 'node:test';

import {
    saveWorkspaceChanges,
    saveWorkspaceCrmChanges,
    tokenMatchesWorkspace,
    type WorkspacePatch,
    type WorkspaceRequestIdentity,
} from './workspaceOperationsRequest.ts';

const acme: WorkspaceRequestIdentity = {
    slug: 'acme',
    token: 'acme-support-token',
    headers: {},
};

test('a token for another workspace cannot load or save this form', () => {
    const token = `eyJhbGciOiJIUzI1NiJ9.${Buffer.from(JSON.stringify({ ws: 'beta' })).toString('base64url')}.signature`;
    assert.equal(tokenMatchesWorkspace(token, 'acme'), false);
    assert.equal(tokenMatchesWorkspace(token, 'beta'), true);
    assert.equal(tokenMatchesWorkspace('mock-token', 'acme'), false);
    assert.equal(tokenMatchesWorkspace('mock-token', 'acme', true), true);
});

test('switching workspaces while operations save is pending cancels the time-zone write', async () => {
    let resolveFirst: ((value: { data: unknown }) => void) | undefined;
    let currentSlug = 'acme';
    const calls: string[] = [];
    const patch: WorkspacePatch = (path, _payload, identity) => {
        calls.push(`${identity.slug}:${path}`);
        return new Promise(resolve => { resolveFirst = resolve; });
    };

    const pending = saveWorkspaceChanges(
        patch,
        acme,
        { deep_prep_sender_name: 'Acme' },
        'America/Costa_Rica',
        () => currentSlug === 'acme',
    );
    assert.equal(calls.length, 1);
    currentSlug = 'beta';
    resolveFirst?.({ data: { deep_prep_sender_name: 'Acme' } });
    const result = await pending;

    assert.equal(result.cancelled, true);
    assert.deepEqual(calls, ['acme:/api/users/workspace/operations']);
});

test('both writes use the same captured workspace identity', async () => {
    const calls: Array<{ path: string; token: string; slug: string }> = [];
    const patch: WorkspacePatch = async (path, _payload, identity) => {
        calls.push({ path, token: identity.token, slug: identity.slug });
        return { data: path.endsWith('time-zone') ? { time_zone: 'America/Costa_Rica' } : {} };
    };

    const result = await saveWorkspaceChanges(
        patch,
        acme,
        { deep_prep_sender_name: 'Acme' },
        'America/Costa_Rica',
        () => true,
    );

    assert.equal(result.cancelled, false);
    assert.deepEqual(calls, [
        { path: '/api/users/workspace/operations', token: acme.token, slug: acme.slug },
        { path: '/api/users/workspace/time-zone', token: acme.token, slug: acme.slug },
    ]);
});

test('stale workspace state sends neither write', async () => {
    let called = false;
    const patch: WorkspacePatch = async () => { called = true; return { data: {} }; };
    const result = await saveWorkspaceChanges(patch, acme, {}, 'America/Costa_Rica', () => false);
    assert.equal(result.cancelled, true);
    assert.equal(called, false);
});

test('CRM save cannot send another workspace form after active token switches', async () => {
    const calls: Array<{ path: string; token: string }> = [];
    let currentToken = acme.token;
    const patch: WorkspacePatch = async (path, _payload, identity) => {
        calls.push({ path, token: identity.token });
        return { data: {} };
    };
    currentToken = 'beta-support-token';
    const result = await saveWorkspaceCrmChanges(
        patch,
        acme,
        { portal_id: '123' },
        () => currentToken === acme.token,
    );
    assert.equal(result, null);
    assert.deepEqual(calls, []);
});

test('CRM save uses the token captured when the form was loaded', async () => {
    const calls: string[] = [];
    let currentToken = acme.token;
    const patch: WorkspacePatch = async (_path, _payload, identity) => {
        calls.push(identity.token);
        currentToken = 'beta-support-token';
        return { data: {} };
    };
    const result = await saveWorkspaceCrmChanges(patch, acme, { portal_id: '123' }, () => currentToken === acme.token);
    assert.equal(result, null);
    assert.deepEqual(calls, [acme.token]);
});
