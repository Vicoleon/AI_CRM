// Overview's tiles, grouped by the dashboard each one links to.
//
//   cd <repo>/frontend && node --test "src/pages/insights/kpiGroups.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { dashboardSlugOf, groupKpisByDashboard } from './kpiGroups.ts';

interface Tile {
    id: string;
    href?: string | null;
}

const tile = (id: string, href?: string | null): Tile => ({ id, href });

describe('dashboardSlugOf', () => {
    it('reads the slug from an Insights link, query and hash included', () => {
        assert.equal(dashboardSlugOf('/insights/crm'), 'crm');
        assert.equal(dashboardSlugOf('/insights/ai-spend?range=7d&dept=sales'), 'ai-spend');
        assert.equal(dashboardSlugOf('/insights/priorities#funnel'), 'priorities');
        assert.equal(dashboardSlugOf('/insights/eos/'), 'eos');
    });

    it('is null for no link, a link outside Insights, or Overview itself', () => {
        assert.equal(dashboardSlugOf(undefined), null);
        assert.equal(dashboardSlugOf(null), null);
        assert.equal(dashboardSlugOf(''), null);
        assert.equal(dashboardSlugOf('/insights'), null);
        assert.equal(dashboardSlugOf('/insights?range=7d'), null);
        assert.equal(dashboardSlugOf('/eos/dashboard'), null);
        assert.equal(dashboardSlugOf('/insights/people/a%40b.com'), null);
    });
});

describe('groupKpisByDashboard', () => {
    it('puts unlinked tiles first, then dashboards in registry order, tiles in their own order', () => {
        const groups = groupKpisByDashboard([
            tile('spend', '/insights/ai-spend'),
            tile('acted', '/insights/priorities'),
            tile('own', null),
            tile('questions', '/insights/ask-cubie'),
            tile('surfaced', '/insights/priorities?dept=sales'),
        ]);
        assert.deepEqual(
            groups.map(g => [g.slug, g.kpis.map(k => k.id)]),
            [
                [null, ['own']],
                ['ask-cubie', ['questions']],
                ['priorities', ['acted', 'surfaced']],
                ['ai-spend', ['spend']],
            ],
        );
    });

    it('titles each group from the registry, and leaves the unlinked group untitled', () => {
        const groups = groupKpisByDashboard([tile('own'), tile('q', '/insights/ask-cubie')]);
        assert.deepEqual(
            groups.map(g => g.title),
            [null, 'Ask Cubie'],
        );
    });

    it('treats a link to an unknown dashboard as no link', () => {
        const groups = groupKpisByDashboard([tile('mystery', '/insights/not-a-dashboard'), tile('own')]);
        assert.equal(groups.length, 1);
        assert.equal(groups[0].slug, null);
        assert.equal(groups[0].title, null);
        assert.deepEqual(
            groups[0].kpis.map(k => k.id),
            ['mystery', 'own'],
        );
    });

    it('returns no groups for no tiles', () => {
        assert.deepEqual(groupKpisByDashboard([]), []);
    });
});
