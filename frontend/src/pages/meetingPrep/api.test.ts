// Contract tests for the Meeting Prep API client.
//
//   cd <repo>/frontend && node --test "src/pages/meetingPrep/**/*.test.ts"
//
// A one-section rewrite is still answered in the request, so it must wait
// longer than the shared client's 105s default: the server keeps going after
// the browser gives up, so a rewrite on the 105s default could be reported as
// failed and then saved anyway, and the reader's next change met a 409 for a
// version they had been told did not exist.
//
// A full build (first version, Regenerate) now answers at once with a 202 and
// runs in the background, so it must NOT hold a long timeout, and the client
// must read the 202 as a build rather than as a brief.
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import { describe, it } from 'node:test';

// `api/meetingPrep.ts` imports `./client` extensionless, as every module in
// this app does; teach the runner Vite's rule for our own files only (see
// `src/api/calls.test.ts`).
const resolveExtensionless: ResolveHookSync = (specifier, context, nextResolve) => {
    const parent = context.parentURL ?? '';
    const relative = /^\.{1,2}\//.test(specifier);
    if (parent.includes('/frontend/src/') && relative && !/\.[a-z]+$/i.test(specifier)) {
        return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
};

registerHooks({ resolve: resolveExtensionless });

const { default: client } = await import('../../api/client.ts');
const { GENERATION_TIMEOUT_MS, getBuildStatus, getPrep, listVersions, regenerate, rewriteSection } = await import(
    '../../api/meetingPrep.ts'
);

type Config = { timeout?: number; params?: unknown } | undefined;

const PENDING = {
    status: 'queued',
    run_id: 'run-9',
    event_id: 'evt-1',
    kind: 'regenerate',
    started_at: '2026-10-02T15:00:00',
    title: 'Quarterly review',
};

/** Replace `client.post` for one call and return the config it was given. */
async function postConfig(
    call: () => Promise<unknown>,
    answer: unknown = { run_id: 'r2', version: 2 },
): Promise<Config> {
    const original = client.post;
    let seen: Config;
    client.post = (async (_url: string, _body?: unknown, config?: Config) => {
        seen = config;
        return { status: 200, data: answer };
    }) as typeof client.post;
    try {
        await call();
    } finally {
        client.post = original;
    }
    return seen;
}

/** Replace `client.get` for one call: answer with `status`/`data`, and
 *  return the url and config it was given alongside the call's result. */
async function withGet<T>(
    status: number,
    data: unknown,
    call: () => Promise<T>,
): Promise<{ url: string; config: Config; result: T }> {
    const original = client.get;
    let url = '';
    let config: Config;
    client.get = (async (u: string, c?: Config) => {
        url = u;
        config = c;
        return { status, data };
    }) as typeof client.get;
    try {
        const result = await call();
        return { url, config, result };
    } finally {
        client.get = original;
    }
}

describe('Meeting Prep client timeouts', () => {
    const DEFAULT_MS = client.defaults.timeout ?? 0;

    it('the generation timeout outlasts the shared default', () => {
        assert.ok(GENERATION_TIMEOUT_MS > DEFAULT_MS, `${GENERATION_TIMEOUT_MS} <= ${DEFAULT_MS}`);
    });

    it('a one-section rewrite waits as long as a generation', async () => {
        const config = await postConfig(() => rewriteSection('r1', 'talking_points', 'shorter'));
        assert.equal(config?.timeout, GENERATION_TIMEOUT_MS);
    });

    it('a regenerate answers at once, so it keeps the shared default', async () => {
        const config = await postConfig(() => regenerate({ eventId: 'evt-1' }), PENDING);
        assert.equal(config?.timeout, undefined);
    });

    it('opening a brief keeps the shared default', async () => {
        const { config } = await withGet(200, { title: 'x', run_id: 'r1', version: 1 }, () => getPrep('evt-1'));
        assert.equal(config?.timeout, undefined);
    });
});

describe('Meeting Prep background builds', () => {
    it('a 200 is the latest version', async () => {
        const brief = { title: 'Quarterly review', run_id: 'r1', version: 3 };
        const { result, url } = await withGet(200, brief, () => getPrep('evt/1@x'));
        assert.deepEqual(result, { kind: 'ready', data: brief });
        assert.equal(url, '/api/homefeed/meeting-prep/evt%2F1%40x');
    });

    it('a 202 is the build making v1', async () => {
        const { result } = await withGet(202, { ...PENDING, kind: 'first' }, () => getPrep('evt-1'));
        assert.equal(result.kind, 'building');
        assert.equal(result.kind === 'building' && result.build.run_id, 'run-9');
        assert.equal(result.kind === 'building' && result.build.kind, 'first');
    });

    it('a regenerate returns the build, not a brief', async () => {
        const original = client.post;
        client.post = (async () => ({ status: 202, data: PENDING })) as typeof client.post;
        try {
            const build = await regenerate({ eventId: 'evt-1', focusOn: '  pilot  ' });
            assert.equal(build.run_id, 'run-9');
            assert.equal(build.status, 'queued');
            assert.equal(build.kind, 'regenerate');
        } finally {
            client.post = original;
        }
    });

    it('a regenerate that answers without a build is an error', async () => {
        const original = client.post;
        client.post = (async () => ({ status: 200, data: { title: 'x' } })) as typeof client.post;
        try {
            await assert.rejects(regenerate({ eventId: 'evt-1' }));
        } finally {
            client.post = original;
        }
    });

    it('the build status is read from /prep-runs/{run_id}/status', async () => {
        const { result, url } = await withGet(
            200,
            { run_id: 'run 9', status: 'ready', kind: 'regenerate', version: 4, finished_at: '2026-10-02T15:01:40' },
            () => getBuildStatus('run 9'),
        );
        assert.equal(url, '/api/homefeed/prep-runs/run%209/status');
        assert.equal(result.status, 'ready');
        assert.equal(result.version, 4);
    });

    it('the version list carries the build in flight', async () => {
        const { result } = await withGet(
            200,
            { event_id: 'evt-1', latest_version: 2, versions: [], pending: PENDING },
            () => listVersions('evt-1'),
        );
        assert.equal(result.pending?.run_id, 'run-9');
    });

    it('a version list with nothing in flight says so', async () => {
        const { result } = await withGet(200, { event_id: 'evt-1', latest_version: 2, versions: [] }, () =>
            listVersions('evt-1'),
        );
        assert.equal(result.pending, null);
    });
});
