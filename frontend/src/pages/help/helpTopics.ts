// The Help topic registry: which pages Help has, what each is called, which
// panel heading it sits under, and who may read it.
//
// ONE LIST DRIVES ALL OF HELP. The Help panel in the left rail (Layout.tsx),
// the topic directory on the Help home page, the /help/:topicId route and the
// previous/next links at the foot of every topic page all read this array, so a
// topic added here shows up everywhere at once and a topic missing from it
// shows up nowhere. That is the failure it replaces: the old single page kept a
// hand-written table of contents beside its sections, and two of them shipped
// with no row in it.
//
// THE BACKEND MIRROR. Chat answers "how do I…" questions from
// rubik_core/help/content.py and links the reader to `/help/<id>`. `id`,
// `title` and `requires` here must match that file's HelpSection for the same
// id; backend/tests/test_help_chat_link.py parses this file and fails on any
// drift. Keep the object literals in the shape below (one `id:` per topic,
// single-quoted strings) so that parse keeps working.
//
// A leaf module with no imports, so Node's built-in test runner can load it
// without a bundler (see helpTopics.test.ts).

/**
 * The gate a topic sits behind, by the backend's name for it
 * (`HelpSection.requires`). The Help page hides a gated topic from a reader the
 * feature is closed to, and Chat refuses to cite it to them.
 */
export type HelpGate =
    | 'xray'
    | 'crm_sync'
    | 'scheduled_tasks'
    | 'todos'
    | 'proposals'
    | 'voice_commands'
    | 'insights'
    | 'whats_new'
    | 'chrome_extension';

/** The reader's verdict per gate. A missing verdict counts as closed. */
export type HelpAccess = Partial<Record<HelpGate, boolean>>;

export interface HelpGroup {
    key: string;
    /** The panel heading, and the heading on the Help home directory. */
    title: string;
}

export interface HelpTopic {
    /** URL segment (`/help/<id>`) and the backend HelpSection id. */
    id: string;
    /** The page heading. Must equal the backend HelpSection title. */
    title: string;
    /** The panel row. Short, because the panel is 204px wide. */
    label: string;
    /** Key of the HelpGroup the topic is filed under. */
    group: string;
    /** One sentence for the Help home directory: what this page answers. */
    summary: string;
    requires?: HelpGate;
}

// Panel headings, in panel order. They follow the app's own areas, so the help
// for a feature sits under the name of the place you found the feature.
export const HELP_GROUPS: readonly HelpGroup[] = [
    { key: 'start', title: 'Start here' },
    { key: 'ask', title: 'Ask Cubie' },
    { key: 'work', title: 'My Work' },
    { key: 'sales', title: 'Sales' },
    { key: 'eos', title: 'EOS & Teams' },
    { key: 'account', title: 'Account' },
];

// Panel order. Topics of one group MUST be contiguous: the panel opens a heading
// whenever the group changes, so a topic filed out of order would reopen its
// heading further down (helpTopics.test.ts pins this).
export const HELP_TOPICS: readonly HelpTopic[] = [
    {
        id: 'getting-started',
        title: 'Getting started',
        label: 'Getting started',
        group: 'start',
        summary: 'What Cubie is, which accounts it uses, and how to get around the app.',
    },
    {
        id: 'daily-briefing',
        title: 'Daily Briefing (your home page)',
        label: 'Daily Briefing',
        group: 'start',
        summary: 'Your home page: the AI Priorities Cubie ranks for you each morning, and what each card can do.',
    },
    {
        id: 'tips',
        title: 'Tips & feedback',
        label: 'Tips & feedback',
        group: 'start',
        summary: 'Quick habits for better answers, how your feedback trains Cubie, and how long you stay signed in.',
    },
    {
        id: 'voice-commands',
        title: 'Voice commands',
        label: 'Voice commands',
        group: 'start',
        summary: 'Open any page, ask a question or change EOS by speaking, with Option+Command (Ctrl+Alt on a PC).',
        requires: 'voice_commands',
    },
    {
        id: 'whats-new',
        title: 'What’s new',
        label: 'What’s new',
        group: 'start',
        summary: 'What has changed in Cubie lately, and how to keep up with it.',
        requires: 'whats_new',
    },
    {
        id: 'chat',
        title: 'Chat',
        label: 'Chat',
        group: 'ask',
        summary: 'What you can ask Cubie, where it looks for answers, and what it can do for you.',
    },
    {
        id: 'crm',
        title: 'Ask Cubie about CRM',
        label: 'Ask about CRM',
        group: 'ask',
        summary: 'Pipeline and deal questions Cubie answers from live HubSpot, and the ones it can’t yet.',
    },
    {
        id: 'scheduled-tasks',
        title: 'Scheduled tasks',
        label: 'Scheduled tasks',
        group: 'ask',
        summary: 'Have Cubie run a request for you on a repeating schedule, and manage what you have set up.',
        requires: 'scheduled_tasks',
    },
    {
        id: 'productivity',
        title: 'Productivity',
        label: 'Productivity',
        group: 'work',
        summary: 'Full-page views of your email, calendar, Slack and Drive files, and how to add a file to Cubie’s knowledge base.',
    },
    {
        id: 'my-todos',
        title: 'My To-Dos',
        label: 'My To-Dos',
        group: 'work',
        summary: 'Your personal list of what you said you would do, for whom, and by when.',
        requires: 'todos',
    },
    {
        id: 'cubie-for-chrome',
        title: 'Cubie for Chrome',
        label: 'Cubie for Chrome',
        group: 'work',
        summary: 'Your promises past due and today’s priorities on every new tab in Chrome.',
        requires: 'chrome_extension',
    },
    {
        id: 'assets',
        title: 'Assets & uploading',
        label: 'Assets & uploading',
        group: 'work',
        summary: 'Add documents to the knowledge base, then find, share and update them as new editions arrive.',
    },
    {
        id: 'meeting-prep',
        title: 'Meeting prep',
        label: 'Meeting prep',
        group: 'work',
        summary: 'Get a briefing before a meeting, and edit or rewrite it before you walk in.',
    },
    {
        id: 'handoffs',
        title: 'Handoffs (departed teammates)',
        label: 'Handoffs',
        group: 'work',
        summary: 'Picking up the open work of a teammate who has left your company.',
    },
    {
        id: 'insights',
        title: 'Insights dashboards',
        label: 'Insights',
        group: 'work',
        summary: 'Dashboards of how Cubie is used and what it produces, by department, with every rate shown out of its total.',
        requires: 'insights',
    },
    {
        id: 'sales-cockpit',
        title: 'Sales cockpit (My Day)',
        label: 'Sales cockpit',
        group: 'sales',
        summary: 'Off until your company switches it on: My Day and the team views, your sales morning in the order you work it.',
    },
    {
        id: 'sales-rhythm',
        title: 'Sales daily rhythm',
        label: 'Sales daily rhythm',
        group: 'sales',
        summary: 'The morning, mid-day and end-of-day sales checklist, answered from your own deals in one click per step.',
    },
    {
        id: 'loose-ends',
        title: 'Loose ends',
        label: 'Loose ends',
        group: 'sales',
        summary: 'Client emails not in HubSpot, replies you owe and late promises, checked each morning and cleared by the record.',
    },
    {
        id: 'sales-calls',
        title: 'Sales calls (call capture)',
        label: 'Sales calls',
        group: 'sales',
        summary: 'Off until your company switches it on: start client calls from your calendar and approve the HubSpot updates.',
    },
    {
        id: 'crm-sync',
        title: 'CRM Sync',
        label: 'CRM Sync',
        group: 'sales',
        summary: 'Review and approve the HubSpot updates Cubie suggests from your meetings.',
        requires: 'crm_sync',
    },
    {
        id: 'proposals',
        title: 'Draft a proposal from a call',
        label: 'Proposals',
        group: 'sales',
        summary: 'Turn a captured client call into a draft proposal you can edit and send.',
        requires: 'proposals',
    },
    {
        id: 'customer-xray',
        title: 'Customer X-Ray',
        label: 'Customer X-Ray',
        group: 'sales',
        summary: 'A researched, one-page picture of a client before you talk to them.',
        requires: 'xray',
    },
    {
        id: 'eos',
        title: 'EOS',
        label: 'EOS',
        group: 'eos',
        summary: 'Scorecard, Rocks, To-Dos, Issues and the L10 meeting, and working across more than one team.',
    },
    {
        id: 'smart-teams',
        title: 'Smart Teams',
        label: 'Smart Teams',
        group: 'eos',
        summary: 'Your work-style interview and guide, and finding the right people for a team.',
    },
    {
        id: 'settings',
        title: 'Settings',
        label: 'Settings',
        group: 'account',
        summary: 'Your timezone, AI personalization, email voice and connected accounts.',
    },
    {
        id: 'admins',
        title: 'For admins & execs',
        label: 'For admins & execs',
        group: 'account',
        summary: 'Who can see what, and the tools that appear only for admins and executives.',
    },
    {
        id: 'company-setup',
        title: 'Company setup',
        label: 'Company setup',
        group: 'account',
        summary: 'Telling Cubie about your company: its profile, product catalog and proposal templates.',
    },
];

/** In-app path of a topic's page. Chat links to the same path. */
export const helpPath = (id: string): string => `/help/${id}`;

export const findHelpTopic = (id: string | undefined): HelpTopic | undefined =>
    HELP_TOPICS.find((t) => t.id === id);

export const helpGroupTitle = (key: string): string =>
    HELP_GROUPS.find((g) => g.key === key)?.title ?? key;

/** Fail-closed: a gated topic shows only on an explicit `true` verdict. */
export const canReadHelpTopic = (topic: HelpTopic, access: HelpAccess): boolean =>
    !topic.requires || access[topic.requires] === true;

export const visibleHelpTopics = (access: HelpAccess): HelpTopic[] =>
    HELP_TOPICS.filter((t) => canReadHelpTopic(t, access));

/** Visible topics folded under their headings, in panel order. Empty groups drop out. */
export const groupedHelpTopics = (access: HelpAccess): { group: HelpGroup; topics: HelpTopic[] }[] => {
    const visible = visibleHelpTopics(access);
    return HELP_GROUPS.map((group) => ({
        group,
        topics: visible.filter((t) => t.group === group.key),
    })).filter((g) => g.topics.length > 0);
};

/** The topics either side of `id` in reading order, among those the reader can see. */
export const helpNeighbours = (
    id: string,
    access: HelpAccess,
): { prev: HelpTopic | null; next: HelpTopic | null } => {
    const visible = visibleHelpTopics(access);
    const i = visible.findIndex((t) => t.id === id);
    if (i < 0) return { prev: null, next: null };
    return { prev: visible[i - 1] ?? null, next: visible[i + 1] ?? null };
};

/**
 * Where an old `/help#<id>` link should land now. Every Help link Chat wrote
 * before topics became pages has that shape, and those answers are still in
 * people's chat history, so the anchor keeps working: a known topic id goes to
 * its page. Anything else stays on the Help home.
 */
export const legacyHelpHashTarget = (hash: string): string | null => {
    const id = hash.replace(/^#/, '');
    return findHelpTopic(id) ? helpPath(id) : null;
};

/** Case-insensitive match on what the reader sees on the directory card. */
export const matchesHelpQuery = (topic: HelpTopic, query: string): boolean => {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return [topic.title, topic.label, topic.summary, helpGroupTitle(topic.group)].some((s) =>
        s.toLowerCase().includes(q),
    );
};
