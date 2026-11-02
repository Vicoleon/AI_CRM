// The old product and company names, for the frontend's `node --test` guards.
//
// The repo's canonical banned-terms scan is backend/tests/_legacy_brand.py
// (LEGACY_PATTERN, run over every file by test_no_legacy_brand.py). This is
// its frontend twin for the copy checks that run with `npm test`: the old
// product names and the old company, matched by the same rules.
// legacyBrand.test.ts pins both term lists to the Python ones.
//
// Test-only: *.test.ts files import it, the app never does. It reads files
// with node:fs, so tsconfig.app.json leaves src/testing out and
// tsconfig.test.json checks it.
//
// Every term is assembled from fragments, so neither this module nor the
// tests that import it ever match the scan themselves.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

/** The frontend folder. Hits are reported relative to it (`src/tours/…`). */
const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));

/** The old product names. Mirrors `_PRODUCT_NAMES` in backend/tests/_legacy_brand.py. */
export const PRODUCT_NAMES: readonly string[] = ['At' + 'las', 'Bri' + 'an', 'Jar' + 'vis'];

/** The old company's full name (which is also its email domain) and its
 *  abbreviation, as regex source matched in any case. Mirrors `_COMPANY` in
 *  backend/tests/_legacy_brand.py. */
export const COMPANY_PATTERNS: readonly string[] = ['mq' + 'm' + '[\\s_-]*' + 'research', 'mq' + 'mr'];

/** A product name as a whole token in any casing or identifier style (prose,
 *  kebab, snake, camelCase, SCREAMING_CASE), but not inside a longer word of
 *  its own case. The same rule as `_product_name` in the Python helper. */
function productName(word: string): string {
    const lower = word.toLowerCase();
    const title = word.charAt(0).toUpperCase() + lower.slice(1);
    const upper = word.toUpperCase();
    return `${title}(?![a-z])|(?<![a-z])${lower}(?![a-z])|(?<![A-Z])${upper}(?![A-Z])`;
}

const OLD_PRODUCT_NAMES = new RegExp(PRODUCT_NAMES.map(productName).join('|'), 'g');
const OLD_COMPANY_NAMES = new RegExp(COMPANY_PATTERNS.join('|'), 'gi');

/** Every old product or company name in `text`: product names first, then the company. */
export function oldNames(text: string): string[] {
    return [...text.matchAll(OLD_PRODUCT_NAMES), ...text.matchAll(OLD_COMPANY_NAMES)].map(m => m[0]);
}

/** Every file under `dir`, at any depth, tests included. */
export function everyFile(dir: string): string[] {
    return readdirSync(dir).flatMap(name => {
        const full = join(dir, name);
        return statSync(full).isDirectory() ? everyFile(full) : [full];
    });
}

/** Every old name in the files under `dir`, as `src/…/file.ts:12: name`, and
 *  in their paths below `dir` (folder and file names), as
 *  `src/…/file.ts (file name): name`. The folders above `dir` are not checked. */
export function oldNamesUnder(dir: string): string[] {
    return everyFile(dir).flatMap(file => {
        const rel = relative(FRONTEND, file);
        return [
            ...oldNames(relative(dir, file)).map(name => `${rel} (file name): ${name}`),
            ...readFileSync(file, 'utf8')
                .split('\n')
                .flatMap((line, i) => oldNames(line).map(name => `${rel}:${i + 1}: ${name}`)),
        ];
    });
}
