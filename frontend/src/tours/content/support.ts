// Page tours for this area. See ../types.ts for the shape and the rules.
import type { PageTour } from '../types.ts';

export const tours: readonly PageTour[] = [
    // Administration (/admin, admins only)
    {
        id: 'admin.intro',
        kind: 'intro',
        routes: ['/admin'],
        title: 'Administration tour',
        helpTopic: 'admins',
        steps: [
            {
                id: 'welcome',
                title: 'Run Cubie from here',
                body: 'Manage who can use Cubie and how its AI behaves. The sections are listed in the panel on the left, under Workspace, Access, AI Settings, Productivity and Admin.',
                target: 'admin-header',
                placement: 'bottom',
            },
            {
                id: 'section',
                title: 'The section you picked',
                body: 'The section you choose opens here. Without a choice the page opens on “Users”, where you can search, filter, add and edit people, offboard someone from their row, or delete a junk or test account.',
                target: 'admin-section',
                placement: 'top',
            },
            {
                id: 'company-setup',
                title: 'Tell Cubie about the company',
                body: 'Under Workspace, “Company setup” holds the company profile the AI reads, the product catalog and the proposal templates. All three start empty.',
            },
            {
                id: 'workspace-access',
                title: 'Set up the workspace',
                body: 'Under Workspace, see company domains and available areas. Workspace admins can set HubSpot details, the business time zone, report audience, sender details, client-owned AI providers for compatible Chat, Sales, summary and scheduled work, choose knowledge search embeddings by collection, and allow extra Gmail accounts.',
            },
            {
                id: 'feature-access',
                title: 'Decide who gets each feature',
                body: 'Under Access, “Feature Access” sets which teams can reach each feature. A change takes effect the next time people load the app, and you see who gains or loses access before you save.',
            },
            {
                id: 'ai-settings',
                title: 'Tune how the AI works',
                body: 'Under AI Settings, “Sales Models” picks the model each sales feature runs on, and “AI Cost” shows what each AI feature spends.',
            },
            {
                id: 'loose-ends',
                title: 'The Loose Ends daily post',
                body: 'Under Admin, “Loose Ends” turns the daily Loose Ends summary in Slack on or off and picks its channel. “Preview today’s post” shows it without posting anything.',
            },
        ],
    },

    {
        id: 'admin.new.multiple-gmail',
        kind: 'new',
        routes: ['/admin'],
        query: { tab: 'workspace' },
        title: 'Optional extra Gmail accounts',
        helpTopic: 'admins',
        since: '2026-10-30',
        steps: [
            {
                id: 'multi-gmail',
                title: 'Let people connect another mailbox',
                body: 'This switch starts off. Enable it to let people connect another Gmail account. Their work inbox stays the default. Connected mailboxes feed each owner’s labeled AI Priorities and scheduled summaries; personal mail stays out of team and manager views.',
                target: 'admin-workspace-multi-gmail',
                placement: 'top',
            },
        ],
    },

    {
        id: 'admin.new.workspace-ai-providers',
        kind: 'new',
        routes: ['/admin'],
        query: { tab: 'workspace' },
        title: 'New in Administration',
        helpTopic: 'admins',
        since: '2026-10-30',
        steps: [
            {
                id: 'workspace-ai-providers',
                title: 'Use your own AI provider account',
                body: 'Connect your provider account and test a compatible model for each supported workload, including Chat, Sales drafting, priorities, recaps, CRM Sync, call wrap-ups, proposals, X-Ray analysis and grounded web research. Each choice applies only to its named workload.',
                target: 'admin-workspace-ai-providers',
                placement: 'top',
            },
        ],
    },

    {
        id: 'admin.new.embedding-index',
        kind: 'new',
        routes: ['/admin'],
        query: { tab: 'workspace' },
        title: 'Knowledge search embeddings',
        helpTopic: 'admins',
        since: '2026-10-30',
        steps: [
            {
                id: 'embedding-index',
                title: 'Rebuild before changing the embedding model',
                body: 'Choose a knowledge collection, rebuild and verify its search index, then activate it. Search keeps using the current index until activation. Cloud workspaces store client-owned vectors in their own database and sync document updates.',
                target: 'admin-embedding-index',
                placement: 'top',
            },
        ],
    },

    {
        id: 'admin.new.workspace-operations',
        kind: 'new',
        routes: ['/admin'],
        query: { tab: 'workspace' },
        title: 'New in Administration',
        helpTopic: 'admins',
        since: '2026-10-30',
        steps: [
            {
                id: 'workspace-operations',
                title: 'Set this workspace’s clock and reports',
                body: 'Choose the business time zone, ingestion health audience and sender mailboxes for this company. L10 recap delivery starts only after you set its start date.',
                target: 'admin-workspace-operations',
                placement: 'top',
            },
        ],
    },

    {
        id: 'admin.new.crm-settings',
        kind: 'new',
        routes: ['/admin'],
        query: { tab: 'workspace' },
        title: 'New in Administration',
        helpTopic: 'admins',
        since: '2026-10-30',
        steps: [
            {
                id: 'crm-settings',
                title: 'Set up HubSpot details',
                body: 'Platform support assigns the deal pipelines Cubie may read or update. You can name your won stages and custom fields here. No assigned pipeline means no deal reads or writes.',
                target: 'admin-crm-settings',
                placement: 'top',
            },
        ],
    },

    {
        id: 'admin.new.workspace-modules',
        kind: 'new',
        routes: ['/admin'],
        query: { tab: 'workspace' },
        title: 'New in Administration',
        helpTopic: 'admins',
        since: '2026-10-29',
        steps: [
            {
                id: 'workspace-modules',
                title: 'See your workspace access',
                body: 'Review the sign-in domains and available Cubie areas here. If a domain or area needs to change, ask platform support.',
                target: 'admin-workspace-modules',
                placement: 'top',
            },
        ],
    },

    // Administration: the Company setup section (Workspace → Company setup)
    {
        id: 'admin.new.company-setup',
        kind: 'new',
        routes: ['/admin'],
        query: { tab: 'company' },
        title: 'New in Administration',
        helpTopic: 'company-setup',
        since: '2026-10-28',
        steps: [
            {
                id: 'profile',
                title: 'Describe the company',
                body: 'The profile tells the AI what the company sells, to whom and how. Chat, call wrap-ups, CRM Sync, X-Ray, Meeting Prep and proposals read it. Blank fields are left out.',
                target: 'company-profile',
                placement: 'top',
            },
            {
                id: 'catalog',
                title: 'Upload the catalog',
                body: 'Upload your products and services as a CSV. Only the name is required. A file with a bad row changes nothing and lists each row to fix.',
                target: 'company-catalog',
                placement: 'top',
            },
            {
                id: 'templates',
                title: 'Write proposal templates',
                body: 'A template is a standard write-up of one service, plus the catalog items that price it. Proposals print it word for word after the scope of work.',
                target: 'company-templates',
                placement: 'top',
            },
        ],
    },

    // Administration: the Sales Models section
    {
        id: 'admin.new.sales-models',
        kind: 'new',
        routes: ['/admin'],
        title: 'New in Administration',
        helpTopic: 'admins',
        since: '2026-09-17',
        steps: [
            {
                id: 'sales-models',
                title: 'Pick each sales model',
                body: 'Under AI Settings, “Sales Models” sets which model each sales feature runs on, such as proposal drafting and Customer X-Ray. A change applies from the next run.',
            },
        ],
    },

    // Administration: the Loose Ends daily post section
    {
        id: 'admin.new.loose-ends-post',
        kind: 'new',
        routes: ['/admin'],
        title: 'New in Administration',
        helpTopic: 'admins',
        since: '2026-10-07',
        steps: [
            {
                id: 'loose-ends-post',
                title: 'The Loose Ends daily post',
                body: 'Under Admin, “Loose Ends” turns the daily Loose Ends summary in Slack on or off and picks its channel. “Preview today’s post” shows it without posting anything.',
            },
        ],
    },

    // Administration: deleting a junk account that has priorities history
    {
        id: 'admin.new.delete-junk-accounts',
        kind: 'new',
        routes: ['/admin'],
        title: 'New in Administration',
        helpTopic: 'handoffs',
        since: '2026-10-16',
        steps: [
            {
                id: 'delete-junk-accounts',
                title: 'Remove a test account',
                body: 'Under Users, Delete now removes a junk or test account even when Cubie has been writing priorities cards for it. It shows how many there are and asks a second time. A real person should still be offboarded, so their work goes to a successor.',
            },
        ],
    },

    // Help home (/help)
    {
        id: 'help.intro',
        kind: 'intro',
        routes: ['/help'],
        title: 'Help tour',
        helpTopic: 'getting-started',
        steps: [
            {
                id: 'welcome',
                title: 'Guides for all of Cubie',
                body: 'Guides and answers for getting the most out of Cubie. Each topic is a short page of its own.',
                target: 'help-header',
                placement: 'bottom',
            },
            {
                id: 'ask',
                title: 'Rather just ask?',
                body: 'Chat answers questions about Cubie itself from these pages, with a link to the page it used. Click any question in quotes to ask it in Chat.',
                target: 'help-ask',
                placement: 'bottom',
                requires: 'copilot',
            },
            {
                id: 'search',
                title: 'Find a topic',
                body: 'Type a word to narrow the topics below to the ones that match.',
                target: 'help-search',
                placement: 'bottom',
            },
            {
                id: 'topics',
                title: 'Topics by area',
                body: 'Topics are grouped under the app’s own areas, such as “Start here” and “EOS & Teams”. The same list sits in the Help panel on the left.',
                target: 'help-topics',
                placement: 'top',
            },
            {
                id: 'support',
                title: 'Reach RubikSoft support',
                body: 'As an admin, you can write to RubikSoft support at the address at the foot of every Help page.',
                target: 'help-support-line',
                placement: 'top',
                requires: 'admin',
            },
        ],
    },

    // Help home: one page per topic
    {
        id: 'help.new.topic-pages',
        kind: 'new',
        routes: ['/help'],
        title: 'New in Help',
        since: '2026-10-02',
        steps: [
            {
                id: 'topic-pages',
                title: 'One page per topic',
                body: 'Help is now a short page per topic, grouped by area. The same list sits in the Help panel on the left.',
                target: 'help-topics',
                placement: 'top',
            },
            {
                id: 'find',
                title: 'Find a topic',
                body: 'Type a word here to narrow the list to the topics that match.',
                target: 'help-search',
                placement: 'bottom',
            },
        ],
    },

    // Help home: RubikSoft's support address, for admins
    {
        id: 'help.new.support-line',
        kind: 'new',
        routes: ['/help'],
        title: 'New in Help',
        helpTopic: 'tips',
        requires: 'admin',
        since: '2026-10-29',
        steps: [
            {
                id: 'support-line',
                title: 'Reach RubikSoft support',
                body: 'As an admin, you can now write to RubikSoft support at the address at the foot of every Help page. Everyone else is pointed to their Cubie admin.',
                target: 'help-support-line',
                placement: 'top',
            },
        ],
    },

    // Smart Teams: My Work Style (/smart-teams)
    {
        id: 'smart-teams.intro',
        kind: 'intro',
        routes: ['/smart-teams'],
        title: 'Smart Teams tour',
        helpTopic: 'smart-teams',
        requires: 'smart_teams',
        steps: [
            {
                id: 'welcome',
                title: 'Your work-style profile',
                body: 'How you like to work, and how colleagues can get the best out of working with you. Smart Teams helps people work together and never rates anyone.',
                target: 'smart-teams-header',
                placement: 'bottom',
            },
            {
                id: 'interview',
                title: 'Start with the interview',
                body: 'The Work Style Interview mixes quick statements to rate with a short conversation in your own words. It saves as you go, so you can stop and pick up where you left off.',
                target: 'smart-teams-start',
                placement: 'bottom',
            },
            {
                id: 'guide-and-skills',
                title: 'Your guide and your skills',
                body: 'Cubie turns your answers into a “How to work with me” guide that nobody sees until you approve it. “Your skills” lists what you are happy to be asked about on a project.',
            },
            {
                id: 'with-others',
                title: 'Working with others',
                body: '“Compare two people” helps you work with one colleague. “Team Builder” and “Find people by skill” help you find the right people for a piece of work.',
                target: 'smart-teams-copilot-links',
                placement: 'top',
                requires: 'copilot',
            },
            {
                id: 'advisory',
                title: 'Advisory only',
                body: 'Smart Teams is never used for performance reviews, promotion or pay. Only you and the admins who support Smart Teams see your answers, never your manager or the Exec team. Colleagues see the skills you confirm and, once you approve it, your guide.',
            },
        ],
    },

    // Smart Teams: the Work Style Interview (/smart-teams/interview)
    {
        id: 'smart-teams-interview.intro',
        kind: 'intro',
        routes: ['/smart-teams/interview'],
        title: 'Interview tour',
        helpTopic: 'smart-teams',
        requires: 'smart_teams',
        steps: [
            {
                id: 'welcome',
                title: 'One section at a time',
                body: 'The Work Style Interview runs in sections: some are statements you rate, others a short conversation in your own words. This shows the section you are in.',
                target: 'smart-teams-interview-section',
                placement: 'bottom',
            },
            {
                id: 'keyboard',
                title: 'Answer from the keyboard',
                body: 'Press the number for your answer, or use the arrow keys. Answering jumps to the next statement. Questions with two options take A or B.',
                target: 'smart-teams-interview-scale',
                placement: 'bottom',
            },
            {
                id: 'save',
                title: 'Saved as you go',
                body: 'Every answer is saved as you give it. Press “Save & exit” to stop, and pick up where you left off whenever you like.',
                target: 'smart-teams-interview-save',
                placement: 'bottom',
            },
            {
                id: 'finish',
                title: 'When you finish',
                body: 'After the last section, send your answers for scoring. Cubie works through them in the background for a few minutes, then builds the first draft of your profile for you to review.',
            },
        ],
    },

    // Smart Teams: My Guide (/smart-teams/guide)
    {
        id: 'smart-teams-guide.intro',
        kind: 'intro',
        routes: ['/smart-teams/guide'],
        title: 'My Guide tour',
        helpTopic: 'smart-teams',
        requires: 'smart_teams',
        steps: [
            {
                id: 'welcome',
                title: 'Your working guide',
                body: 'A guide to working with you, written from your Work Style Interview. It is about how you prefer to work, never how well you do it.',
                target: 'smart-teams-guide-header',
                placement: 'bottom',
            },
            {
                id: 'write',
                title: 'Write it when you’re ready',
                body: 'Once your interview is scored, press “Write my guide”. Cubie drafts it in the background in a few minutes, and the draft stays private to you.',
            },
            {
                id: 'publish',
                title: 'Publishing is your decision',
                body: 'When your draft is ready, read it, tick the box and press the Publish button: everyone at your company can then read it. You can take it back out of view whenever you like, and download a PDF or Word copy.',
            },
            {
                id: 'colleague',
                title: 'Read a colleague’s guide',
                body: 'Type a colleague’s work email address and press “Open”. You see their guide only if they have published one.',
                target: 'smart-teams-guide-colleague',
                placement: 'top',
            },
        ],
    },

    // Smart Teams: a colleague's published guide (/smart-teams/guide/:email)
    {
        id: 'smart-teams-colleague-guide.intro',
        kind: 'intro',
        routes: ['/smart-teams/guide/:email'],
        title: 'Colleague guide tour',
        helpTopic: 'smart-teams',
        requires: 'smart_teams',
        steps: [
            {
                id: 'welcome',
                title: 'A colleague’s working guide',
                body: 'Their own account of how they prefer to work, written from the Work Style Interview. It is advisory only, never a statement about how well they do it.',
                target: 'smart-teams-guide-header',
                placement: 'bottom',
            },
            {
                id: 'status',
                title: 'When it was published',
                body: 'This line says when they published the guide, or warns you when what you see is a draft they have not shared.',
                target: 'smart-teams-colleague-guide-status',
                placement: 'bottom',
            },
            {
                id: 'download',
                title: 'Saving a copy',
                body: 'You can download a PDF or Word copy. A saved copy stays even if they later take their guide out of view, so treat it as something they told you about themselves.',
                target: 'smart-teams-colleague-guide-download',
                placement: 'top',
            },
        ],
    },

    // Smart Teams: My Skills (/smart-teams/skills)
    {
        id: 'smart-teams-skills.intro',
        kind: 'intro',
        routes: ['/smart-teams/skills'],
        title: 'My Skills tour',
        helpTopic: 'smart-teams',
        requires: 'smart_teams',
        steps: [
            {
                id: 'welcome',
                title: 'What people can ask you about',
                body: 'Your skills are what you would be happy to be asked about on a project. They are not a rating of you.',
                target: 'smart-teams-skills-header',
                placement: 'bottom',
            },
            {
                id: 'suggestions',
                title: 'Suggestions from your own work',
                body: 'Cubie suggests skills from meetings you were in and documents you uploaded. No colleague sees one until you answer “Yes, that’s me”. “Take a fresh look” asks it to look again.',
                target: 'smart-teams-skills-suggestions',
                placement: 'bottom',
            },
            {
                id: 'declare',
                title: 'Add a skill yourself',
                body: 'Search the list (“gdpr” finds data privacy), say how deep you are on each skill, and add them. This is what Team Builder matches on.',
                target: 'smart-teams-skills-declare',
                placement: 'top',
            },
            {
                id: 'lookup',
                title: 'Look someone up',
                body: 'Type a colleague’s work email address and press “Look up” to see the skills they have confirmed, before you ask them onto a project.',
                target: 'smart-teams-skills-lookup',
                placement: 'top',
            },
            {
                id: 'yours',
                title: 'Only you change your skills',
                body: 'Only you can confirm, add or remove your skills. Colleagues see each skill and its level, never why Cubie suggested it.',
            },
        ],
    },

    // Smart Teams: Compare (/smart-teams/compare)
    {
        id: 'smart-teams-compare.intro',
        kind: 'intro',
        routes: ['/smart-teams/compare'],
        title: 'Compare tour',
        helpTopic: 'smart-teams',
        requires: 'smart_teams',
        steps: [
            {
                id: 'welcome',
                title: 'You and one colleague',
                body: 'See how you and one colleague each describe the way you work, and where that lines up or pulls apart. It is advisory only and never rates either of you.',
                target: 'smart-teams-compare-header',
                placement: 'bottom',
            },
            {
                id: 'pick',
                title: 'Pick a colleague',
                body: 'Type at least two characters of their name or email address, then pick them from the list.',
                target: 'smart-teams-colleague-picker',
                placement: 'bottom',
            },
            {
                id: 'both-published',
                title: 'Both guides must be published',
                body: 'Compare needs both of you to have published your own guide. Cubie writes the result to you, about them, and nobody can ask for a comparison of two other people.',
            },
            {
                id: 'words',
                title: 'Words, not scores',
                body: 'Each dimension shows your band and theirs, marked “Same”, “Close” or “Apart”. Bands, not numbers, and nothing here scores how well you work together.',
            },
        ],
    },

    // Smart Teams: Team Builder (/smart-teams/team-builder)
    {
        id: 'smart-teams-team-builder.intro',
        kind: 'intro',
        routes: ['/smart-teams/team-builder'],
        title: 'Team Builder tour',
        helpTopic: 'smart-teams',
        requires: 'smart_teams',
        steps: [
            {
                id: 'welcome',
                title: 'Build a group for work',
                body: 'Name the skills a piece of work needs and Cubie proposes one group that covers them. It never ranks or scores anybody.',
                target: 'smart-teams-team-builder-header',
                placement: 'bottom',
            },
            {
                id: 'skills',
                title: 'What the work needs',
                body: 'Search the skill list and mark each skill “Needed” or “Nice to have”. Cubie matches only the skills people have confirmed about themselves.',
                target: 'smart-teams-team-builder-skills',
                placement: 'bottom',
            },
            {
                id: 'when',
                title: 'Size and dates',
                body: 'Optionally set a group size and dates. Dates are only used to check booked time off; workload and meetings are not considered.',
                target: 'smart-teams-team-builder-when',
                placement: 'top',
            },
            {
                id: 'styles',
                title: 'Mix of working styles',
                body: 'Choose a mix of styles, similar styles or no preference, then press “Find a group”. The answer is not saved, so copy anything you want to keep.',
                target: 'smart-teams-team-builder-styles',
                placement: 'top',
            },
        ],
    },

    // Smart Teams: Find People (/smart-teams/find-people)
    {
        id: 'smart-teams-find-people.intro',
        kind: 'intro',
        routes: ['/smart-teams/find-people'],
        title: 'Find People tour',
        helpTopic: 'smart-teams',
        requires: 'smart_teams',
        steps: [
            {
                id: 'welcome',
                title: 'Find people by skill',
                body: 'Name the work and Cubie lists everyone who told us they do it.',
                target: 'smart-teams-find-people-header',
                placement: 'bottom',
            },
            {
                id: 'search',
                title: 'Name up to six skills',
                body: 'Type a skill and press Enter to add it, up to six at a time, then press “Search”. Everyday wording works: “gdpr” finds data privacy.',
                target: 'smart-teams-find-people-search',
                placement: 'bottom',
            },
            {
                id: 'results',
                title: 'Everyone, not a shortlist',
                body: 'Results list everybody who confirmed a skill about themselves, in name order. It is not a ranking. Click a name to see their guide, if they have published one.',
            },
        ],
    },

    // Smart Teams: Curate skills (/smart-teams/skills/curate, support admins)
    {
        id: 'smart-teams-curate.intro',
        kind: 'intro',
        routes: ['/smart-teams/skills/curate'],
        title: 'Curate skills tour',
        helpTopic: 'smart-teams',
        requires: 'smart_teams',
        steps: [
            {
                id: 'welcome',
                title: 'The shared skills list',
                body: 'The list everyone picks their skills from, searches and gets staffed on. A skill you add with “Add a skill” is searchable straight away.',
                target: 'smart-teams-curate-header',
                placement: 'bottom',
            },
            {
                id: 'added',
                title: 'Skills added here',
                body: 'You can “Retire” a skill added here and “Restore” it later. Skills are never deleted, because someone may already have claimed them.',
                target: 'smart-teams-curate-added',
                placement: 'top',
            },
            {
                id: 'shipped',
                title: 'Skills that ship with Cubie',
                body: 'These come with the app and cannot be changed here. Ask an admin to change one.',
                target: 'smart-teams-curate-shipped',
                placement: 'top',
            },
        ],
    },

    // Smart Teams: Raw profile lookup (/smart-teams/support, support admins)
    {
        id: 'smart-teams-support.intro',
        kind: 'intro',
        routes: ['/smart-teams/support'],
        title: 'Profile lookup tour',
        helpTopic: 'smart-teams',
        requires: 'smart_teams',
        steps: [
            {
                id: 'welcome',
                title: 'Raw profile lookup',
                body: 'For the admins who support Smart Teams: see where one person is in Smart Teams and what is holding them up.',
                target: 'smart-teams-support-header',
                placement: 'bottom',
            },
            {
                id: 'privacy',
                title: 'Private, read-only and logged',
                body: 'This shows one person’s private answers and scores. Use it to fix a problem with them, never to read about them. Nothing can be changed here, and every lookup is logged against you.',
                target: 'smart-teams-support-notice',
                placement: 'bottom',
            },
            {
                id: 'pick',
                title: 'Pick the person',
                body: 'Search the company directory or type the person’s full work email address. Looking yourself up is the safest way to see what this screen shows.',
                target: 'smart-teams-colleague-picker',
                placement: 'bottom',
            },
        ],
    },
];
