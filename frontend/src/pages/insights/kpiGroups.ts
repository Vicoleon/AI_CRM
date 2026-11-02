// Overview's headline tiles, grouped by the dashboard each one comes from.
//
// Overview shows every dashboard's headline numbers. With eleven dashboards
// that is twenty-odd tiles, and one flat grid of them reads as noise:
// "Active share" beside "Marked 'not a real miss'" beside "Promises kept".
// Each tile already carries `href`, the dashboard that owns the number
// (docs/insights/README.md §4), so the page groups tiles by it: one small
// heading per dashboard, in the same order as the nav (the registry in
// `dashboards.ts`). Tiles with no link (Overview's own numbers) lead, with no
// heading. A link to a slug the registry does not know is treated as no link,
// so a newer server can never produce an untitled heading.
//
// A leaf module (it imports only `dashboards.ts`, with its extension), so
// `node --test` loads it without a bundler.
import { DASHBOARDS } from './dashboards.ts';

export interface KpiGroup<K> {
    /** The dashboard the tiles open, or null for tiles that open nothing. */
    slug: string | null;
    /** That dashboard's title from the registry; null with `slug`. */
    title: string | null;
    kpis: K[];
}

/**
 * The dashboard slug in an Insights link: `/insights/crm?range=7d` → `crm`.
 * Null for no link, a link outside Insights, or the bare `/insights`
 * (Overview itself).
 */
export function dashboardSlugOf(href: string | null | undefined): string | null {
    if (!href) return null;
    const path = href.split('#', 1)[0].split('?', 1)[0];
    const m = /^\/insights\/([^/]+)\/?$/.exec(path);
    return m ? decodeURIComponent(m[1]) : null;
}

/**
 * Tiles grouped by the dashboard they link to: unlinked tiles first, then
 * one group per dashboard in registry order, each keeping the tiles' own
 * order.
 */
export function groupKpisByDashboard<K extends { href?: string | null }>(kpis: readonly K[]): KpiGroup<K>[] {
    const order = new Map(DASHBOARDS.map((d, i) => [d.slug, i]));
    const groups = new Map<string | null, KpiGroup<K>>();
    for (const k of kpis) {
        const raw = dashboardSlugOf(k.href);
        const slug = raw !== null && order.has(raw) ? raw : null;
        let group = groups.get(slug);
        if (!group) {
            const title = slug === null ? null : (DASHBOARDS[order.get(slug) ?? -1]?.title ?? null);
            group = { slug, title, kpis: [] };
            groups.set(slug, group);
        }
        group.kpis.push(k);
    }
    const rank = (slug: string | null): number => (slug === null ? -1 : (order.get(slug) ?? Number.MAX_SAFE_INTEGER));
    return [...groups.values()].sort((a, b) => rank(a.slug) - rank(b.slug));
}
