// Pure helpers for the official-report surfaces. Split out of the components for
// the same reason as `guide/guideState.ts` — the repo's lint rules want a module
// to export components or plain values, not both.
import type { ParsedUploadPayload, UploadRecord } from '../../../api/smartTeams';

/** `disc.DIMENSIONS` — the canonical order, so the table reads the way every
 *  DISC report the person is holding reads. */
export const DISC_ORDER = ['D', 'I', 'S', 'C'] as const;

/** `disc.DIMENSION_NAMES`, mirrored. NOT the 1928 Marston labels, which the
 *  instrument keeps for its methodology footnote and deliberately never shows a
 *  person ("Submission" reads as an insult in a 2026 workplace). */
export const DISC_NAMES: Record<string, string> = {
    D: 'Dominance',
    I: 'Influence',
    S: 'Steadiness',
    C: 'Conscientiousness',
};

/** Framework keys as a person would name them: names of instruments, never a
 *  claim about what one measures. An unknown key renders as itself. */
const FRAMEWORK_NAMES: Record<string, string> = {
    disc: 'DISC',
    big_five: 'Big Five',
    type16: 'four-letter type',
    strengths: 'strengths',
    motivators: 'motivators',
    enneagram: 'Enneagram',
};

export function frameworkName(key: string | null | undefined): string {
    if (!key) return 'assessment';
    return FRAMEWORK_NAMES[key.toLowerCase()] ?? key;
}

/** How the server said the numbers are scaled. `uploads._ACCEPTED_SCALES` has
 *  two members and a percentile report is refused outright rather than rescaled,
 *  so there is no third case to guess at. */
export function scaleLabel(scale: ParsedUploadPayload['scale']): string {
    return scale === 'percent' ? 'as percentages' : 'on a 0–100 scale';
}

/** Is the parse job still the thing we are waiting on?
 *
 *  `pending` with neither a proposal nor a refusal. A parse job that died leaves
 *  the row in exactly this state for ever, which is why every poll built on it
 *  is bounded. */
export function isParsing(record: UploadRecord): boolean {
    return record.review_status === 'pending' && !record.parsed && !record.parse_error;
}
