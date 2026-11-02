// Tests for the section editor's draft and how it follows the version on
// screen.
//
//   cd <repo>/frontend && node --test "src/pages/meetingPrep/**/*.test.ts"
//
// The case these guard: an editor open on v3, a 409 (or Regenerate, or
// Restore) puts v4 on screen, and the editor kept comparing against v3 — so a
// second Save quietly replaced v4's text, which the reader had never seen.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { draftValue, isDirty, openEditing, rebaseEditing, syncEditing, type Editing } from './draft.ts';

/** The editor after the reader typed `text` into a text section. */
const typed = (e: Editing, text: string): Editing => ({ ...e, draft: { kind: 'text', text } });

describe('syncEditing — a list section', () => {
    const v3 = ['Open with Q3', 'Ask about pricing'];
    const v4 = ['Open with Q3', 'Ask about pricing', 'Raise the renewal'];

    it('is in step on the run it was opened on', () => {
        const e = typed(openEditing('talking_points', v3, 'run-3'), 'Open with Q3\nAsk about pricing\nMine');
        assert.equal(syncEditing('talking_points', e, 'run-3', v3), 'in-step');
    });

    it('is in step when a newer version left this section alone (another section changed)', () => {
        const e = typed(openEditing('talking_points', v3, 'run-3'), 'Open with Q3\nMine');
        assert.equal(syncEditing('talking_points', e, 'run-4', [...v3]), 'in-step');
    });

    it('re-seeds a clean draft when the section changed underneath it', () => {
        const e = openEditing('talking_points', v3, 'run-3');
        assert.equal(syncEditing('talking_points', e, 'run-4', v4), 'reseed');
        // What the re-seeded editor holds: v4, clean, based on v4.
        const next = openEditing('talking_points', v4, 'run-4');
        assert.deepEqual(draftValue('talking_points', next.draft), v4);
        assert.equal(syncEditing('talking_points', next, 'run-4', v4), 'in-step');
    });

    it('flags a dirty draft when the section changed underneath it', () => {
        const e = typed(openEditing('talking_points', v3, 'run-3'), 'Open with Q3\nMy own point');
        assert.equal(syncEditing('talking_points', e, 'run-4', v4), 'conflict');
        // The edits survive: Save would still send them.
        assert.deepEqual(draftValue('talking_points', e.draft), ['Open with Q3', 'My own point']);
        assert.ok(isDirty('talking_points', e.seed, e.draft));
    });

    it('asks only once: after the reader agrees, the editor builds on the version on screen', () => {
        const e = typed(openEditing('talking_points', v3, 'run-3'), 'Open with Q3\nMy own point');
        const agreed = rebaseEditing(e, v4, 'run-4');
        assert.equal(syncEditing('talking_points', agreed, 'run-4', v4), 'in-step');
        assert.equal(agreed.draft, e.draft);
        // ...and a later change under it is a conflict again.
        assert.equal(syncEditing('talking_points', agreed, 'run-5', ['Something else']), 'conflict');
    });

    it("is not a conflict when the newer version already holds the draft (the reader's own save landing)", () => {
        const e = typed(openEditing('talking_points', v3, 'run-3'), 'Open with Q3\nAsk about pricing\nRaise the renewal');
        assert.equal(syncEditing('talking_points', e, 'run-4', v4), 'reseed');
    });

    it('follows Restore back to an older text the same way', () => {
        const e = typed(openEditing('talking_points', v4, 'run-4'), 'Open with Q3\nMine');
        // v5 restores v2, where this section read differently.
        assert.equal(syncEditing('talking_points', e, 'run-5', ['Old point']), 'conflict');
        assert.equal(syncEditing('talking_points', openEditing('talking_points', v4, 'run-4'), 'run-5', ['Old point']), 'reseed');
    });
});

describe('syncEditing — prose and stakeholders', () => {
    it('prose: clean re-seeds, dirty conflicts', () => {
        const e = openEditing('executive_summary', 'Old summary', 'run-3');
        assert.equal(syncEditing('executive_summary', e, 'run-4', 'New summary'), 'reseed');
        assert.equal(syncEditing('executive_summary', typed(e, 'My summary'), 'run-4', 'New summary'), 'conflict');
        assert.equal(syncEditing('executive_summary', typed(e, 'My summary'), 'run-4', 'Old summary'), 'in-step');
    });

    it('stakeholders: clean re-seeds, an edited row conflicts', () => {
        const v3 = JSON.stringify([{ name: 'Ana', email: 'ana@x.example', role: 'CFO', context: '' }]);
        const v4 = JSON.stringify([{ name: 'Ana', email: 'ana@x.example', role: 'CEO', context: '' }]);
        const e = openEditing('stakeholder_map', v3, 'run-3');
        assert.equal(syncEditing('stakeholder_map', e, 'run-4', v4), 'reseed');
        assert.equal(e.draft.kind, 'people');
        if (e.draft.kind !== 'people') return;
        const edited: Editing = {
            ...e,
            draft: { ...e.draft, rows: e.draft.rows.map(r => ({ ...r, context: 'Signs off.' })) },
        };
        assert.equal(syncEditing('stakeholder_map', edited, 'run-4', v4), 'conflict');
    });
});
