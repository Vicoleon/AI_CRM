import React from 'react';
import type { InsightsPeriod, Section } from '../../api/insights';
import { barSummary, funnelRows, funnelSummary } from '../../pages/insights/chartText';
import { csvFilename, sectionToCsv } from '../../pages/insights/csv';
import { bucketLabel, denominatorText, formatCount, formatPct, formatValue } from '../../pages/insights/insightsFormat';
import { BarList } from './BarList';
import { AltTable, ChartCard } from './ChartCard';
import { DataTable } from './DataTable';
import { Funnel } from './Funnel';
import { MatrixGrid } from './MatrixGrid';
import { SeriesLegend, TrendChart } from './TrendChart';

const hasNumber = (v: unknown): boolean => typeof v === 'number' && Number.isFinite(v);

/** True when a section has nothing worth drawing. A zero is data; a gap is not. */
function sectionIsEmpty(section: Section): boolean {
    switch (section.kind) {
        case 'timeseries':
            return section.data.x.length === 0 || !section.data.series.some(s => s.values.some(hasNumber));
        case 'bar':
            return section.data.items.length === 0;
        case 'funnel':
            return section.data.steps.length === 0 || section.data.steps.every(s => !s.value);
        case 'matrix':
            return section.data.rows.length === 0 || section.data.cols.length === 0;
        case 'table':
            return section.data.rows.length === 0;
        default:
            return true;
    }
}

export interface SectionContext {
    period: InsightsPeriod;
    /** Filename parts for CSVs: dashboard slug, period. */
    dashboard: string;
    onDrillDepartment: (slug: string) => void;
    /** Profile path for an email, or null when the viewer has no person view. */
    personHref: (email: string) => string | null;
    activeDepartments: readonly string[];
}

/**
 * One section of a dashboard payload, drawn by its `kind`. Every kind gets the
 * same card (title, "View as table", CSV, empty state); an unknown kind from a
 * newer server says so instead of rendering nothing.
 */
export const SectionRenderer: React.FC<{ section: Section; ctx: SectionContext }> = ({ section, ctx }) => {
    const empty = sectionIsEmpty(section);
    const filename = csvFilename(['insights', ctx.dashboard, section.id, ctx.period.start, 'to', ctx.period.end]);
    const common = {
        title: section.title,
        subtitle: section.subtitle,
        width: section.width,
        isEmpty: empty,
        emptyHint: section.empty_hint,
        csv: () => sectionToCsv(section),
        csvFilename: filename,
    };

    switch (section.kind) {
        case 'timeseries': {
            const { data } = section;
            const bucket = ctx.period.bucket;
            return (
                <ChartCard
                    {...common}
                    aside={<SeriesLegend data={data} />}
                    table={
                        <AltTable
                            caption={section.title}
                            columns={[
                                { key: 'x', label: bucket === 'week' ? 'Week of' : 'Day' },
                                ...data.series.map(s => ({ key: s.key, label: s.label, numeric: true })),
                            ]}
                            rows={data.x.map((x, i) => ({
                                key: x,
                                cells: {
                                    x: bucketLabel(x, 'day'),
                                    ...Object.fromEntries(data.series.map(s => [s.key, formatValue(s.values[i] ?? null, s.unit)])),
                                },
                            }))}
                        />
                    }
                >
                    <TrendChart data={data} bucket={bucket} title={section.title} />
                </ChartCard>
            );
        }
        case 'bar': {
            const { data } = section;
            return (
                <ChartCard
                    {...common}
                    table={
                        <AltTable
                            caption={barSummary(section.title, data)}
                            columns={[
                                { key: 'label', label: section.drill === 'person' ? 'Person' : section.drill === 'department' ? 'Department' : 'Item' },
                                { key: 'value', label: 'Value', numeric: true },
                                { key: 'of', label: 'Out of', numeric: true },
                            ]}
                            rows={data.items.map(i => ({
                                key: i.key,
                                cells: {
                                    label: i.label,
                                    value: i.value === null && i.note ? 'Hidden' : formatValue(i.value, data.unit),
                                    of: i.value === null && i.note ? i.note : denominatorText(i.numerator, i.denominator) ?? '—',
                                },
                            }))}
                        />
                    }
                >
                    <BarList
                        data={data}
                        title={section.title}
                        drill={section.drill ?? null}
                        onDrillDepartment={ctx.onDrillDepartment}
                        personHref={ctx.personHref}
                        activeKeys={ctx.activeDepartments}
                    />
                </ChartCard>
            );
        }
        case 'funnel': {
            const rows = funnelRows(section.data.steps);
            const first = rows[0];
            return (
                <ChartCard
                    {...common}
                    table={
                        <AltTable
                            caption={funnelSummary(section.title, section.data.steps)}
                            columns={[
                                { key: 'label', label: 'Step' },
                                { key: 'value', label: 'Count', numeric: true },
                                { key: 'first', label: `Share of ${first?.label.toLowerCase() ?? 'first step'}`, numeric: true },
                                { key: 'prev', label: 'Share of step before', numeric: true },
                            ]}
                            rows={rows.map(r => ({
                                key: r.key,
                                cells: {
                                    label: r.label,
                                    value: formatCount(r.value),
                                    first: r.ofFirst === null ? '—' : `${formatPct(r.ofFirst)} (${denominatorText(r.value, first.value)})`,
                                    prev: r.ofPrev === null ? '—' : formatPct(r.ofPrev),
                                },
                            }))}
                        />
                    }
                >
                    <Funnel steps={section.data.steps} mode={section.data.mode} title={funnelSummary(section.title, section.data.steps)} />
                </ChartCard>
            );
        }
        case 'matrix':
            return (
                <ChartCard
                    {...common}
                    table={<MatrixGrid data={section.data} title={section.title} plain />}
                >
                    <MatrixGrid
                        data={section.data}
                        title={section.title}
                        drill={section.drill ?? null}
                        onDrillDepartment={ctx.onDrillDepartment}
                        activeKeys={ctx.activeDepartments}
                    />
                </ChartCard>
            );
        case 'table':
            return (
                <ChartCard {...common}>
                    <DataTable data={section.data} title={section.title} personHref={ctx.personHref} />
                </ChartCard>
            );
        default: {
            const unknown = section as { id?: string; title?: string; width?: Section['width'] };
            return (
                <ChartCard
                    title={unknown.title ?? 'Section'}
                    width={unknown.width ?? 'full'}
                    isEmpty
                    emptyHint="This section needs a newer version of Cubie. Reload the page to get it."
                >
                    {null}
                </ChartCard>
            );
        }
    }
};
