// The style map: where people sit, and where a group clusters or is thin.
//
// PROVENANCE — READ BEFORE CHANGING ANY LABEL HERE
// -----------------------------------------------
// Marston's MODEL is public domain and `disc.py`'s legal header says so: "we own
// the model, we author everything else ourselves." Every vendor's *instrument*
// and *chart* is theirs. So this drawing is built only from things we own:
//
//   * the four letters D / I / S / C, from the 1928 model;
//   * our own seventeen style names ("Front Runner", not a vendor's vocabulary);
//   * ring labels that are the instrument's OWN axis words — agency (getting
//     ahead) and communion (getting along), quoted from `disc.py`;
//   * the brand colour, one hue, not a vendor's four-colour quadrant palette.
//
// `disc.py` names one thing as forbidden outright: Wiley's trademarked
// lowercase-i styling. DISC is written in FULL CAPS everywhere below and must
// stay that way. Do not relabel the rings with vendor words (ACTION,
// ENTHUSIASM, COLLABORATION, SUPPORT and friends are Everything DiSC trade
// dress), and do not adopt their green/red/blue/yellow quadrants.
//
// WHAT THE PICTURE MAY AND MAY NOT SAY
// ------------------------------------
// Position is direction only — every placed dot is at the same radius, so the
// map cannot draw "how strongly D" anybody is. There is no ideal point, no
// distance-to-ideal, no ordering. People who share a style land on the same
// anchor and are fanned around it deterministically, because two dots in one
// place IS the finding: those two read the same.
//
// Somebody whose DISC layer is closed has no marker. They are listed under the
// map as "not placed" rather than dropped — a person missing from a team
// picture reads as though they were not on the team.
import React from 'react';

import type { StyleMarker } from '../../../api/smartTeams';

export interface MapDot {
    /** Stable across renders. */
    id: string;
    label: string;
    marker: StyleMarker | null;
    /** Draws filled rather than outlined — used for "you" on the pair map. */
    emphasis?: boolean;
}

/** Viewbox geometry. The circle is drawn in a -1..1 space and scaled here. */
const SIZE = 320;
const CENTRE = SIZE / 2;
const RADIUS = SIZE / 2 - 34; // room for the axis labels outside the ring

/** Fan people who share a style around their common anchor.
 *
 *  Deterministic by index, so a re-render never shuffles the picture. The offset
 *  is a few pixels — enough to see two dots, small enough that nobody reads it
 *  as a difference in position, because it is not one. */
function fan(x: number, y: number, index: number, total: number): [number, number] {
    const cx = CENTRE + x * RADIUS;
    const cy = CENTRE - y * RADIUS; // SVG y grows downward; agency grows up
    if (total <= 1) return [cx, cy];
    const step = (Math.PI * 2) / total;
    const spread = 9;
    return [cx + Math.cos(index * step) * spread, cy + Math.sin(index * step) * spread];
}

/** The four work-phase clusters, in `heatmap.CLUSTER_ORDER`, each owning a fixed
 *  slot on the badge ring: 12, 3, 6 and 9 o'clock in that order.
 *
 *  Fixed slots are what make the badge readable without colour — a reader (or a
 *  colour-blind one) identifies Momentum by it being the right-hand notch, not by
 *  its hue. Distinct hues are still used because these are NOMINAL categories,
 *  not positions on a scale; the one-hue rule elsewhere exists so a scale cannot
 *  imply a good and a bad end, and four unordered categories have neither. */
const CLUSTER_SLOTS: { key: string; label: string; hue: string }[] = [
    { key: 'sense_making', label: 'Sense-Making', hue: '#2563eb' },
    { key: 'momentum', label: 'Momentum', hue: '#059669' },
    { key: 'connection', label: 'Connection', hue: '#d97706' },
    { key: 'stewardship', label: 'Stewardship', hue: '#7c3aed' },
];

const clusterLabel = (key: string): string =>
    CLUSTER_SLOTS.find(slot => slot.key === key)?.label ?? key;

const DOT_R = 8;
const BADGE_R = 12.5;

/** One dot's cluster ring: an arc per covered cluster, in its fixed slot. */
function badgeArcs(clusters: string[], cx: number, cy: number) {
    const covered = new Set(clusters);
    return CLUSTER_SLOTS.map((slot, index) => {
        if (!covered.has(slot.key)) return null;
        // Quarter arcs starting at 12 o'clock, with a small gap between them so
        // adjacent slots stay countable rather than merging into one ring.
        const gap = 0.16;
        const start = -Math.PI / 2 + index * (Math.PI / 2) + gap / 2;
        const end = start + Math.PI / 2 - gap;
        const x1 = cx + Math.cos(start) * BADGE_R;
        const y1 = cy + Math.sin(start) * BADGE_R;
        const x2 = cx + Math.cos(end) * BADGE_R;
        const y2 = cy + Math.sin(end) * BADGE_R;
        return (
            <path
                key={slot.key}
                d={`M ${x1} ${y1} A ${BADGE_R} ${BADGE_R} 0 0 1 ${x2} ${y2}`}
                fill="none"
                stroke={slot.hue}
                strokeWidth={2.5}
                strokeLinecap="round"
            />
        );
    });
}

const QUADRANTS: { key: string; label: string; x: number; y: number }[] = [
    { key: 'D', label: 'D', x: -0.5, y: 0.5 },
    { key: 'I', label: 'I', x: 0.5, y: 0.5 },
    { key: 'C', label: 'C', x: -0.5, y: -0.5 },
    { key: 'S', label: 'S', x: 0.5, y: -0.5 },
];

export const TeamMap: React.FC<{
    dots: MapDot[];
    /** Shown above the map. Say what the picture is of, not what it means. */
    caption?: string;
}> = ({ dots, caption }) => {
    const placed = dots.filter(d => d.marker);
    const unplaced = dots.filter(d => !d.marker);

    // Group by anchor so shared styles fan instead of overlapping invisibly.
    const byAnchor = new Map<string, MapDot[]>();
    for (const dot of placed) {
        // Keyed on POSITION, not on the style label: two people can land on the
        // same spot from different layers now that Big Five also votes, and they
        // still need fanning apart to both be visible.
        const key = `${dot.marker!.x}:${dot.marker!.y}`;
        if (!byAnchor.has(key)) byAnchor.set(key, []);
        byAnchor.get(key)!.push(dot);
    }

    return (
        <div>
            {caption && (
                <p className="mb-2 text-xs leading-relaxed text-gray-500">{caption}</p>
            )}
            <div className="flex flex-wrap items-start gap-5">
                <svg
                    viewBox={`0 0 ${SIZE} ${SIZE}`}
                    className="h-[280px] w-[280px] flex-shrink-0"
                    role="img"
                    aria-label={
                        placed.length
                            ? `Style map. ${placed
                                  .map(
                                      d =>
                                          `${d.label}${d.marker!.style_name ? ` is ${d.marker!.style_name}` : ''}` +
                                          (d.marker!.clusters?.length
                                              ? `, covering ${d.marker!.clusters
                                                    .map(c => clusterLabel(c))
                                                    .join(' and ')}`
                                              : ''),
                                  )
                                  .join('; ')}.`
                            : 'Style map with nobody placed yet.'
                    }
                >
                    {/* One flat tint for the whole disc, not four coloured
                        quadrants. Two reasons, and both are deliberate: the
                        four-colour quadrant palette is vendor trade dress, and
                        `big_five.Band` is explicit that there is no good or bad
                        end of these scales — so no quadrant may be drawn as
                        weightier than another. The cross lines and the four
                        letters carry the structure instead. */}
                    <circle cx={CENTRE} cy={CENTRE} r={RADIUS} className="fill-brand/[0.055]" />

                    <circle
                        cx={CENTRE}
                        cy={CENTRE}
                        r={RADIUS}
                        className="fill-none stroke-gray-300"
                        strokeDasharray="3 4"
                    />
                    <line x1={CENTRE - RADIUS} y1={CENTRE} x2={CENTRE + RADIUS} y2={CENTRE} className="stroke-gray-300" />
                    <line x1={CENTRE} y1={CENTRE - RADIUS} x2={CENTRE} y2={CENTRE + RADIUS} className="stroke-gray-300" />

                    {/* The four letters. FULL CAPS, always — see the header. */}
                    {QUADRANTS.map(q => (
                        <text
                            key={q.key}
                            x={CENTRE + q.x * RADIUS * 0.92}
                            y={CENTRE - q.y * RADIUS * 0.92}
                            textAnchor="middle"
                            dominantBaseline="central"
                            className="fill-brand/25 text-[34px] font-bold"
                        >
                            {q.label}
                        </text>
                    ))}

                    {/* Our own axis words, from disc.py. Never a vendor's ring. */}
                    <text x={CENTRE} y={13} textAnchor="middle" className="fill-gray-500 text-[10px] font-medium uppercase tracking-wider">
                        Getting ahead
                    </text>
                    <text x={CENTRE} y={SIZE - 5} textAnchor="middle" className="fill-gray-500 text-[10px] font-medium uppercase tracking-wider">
                        Steadier pace
                    </text>
                    <text x={9} y={CENTRE} textAnchor="middle" dominantBaseline="central" transform={`rotate(-90 9 ${CENTRE})`} className="fill-gray-500 text-[10px] font-medium uppercase tracking-wider">
                        Task first
                    </text>
                    <text x={SIZE - 9} y={CENTRE} textAnchor="middle" dominantBaseline="central" transform={`rotate(90 ${SIZE - 9} ${CENTRE})`} className="fill-gray-500 text-[10px] font-medium uppercase tracking-wider">
                        Getting along
                    </text>

                    {[...byAnchor.values()].map(group =>
                        group.map((dot, i) => {
                            const [cx, cy] = fan(dot.marker!.x, dot.marker!.y, i, group.length);
                            return (
                                <g key={dot.id}>
                                    {badgeArcs(dot.marker!.clusters ?? [], cx, cy)}
                                    <circle
                                        cx={cx}
                                        cy={cy}
                                        r={DOT_R}
                                        className={
                                            dot.emphasis
                                                ? 'fill-brand stroke-white'
                                                : 'fill-white stroke-brand'
                                        }
                                        strokeWidth={2.5}
                                    />
                                    <title>
                                        {dot.marker!.style_name
                                            ? `${dot.label} — ${dot.marker!.style_name}`
                                            : dot.label}
                                    </title>
                                </g>
                            );
                        }),
                    )}
                </svg>

                <div className="min-w-[10rem] flex-1">
                    <ul className="space-y-1.5">
                        {placed.map(dot => (
                            <li key={dot.id} className="flex items-baseline gap-2 text-sm">
                                <span
                                    aria-hidden="true"
                                    className={`mt-1 inline-block h-2.5 w-2.5 flex-shrink-0 rounded-full border-2 border-brand ${
                                        dot.emphasis ? 'bg-brand' : 'bg-white'
                                    }`}
                                />
                                <span className="text-gray-900">{dot.label}</span>
                                <span className="min-w-0 text-xs text-gray-500">
                                    {dot.marker!.style_name}
                                    {dot.marker!.style_name &&
                                        !!dot.marker!.clusters?.length &&
                                        ' · '}
                                    {dot.marker!.clusters?.map((cluster, i) => (
                                        <span key={cluster}>
                                            {i > 0 && ', '}
                                            <span
                                                aria-hidden="true"
                                                className="mr-0.5 inline-block h-2 w-2 rounded-full align-middle"
                                                style={{
                                                    backgroundColor: CLUSTER_SLOTS.find(
                                                        sl => sl.key === cluster,
                                                    )?.hue,
                                                }}
                                            />
                                            {clusterLabel(cluster)}
                                        </span>
                                    ))}
                                </span>
                            </li>
                        ))}
                    </ul>
                    {placed.some(d => d.marker!.clusters?.length) && (
                        <div className="mt-3 border-t border-gray-100 pt-2">
                            <p className="mb-1.5 text-xs font-medium text-gray-600">
                                The ring around a dot shows what they bring
                            </p>
                            <ul className="flex flex-wrap gap-x-3 gap-y-1">
                                {CLUSTER_SLOTS.map((slot, i) => (
                                    <li
                                        key={slot.key}
                                        className="flex items-center gap-1.5 text-xs text-gray-600"
                                    >
                                        <span
                                            aria-hidden="true"
                                            className="inline-block h-2.5 w-2.5 rounded-full"
                                            style={{ backgroundColor: slot.hue }}
                                        />
                                        {slot.label}
                                        <span className="text-gray-400">
                                            {['top', 'right', 'bottom', 'left'][i]}
                                        </span>
                                    </li>
                                ))}
                            </ul>
                            <p className="mt-1.5 text-xs leading-relaxed text-gray-500">
                                Each one always sits in the same place on the ring, so you can
                                read it without relying on the colour. No ring at all means
                                their strengths layer is closed — not that they cover none of
                                these.
                            </p>
                        </div>
                    )}

                    {unplaced.length > 0 && (
                        <p className="mt-3 border-t border-gray-100 pt-2 text-xs leading-relaxed text-gray-500">
                            Not placed:{' '}
                            <span className="text-gray-700">
                                {unplaced.map(d => d.label).join(', ')}
                            </span>
                            . Their answers did not support a working style, so there is no
                            position to draw — it does not mean they sit in the middle.
                        </p>
                    )}
                </div>
            </div>
        </div>
    );
};
