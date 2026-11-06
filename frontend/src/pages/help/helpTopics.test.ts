// Regression tests for the Help topic registry.
//
// Runs on Node's built-in test runner against Node's built-in TypeScript
// stripping (`npm test`; there is no frontend test framework),
// which is why helpTopics.ts is a leaf module with no imports:
//
//   cd <repo>/frontend && node --test "src/pages/help/helpTopics.test.ts"
//
// What is pinned here:
//
//   * Panel order. The Help panel opens a heading whenever the group changes,
//     so a topic filed out of order would reopen its heading further down.
//   * Gates fail closed. A topic behind a gate shows only on an explicit
//     `true`; a verdict that has not arrived yet must hide it.
//   * Old links still land. Chat wrote /help#<id> until topics became pages,
//     and those answers are still in people's chat history.
//   * The copy says Cubie. No topic, heading, page or file name under
//     pages/help names an old product or company (the shared matcher in
//     src/testing/legacyBrand.ts, pinned to the repo's legacy-brand scan), and
//     the Chrome topic lives at its new id.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';
import { oldNames, oldNamesUnder } from '../../testing/legacyBrand.ts';
import {
    HELP_GROUPS,
    HELP_TOPICS,
    canReadHelpTopic,
    findHelpTopic,
    groupedHelpTopics,
    helpNeighbours,
    helpPath,
    legacyHelpHashTarget,
    matchesHelpQuery,
    visibleHelpTopics,
} from './helpTopics.ts';
import type { HelpAccess } from './helpTopics.ts';

const ALL_OPEN: HelpAccess = {
    xray: true,
    crm_sync: true,
    scheduled_tasks: true,
    todos: true,
    proposals: true,
    voice_commands: true,
    insights: true,
    whats_new: true,
    chrome_extension: true,
};

const HELP_DIR = fileURLToPath(new URL('.', import.meta.url));

describe('HELP_TOPICS', () => {
    it('has unique ids that are safe as a URL segment', () => {
        const ids = HELP_TOPICS.map((t) => t.id);
        assert.equal(new Set(ids).size, ids.length);
        for (const id of ids) assert.match(id, /^[a-z][a-z-]*$/);
    });

    it('files every topic under a real heading', () => {
        const keys = new Set(HELP_GROUPS.map((g) => g.key));
        for (const t of HELP_TOPICS) assert.ok(keys.has(t.group), `${t.id} → ${t.group}`);
    });

    it('keeps each heading’s topics contiguous and in heading order', () => {
        const order = HELP_GROUPS.map((g) => g.key);
        let last = -1;
        for (const t of HELP_TOPICS) {
            const at = order.indexOf(t.group);
            assert.ok(at >= last, `${t.id} reopens "${t.group}" after a later heading`);
            last = at;
        }
    });

    it('gives every topic a label, a title and a one-line summary', () => {
        for (const t of HELP_TOPICS) {
            assert.ok(t.label.length > 0 && t.label.length <= 24, `${t.id} label "${t.label}"`);
            assert.ok(t.title.length > 0, t.id);
            assert.ok(t.summary.length > 20 && t.summary.length <= 140, `${t.id} summary length`);
        }
    });
});

describe('gates', () => {
    const gated = HELP_TOPICS.filter((t) => t.requires);

    it('hides every gated topic when no verdict has arrived', () => {
        const visible = new Set(visibleHelpTopics({}).map((t) => t.id));
        assert.ok(gated.length > 0);
        for (const t of gated) assert.ok(!visible.has(t.id), t.id);
    });

    it('shows a gated topic only on an explicit true', () => {
        for (const t of gated) {
            const gate = t.requires!;
            assert.equal(canReadHelpTopic(t, { [gate]: false }), false);
            assert.equal(canReadHelpTopic(t, { [gate]: true }), true);
        }
    });

    it('shows every topic to a reader with every gate open', () => {
        assert.equal(visibleHelpTopics(ALL_OPEN).length, HELP_TOPICS.length);
    });

    it('drops a heading whose every topic is gated away', () => {
        const closed = groupedHelpTopics({});
        for (const g of closed) assert.ok(g.topics.length > 0, g.group.title);
    });
});

describe('helpNeighbours', () => {
    it('skips topics the reader cannot see', () => {
        const visible = visibleHelpTopics({});
        for (let i = 1; i < visible.length - 1; i++) {
            const { prev, next } = helpNeighbours(visible[i].id, {});
            assert.equal(prev?.id, visible[i - 1].id);
            assert.equal(next?.id, visible[i + 1].id);
        }
    });

    it('has no previous for the first topic and no next for the last', () => {
        const first = HELP_TOPICS[0].id;
        const last = HELP_TOPICS[HELP_TOPICS.length - 1].id;
        assert.equal(helpNeighbours(first, ALL_OPEN).prev, null);
        assert.equal(helpNeighbours(last, ALL_OPEN).next, null);
    });
});

describe('legacyHelpHashTarget', () => {
    it('sends an old anchor link to the topic page', () => {
        assert.equal(legacyHelpHashTarget('#assets'), helpPath('assets'));
        assert.equal(legacyHelpHashTarget('#smart-teams'), '/help/smart-teams');
    });

    it('leaves an unknown or empty anchor on the Help home', () => {
        assert.equal(legacyHelpHashTarget(''), null);
        assert.equal(legacyHelpHashTarget('#nope'), null);
    });
});

describe('matchesHelpQuery', () => {
    const chat = HELP_TOPICS.find((t) => t.id === 'chat')!;

    it('matches the label, title, summary or heading, ignoring case', () => {
        assert.ok(matchesHelpQuery(chat, 'CHAT'));
        assert.ok(matchesHelpQuery(chat, 'ask cubie'));
        assert.ok(matchesHelpQuery(chat, '  '));
        assert.ok(!matchesHelpQuery(chat, 'scorecard'));
    });
});

describe('Help says Cubie', () => {
    it('names no old product or company in a topic or a heading', () => {
        const bad = [
            ...HELP_GROUPS.map((g) => ({ where: `group ${g.key}`, text: g.title })),
            ...HELP_TOPICS.map((t) => ({ where: t.id, text: [t.id, t.title, t.label, t.summary].join(' | ') })),
        ].flatMap(({ where, text }) => oldNames(text).map((name) => `${where}: ${name}`));
        assert.deepEqual(bad, []);
    });

    it('calls the chat heading Ask Cubie', () => {
        assert.equal(HELP_GROUPS.find((g) => g.key === 'ask')?.title, 'Ask Cubie');
    });

    it('serves the Chrome topic at cubie-for-chrome', () => {
        const chrome = findHelpTopic('cubie-for-chrome');
        assert.ok(chrome, 'no topic with id cubie-for-chrome');
        assert.equal(chrome.title, 'Cubie for Chrome');
        assert.equal(chrome.label, 'Cubie for Chrome');
        assert.equal(chrome.requires, 'chrome_extension');
    });

    it('leaves no old product or company name in any Help file or file name', () => {
        assert.deepEqual(oldNamesUnder(HELP_DIR), []);
    });
});
