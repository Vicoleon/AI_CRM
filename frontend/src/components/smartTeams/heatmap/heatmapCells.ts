// How a heatmap cell is coloured and what it is allowed to say.
//
// A leaf module with type-only imports, so `heatmapCells.test.ts` runs under
// Node's own test runner and TypeScript stripping — same arrangement as
// `interviewFlow.ts` and `support/diagnosis.ts`, and for the same reason: the
// repo has no component test runner and this logic is worth pinning without
// adding one.
//
//   cd <repo>/frontend && node --test "src/components/smartTeams/**/*.test.ts"
//
// THE RULE THAT DECIDES THE PALETTE
// ---------------------------------
// `not_comparable` must never look like `aligned`. They are opposite findings —
// "we could not look" versus "you two read the same" — and a heatmap is exactly
// the format that invites collapsing them into one neutral grey. So the unknown
// state gets a HATCH and no fill, agreement gets a real colour, and neither can
// be mistaken for the other at a glance or by a colour-blind reader. The same
// distinction is enforced server-side by `RefusedDimension` being a separate
// type from an empty `AssessedDimension`; this is that rule wearing CSS.
//
// AND THE ONE THAT DECIDES THE NUMBERS
// ------------------------------------
// `score` is absent from a cell unless the viewer holds the raw-profile gate for
// that cell's subject. Absent, not null — so every read here goes through
// `hasScore`, and nothing in this file ever coalesces a missing score to zero.
import type { HeatmapCell, OverlapState, PairHeatmapRow } from '../../../api/smartTeams';

// ── Numbers ──────────────────────────────────────────────────────────────────

/** True only when the gate opened for this cell.
 *
 *  `cell.score ?? 0` and `!!cell.score` are both wrong: the first renders a
 *  withheld number as a real zero, and the second hides a genuine 0.0. */
export function hasScore(cell: HeatmapCell | null | undefined): boolean {
    return !!cell && typeof cell.score === 'number' && Number.isFinite(cell.score);
}

/** The number to print, or null. Rounded — an intensity is not precise to
 *  decimals and printing 71.4 implies a resolution the instrument does not have. */
export function scoreLabel(cell: HeatmapCell | null | undefined): string | null {
    return hasScore(cell) ? String(Math.round(cell!.score!)) : null;
}

// ── Bands ────────────────────────────────────────────────────────────────────

/** A stored band key as a person would read it.
 *
 *  DISC already stores display words ("Very high"); Big Five stores enum values
 *  ("clearly_higher") and motivators store ladder positions. Underscores out,
 *  first letter up — and an unrecognised band is returned as-is rather than
 *  blanked, because showing the raw key is more useful to whoever has to debug
 *  it than showing nothing. */
export function bandLabel(band: string | null): string {
    if (!band) return 'Not measured';
    const words = band.replace(/_/g, ' ').trim();
    return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Where a cell sits on its own framework's ladder, 0..1, or null if unreadable.
 *
 *  Normalised by `band_count` rather than against a shared maximum, because the
 *  ladders are different lengths: DISC's top rung is 3 of 4 and Big Five's is
 *  4 of 5, and a ramp that assumed one scale would wash out every DISC row. */
export function bandFraction(cell: HeatmapCell | null | undefined): number | null {
    if (!cell || cell.band_ordinal === null || cell.band_count < 2) return null;
    return cell.band_ordinal / (cell.band_count - 1);
}

/** Tailwind classes for one person's band cell. Five steps of one hue.
 *
 *  One hue, not a rainbow: these are positions on a scale with no good or bad
 *  end (`big_five.Band` says so explicitly), and a red-to-green ramp would
 *  invent a verdict the instruments refuse to make. */
export function bandClass(cell: HeatmapCell | null | undefined): string {
    const fraction = bandFraction(cell);
    if (fraction === null) return 'bg-[repeating-linear-gradient(45deg,#f8fafc,#f8fafc_4px,#e2e8f0_4px,#e2e8f0_8px)] text-gray-400';
    if (fraction >= 0.99) return 'bg-brand text-white';
    if (fraction >= 0.74) return 'bg-brand/70 text-white';
    if (fraction >= 0.49) return 'bg-brand/45 text-gray-900';
    if (fraction >= 0.24) return 'bg-brand/25 text-gray-900';
    return 'bg-brand/10 text-gray-700';
}

// ── Overlap ──────────────────────────────────────────────────────────────────

interface OverlapStyle {
    /** Cell fill. */
    box: string;
    /** What the state is called on screen and in the legend. */
    label: string;
    /** One sentence a reader can act on. */
    meaning: string;
}

/** The palette, and the copy that goes with it.
 *
 *  `not_comparable` is a hatch with no fill precisely so it cannot read as a
 *  quiet agreement — see the header. Its copy says what is missing rather than
 *  what was found, because nothing was found. */
const OVERLAP_STYLES: Record<OverlapState, OverlapStyle> = {
    aligned: {
        box: 'bg-emerald-500/80 text-white border-emerald-600/30',
        label: 'Same',
        meaning: 'You both land in the same band here. Expect this to feel easy.',
    },
    near: {
        box: 'bg-emerald-200/70 text-emerald-950 border-emerald-300/50',
        label: 'Close',
        meaning: 'One step apart — a difference in degree, not a clash.',
    },
    apart: {
        box: 'bg-amber-400/80 text-amber-950 border-amber-500/30',
        label: 'Apart',
        meaning:
            'Two or more steps apart. This is where you are most likely to grate, and where naming it early helps.',
    },
    not_comparable: {
        box: 'border-gray-300 text-gray-400 bg-[repeating-linear-gradient(45deg,#ffffff,#ffffff_5px,#eef2f6_5px,#eef2f6_10px)]',
        label: 'Not measured',
        meaning:
            'One of you has nothing reportable on this dimension, so there is nothing to compare. This is not agreement.',
    },
};

export function overlapStyle(state: OverlapState): OverlapStyle {
    return OVERLAP_STYLES[state] ?? OVERLAP_STYLES.not_comparable;
}

/** Every state, in the order a legend should list them. */
export const OVERLAP_LEGEND: OverlapState[] = [
    'aligned',
    'near',
    'apart',
    'not_comparable',
];

// ── Summaries ────────────────────────────────────────────────────────────────

export interface OverlapTally {
    aligned: number;
    near: number;
    apart: number;
    not_comparable: number;
    /** Rows we could actually read. */
    comparable: number;
}

export function tally(rows: PairHeatmapRow[]): OverlapTally {
    const out: OverlapTally = {
        aligned: 0,
        near: 0,
        apart: 0,
        not_comparable: 0,
        comparable: 0,
    };
    for (const row of rows) {
        out[row.overlap] += 1;
        if (row.overlap !== 'not_comparable') out.comparable += 1;
    }
    return out;
}

/** One honest sentence over a whole grid.
 *
 *  Deliberately NOT a compatibility score. `comparison_contracts` rejects
 *  generated text carrying one (`REJECT_COMPATIBILITY_SCORE`), and a "68% match"
 *  computed in the browser would be the same forbidden claim with the screen
 *  routed around. So this counts dimensions and names them; it never divides one
 *  count by another to produce a verdict about a relationship. */
export function summarise(rows: PairHeatmapRow[]): string {
    const counts = tally(rows);
    if (counts.comparable === 0) {
        return 'There is nothing to compare yet — neither of you has enough scored for a dimension you both share.';
    }
    const apartNames = rows
        .filter(row => row.overlap === 'apart')
        .map(row => row.name);
    const together = counts.aligned + counts.near;
    const parts: string[] = [];
    if (together > 0) {
        parts.push(
            `You read the same or close on ${together} of ${counts.comparable} dimensions`,
        );
    }
    if (apartNames.length > 0) {
        parts.push(
            `${parts.length ? 'and pull apart on' : 'You pull apart on'} ${listNames(apartNames)}`,
        );
    }
    if (!parts.length) return `All ${counts.comparable} dimensions read close.`;
    let sentence = `${parts.join(', ')}.`;
    if (counts.not_comparable > 0) {
        sentence += ` ${counts.not_comparable} could not be compared.`;
    }
    return sentence;
}

/** "A", "A and B", "A, B and C". */
export function listNames(names: string[]): string {
    if (names.length <= 1) return names[0] ?? '';
    if (names.length === 2) return `${names[0]} and ${names[1]}`;
    return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

// ── Grouping ─────────────────────────────────────────────────────────────────

/** Human names for the layers a grid can show. */
export const FRAMEWORK_LABELS: Record<string, string> = {
    disc: 'Working style',
    big_five: 'Temperament',
    motivators: 'What drives you',
};

/** Rows grouped by framework, in the order the server sent them.
 *
 *  Server order is canonical (`contracts.FRAMEWORKS`), so this preserves it
 *  rather than sorting — two screens sorting differently would put the same
 *  person's dimensions in two orders. */
export function groupByFramework<T extends { framework: string }>(
    rows: T[],
): { framework: string; label: string; rows: T[] }[] {
    const order: string[] = [];
    const bucket = new Map<string, T[]>();
    for (const row of rows) {
        if (!bucket.has(row.framework)) {
            bucket.set(row.framework, []);
            order.push(row.framework);
        }
        bucket.get(row.framework)!.push(row);
    }
    return order.map(framework => ({
        framework,
        label: FRAMEWORK_LABELS[framework] ?? framework,
        rows: bucket.get(framework)!,
    }));
}
