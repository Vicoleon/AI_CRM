// Tests for the Meeting Prep section registry and version helpers.
//
//   cd <repo>/frontend && node --test "src/pages/meetingPrep/**/*.test.ts"
//
// The registry is re-derived FROM the Python source rather than restated here:
// the page and the server must agree on every key, its shape and its two
// labels, and a constant in this file could drift exactly as easily as the
// page's own copy.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

import {
    LEADING_MARKER,
    SECTIONS,
    cleanStakeholders,
    describeChange,
    editedSectionKeys,
    formatVersionWhen,
    isGeneration,
    isSectionKey,
    listToText,
    normalizeProseContent,
    parseStakeholderMap,
    provenanceLabel,
    sameList,
    sameStakeholders,
    sectionLabel,
    textToList,
    versionMenuLabel,
} from './sections.ts';

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..', '..');
const REPO = path.resolve(FRONTEND, '..');

describe('SECTIONS mirrors rubik_core/meeting_prep/sections.py', () => {
    const py = fs.readFileSync(path.join(REPO, 'rubik_core/meeting_prep/sections.py'), 'utf8');
    const specs = [
        ...py.matchAll(
            /SectionSpec\(\s*key="([^"]+)",\s*kind="([^"]+)",\s*external_label="([^"]+)",\s*internal_label="([^"]+)"/g,
        ),
    ].map(m => ({ key: m[1], kind: m[2], externalLabel: m[3], internalLabel: m[4] }));

    it('reads the Python registry (guards the regex itself)', () => {
        assert.ok(specs.length >= 10, `only ${specs.length} SectionSpec entries parsed`);
    });

    it('has the same keys, kinds and labels, in the same order', () => {
        assert.deepEqual(
            SECTIONS.map(s => ({ ...s })),
            specs,
        );
    });
});

describe('sectionLabel', () => {
    it('follows the persona', () => {
        assert.equal(sectionLabel('client_profile', false), 'Client Profile');
        assert.equal(sectionLabel('client_profile', true), 'Relationship Context');
        assert.equal(sectionLabel('discovery_questions', true), 'Key Questions');
        assert.equal(sectionLabel('stakeholder_map', false), 'Stakeholder Map');
        assert.equal(sectionLabel('stakeholder_map', true), 'Roles & Dynamics');
    });

    it('reads an unknown key as words, and a missing one as "a section"', () => {
        assert.equal(sectionLabel('brand_new_thing', false), 'Brand New Thing');
        assert.equal(sectionLabel(null, false), 'a section');
    });

    it('knows which keys are editable', () => {
        assert.ok(isSectionKey('talking_points'));
        assert.ok(!isSectionKey('attendee_intel'));
        assert.ok(!isSectionKey('source_evidence'));
        assert.ok(!isSectionKey(''));
    });
});

describe('listToText / textToList', () => {
    it('round-trips a clean list', () => {
        const items = ['Open with the Q3 numbers', 'Ask about **pricing**', 'Agree next steps'];
        assert.deepEqual(textToList(listToText(items)), items);
    });

    it('one item per line, trimmed, blank lines dropped', () => {
        assert.deepEqual(textToList('  first  \n\n second\r\n   \nthird'), ['first', 'second', 'third']);
        assert.equal(listToText(['  a ', '', '   ', 'b']), 'a\nb');
    });

    it('splits a multi-line item into one line each, markers stripped as the server does', () => {
        // The shapes an old brief stores as ONE item: a bulleted block the
        // full brief's splitter left whole, a nested list `_to_str` joined.
        const items = ['* a\n* b', '• first\n  • second', 'Open with Q3', '- x\r\n2. y\n3) z', 'plain\ncontinued'];
        assert.equal(listToText(items), 'a\nb\nfirst\nsecond\nOpen with Q3\nx\ny\nz\nplain\ncontinued');
    });

    it('shows exactly what Save stores: untouched multi-line items are never folded into one line', () => {
        const items = ['* call **Ana** first\n* then Bo', 'Agree next steps'];
        const seed = listToText(items);
        // The reader edits only the last line...
        const draft = seed.replace('Agree next steps', 'Agree next steps by Friday');
        const saved = textToList(draft);
        assert.deepEqual(saved, ['call **Ana** first', 'then Bo', 'Agree next steps by Friday']);
        // ...and every line they were shown is an item, verbatim.
        assert.deepEqual(textToList(seed), seed.split('\n'));
        assert.ok(saved.every(item => !/[\r\n]/.test(item) && !item.includes(' * ')), saved.join(' | '));
    });

    it('leaves a single-line item exactly as stored, marker and all', () => {
        assert.equal(listToText(['- keep my dash', '1. and my number']), '- keep my dash\n1. and my number');
    });

    it('LEADING_MARKER mirrors _LEADING_MARKER in rewrite.py', () => {
        const py = fs.readFileSync(path.join(REPO, 'rubik_core/meeting_prep/rewrite.py'), 'utf8');
        const m = /^_LEADING_MARKER = re\.compile\(r"([^"]+)"\)/m.exec(py);
        assert.ok(m, '_LEADING_MARKER not found in rewrite.py');
        assert.equal(LEADING_MARKER.source, m[1]);
    });

    it('treats null as empty', () => {
        assert.equal(listToText(null), '');
        assert.deepEqual(textToList(undefined), []);
    });

    it('sameList compares in order', () => {
        assert.ok(sameList(['a', 'b'], ['a', 'b']));
        assert.ok(!sameList(['a', 'b'], ['b', 'a']));
        assert.ok(!sameList(['a'], ['a', 'b']));
    });
});

describe('normalizeProseContent', () => {
    it('turns a JSON-array string into bullets', () => {
        assert.equal(normalizeProseContent('["one", "two"]'), '- one\n- two');
    });

    it('leaves prose (and bracketed prose that is not JSON) alone', () => {
        assert.equal(normalizeProseContent('Plain **text**'), 'Plain **text**');
        assert.equal(normalizeProseContent('[not json]'), '[not json]');
        assert.equal(normalizeProseContent(null), '');
    });
});

describe('parseStakeholderMap', () => {
    it('reads the JSON array the model and the editor write', () => {
        const raw = JSON.stringify([{ name: 'Ana Ruiz', email: 'ana@client.com', role: 'CFO', context: 'Signs off.' }]);
        assert.deepEqual(parseStakeholderMap(raw), {
            kind: 'people',
            people: [{ name: 'Ana Ruiz', email: 'ana@client.com', role: 'CFO', context: 'Signs off.' }],
        });
    });

    it('fills missing fields with empty strings and skips non-objects', () => {
        const parsed = parseStakeholderMap(JSON.stringify([{ name: 'Bo' }, 'junk', null]));
        assert.deepEqual(parsed, { kind: 'people', people: [{ name: 'Bo', email: '', role: '', context: '' }] });
    });

    it('reads the older pipe-separated shape', () => {
        const parsed = parseStakeholderMap(
            'ana@client.com: Ana Ruiz is the CFO of Client. She signs off. | bo@client.com: Ops lead',
        );
        assert.equal(parsed.kind, 'people');
        if (parsed.kind !== 'people') return;
        assert.equal(parsed.people.length, 2);
        assert.equal(parsed.people[0].name, 'Ana Ruiz');
        assert.equal(parsed.people[0].role, 'CFO of Client');
        assert.equal(parsed.people[0].email, 'ana@client.com');
        assert.equal(parsed.people[1].name, 'bo@client.com');
    });

    it('falls back to markdown text', () => {
        assert.deepEqual(parseStakeholderMap('**Ana** runs finance.'), { kind: 'text', text: '**Ana** runs finance.' });
    });

    it('empty is an empty list of people', () => {
        assert.deepEqual(parseStakeholderMap(''), { kind: 'people', people: [] });
        assert.deepEqual(parseStakeholderMap('[]'), { kind: 'people', people: [] });
    });
});

describe('cleanStakeholders / sameStakeholders', () => {
    const ana = { name: ' Ana ', email: 'ana@x.example', role: 'CFO ', context: '' };
    const blank = { name: ' ', email: '', role: '', context: '  ' };

    it('trims every field and drops all-blank rows, as the server does', () => {
        assert.deepEqual(cleanStakeholders([ana, blank]), [
            { name: 'Ana', email: 'ana@x.example', role: 'CFO', context: '' },
        ]);
    });

    it('ignores whitespace and blank rows when comparing', () => {
        assert.ok(sameStakeholders([ana, blank], [{ name: 'Ana', email: 'ana@x.example', role: 'CFO', context: '' }]));
        assert.ok(!sameStakeholders([ana], [{ ...ana, role: 'CEO' }]));
    });

    it('drops extra keys (a row id) from what is saved', () => {
        const withId = { ...ana, rowId: 'r1' } as typeof ana & { rowId: string };
        assert.deepEqual(Object.keys(cleanStakeholders([withId])[0]).sort(), ['context', 'email', 'name', 'role']);
    });
});

describe('describeChange', () => {
    it('names each kind of change', () => {
        assert.equal(describeChange({ version: 1, change_kind: 'generated' }, false), 'First version');
        assert.equal(describeChange({ version: 3, change_kind: 'generated' }, false), 'Generated');
        assert.equal(describeChange({ version: 2, change_kind: 'regenerated' }, false), 'Regenerated');
        assert.equal(
            describeChange({ version: 4, change_kind: 'section_edited', changed_section: 'talking_points' }, false),
            'You edited Talking Points',
        );
        assert.equal(
            describeChange(
                {
                    version: 5,
                    change_kind: 'section_rewritten',
                    changed_section: 'discovery_questions',
                    change_note: 'shorter',
                },
                false,
            ),
            'Cubie rewrote Discovery Questions — “shorter”',
        );
        assert.equal(describeChange({ version: 6, change_kind: 'restored', base_version: 2 }, false), 'Restored v2');
    });

    it('uses the persona label', () => {
        assert.equal(
            describeChange({ version: 5, change_kind: 'section_rewritten', changed_section: 'discovery_questions' }, true),
            'Cubie rewrote Key Questions',
        );
    });

    it('shortens a long instruction', () => {
        const out = describeChange(
            { version: 2, change_kind: 'section_rewritten', changed_section: 'next_steps', change_note: 'x'.repeat(200) },
            false,
        );
        assert.ok(out.length < 120, out);
        assert.ok(out.endsWith('…”'));
    });

    it('copes with missing data', () => {
        assert.equal(describeChange(null, false), 'Briefing');
        assert.equal(describeChange({ version: 1 }, false), 'First version');
        assert.equal(describeChange({ version: 4, change_kind: 'something_new' }, false), 'Version 4');
        assert.equal(describeChange({ version: 3, change_kind: 'restored' }, false), 'Restored an earlier version');
    });

    it('isGeneration splits generations from edits', () => {
        assert.ok(isGeneration('generated'));
        assert.ok(isGeneration('regenerated'));
        assert.ok(isGeneration(null));
        assert.ok(!isGeneration('section_edited'));
        assert.ok(!isGeneration('restored'));
    });
});

describe('provenance', () => {
    it('labels a hand edit and an assistant rewrite', () => {
        assert.equal(provenanceLabel({ origin: 'edited', version: 4 }), 'Edited by you · v4');
        assert.equal(provenanceLabel({ origin: 'rewritten', version: 5, instruction: 'shorter' }), 'Rewritten by Cubie · v5');
        assert.equal(provenanceLabel(null), null);
        assert.equal(provenanceLabel({ origin: 'something' }), null);
    });

    it('editedSectionKeys counts only hand edits', () => {
        assert.deepEqual(
            editedSectionKeys({
                talking_points: { origin: 'edited', version: 3 },
                next_steps: { origin: 'rewritten', version: 4 },
                client_profile: { origin: 'edited', version: 5 },
            }).sort(),
            ['client_profile', 'talking_points'],
        );
        assert.deepEqual(editedSectionKeys(undefined), []);
    });
});

describe('versionMenuLabel', () => {
    it('says latest, or where in the history you are', () => {
        assert.equal(versionMenuLabel(5, 5), 'v5 · latest');
        assert.equal(versionMenuLabel(3, 5), 'v3 of 5');
        assert.equal(versionMenuLabel(1, null), 'v1 · latest');
        assert.equal(versionMenuLabel(null, 5), 'Versions');
    });
});

describe('formatVersionWhen', () => {
    it('reads a naive API timestamp as UTC (same instant as with a Z)', () => {
        assert.equal(formatVersionWhen('2026-10-01T19:14:00'), formatVersionWhen('2026-10-01T19:14:00Z'));
        assert.equal(formatVersionWhen('2026-10-01T19:14:00.123456'), formatVersionWhen('2026-10-01T19:14:00.123Z'));
    });

    it('matches the viewer-local rendering of that UTC instant', () => {
        const expected = new Date(Date.UTC(2026, 9, 1, 19, 14)).toLocaleString(undefined, {
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit',
        });
        assert.equal(formatVersionWhen('2026-10-01T19:14:00', new Date(Date.UTC(2026, 9, 2, 12))), expected);
    });

    it('adds the year only when it is not this year', () => {
        const now = new Date(Date.UTC(2026, 9, 2, 12));
        assert.ok(!formatVersionWhen('2026-06-01T12:00:00', now).includes('2026'));
        assert.ok(formatVersionWhen('2025-06-01T12:00:00', now).includes('2025'));
    });

    it('falls back on empty or unparseable input', () => {
        assert.equal(formatVersionWhen(null), '—');
        assert.equal(formatVersionWhen('not a date', undefined, ''), '');
    });
});
