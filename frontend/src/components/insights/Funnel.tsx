import React from 'react';
import { ArrowDown } from 'lucide-react';
import type { FunnelStep } from '../../api/insights';
import { funnelRows } from '../../pages/insights/chartText';
import { denominatorText, formatCount, formatPct } from '../../pages/insights/insightsFormat';
import { ordinalColor } from './palette';

/**
 * A funnel as horizontal step bars. Each step's width and percentage are
 * relative to the FIRST step (the contract's rule), printed with the count it
 * is out of; between steps, the share that made it from the step before —
 * except in `breakdown` mode, where the later steps split the first one into
 * outcomes and a step-to-step rate ("1,144% of snoozed") would be nonsense.
 */
export const Funnel: React.FC<{ steps: readonly FunnelStep[]; title: string; mode?: 'sequential' | 'breakdown' }> = ({
    steps,
    title,
    mode = 'sequential',
}) => {
    const rows = funnelRows(steps);
    const first = rows[0];
    return (
        <ol className="space-y-1" aria-label={title}>
            {rows.map((r, i) => {
                const width = r.ofFirst === null ? (i === 0 ? 1 : 0) : Math.min(1, r.ofFirst);
                const of = i === 0 ? null : denominatorText(r.value, first.value);
                return (
                    <li key={r.key}>
                        {i > 0 && mode === 'sequential' && (
                            <div className="flex items-center gap-1.5 py-0.5 pl-1 text-[11px] text-gray-500">
                                <ArrowDown size={11} aria-hidden="true" className="text-gray-300" />
                                {r.ofPrev === null ? (
                                    <span>No earlier step to compare</span>
                                ) : (
                                    <span className="tabular-nums">
                                        {formatPct(r.ofPrev)} of {rows[i - 1].label.toLowerCase()}
                                        {r.ofPrev < 1 && (
                                            <span className="text-gray-400"> · {formatPct(1 - r.ofPrev)} fewer</span>
                                        )}
                                    </span>
                                )}
                            </div>
                        )}
                        <div className="grid grid-cols-[minmax(0,7rem)_minmax(0,1fr)] items-center gap-3 sm:grid-cols-[minmax(0,10rem)_minmax(0,1fr)]">
                            <span className="truncate text-sm text-gray-700" title={r.label}>
                                {r.label}
                            </span>
                            <div className="flex min-w-0 items-center gap-2">
                                <span className="h-6 min-w-0 flex-1 overflow-hidden rounded-r bg-gray-100" aria-hidden="true">
                                    <span
                                        className="block h-full rounded-r transition-[width] duration-300 motion-reduce:transition-none"
                                        style={{ width: `${(width * 100).toFixed(1)}%`, backgroundColor: ordinalColor(i, rows.length) }}
                                    />
                                </span>
                                <span className="w-28 flex-shrink-0 text-right leading-tight sm:w-36">
                                    <span className="block text-sm font-medium text-gray-900 tabular-nums">{formatCount(r.value)}</span>
                                    <span className="block text-[11px] text-gray-500 tabular-nums">
                                        {i === 0 ? (mode === 'breakdown' ? 'the whole' : 'the starting point') : r.ofFirst === null ? '—' : `${formatPct(r.ofFirst)} · ${of}`}
                                    </span>
                                </span>
                            </div>
                        </div>
                    </li>
                );
            })}
        </ol>
    );
};
