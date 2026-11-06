import React, { useMemo } from 'react';
import { Bar, Line } from 'react-chartjs-2';
import type { ChartOptions, TooltipItem } from 'chart.js';
import type { TimeseriesData } from '../../api/insights';
import { bucketLabel, formatTick, formatValue } from '../../pages/insights/insightsFormat';
import { trendSummary } from '../../pages/insights/chartText';
import { CHART_OPTIONS_BASE, prefersReducedMotion } from './chartTheme';
import { seriesColor, withAlpha } from './palette';

/** Legend for two or more series: a line key for lines, a square for bars. */
export const SeriesLegend: React.FC<{ data: TimeseriesData }> = ({ data }) => {
    if (data.series.length < 2) return null;
    return (
        <ul className="flex flex-wrap gap-x-4 gap-y-1.5" aria-label="Legend">
            {data.series.map((s, i) => (
                <li key={s.key} className="inline-flex items-center gap-1.5 text-xs text-gray-600">
                    <span
                        aria-hidden="true"
                        className={data.chart === 'line' ? 'h-0.5 w-3.5 rounded-full' : 'h-2.5 w-2.5 rounded-sm'}
                        style={{ backgroundColor: seriesColor(i) }}
                    />
                    {s.label}
                </li>
            ))}
        </ul>
    );
};

/**
 * A time series as a line or (stacked) bar chart. Daily buckets up to 31 days,
 * weekly beyond (the server decides; `bucket` only changes the labels).
 *
 * The canvas is hidden from assistive tech; the wrapper is `role="img"` with a
 * sentence summary, and the card's "View as table" carries every value.
 */
export const TrendChart: React.FC<{
    data: TimeseriesData;
    bucket: 'day' | 'week';
    title: string;
    height?: number;
}> = ({ data, bucket, title, height = 240 }) => {
    const unit = data.series[0]?.unit ?? 'count';
    const labels = useMemo(() => data.x.map(x => bucketLabel(x, bucket)), [data.x, bucket]);
    const animate = !prefersReducedMotion();
    const single = data.series.length === 1;

    const options = useMemo(() => {
        const tooltip = {
            ...CHART_OPTIONS_BASE.plugins.tooltip,
            callbacks: {
                label: (ctx: TooltipItem<'line' | 'bar'>) => {
                    const s = data.series[ctx.datasetIndex];
                    const v = ctx.parsed.y;
                    return ` ${s?.label ?? ''}: ${formatValue(v, s?.unit ?? unit)}`;
                },
            },
        };
        return {
            ...CHART_OPTIONS_BASE,
            animation: animate ? { duration: 300 } : (false as const),
            interaction: { mode: 'index' as const, intersect: false },
            plugins: { ...CHART_OPTIONS_BASE.plugins, tooltip },
            scales: {
                x: {
                    ...CHART_OPTIONS_BASE.scales.x,
                    stacked: data.stacked,
                    ticks: { ...CHART_OPTIONS_BASE.scales.x.ticks, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
                },
                y: {
                    ...CHART_OPTIONS_BASE.scales.y,
                    stacked: data.stacked,
                    border: { display: false },
                    ticks: {
                        ...CHART_OPTIONS_BASE.scales.y.ticks,
                        maxTicksLimit: 5,
                        callback: (v: number | string) => formatTick(Number(v), unit),
                    },
                    ...(unit === 'pct' ? { suggestedMax: 1 } : {}),
                },
            },
        };
    }, [data.series, data.stacked, unit, animate]);

    const summary = trendSummary(title, data, bucket);

    return (
        <div role="img" aria-label={summary} className="relative w-full" style={{ height }}>
            {data.chart === 'bar' ? (
                <Bar
                    aria-hidden="true"
                    data={{
                        labels,
                        datasets: data.series.map((s, i) => ({
                            label: s.label,
                            data: s.values,
                            backgroundColor: seriesColor(i),
                            hoverBackgroundColor: seriesColor(i),
                            maxBarThickness: 24,
                            // Stacked segments are parted by a 2px white gap on top;
                            // a lone bar gets the rounded data-end, square at its base.
                            borderRadius: data.stacked ? 0 : 4,
                            borderSkipped: 'start' as const,
                            borderColor: '#ffffff',
                            borderWidth: data.stacked ? { top: 2, right: 0, bottom: 0, left: 0 } : 0,
                            stack: data.stacked ? 'all' : undefined,
                        })),
                    }}
                    options={options as ChartOptions<'bar'>}
                />
            ) : (
                <Line
                    aria-hidden="true"
                    data={{
                        labels,
                        datasets: data.series.map((s, i) => ({
                            label: s.label,
                            data: s.values,
                            borderColor: seriesColor(i),
                            backgroundColor: single ? withAlpha(seriesColor(i), 0.1) : data.stacked ? withAlpha(seriesColor(i), 0.18) : seriesColor(i),
                            fill: single ? 'origin' : data.stacked ? '-1' : false,
                            tension: 0.3,
                            borderWidth: 2,
                            pointRadius: data.x.length <= 2 ? 4 : 0,
                            pointHoverRadius: 4,
                            pointHitRadius: 12,
                            pointBackgroundColor: seriesColor(i),
                            pointBorderColor: '#ffffff',
                            pointBorderWidth: 2,
                            spanGaps: false,
                        })),
                    }}
                    options={options as ChartOptions<'line'>}
                />
            )}
        </div>
    );
};
