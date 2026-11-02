// Drift and wiring tests for Insights. The contract (docs/insights/README.md),
// the backend registry and the app's route table are read as TEXT, the way
// looseEnds.test.ts and cockpit.test.ts pin theirs:
//
//   cd <repo>/frontend && node --test "src/pages/insights/insightsWiring.test.ts"
//
// Pinned here:
//
//   * The dashboard registry is the contract's table (§2) — same slugs,
//     titles, groups and phases — and every slug is registered by the backend
//     in `rubik_core/insights/dashboards/__init__.py`.
//   * The client's section kinds and units are the contract's.
//   * The client calls the endpoints the contract names.
//   * Errors normalise the way the pages expect (404 → not enabled / not found,
//     403 → forbidden).
//   * The surface is wired: routes (literal before parameter), the route guard
//     and the nav on `hasFeature('insights')`, the FEATURE keys, the Help
//     topic and its gate, and the shared chart theme.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import type { ResolveHookSync } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';
import { oldNames } from '../../testing/legacyBrand.ts';

// `api/insights.ts` imports `./client` extensionless, the way every module in
// this app does. Teach the runner Vite's rule, scoped to our own files (an
// unscoped hook breaks axios's CJS requires).
const resolveExtensionless: ResolveHookSync = (specifier, context, nextResolve) => {
    const parent = context.parentURL ?? '';
    const relative = /^\.{1,2}\//.test(specifier);
    if (parent.includes('/frontend/src/') && relative && !/\.[a-z]+$/i.test(specifier)) {
        return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
};

registerHooks({ resolve: resolveExtensionless });

const { DASHBOARDS, DASHBOARD_GROUPS, navDashboards, insightsPath, isDashboardPath, personPath } = await import('./dashboards.ts');
const { SECTION_KINDS, insightsError, isForbidden, isNotEnabled, isNotFound } = await import('../../api/insights.ts');
const { HELP_TOPICS } = await import('../help/helpTopics.ts');

const FRONTEND = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..', '..', '..');
const REPO = path.resolve(FRONTEND, '..');
const read = (relative: string): string => fs.readFileSync(path.join(REPO, relative), 'utf8');
const readIfExists = (relative: string): string | null => {
    const full = path.join(REPO, relative);
    return fs.existsSync(full) ? fs.readFileSync(full, 'utf8') : null;
};

const CONTRACT_MD = read('docs/insights/README.md');
const APP_TSX = read('frontend/src/App.tsx');
const LAYOUT_TSX = read('frontend/src/components/Layout.tsx');
const AUTH_TSX = read('frontend/src/context/AuthContext.tsx');
const API_TS = read('frontend/src/api/insights.ts');
const HELP_ACCESS_TS = read('frontend/src/pages/help/useHelpAccess.ts');
const TOPIC_PAGES_TSX = read('frontend/src/pages/help/topicPages.tsx');
const EXEC_DASHBOARD_TSX = read('frontend/src/pages/eos/ExecDashboard.tsx');
const BACKEND_REGISTRY = 'rubik_core/insights/dashboards/__init__.py';

/** `| \`overview\` | Overview | company | 1 |` rows of the contract's §2 table. */
function contractDashboards(): { slug: string; title: string; group: string; phase: number }[] {
    const section = CONTRACT_MD.split('## 2. Dashboards', 2)[1]?.split('\n## ', 1)[0] ?? '';
    const rows = [...section.matchAll(/^\|\s*`([a-z-]+)`\s*\|\s*([^|]+?)\s*\|\s*([a-z]+)\s*\|\s*(\d)\s*\|$/gm)];
    return rows.map(m => ({ slug: m[1], title: m[2], group: m[3], phase: Number(m[4]) }));
}

describe('the dashboard registry is the contract’s', () => {
    const contract = contractDashboards();

    it('parses the contract table', () => {
        assert.equal(contract.length, 10, 'docs/insights/README.md §2 should list ten dashboards');
    });

    it('lists the same dashboards, in the same order, with the same title, group and phase', () => {
        assert.deepEqual(
            DASHBOARDS.map(d => ({ slug: d.slug, title: d.title, group: d.group, phase: d.phase })),
            contract,
        );
    });

    it('ships every dashboard live', () => {
        assert.deepEqual(
            DASHBOARDS.filter(d => d.live).map(d => d.slug),
            DASHBOARDS.map(d => d.slug),
        );
        assert.equal(DASHBOARDS.length, 10);
    });

    it('renders the groups in the contract’s order and words', () => {
        assert.deepEqual(
            DASHBOARD_GROUPS.map(g => `${g.key} "${g.title}"`).join(', '),
            'company "Company", work "Work", sales "Sales", platform "Platform"',
        );
        assert.match(CONTRACT_MD, /Groups render in this order: company "Company", work "Work", sales "Sales",\s+platform "Platform"\./);
    });

    it('has every slug registered by the backend', () => {
        const py = readIfExists(BACKEND_REGISTRY);
        assert.ok(
            py !== null,
            `${BACKEND_REGISTRY} does not exist yet. The backend registers the dashboards there; ` +
                'this test checks every slug in pages/insights/dashboards.ts against it.',
        );
        const missing = DASHBOARDS.map(d => d.slug).filter(slug => !py.includes(`"${slug}"`) && !py.includes(`'${slug}'`));
        assert.deepEqual(missing, [], `slugs in dashboards.ts that ${BACKEND_REGISTRY} does not register`);
    });

    it('builds paths and knows which page is which', () => {
        assert.equal(insightsPath('overview'), '/insights');
        assert.equal(insightsPath('ask-cubie'), '/insights/ask-cubie');
        assert.equal(personPath('kendall+x@acme.example'), '/insights/people/kendall%2Bx%40acme.example');
        assert.ok(isDashboardPath('/insights', 'overview'));
        assert.ok(isDashboardPath('/insights/overview', 'overview'));
        assert.ok(isDashboardPath('/insights/adoption/', 'adoption'));
        assert.ok(!isDashboardPath('/insights/adoption', 'overview'));
    });

    it('folds the nav under the group headings, hiding what the server says is off', () => {
        const nav = navDashboards();
        assert.deepEqual(nav.map(g => g.title), ['Company', 'Work', 'Sales', 'Platform']);
        assert.deepEqual(nav.flatMap(g => g.dashboards.map(d => d.slug)), [
            'overview',
            'adoption',
            'ask-cubie',
            'priorities',
            'follow-through',
            'crm',
            'proposals',
            'ai-spend',
            'knowledge',
            'quality',
        ]);
        const withServer = navDashboards(new Map([['knowledge', false], ['adoption', false]]));
        assert.ok(withServer.some(g => g.title === 'Platform'));
        assert.ok(!withServer.flatMap(g => g.dashboards).some(d => d.slug === 'knowledge'));
        assert.ok(!withServer.flatMap(g => g.dashboards).some(d => d.slug === 'adoption'));
    });
});

describe('the client’s vocabulary is the contract’s', () => {
    it('section kinds', () => {
        const block = (CONTRACT_MD.split('The shape of `data` depends on `kind`:', 2)[1] ?? '').split('**Units:**', 1)[0];
        const kinds = [...block.matchAll(/^- `([a-z]+)`/gm)].map(m => m[1]);
        assert.ok(kinds.length > 0, 'could not read the section kinds from the contract');
        assert.deepEqual([...SECTION_KINDS], kinds);
    });

    it('units', () => {
        const line = /\*\*Units:\*\*([^\n]+)/.exec(CONTRACT_MD)?.[1] ?? '';
        const contractUnits = [...line.matchAll(/`([a-z]+)`/g)].map(m => m[1]).sort();
        const union = /export type InsightsUnit =([\s\S]*?);/.exec(API_TS)?.[1] ?? '';
        const clientUnits = [...union.matchAll(/'([a-z]+)'/g)].map(m => m[1]).sort();
        assert.deepEqual(clientUnits, contractUnits);
    });

    it('filter ranges', () => {
        assert.match(CONTRACT_MD, /`7d`, `30d`, `90d`, `qtd`, `ytd`, `custom` \| `30d`/);
    });

    it('endpoints', () => {
        for (const endpoint of ['GET /insights/meta', 'GET /insights/dashboards/{slug}', 'GET /insights/people/{email}', 'POST /insights/dashboards/{slug}/summary']) {
            assert.ok(CONTRACT_MD.includes(endpoint), `contract no longer names ${endpoint}`);
        }
        assert.match(API_TS, /const BASE = '\/api\/insights';/);
        assert.match(API_TS, /`\$\{BASE\}\/meta`/);
        assert.match(API_TS, /`\$\{BASE\}\/dashboards\/\$\{encodeURIComponent\(slug\)\}`/);
        assert.match(API_TS, /`\$\{BASE\}\/dashboards\/\$\{encodeURIComponent\(slug\)\}\/summary`/);
        assert.match(API_TS, /`\$\{BASE\}\/people\/\$\{encodeURIComponent\(email\)\}`/);
    });

    it('the backend router serves them, once it exists', () => {
        const router = readIfExists('backend/routers/insights.py');
        if (router === null) return; // written by the backend in the same PR
        for (const piece of ['/meta', '/dashboards/', '/summary', '/people/']) {
            assert.ok(router.includes(piece), `backend/routers/insights.py has no "${piece}" route`);
        }
    });
});

describe('errors', () => {
    const rejection = (status: number, detail?: unknown) => ({ response: { status, data: { detail } } });

    it('a 404 is the kill switch on a dashboard read', () => {
        const e = insightsError(rejection(404, 'Not Found'));
        assert.ok(isNotEnabled(e));
        assert.equal(e.status, 404);
    });

    it('a 404 on a person read means outside your departments', () => {
        const e = insightsError(rejection(404), { notFound: 'not_found' });
        assert.ok(isNotFound(e));
        assert.ok(!isNotEnabled(e));
    });

    it('a 403 is forbidden, keeping the server’s sentence', () => {
        const e = insightsError(rejection(403, 'Department "ops" is outside your scope.'));
        assert.ok(isForbidden(e));
        assert.equal(e.message, 'Department "ops" is outside your scope.');
        assert.ok(isForbidden(insightsError(rejection(403, { code: 'x', message: 'Nope.' }))));
    });

    it('a 400/422 is a filter problem, and anything else a plain error', () => {
        assert.equal(insightsError(rejection(422)).code, 'bad_request');
        assert.equal(insightsError(rejection(500, 'Boom.')).message, 'Boom.');
        assert.equal(insightsError(new Error('Network Error')).message, 'Network Error');
        assert.equal(insightsError(undefined).code, 'error');
    });
});

describe('the surface is wired', () => {
    it('registers the routes, the literal people segment before the parameter', () => {
        const root = APP_TSX.indexOf('<Route path="insights" element={<FeatureRoute feature="insights"><InsightsDashboardPage /></FeatureRoute>} />');
        const people = APP_TSX.indexOf('<Route path="insights/people/:email" element={<FeatureRoute feature="insights"><InsightsPersonProfile /></FeatureRoute>} />');
        const param = APP_TSX.indexOf('<Route path="insights/:dashboard" element={<FeatureRoute feature="insights"><InsightsDashboardPage /></FeatureRoute>} />');
        assert.ok(root > 0, 'no /insights route');
        assert.ok(people > 0, 'no /insights/people/:email route');
        assert.ok(param > 0, 'no /insights/:dashboard route');
        assert.ok(people < param, 'the literal people/ route must come before :dashboard');
    });

    it('guards the routes on the insights verdict, waiting for the profile', () => {
        const guard = /const FeatureRoute: React\.FC[\s\S]*?\n\};/.exec(APP_TSX)?.[0] ?? '';
        assert.ok(guard, 'FeatureRoute not found');
        assert.match(guard, /fallback = '\/'/);
        assert.match(guard, /isLoading \|\| !user/);
        assert.match(guard, /if \(!hasFeature\(feature\)\) return <Navigate to=\{fallback\} replace \/>;/);
    });

    it('offers the rail area on the insights verdict, carrying the filters', () => {
        assert.match(LAYOUT_TSX, /const canAccessInsights = hasFeature\('insights'\);/);
        assert.match(LAYOUT_TSX, /if \(canAccessInsights\) \{/);
        assert.match(LAYOUT_TSX, /key: 'insights',/);
        assert.match(LAYOUT_TSX, /owns: p => startsWithAny\(p, \['\/insights'\]\),/);
        assert.match(LAYOUT_TSX, /filterQueryFromSearch\(location\.search\)/);
        assert.match(LAYOUT_TSX, /\| 'insights'/);
        const deps = LAYOUT_TSX.split('return built.filter(a => a.items.length > 0);', 2)[1]?.split(']);', 1)[0] ?? '';
        assert.match(deps, /\bcanAccessInsights,/, 'canAccessInsights missing from the areas useMemo dependencies');
        for (const heading of ['Company', 'Work', 'Platform']) {
            assert.match(LAYOUT_TSX, new RegExp(`\\n\\s+${heading}: <`), `SECTION_ICONS has no "${heading}" glyph`);
        }
    });

    it('knows both feature keys', () => {
        assert.match(AUTH_TSX, /insights: 'insights',/);
        assert.match(AUTH_TSX, /insightsPeople: 'insights_people',/);
    });

    it('has a Help topic behind the same verdict, with a page', () => {
        const topic = HELP_TOPICS.find(t => t.id === 'insights');
        assert.ok(topic, 'no insights Help topic');
        assert.equal(topic.title, 'Insights dashboards');
        assert.equal(topic.requires, 'insights');
        assert.match(HELP_ACCESS_TS, /insights: canAccessInsights,/);
        assert.match(HELP_ACCESS_TS, /const canAccessInsights = hasFeature\('insights'\);/);
        assert.match(TOPIC_PAGES_TSX, /^\s+insights: \{ Page: Insights, Icon: /m);
    });

    it('shares one chart theme with the EOS Exec Dashboard', () => {
        assert.match(EXEC_DASHBOARD_TSX, /import \{ CHART_OPTIONS_BASE \} from '\.\.\/\.\.\/components\/insights\/chartTheme';/);
        assert.doesNotMatch(EXEC_DASHBOARD_TSX, /ChartJS\.register\(/);
        assert.doesNotMatch(EXEC_DASHBOARD_TSX, /const CHART_OPTIONS_BASE =/);
    });

    it('never names an old product or company to users', () => {
        const hits = [
            'frontend/src/pages/insights/InsightsDashboardPage.tsx',
            'frontend/src/pages/insights/PersonProfile.tsx',
            'frontend/src/pages/help/topics/Insights.tsx',
            'frontend/src/components/insights/PageStates.tsx',
            'frontend/src/components/insights/AiSummaryCard.tsx',
        ].flatMap(file => oldNames(read(file)).map(name => `${file}: ${name}`));
        assert.deepEqual(hits, []);
    });
});
