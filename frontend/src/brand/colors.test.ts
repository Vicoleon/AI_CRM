// Brand colours for code that cannot use a Tailwind class (chart canvases,
// inline styles) come from the CSS variables, so a workspace's own accent
// reaches them too. With no variable set, they fall back to Cubie's palette.
//
//   cd <repo>/frontend && node --test src/brand/colors.test.ts
import assert from 'node:assert/strict';
import { afterEach, describe, it } from 'node:test';

import { getBrandColor, invalidateBrandColors } from './colors.ts';

const scope = globalThis as unknown as Record<string, unknown>;
const saved = { document: scope.document, getComputedStyle: scope.getComputedStyle };

/** A document whose root carries exactly these custom properties. */
function stubVariables(variables: Record<string, string>): void {
    scope.document = { documentElement: {} };
    scope.getComputedStyle = () => ({
        getPropertyValue: (name: string) => variables[name] ?? '',
    });
}

afterEach(() => {
    scope.document = saved.document;
    scope.getComputedStyle = saved.getComputedStyle;
});

describe('getBrandColor', () => {
    it('reads the CSS variable as rgb()', () => {
        stubVariables({ '--brand': '124 58 237' });
        assert.equal(getBrandColor('brand'), 'rgb(124 58 237)');
    });

    it('follows whatever the variable holds, so a workspace accent applies', () => {
        stubVariables({ '--brand': ' 3 85 62 ', '--brand-hover': '1 2 3' });
        assert.equal(getBrandColor('brand'), 'rgb(3 85 62)');
        assert.equal(getBrandColor('brandHover'), 'rgb(1 2 3)');
    });

    it('falls back to the Cubie palette when the variable is unset', () => {
        stubVariables({});
        assert.equal(getBrandColor('brand'), 'rgb(124 58 237)');
        assert.equal(getBrandColor('brandHover'), 'rgb(109 40 217)');
        assert.equal(getBrandColor('tint'), 'rgb(242 240 247)');
        assert.equal(getBrandColor('accent'), 'rgb(164 123 255)');
        assert.equal(getBrandColor('ink'), 'rgb(14 7 41)');
    });

    it('falls back with no DOM at all, and when reading styles throws', () => {
        scope.document = undefined;
        scope.getComputedStyle = undefined;
        assert.equal(getBrandColor('brand'), 'rgb(124 58 237)');

        scope.document = { documentElement: {} };
        scope.getComputedStyle = () => {
            throw new Error('no styles');
        };
        assert.equal(getBrandColor('ink'), 'rgb(14 7 41)');
    });

    it('adds an alpha channel when asked', () => {
        stubVariables({ '--brand': '124 58 237' });
        assert.equal(getBrandColor('brand', 0.18), 'rgb(124 58 237 / 0.18)');
    });
});

/** A document whose root carries `attributes`, and counts style reads. */
function stubCountingDocument(variables: Record<string, string>) {
    const attributes: Record<string, string> = {};
    const root = { getAttribute: (name: string) => attributes[name] ?? null };
    const reads = { count: 0 };
    scope.document = { documentElement: root };
    scope.getComputedStyle = () => {
        reads.count += 1;
        return { getPropertyValue: (name: string) => variables[name] ?? '' };
    };
    return { attributes, variables, reads };
}

describe('getBrandColor caching', () => {
    it('reads the styles once per colour, not on every render', () => {
        const { reads } = stubCountingDocument({ '--brand': '124 58 237', '--ink': '14 7 41' });
        for (let i = 0; i < 5; i += 1) {
            assert.equal(getBrandColor('brand'), 'rgb(124 58 237)');
            assert.equal(getBrandColor('brand', 0.45), 'rgb(124 58 237 / 0.45)');
        }
        assert.equal(reads.count, 1);
        getBrandColor('ink');
        getBrandColor('ink');
        assert.equal(reads.count, 2);
    });

    it('reads afresh when the theme on the root changes', () => {
        const { attributes, variables, reads } = stubCountingDocument({ '--brand': '124 58 237' });
        assert.equal(getBrandColor('brand'), 'rgb(124 58 237)');

        variables['--brand'] = '3 85 62';
        attributes['data-theme'] = 'dark';
        assert.equal(getBrandColor('brand'), 'rgb(3 85 62)');

        variables['--brand'] = '1 2 3';
        attributes.style = '--brand: 1 2 3';
        assert.equal(getBrandColor('brand'), 'rgb(1 2 3)');

        variables['--brand'] = '4 5 6';
        attributes.class = 'acme-accent';
        assert.equal(getBrandColor('brand'), 'rgb(4 5 6)');
        assert.equal(getBrandColor('brand'), 'rgb(4 5 6)');
        assert.equal(reads.count, 4);
    });

    it('reads afresh after an explicit invalidation', () => {
        const { variables, reads } = stubCountingDocument({ '--brand': '124 58 237' });
        assert.equal(getBrandColor('brand'), 'rgb(124 58 237)');
        variables['--brand'] = '9 9 9';
        assert.equal(getBrandColor('brand'), 'rgb(124 58 237)', 'still cached');
        invalidateBrandColors();
        assert.equal(getBrandColor('brand'), 'rgb(9 9 9)');
        assert.equal(reads.count, 2);
    });

    it('does not cache an unset variable, so a late stylesheet still applies', () => {
        const { variables, reads } = stubCountingDocument({});
        assert.equal(getBrandColor('brand'), 'rgb(124 58 237)');
        variables['--brand'] = '3 85 62';
        assert.equal(getBrandColor('brand'), 'rgb(3 85 62)');
        assert.equal(getBrandColor('brand'), 'rgb(3 85 62)');
        assert.equal(reads.count, 2);
    });
});
