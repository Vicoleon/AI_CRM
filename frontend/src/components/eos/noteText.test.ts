// Tests for the EOS notes composer's text helpers.
//
// Runs on Node's built-in test runner + TypeScript stripping (`npm test`, no
// framework) — `noteText.ts` is a leaf module with no imports so it loads
// without a bundler:
//
//   cd <repo>/frontend && node --test "src/components/eos/**/*.test.ts"
//
// What's pinned here is the behaviour a bulleted note depends on: Enter
// continues the list, Enter on an empty bullet ENDS it (without that escape
// hatch you can never get back to a paragraph), and a note round-trips —
// what you type as bullets reads back as bullets.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    composerRows,
    continueList,
    hasList,
    listMarker,
    noteBlocks,
    toggleBullets,
} from './noteText.ts';

describe('listMarker', () => {
    it('recognises the three bullet glyphs', () => {
        for (const glyph of ['-', '*', '•']) {
            const parsed = listMarker(`${glyph} ship it`);
            assert.ok(parsed, `${glyph} should parse`);
            assert.equal(parsed.body, 'ship it');
            assert.equal(parsed.ordinal, null);
        }
    });

    it('recognises ordered markers and keeps their delimiter', () => {
        assert.deepEqual(listMarker('3) third')?.ordinal, { value: 3, delimiter: ')' });
        assert.deepEqual(listMarker('10. tenth')?.ordinal, { value: 10, delimiter: '.' });
    });

    it('preserves indentation', () => {
        assert.equal(listMarker('    - nested')?.indent, '    ');
    });

    it('is not fooled by prose that merely starts with a dash', () => {
        // No space after the glyph — this is a word ("-ish"), not a bullet.
        assert.equal(listMarker('-ish'), null);
        assert.equal(listMarker('plain text'), null);
        assert.equal(listMarker(''), null);
    });

    it('accepts an empty bullet (the line you press Enter on to escape)', () => {
        assert.equal(listMarker('- ')?.body, '');
    });
});

describe('hasList', () => {
    it('sees a list anywhere in the note', () => {
        assert.equal(hasList('context line\n- one\n- two'), true);
        assert.equal(hasList('no list here'), false);
    });
});

describe('continueList', () => {
    it('returns null off a list line so the browser inserts the newline', () => {
        assert.equal(continueList('just prose', 10), null);
    });

    it('continues an unordered item', () => {
        const value = '- first';
        const out = continueList(value, value.length);
        assert.ok(out);
        assert.equal(out.value, '- first\n- ');
        assert.equal(out.selectionStart, out.value.length);
    });

    it('increments an ordered item', () => {
        const value = '1. first';
        const out = continueList(value, value.length);
        assert.equal(out?.value, '1. first\n2. ');
    });

    it('keeps the indent of a nested item', () => {
        const value = '  * nested';
        assert.equal(continueList(value, value.length)?.value, '  * nested\n  * ');
    });

    it('ends the list when Enter lands on an empty bullet', () => {
        const value = '- one\n- ';
        const out = continueList(value, value.length);
        assert.ok(out);
        assert.equal(out.value, '- one\n');
        assert.equal(out.selectionStart, value.indexOf('\n') + 1);
    });

    it('splits mid-line rather than appending at the end', () => {
        const value = '- alpha beta';
        const out = continueList(value, '- alpha'.length);
        assert.equal(out?.value, '- alpha\n-  beta');
    });

    it('reads the line the caret is on, not the last line', () => {
        const value = '- one\n- two\nprose';
        const out = continueList(value, '- one'.length);
        assert.equal(out?.value, '- one\n- \n- two\nprose');
    });
});

describe('toggleBullets', () => {
    it('bullets the caret line when nothing is selected', () => {
        const out = toggleBullets('renewal risk', 3, 3);
        assert.equal(out.value, '- renewal risk');
    });

    it('bullets every selected line', () => {
        const value = 'one\ntwo\nthree';
        const out = toggleBullets(value, 0, value.length);
        assert.equal(out.value, '- one\n- two\n- three');
    });

    it('strips bullets only when every line already has one', () => {
        const value = '- one\n- two';
        assert.equal(toggleBullets(value, 0, value.length).value, 'one\ntwo');
    });

    it('bullets a mixed selection instead of stripping it', () => {
        const value = '- one\ntwo';
        assert.equal(toggleBullets(value, 0, value.length).value, '- one\n- two');
    });

    it('leaves blank lines alone', () => {
        const value = 'one\n\ntwo';
        assert.equal(toggleBullets(value, 0, value.length).value, '- one\n\n- two');
    });

    it('selects what it rewrote so a second press undoes it', () => {
        const value = 'one\ntwo';
        const once = toggleBullets(value, 0, value.length);
        const twice = toggleBullets(once.value, once.selectionStart, once.selectionEnd);
        assert.equal(twice.value, value);
    });
});

describe('noteBlocks', () => {
    it('groups consecutive list lines into one block', () => {
        assert.deepEqual(noteBlocks('- one\n- two'), [
            { kind: 'list', items: ['one', 'two'] },
        ]);
    });

    it('keeps prose and lists in reading order', () => {
        assert.deepEqual(noteBlocks('Root cause:\n- staffing\n- tooling\nNext step is Ana.'), [
            { kind: 'text', text: 'Root cause:' },
            { kind: 'list', items: ['staffing', 'tooling'] },
            { kind: 'text', text: 'Next step is Ana.' },
        ]);
    });

    it('treats a note with no list as a single text block', () => {
        assert.deepEqual(noteBlocks('one line\nsecond line'), [
            { kind: 'text', text: 'one line\nsecond line' },
        ]);
    });

    it('drops blank lines without emitting empty blocks', () => {
        assert.deepEqual(noteBlocks('alpha\n\n\nbeta'), [
            { kind: 'text', text: 'alpha' },
            { kind: 'text', text: 'beta' },
        ]);
    });

    it('round-trips what the bullet button produced', () => {
        const typed = toggleBullets('staffing\ntooling', 0, 'staffing\ntooling'.length).value;
        assert.deepEqual(noteBlocks(typed), [
            { kind: 'list', items: ['staffing', 'tooling'] },
        ]);
    });

    it('returns nothing for an empty note', () => {
        assert.deepEqual(noteBlocks(''), []);
    });
});

describe('composerRows', () => {
    it('never starts smaller than the minimum', () => {
        assert.equal(composerRows(''), 4);
        assert.equal(composerRows('one line'), 4);
    });

    it('grows a row ahead of the text', () => {
        assert.equal(composerRows('a\nb\nc\nd'), 5);
    });

    it('stops at the maximum so the Add button stays reachable', () => {
        assert.equal(composerRows(Array(40).fill('x').join('\n')), 14);
    });
});
