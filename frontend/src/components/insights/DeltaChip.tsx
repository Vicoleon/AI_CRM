import React from 'react';
import type { GoodDirection, InsightsUnit } from '../../api/insights';
import { computeDelta, deltaArrow, deltaSentence, deltaTone, formatDelta } from '../../pages/insights/insightsFormat';
import type { DeltaTone } from '../../pages/insights/insightsFormat';

// Full literal class strings (Tailwind keeps only what it can see).
const TONE: Record<DeltaTone, string> = {
    good: 'bg-emerald-50 text-emerald-700 ring-emerald-600/15',
    bad: 'bg-red-50 text-red-700 ring-red-600/15',
    neutral: 'bg-gray-100 text-gray-600 ring-gray-500/10',
};

/**
 * Change versus the previous period of the same length. The colour says
 * whether the move is GOOD for this metric (green) or not (red) — never just
 * up or down — and the arrow and the words carry it too, so colour is never
 * the only signal.
 */
export const DeltaChip: React.FC<{
    value: number | null | undefined;
    prev: number | null | undefined;
    unit: InsightsUnit | string;
    good: GoodDirection | null | undefined;
    /** "vs previous 30 days". */
    comparison: string;
}> = ({ value, prev, unit, good, comparison }) => {
    if (prev === undefined) return null;
    const delta = computeDelta(value, prev);
    const sentence = deltaSentence(delta, unit, prev, comparison);
    if (!delta) {
        return (
            <span className="inline-flex items-center rounded-full bg-gray-50 px-2 py-0.5 text-[11px] font-medium text-gray-500" title={sentence}>
                No prior data
            </span>
        );
    }
    const tone = deltaTone(delta, good, unit);
    return (
        <span
            className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-semibold tabular-nums ring-1 ring-inset ${TONE[tone]}`}
            title={sentence}
            aria-label={sentence}
        >
            <span aria-hidden="true" className="text-[9px]">
                {deltaArrow(delta, unit)}
            </span>
            {formatDelta(delta, unit)}
        </span>
    );
};
