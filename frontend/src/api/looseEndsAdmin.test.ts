// Drift tests for Admin → Loose Ends (the daily Slack post's switch and
// channel). The Python is read as text, the same way looseEnds.test.ts pins
// the page's vocabularies:
//
//   cd <repo>/frontend && node --test "src/api/looseEndsAdmin.test.ts"
//
// Pinned here:
//
//   * The three paths the client calls are the ones the router serves, under
//     the /api prefix main.py mounts it with.
//   * The field names the card reads are exactly the ones the router and the
//     digest write (GET/PUT shape, last_post, preview), and the PUT body is the
//     one the router's model declares.
//   * The card's sentences: Off / On with the channel, the bot check with the
//     exact invite fix (or the archived channel), the last post or "Nothing
//     posted yet", and the saved message honest about a scan switched off.
//   * Save stays usable with nothing changed while the bot check is not a ✓
//     ("Save and re-check"), and a save clears a preview it would contradict.
//   * The tab is registered in the Admin sections and rendered by Admin.tsx.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

// `api/looseEndsAdmin.ts` imports `./client` extensionless, the way every
// module in this app does. Teach the runner Vite's rule, scoped to our own
// files (an unscoped hook breaks axios's CJS requires).
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
    DIGEST_LAST_POST_FIELDS,
    DIGEST_PREVIEW_FIELDS,
    DIGEST_PREVIEW_PATH,
    DIGEST_SETTINGS_FIELDS,
    DIGEST_SETTINGS_PATH,
    botCheck,
    digestAdminError,
    digestStatusLine,
    displayChannel,
    inviteFix,
    lastPostLine,
    postDateLabel,
    saveButton,
    savedMessage,
} = await import('./looseEndsAdmin.ts');
const { PRODUCT_NAME } = await import('../brand/names.ts');

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string => fs.readFileSync(path.join(REPO, relative), 'utf8');

const ROUTER_PY = read('backend/routers/loose_ends_admin.py');
const DIGEST_PY = read('rubik_core/loop_closure/digest.py');
const SETTINGS_PY = read('rubik_core/loop_closure/digest_settings.py');
const MAIN_PY = read('backend/main.py');
const API_TS = read('frontend/src/api/looseEndsAdmin.ts');
const TAB_TSX = read('frontend/src/pages/admin/LooseEndsTab.tsx');
const SECTIONS_TS = read('frontend/src/pages/adminSections.ts');
const ADMIN_TSX = read('frontend/src/pages/Admin.tsx');

/** The source without its comment lines, so a check reads only the code. */
const codeOnly = (source: string): string =>
    source
        .split('\n')
        .filter(line => !/^\s*(\/\/|\/\*|\*)/.test(line))
        .join('\n');

/** The body of a top-level Python function, up to the next top-level def. */
const pyFunction = (source: string, name: string): string => {
    const m = new RegExp(`^def ${name}\\([\\s\\S]*?(?=^def |^class |^@|(?![\\s\\S]))`, 'm').exec(source);
    assert.ok(m, `def ${name} not found`);
    return m[0];
};

/** The keys of every `return {...}` dict literal in a function body. */
const returnedKeys = (body: string): string[][] =>
    [...body.matchAll(/return \{([\s\S]*?)\n\s*\}/g)].map(m =>
        [...m[1].matchAll(/^\s*"([a-z_]+)":/gm)].map(k => k[1]),
    );

describe('paths', () => {
    const prefix = /APIRouter\(prefix="([^"]+)"/.exec(ROUTER_PY)?.[1];

    it('mounts the router under /api, like sales_admin', () => {
        assert.match(MAIN_PY, /app\.include_router\(loose_ends_admin\.router, prefix="\/api"\)/);
        assert.equal(prefix, '/admin/loose-ends');
    });

    it('calls the GET and PUT the router serves', () => {
        assert.match(ROUTER_PY, /@router\.get\("\/digest"\)/);
        assert.match(ROUTER_PY, /@router\.put\("\/digest"\)/);
        assert.equal(DIGEST_SETTINGS_PATH, `/api${prefix}/digest`);
    });

    it('calls the preview the router serves, as a POST', () => {
        assert.match(ROUTER_PY, /@router\.post\("\/digest\/preview"\)/);
        assert.equal(DIGEST_PREVIEW_PATH, `/api${prefix}/digest/preview`);
        assert.match(API_TS, /client\.post<LooseEndsDigestPreview>\(DIGEST_PREVIEW_PATH\)/);
    });

    it('is admin-only on every endpoint', () => {
        const routes = [...ROUTER_PY.matchAll(/@router\.(get|put|post)\(/g)].length;
        const gated = [...ROUTER_PY.matchAll(/Depends\(admin_required\)/g)].length;
        assert.equal(routes, 3);
        assert.equal(gated, routes);
    });
});

describe('field names', () => {
    it('reads exactly the fields the GET / PUT writes', () => {
        const [keys] = returnedKeys(pyFunction(ROUTER_PY, '_view'));
        assert.deepEqual(keys, [...DIGEST_SETTINGS_FIELDS]);
    });

    it('reads exactly the fields last_post writes', () => {
        const [keys] = returnedKeys(pyFunction(DIGEST_PY, 'last_post'));
        assert.deepEqual(keys, [...DIGEST_LAST_POST_FIELDS]);
    });

    it('reads exactly the fields every preview answer writes', () => {
        const shapes = returnedKeys(pyFunction(DIGEST_PY, 'render_today'));
        assert.ok(shapes.length >= 3, `only found ${shapes.length} preview answers`);
        for (const keys of shapes) assert.deepEqual(keys, [...DIGEST_PREVIEW_FIELDS]);
    });

    it('sends the PUT body the router declares', () => {
        const model = /class DigestSettingsIn\(BaseModel\):([\s\S]*?)\n\n/.exec(ROUTER_PY)?.[1] ?? '';
        assert.match(model, /^\s+enabled: bool$/m);
        assert.match(model, /^\s+channel: str/m);
        assert.match(API_TS, /DIGEST_SETTINGS_PATH, \{ enabled, channel \}\)/);
    });

    it('ships off, pointed at #sales, on both sides of the wire', () => {
        assert.match(SETTINGS_PY, /"enabled": False,\n\s+"channel_name": "#sales",\n\s+"channel_id": None,/);
        assert.match(TAB_TSX, /placeholder="#sales"/);
    });
});

describe('the card says it plainly', () => {
    const base = { enabled: false, channel_name: '#sales', scan_enabled: true };

    it('says Off, or On with the channel and when', () => {
        assert.equal(digestStatusLine(base), 'Off');
        assert.equal(
            digestStatusLine({ ...base, enabled: true }),
            'On — posting to #sales each weekday morning after the scan',
        );
        assert.match(digestStatusLine({ ...base, enabled: true, scan_enabled: false }), /nothing posts/);
        assert.equal(
            digestStatusLine({ ...base, enabled: true, channel_archived: true }),
            'On for #sales, but Slack says the channel is archived, so nothing posts',
        );
    });

    it('says after a save whether anything will post', () => {
        assert.equal(savedMessage(base), 'Saved. The daily post is off; nothing goes to #sales.');
        assert.equal(
            savedMessage({ ...base, enabled: true }),
            'Saved. The next weekday-morning scan posts to #sales.',
        );
        assert.match(
            savedMessage({ ...base, enabled: true, scan_enabled: false }),
            /^Saved\. The post is on for #sales, but the Loose Ends scan is switched off on this server, so nothing posts/,
        );
    });

    it('keeps Save usable to re-check the bot when nothing changed', () => {
        assert.deepEqual(saveButton(true, { bot_in_channel: true }), { enabled: true, label: 'Save' });
        assert.deepEqual(saveButton(false, { bot_in_channel: true }), { enabled: false, label: 'Save' });
        for (const bot of [false, null] as const) {
            assert.deepEqual(saveButton(false, { bot_in_channel: bot }), {
                enabled: true,
                label: 'Save and re-check',
            });
        }
        assert.deepEqual(saveButton(false, { bot_in_channel: true, channel_archived: true }), {
            enabled: true,
            label: 'Save and re-check',
        });
        assert.match(TAB_TSX, /const save = saveButton\(dirty, data\);/);
        assert.match(TAB_TSX, /disabled=\{saving \|\| !save\.enabled\}/);
        assert.match(TAB_TSX, /\{save\.label\}/);
    });

    it('clears a preview the save would contradict', () => {
        const save = /const handleSave = async \(\) => \{([\s\S]*?)\n {4}\};/.exec(TAB_TSX)?.[1] ?? '';
        assert.match(save, /setPreview\(null\);/);
        assert.match(save, /setPreviewError\(null\);/);
        assert.match(save, /setSaved\(savedMessage\(fresh\)\);/);
    });

    it('shows a preview caveat even when the post would go out', () => {
        assert.match(TAB_TSX, /preview\.would_post && preview\.reason \?/);
    });

    it('names the invite fix when the bot is not in the channel', () => {
        assert.deepEqual(botCheck({ bot_in_channel: true, channel_name: '#sales' }), {
            ok: true,
            text: `${PRODUCT_NAME} is in #sales`,
        });
        assert.deepEqual(botCheck({ bot_in_channel: false, channel_name: '#sales' }), {
            ok: false,
            text: `${PRODUCT_NAME} isn't in #sales yet — type /invite @${PRODUCT_NAME} in #sales, then Save to re-check`,
        });
        assert.equal(botCheck({ bot_in_channel: null, channel_name: '#sales' }).ok, null);
        assert.deepEqual(botCheck({ bot_in_channel: true, channel_archived: true, channel_name: '#sales' }), {
            ok: false,
            text: 'Slack says #sales is archived — pick another channel and Save',
        });
    });

    it('agrees with the server on the invite sentence', () => {
        // Both sides build it from PRODUCT_NAME (the brand modules, which
        // backend/tests/test_brand.py keeps equal), so render the server's
        // template with the frontend's name and compare the two sentences.
        const template = /_INVITE_FIX = f"([^"]+)"/.exec(SETTINGS_PY)?.[1];
        assert.ok(template, 'digest_settings.py no longer defines _INVITE_FIX');
        const server = template
            .replace('{PRODUCT_NAME}', PRODUCT_NAME)
            .replace('{{name}}', '#sales');
        assert.equal(inviteFix('#sales'), server);
        assert.ok(botCheck({ bot_in_channel: false, channel_name: '#sales' }).text.includes(server));
        assert.doesNotMatch(codeOnly(API_TS), /@Cubie\b|`Cubie /);
    });

    it('shows the last post with its date and count, or that there is none', () => {
        assert.equal(lastPostLine(null), 'Nothing posted yet');
        const line = lastPostLine({
            local_date: '2026-10-07',
            posted_at: '2026-10-07T12:11:00',
            item_count: 1,
            channel_name: '#sales',
        });
        assert.equal(line, `Last post: ${postDateLabel('2026-10-07')} to #sales — 1 open item`);
        assert.match(postDateLabel('2026-10-07'), /Oct 7/);
        assert.match(lastPostLine({ local_date: '2026-10-08', posted_at: '', item_count: 4, channel_name: '#sales' }), /4 open items$/);
    });

    it('shows what was typed the way Slack names it', () => {
        assert.equal(displayChannel(' #Sales '), '#sales');
        assert.equal(displayChannel('sales'), '#sales');
        assert.equal(displayChannel('C0123ABCD'), 'C0123ABCD');
        assert.equal(displayChannel('  '), '');
        // The server's rule: an id is typed bare, with a digit in it.
        assert.equal(displayChannel('#CUSTOMERSUCCESS'), '#customersuccess');
        assert.equal(displayChannel('CUSTOMERSUCCESS'), '#customersuccess');
        assert.equal(displayChannel('#GROWTH1'), '#growth1');
        assert.equal(displayChannel('#G0PRIV8TE'), '#g0priv8te');
        assert.match(SETTINGS_PY, /not raw\.startswith\("#"\)/);
        assert.match(SETTINGS_PY, /any\(c\.isdigit\(\) for c in bare\)/);
    });

    it('shows the server sentence of a refused save as it is', () => {
        const sentence = 'Invite Cubie to #sales first: type /invite @Cubie in #sales, then turn this on.';
        assert.equal(digestAdminError({ response: { status: 400, data: { detail: sentence } } }, 'x'), sentence);
        assert.equal(digestAdminError(new Error('Network Error'), 'Could not save.'), 'Could not save.');
    });

    it('explains what the post contains and that a quiet day posts nothing', () => {
        assert.match(TAB_TSX, /Loose Ends — daily Slack post/);
        assert.match(TAB_TSX, /never includes an email subject/);
        assert.match(TAB_TSX, /A quiet day \(nothing open and everyone checked\) posts nothing\./);
        assert.match(TAB_TSX, /If\s+someone could not be checked, the post goes out to say so\./);
        assert.match(TAB_TSX, /Preview today&apos;s post/);
        assert.match(TAB_TSX, /whitespace-pre-wrap/);
    });

    it('derives the spinner and sets state only inside the async load', () => {
        assert.match(TAB_TSX, /if \(data === null && loadError === null\)/);
        assert.match(TAB_TSX, /let cancelled = false;/);
        assert.doesNotMatch(TAB_TSX, /setLoading\(/);
    });
});

describe('the tab is wired', () => {
    it('is an Admin section and renders the tab', () => {
        assert.match(SECTIONS_TS, /\{ key: 'looseends', label: 'Loose Ends', Icon: Send \}/);
        assert.match(ADMIN_TSX, /\{activeTab === 'looseends' && <LooseEndsTab \/>\}/);
    });
});
