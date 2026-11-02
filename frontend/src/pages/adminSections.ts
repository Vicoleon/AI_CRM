import {
    Users,
    Shield,
    Globe,
    Lock,
    ToggleRight,
    Sparkles,
    MessageSquare,
    Briefcase,
    DollarSign,
    Tag,
    FolderTree,
    Building2,
    CalendarClock,
    LogIn,
    ThumbsUp,
    Send,
    Megaphone,
    Store,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

/**
 * The Administration sections, grouped — and the single source of truth for
 * them.
 *
 * THIS LIVES OUTSIDE Admin.tsx ON PURPOSE. Two places need it and they must not
 * drift: the app's left nav panel (components/Layout.tsx) renders the tree,
 * because that is where an area's destinations belong, and Admin.tsx derives its
 * tab union and its `?tab=` allow-list from it so a new section cannot be added
 * without both the menu and the validation learning about it.
 *
 * The groups are the mental model an admin already has: who the company is,
 * who can get in, how the AI behaves, what feeds it, and the org itself.
 *
 * Icons are component references rather than elements so each consumer picks its
 * own size — the panel renders at 17px, and anything else can differ without a
 * second copy of this list.
 *
 * `departments` is a tab here, not a route of its own: /departments redirects to
 * ?tab=departments, which keeps old bookmarks and Help links alive.
 */
export const ADMIN_NAV_GROUPS = [
    {
        title: 'Workspace',
        items: [
            /* The company profile every AI prompt reads, the product/service
               catalog and the proposal templates. All three ship empty, and
               the prompts say "the company" until the profile is filled in. */
            { key: 'company', label: 'Company setup', Icon: Store },
        ],
    },
    {
        title: 'Access',
        items: [
            { key: 'users', label: 'Users', Icon: Users },
            { key: 'roles', label: 'Roles', Icon: Shield },
            { key: 'workspace', label: 'Google Workspace', Icon: Globe },
            { key: 'access', label: 'Access Control', Icon: Lock },
            /* Which teams may reach each gated feature — the core copilot,
               X-Ray, CRM Sync, the cockpit views, Smart Teams … Onboarding a
               department used to be a code change and a deploy; it is a
               checkbox here now. */
            { key: 'features', label: 'Feature Access', Icon: ToggleRight },
        ],
    },
    {
        title: 'AI Settings',
        items: [
            { key: 'priorities', label: 'AI Priorities', Icon: Sparkles },
            { key: 'chat', label: 'Chat', Icon: MessageSquare },
            /* Which model each sales feature runs on — proposals, call wrap-up,
               X-Ray, CRM Sync. Swapping one used to be a code change
               and a deploy; it is a dropdown here now, per job. */
            { key: 'salesmodels', label: 'Sales Models', Icon: Briefcase },
            { key: 'aicost', label: 'AI Cost', Icon: DollarSign },
        ],
    },
    {
        title: 'Productivity',
        items: [
            { key: 'labels', label: 'Drive Labels', Icon: Tag },
            { key: 'folders', label: 'Folders', Icon: FolderTree },
        ],
    },
    {
        title: 'Admin',
        items: [
            { key: 'departments', label: 'Departments', Icon: Building2 },
            { key: 'l10', label: 'L10 Meetings', Icon: CalendarClock },
            /* The Loose Ends daily Slack post: on/off and its channel (#sales).
               It ships off; turning it on used to be an env var and a deploy. */
            { key: 'looseends', label: 'Loose Ends', Icon: Send },
            /* What's New: the Slack channel and switch of the morning post
               of new release notes, and the weekly tease. It applies to
               everyone, so it lives here rather than in Settings;
               an old /settings?tab=whats-new link redirects here. */
            { key: 'whatsnew', label: "What's New", Icon: Megaphone },
            { key: 'logins', label: 'Login Activity', Icon: LogIn },
            { key: 'feedback', label: 'Feedback', Icon: ThumbsUp },
        ],
    },
] as const satisfies ReadonlyArray<{
    title: string;
    items: ReadonlyArray<{ key: string; label: string; Icon: LucideIcon }>;
}>;

/** Every section key, e.g. `'users' | 'roles' | …`. */
export type AdminTab = (typeof ADMIN_NAV_GROUPS)[number]['items'][number]['key'];

/** Flat allow-list for validating an incoming `?tab=`. */
export const ADMIN_TABS: readonly string[] = ADMIN_NAV_GROUPS.flatMap(g =>
    g.items.map(i => i.key)
);

export const isAdminTab = (value: string | null): value is AdminTab =>
    value !== null && ADMIN_TABS.includes(value);

/** The section a URL resolves to. An unknown or absent tab means Users. */
export const adminTabFromSearch = (search: string): AdminTab => {
    const requested = new URLSearchParams(search).get('tab');
    return isAdminTab(requested) ? requested : 'users';
};
