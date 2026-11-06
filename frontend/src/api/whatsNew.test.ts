// What's New client: the calls it makes, the sentences its errors become, and
// drift tests against the routers (the Python is read as text, the way
// looseEndsAdmin.test.ts does it):
//
//   cd <repo>/frontend && node --test "src/api/whatsNew.test.ts"
//
// Pinned here:
//
//   * Every path and body the client sends, captured through the shared axios
//     client (no network): the feed (and a Slack link's note), an open with
//     its source, the admin settings.
//   * The paths are the ones the routers serve, under the /api prefix main.py
//     mounts them with, and the reader router sits outside the copilot gate.
//   * The field lists are complete against the interfaces, and each name is
//     one the server writes; the entry-id rule matches the server's.
//   * The error sentences: the kill switch, a note that is gone, and the
//     server's own sentence passed through as it is.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';
import type { AxiosAdapter } from 'axios';

import type { WhatsNewFeed, WhatsNewItem } from './whatsNew';
import type { WhatsNewSettings } from './whatsNewAdmin';

// `api/whatsNew.ts` imports `./client` extensionless, the way every module in
// this app does. Teach the runner Vite's rule, scoped to our own files.
const resolveExtensionless: ResolveHookSync = (specifier, context, nextResolve) => {
    const parent = context.parentURL ?? '';
    const relative = /^\.{1,2}\//.test(specifier);
    if (parent.includes('/frontend/src/') && relative && !/\.[a-z]+$/i.test(specifier)) {
        return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
};

registerHooks({ resolve: resolveExtensionless });

// The client's request interceptor reads the token from localStorage.
const memory = new Map<string, string>();
Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
        getItem: (k: string) => memory.get(k) ?? null,
        setItem: (k: string, v: string) => void memory.set(k, v),
        removeItem: (k: string) => void memory.delete(k),
    },
});

const { default: client } = await import('./client.ts');
const api = await import('./whatsNew.ts');
const admin = await import('./whatsNewAdmin.ts');

interface Sent {
    method: string;
    url: string;
    body: unknown;
}
const sent: Sent[] = [];
const adapter: AxiosAdapter = async config => {
    sent.push({
        method: (config.method ?? '').toUpperCase(),
        url: config.url ?? '',
        body: typeof config.data === 'string' && config.data ? JSON.parse(config.data) : config.data,
    });
    return { data: {}, status: 200, statusText: 'OK', headers: {}, config };
};
client.defaults.adapter = adapter;

const last = (): Sent => {
    const s = sent.at(-1);
    assert.ok(s, 'no request sent');
    return s;
};

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string => fs.readFileSync(path.join(REPO, relative), 'utf8');

describe('the calls', () => {
    it('reads the feed', async () => {
        await api.fetchWhatsNew();
        assert.deepEqual(last(), { method: 'GET', url: '/api/whats-new', body: undefined });
        // A Slack link's note, so an old one still opens on itself.
        await api.fetchWhatsNew('rel-12');
        assert.deepEqual(last(), { method: 'GET', url: '/api/whats-new?item=rel-12', body: undefined });
        await api.fetchWhatsNew(null);
        assert.equal(last().url, '/api/whats-new');
    });

    it('counts an open with its source', async () => {
        await api.recordOpen('rel-5', 'slack');
        assert.deepEqual(last(), { method: 'POST', url: '/api/whats-new/rel-5/open', body: { source: 'slack' } });
        await api.recordOpen('rel-5', 'try_it');
        assert.deepEqual(last().body, { source: 'try_it' });
    });

    it('reads and saves the admin settings', async () => {
        await admin.fetchWhatsNewSettings();
        assert.deepEqual(last(), { method: 'GET', url: '/api/admin/whats-new/settings', body: undefined });
        const input = { slack_enabled: false, channel: '#announcements', tease_enabled: true };
        await admin.saveWhatsNewSettings(input);
        assert.deepEqual(last(), { method: 'PUT', url: '/api/admin/whats-new/settings', body: input });
    });

    it('knows an entry id the way the server does', () => {
        for (const good of ['1', 'rel-2026-10-27', 'a_b-C9', 'x'.repeat(64)]) assert.equal(api.isEntryId(good), true, good);
        for (const bad of ['', ' ', 'a/b', '../x', 'x'.repeat(65), '-lead', 'é', 12, null]) {
            assert.equal(api.isEntryId(bad), false, String(bad));
        }
    });
});

describe('errors', () => {
    const fail = (status: number, detail?: unknown) => ({ response: { status, data: detail === undefined ? {} : { detail } } });

    it('says the feature is off for the kill switch, or a server without it', () => {
        assert.equal(api.whatsNewError(fail(404, "What's New is not switched on."), 'x'), "What's New is not switched on.");
        assert.equal(api.whatsNewError(fail(404, 'Not Found'), 'x'), api.NOT_SWITCHED_ON);
        assert.equal(api.whatsNewError(fail(404), 'x'), api.NOT_SWITCHED_ON);
        assert.equal(api.isWhatsNewOff(fail(404, 'Not Found')), true);
    });

    it('says an item is gone', () => {
        const gone = fail(404, 'This update is no longer available.');
        assert.equal(api.whatsNewError(gone, 'x'), 'This update is no longer available.');
        assert.equal(api.isWhatsNewOff(gone), false);
    });

    it("passes the server's sentence through as it is", () => {
        assert.equal(api.whatsNewError(fail(422, [{ msg: 'Field required' }]), 'x'), 'Field required');
        assert.equal(
            api.whatsNewError(fail(403, "What's New isn't open to your team."), 'x'),
            "What's New isn't open to your team.",
        );
        assert.equal(
            admin.whatsNewSettingsError(fail(400, "Cubie can't find #x. Create it in Slack, then save again."), 'x'),
            "Cubie can't find #x. Create it in Slack, then save again.",
        );
    });

    it('falls back when there is no sentence', () => {
        assert.equal(api.whatsNewError(new Error('Network Error'), 'Could not load updates.'), 'Could not load updates.');
        assert.equal(api.whatsNewError(fail(500), 'Could not load updates.'), 'Could not load updates.');
        assert.equal(api.isWhatsNewOff(fail(500)), false);
        assert.equal(api.isWhatsNewOff(null), false);
        assert.equal(admin.whatsNewSettingsError(fail(502), 'Could not save.'), 'Could not save.');
    });

});

// Compile-time: each field list names every key of its interface. A key
// added to an interface but not to its list fails `tsc -b`.
type Complete<T, K extends readonly (keyof T)[]> = [Exclude<keyof T, K[number]>] extends [never] ? true : false;
const complete: [
    Complete<WhatsNewFeed, typeof api.WHATS_NEW_FEED_FIELDS>,
    Complete<WhatsNewItem, typeof api.WHATS_NEW_ITEM_FIELDS>,
    Complete<WhatsNewSettings, typeof admin.WHATS_NEW_SETTINGS_FIELDS>,
] = [true, true, true];

describe('the routers serve it', () => {
    // Read inside each test, not while the suite is built: a missing router
    // must fail a test (node --test exits 0 on an error thrown at suite level).
    const ROUTER_PY = () => read('backend/routers/whats_new.py');
    const ADMIN_PY = () => read('backend/routers/whats_new_admin.py');
    const MAIN_PY = () => read('backend/main.py');
    const PACKAGE_PY = () =>
        fs
            .readdirSync(path.join(REPO, 'rubik_core/whats_new'))
            .filter(f => f.endsWith('.py'))
            .map(f => read(`rubik_core/whats_new/${f}`))
            .join('\n');
    const prefix = (source: string): string | undefined => /APIRouter\(\s*prefix="([^"]+)"/.exec(source)?.[1];
    /** A route decorator for `method` and `route` (path parameters by any name). */
    const route = (method: string, routePath: string): RegExp =>
        new RegExp(
            `@router\\.${method}\\(\\s*"${routePath
                .replace(/[.*+?^$()|[\]\\]/g, '\\$&')
                .replace(/\{id\}/g, '\\{[a-z_]+(?::int)?\\}')}"`,
        );

    it('mounts both routers under /api, the reader outside the copilot gate', () => {
        const main = MAIN_PY();
        assert.equal(prefix(ROUTER_PY()), '/whats-new');
        assert.equal(prefix(ADMIN_PY()), '/admin/whats-new');
        assert.match(main, /app\.include_router\(whats_new\.router, prefix="\/api"\)/);
        assert.match(main, /app\.include_router\(whats_new_admin\.router, prefix="\/api"\)/);
        assert.doesNotMatch(main, /core\.include_router\(whats_new/);
    });

    it('serves every reader path the client calls', () => {
        const ROUTER = ROUTER_PY();
        const base = `/api${prefix(ROUTER)}`;
        assert.match(ROUTER, /@router\.get\(\s*"\/?"/);
        assert.equal(api.WHATS_NEW_PATHS.feed, base);
        assert.match(ROUTER, route('post', '/{id}/open'), 'POST /{id}/open');
        assert.equal(api.WHATS_NEW_PATHS.open('rel-9'), `${base}/rel-9/open`);
        // Nothing else: the approval flow left with the System KB drafting.
        const routes = [...ROUTER.matchAll(/@router\.(get|put|post|delete)\(/g)].length;
        assert.equal(routes, 2);
    });

    it('checks an entry id with the same rule as the server', () => {
        const server = /_ENTRY_ID = re\.compile\(r"([^"]+)"\)/.exec(read('rubik_core/whats_new/changelog.py'))?.[1];
        assert.ok(server, 'rubik_core/whats_new/changelog.py never defines _ENTRY_ID');
        assert.equal(api.ENTRY_ID_PATTERN.source, `^${server}$`);
    });

    it('serves the admin settings, admins only', () => {
        const ADMIN = ADMIN_PY();
        assert.match(ADMIN, route('get', '/settings'));
        assert.match(ADMIN, route('put', '/settings'));
        assert.equal(admin.WHATS_NEW_SETTINGS_PATH, `/api${prefix(ADMIN)}/settings`);
        const routes = [...ADMIN.matchAll(/@router\.(get|put|post|delete)\(/g)].length;
        const gated = [...ADMIN.matchAll(/Depends\(admin_required\)/g)].length;
        assert.ok(routes >= 2 && gated >= routes, `${gated} admin gates for ${routes} routes`);
    });

    it('writes every field name the client reads', () => {
        assert.ok(complete.every(Boolean));
        const server = `${ROUTER_PY()}\n${ADMIN_PY()}\n${PACKAGE_PY()}`;
        const lists = [api.WHATS_NEW_FEED_FIELDS, api.WHATS_NEW_ITEM_FIELDS, admin.WHATS_NEW_SETTINGS_FIELDS];
        for (const list of lists) {
            for (const field of list) assert.ok(server.includes(`"${field}"`), `the server never writes "${field}"`);
        }
    });
});
