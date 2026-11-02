import React from 'react';
import type { MatrixData, SectionDrill } from '../../api/insights';
import { matrixCellText, matrixDomain, matrixPosition } from '../../pages/insights/chartText';
import { denominatorText, formatValue } from '../../pages/insights/insightsFormat';
import { divergingColor, inkOn, sequentialColor } from './palette';

/**
 * A heat-coloured grid (department × surface, cohorts, weekday × hour). One
 * hue, light to dark, so darker always means more; every cell also prints its
 * value, and its tooltip adds what a rate is out of ("12 of 15").
 *
 * The first column sticks while the grid scrolls sideways on a phone. With
 * drill `department`, a row's name filters the dashboard to that department.
 * `plain` drops the colour for the "View as table" alternative and prints the
 * denominators inline.
 */
export const MatrixGrid: React.FC<{
    data: MatrixData;
    title: string;
    drill?: SectionDrill;
    onDrillDepartment?: (slug: string) => void;
    activeKeys?: readonly string[];
    plain?: boolean;
}> = ({ data, title, drill, onDrillDepartment, activeKeys = [], plain = false }) => {
    const domain = matrixDomain(data);
    const colorFor = (v: number): string => {
        const t = matrixPosition(v, domain, data.scale);
        return data.scale === 'diverging' ? divergingColor(t) : sequentialColor(t);
    };
    const canDrill = drill === 'department' && !!onDrillDepartment;

    return (
        <div className="space-y-3">
            <div className="relative overflow-x-auto rounded-md">
                <table className={`min-w-full text-xs ${plain ? '' : 'border-separate border-spacing-0.5'}`}>
                    <caption className="sr-only">{title}</caption>
                    <thead>
                        <tr>
                            <th scope="col" className="sticky left-0 z-[1] bg-white px-2 py-1.5 text-left font-medium text-gray-400">
                                <span className="sr-only">Row</span>
                            </th>
                            {data.cols.map(c => (
                                <th key={c.key} scope="col" className="whitespace-nowrap px-2 py-1.5 text-center font-medium text-gray-500">
                                    {c.label}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody className={plain ? 'divide-y divide-gray-100' : ''}>
                        {data.rows.map((row, ri) => {
                            const active = activeKeys.includes(row.key);
                            return (
                                <tr key={row.key}>
                                    <th
                                        scope="row"
                                        className="sticky left-0 z-[1] whitespace-nowrap bg-white pr-3 text-left font-normal"
                                    >
                                        {canDrill ? (
                                            <button
                                                type="button"
                                                onClick={() => onDrillDepartment!(row.key)}
                                                className={`rounded px-1.5 py-1 text-left text-[13px] transition-colors hover:bg-gray-50 hover:text-brand focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30 ${
                                                    active ? 'font-semibold text-brand' : 'text-gray-700'
                                                }`}
                                                title={`Filter the dashboard to ${row.label}`}
                                            >
                                                {row.label}
                                            </button>
                                        ) : (
                                            <span className="px-1.5 text-[13px] text-gray-700">{row.label}</span>
                                        )}
                                    </th>
                                    {data.cols.map((col, ci) => {
                                        const cell = data.cells[ri]?.[ci] ?? null;
                                        const v = cell?.value;
                                        const has = typeof v === 'number' && Number.isFinite(v);
                                        // Withheld on purpose (small group): say so, not "—".
                                        const empty = cell?.note ? 'Hidden' : '—';
                                        const text = matrixCellText(row.label, col.label, cell, data.unit);
                                        if (plain) {
                                            const of = denominatorText(cell?.numerator, cell?.denominator);
                                            return (
                                                <td key={col.key} className="whitespace-nowrap px-2 py-1.5 text-center text-gray-700 tabular-nums">
                                                    {has ? formatValue(v, data.unit) : empty}
                                                    {!has && cell?.note && <span className="block text-[10px] text-gray-500">{cell.note}</span>}
                                                    {has && of && <span className="block text-[10px] text-gray-500">{of}</span>}
                                                </td>
                                            );
                                        }
                                        const bg = has ? colorFor(v as number) : '#f9fafb';
                                        return (
                                            <td
                                                key={col.key}
                                                title={text}
                                                className="h-9 min-w-[3.5rem] rounded px-2 text-center font-medium tabular-nums"
                                                style={{ backgroundColor: bg, color: has ? inkOn(bg) : '#9ca3af' }}
                                            >
                                                <span aria-hidden="true" className={!has && cell?.note ? 'text-[11px] font-normal italic' : undefined}>
                                                    {has ? formatValue(v, data.unit) : empty}
                                                </span>
                                                <span className="sr-only">{text}</span>
                                            </td>
                                        );
                                    })}
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
            {!plain && <ScaleKey data={data} min={domain.min} max={domain.max} />}
        </div>
    );
};

/** The colour key: what pale and dark mean, in the grid's own unit. */
const ScaleKey: React.FC<{ data: MatrixData; min: number; max: number }> = ({ data, min, max }) => {
    const stops = data.scale === 'diverging' ? [-1, -0.5, 0, 0.5, 1].map(divergingColor) : [0, 0.25, 0.5, 0.75, 1].map(sequentialColor);
    return (
        <div className="flex items-center gap-2 text-[11px] text-gray-500" aria-hidden="true">
            <span className="tabular-nums">{formatValue(min, data.unit)}</span>
            <span className="h-2 w-32 rounded-full" style={{ background: `linear-gradient(to right, ${stops.join(', ')})` }} />
            <span className="tabular-nums">{formatValue(max, data.unit)}</span>
            <span className="ml-2 hidden sm:inline">Hover a cell for what it is out of.</span>
        </div>
    );
};
