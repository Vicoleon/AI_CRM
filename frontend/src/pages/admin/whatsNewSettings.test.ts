// Admin → What's New: the Slack channel check with its fix, and the Save
// button's re-check rule. Also pins the tab's wiring: it is an Administration
// section (moved from Settings), Settings no longer lists it,
// and an admin's old /settings?tab=whats-new link lands on it.
//
//   cd <repo>/frontend && node --test "src/pages/admin/whatsNewSettings.test.ts"
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

import type { WhatsNewSettings } from '../../api/whatsNewAdmin';
import { PRODUCT_NAME } from '../../brand/names.ts';
import {
    channelCheck,
    channelName,
    formFromSettings,
    formProblem,
    isDirty,
    saveButton,
    savedMessage,
    slackStatusLine,
    toInput,
} from './whatsNewSettings.ts';

const SRC = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const read = (relative: string): string => fs.readFileSync(path.join(SRC, relative), 'utf8');

/** The source without its comment lines, so a check reads only the code. */
const codeOnly = (source: string): string =>
    source
        .split('\n')
        .filter(line => !/^\s*(\/\/|\/\*|\*)/.test(line))
        .join('\n');

/** Every '…', "…" and `…` literal in the code, `${…}` holes included. */
const stringLiterals = (code: string): string[] =>
    code.match(/'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`/g) ?? [];

/** Literals that spell the product name out instead of building it from PRODUCT_NAME. */
const hardCodedName = (code: string): string[] =>
    stringLiterals(code).filter(literal => new RegExp(`\\b${PRODUCT_NAME}\\b`).test(literal));

const saved = (over: Partial<WhatsNewSettings> = {}): WhatsNewSettings => ({
    slack_enabled: false,
    channel_name: '#announcements',
    channel_id: null,
    bot_in_channel: null,
    channel_archived: false,
    tease_enabled: true,
    updated_by: null,
    updated_at: null,
    ...over,
});

describe('the form', () => {
    it('starts from what is saved and is not dirty', () => {
        const s = saved();
        assert.equal(isDirty(formFromSettings(s), s), false);
        assert.deepEqual(toInput(formFromSettings(s)), {
            slack_enabled: false,
            channel: '#announcements',
            tease_enabled: true,
        });
    });

    it('carries no approvers: the notes come from RubikSoft', () => {
        assert.deepEqual(Object.keys(toInput(formFromSettings(saved()))).sort(), [
            'channel',
            'slack_enabled',
            'tease_enabled',
        ]);
    });

    it('compares the channel the way the server stores it', () => {
        const s = saved();
        assert.equal(isDirty({ ...formFromSettings(s), channel: ' Announcements ' }, s), false);
        assert.equal(isDirty({ ...formFromSettings(s), channel: '#general' }, s), true);
        assert.equal(isDirty({ ...formFromSettings(s), tease_enabled: false }, s), true);
        assert.equal(channelName('C0123ABCD'), 'C0123ABCD');
        assert.equal(channelName('#'), '');
    });

    it('needs a channel to post in', () => {
        const s = saved();
        assert.equal(formProblem(formFromSettings(s)), null);
        assert.match(formProblem({ ...formFromSettings(s), slack_enabled: true, channel: ' ' }) ?? '', /channel/);
    });
});

describe('Slack', () => {
    it('says the status plainly', () => {
        assert.match(slackStatusLine(saved()), /^Off/);
        assert.equal(
            slackStatusLine(saved({ slack_enabled: true, bot_in_channel: true })),
            'On — new updates go out in #announcements in one post each weekday morning',
        );
        assert.match(slackStatusLine(saved({ slack_enabled: true, channel_archived: true })), /archived/);
        assert.match(slackStatusLine(saved({ slack_enabled: true, bot_in_channel: false })), /isn't in the channel/);
    });

    it('gives the invite fix', () => {
        assert.deepEqual(channelCheck(saved({ bot_in_channel: true })), {
            ok: true,
            text: `${PRODUCT_NAME} is in #announcements`,
        });
        assert.equal(
            channelCheck(saved()).text,
            `Create #announcements in Slack and type /invite @${PRODUCT_NAME} there, then Save`,
        );
        assert.ok(
            channelCheck(saved({ bot_in_channel: false })).text.includes(
                `type /invite @${PRODUCT_NAME} in #announcements`,
            ),
        );
        assert.match(channelCheck(saved({ channel_archived: true })).text, /archived/);
    });

    it('agrees with the server on the invite command, both built from PRODUCT_NAME', () => {
        const server = fs.readFileSync(path.join(SRC, '..', '..', 'rubik_core/whats_new/settings.py'), 'utf8');
        assert.match(server, /f"\/invite @\{PRODUCT_NAME\} in that channel\."/);
        assert.doesNotMatch(codeOnly(read('pages/admin/whatsNewSettings.ts')), /@Cubie\b|`Cubie /);
    });

    it('spells the product name nowhere in a string: every sentence takes it from PRODUCT_NAME', () => {
        assert.deepEqual(hardCodedName(codeOnly(read('pages/admin/whatsNewSettings.ts'))), []);
        assert.match(
            slackStatusLine(saved({ slack_enabled: true, bot_in_channel: false })),
            new RegExp(`but ${PRODUCT_NAME} isn't in the channel`),
        );
    });

    it('the product-name guard catches the name mid-string, in every kind of literal', () => {
        const name = PRODUCT_NAME;
        for (const snippet of [
            `const a = \`On, but ${name} isn't in the channel\`;`,
            `const b = 'type /invite @${name} there';`,
            `const c = "${name} is in #general";`,
            `const d = \`\${channel} needs ${name}\`;`,
        ]) {
            assert.equal(hardCodedName(snippet).length, 1, snippet);
        }
        assert.deepEqual(hardCodedName('const e = `${PRODUCT_NAME} is in ${name}`;'), []);
    });

    it('keeps Save usable as the re-check while posting is on and the check is not a ✓', () => {
        assert.deepEqual(saveButton(true, saved()), { enabled: true, label: 'Save' });
        assert.deepEqual(saveButton(false, saved()), { enabled: false, label: 'Save' });
        assert.deepEqual(saveButton(false, saved({ slack_enabled: true, bot_in_channel: false })), {
            enabled: true,
            label: 'Save and re-check',
        });
        assert.deepEqual(saveButton(false, saved({ slack_enabled: true, bot_in_channel: true })), {
            enabled: false,
            label: 'Save',
        });
    });

    it('says what a save did', () => {
        assert.match(savedMessage(saved()), /nothing is posted in Slack until posting is on/);
        assert.equal(
            savedMessage(saved({ slack_enabled: true })),
            'Saved. New updates go out in #announcements in one post each weekday morning.',
        );
    });
});

describe('wiring', () => {
    const settings = read('pages/Settings.tsx');
    const admin = read('pages/Admin.tsx');
    const sections = read('pages/adminSections.ts');
    const tab = read('pages/admin/WhatsNewTab.tsx');

    it('is an Administration section, next to Loose Ends, and Admin renders it', () => {
        assert.match(sections, /\{ key: 'whatsnew', label: "What's New", Icon: Megaphone \}/);
        const looseEnds = sections.indexOf("key: 'looseends'");
        const whatsNew = sections.indexOf("key: 'whatsnew'");
        assert.ok(looseEnds > 0 && whatsNew > looseEnds, 'What\'s New sits in the Admin group after Loose Ends');
        assert.ok(sections.lastIndexOf("title: '", whatsNew) === sections.lastIndexOf("title: '", looseEnds), 'same group');
        assert.match(admin, /import \{ WhatsNewTab \} from '\.\/admin\/WhatsNewTab';/);
        assert.match(admin, /\{activeTab === 'whatsnew' && <WhatsNewTab \/>\}/);
    });

    it('is no longer a Settings tab, and an admin\'s old link lands on the Admin section', () => {
        assert.doesNotMatch(settings, /type TabKey = [^;]*'whats-new'/);
        assert.doesNotMatch(settings, /const TAB_KEYS: TabKey\[\] = \[[^\]]*'whats-new'/);
        assert.doesNotMatch(settings, /WhatsNewSettingsTab/);
        assert.match(
            settings,
            /if \(requestedTab === 'whats-new' && isAdmin\) return <Navigate to="\/admin\?tab=whatsnew" replace \/>;/,
        );
        for (const gone of ['pages/settings/WhatsNewSettingsTab.tsx', 'pages/settings/WhatsNewSettingsForm.tsx']) {
            assert.equal(fs.existsSync(path.join(SRC, gone)), false, `${gone} should be gone`);
        }
    });

    it('needs no refusal of its own: the Admin page is admin-only', () => {
        assert.doesNotMatch(tab, /useAuth|isAdmin/);
        assert.match(admin, /if \(!isAdmin\) \{/);
        assert.match(tab, /<WhatsNewSettingsForm \/>/);
        assert.match(tab, /These settings apply to everyone in Cubie\./);
    });

    it('says the switch posts once each weekday morning, and asks for no approvers', () => {
        assert.match(tab, /label="Post new updates in Slack each weekday morning"/);
        assert.match(tab, /posts the release notes published since the last post in the channel as one message/);
        assert.doesNotMatch(tab, /approver/i);
    });
});
