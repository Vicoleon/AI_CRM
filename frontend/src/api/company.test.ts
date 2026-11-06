// Drift tests for Admin → Company setup. The Python is read as text:
//
//   cd <repo>/frontend && node --test "src/api/company.test.ts"
//
// Pinned here:
//
//   * The paths the client calls are the ones backend/routers/company.py
//     serves, under the /api prefix main.py mounts it with (on `core`).
//   * The profile fields the form edits are exactly the CompanyProfile
//     dataclass fields, and the CSV header the page shows is the parser's.
//   * The empty-name fallback reads "your company", never a blank.
//   * The tab is registered in the Admin sections and rendered by Admin.tsx.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

// `api/company.ts` imports `./client` extensionless, the way every module in
// this app does. Teach the runner Vite's rule, scoped to our own files.
const resolveExtensionless: ResolveHookSync = (specifier, context, nextResolve) => {
    const parent = context.parentURL ?? '';
    const relative = /^\.{1,2}\//.test(specifier);
    if (parent.includes('/frontend/src/') && relative && !/\.[a-z]+$/i.test(specifier)) {
        return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
};

registerHooks({ resolve: resolveExtensionless });

const {
    CATALOG_CSV_HEADER,
    COMPANY_CATALOG_IMPORT_PATH,
    COMPANY_CATALOG_PATH,
    COMPANY_PROFILE_PATH,
    PROPOSAL_TEMPLATES_PATH,
    companyAdminError,
    companyNameOrDefault,
    emptyCompanyProfile,
    productsToText,
    textToProducts,
} = await import('./company.ts');

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string => fs.readFileSync(path.join(REPO, relative), 'utf8');

const ROUTER_PY = read('backend/routers/company.py');
const PROFILE_PY = read('rubik_core/company/profile.py');
const CATALOG_PY = read('rubik_core/company/catalog.py');
const MAIN_PY = read('backend/main.py');
const SECTIONS_TS = read('frontend/src/pages/adminSections.ts');
const ADMIN_TSX = read('frontend/src/pages/Admin.tsx');

describe('company API paths', () => {
    it('match the router, mounted on core under /api', () => {
        assert.match(ROUTER_PY, /APIRouter\(prefix="\/company"/);
        assert.match(MAIN_PY, /core\.include_router\(company\.router\)/);
        const served = [...ROUTER_PY.matchAll(/@router\.\w+\("([^"]+)"/g)].map(m => `/api/company${m[1]}`);
        for (const p of [COMPANY_PROFILE_PATH, COMPANY_CATALOG_PATH, COMPANY_CATALOG_IMPORT_PATH, PROPOSAL_TEMPLATES_PATH]) {
            assert.ok(served.includes(p), `${p} is not served (router serves ${served.join(', ')})`);
        }
    });
});

describe('company profile', () => {
    it('edits exactly the dataclass fields', () => {
        const body = /class CompanyProfile:[\s\S]*?\n\n {4}@property/.exec(PROFILE_PY)?.[0] ?? '';
        const pyFields = [...body.matchAll(/^ {4}(\w+): /gm)].map(m => m[1]).sort();
        assert.deepEqual(Object.keys(emptyCompanyProfile()).sort(), pyFields);
    });

    it('turns the products textarea into a clean list and back', () => {
        assert.deepEqual(textToProducts(' Nightly clean \n\n Window wash\n'), ['Nightly clean', 'Window wash']);
        assert.equal(productsToText(['A', 'B']), 'A\nB');
    });

    it('falls back to "your company" for an empty name', () => {
        assert.equal(companyNameOrDefault(''), 'your company');
        assert.equal(companyNameOrDefault(undefined), 'your company');
        assert.equal(companyNameOrDefault('  Acme Corp '), 'Acme Corp');
    });

    it('shows the server sentence for a refused save', () => {
        const err = { response: { data: { detail: 'A template needs a name.' } } };
        assert.equal(companyAdminError(err, 'fallback'), 'A template needs a name.');
        assert.equal(companyAdminError(new Error('x'), 'fallback'), 'fallback');
    });
});

describe('catalog upload', () => {
    it('shows the header the parser expects', () => {
        const header = /HEADER: tuple\[str, \.\.\.\] = \(([^)]*)\)/.exec(CATALOG_PY)?.[1] ?? '';
        const columns = [...header.matchAll(/"(\w+)"/g)].map(m => m[1]);
        assert.equal(CATALOG_CSV_HEADER, columns.join(','));
    });
});

describe('the Company setup tab', () => {
    it('is registered under Workspace and rendered by Admin.tsx', () => {
        assert.match(SECTIONS_TS, /title: 'Workspace'[\s\S]*?key: 'company'/);
        assert.match(ADMIN_TSX, /activeTab === 'company' && <CompanyTab \/>/);
    });
});
