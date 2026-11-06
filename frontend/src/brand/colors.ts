// Brand colours for code that cannot use a Tailwind class: chart canvases and
// the odd inline style. They are read from the CSS variables `index.css`
// defines (RGB channels, e.g. `--brand: 124 58 237`), so a workspace's own
// accent reaches a chart exactly as it reaches a `bg-brand` button. With no
// variable set, or no DOM at all, they fall back to Cubie's palette.
//
// Reading a variable means `getComputedStyle`, which can force a style
// recalculation, and these are called during render. So each value is read
// once per theme and cached. The theme is the root element's `class`,
// `data-theme` and `style` (where a theme switch or a workspace accent lands),
// so changing any of them reads the variables afresh; anything else that
// changes them (a stylesheet swapped in) calls `invalidateBrandColors()`.
//
// Pure (no imports), so `node --test` loads it.

export type BrandColorName = 'brand' | 'brandHover' | 'tint' | 'accent' | 'ink';

const VARIABLES: Record<BrandColorName, string> = {
    brand: '--brand',
    brandHover: '--brand-hover',
    tint: '--brand-tint',
    accent: '--accent',
    ink: '--ink',
};

/** Cubie's palette as RGB channels: violet, darker violet, off-white, lavender, ink. */
const FALLBACK: Record<BrandColorName, string> = {
    brand: '124 58 237',
    brandHover: '109 40 217',
    tint: '242 240 247',
    accent: '164 123 255',
    ink: '14 7 41',
};

interface ThemeCache {
    theme: string;
    channels: Map<BrandColorName, string>;
}

// Per root element, so a different document (a test's stub, an iframe) never
// sees another's values.
let caches = new WeakMap<object, ThemeCache>();

function rootElement(): Element | undefined {
    try {
        return globalThis.document?.documentElement ?? undefined;
    } catch {
        return undefined;
    }
}

/** What identifies the root's theme: the attributes a theme change touches. */
function themeOf(root: Element): string {
    const attribute = (name: string): string => {
        try {
            return typeof root.getAttribute === 'function' ? (root.getAttribute(name) ?? '') : '';
        } catch {
            return '';
        }
    };
    return [attribute('class'), attribute('data-theme'), attribute('style')].join('|');
}

function readVariable(root: Element, variable: string): string {
    try {
        if (typeof globalThis.getComputedStyle !== 'function') return '';
        return globalThis.getComputedStyle(root).getPropertyValue(variable).trim();
    } catch {
        return '';
    }
}

/** The variable's channels for the current theme, read at most once per theme. */
function channelsFor(name: BrandColorName): string {
    const root = rootElement();
    if (!root) return '';
    const theme = themeOf(root);
    let cache = caches.get(root);
    if (!cache || cache.theme !== theme) {
        cache = { theme, channels: new Map() };
        caches.set(root, cache);
    }
    const cached = cache.channels.get(name);
    if (cached !== undefined) return cached;
    const channels = readVariable(root, VARIABLES[name]);
    // An unset variable is not cached: the stylesheet may simply not be
    // applied yet, and the next call should see it once it is.
    if (channels) cache.channels.set(name, channels);
    return channels;
}

/** Forget every cached brand colour, for a change the theme key cannot see. */
export function invalidateBrandColors(): void {
    caches = new WeakMap();
}

/**
 * `rgb(…)` for a brand colour, with an optional `alpha` (0–1).
 * `getBrandColor('brand')` → `"rgb(124 58 237)"`.
 */
export function getBrandColor(name: BrandColorName, alpha?: number): string {
    const channels = channelsFor(name) || FALLBACK[name];
    return alpha === undefined ? `rgb(${channels})` : `rgb(${channels} / ${alpha})`;
}
