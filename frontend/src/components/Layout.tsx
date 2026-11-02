import React, { useState, useEffect, useRef, useMemo } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { eosUiGate } from '../auth/eosAccess';
import { useNotifications } from '../context/NotificationsContext';
import client from '../api/client';
import { ToastHost } from './eos/Toast';
import { ConfirmHost } from './eos/ConfirmDialog';
import { toast } from './eos/Toast';
import { useTours } from '../tours/context';
import { NotificationBell } from './notifications/NotificationBell';
import { NotificationToaster } from './notifications/NotificationToaster';
import { VoiceCommandBar } from './voice/VoiceCommandBar';
import { WhatsNewButton } from './whatsNew/WhatsNewButton';
import { WhatsNewDrawer } from './whatsNew/WhatsNewDrawer';
import { useWhatsNew } from './whatsNew/WhatsNewProvider';
import { isLiveL10Path } from './whatsNew/logic';
import type { VoiceDestination } from '../api/voiceCommands';
import {
    MessageSquare,
    FileBox,
    LogOut,
    Shield,
    Sunrise,
    Briefcase,
    Loader2,
    CheckCircle2,
    X,
    Settings as SettingsIcon,
    Compass,
    Target,
    BarChart3,
    Mountain,
    ListTodo,
    AlertCircle,
    Calendar,
    Workflow,
    HelpCircle,
    RefreshCw,
    Cpu,
    TrendingUp,
    Bell,
    PhoneCall,
    Users,
    ClipboardList,
    ListChecks,
    FileSignature,
    FileText,
    Wrench,
    GitCompare,
    UsersRound,
    Search,
    Sparkles,
    Mail,
    SlidersHorizontal,
    PanelLeftClose,
    UserMinus,
    ChevronDown,
    Building2,
    CalendarClock,
    Link2Off,
    Eye,
    EyeOff,
} from 'lucide-react';
import { ADMIN_NAV_GROUPS, adminTabFromSearch } from '../pages/adminSections';
import { groupedHelpTopics, helpGroupTitle, helpPath } from '../pages/help/helpTopics';
import { HELP_GROUP_ICONS, TOPIC_PAGES } from '../pages/help/topicPages';
import { useHelpAccess } from '../pages/help/useHelpAccess';
import { dashboardGroupTitle, insightsPath, isDashboardPath, navDashboards } from '../pages/insights/dashboards';
import { filterQueryFromSearch } from '../pages/insights/insightsFilters';
import { DASHBOARD_ICONS } from './insights/dashboardIcons';
import { motion } from 'framer-motion';
import { PRODUCT_NAME, VENDOR_NAME, cubeMark } from '../brand/brand';

/* ─── Shell geometry ─────────────────────────────────────────────────────────
 *
 * The nav is two tiers: a permanent icon rail of AREAS, plus a panel showing
 * only the active area's destinations. Widths are constants rather than
 * Tailwind literals because the aside animates between them and the footer has
 * to span whichever total is current.
 */
const RAIL_WIDTH = 68;
const PANEL_WIDTH = 204;
/**
 * The cube mark in the rail's lavender. `cubeMark` is filled with
 * `currentColor`, but as an <img> it can only show its own violet, which is
 * dim on the ink rail; used as a mask it takes the element's text colour.
 */
const RailCubeMark: React.FC<{ className?: string }> = ({ className = '' }) => {
    // Quoted: Vite inlines the small SVG as a data URI with quotes inside it.
    const mask = `url("${cubeMark}") center / contain no-repeat`;
    return (
        <span
            aria-hidden="true"
            className={`inline-block flex-shrink-0 bg-current text-accent ${className}`}
            style={{ WebkitMask: mask, mask }}
        />
    );
};

/** Panel open/closed survives a reload — '0' means the user collapsed it. */
const PANEL_STATE_KEY = 'navPanelOpen';
/**
 * Which panel sections the reader folded away, as a `{"area:section": true}`
 * map. Same reasoning as PANEL_STATE_KEY: a nav someone tidied should still be
 * tidy after a reload.
 */
const SECTION_STATE_KEY = 'navSectionsCollapsed';

/**
 * Glyph beside a section heading, keyed by the heading text. Declared here
 * rather than carried on an item because every row in My Work sits behind its
 * own gate — "the first item of the Sales section" is a different row for
 * sales operations, the sales director and an AE, so there is no stable item
 * to hang it on.
 * A section with no entry renders label-only, which is what Administration
 * wants: its thirteen rows already carry icons of their own.
 */
// Every section heading needs an entry here, or it renders bare next to the
// ones that have a glyph.
const SECTION_ICONS: Record<string, React.ReactNode> = {
    'My Work': <Briefcase size={14} />,
    Sales: <TrendingUp size={14} />,
    'HR & Culture': <UsersRound size={14} />,
    // The Insights panel's groups (pages/insights/dashboards.ts). "Sales"
    // above already covers that one.
    Company: <Building2 size={14} />,
    Work: <Briefcase size={14} />,
    Platform: <Cpu size={14} />,
    // The Help panel's headings (pages/help/helpTopics.ts). Spread last but
    // never overriding: "My Work" and "Sales" carry the same glyph either way.
    ...Object.fromEntries(
        Object.entries(HELP_GROUP_ICONS).filter(([k]) => !['My Work', 'Sales'].includes(k))
    ),
};

type AreaKey =
    | 'today'
    | 'work'
    | 'eos'
    | 'ask'
    | 'teams'
    | 'insights'
    | 'settings'
    | 'guide'
    | 'admin';

interface RouteMatch {
    pathname: string;
    search: string;
}

interface PanelItem {
    to: string;
    label: string;
    icon: React.ReactNode;
    /** True when this item is the page currently open. */
    isActive: (loc: RouteMatch) => boolean;
    /**
     * Replace the history entry instead of pushing one. Set on sections of a
     * single page (Settings, Administration), where always-visible links would
     * otherwise stack a Back press per section between the reader and the page
     * they came from.
     */
    replace?: boolean;
    /**
     * Collapsible group heading rendered above this item. Needed once an area
     * carries enough destinations that a flat list stops being scannable —
     * Administration's thirteen, My Work's twelve. A heading opens whenever this
     * differs from the item above, so ITEMS MUST BE LISTED IN SECTION ORDER: a
     * row filed out of order lands under the wrong heading, or reopens one
     * further down. Give the heading a glyph by adding it to SECTION_ICONS.
     */
    section?: string;
    /**
     * Show the unread dot on this row (and on its rail icon). Says "something
     * arrived here since you last looked" — never a count, because the number
     * only means anything once you are on the page that lists the items.
     */
    dot?: boolean;
}

interface Area {
    key: AreaKey;
    /** Short label under the rail icon — must fit 68px, so one word. */
    rail: string;
    /** Panel heading, where there is room to be explicit. */
    title: string;
    icon: React.ReactNode;
    items: PanelItem[];
    /**
     * Does the current route belong to this area? Wider than the item list on
     * purpose: detail routes with no nav entry of their own (a meeting-prep
     * page, an X-Ray, an L10 session) still have to keep their area lit.
     */
    owns: (pathname: string) => boolean;
}

/**
 * "Something new is waiting here."
 *
 * Lavender, not the usual alert red or amber: a warning colour on the nav
 * reads as a problem to fix, and a scheduled result that arrived on time is
 * good news.
 *
 * The SOFT lavender (`brand-soft`) rather than the `accent` lavender the Chat
 * sidebar uses: this dot also sits on the violet of the active area and the
 * open page, where the mid-tone accent all but vanishes. Not white: white is
 * already this nav's active-row border and reads as "you are here".
 */
const UnreadDot: React.FC<{ className?: string; title?: string }> = ({ className = '', title }) => (
    <span
        className={`inline-block h-2 w-2 rounded-full bg-brand-soft flex-shrink-0 ${className}`}
        title={title ?? 'New since you last looked'}
        aria-hidden="true"
    />
);

const exact = (to: string) => (loc: RouteMatch) => loc.pathname === to;
const under = (base: string) => (loc: RouteMatch) =>
    loc.pathname === base || loc.pathname.startsWith(`${base}/`);
/** Today's Calls owns the whole /calls subtree EXCEPT the script editor, which
 *  has a row of its own. Without the exclusion both rows light on /calls/scripts
 *  and the panel stops saying where you are. Still `under('/calls')` otherwise,
 *  because the wrap-up (`/calls/:id/wrapup`) is a detail screen of Today's Calls
 *  and has no row to light. */
const callsExceptScripts = (loc: RouteMatch) =>
    under('/calls')(loc) && !under('/calls/scripts')(loc);
/** Settings sections are one page keyed by ?tab=, defaulting to General. */
const settingsTab = (tab: string) => (loc: RouteMatch) =>
    loc.pathname === '/settings' &&
    (new URLSearchParams(loc.search).get('tab') || 'general') === tab;

/** Administration sections are one page keyed by ?tab=, defaulting to Users. */
const adminTab = (tab: string) => (loc: RouteMatch) =>
    loc.pathname === '/admin' && adminTabFromSearch(loc.search) === tab;

const startsWithAny = (pathname: string, bases: string[]) =>
    bases.some(b => pathname === b || pathname.startsWith(`${b}/`));

export const Layout: React.FC = () => {
    const {
        user,
        token,
        isLoading,
        logout,
        exitSupport,
        isAdmin,
        isExec,
        canAccessFullApp,
        canAccessCrmSync,
        canAccessCockpit,
        canAccessCockpitManager,
        canAccessCockpitSupervisor,
        canAccessCockpitExec,
        canAccessCcs,
        canAccessXray,
        canAccessSmartTeams,
        canAccessScheduledTasks,
        canAccessTodos,
        canAccessProposals,
        hasFeature,
        ingestionComplete,
        dismissIngestionBanner,
        isIngestingTranscripts,
    } = useAuth();
    const { showNav: canAccessEos } = eosUiGate({ token, user, isLoading });
    // Unread scheduled-task results, so a run that landed while you were
    // elsewhere in Cubie is visible without opening Chat to go look.
    const { chatUnreadCount, settingsUnreadCount } = useNotifications();
    // Page tours: the account menu replays the page's tour (TourProvider).
    const tours = useTours();
    // What's New is company-wide: its button shows for anyone the feature is
    // on for, once there is something to read (or drafts to review).
    const { showButton: showWhatsNewButton } = useWhatsNew();
    const location = useLocation();
    const navigate = useNavigate();

    const [departments, setDepartments] = useState<string[]>([]);
    /* The impersonation menu's value: a department name, or `user:<email>` for
       a person (read-only — see rubik_core/auth/impersonation.py). */
    const [impersonateValue, setImpersonateValue] = useState<string>(() => {
        const person = localStorage.getItem('impersonateUser');
        return person ? `user:${person}` : localStorage.getItem('impersonateDept') || '';
    });
    const [impersonationTargets, setImpersonationTargets] = useState<{ email: string; name: string }[]>([]);
    const [isPanelOpen, setIsPanelOpen] = useState<boolean>(
        () => localStorage.getItem(PANEL_STATE_KEY) !== '0'
    );
    /* Folded-away panel sections, keyed `area:section` rather than by heading
       alone, so two areas may carry the same heading text without sharing one
       collapsed state — Administration already has a "Productivity" group, and
       nothing stops another area gaining one. Parsed defensively: this is a
       cosmetic preference, and a corrupt or hand-edited entry must never be the
       reason the nav fails to render. */
    const [collapsedSections, setCollapsedSections] = useState<Record<string, boolean>>(() => {
        try {
            const parsed = JSON.parse(localStorage.getItem(SECTION_STATE_KEY) || '{}');
            return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
        } catch {
            return {};
        }
    });

    const toggleSection = (key: string) => {
        setCollapsedSections(prev => {
            const next = { ...prev, [key]: !prev[key] };
            try {
                localStorage.setItem(SECTION_STATE_KEY, JSON.stringify(next));
            } catch { /* a full or blocked store costs the preference, nothing else */ }
            return next;
        });
    };
    // A live L10 owns the whole screen. Users kept clicking this nav out of
    // habit and leaving the meeting mid-agenda, so hide the rail entirely while
    // a session is open. Safe to hide because every branch of the meeting page
    // carries its own way out ("Exit meeting" in the agenda rail; "Back to
    // meetings" on the loading, error and concluded states) — leaving an L10
    // should be a decision, not a stray click. Derived from the route rather
    // than written into `isPanelOpen`, so whatever the user chose for the panel
    // is restored the moment they exit. The same test keeps What's New's
    // weekly tease off the meeting (components/whatsNew/logic.ts).
    const isNavHidden = isLiveL10Path(location.pathname);
    const [isUserMenuOpen, setIsUserMenuOpen] = useState(false);
    const [isExitingSupport, setIsExitingSupport] = useState(false);
    const userMenuRef = useRef<HTMLDivElement>(null);
    /** The rail button for the active area — focus lands here on collapse. */
    const activeAreaButtonRef = useRef<HTMLButtonElement>(null);
    // Handoffs (offboard inheritance) shows only for people who actually hold
    // one: the roster of successors is the API's to know, and a nav item that
    // opens an empty page is noise. Admins and org-wide execs get every record
    // back, so the same call covers them.
    const [hasHandoffs, setHasHandoffs] = useState(false);

    useEffect(() => {
        let cancelled = false;
        client.get('/api/offboarding/records')
            .then(res => { if (!cancelled) setHasHandoffs((res.data?.records ?? []).length > 0); })
            .catch(() => { /* item simply stays hidden */ });
        return () => { cancelled = true; };
    }, []);

    // The Sales Cockpit's AUDIENCE is mirrorable and lives in AuthContext
    // (`canAccessCockpit` and the three role booleans). Whether the cockpit
    // EXISTS in this environment is not: `COCKPIT_ENABLED` is a server setting
    // the client never receives, it defaults OFF, and every cockpit endpoint
    // 404s until someone flips it. A roster-only gate would therefore ship a
    // "Sales" row to every AE and sales leader today, leading to a page that
    // says "not switched on yet" — and this feature's whole shipping promise is
    // that nothing reaches a live user before a flag flips. So ask the server,
    // and only for people the mirror already admits: everybody else pays no
    // request and sees no rows. Any failure (404 flag-off, 403, network) leaves
    // every row hidden — a nav item that 403s is worse than none.
    //
    // It probes the AE surface because that is the only GET behind
    // `require_cockpit` and there is no flag endpoint to ask instead, so the
    // cost is one extra My Day build per app load for the cockpit teams. If
    // that ever bites, the fix is a cheap `/api/sales/access` on the router —
    // not moving this decision back into the client.
    const [hasCockpit, setHasCockpit] = useState(false);

    useEffect(() => {
        // Keyed on the mirror rather than mount-only: the booleans are false
        // until /api/users/me lands, so a mount-only probe would never fire.
        if (!canAccessCockpit) return;
        let cancelled = false;
        client.get('/api/sales/my-day')
            .then(() => { if (!cancelled) setHasCockpit(true); })
            .catch(() => { /* rows simply stay hidden */ });
        return () => { cancelled = true; };
    }, [canAccessCockpit]);

    // Sales Call Capture is a SECOND flag, so it needs a second probe. `CCS_ENABLED`
    // and `COCKPIT_ENABLED` are independent server settings that both default off, so
    // reusing `hasCockpit` here would offer "Today's Calls" whenever the cockpit was on
    // and every /api/calls endpoint was still 404ing — the flag-off nav item this file
    // already refuses to ship. `/api/calls/pending` is the analogous GET behind
    // `require_ccs`, so the same rule applies: only people the mirror already admits pay
    // a request, and any failure (404 flag-off, 403, network) leaves the row hidden.
    const [hasCcs, setHasCcs] = useState(false);

    useEffect(() => {
        // Keyed on the mirror, not mount-only: `canAccessCcs` is false until
        // /api/users/me lands, so a mount-only probe would never fire.
        if (!canAccessCcs) return;
        let cancelled = false;
        client.get('/api/calls/pending')
            .then(() => { if (!cancelled) setHasCcs(true); })
            .catch(() => { /* the row simply stays hidden */ });
        return () => { cancelled = true; };
    }, [canAccessCcs]);

    // Call script AUTHORING is a THIRD question, narrower than either of the two
    // above, so it gets a third probe rather than reusing `hasCcs`. The row leads
    // to an editor, and three separate things have to be true before it is worth
    // offering: the caller is in the Call Capture audience (`require_ccs`), the
    // feature is switched on in this environment (`CCS_ENABLED`, a server setting
    // the client never receives), AND they may author — `include_drafts=true`
    // 403s for anyone outside `user_can_access_cockpit_supervisor` (the
    // `sales_cockpit_supervisor` roster in Admin → Feature Access, plus admins).
    // `GET /api/scripts?include_drafts=true` is the one request that asks all
    // three at once, which is exactly why it is the probe: it succeeds only when
    // the row would lead somewhere usable.
    //
    // The two mirrors gate the request itself so nobody else pays for it. Any
    // failure (404 flag-off, 403 either gate, network) leaves the row hidden: a
    // nav item that 403s is worse than none.
    const [hasScriptAuthoring, setHasScriptAuthoring] = useState(false);

    useEffect(() => {
        // Keyed on the mirrors, not mount-only: both are false until
        // /api/users/me lands, so a mount-only probe would never fire.
        if (!canAccessCcs || !canAccessCockpitSupervisor) return;
        let cancelled = false;
        client.get('/api/scripts', { params: { include_drafts: true } })
            .then(() => { if (!cancelled) setHasScriptAuthoring(true); })
            .catch(() => { /* the row simply stays hidden */ });
        return () => { cancelled = true; };
    }, [canAccessCcs, canAccessCockpitSupervisor]);

    useEffect(() => {
        localStorage.setItem(PANEL_STATE_KEY, isPanelOpen ? '1' : '0');
    }, [isPanelOpen]);

    useEffect(() => {
        if (!isUserMenuOpen) return;
        const onClickOutside = (e: MouseEvent) => {
            if (userMenuRef.current && !userMenuRef.current.contains(e.target as Node)) {
                setIsUserMenuOpen(false);
            }
        };
        const onEscape = (e: KeyboardEvent) => {
            if (e.key === 'Escape') setIsUserMenuOpen(false);
        };
        document.addEventListener('mousedown', onClickOutside);
        document.addEventListener('keydown', onEscape);
        return () => {
            document.removeEventListener('mousedown', onClickOutside);
            document.removeEventListener('keydown', onEscape);
        };
    }, [isUserMenuOpen]);

    useEffect(() => {
        if (isAdmin) {
            client.get('/api/departments/')
                .then(res => setDepartments(res.data))
                .catch(err => console.error("Failed to load departments", err));
            client.get<{ targets: { email: string; name: string }[] }>('/api/admin/impersonation/targets')
                .then(res => setImpersonationTargets(res.data.targets))
                .catch(() => setImpersonationTargets([]));
        }
    }, [isAdmin]);

    /**
     * The account menu lives inside the footer, which is only as wide as the
     * open panel — collapsing would clip it. Closing it here rather than in an
     * effect keeps it a consequence of the action that caused it.
     */
    const collapsePanel = () => {
        setIsPanelOpen(false);
        setIsUserMenuOpen(false);
        /* The collapse button unmounts with the panel it closed, which would
           drop focus to <body> and send a keyboard user back to the top of the
           document. Hand focus to the rail's active area button — the control
           that now re-opens the panel. */
        requestAnimationFrame(() => activeAreaButtonRef.current?.focus());
    };

    const handleLogout = () => {
        localStorage.removeItem('impersonateDept');
        localStorage.removeItem('impersonateUser');
        logout();
        navigate('/login');
    };

    const handleExitSupport = async () => {
        if (isExitingSupport) return;
        setIsExitingSupport(true);
        try {
            await exitSupport();
        } catch {
            toast.error('Could not exit platform support. Try again.');
            setIsExitingSupport(false);
        }
    };

    const handleImpersonateChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
        const val = e.target.value;
        localStorage.removeItem('impersonateDept');
        localStorage.removeItem('impersonateUser');
        if (val.startsWith('user:')) {
            localStorage.setItem('impersonateUser', val.slice('user:'.length));
        } else if (val) {
            localStorage.setItem('impersonateDept', val);
        }
        setImpersonateValue(val);
        window.location.assign('/');
    };

    /* Person impersonation strips the admin role, so the menu above is hidden
       while it runs — this is the way back. */
    const stopImpersonating = () => {
        localStorage.removeItem('impersonateUser');
        window.location.assign('/');
    };

    /* Which Help topics this reader may open — the same verdicts the Help pages
       use, so the panel never lists a topic its page would refuse. */
    const helpAccess = useHelpAccess();

    /* Loose Ends' audience is the server's verdict (`loop_closure`, Admin →
       Feature Access; /api/users/me folds LOOP_CLOSURE_ENABLED into it, so a
       switched-off feature arrives as false). Deliberately NOT behind
       `hasCockpit`: an AE who is on the Loose Ends roster but not the cockpit
       roster still gets their row. A leader with only the TEAM verdict
       (`loop_closure_team` — leaders who read the roll-up and are not
       scanned themselves) gets the row too, pointed straight at the Team tab. */
    /* Insights (the BI dashboards): the server's verdict, `insights` in Admin →
       Feature Access, with INSIGHTS_ENABLED folded in by /api/users/me. */
    const canAccessInsights = hasFeature('insights');

    const canSeeOwnLooseEnds = hasFeature('loop_closure');
    const canAccessLooseEnds = canSeeOwnLooseEnds || hasFeature('loop_closure_team');

    /* ─── The rail ───────────────────────────────────────────────────────────
     *
     * One continuous column, top to bottom: Today, Work, EOS, Ask, Teams,
     * Insights, Support, Settings, Help, Admin. Two orderings here are
     * deliberate and should not be "tidied":
     *
     *   - Ask (Chat) sits BELOW EOS. EOS is the company-wide surface and the
     *     one most people open first; Chat is reached on purpose, not
     *     browsed.
     *   - Help then Admin come last, and nothing is pinned to the floor — the
     *     column just runs out. Anything new goes ABOVE Help.
     *
     * An area with no visible items is dropped entirely, so the rail length is
     * a function of what this person can actually reach.
     */
    const areas = useMemo<Area[]>(() => {
        const built: Area[] = [];

        if (canAccessFullApp) {
            built.push({
                key: 'today',
                rail: 'Today',
                title: 'Today',
                icon: <Sunrise size={19} />,
                owns: p => p === '/' || startsWithAny(p, ['/meeting-prep']),
                items: [
                    { to: '/', label: 'Daily Briefing', icon: <Sunrise size={17} />, isActive: exact('/') },
                ],
            });

            /* THE PANEL IS THREE SECTIONS, NOT ONE LIST, and the headings are
               doing work the labels were being asked to do on their own. My Work
               had grown to twelve rows where a personal inbox, an AE's call list
               and an offboarding handoff sat as peers, so finding anything meant
               already knowing its label. The split answers the question a reader
               actually arrives with: is this MINE, is this SALES, is this PEOPLE.

               ITEMS MUST STAY IN SECTION ORDER. The renderer opens a heading
               whenever `section` changes, so a row filed out of order either
               lands under the wrong heading or reopens one further down. An empty
               section costs nothing — no items, no heading — which is what makes
               it safe for every row here to stay behind its own gate. */
            const workItems: PanelItem[] = [
                /* Productivity stays FIRST on purpose: the rail lands a click on
                   the Work icon on the area's first row (handleAreaClick), so
                   whichever row leads here is where "Work" opens. Putting My
                   To-Dos above it made the Work icon open the To-Dos page
                   instead of the panel-with-Productivity people expect. */
                { to: '/productivity', label: 'Productivity', section: 'My Work', icon: <Briefcase size={17} />, isActive: under('/productivity') },
                /* My To-Dos — the reader's own promises. Server verdict `todos`
                   (Admin → Feature Access). */
                ...(canAccessTodos
                    ? [{ to: '/todos', label: 'My To-Dos', section: 'My Work', icon: <ListChecks size={17} />, isActive: exact('/todos') } as PanelItem]
                    : []),
                { to: '/assets', label: 'Assets', section: 'My Work', icon: <FileBox size={17} />, isActive: exact('/assets') },
            ];

            /* ── Sales ──────────────────────────────────────────────────────
               The Sales Cockpit and the two surfaces that grew up beside
               it. `hasCockpit` is the server's verdict (the probe above —
               COCKPIT_ENABLED is invisible from here), `hasCcs` and
               `hasScriptAuthoring` are the other two flags' verdicts, and the
               role booleans are AuthContext's mirror of the gates in
               backend/auth.py. So each row is offered only to the person whose
               view it is: an AE sees one or two, a sales manager and sales
               operations a handful, the sales director their numbers, and only
               an admin sees the section entire.

               THE ROLE VIEWS KEEP THEIR OWN ROWS rather than being reached from
               inside My Day. Each is a different person's daily surface — the
               manager's roll-up, sales operations' worklist, the director's
               numbers — not a drill-down of the AE's day: My Day is the AE's
               morning list and links to none of them, so a row here is the
               only door. What
               changed is that they are now a NAMED GROUP instead of a run of
               rows in the middle of everything else, which is also why the row
               for /sales reads "My Day" — under a heading that already says
               Sales, a row labelled "Sales" named the section, not the page it
               opens (MyDay.tsx has always titled itself "My Day").

               Still not a rail area of its own: that spends a permanent rail
               slot on a feature that is flag-off, and a lone AE would get a
               rail-only area anyway. */
            if (hasCockpit) {
                /* `exact`, not `under('/sales')`: the sibling rows below are all
                   under /sales, so `under` would light two items at once. My
                   Day's own "+N more" links are `/sales?block=…`, which `exact`
                   still matches — it compares the pathname only. */
                workItems.push({ to: '/sales', label: 'My Day', section: 'Sales', icon: <Sunrise size={17} />, isActive: exact('/sales') });
            }
            /* Loose Ends sits right under My Day: it is the AE's own morning
               list too (what slipped through yesterday), and My Day's panel
               links here. Its own gate, independent of the cockpit probe — so
               when it is the ONLY Sales row the heading still opens on it (a
               section is just a run of rows naming it). `under`, and My Day
               stays `exact('/sales')`, so only one row lights. */
            if (canAccessLooseEnds) {
                workItems.push({ to: canSeeOwnLooseEnds ? '/sales/loose-ends' : '/sales/loose-ends?view=team', label: 'Loose Ends', section: 'Sales', icon: <Link2Off size={17} />, isActive: under('/sales/loose-ends') });
            }
            /* Today's Calls earns a row, and the comment here used to say the
               opposite. `/calls/start` was a screen you opened in the seconds before
               one call — a detail screen of My Day, correctly reached from it. It is
               now the AE's list of today's client calls, read from their calendar: a
               destination they open in the morning, and the only place the company
               gets bound before a call. Nothing in the app linked to it, so without
               this row the whole screen was reachable only by typing the URL.

               Gated on its OWN flag probe and its OWN audience, not the cockpit's —
               `canAccessCcs` mirrors `auth.user_can_access_ccs`, which is the narrower
               question by intent because this surface creates a recording-adjacent
               artifact of a client conversation. */
            if (hasCcs) {
                workItems.push({ to: '/calls/start', label: "Today's Calls", section: 'Sales', icon: <PhoneCall size={17} />, isActive: callsExceptScripts });
            }
            /* Call Scripts is the AUTHORING surface for the per-product checklists
               the calendar panel renders and the wrap-up measures a call against —
               and it is a different person's screen from every other row here.
               The people who own the company's products open this to add or
               approve a checklist, not to take a call. Nothing else in the app
               links to it, so without a row it is reachable only by typing the URL,
               which is exactly the "only an engineer can update it" failure the
               feature was built to remove.

               Gated on its OWN probe, which asks all three questions at once (see
               `hasScriptAuthoring` above). It sits after Today's Calls because the
               calls are the daily work and the scripts are the setup behind them. */
            if (hasScriptAuthoring) {
                workItems.push({ to: '/calls/scripts', label: 'Call Scripts', section: 'Sales', icon: <ListChecks size={17} />, isActive: under('/calls/scripts') });
            }
            if (hasCockpit && canAccessCockpitSupervisor) {
                workItems.push({ to: '/sales/supervisor', label: 'Nudge Worklist', section: 'Sales', icon: <Bell size={17} />, isActive: under('/sales/supervisor') });
            }
            if (hasCockpit && canAccessCockpitManager) {
                workItems.push({ to: '/sales/manager', label: 'Team Roll-up', section: 'Sales', icon: <Users size={17} />, isActive: under('/sales/manager') });
            }
            if (hasCockpit && canAccessCockpitExec) {
                workItems.push({ to: '/sales/exec', label: 'Exec Roll-up', section: 'Sales', icon: <BarChart3 size={17} />, isActive: under('/sales/exec') });
            }
            /* CRM Sync files under Sales rather than at the top of the panel: it
               is the transcript-to-HubSpot approval queue, so the people who open
               it are the people already living in the rows above. Its own gate is
               unchanged. */
            if (canAccessCrmSync) {
                workItems.push({ to: '/crm-sync', label: 'CRM Sync', section: 'Sales', icon: <RefreshCw size={17} />, isActive: exact('/crm-sync') });
            }
            /* Proposals sits right under CRM Sync: both are "what a call produced",
               one for the CRM record and one for the prospect. The page lists the
               AE's proposals and the captured calls a proposal can be drafted from
               (admins see everyone's, for support). The Draft proposal button on a
               wrap-up is still the primary entry; this row is how you find a
               proposal again, and how a Fathom-recorded call you never opened from
               Today's Calls becomes visible at all. `under` so a proposal's own
               page keeps the row lit. Gate mirrors backend feature key
               `proposal_generator`; the server refuses what the roster excludes. */
            if (canAccessProposals) {
                workItems.push({ to: '/sales/proposals', label: 'Proposals', section: 'Sales', icon: <FileSignature size={17} />, isActive: under('/sales/proposals') });
            }
            /* Customer X-Ray closes the section: it is worked against a named
               company rather than against your own day. It was the seventh tab
               on Productivity, filed with a personal inbox and calendar; it is
               account work, and the people who open it are the ones already
               living in the rows above. The row is named after the feature,
               not the noun ("Customers"), because the noun read as a customer
               list and this is not one: it is where you run an X-Ray.
               `canAccessXray` mirrors backend `auth.user_can_access_xray`,
               the `customer_xray` entry in Admin → Feature Access (everyone
               by default).

               `under`, not `exact`: the X-Ray itself is '/customers/:id/xray', and
               the row must stay lit through that drill-in. */
            if (canAccessXray) {
                workItems.push({ to: '/customers', label: 'Customer X-Ray', section: 'Sales', icon: <Building2 size={17} />, isActive: under('/customers') });
            }

            /* ── HR & Culture ───────────────────────────────────────────────
               The row that is about PEOPLE rather than about the reader's own
               work. A narrow audience already (`hasHandoffs` is the API's
               answer about this specific person), so for most readers the
               heading never appears. */
            if (hasHandoffs) {
                workItems.push({ to: '/handoffs', label: 'Handoffs', section: 'HR & Culture', icon: <UserMinus size={17} />, isActive: exact('/handoffs') });
            }
            built.push({
                key: 'work',
                rail: 'Work',
                title: 'My Work',
                icon: <Briefcase size={19} />,
                /* '/calls' is owned here for BOTH of its screens, which now differ:
                   Today's Calls has a row of its own above, while the wrap-up stays a
                   detail screen reached from My Day (`BackButton fallback="/sales"`).
                   The area must own the wrap-up either way, or the rail goes dark on
                   the screen where an AE spends five focused minutes. Same reason
                   '/customers' is here for the X-Ray. */
                owns: p => startsWithAny(p, ['/productivity', '/todos', '/assets', '/crm-sync', '/customers', '/handoffs', '/sales', '/calls']),
                items: workItems,
            });
        }

        if (canAccessEos) {
            const eosItems: PanelItem[] = [];
            if (isAdmin || isExec) {
                eosItems.push({ to: '/eos/dashboard', label: 'Dashboard', icon: <BarChart3 size={17} />, isActive: exact('/eos/dashboard') });
            }
            eosItems.push(
                { to: '/eos/vto', label: 'VTO', icon: <Target size={17} />, isActive: exact('/eos/vto') },
                { to: '/eos/scorecard', label: 'Scorecard', icon: <BarChart3 size={17} />, isActive: exact('/eos/scorecard') },
                { to: '/eos/rocks', label: 'Rocks', icon: <Mountain size={17} />, isActive: exact('/eos/rocks') },
                { to: '/eos/todos', label: 'To-Dos', icon: <ListTodo size={17} />, isActive: exact('/eos/todos') },
                { to: '/eos/issues', label: 'Issues', icon: <AlertCircle size={17} />, isActive: exact('/eos/issues') },
                { to: '/eos/meetings', label: 'L10', icon: <Calendar size={17} />, isActive: under('/eos/meetings') },
                { to: '/eos/processes', label: 'Processes', icon: <Workflow size={17} />, isActive: exact('/eos/processes') },
            );
            built.push({
                key: 'eos',
                rail: 'EOS',
                title: 'EOS',
                icon: <Compass size={19} />,
                owns: p => startsWithAny(p, ['/eos']),
                items: eosItems,
            });
        }

        if (canAccessFullApp) {
            built.push({
                key: 'ask',
                rail: 'Ask',
                title: 'Ask Cubie',
                icon: <MessageSquare size={19} />,
                owns: p => startsWithAny(p, ['/chat']),
                items: [
                    { to: '/chat', label: 'Chat', icon: <MessageSquare size={17} />, isActive: exact('/chat'), dot: chatUnreadCount > 0 },
                ],
            });
        }

        /* Smart Teams has its own audience (`smart_teams` in Admin → Feature
           Access), separate from the copilot's. THE AREA IS TWO GATES, NOT
           ONE: the first four entries follow canAccessSmartTeams, while Find
           People, Compare and Team Builder additionally require the copilot
           because their routers stack `require_full_app_access`. Do not fold
           them into the outer condition — that ships nav entries that 403. */
        if (canAccessSmartTeams) {
            const teamItems: PanelItem[] = [
                { to: '/smart-teams', label: 'My Work Style', icon: <Users size={17} />, isActive: exact('/smart-teams') },
                { to: '/smart-teams/interview', label: 'Interview', icon: <ClipboardList size={17} />, isActive: under('/smart-teams/interview') },
                { to: '/smart-teams/guide', label: 'My Guide', icon: <FileText size={17} />, isActive: under('/smart-teams/guide') },
                { to: '/smart-teams/skills', label: 'My Skills', icon: <Wrench size={17} />, isActive: under('/smart-teams/skills') },
            ];
            if (canAccessFullApp) {
                teamItems.push(
                    // Find People shipped as a routed page with no nav entry at
                    // all, reachable only from a card on the Smart Teams page.
                    { to: '/smart-teams/find-people', label: 'Find People', icon: <Search size={17} />, isActive: under('/smart-teams/find-people') },
                    { to: '/smart-teams/compare', label: 'Compare', icon: <GitCompare size={17} />, isActive: under('/smart-teams/compare') },
                    { to: '/smart-teams/team-builder', label: 'Team Builder', icon: <UsersRound size={17} />, isActive: under('/smart-teams/team-builder') },
                );
            }
            built.push({
                key: 'teams',
                rail: 'Teams',
                title: 'Smart Teams',
                icon: <Users size={19} />,
                owns: p => startsWithAny(p, ['/smart-teams']),
                items: teamItems,
            });
        }

        /* ── Insights ─────────────────────────────────────────────────────
           The BI dashboards, under the four group headings of
           pages/insights/dashboards.ts. Only dashboards that have shipped get a
           row (`live`); the rest are listed by the server as coming soon and
           the page says so if someone deep-links to one.

           EVERY ROW CARRIES THE CURRENT FILTERS. While the reader is inside
           Insights, the department and date range in the URL ride along on
           each link, so moving from Adoption to Ask Cubie keeps "Sales, last 90
           days" instead of resetting it. Outside Insights the rows are bare and
           the page restores the reader's last-used filters itself. */
        if (canAccessInsights) {
            const insightsQuery = startsWithAny(location.pathname, ['/insights'])
                ? filterQueryFromSearch(location.search)
                : '';
            built.push({
                key: 'insights',
                rail: 'Insights',
                title: 'Insights',
                icon: <BarChart3 size={19} />,
                owns: p => startsWithAny(p, ['/insights']),
                items: navDashboards().flatMap(({ title, dashboards }) =>
                    dashboards.map(d => {
                        const DashIcon = DASHBOARD_ICONS[d.icon];
                        return {
                            to: `${insightsPath(d.slug)}${insightsQuery}`,
                            label: d.navLabel ?? d.title,
                            section: title,
                            icon: <DashIcon size={17} />,
                            isActive: (loc: RouteMatch) => isDashboardPath(loc.pathname, d.slug),
                        } as PanelItem;
                    })
                ),
            });
        }

        /* Settings is a first-class area rather than an avatar-menu entry, and
           its sections are the panel. The page reads ?tab= and renders one
           section, so these links are the whole navigation for it — the page no
           longer carries its own. Gated on the copilot to match the /settings
           route guard (FullAppRoute); an EOS-only user still has no Settings,
           which is a real gap but a separate change from this one. */
        if (canAccessFullApp) {
            const settingsItems: PanelItem[] = [
                { to: '/settings?tab=general', label: 'General', icon: <SlidersHorizontal size={17} />, isActive: settingsTab('general'), replace: true },
                { to: '/settings?tab=ai', label: 'AI Personalization', icon: <Sparkles size={17} />, isActive: settingsTab('ai'), replace: true },
                { to: '/settings?tab=email', label: 'Email Preferences', icon: <Mail size={17} />, isActive: settingsTab('email'), replace: true },
                { to: '/settings?tab=slack', label: 'Slack', icon: <MessageSquare size={17} />, isActive: settingsTab('slack'), replace: true },
            ];
            /* Scheduled tasks. The gate is false until /api/users/me lands, so
               also show the entry
               whenever the reader is already ON that section — otherwise a
               link from Chat renders a page missing from its own menu. */
            const onScheduledTasks = settingsTab('scheduled-tasks')({
                pathname: location.pathname,
                search: location.search,
            });
            if (canAccessScheduledTasks || onScheduledTasks) {
                settingsItems.push({ to: '/settings?tab=scheduled-tasks', label: 'Scheduled tasks', icon: <CalendarClock size={17} />, isActive: settingsTab('scheduled-tasks'), replace: true, dot: settingsUnreadCount > 0 });
            }
            /* What's New's settings are not a row here any more: they apply
               to everyone, so they are an Administration section (Admin →
               What's New, pages/adminSections.ts), and
               Settings sends an admin's old ?tab=whats-new link there. */
            built.push({
                key: 'settings',
                rail: 'Settings',
                title: 'Settings',
                icon: <SettingsIcon size={19} />,
                owns: p => startsWithAny(p, ['/settings']),
                items: settingsItems,
            });
        }

        /* UNGATED ON PURPOSE. Help used to render inside the copilot-gated
           block, which meant every EOS-only user had no Help link anywhere in
           the app. It is the fallback destination, so it must
           be reachable by everyone who can log in.

           Help was one 2,700-line page with a table of contents; every topic is
           now a page of its own, and the panel is the table of contents. The
           rows come from pages/help/helpTopics.ts — the same list the Help
           home and the topic route read — under headings that follow this
           rail's own areas, so a feature's help sits under the name of the
           place you found the feature. "Help home" leads because the rail
           lands a click on an area's first row. A gated topic is left out
           for a reader the feature is closed to, exactly as its page is. */
        built.push({
            key: 'guide',
            rail: 'Help',
            title: 'Help',
            icon: <HelpCircle size={19} />,
            owns: p => startsWithAny(p, ['/help']),
            items: [
                { to: '/help', label: 'Help home', section: helpGroupTitle('start'), icon: <HelpCircle size={17} />, isActive: exact('/help') },
                ...groupedHelpTopics(helpAccess).flatMap(({ group, topics }) =>
                    topics.map(topic => {
                        const TopicIcon = TOPIC_PAGES[topic.id]?.Icon ?? HelpCircle;
                        return {
                            to: helpPath(topic.id),
                            label: topic.label,
                            section: group.title,
                            icon: <TopicIcon size={17} />,
                            isActive: exact(helpPath(topic.id)),
                        } as PanelItem;
                    })
                ),
            ],
        });

        if (isAdmin) {
            built.push({
                key: 'admin',
                rail: 'Admin',
                title: 'Administration',
                icon: <Shield size={19} />,
                /* `owns` claims /departments too, so the rail stays lit during
                   the redirect hop to ?tab=departments rather than flashing to
                   another area. */
                owns: p => startsWithAny(p, ['/admin', '/departments']),
                /* THE WHOLE ADMINISTRATION TREE, from the one list that defines
                   it (pages/adminSections). It used to render inside Admin.tsx
                   as a 224px column while this panel held a single row reading
                   "Admin" — 204px spent restating the shield icon beside it, and
                   two near-identical vertical lists 32px apart. The sections are
                   the area's destinations, so they belong to the area's panel;
                   the page keeps only its content. Deep links such as
                   /admin?tab=access are untouched by the move. */
                items: ADMIN_NAV_GROUPS.flatMap(group =>
                    group.items.map(item => ({
                        section: group.title,
                        to: `/admin?tab=${item.key}`,
                        label: item.label,
                        icon: <item.Icon size={17} />,
                        isActive: adminTab(item.key),
                        replace: true,
                    }))
                ),
            });
        }

        return built.filter(a => a.items.length > 0);
    }, [
        canAccessFullApp,
        canAccessEos,
        canAccessCrmSync,
        canAccessCockpitManager,
        canAccessCockpitSupervisor,
        canAccessCockpitExec,
        canAccessXray,
        canAccessSmartTeams,
        canAccessScheduledTasks,
        canAccessTodos,
        hasHandoffs,
        hasCockpit,
        hasCcs,
        hasScriptAuthoring,
        isAdmin,
        isExec,
        chatUnreadCount,
        settingsUnreadCount,
        canAccessProposals,
        canAccessLooseEnds,
        canSeeOwnLooseEnds,
        canAccessInsights,
        helpAccess,
        location.pathname,
        location.search,
    ]);

    /* WHERE A VOICE COMMAND MAY GO: exactly the rows this person's panel
       shows, plus each area by its own name ("open my work" lands where a
       click on the Work icon lands — its first row). Built from `areas` rather
       than kept as a second list, so a page gains a row and can be opened by
       voice in the same edit, and a page the person cannot reach is never
       offered. The server picks only from this list; every page is still
       gated where it loads. */
    const voiceDestinations = useMemo<VoiceDestination[]>(() => {
        const out: VoiceDestination[] = [];
        for (const area of areas) {
            out.push({ path: area.items[0].to, label: area.title, group: 'Area' });
            for (const item of area.items) {
                out.push({
                    path: item.to,
                    label: item.label,
                    group: item.section ? `${area.title} › ${item.section}` : area.title,
                });
            }
        }
        return out;
    }, [areas]);
    const canUseVoiceCommands = canAccessFullApp && hasFeature('voice_commands');

    /* Which area the current route belongs to. Falls back to the first area
       rather than nothing: a route outside every area (or a role whose areas
       changed under it) should still show a populated panel. Help is ungated,
       so `areas` is never empty. */
    const activeArea =
        areas.find(a => a.owns(location.pathname)) ?? areas[0];

    /* The flat item list, folded into its headings. The list stays the single
       source of truth — a section is just a run of items that name it — so a
       section every one of whose rows was gated away simply never appears, and
       nothing has to be told it existed. Items with no `section` (most areas)
       fold into one unnamed group and render exactly as they always have. */
    const panelGroups = useMemo(() => {
        const out: { section: string | null; items: PanelItem[] }[] = [];
        for (const item of activeArea?.items ?? []) {
            const section = item.section ?? null;
            const last = out[out.length - 1];
            if (last && last.section === section) last.items.push(item);
            else out.push({ section, items: [item] });
        }
        return out;
    }, [activeArea]);

    /* Activity-bar interaction, the one people already know from VS Code:
       a different icon switches the panel and navigates; the active icon
       collapses the panel; any icon re-opens it. Note the asymmetry — re-opening
       on the area you are ALREADY in never navigates, so you can always get the
       labels back without leaving the page; re-opening on a different area does
       navigate, because that is a request to go there. */
    const handleAreaClick = (area: Area) => {
        const isActiveArea = area.key === activeArea?.key;
        /* A single-destination area has no panel to toggle, so its icon behaves
           as a plain link — a click must never be swallowed by a toggle. */
        if (area.items.length === 1) {
            if (!isActiveArea) navigate(area.items[0].to);
            return;
        }
        if (!isPanelOpen) {
            setIsPanelOpen(true);
            if (!isActiveArea) navigate(area.items[0].to);
            return;
        }
        if (isActiveArea) {
            collapsePanel();
            return;
        }
        navigate(area.items[0].to);
    };

    /* An area with ONE destination has nothing to put in a panel but its own
       name, which the rail icon beside it already prints. Rather than spend 204px
       restating it, those areas run rail-only and hand the width to the page —
       Chat gains the most, since it carries its own 256px thread list on top of
       the app chrome. `isPanelOpen` stays the user's preference; this is the
       separate question of whether there is anything worth showing. */
    const panelHasContent = (activeArea?.items.length ?? 0) > 1;
    const showPanel = isPanelOpen && panelHasContent;

    const asideWidth = isNavHidden ? 0 : showPanel ? RAIL_WIDTH + PANEL_WIDTH : RAIL_WIDTH;

    return (
        <div className="flex h-screen bg-gray-50 font-sans">
            {/* `invisible` (visibility: hidden), not just width 0 — a clipped
                zero-width rail still holds its links in the tab order, so a
                keyboard user could tab into the hidden nav and leave the
                meeting. visibility:hidden takes them out of it. */}
            {/* Width is a plain style with a CSS transition, NOT a JS-animated
                value. framer-motion interpolates into the inline style, so an
                interrupted animation (backgrounded tab, throttled frames) leaves
                the rail resting at an in-between width — which, with the panel
                collapsed, renders as a wide empty band instead of a 68px rail.
                A declared width can only ever rest on the right number. */}
            <aside
                style={{ width: asideWidth }}
                className={`bg-rail text-white flex flex-col shadow-lg z-10 relative overflow-hidden transition-[width] duration-200 ease-out motion-reduce:transition-none ${
                    isNavHidden ? 'invisible' : ''
                }`}
                aria-hidden={isNavHidden}
            >
                <div className="flex flex-1 min-h-0">
                    {/* ── Tier 1: the area rail ── */}
                    <nav
                        aria-label="Areas"
                        className="flex-shrink-0 bg-black/30 flex flex-col overflow-y-auto scrollbar-hide pb-2"
                        style={{ width: RAIL_WIDTH }}
                    >
                        {/* The cube alone while the rail is the whole nav. When the
                            panel is open it carries the cube with the wordmark,
                            and one logo is enough. */}
                        {!showPanel ? (
                            <div className="mt-3 mb-2 h-9 flex items-center justify-center flex-shrink-0" title={PRODUCT_NAME}>
                                <RailCubeMark className="h-8 w-8" />
                                <span className="sr-only">{PRODUCT_NAME}</span>
                            </div>
                        ) : (
                            <div className="mt-3 mb-2 h-9 flex-shrink-0" aria-hidden="true" />
                        )}
                        {areas.map(area => {
                            const isActiveArea = area.key === activeArea?.key;
                            return (
                                <button
                                    key={area.key}
                                    type="button"
                                    onClick={() => handleAreaClick(area)}
                                    title={area.title}
                                    ref={isActiveArea ? activeAreaButtonRef : undefined}
                                    aria-current={isActiveArea ? 'true' : undefined}
                                    className={`flex flex-col items-center gap-0.5 py-2.5 border-l-[3px] transition-colors ${
                                        isActiveArea
                                            ? 'bg-rail-active text-white border-white'
                                            : 'text-gray-300 border-transparent hover:bg-white/10 hover:text-white'
                                    }`}
                                >
                                    <span className="relative">
                                        {area.icon}
                                        {area.items.some(i => i.dot) && (
                                            <UnreadDot className={`absolute -top-0.5 -right-1.5 ring-2 ${isActiveArea ? 'ring-rail-active' : 'ring-rail'}`} />
                                        )}
                                    </span>
                                    <span className="text-[9px] font-semibold tracking-wide">{area.rail}</span>
                                </button>
                            );
                        })}
                    </nav>

                    {/* ── Tier 2: the active area's destinations ── */}
                    {showPanel && activeArea && (
                        <div
                            className="flex-shrink-0 flex flex-col min-h-0"
                            style={{ width: PANEL_WIDTH }}
                        >
                            <div className="mx-4 mt-3 mb-1 h-9 flex items-center gap-2.5 flex-shrink-0">
                                <RailCubeMark className="h-8 w-8" />
                                <span className="font-display text-xl font-semibold leading-none tracking-tight text-white">
                                    {PRODUCT_NAME}
                                </span>
                            </div>
                            <div className="flex items-center justify-between gap-2 px-4 pt-2 pb-2 flex-shrink-0">
                                <h2 className="text-[15px] font-semibold tracking-tight truncate">
                                    {activeArea.title}
                                </h2>
                                <button
                                    type="button"
                                    onClick={collapsePanel}
                                    title="Collapse panel"
                                    aria-label="Collapse panel"
                                    /* p-1 takes the hit area to 24x24, the WCAG 2.2
                                       SC 2.5.8 minimum; the glyph stays 16px. */
                                    className="text-gray-300 hover:text-white transition-colors flex-shrink-0 p-1 -m-1 rounded"
                                >
                                    <PanelLeftClose size={16} />
                                </button>
                            </div>
                            <nav
                                aria-label={activeArea.title}
                                className="flex-1 overflow-y-auto scrollbar-hide pb-2"
                                data-tour={activeArea.key === 'settings' ? 'settings-sections' : undefined}
                            >
                                {panelGroups.map((group, gi) => {
                                    // aria-current belongs on the <a> React
                                    // Router renders, not on the styling div
                                    // inside it — on a role=generic wrapper the
                                    // state never reaches the link's accessible
                                    // object, so every row announces identically.
                                    /* `nested` is every row that sits under a
                                       section heading. Those rows are that
                                       heading's children and have to look it —
                                       indented past its glyph and a step down in
                                       type size. Left flat, a heading and its
                                       eight destinations read as nine equal
                                       siblings, which is what made a twelve-row
                                       My Work panel a wall of identical rows.
                                       Areas with no headings are unaffected: an
                                       ungrouped row has no parent to defer to,
                                       so it keeps the full-size treatment. */
                                    const renderItem = (item: PanelItem, nested = false) => {
                                        const active = item.isActive({
                                            pathname: location.pathname,
                                            search: location.search,
                                        });
                                        return (
                                            <Link
                                                key={item.to}
                                                to={item.to}
                                                replace={item.replace}
                                                aria-current={active ? 'page' : undefined}
                                            >
                                                <div
                                                    className={`flex items-center gap-2.5 border-l-4 transition-[background-color,color,transform] duration-150 hover:translate-x-[3px] motion-reduce:hover:translate-x-0 ${
                                                        /* A nested row starts at 32px, twenty past the
                                                           heading glyph's 12, and the tighter padding
                                                           still clears the WCAG 2.2 SC 2.5.8 24px
                                                           target — the 17px icon sets the row height. */
                                                        nested ? 'pl-7 pr-4 py-1.5' : 'px-4 py-2'
                                                    } ${
                                                        active
                                                            ? 'bg-rail-active text-white font-medium border-white'
                                                            : 'text-gray-300 border-transparent hover:bg-white/5 hover:text-white'
                                                    }`}
                                                >
                                                    <span className="opacity-80 flex-shrink-0">{item.icon}</span>
                                                    <span className={`truncate ${nested ? 'text-[13px] leading-5' : 'text-sm'}`}>{item.label}</span>
                                                    {item.dot && <UnreadDot className="ml-auto" />}
                                                </div>
                                            </Link>
                                        );
                                    };

                                    if (!group.section) {
                                        return (
                                            <React.Fragment key={`ungrouped-${gi}`}>
                                                {group.items.map(it => renderItem(it))}
                                            </React.Fragment>
                                        );
                                    }

                                    const stateKey = `${activeArea.key}:${group.section}`;
                                    /* A section holding the OPEN PAGE never folds
                                       away, whatever the reader last chose. The
                                       row that tells them where they are has to
                                       stay on screen, and a nav that can hide the
                                       current page is a dead end you get out of
                                       only by remembering which heading you put it
                                       under. The preference is kept, not cleared —
                                       it takes effect again the moment they
                                       navigate out of the section. */
                                    const holdsActive = group.items.some(it =>
                                        it.isActive({
                                            pathname: location.pathname,
                                            search: location.search,
                                        })
                                    );
                                    const collapsed = !!collapsedSections[stateKey] && !holdsActive;
                                    const listId = `nav-section-${activeArea.key}-${gi}`;
                                    /* The Insights "Platform" heading (once
                                       "Systems") carries the anchor its
                                       insights.new.platform-group tour points at. */
                                    const isPlatformGroup =
                                        activeArea.key === 'insights' && group.section === dashboardGroupTitle('platform');
                                    return (
                                        <div key={stateKey} data-tour={isPlatformGroup ? 'insights-platform-group' : undefined}>
                                            <button
                                                type="button"
                                                onClick={() => toggleSection(stateKey)}
                                                aria-expanded={!collapsed}
                                                aria-controls={listId}
                                                /* A band, not one more row: a
                                                   surface LIGHTER than the panel
                                                   and a small-caps eyebrow, which
                                                   puts it apart from the rows —
                                                   hover is a fainter wash and
                                                   the open page is violet.
                                                   Headings lift, the page you
                                                   are on is lit, and nothing in
                                                   the panel shares a colour.
                                                   py-2 around a 16px line keeps the
                                                   toggle past the WCAG 2.2 SC 2.5.8
                                                   24px minimum, and mt-2 is the gap
                                                   that separates this section from
                                                   the last. */
                                                className="w-full flex items-center gap-2 px-3 py-2 mt-2 bg-white/10 text-[12px] leading-4 font-semibold uppercase tracking-[0.08em] text-white/80 hover:bg-white/20 hover:text-white transition-colors"
                                            >
                                                {SECTION_ICONS[group.section] && (
                                                    <span className="flex-shrink-0">{SECTION_ICONS[group.section]}</span>
                                                )}
                                                <span className="truncate">{group.section}</span>
                                                <ChevronDown
                                                    size={14}
                                                    aria-hidden="true"
                                                    className={`ml-auto flex-shrink-0 transition-transform duration-150 motion-reduce:transition-none ${
                                                        collapsed ? '-rotate-90' : ''
                                                    }`}
                                                />
                                            </button>
                                            {!collapsed && (
                                                <div id={listId}>
                                                    {group.items.map(it => renderItem(it, true))}
                                                </div>
                                            )}
                                        </div>
                                    );
                                })}
                            </nav>
                        </div>
                    )}
                </div>

                {/* ── Notifications, just above the account footer ──
                    Not an area: it opens a panel over the page rather than
                    navigating, and it has to be in reach from every area, so it
                    sits with the account controls instead of in the rail.
                    Outside the footer's ref on purpose: a click on it is a
                    click outside the account menu, so that menu closes.
                    Full app only: the notifications API sits behind
                    `require_full_app_access`, and everything it announces (a
                    meeting prep, a scheduled task) is full-app too. */}
                {/* Voice commands sit with Notifications for the same reason:
                    in reach from every area. The panel they open is portalled
                    to <body>, so the shortcut still works while a live L10
                    hides this whole sidebar. */}
                {canUseVoiceCommands && (
                    <div className="bg-black/20 border-t border-white/10 flex-shrink-0">
                        <VoiceCommandBar expanded={showPanel} destinations={voiceDestinations} />
                    </div>
                )}
                {canAccessFullApp && (
                    <div className="bg-black/20 border-t border-white/10 flex-shrink-0">
                        <NotificationBell expanded={showPanel} />
                    </div>
                )}
                {/* What's New sits here for the same reason — it opens the
                    drawer over the page, from every area — but NOT behind
                    `canAccessFullApp`: it is company-wide, so EOS-only people
                    get it too. Shown once the feed has something for this
                    person, or they review drafts. */}
                {showWhatsNewButton && (
                    <div className="bg-black/20 border-t border-white/10 flex-shrink-0">
                        <WhatsNewButton expanded={showPanel} />
                    </div>
                )}

                {/* ── Account footer, spanning whichever total width is current ── */}
                <div className="bg-black/20 border-t border-white/10 relative flex-shrink-0" ref={userMenuRef}>
                    <button
                        data-tour="account-menu"
                        onClick={() => (showPanel ? setIsUserMenuOpen(o => !o) : setIsPanelOpen(true))}
                        className="w-full flex items-center gap-3 p-3 hover:bg-white/5 transition-colors text-left"
                        title={showPanel ? 'Account menu' : panelHasContent ? 'Expand panel' : 'Account menu'}
                        /* Explicit name because the button's content is the user's
                           NAME while its action is "expand the panel" when the rail
                           is collapsed — the visible text would be a lie. No
                           aria-haspopup="menu": this is a disclosure, not a menu
                           widget with arrow-key semantics. */
                        aria-label={showPanel ? 'Account menu' : panelHasContent ? 'Expand panel' : 'Account menu'}
                        aria-expanded={showPanel ? isUserMenuOpen : undefined}
                    >
                        {user?.picture ? (
                            <img
                                src={user.picture}
                                alt={user.name || 'User'}
                                referrerPolicy="no-referrer"
                                onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }}
                                className="h-10 w-10 rounded-full flex-shrink-0 object-cover shadow-inner bg-white/20"
                            />
                        ) : (
                            <div className="h-10 w-10 bg-white/20 rounded-full flex-shrink-0 flex items-center justify-center font-bold text-white shadow-inner">
                                {user?.name ? user.name.slice(0, 2).toUpperCase() : 'U'}
                            </div>
                        )}
                        {showPanel && (
                            <div className="flex-1 min-w-0">
                                <p className="font-semibold text-sm truncate text-white">
                                    {user?.name || (isAdmin ? 'Admin' : 'Client User')}
                                    <span className="font-normal text-xs text-gray-300 truncate block">
                                        {user?.email || 'user@example.com'}
                                    </span>
                                </p>
                            </div>
                        )}
                    </button>

                    {isUserMenuOpen && showPanel && !isNavHidden && (
                        <div className="absolute bottom-full left-3 right-3 mb-2 bg-white text-gray-900 rounded-lg shadow-xl border border-gray-200 overflow-hidden z-50">
                            <div className="px-4 py-3 border-b border-gray-100">
                                <p className="text-xs text-gray-500 truncate">{user?.email || ''}</p>
                            </div>
                            {/* Settings has its own rail area now; this stays as
                                the familiar door for anyone who looks here
                                first. Same copilot gate as the area and the route. */}
                            {canAccessFullApp && (
                                <Link
                                    to="/settings"
                                    onClick={() => setIsUserMenuOpen(false)}
                                    className="flex items-center gap-2 px-4 py-2.5 text-sm hover:bg-gray-100 transition-colors"
                                >
                                    <SettingsIcon size={16} className="text-gray-500" />
                                    Settings
                                </Link>
                            )}
                            {/* Page tours. The replay row shows only on a page
                                that has a tour. The on/off row is for people who
                                cannot open Settings (EOS-only), where everyone
                                else finds the same switch under General. */}
                            {tours.available && tours.pageHasTour && (
                                <button
                                    type="button"
                                    onClick={() => {
                                        setIsUserMenuOpen(false);
                                        tours.replay();
                                    }}
                                    className="w-full flex items-center gap-2 px-4 py-2.5 text-sm hover:bg-gray-100 transition-colors text-left"
                                >
                                    <Compass size={16} className="text-gray-500" />
                                    Replay this page&apos;s tour
                                </button>
                            )}
                            {tours.available && tours.loaded && !canAccessFullApp && (
                                <button
                                    type="button"
                                    onClick={() => {
                                        const on = !tours.enabled;
                                        tours.setEnabled(on).then(
                                            () => toast.info(on ? 'Page tours are on.' : 'Page tours are off. Replay one any time from this menu.'),
                                            () => toast.error('That didn’t save. Try again in a moment.'),
                                        );
                                    }}
                                    className="w-full flex items-center gap-2 px-4 py-2.5 text-sm hover:bg-gray-100 transition-colors text-left"
                                >
                                    {tours.enabled ? <EyeOff size={16} className="text-gray-500" /> : <Eye size={16} className="text-gray-500" />}
                                    {tours.enabled ? 'Turn off page tours' : 'Turn on page tours'}
                                </button>
                            )}
                            <button
                                onClick={() => {
                                    setIsUserMenuOpen(false);
                                    handleLogout();
                                }}
                                className="w-full flex items-center gap-2 px-4 py-2.5 text-sm hover:bg-gray-100 transition-colors border-t border-gray-100 text-left"
                            >
                                <LogOut size={16} className="text-gray-500" />
                                Log out
                            </button>
                        </div>
                    )}

                    {isAdmin && showPanel && (
                        <div className="px-3 pb-3">
                            <select
                                value={impersonateValue}
                                onChange={handleImpersonateChange}
                                aria-label="Impersonate department"
                                className="w-full text-xs border-none rounded bg-white/10 text-white p-2 focus:ring-1 focus:ring-white/50 outline-none"
                            >
                                <option value="" className="text-black">No Impersonation</option>
                                {impersonationTargets.length > 0 && (
                                    <optgroup label="People (read-only, no email or transcripts)" className="text-black">
                                        {impersonationTargets.map(t => (
                                            <option key={t.email} value={`user:${t.email}`} className="text-black">
                                                {t.name}
                                            </option>
                                        ))}
                                    </optgroup>
                                )}
                                <optgroup label="Departments" className="text-black">
                                    {departments.map(d => (
                                        <option key={d} value={d} className="text-black">{d}</option>
                                    ))}
                                </optgroup>
                            </select>
                        </div>
                    )}

                    {showPanel && (
                        <div className="px-4 pb-3 flex items-center justify-between opacity-40">
                            <p className="text-[9px] text-white font-medium uppercase tracking-widest">
                                {PRODUCT_NAME} by {VENDOR_NAME}
                            </p>
                            <p className="text-[9px] text-white font-bold">v1.0.3</p>
                        </div>
                    )}
                </div>
            </aside>

            {/* Main Content */}
            <main className="flex-1 min-w-0 overflow-y-auto bg-gray-50 relative scroll-smooth">
                {user?.support && (
                    <div
                        data-tour="platform-support-banner"
                        role="status"
                        className="sticky top-0 z-[90] flex flex-wrap items-center justify-between gap-2 border-b border-indigo-300 bg-indigo-100 px-4 py-2 text-sm text-indigo-950"
                    >
                        <span className="font-semibold">Acting in {user.support.workspace_name} as RubikSoft support</span>
                        <button
                            type="button"
                            onClick={handleExitSupport}
                            disabled={isExitingSupport}
                            className="rounded-md bg-indigo-900 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-800 disabled:opacity-50"
                        >
                            {isExitingSupport ? 'Exiting…' : 'Exit'}
                        </button>
                    </div>
                )}
                {user?.impersonation && (
                    <div
                        role="status"
                        className="sticky top-0 z-[90] flex flex-wrap items-center justify-between gap-2 bg-amber-100 px-4 py-2 text-sm text-amber-900 border-b border-amber-200"
                    >
                        <span>
                            <span className="font-semibold">Viewing Cubie as {user.impersonation.target_name}</span>
                            {' '}— read-only. Email, calendar, Drive, call transcripts and Ask Cubie are off.
                        </span>
                        <button
                            type="button"
                            onClick={stopImpersonating}
                            className="rounded-md bg-amber-900 px-3 py-1 text-xs font-medium text-white hover:bg-amber-800"
                        >
                            Stop impersonating
                        </button>
                    </div>
                )}
                {/* Subtle Ingestion Notifications (Top-Right) */}
                <div className="fixed top-6 right-6 z-[100] flex flex-col gap-3 items-end pointer-events-none">
                    {/* Active Ingestion Indicator */}
                    {isIngestingTranscripts && (
                        <motion.div
                            initial={{ opacity: 0, y: -20, scale: 0.9 }}
                            animate={{ opacity: 1, y: 0, scale: 1 }}
                            exit={{ opacity: 0, scale: 0.9 }}
                            className="bg-white border border-blue-100 shadow-xl rounded-full px-4 py-2 flex items-center gap-3 pointer-events-auto"
                        >
                            <Loader2 className="animate-spin text-blue-600" size={16} />
                            <span className="text-xs font-semibold text-gray-700">SYNCING TRANSCRIPTS (V2)...</span>
                        </motion.div>
                    )}

                    {/* Completion Toast */}
                    {ingestionComplete && (
                        <motion.div
                            initial={{ opacity: 0, x: 50 }}
                            animate={{ opacity: 1, x: 0 }}
                            className="bg-white border border-green-100 shadow-xl rounded-xl p-4 max-w-xs pointer-events-auto flex items-start gap-3"
                        >
                            <div className="bg-green-100 rounded-full p-1 flex-shrink-0">
                                <CheckCircle2 className="text-green-600" size={16} />
                            </div>
                            <div className="flex-1 min-w-0">
                                <p className="text-xs font-bold text-gray-900 mb-0.5">Ingestion Complete</p>
                                <p className="text-[11px] text-gray-500 leading-tight">
                                    Your Google Meet transcripts are now securely locked and searchable.
                                </p>
                            </div>
                            <button onClick={dismissIngestionBanner} className="text-gray-400 hover:text-gray-600 transition-colors">
                                <X size={14} />
                            </button>
                        </motion.div>
                    )}

                    {/* "Your meeting prep is ready" (or couldn't be built) —
                        cards with a link, wherever the person is. */}
                    <NotificationToaster />
                </div>

                <Outlet />
            </main>
            {/* App-wide toast + confirm hosts (mounted once; render via portal). */}
            <ToastHost />
            <ConfirmHost />
            {/* The What's New drawer, once for the whole shell (portalled). */}
            <WhatsNewDrawer />
        </div>
    );
};
