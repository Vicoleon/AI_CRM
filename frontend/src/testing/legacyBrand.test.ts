// Pins the frontend's old-name matcher (legacyBrand.ts) to the repo's
// canonical banned-terms scan, backend/tests/_legacy_brand.py, so the two
// cannot drift apart, and checks it matches the way that scan does:
//
//   cd <repo>/frontend && node --test "src/testing/legacyBrand.test.ts"
//
// Every name below is assembled from the lists the helper exports, so this
// file never matches the scan itself.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { COMPANY_PATTERNS, PRODUCT_NAMES, oldNames } from './legacyBrand.ts';

const PYTHON_HELPER = readFileSync(new URL('../../../backend/tests/_legacy_brand.py', import.meta.url), 'utf8');

/** The strings of the Python list literal `name = [...]`, each element's
 *  `"a" + r"b"` fragments joined back into one string. */
function pythonList(name: string): string[] {
    const body = PYTHON_HELPER.match(new RegExp(`^${name} = \\[([\\s\\S]*?)\\]\\n`, 'm'))?.[1];
    assert.ok(body !== undefined, `${name} is not a list literal in backend/tests/_legacy_brand.py`);
    const code = body.replace(/^\s*#.*$/gm, '');
    return [...code.matchAll(/(?:r?"[^"]*"\s*\+\s*)*r?"[^"]*"/g)].map(element =>
        [...element[0].matchAll(/r?"([^"]*)"/g)].map(fragment => fragment[1]).join(''),
    );
}

describe('the frontend old-name matcher', () => {
    it('bans the product names the repo scan bans', () => {
        assert.deepEqual([...PRODUCT_NAMES], pythonList('_PRODUCT_NAMES'));
    });

    it('bans the company names the repo scan bans', () => {
        assert.deepEqual([...COMPANY_PATTERNS], pythonList('_COMPANY'));
    });

    it('finds a product name in prose and in every identifier style', () => {
        for (const word of PRODUCT_NAMES) {
            const lower = word.toLowerCase();
            const upper = word.toUpperCase();
            for (const text of [`Ask ${word} today`, `ask-${lower}`, `ask_${lower}`, `ask${word}Href`, `ASK_${upper}`]) {
                assert.equal(oldNames(text).length, 1, text);
            }
        }
    });

    it('leaves a longer word that only contains a product name alone', () => {
        for (const word of PRODUCT_NAMES) {
            const text = `${word}sian my${word.toLowerCase()} X${word.toUpperCase()}`;
            assert.deepEqual(oldNames(text), [], text);
        }
    });

    it('finds the company by name, email domain or abbreviation, in any case', () => {
        const [name, abbreviation] = ['mq' + 'm', 'mq' + 'mr'];
        for (const text of [`${name} Research`, `${name.toUpperCase()}_RESEARCH`, `someone@${name}research.com`, abbreviation.toUpperCase()]) {
            assert.equal(oldNames(text).length, 1, text);
        }
    });

    it('finds nothing in the new names', () => {
        assert.deepEqual(oldNames('Ask Cubie, by RubikSoft, for your company'), []);
    });
});
