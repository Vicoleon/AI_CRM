import assert from 'node:assert/strict';
import { afterEach, describe, it } from 'node:test';

import { exchangeGoogleCredential, setupReason, SessionExchangeError } from './session.ts';

const originalFetch = globalThis.fetch;

afterEach(() => {
    globalThis.fetch = originalFetch;
});

describe('workspace sign-in', () => {
    it('sends the Google ID credential once and accepts only a session token', async () => {
        const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
        globalThis.fetch = async (input, init) => {
            calls.push([input, init]);
            return new Response(JSON.stringify({ token: 'cubie-session' }), { status: 200 });
        };

        assert.equal(await exchangeGoogleCredential('signed-google-id-token'), 'cubie-session');
        assert.equal(calls.length, 1);
        assert.equal(calls[0][0], '/api/auth/session');
        assert.equal(calls[0][1]?.method, 'POST');
        assert.equal((calls[0][1]?.headers as Record<string, string>).Authorization, 'Bearer signed-google-id-token');
    });

    it('recognizes only the setup codes used by the public not-set-up page', () => {
        assert.equal(setupReason({ detail: { code: 'workspace_unknown' } }), 'workspace_unknown');
        assert.equal(setupReason({ detail: { code: 'workspace_personal' } }), 'workspace_personal');
        assert.equal(setupReason({ code: 'workspace_suspended' }), 'workspace_suspended');
        assert.equal(setupReason({ detail: { code: '../acme' } }), null);
        assert.equal(setupReason({ detail: 'workspace_unknown' }), null);
    });

    it('exposes a structured 403 setup error without accepting a malformed session', async () => {
        globalThis.fetch = async () => new Response(
            JSON.stringify({ detail: { code: 'workspace_suspended', message: 'Ask your workspace admin.' } }),
            { status: 403 },
        );
        await assert.rejects(
            exchangeGoogleCredential('signed-google-id-token'),
            (error: unknown) => error instanceof SessionExchangeError
                && error.status === 403
                && error.reason === 'workspace_suspended',
        );
        globalThis.fetch = async () => new Response(JSON.stringify({ token: null }), { status: 200 });
        await assert.rejects(exchangeGoogleCredential('signed-google-id-token'), /Sign-in failed/);
    });
});
