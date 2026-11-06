import React from 'react';
import { Info } from 'lucide-react';
import type { MetricDef } from '../../api/insights';
import { usePopover } from './usePopover';

/** Plain-language "which way is good" for a metric. */
const DIRECTION_TEXT: Record<MetricDef['good_direction'], string> = {
    up: 'Higher is better.',
    down: 'Lower is better.',
    neutral: 'Neither direction is better on its own.',
};

/**
 * The ⓘ beside a metric: its definition, what it is out of, and which way is
 * good — straight from the server's metric catalog, so the words a reader sees
 * here are the same ones the Definitions drawer and the AI summary use.
 */
export const MetricInfo: React.FC<{
    metric: MetricDef | null | undefined;
    /** The label to name in the button's accessible name when the catalog has none. */
    label: string;
    align?: 'left' | 'right';
}> = ({ metric, label, align = 'right' }) => {
    const { open, toggle, rootRef, triggerRef, panelId } = usePopover();
    if (!metric) return null;
    return (
        <div ref={rootRef} className="relative z-10 flex-shrink-0">
            <button
                ref={triggerRef}
                type="button"
                onClick={toggle}
                aria-expanded={open}
                aria-controls={panelId}
                aria-label={`What "${metric.label || label}" means`}
                title="What this means"
                className="-m-1 rounded-full p-1 text-gray-400 transition-colors hover:text-brand focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
            >
                <Info size={14} aria-hidden="true" />
            </button>
            {open && (
                <div
                    id={panelId}
                    role="dialog"
                    aria-label={metric.label}
                    className={`absolute top-full mt-2 w-72 max-w-[80vw] rounded-lg border border-gray-200 bg-white p-3 text-left shadow-lg ${
                        align === 'right' ? 'right-0' : 'left-0'
                    }`}
                >
                    <p className="text-sm font-semibold text-gray-900">{metric.label}</p>
                    <p className="mt-1 text-[13px] leading-relaxed text-gray-600">{metric.definition}</p>
                    {metric.denominator && (
                        <p className="mt-2 text-xs text-gray-500">
                            <span className="font-medium text-gray-700">Out of:</span> {metric.denominator}
                        </p>
                    )}
                    <p className="mt-1 text-xs text-gray-500">{DIRECTION_TEXT[metric.good_direction] ?? ''}</p>
                </div>
            )}
        </div>
    );
};
