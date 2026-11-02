import React from 'react';

/**
 * A tiny trend line under a KPI: the period's buckets in a quiet grey, the
 * latest one marked in brand violet. Decorative — the tile it sits in already
 * states the value and the change — so it is hidden from screen readers.
 *
 * The SVG stretches to its box (`preserveAspectRatio="none"`), so the stroke
 * uses `non-scaling-stroke` and the end dot is an HTML dot placed by
 * percentage: a circle inside a stretched SVG would come out as an oval.
 */
export const Sparkline: React.FC<{
    values: readonly (number | null)[] | null | undefined;
    className?: string;
    height?: number;
}> = ({ values, className = '', height = 28 }) => {
    const points = (values ?? []).map(v => (typeof v === 'number' && Number.isFinite(v) ? v : null));
    const numeric = points.filter((v): v is number => v !== null);
    if (numeric.length < 2) return <div className={className} style={{ height }} aria-hidden="true" />;

    const min = Math.min(0, ...numeric);
    const max = Math.max(...numeric);
    const span = max - min || 1;
    const W = 100;
    const H = 100;
    const PAD = 6;
    const x = (i: number) => (points.length === 1 ? W / 2 : (i / (points.length - 1)) * W);
    const y = (v: number) => H - PAD - ((v - min) / span) * (H - 2 * PAD);

    // Break the line at a missing bucket instead of drawing through it.
    let d = '';
    let pen = false;
    for (let i = 0; i < points.length; i++) {
        const v = points[i];
        if (v === null) {
            pen = false;
            continue;
        }
        d += `${pen ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
        pen = true;
    }

    let lastIndex = points.length - 1;
    while (lastIndex >= 0 && points[lastIndex] === null) lastIndex -= 1;
    const last = points[lastIndex] as number;

    return (
        <div className={`relative ${className}`} style={{ height }} aria-hidden="true">
            <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-full w-full overflow-visible">
                <path
                    d={d.trim()}
                    fill="none"
                    stroke="#9ca3af"
                    strokeWidth={1.5}
                    strokeLinejoin="round"
                    strokeLinecap="round"
                    vectorEffect="non-scaling-stroke"
                />
            </svg>
            <span
                className="absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-brand ring-2 ring-white"
                style={{ left: `${x(lastIndex)}%`, top: `${y(last)}%` }}
            />
        </div>
    );
};
