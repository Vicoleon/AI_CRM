// The Insights chart palette.
//
// CATEGORICAL. Eight hues in a FIXED order, validated as a set (the dataviz
// skill's `validate_palette.js`, light mode on the white card surface): every
// adjacent pair clears the colour-blind separation target (worst ΔE 14.0,
// protan; tritan 9.6) and the normal-vision floor (worst ΔE 17.4). Slot 1 is
// Cubie violet (#7c3aed). Green sits between it and blue on purpose: violet
// beside blue drops to ΔE 5.2 for deuteranopes. Three slots (green, yellow,
// pink) are under 3:1 against white, which is why every chart here also
// carries a legend, a value label or its "View as table" alternative.
//
// Series are coloured by POSITION in the server's list, which is stable per
// dashboard, so a colour follows its series rather than its rank. There is no
// ninth hue: anything past eight is drawn in neutral grey ("Other").
//
// SEQUENTIAL (heat cells, the funnel, the activity calendar): one hue, Cubie
// violet, light to dark, with the brand violet itself as a stop. DIVERGING:
// teal ↔ grey ↔ violet, with grey meaning "no difference" (blue would sit too
// close to violet for colour-blind readers).
//
// Hex, not the CSS variables: the ramps interpolate between stops in JS. A
// single brand mark that should follow a workspace accent uses the `bg-brand`
// class or `getBrandColor` instead.
//
// Pure (no imports), so `node --test` loads it.

export const SERIES_COLORS = ['#7c3aed', '#1baf7a', '#2a78d6', '#eda100', '#008300', '#e87ba4', '#9c4a1a', '#eb6834'] as const;

/** Past the eighth series: neutral, never a generated hue. */
export const OTHER_COLOR = '#9ca3af';

export const seriesColor = (index: number): string => SERIES_COLORS[index] ?? OTHER_COLOR;

/** Light → dark violet. The first stop is the near-zero wash; the fourth is Cubie violet. */
export const SEQUENTIAL_STOPS = ['#f5f3ff', '#ddd6fe', '#a78bfa', '#7c3aed', '#5b21b6', '#2e1065'] as const;

const DIVERGING_LOW = '#0d9488';
const DIVERGING_MID = '#f0efec';
const DIVERGING_HIGH = '#7c3aed';

type Rgb = [number, number, number];

const toRgb = (hex: string): Rgb => {
    const h = hex.replace('#', '');
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
};

const toHex = ([r, g, b]: Rgb): string =>
    `#${[r, g, b].map(v => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, '0')).join('')}`;

const mix = (a: string, b: string, t: number): string => {
    const ra = toRgb(a);
    const rb = toRgb(b);
    return toHex([ra[0] + (rb[0] - ra[0]) * t, ra[1] + (rb[1] - ra[1]) * t, ra[2] + (rb[2] - ra[2]) * t]);
};

const clamp01 = (t: number): number => (Number.isFinite(t) ? Math.max(0, Math.min(1, t)) : 0);

/** `t` in 0–1 → a point on the violet ramp. */
export function sequentialColor(t: number): string {
    const x = clamp01(t) * (SEQUENTIAL_STOPS.length - 1);
    const i = Math.min(SEQUENTIAL_STOPS.length - 2, Math.floor(x));
    return mix(SEQUENTIAL_STOPS[i], SEQUENTIAL_STOPS[i + 1], x - i);
}

/** `t` in −1…1 → teal (below) through grey (0) to violet (above). */
export function divergingColor(t: number): string {
    const v = Number.isFinite(t) ? Math.max(-1, Math.min(1, t)) : 0;
    return v < 0 ? mix(DIVERGING_MID, DIVERGING_LOW, -v) : mix(DIVERGING_MID, DIVERGING_HIGH, v);
}

/** Relative luminance (WCAG). */
export function luminance(hex: string): number {
    const lin = toRgb(hex).map(v => {
        const c = v / 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

/** White or ink for text set on a filled cell, whichever reads better. */
export const inkOn = (hex: string): string => (luminance(hex) < 0.32 ? '#ffffff' : '#111827');

/** An ordinal step for the i-th of n ordered marks (funnel steps): darkest
 *  first, never lighter than a step that still clears 2:1 on white. */
export const ordinalColor = (i: number, n: number): string =>
    sequentialColor(n <= 1 ? 0.8 : 0.8 - (i / (n - 1)) * 0.38);

/** `rgba()` for a hex at `alpha`, for area washes. */
export const withAlpha = (hex: string, alpha: number): string => {
    const [r, g, b] = toRgb(hex);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
};

/** The activity calendar's five shades: none, then four steps of violet. */
export const CALENDAR_SHADES = ['#ededea', '#ddd6fe', '#a78bfa', '#7c3aed', '#5b21b6'] as const;
