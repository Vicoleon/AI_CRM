import React from 'react';
import { Link } from 'react-router-dom';
import { ArrowUpRight } from 'lucide-react';
import type { Kpi, MetricDef } from '../../api/insights';
import { denominatorSentence, denominatorText, formatValue } from '../../pages/insights/insightsFormat';
import { DeltaChip } from './DeltaChip';
import { MetricInfo } from './MetricInfo';
import { Sparkline } from './Sparkline';

/** A KPI whose id is missing from the catalog still renders, under its raw id. */
const fallbackLabel = (id: string): string => id.replace(/_/g, ' ').replace(/^\w/, c => c.toUpperCase());

/**
 * One headline number: its label and ⓘ definition, the value, the change
 * versus the previous period, what a rate is out of ("212 of 640"), and the
 * period's trend.
 *
 * With `href` the whole card opens the dashboard that owns the number (the
 * Overview tiles). The link is stretched over the card from the title rather
 * than wrapping it, so the ⓘ button inside stays its own control instead of a
 * button nested in a link.
 */
export const KpiTile: React.FC<{
    kpi: Kpi;
    metric: MetricDef | null | undefined;
    /** "vs previous 30 days". */
    comparison: string;
    /** Already carrying the current filters. */
    href?: string | null;
    /** Name of the place `href` opens, for the link's accessible name. */
    hrefLabel?: string | null;
}> = ({ kpi, metric, comparison, href, hrefLabel }) => {
    const label = metric?.label ?? fallbackLabel(kpi.id);
    const unit = metric?.unit ?? 'count';
    const value = formatValue(kpi.value, unit, { compact: true });
    const fullValue = formatValue(kpi.value, unit);
    const ofText = denominatorText(kpi.numerator, kpi.denominator, unit, metric?.part_unit);
    const ofSentence = denominatorSentence(kpi.numerator, kpi.denominator, metric?.denominator, unit, metric?.part_unit);
    const valueTitle =
        kpi.value === null
            ? 'Not enough data to work this out for the period (nothing to divide by).'
            : [`${label}: ${fullValue}`, ofSentence].filter(Boolean).join(' — ');

    return (
        <div
            className={`group relative flex min-w-0 flex-col gap-1.5 rounded-lg border border-gray-200 bg-white p-4 shadow-sm transition-[border-color,box-shadow] ${
                href ? 'hover:border-brand/40 hover:shadow-md focus-within:ring-2 focus-within:ring-brand/30' : ''
            }`}
        >
            <div className="flex items-start justify-between gap-2">
                <h3 className="min-w-0 text-[13px] font-medium leading-snug text-gray-600">
                    {href ? (
                        <Link
                            to={href}
                            className="after:absolute after:inset-0 after:rounded-lg after:content-[''] focus:outline-none"
                        >
                            {label}
                            <span className="sr-only">{hrefLabel ? ` — open ${hrefLabel}` : ' — open its dashboard'}</span>
                        </Link>
                    ) : (
                        label
                    )}
                </h3>
                <div className="flex items-center gap-1">
                    {href && (
                        <ArrowUpRight
                            size={14}
                            aria-hidden="true"
                            className="text-gray-300 transition-colors group-hover:text-brand"
                        />
                    )}
                    <MetricInfo metric={metric} label={label} />
                </div>
            </div>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span
                    className="text-2xl font-semibold leading-tight tracking-tight text-gray-900 tabular-nums sm:text-[1.75rem]"
                    title={valueTitle}
                >
                    {value}
                </span>
                <DeltaChip
                    value={kpi.value}
                    prev={kpi.prev_value}
                    unit={unit}
                    good={metric?.good_direction}
                    comparison={comparison}
                />
            </div>
            <p className="line-clamp-2 min-h-[1rem] text-xs leading-snug text-gray-500 tabular-nums" title={ofSentence ?? undefined}>
                {ofText ?? ' '}
                {ofText && metric?.denominator && <span className="text-gray-400"> {metric.denominator}</span>}
            </p>
            <Sparkline values={kpi.spark} className="mt-auto" />
        </div>
    );
};

// Full literal class strings so Tailwind sees every column count.
const GRID_COLS: Record<number, string> = {
    1: 'grid-cols-1 sm:grid-cols-2',
    2: 'grid-cols-2',
    3: 'grid-cols-2 md:grid-cols-3',
    4: 'grid-cols-2 lg:grid-cols-4',
    5: 'grid-cols-2 md:grid-cols-3 xl:grid-cols-5',
};
const GRID_MANY = 'grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5';

/** Two columns on a phone, up to five on a wide screen, sized to the count so
 *  a row of four never leaves a hole where a fifth would go. */
export const KpiGrid: React.FC<{ count: number; children: React.ReactNode }> = ({ count, children }) => (
    <div className={`grid gap-3 ${GRID_COLS[count] ?? GRID_MANY}`}>{children}</div>
);
