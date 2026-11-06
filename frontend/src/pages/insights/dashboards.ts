// The Insights dashboard registry: every dashboard's slug, title, nav group,
// one-line description, icon and phase.
//
// THE SLUGS ARE THE BACKEND'S. `rubik_core/insights/dashboards/__init__.py`
// registers the same eleven, and docs/insights/README.md §2 lists them;
// `insightsWiring.test.ts` reads the Python as text and fails on any slug that
// exists on one side only.
//
// `live` is the nav's answer to "has this shipped?" — the left rail is built
// before `/meta` is fetched, so it cannot ask the server. The page itself
// still asks: `/meta`'s `available` wins wherever the two disagree, so a
// dashboard switched off on the server shows "Coming soon" even if this file
// says live.
//
// A leaf module (no imports), so `node --test` loads it without a bundler.
// Icons are NAMES here; `components/insights/dashboardIcons.ts` maps them to
// lucide components.

export const DASHBOARD_GROUPS = [
    { key: 'company', title: 'Company' },
    { key: 'work', title: 'Work' },
    { key: 'sales', title: 'Sales' },
    { key: 'platform', title: 'Platform' },
] as const;

export type DashboardGroup = (typeof DASHBOARD_GROUPS)[number]['key'];

export type DashboardIconName =
    | 'overview'
    | 'adoption'
    | 'chat'
    | 'priorities'
    | 'followThrough'
    | 'crm'
    | 'proposals'
    | 'spend'
    | 'knowledge'
    | 'quality';

export interface DashboardEntry {
    slug: string;
    title: string;
    /** A shorter nav row where the title would be cut off in the 204px panel. */
    navLabel?: string;
    group: DashboardGroup;
    /** The question the dashboard answers, in one line. */
    description: string;
    icon: DashboardIconName;
    phase: 1 | 2 | 3;
    /** Shipped, so it has a nav row. */
    live: boolean;
}

export const DEFAULT_DASHBOARD = 'overview';

export const DASHBOARDS: readonly DashboardEntry[] = [
    {
        slug: 'overview',
        title: 'Overview',
        group: 'company',
        description: 'Is Cubie working for the company? The headline number from every dashboard, by department.',
        icon: 'overview',
        phase: 1,
        live: true,
    },
    {
        slug: 'adoption',
        title: 'Adoption & Engagement',
        navLabel: 'Adoption',
        group: 'company',
        description: 'Who uses Cubie, how often, and which features they rely on.',
        icon: 'adoption',
        phase: 1,
        live: true,
    },
    {
        slug: 'ask-cubie',
        title: 'Ask Cubie',
        group: 'work',
        description: 'How much people rely on chat, how fast it answers, and whether the answers help.',
        icon: 'chat',
        phase: 1,
        live: true,
    },
    {
        slug: 'priorities',
        title: 'AI Priorities',
        group: 'work',
        description: 'What Cubie caught each morning, and what people did with it.',
        icon: 'priorities',
        phase: 1,
        live: true,
    },
    {
        slug: 'follow-through',
        title: 'Follow-through',
        group: 'work',
        description: 'Are promises and loose ends closing, and how fast?',
        icon: 'followThrough',
        phase: 2,
        live: true,
    },
    {
        slug: 'crm',
        title: 'CRM & HubSpot',
        group: 'sales',
        description: 'What Cubie wrote into HubSpot, and whether people trust it.',
        icon: 'crm',
        phase: 2,
        live: true,
    },
    {
        slug: 'proposals',
        title: 'Proposals & Prep',
        group: 'sales',
        description: 'Proposals drafted, meeting briefs prepared, and how much they were edited.',
        icon: 'proposals',
        phase: 2,
        live: true,
    },
    {
        slug: 'ai-spend',
        title: 'AI Spend',
        group: 'platform',
        description: 'What Cubie costs, per feature, per department and per active person.',
        icon: 'spend',
        phase: 3,
        live: true,
    },
    {
        slug: 'knowledge',
        title: 'Knowledge & Content',
        navLabel: 'Knowledge',
        group: 'platform',
        description: 'Is the knowledge base growing, and are answers using it?',
        icon: 'knowledge',
        phase: 3,
        live: true,
    },
    {
        slug: 'quality',
        title: 'Quality & Trust',
        group: 'platform',
        description: 'Where the AI is strongest and weakest, one trust scorecard per surface.',
        icon: 'quality',
        phase: 3,
        live: true,
    },
];

export const findDashboard = (slug: string | null | undefined): DashboardEntry | undefined =>
    DASHBOARDS.find(d => d.slug === slug);

export const dashboardGroupTitle = (key: string): string =>
    DASHBOARD_GROUPS.find(g => g.key === key)?.title ?? key;

/** The in-app path of a dashboard. Overview lives at the area's root. */
export const insightsPath = (slug: string): string =>
    slug === DEFAULT_DASHBOARD ? '/insights' : `/insights/${slug}`;

/** The person profile's path. */
export const personPath = (email: string): string => `/insights/people/${encodeURIComponent(email)}`;

/**
 * The dashboards a nav lists, folded under their group headings in group
 * order. `available` (from `/meta`) overrides `live` when given.
 */
export function navDashboards(available?: ReadonlyMap<string, boolean>): { group: DashboardGroup; title: string; dashboards: DashboardEntry[] }[] {
    const shown = DASHBOARDS.filter(d => (available?.has(d.slug) ? available.get(d.slug) === true : d.live));
    return DASHBOARD_GROUPS.map(g => ({
        group: g.key,
        title: g.title,
        dashboards: shown.filter(d => d.group === g.key),
    })).filter(g => g.dashboards.length > 0);
}

/** Is this pathname the given dashboard's page? `/insights` and `/insights/overview` are both Overview. */
export function isDashboardPath(pathname: string, slug: string): boolean {
    const clean = pathname.replace(/\/+$/, '') || '/';
    if (slug === DEFAULT_DASHBOARD) return clean === '/insights' || clean === '/insights/overview';
    return clean === `/insights/${slug}`;
}
