// The editable sections of a Meeting Prep brief, and the pure helpers the page
// uses to edit them and to describe its versions.
//
// MIRRORS `rubik_core/meeting_prep/sections.py` — the same 14 keys, the same
// kind per key, the same two labels. `sections.test.ts` re-reads the Python
// registry and fails if the two drift, because a drift here is silent: a key
// the page offers but the server refuses is a 400 on Save, and a label that
// differs means the version list ("You edited Talking Points") names a section
// the page calls something else.
//
// Deliberately import-free apart from `datetime.ts` (imported WITH its
// extension), so it loads under Node's test runner without a bundler:
//
//   cd <repo>/frontend && node --test "src/pages/meetingPrep/**/*.test.ts"

import { parseInstant } from '../../utils/datetime.ts';

export type SectionKind = 'prose' | 'list' | 'stakeholders';

export type SectionKey =
    | 'executive_summary'
    | 'meeting_objective'
    | 'client_profile'
    | 'stakeholder_map'
    | 'recent_interactions'
    | 'past_meeting_context'
    | 'product_fit_analysis'
    | 'recommended_positioning'
    | 'likely_objections'
    | 'suggested_agenda'
    | 'talking_points'
    | 'open_items_you_owe'
    | 'discovery_questions'
    | 'next_steps';

export interface SectionDef {
    key: SectionKey;
    kind: SectionKind;
    externalLabel: string;
    internalLabel: string;
}

export const SECTIONS: readonly SectionDef[] = [
    { key: 'executive_summary', kind: 'prose', externalLabel: 'Executive Summary', internalLabel: 'Executive Summary' },
    { key: 'meeting_objective', kind: 'prose', externalLabel: 'Meeting Objective', internalLabel: 'Meeting Objective' },
    { key: 'client_profile', kind: 'prose', externalLabel: 'Client Profile', internalLabel: 'Relationship Context' },
    { key: 'stakeholder_map', kind: 'stakeholders', externalLabel: 'Stakeholder Map', internalLabel: 'Roles & Dynamics' },
    { key: 'recent_interactions', kind: 'prose', externalLabel: 'Recent Interactions', internalLabel: 'Recent Interactions' },
    { key: 'past_meeting_context', kind: 'prose', externalLabel: 'Past Meeting Context', internalLabel: 'Past Meeting Context' },
    { key: 'product_fit_analysis', kind: 'prose', externalLabel: 'Product Fit Analysis', internalLabel: 'Project/Status Context' },
    { key: 'recommended_positioning', kind: 'prose', externalLabel: 'Recommended Positioning', internalLabel: 'Communication Style' },
    { key: 'likely_objections', kind: 'prose', externalLabel: 'Likely Objections', internalLabel: 'Potential Friction' },
    { key: 'suggested_agenda', kind: 'list', externalLabel: 'Suggested Agenda', internalLabel: 'Suggested Agenda' },
    { key: 'talking_points', kind: 'list', externalLabel: 'Talking Points', internalLabel: 'Talking Points' },
    { key: 'open_items_you_owe', kind: 'list', externalLabel: 'What You Owe Them', internalLabel: 'What You Owe Them' },
    { key: 'discovery_questions', kind: 'list', externalLabel: 'Discovery Questions', internalLabel: 'Key Questions' },
    { key: 'next_steps', kind: 'list', externalLabel: 'Next Steps', internalLabel: 'Next Steps' },
];

const BY_KEY: ReadonlyMap<string, SectionDef> = new Map(SECTIONS.map(s => [s.key, s]));

export const isSectionKey = (key: string | null | undefined): key is SectionKey =>
    !!key && BY_KEY.has(key);

export const sectionKind = (key: SectionKey): SectionKind => BY_KEY.get(key)!.kind;

/** The label the page shows for a section, in the brief's persona. An unknown
 *  key (a newer server, an old row) reads as words rather than as snake_case. */
export function sectionLabel(key: string | null | undefined, isInternal: boolean): string {
    if (!key) return 'a section';
    const def = BY_KEY.get(key);
    if (def) return isInternal ? def.internalLabel : def.externalLabel;
    return key
        .split('_')
        .filter(Boolean)
        .map(w => w.charAt(0).toUpperCase() + w.slice(1))
        .join(' ');
}

// ─── Prose sections ───────────────────────────────────────────────────────

/** A prose field that arrived as a raw JSON array string (older briefs) as
 *  markdown bullets; anything else unchanged. Used for display AND to seed the
 *  editor, so a person edits what they were reading. */
export function normalizeProseContent(content: string | null | undefined): string {
    const text = String(content ?? '');
    const trimmed = text.trim();
    if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
        try {
            const parsed: unknown = JSON.parse(trimmed);
            if (Array.isArray(parsed)) {
                return parsed
                    .map(item => `- ${typeof item === 'string' ? item : JSON.stringify(item)}`)
                    .join('\n');
            }
        } catch {
            // not valid JSON — fall through
        }
    }
    return text;
}

// ─── List sections: one item per line ─────────────────────────────────────

/** A bullet or number at the start of a line ("- ", "* ", "• ", "2. ", "3) ").
 *  MIRRORS `_LEADING_MARKER` in `rubik_core/meeting_prep/rewrite.py`, the rule
 *  the server uses when it splits a block of text into list items;
 *  `sections.test.ts` re-reads the Python and fails if the two drift. */
export const LEADING_MARKER = /^\s*(?:[-*•]|\d+[.)])\s+/;

/** A list as the editor's textarea text: one item per line.
 *
 *  An item that itself holds several lines (an old brief can store "* a\n* b"
 *  as one item) is split into one item per line, its markers stripped the way
 *  the server splits a block — NOT folded onto one line. Folding turned the
 *  sub-bullets of an item the reader never touched into a run-on "a * b" the
 *  moment they saved a change anywhere else in the list. Splitting means the
 *  textarea shows exactly what Save will store: `textToList` of this text. */
export function listToText(items: readonly string[] | null | undefined): string {
    return (items ?? [])
        .flatMap(item => {
            const text = String(item ?? '');
            if (!/[\r\n]/.test(text)) return [text.trim()];
            return text.split(/[\r\n]+/).map(line => line.replace(LEADING_MARKER, '').trim());
        })
        .filter(Boolean)
        .join('\n');
}

/** The textarea text back into a list: one item per line, trimmed, blank lines
 *  dropped — the same rule `normalize_section_value` applies server-side. */
export function textToList(text: string | null | undefined): string[] {
    return String(text ?? '')
        .split(/\r?\n/)
        .map(line => line.trim())
        .filter(Boolean);
}

export const sameList = (a: readonly string[], b: readonly string[]): boolean =>
    a.length === b.length && a.every((v, i) => v === b[i]);

// ─── Stakeholders ─────────────────────────────────────────────────────────

export interface Stakeholder {
    name: string;
    email: string;
    role: string;
    context: string;
}

export const emptyStakeholder = (): Stakeholder => ({ name: '', email: '', role: '', context: '' });

const asText = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v));

/**
 * Read the stored `stakeholder_map` the way the page always has.
 *
 * - A JSON array of people (what the model and the editor both write) →
 *   `{kind: 'people'}`.
 * - The older `"email: description | email: description"` shape → people,
 *   with a best-effort name and role pulled out of the description.
 * - Anything else → `{kind: 'text'}`, rendered as markdown.
 */
export type StakeholderMap =
    | { kind: 'people'; people: Stakeholder[] }
    | { kind: 'text'; text: string };

export function parseStakeholderMap(raw: string | null | undefined): StakeholderMap {
    const text = String(raw ?? '');
    const trimmed = text.trim();
    if (!trimmed) return { kind: 'people', people: [] };

    if (trimmed.startsWith('[')) {
        try {
            const parsed: unknown = JSON.parse(trimmed);
            if (Array.isArray(parsed)) {
                const people = parsed
                    .filter((p): p is Record<string, unknown> => !!p && typeof p === 'object')
                    .map(p => ({
                        name: asText(p.name),
                        email: asText(p.email),
                        role: asText(p.role),
                        context: asText(p.context),
                    }));
                return { kind: 'people', people };
            }
        } catch {
            // not JSON after all — fall through to the older shapes
        }
    }

    if (trimmed.includes('|')) {
        const people = trimmed
            .split('|')
            .map(s => s.trim())
            .filter(Boolean)
            .map((chunk): Stakeholder => {
                const colon = chunk.indexOf(':');
                if (colon === -1) return { name: chunk, email: '', role: '', context: '' };
                const emailOrName = chunk.slice(0, colon).trim();
                const context = chunk.slice(colon + 1).trim();
                // Usually "FirstName LastName is the …"
                const nameMatch = context.match(/^([A-Z][a-z]+(?: [A-Z][a-z]+)+) is /);
                const roleMatch = context.match(/(?:is (?:the |a |an ))([^.]+?)\./);
                return {
                    name: nameMatch ? nameMatch[1] : emailOrName,
                    email: emailOrName,
                    role: roleMatch ? roleMatch[1].replace(/^(the |a |an )/i, '').trim() : '',
                    context,
                };
            });
        return { kind: 'people', people };
    }

    return { kind: 'text', text };
}

/** Rows as the server will store them: every field trimmed, all-blank rows
 *  dropped (`normalize_section_value` does the same). */
export function cleanStakeholders(rows: readonly Stakeholder[]): Stakeholder[] {
    return rows
        .map(r => ({
            name: asText(r.name).trim(),
            email: asText(r.email).trim(),
            role: asText(r.role).trim(),
            context: asText(r.context).trim(),
        }))
        .filter(r => r.name || r.email || r.role || r.context);
}

export const sameStakeholders = (a: readonly Stakeholder[], b: readonly Stakeholder[]): boolean => {
    const x = cleanStakeholders(a);
    const y = cleanStakeholders(b);
    return (
        x.length === y.length &&
        x.every(
            (r, i) =>
                r.name === y[i].name &&
                r.email === y[i].email &&
                r.role === y[i].role &&
                r.context === y[i].context,
        )
    );
};

// ─── Provenance and version descriptions ──────────────────────────────────

/** `section_meta[key]` as the server stores it. */
export interface SectionMetaEntry {
    origin: 'edited' | 'rewritten' | string;
    version?: number | null;
    at?: string | null;
    instruction?: string | null;
}

/** The badge on a section header: who last changed it and in which version. */
export function provenanceLabel(meta: SectionMetaEntry | null | undefined): string | null {
    if (!meta) return null;
    const v = typeof meta.version === 'number' ? ` · v${meta.version}` : '';
    if (meta.origin === 'edited') return `Edited by you${v}`;
    if (meta.origin === 'rewritten') return `Rewritten by Cubie${v}`;
    return null;
}

/** Sections the person changed BY HAND — the ones "Keep my edits" carries into
 *  a full regenerate (Cubie's rewrites are not carried). */
export function editedSectionKeys(
    meta: Record<string, SectionMetaEntry> | null | undefined,
): string[] {
    return Object.entries(meta ?? {})
        .filter(([, m]) => m?.origin === 'edited')
        .map(([k]) => k);
}

/** What `describeChange` needs from a version — satisfied by both the full
 *  brief and a row of the version list. */
export interface ChangeSummary {
    version?: number | null;
    change_kind?: string | null;
    change_note?: string | null;
    changed_section?: string | null;
    base_version?: number | null;
}

const quoted = (note: string | null | undefined): string => {
    const n = (note ?? '').trim();
    if (!n) return '';
    const short = n.length > 80 ? `${n.slice(0, 77).trimEnd()}…` : n;
    return ` — “${short}”`;
};

/** One line on what a version changed: "First version", "Regenerated",
 *  "You edited Talking Points", "Cubie rewrote Key Questions — “shorter”",
 *  "Restored v2". */
export function describeChange(summary: ChangeSummary | null | undefined, isInternal: boolean): string {
    if (!summary) return 'Briefing';
    const label = sectionLabel(summary.changed_section, isInternal);
    switch (summary.change_kind) {
        case 'generated':
            return !summary.version || summary.version === 1 ? 'First version' : 'Generated';
        case 'regenerated':
            return `Regenerated${quoted(summary.change_note)}`;
        case 'section_edited':
            return `You edited ${label}`;
        case 'section_rewritten':
            return `Cubie rewrote ${label}${quoted(summary.change_note)}`;
        case 'restored':
            return summary.base_version ? `Restored v${summary.base_version}` : 'Restored an earlier version';
        case 'failed':
            return 'Generation failed';
        default:
            if (summary.version === 1) return 'First version';
            return summary.version ? `Version ${summary.version}` : 'Briefing';
    }
}

/** Whether a version was produced by a full generation (vs. an edit/restore). */
export const isGeneration = (kind: string | null | undefined): boolean =>
    !kind || kind === 'generated' || kind === 'regenerated';

/** The version picker's button text: "v5 · latest" or "v3 of 5". */
export function versionMenuLabel(version: number | null | undefined, latest: number | null | undefined): string {
    if (!version) return 'Versions';
    if (!latest || latest <= version) return `v${version} · latest`;
    return `v${version} of ${latest}`;
}

/**
 * When a version was saved, in the viewer's own timezone: "Oct 1, 2:14 PM"
 * (with the year when it is not this year's).
 *
 * The API sends naive UTC (`datetime.utcnow().isoformat()`); `parseInstant`
 * reads that as the UTC instant it is, where `new Date()` would read it as
 * local wall-clock time.
 */
export function formatVersionWhen(
    iso: string | null | undefined,
    now: Date = new Date(),
    fallback = '—',
): string {
    const at = parseInstant(iso);
    if (!at) return fallback;
    const opts: Intl.DateTimeFormatOptions = {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
    };
    if (at.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
    try {
        return at.toLocaleString(undefined, opts);
    } catch {
        return at.toLocaleString();
    }
}
