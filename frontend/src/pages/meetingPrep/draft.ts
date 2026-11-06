// The section editor's draft, and how an open editor follows the version on
// screen.
//
// An editor stays open while the version under it changes: a 409 loads the
// newer version, Regenerate or Restore lands a new one, or the reader opens an
// older one and comes back. Each editor remembers the run it was opened on and
// that section's value at the time. When the version changes:
//
//   * the section itself is unchanged (another section was edited) — nothing
//     to do; Save builds on the new version as before;
//   * the section changed and the draft is clean — the editor re-seeds from
//     the new value, silently;
//   * the section changed and the draft has edits — the editor keeps them and
//     says so ("changed in vN since you started editing"); the first Save
//     after that asks before replacing the newer text.
//
// Without this a dirty editor kept comparing against the old seed, and a
// second Save quietly overwrote the version the 409 had just "shown" (it was
// never visible: the editor covers the read view).
//
// Import-free apart from `sections.ts` (with its extension), so it runs under
// Node's test runner.

import {
    cleanStakeholders,
    emptyStakeholder,
    listToText,
    normalizeProseContent,
    parseStakeholderMap,
    sameList,
    sameStakeholders,
    sectionKind,
    textToList,
    type SectionKey,
    type Stakeholder,
} from './sections.ts';

export type Row = Stakeholder & { rowId: string };

export type Draft =
    | { kind: 'text'; text: string }
    | { kind: 'people'; rows: Row[]; replacesText: boolean };

let rowSeq = 0;
export const withId = (s: Stakeholder): Row => ({ ...s, rowId: `r${++rowSeq}` });

const asString = (v: unknown): string => (typeof v === 'string' ? v : '');
const asList = (v: unknown): string[] =>
    Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];

export function seedDraft(key: SectionKey, value: unknown): Draft {
    const kind = sectionKind(key);
    if (kind === 'prose') return { kind: 'text', text: normalizeProseContent(asString(value)) };
    if (kind === 'list') return { kind: 'text', text: listToText(asList(value)) };
    const parsed = parseStakeholderMap(asString(value));
    if (parsed.kind === 'people') {
        const rows = parsed.people.length ? parsed.people : [emptyStakeholder()];
        return { kind: 'people', rows: rows.map(withId), replacesText: false };
    }
    return { kind: 'people', rows: [withId(emptyStakeholder())], replacesText: true };
}

export function isDirty(key: SectionKey, seed: Draft, draft: Draft): boolean {
    if (seed.kind === 'people' || draft.kind === 'people') {
        if (seed.kind !== 'people' || draft.kind !== 'people') return true;
        // Free text being replaced: savable once there is at least one person,
        // never as an empty list that would silently wipe the old text.
        if (seed.replacesText) return cleanStakeholders(draft.rows).length > 0;
        return !sameStakeholders(seed.rows, draft.rows);
    }
    if (sectionKind(key) === 'list') return !sameList(textToList(seed.text), textToList(draft.text));
    return seed.text.trim() !== draft.text.trim();
}

/** What Save sends: the same shape as `SectionValue` in `api/meetingPrep.ts`. */
export function draftValue(key: SectionKey, draft: Draft): string | string[] | Stakeholder[] {
    if (draft.kind === 'people') return cleanStakeholders(draft.rows);
    return sectionKind(key) === 'list' ? textToList(draft.text) : draft.text.trim();
}

// ─── Following the version on screen ──────────────────────────────────────

/** An open editor. */
export interface Editing {
    seed: Draft;
    draft: Draft;
    /** The run the editor was opened on (or last caught up with). */
    baseRunId: string | null;
    /** The section's stored value at that point (`valueKey`). */
    baseValue: string;
}

/** A section value as a comparable string (values are strings or lists). */
export const valueKey = (value: unknown): string => JSON.stringify(value ?? null);

export function openEditing(key: SectionKey, value: unknown, runId: string | null): Editing {
    const seed = seedDraft(key, value);
    return { seed, draft: seed, baseRunId: runId, baseValue: valueKey(value) };
}

/** The editor caught up with the version on screen, edits kept: after the
 *  reader has agreed to replace the newer text. */
export const rebaseEditing = (editing: Editing, value: unknown, runId: string | null): Editing => ({
    ...editing,
    baseRunId: runId,
    baseValue: valueKey(value),
});

/**
 * How an open editor relates to the version on screen (`runId`, `value`):
 *
 * * `in-step` — same run, or this section's value has not changed.
 * * `reseed` — it changed, and the draft has nothing to lose: no edits, or the
 *   new value already IS the draft (the reader's own save landing).
 * * `conflict` — it changed under edits the reader has not saved.
 */
export type EditorSync = 'in-step' | 'reseed' | 'conflict';

export function syncEditing(key: SectionKey, editing: Editing, runId: string | null, value: unknown): EditorSync {
    if (editing.baseRunId === runId || editing.baseValue === valueKey(value)) return 'in-step';
    if (!isDirty(key, editing.seed, editing.draft)) return 'reseed';
    if (!isDirty(key, seedDraft(key, value), editing.draft)) return 'reseed';
    return 'conflict';
}
