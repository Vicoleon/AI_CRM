// Page tours for this area. See ../types.ts for the shape and the rules.
import type { PageTour } from '../types.ts';

export const tours: readonly PageTour[] = [
    // Productivity (/productivity)
    {
        id: 'productivity.intro',
        kind: 'intro',
        routes: ['/productivity'],
        title: 'Productivity tour',
        helpTopic: 'productivity',
        requires: 'copilot',
        steps: [
            {
                id: 'welcome',
                title: 'Your day in full view',
                body: 'Productivity gives you full-page views of your email, calendar, Slack and Drive documents, so you can work through your day in one place.',
                target: 'productivity-header',
                placement: 'bottom',
            },
            {
                id: 'refresh',
                title: 'Always up to date',
                body: 'Email, Calendar, Slack and Documents open with what Cubie has already loaded, and anything over a minute old updates while you look. Click Refresh to pull your latest email, calendar and Slack straight away.',
                target: 'productivity-refresh',
                placement: 'left',
            },
            {
                id: 'tabs',
                title: 'Pick a view',
                body: 'Email shows unread mail addressed to you and flags AI triage failures without hiding messages. If allowed, choose another Gmail mailbox. Calendar lists events and free slots. Team Folders links to shared Drive folders.',
                target: 'productivity-tabs',
                placement: 'bottom',
            },
            {
                id: 'slack',
                title: 'Catch up on Slack',
                body: 'The Slack tab shows your unread channels, mentions and direct messages. Hover over a channel for an AI summary of its unread messages. If Slack isn’t connected yet, use “Connect Slack” there.',
                target: 'productivity-tab-slack',
                placement: 'bottom',
            },
            {
                id: 'documents',
                title: 'Your recent Drive files',
                body: 'The Documents tab lists recent activity on your Drive files; you can pick folders to narrow it. With upload access, “Knowledge” adds a file to the knowledge base for a department you choose, so Chat can answer from it.',
                target: 'productivity-tab-documents',
                placement: 'bottom',
            },
        ],
    },

    {
        id: 'productivity.new.ai-triage-status',
        kind: 'new',
        routes: ['/productivity'],
        query: { tab: 'email' },
        title: 'Email stays visible when AI triage fails',
        helpTopic: 'productivity',
        since: '2026-10-30',
        steps: [
            {
                id: 'email-ai-triage',
                title: 'See your email even when AI is unavailable',
                body: 'If your workspace’s selected AI provider cannot classify importance, Cubie shows a status above the inbox and leaves the messages visible without importance badges. Your admin can check the provider connection.',
                target: 'productivity-email-inbox',
                placement: 'top',
            },
        ],
    },

    {
        id: 'productivity.new.multiple-gmail',
        kind: 'new',
        routes: ['/productivity'],
        query: { tab: 'email' },
        title: 'Choose your Gmail mailbox',
        helpTopic: 'productivity',
        since: '2026-10-30',
        steps: [
            {
                id: 'mailbox',
                title: 'Your work inbox stays the default',
                body: 'If your admin enables additional Gmail accounts, connect one in Settings → Email and choose it here to view its inbox. Connected mailboxes also feed your own AI Priorities and scheduled summaries automatically, with each email item labeled by mailbox.',
                target: 'productivity-email-account',
                placement: 'bottom',
            },
        ],
    },

    // My To-Dos (/todos)
    {
        id: 'todos.intro',
        kind: 'intro',
        routes: ['/todos'],
        title: 'My To-Dos tour',
        helpTopic: 'my-todos',
        requires: 'todos',
        steps: [
            {
                id: 'welcome',
                title: 'Your promises in one list',
                body: 'My To-Dos is your own list of what you said you would do, for whom and by when, grouped by who is waiting on you.',
                target: 'eos-page-header',
                placement: 'bottom',
            },
            {
                id: 'capture',
                title: 'Add one in plain words',
                body: 'Type a promise the way you’d say it out loud and click Add. Cubie shows what it understood: what, for whom and by when. Check it, then click “Looks right, add it”.',
                target: 'todos-capture',
                placement: 'bottom',
            },
            {
                id: 'actions',
                title: 'Start the work in one click',
                body: 'Each to-do has a button that does the first part, like “Prep for it”, “Reply in Gmail” or “Block 30 min”. Click “Kept” when it’s done, or “Snooze” it to another day.',
            },
            {
                id: 'heard',
                title: 'Promises Cubie heard',
                body: 'Promises Cubie hears you make in meetings, L10s, sent email and Slack wait under “Cubie heard you promise…”. Nothing counts until you click “Yes, add it”. If Cubie later finds proof one is done, it asks you to confirm.',
                target: 'todos-heard',
                placement: 'bottom',
                requires: 'promise_accountability',
            },
            {
                id: 'ledger',
                title: 'Your week at a glance',
                body: 'The footer counts what you kept, dropped and still have open this week. To-dos that are overdue or due soon can also show up in your Daily Briefing.',
                target: 'todos-ledger',
                placement: 'top',
            },
            {
                id: 'elsewhere',
                title: 'Add from anywhere',
                body: 'You can also say “remind me to…” in Chat, use /todo in Slack and pick “My list”, or choose “Add to my To-Dos” on a Daily Briefing card.',
            },
        ],
    },

    // My To-Dos (/todos): promises Cubie hears and checks
    {
        id: 'todos.new.heard-promises',
        kind: 'new',
        routes: ['/todos'],
        title: 'New in My To-Dos',
        helpTopic: 'my-todos',
        requires: 'promise_accountability',
        since: '2026-10-01',
        steps: [
            {
                id: 'heard',
                title: 'Cubie now hears your promises',
                body: 'Promises you make in meetings, L10s, sent email and Slack now show up here under “Cubie heard you promise…”, with your exact words. Click “Yes, add it” to keep one; nothing counts until you do.',
                target: 'todos-heard',
                placement: 'bottom',
            },
            {
                id: 'confirm',
                title: 'Confirm what’s done',
                body: 'Once a promise’s date passes, Cubie checks your sent email, Slack, Drive and EOS To-Dos for signs you kept it. If it finds one, the promise moves to “Looks done — confirm?” Cubie never marks anything kept on its own.',
                target: 'todos-confirm',
                placement: 'bottom',
            },
        ],
    },

    // Meeting Prep (/meeting-prep/:eventId)
    {
        id: 'meeting-prep.intro',
        kind: 'intro',
        routes: ['/meeting-prep/:eventId'],
        title: 'Meeting Prep tour',
        helpTopic: 'meeting-prep',
        requires: 'copilot',
        steps: [
            {
                id: 'welcome',
                title: 'Your briefing for this meeting',
                body: 'Cubie builds this briefing from your email, Drive and past meetings with these attendees and, for clients, HubSpot. The “Built from” line shows which sources it used.',
                target: 'meeting-prep-header',
                placement: 'bottom',
            },
            {
                id: 'regenerate',
                title: 'Build a new version',
                body: 'Click Regenerate for a fresh version, steered by an optional “Meeting intent”. Cubie builds it in the background, so you can leave the page; it notifies you with a link when it’s ready.',
                target: 'meeting-prep-regenerate',
                placement: 'bottom',
            },
            {
                id: 'edit',
                title: 'Edit or rewrite any section',
                body: 'Use the pencil to change a section yourself, or the sparkle to have Cubie rewrite just that section, with an optional instruction like “shorter”. Every change is saved as a new version.',
                target: 'meeting-prep-section-edit',
                placement: 'left',
            },
            {
                id: 'versions',
                title: 'Every version is kept',
                body: 'Open the version menu to see accessible versions, when each was made and what changed. Older versions open read-only, with “Restore this version” to make one the latest again.',
                target: 'meeting-prep-versions',
                placement: 'bottom',
            },
            {
                id: 'export',
                title: 'Take it with you',
                body: 'Print or save it as a PDF, download an editable Word copy, or use “Save to Assets” to share it with teammates from your Assets page. Each one uses the version you have open.',
                target: 'meeting-prep-export',
                placement: 'bottom',
            },
        ],
    },

    // Meeting Prep (/meeting-prep/:eventId): versions and editing on the page
    {
        id: 'meeting-prep.new.versions',
        kind: 'new',
        routes: ['/meeting-prep/:eventId'],
        title: 'New in Meeting Prep',
        helpTopic: 'meeting-prep',
        requires: 'copilot',
        since: '2026-10-01',
        steps: [
            {
                id: 'versions',
                title: 'Your briefing now has versions',
                body: 'Opening a meeting’s prep now shows your latest saved version instead of rebuilding it. The version menu lists every version and what changed, and an older one can be restored.',
                target: 'meeting-prep-versions',
                placement: 'bottom',
            },
            {
                id: 'edit',
                title: 'Edit right on the page',
                body: 'Use the pencil to change a section yourself, or the sparkle to have Cubie rewrite just that section. Regenerate keeps your edits unless you untick “Keep my edits”.',
                target: 'meeting-prep-section-edit',
                placement: 'left',
            },
        ],
    },

    {
        id: 'meeting-prep.new.crm-access-refresh',
        kind: 'new',
        routes: ['/meeting-prep/:eventId'],
        title: 'Meeting Prep access refresh',
        helpTopic: 'meeting-prep',
        requires: 'copilot',
        since: '2026-10-30',
        steps: [
            {
                id: 'crm-access',
                title: 'Current client access shapes your briefing',
                body: 'If access to a client changes in HubSpot, earlier briefings based on that client become unavailable. Cubie builds a fresh briefing from sources you can currently access.',
                target: 'meeting-prep-header',
                placement: 'bottom',
            },
        ],
    },

    // Handoffs (/handoffs)
    {
        id: 'handoffs.intro',
        kind: 'intro',
        routes: ['/handoffs'],
        title: 'Handoffs tour',
        helpTopic: 'handoffs',
        steps: [
            {
                id: 'welcome',
                title: 'Work handed to you',
                body: 'When a teammate leaves the company, the open work they left behind lands with their successor. If that’s you, each person you took over from is listed here.',
                target: 'handoffs-header',
                placement: 'bottom',
            },
            {
                id: 'open',
                title: 'Open a handoff',
                body: 'Open a handoff to see its brief and checklist. Deal details appear only while Cubie confirms workspace access. Email and calendar commitments link to a verified source. Mark an item done or dismiss it when it is not needed.',
                target: 'handoffs-list',
                placement: 'top',
            },
            {
                id: 'follow-up',
                title: 'Cubie keeps following up',
                body: 'Open items stay in your daily AI Priorities until you close them, here or on the card. Inside a handoff you can also read their past priorities, read-only.',
            },
        ],
    },

    {
        id: 'handoffs.new.workspace-crm-proof',
        kind: 'new',
        routes: ['/handoffs'],
        title: 'Workspace access in handoffs',
        helpTopic: 'handoffs',
        since: '2026-10-30',
        steps: [
            {
                id: 'workspace-crm',
                title: 'Current access to old CRM work',
                body: 'Older deal details and inherited cards can disappear if your workspace no longer has access to those HubSpot deals. Email and calendar commitments need a verifiable source too. Cubie checks before showing them.',
                target: 'handoffs-list',
                placement: 'top',
            },
        ],
    },

    // Insights dashboards (/insights, /insights/:dashboard)
    {
        id: 'insights.intro',
        kind: 'intro',
        routes: ['/insights', '/insights/:dashboard'],
        title: 'Insights tour',
        helpTopic: 'insights',
        requires: 'insights',
        steps: [
            {
                id: 'welcome',
                title: 'See how Cubie is used',
                body: 'Insights shows how Cubie is used and what it produces, by department. Pick a dashboard from the Insights panel on the left, under Company, Work, Sales or Platform; each one answers the question under its title.',
                target: 'insights-header',
                placement: 'bottom',
            },
            {
                id: 'filters',
                title: 'Filter by department and date',
                body: 'Choose departments and a date range: 7D, 30D, 90D, QTD, YTD or Custom. The numbers below follow these filters, and they stay with you when you switch dashboards.',
                target: 'insights-filters',
                placement: 'bottom',
            },
            {
                id: 'summary',
                title: 'Get a quick read',
                body: 'Click “Summarize this view” for a short AI read of what moved and what is worth watching. It runs only when you ask, and uses only the numbers on this page.',
                target: 'insights-ai-summary',
                placement: 'bottom',
            },
            {
                id: 'numbers',
                title: 'Every number explains itself',
                body: 'Each rate shows what it is out of. The ⓘ beside a number says what it counts, and the chip beside it compares it with the same number of days just before.',
                target: 'insights-kpis',
                placement: 'top',
            },
            {
                id: 'share',
                title: 'Share or download a view',
                body: '“Copy link” copies this exact view, filters included, so a colleague sees the same thing within their own access. “Export” downloads the headline numbers, and “Definitions” explains each one.',
                target: 'insights-actions',
                placement: 'bottom',
            },
            {
                id: 'people',
                title: 'Open a person’s profile',
                body: 'Where a table lists people, click a name to open that person’s profile: their activity, numbers and the features they use. Each profile you open is recorded.',
                requires: 'insights_people',
            },
        ],
    },

    // Insights: the "Systems" group of the panel is now "Platform", 2026-10-29
    {
        id: 'insights.new.platform-group',
        kind: 'new',
        routes: ['/insights', '/insights/:dashboard'],
        title: 'New in Insights',
        helpTopic: 'insights',
        requires: 'insights',
        since: '2026-10-29',
        steps: [
            {
                id: 'platform',
                title: 'Systems is now Platform',
                body: 'The Insights panel’s Systems heading is now called Platform. AI Spend, Knowledge, and Quality & Trust are under it, unchanged: what Cubie costs, whether the knowledge base is used, and how far people trust it.',
                target: 'insights-platform-group',
                placement: 'right',
            },
        ],
    },

    // Insights person profile (/insights/people/:email)
    {
        id: 'insights-person.intro',
        kind: 'intro',
        routes: ['/insights/people/:email'],
        title: 'Person profile tour',
        helpTopic: 'insights',
        requires: 'insights_people',
        steps: [
            {
                id: 'welcome',
                title: 'One person’s use of Cubie',
                body: 'This profile shows how one person uses Cubie in numbers only (activity counts, rates, AI spend), never the text of their messages, email or chats. Use it to spot a feature they haven’t found yet, not as a performance review.',
                target: 'insights-person-header',
                placement: 'bottom',
            },
            {
                id: 'range',
                title: 'Pick the period',
                body: 'Only the date range applies to a person. Their numbers are compared with the same number of days just before.',
                target: 'insights-person-range',
                placement: 'bottom',
            },
            {
                id: 'activity',
                title: 'Their last 90 days',
                body: 'Each square is a day, and darker means busier. Point at a square for the exact count. Days are counted in the company’s business time zone.',
                target: 'insights-person-activity',
                placement: 'right',
            },
            {
                id: 'dashboards',
                title: 'Their numbers by dashboard',
                body: 'A card per dashboard shows this person’s numbers there. “Open” takes you to that dashboard, filtered to their department.',
                target: 'insights-person-dashboards',
                placement: 'top',
            },
            {
                id: 'features',
                title: 'What they can use, and do',
                body: 'See the features they have access to and whether they used each one in this period. “Not used yet” marks one they can open but didn’t use. Each profile you open is recorded.',
                target: 'insights-person-features',
                placement: 'top',
            },
        ],
    },
];
