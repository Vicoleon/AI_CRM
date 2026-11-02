// Splitting a pair guide's `content_md` into its authored sections.
//
// WHY THIS EXISTS, AND WHAT IT IS NOT
// -----------------------------------
// The prose in a pair guide is authored server-side —
// `rubik_core/smart_teams/comparison.render_markdown` emits it, every sentence
// either an authored constant or a model line that passed
// `guide_prompts.screen_prose`. This module does not read, summarise, re-word,
// re-order or judge any of it. It cuts the string on its `##` headings and
// reports two STRUCTURAL facts per section — "does it have `###` subsections"
// and "does it have table rows" — so the page can give the sections different
// containers.
//
// The reason a plain `<ReactMarkdown>{content_md}</ReactMarkdown>` is not good
// enough is the one thing this feature must not do. The document contains two
// different negative results and they mean opposite things:
//
//   * "Where your stated preferences pull in different directions" with no
//     `###` subsections under it — Cubie compared the parts reported for BOTH
//     people and found no collision. `comparison.FRICTION_NONE`: "That is a
//     real result: Cubie looked."
//   * "What this guide cannot speak to" — dimensions Cubie could not read at
//     all, because one or both profiles are missing the layer behind them.
//     `comparison.REFUSALS_INTRO` says why the section exists at all: "They are
//     listed so that a missing section is not read as 'nothing found here'."
//
// Rendered in one undifferentiated column of prose those two read the same, and
// a reader skimming reaches "no problems found" for a dimension nobody
// assessed. Classifying the sections is what lets the page keep them apart.
//
// HOW CLASSIFICATION DEGRADES
// ---------------------------
// The labels below are transcribed from the `*_LABEL` constants in
// `comparison.py`. If one is renamed server-side the match misses and the
// section falls through to `kind: 'other'` — which renders as neutral prose,
// heading and all, with no badge. That is the failure mode on purpose: an
// unrecognised section is rendered plainly and completely, never dropped and
// never given a label that might now be wrong. The section's own intro sentence
// still says what it is, because that sentence is in the body.

/** `comparison.SHORTFALL_LABEL` — not enough reported for both people, so no
 *  guide was written. */
export const SHORTFALL_LABEL = 'Not enough reported for both of you yet';

/** `comparison.SYNERGY_LABEL` — the table of where the two line up. */
export const SYNERGY_LABEL = 'Where you line up';

/** `comparison.FRICTION_LABEL` — the numbered friction sections. */
export const FRICTION_LABEL =
    'Where your stated preferences pull in different directions';

/** `comparison.PANEL_LABEL` (`PairPanel.field_label`) — the two-column
 *  "what each of you asks for" panel. */
export const PANEL_LABEL = 'What each of you asks for';

/** `comparison.CARDS_LABEL` — the three things worth agreeing on. */
export const CARDS_LABEL = 'Three things worth agreeing on';

/** `comparison.REFUSALS_LABEL` — the dimensions Cubie could NOT read. */
export const REFUSALS_LABEL = 'What this guide cannot speak to';

/** `comparison.COMPLETENESS_LABEL` — which layers were reported for both. */
export const COMPLETENESS_LABEL = 'What this guide could and could not read';

export type PairSectionKind =
    | 'shortfall'
    | 'synergy'
    | 'friction'
    | 'panel'
    | 'cards'
    | 'refusals'
    | 'completeness'
    | 'other';

const KIND_BY_HEADING: Record<string, PairSectionKind> = {
    [SHORTFALL_LABEL]: 'shortfall',
    [SYNERGY_LABEL]: 'synergy',
    [FRICTION_LABEL]: 'friction',
    [PANEL_LABEL]: 'panel',
    [CARDS_LABEL]: 'cards',
    [REFUSALS_LABEL]: 'refusals',
    [COMPLETENESS_LABEL]: 'completeness',
};

export interface PairSection {
    kind: PairSectionKind;
    /** The `##` heading, verbatim. */
    heading: string;
    /** Everything under the heading, verbatim markdown. */
    body: string;
    /** Any `### ` line — i.e. the friction/cards sections actually have
     *  numbered subsections rather than the authored "none" sentence. */
    hasSubsections: boolean;
    /** Any markdown table row — i.e. the synergy section has rows. */
    hasTableRows: boolean;
}

/** One `**heading**` column of the "what each of you asks for" panel.
 *
 *  Both columns are the same question asked of each person and
 *  `comparison.PANEL_DISCLAIMER` says they are the same length on purpose and
 *  that neither list is worth more than the other. The page renders them in an
 *  equal-width grid for exactly that reason. */
export interface PanelColumn {
    heading: string;
    bullets: string[];
}

export interface ParsedPanel {
    /** `comparison.PANEL_DISCLAIMER`, verbatim. */
    disclaimer: string;
    columns: PanelColumn[];
}

export interface ParsedPairGuide {
    /** The `# ` line. */
    title: string | null;
    /** Deck, framing and the provenance blockquote — everything before the
     *  first `##`. Verbatim markdown. */
    preamble: string;
    sections: PairSection[];
    /** `pair.closing` — a trailing `> **label** — body` block with no heading of
     *  its own, lifted off the last section so it does not render inside it. */
    closing: string | null;
    /** The advisory notice after the trailing `---`. */
    footer: string;
}

const isTableRow = (line: string): boolean => line.trimStart().startsWith('|');

const isSubheading = (line: string): boolean => /^###\s+/.test(line);

const trimBlank = (lines: string[]): string[] => {
    let start = 0;
    let end = lines.length;
    while (start < end && lines[start].trim() === '') start += 1;
    while (end > start && lines[end - 1].trim() === '') end -= 1;
    return lines.slice(start, end);
};

/** Pull a trailing blockquote block off a section body.
 *
 *  `render_markdown` puts `pair.closing` after the completeness bullets with no
 *  heading of its own, so a naive split leaves "If you do one thing with this"
 *  sitting inside the "what this guide could and could not read" box. Only the
 *  LAST section is treated this way: the friction sections contain
 *  `> **Something to say out loud**` blockquotes too, and those belong exactly
 *  where they are. */
const liftTrailingBlockquote = (
    body: string,
): { body: string; quote: string | null } => {
    const lines = body.split('\n');
    let end = lines.length;
    while (end > 0 && lines[end - 1].trim() === '') end -= 1;
    let start = end;
    while (start > 0 && lines[start - 1].trimStart().startsWith('>')) start -= 1;
    if (start === end) return { body, quote: null };
    // A blockquote that IS the whole body is not a trailing extra; leave it.
    if (trimBlank(lines.slice(0, start)).length === 0) return { body, quote: null };
    return {
        body: trimBlank(lines.slice(0, start)).join('\n'),
        quote: trimBlank(lines.slice(start, end)).join('\n'),
    };
};

/** Split `content_md` into its authored sections.
 *
 *  Never throws and never drops text: anything it cannot classify comes back as
 *  a `kind: 'other'` section with its heading and body intact, and a document
 *  with no `##` headings at all comes back as pure `preamble`. */
export function parsePairGuide(markdown: string): ParsedPairGuide {
    const source = (markdown ?? '').replace(/\r\n?/g, '\n');
    const lines = source.split('\n');

    // The advisory notice sits after a bare `---` at the very end. Split there
    // only when what follows has no heading of its own — a `---` inside a body
    // then stays in the body, which is cosmetic rather than lossy.
    let footer = '';
    let bodyLines = lines;
    for (let i = lines.length - 1; i >= 0; i -= 1) {
        if (lines[i].trim() !== '---') continue;
        const tail = trimBlank(lines.slice(i + 1));
        if (tail.length > 0 && !tail.some(line => /^#{1,6}\s/.test(line))) {
            footer = tail.join('\n');
            bodyLines = lines.slice(0, i);
        }
        break;
    }

    let title: string | null = null;
    const preambleLines: string[] = [];
    const sections: PairSection[] = [];
    let current: { heading: string; lines: string[] } | null = null;

    const flush = () => {
        if (!current) return;
        const body = trimBlank(current.lines).join('\n');
        sections.push({
            kind: KIND_BY_HEADING[current.heading] ?? 'other',
            heading: current.heading,
            body,
            hasSubsections: current.lines.some(isSubheading),
            hasTableRows: current.lines.some(isTableRow),
        });
        current = null;
    };

    for (const line of bodyLines) {
        const h1 = /^#\s+(.*)$/.exec(line);
        if (h1 && title === null && current === null) {
            title = h1[1].trim();
            continue;
        }
        const h2 = /^##\s+(.*)$/.exec(line);
        if (h2) {
            flush();
            current = { heading: h2[1].trim(), lines: [] };
            continue;
        }
        if (current) current.lines.push(line);
        else preambleLines.push(line);
    }
    flush();

    let closing: string | null = null;
    if (sections.length > 0) {
        const last = sections[sections.length - 1];
        const lifted = liftTrailingBlockquote(last.body);
        if (lifted.quote) {
            sections[sections.length - 1] = { ...last, body: lifted.body };
            closing = lifted.quote;
        }
    }

    if (import.meta.env.DEV) {
        for (const section of sections) {
            if (section.kind === 'other') {
                console.warn(
                    '[smart-teams] unrecognised pair-guide section %o — rendering it ' +
                        'as plain prose. If comparison.py renamed a *_LABEL, ' +
                        'pairGuideSections.ts needs the new string.',
                    section.heading,
                );
            }
        }
    }

    return {
        title,
        preamble: trimBlank(preambleLines).join('\n'),
        sections,
        closing,
        footer,
    };
}

/** Parse the two-column panel body.
 *
 *  Shape from `render_markdown`: the disclaimer, then per column a bold-only
 *  line (`**heading**`) followed by `- bullet` lines. Returns `null` when the
 *  body does not look like that, and the caller then renders the body as plain
 *  markdown — a panel whose shape changed should still be readable. */
export function parsePanel(body: string): ParsedPanel | null {
    const lines = (body ?? '').split('\n');
    const disclaimer: string[] = [];
    const columns: PanelColumn[] = [];
    let seenHeading = false;

    for (const line of lines) {
        const trimmed = line.trim();
        const heading = /^\*\*(.+)\*\*$/.exec(trimmed);
        if (heading) {
            seenHeading = true;
            columns.push({ heading: heading[1].trim(), bullets: [] });
            continue;
        }
        const bullet = /^[-*]\s+(.*)$/.exec(trimmed);
        if (bullet && seenHeading && columns.length > 0) {
            columns[columns.length - 1].bullets.push(bullet[1].trim());
            continue;
        }
        if (!seenHeading && trimmed) disclaimer.push(trimmed);
    }

    if (columns.length < 2 || columns.some(column => column.bullets.length === 0)) {
        return null;
    }
    return { disclaimer: disclaimer.join(' '), columns };
}
