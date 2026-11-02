// Text helpers for the EOS notes composer (IDS issue notes and to-do notes) —
// the pure half of `NotesThread`.
//
// A UAT note on the IDS notes box asked for room to write bullets. Two separate
// problems hid behind that one request:
//
//   1. the box was two rows tall, so anything longer than a sentence was typed
//      through a letterbox (fixed in the component), and
//   2. a textarea gives you no list behaviour at all — you type "- ", press
//      Enter, and the next line starts flush left, so a bulleted note has to be
//      hand-maintained line by line.
//
// This module is the second half: recognising a list line, continuing it on
// Enter, toggling bullets over a selection, and re-reading the stored text as
// bullets. It stays a leaf module (no React, no imports) so it runs under
// `node --test` and is covered by `tsc -b`.

/** The marker written when the user asks for bullets. Kept ASCII so the stored
 *  note is still legible everywhere it is echoed — Slack, email, the RAG
 *  corpus — none of which render our list markup. */
export const BULLET_MARKER = '- ';

/** A leading list marker: `-`, `*`, `•`, or `1.` / `1)`. */
const MARKER_RE = /^([ \t]*)([-*•]|(\d+)[.)])([ \t]+)(.*)$/;

export interface ListMarker {
    /** Leading whitespace, preserved so nested lists survive a round trip. */
    indent: string;
    /** The marker glyph as typed (`-`, `*`, `•`) or the ordinal ("1"). */
    marker: string;
    /** Set when the marker was ordered (`1.`), carrying its delimiter. */
    ordinal: { value: number; delimiter: string } | null;
    /** Everything after the marker and its trailing space. */
    body: string;
}

/** Parse one line's list marker, or null when the line isn't a list item. */
export function listMarker(line: string): ListMarker | null {
    const m = MARKER_RE.exec(line);
    if (!m) return null;
    const [, indent, marker, digits, , body] = m;
    return {
        indent,
        marker,
        ordinal: digits
            ? { value: Number(digits), delimiter: marker.slice(digits.length) }
            : null,
        body,
    };
}

/** True when `content` has at least one bullet/numbered line. */
export function hasList(content: string): boolean {
    return content.split('\n').some(line => listMarker(line) !== null);
}

export interface TextEdit {
    value: string;
    /** Where the caret lands after the edit (selection start). */
    selectionStart: number;
    /** Selection end — equal to `selectionStart` for a plain caret. */
    selectionEnd: number;
}

/** Start-of-line index for `cursor` within `value`. */
const lineStart = (value: string, cursor: number): number =>
    value.lastIndexOf('\n', Math.max(0, cursor - 1)) + 1;

/** End-of-line index for `cursor` within `value`. */
const lineEnd = (value: string, cursor: number): number => {
    const idx = value.indexOf('\n', cursor);
    return idx === -1 ? value.length : idx;
};

/**
 * Enter pressed at `cursor` — continue the list the caret sits in.
 *
 * Returns null when the caret isn't on a list line, which the caller reads as
 * "let the browser insert its own newline". Two behaviours matter:
 *
 * - continuing "- foo" gives you "- " on the next line, and "2. " after "1.",
 * - pressing Enter on an EMPTY bullet ends the list (clears the marker) rather
 *   than laying down a third empty bullet — the escape hatch every editor has,
 *   and without it a bulleted note can never get back to a plain paragraph.
 */
export function continueList(value: string, cursor: number): TextEdit | null {
    const start = lineStart(value, cursor);
    const current = value.slice(start, lineEnd(value, cursor));
    const parsed = listMarker(current);
    if (!parsed) return null;

    // Empty item → drop the marker and stay on this line.
    if (parsed.body.trim() === '') {
        const next = value.slice(0, start) + parsed.indent + value.slice(start + current.length);
        const caret = start + parsed.indent.length;
        return { value: next, selectionStart: caret, selectionEnd: caret };
    }

    const marker = parsed.ordinal
        ? `${parsed.ordinal.value + 1}${parsed.ordinal.delimiter}`
        : parsed.marker;
    const inserted = `\n${parsed.indent}${marker} `;
    const next = value.slice(0, cursor) + inserted + value.slice(cursor);
    const caret = cursor + inserted.length;
    return { value: next, selectionStart: caret, selectionEnd: caret };
}

/**
 * Toggle bullets across every line the selection touches (or the caret's line).
 *
 * Bullets are removed only when EVERY non-blank line already has one — a mixed
 * selection gets bulleted, which is what "make this a list" means when half of
 * it already is one.
 */
export function toggleBullets(
    value: string,
    selectionStart: number,
    selectionEnd: number,
): TextEdit {
    const start = lineStart(value, selectionStart);
    const end = lineEnd(value, selectionEnd);
    const lines = value.slice(start, end).split('\n');
    const meaningful = lines.filter(l => l.trim() !== '');
    const allBulleted =
        meaningful.length > 0 && meaningful.every(l => listMarker(l) !== null);

    const next = lines.map(line => {
        if (line.trim() === '') return line;
        const parsed = listMarker(line);
        if (allBulleted) return parsed ? parsed.indent + parsed.body : line;
        if (parsed) return line;
        const indent = /^[ \t]*/.exec(line)?.[0] ?? '';
        return `${indent}${BULLET_MARKER}${line.slice(indent.length)}`;
    });

    const replaced = next.join('\n');
    return {
        value: value.slice(0, start) + replaced + value.slice(end),
        selectionStart: start,
        selectionEnd: start + replaced.length,
    };
}

export type NoteBlock =
    | { kind: 'list'; items: string[] }
    | { kind: 'text'; text: string };

/**
 * Re-read a stored note as blocks so bullets RENDER as bullets.
 *
 * Without this the reader sees the literal "- " a writer typed, and a bullet
 * that wraps onto a second line loses its hanging indent — which is most of
 * why a long note is unreadable in a meeting. Consecutive list lines collapse
 * into one block; everything else stays prose (blank lines are dropped, since
 * `whitespace-pre-wrap` already handles spacing inside a paragraph).
 */
export function noteBlocks(content: string): NoteBlock[] {
    const blocks: NoteBlock[] = [];
    let prose: string[] = [];

    const flushProse = () => {
        if (prose.length === 0) return;
        blocks.push({ kind: 'text', text: prose.join('\n') });
        prose = [];
    };

    for (const line of content.split('\n')) {
        const parsed = listMarker(line);
        if (parsed) {
            flushProse();
            const last = blocks[blocks.length - 1];
            if (last && last.kind === 'list') last.items.push(parsed.body);
            else blocks.push({ kind: 'list', items: [parsed.body] });
            continue;
        }
        if (line.trim() === '') {
            // A blank line closes a list but doesn't start an empty paragraph.
            flushProse();
            continue;
        }
        const last = blocks[blocks.length - 1];
        if (last && last.kind === 'list' && prose.length === 0) flushProse();
        prose.push(line);
    }
    flushProse();
    return blocks;
}

/**
 * Rows the composer should render for `value`.
 *
 * Grows with the note instead of forcing a scroll at line three, and stops at
 * `max` so a long note doesn't push the Add button off screen. Wrapped lines
 * aren't counted — the textarea keeps its own scrollbar for those — so this is
 * a floor on the height, not an exact fit.
 */
export function composerRows(value: string, min = 4, max = 14): number {
    const lines = value === '' ? 0 : value.split('\n').length;
    return Math.min(max, Math.max(min, lines + 1));
}
