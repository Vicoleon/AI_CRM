// Page tours for this area. See ../types.ts for the shape and the rules.
import type { PageTour } from '../types.ts';

export const tours: readonly PageTour[] = [
    // VTO (/eos/vto)
    {
        id: 'eos-vto.intro',
        kind: 'intro',
        routes: ['/eos/vto'],
        title: 'VTO tour',
        helpTopic: 'eos',
        steps: [
            {
                id: 'purpose',
                title: 'Your company’s plan on one page',
                body: 'The VTO (Vision / Traction Organizer) is your company’s strategic plan. The Exec team keeps it current, and everyone in the company can read it here.',
                target: 'eos-page-header',
                placement: 'bottom',
            },
            {
                id: 'vision',
                title: 'Where we’re headed',
                body: 'Vision holds the company’s Core Values, Core Focus and 10-Year Target: who we are and where we want to be.',
                target: 'eos-vto-vision',
                placement: 'right',
            },
            {
                id: 'traction',
                title: 'How we get there',
                body: 'Traction turns the vision into plans: the 3-Year Picture, the 1-Year Plan and the Quarterly Rocks.',
                target: 'eos-vto-traction',
                placement: 'left',
            },
            {
                id: 'publishing',
                title: 'Who updates it',
                body: 'If you’re on the Exec team, the Publish the VTO box at the top uploads a new version from the Excel template. Lines marked Confidential show only to the Exec team and admins.',
            },
        ],
    },

    // Scorecard (/eos/scorecard)
    {
        id: 'eos-scorecard.intro',
        kind: 'intro',
        routes: ['/eos/scorecard'],
        title: 'Scorecard tour',
        helpTopic: 'eos',
        steps: [
            {
                id: 'purpose',
                title: 'Your team’s weekly numbers',
                body: 'The Scorecard shows your team’s weekly numbers against their goals, grouped by owner. A week turns green or red against its goal, so a slipping number stands out.',
                target: 'eos-page-header',
                placement: 'bottom',
            },
            {
                id: 'grid',
                title: 'Enter this week’s number',
                body: 'Click a cell to type that week’s number and add a note on what it’s made of. Anyone on a measurable’s team can fill in its numbers. The 4- and 13-week averages stay pinned on the right.',
                target: 'eos-scorecard-grid',
                placement: 'top',
            },
            {
                id: 'reorder',
                title: 'Put measurables in your order',
                body: 'Hover a measurable and drag the grip on its left to move it up or down among the same owner’s measurables. You can reorder the ones you can edit, and your team sees the new order here.',
                target: 'eos-scorecard-measurables',
                placement: 'bottom',
            },
            {
                id: 'import',
                title: 'Enter many numbers at once',
                body: 'Import lets you download a blank Excel template with one column per week. Type one row per measurable, then upload it to fill in many weeks at once.',
                target: 'eos-scorecard-import',
                placement: 'bottom',
            },
            {
                id: 'add',
                title: 'Track a new number',
                body: 'Add Measurable sets up a new weekly number with its owner and goal. Give it both a Compare and a Target so its weeks turn green or red.',
                target: 'eos-scorecard-add',
                placement: 'bottom',
            },
        ],
    },
    // Scorecard (/eos/scorecard): drag-and-drop ordering
    {
        id: 'eos-scorecard.new.reorder',
        kind: 'new',
        routes: ['/eos/scorecard'],
        title: 'New in Scorecard',
        helpTopic: 'eos',
        since: '2026-09-28',
        steps: [
            {
                id: 'reorder',
                title: 'Put measurables in your order',
                body: 'Hover a measurable and drag the grip on its left to move it up or down among the same owner’s measurables. You can reorder the ones you can edit, and your team sees the new order here.',
                target: 'eos-scorecard-measurables',
                placement: 'bottom',
            },
        ],
    },

    // Rocks (/eos/rocks)
    {
        id: 'eos-rocks.intro',
        kind: 'intro',
        routes: ['/eos/rocks'],
        title: 'Rocks tour',
        helpTopic: 'eos',
        steps: [
            {
                id: 'purpose',
                title: 'Your quarterly priorities',
                body: 'Rocks are the 90-day goals each team commits to. Each one has an owner and a status (On Track, Off Track or Done), and can carry milestones along the way.',
                target: 'eos-page-header',
                placement: 'bottom',
            },
            {
                id: 'grouping',
                title: 'Group the list your way',
                body: 'Switch between By team, By person and By status. By status puts Off Track Rocks first, so whatever needs attention leads.',
                target: 'eos-rocks-grouping',
                placement: 'bottom',
            },
            {
                id: 'list',
                title: 'Open a Rock',
                body: 'Click the arrow on a row to see its milestones and due dates. Hover a row to log the weekly update or edit it. Anyone on the Rock’s team can update it.',
                target: 'eos-rocks-list',
                placement: 'top',
            },
            {
                id: 'carry-over',
                title: 'Carry a Rock into next quarter',
                body: 'Hover a Rock that isn’t Done and click the carry-over icon, or expand it and click Carry over. You get an editable draft for the next quarter with only the unfinished milestones. Last quarter’s Rock stays as it was.',
                target: 'eos-rocks-list',
                placement: 'top',
            },
            {
                id: 'all-teams',
                title: 'Every team’s Rocks',
                body: 'You can see every team’s Rocks except the Exec team’s. The Team filter lists them all: pick one to see and update its Rocks, or choose it when adding a Rock.',
                target: 'eos-rocks-team-filter',
                placement: 'bottom',
                requires: 'eos_rocks_all_teams',
            },
            {
                id: 'new-rock',
                title: 'Add a Rock',
                body: 'New Rock adds a Rock with its owner and team. Type a rough goal in the form and Draft with AI turns it into a Rock with milestones you can edit before saving.',
                target: 'eos-rocks-new',
                placement: 'bottom',
            },
        ],
    },
    // Rocks (/eos/rocks): carry a Rock over to next quarter
    {
        id: 'eos-rocks.new.carry-over',
        kind: 'new',
        routes: ['/eos/rocks'],
        title: 'New in Rocks',
        helpTopic: 'eos',
        since: '2026-10-09',
        steps: [
            {
                id: 'carry-over',
                title: 'Carry a Rock into next quarter',
                body: 'Hover a Rock that isn’t Done and click the carry-over icon, or expand it and click Carry over. You get an editable draft for the next quarter with only the unfinished milestones. Last quarter’s Rock stays as it was.',
                target: 'eos-rocks-list',
                placement: 'top',
            },
        ],
    },
    // Rocks (/eos/rocks): every team's Rocks for the people who keep them
    {
        id: 'eos-rocks.new.all-teams',
        kind: 'new',
        routes: ['/eos/rocks'],
        title: 'New in Rocks',
        helpTopic: 'eos',
        requires: 'eos_rocks_all_teams',
        since: '2026-10-13',
        steps: [
            {
                id: 'all-teams',
                title: 'Every team’s Rocks',
                body: 'The Team filter now lists every team except the Exec team. Pick one to see and update its Rocks, or choose it when adding a Rock.',
                target: 'eos-rocks-team-filter',
                placement: 'bottom',
            },
        ],
    },

    // To-Dos (/eos/todos)
    {
        id: 'eos-todos.intro',
        kind: 'intro',
        routes: ['/eos/todos'],
        title: 'To-Dos tour',
        helpTopic: 'eos',
        steps: [
            {
                id: 'purpose',
                title: 'Your team’s to-dos',
                body: 'To-Dos are short action items, each with an owner, meant to be done within 7 days. The same list shows inside your team’s L10.',
                target: 'eos-page-header',
                placement: 'bottom',
            },
            {
                id: 'due',
                title: 'See what’s due',
                body: 'Open to-dos are grouped by due date: Overdue, This week, Later and No due date, so overdue ones come first. The tiles above count what’s Open, Due this week, Overdue and Completed.',
                target: 'eos-todos-open',
                placement: 'top',
            },
            {
                id: 'progress',
                title: 'Keep it moving',
                body: 'Tick the box to mark a to-do done. Hover a row and click the speech bubble to add a note on progress or what’s blocking it.',
                target: 'eos-todos-open',
                placement: 'top',
            },
            {
                id: 'new-todo',
                title: 'Add a to-do',
                body: 'New To-Do takes one line of up to 120 characters and an owner; a due date is optional. On more than one team? Pick whose L10 it joins. Longer than a line? Raise an Issue instead.',
                target: 'eos-todos-new',
                placement: 'bottom',
            },
        ],
    },

    // Issues (/eos/issues)
    {
        id: 'eos-issues.intro',
        kind: 'intro',
        routes: ['/eos/issues'],
        title: 'Issues tour',
        helpTopic: 'eos',
        steps: [
            {
                id: 'purpose',
                title: 'What your team needs to solve',
                body: 'Issues is your team’s list for IDS: Identify, Discuss, Solve. Raise one with New Issue and it’s ready for your team’s L10.',
                target: 'eos-page-header',
                placement: 'bottom',
            },
            {
                id: 'search',
                title: 'Find an issue fast',
                body: 'Type a few words to search issue titles and descriptions. Solved and archived matches open up while you search.',
                target: 'eos-issues-search',
                placement: 'bottom',
            },
            {
                id: 'open',
                title: 'Work the open list',
                body: 'Open issues are grouped High, Medium and Low. Click an issue to read its full details, or click the circle on its left to mark it solved.',
                target: 'eos-issues-open',
                placement: 'top',
            },
            {
                id: 'todos',
                title: 'Turn an issue into to-dos',
                body: 'Hover an issue and use the add to-do icon to assign follow-up work. The issue can’t be marked solved until all of its to-dos are done.',
                target: 'eos-issues-open',
                placement: 'top',
            },
            {
                id: 'solved',
                title: 'Look back at what’s solved',
                body: 'Click Solved to open what your team has closed. Use 30d, 60d, 90d or All to choose how far back it goes.',
                target: 'eos-issues-solved',
                placement: 'top',
            },
        ],
    },

    // L10 Meetings (/eos/meetings)
    {
        id: 'eos-meetings.intro',
        kind: 'intro',
        routes: ['/eos/meetings'],
        title: 'L10 Meetings tour',
        helpTopic: 'eos',
        steps: [
            {
                id: 'purpose',
                title: 'Your L10 meetings',
                body: 'Here are the L10 meetings you sit on. Prepare for the weekly meeting, start it when it’s time and look back at past sessions.',
                target: 'eos-page-header',
                placement: 'bottom',
            },
            {
                id: 'calendar',
                title: 'From your calendar',
                body: 'When your Google Calendar has upcoming L10s, they’re listed under Next L10s on your calendar. Import brings one into Cubie, Sync refreshes its time and attendees, and Join opens the Meet link.',
                target: 'eos-meetings-calendar',
                placement: 'bottom',
            },
            {
                id: 'cards',
                title: 'Before, during and after',
                body: 'On a meeting’s card, Add headlines lets you share news before the session and History shows past sessions and their Cubie recaps. Start opens the live meeting on its scheduled day; Rejoin takes you back into one in progress.',
                target: 'eos-meetings-list',
                placement: 'top',
            },
            {
                id: 'restart',
                title: 'Restart a live meeting',
                body: 'On a meeting in progress, Restart takes everyone back to the top of the agenda with a fresh timer and keeps the notes and check-ins already captured. The Exec team’s L10 can’t be restarted.',
                target: 'eos-meetings-list',
                placement: 'top',
                requires: 'l10_meeting_restart',
            },
        ],
    },
    {
        id: 'eos-meetings.new.recap-provider',
        kind: 'new',
        routes: ['/eos/meetings'],
        title: 'New in L10 Meetings',
        helpTopic: 'eos',
        since: '2026-10-30',
        steps: [{
            id: 'recap-provider',
            title: 'Your workspace chooses the recap provider',
            body: 'Open History for a concluded meeting to read its Cubie recap. Your admin can choose its AI provider separately from recap emails. If the provider cannot answer, Cubie shows a retryable error.',
            target: 'eos-meetings-list',
            placement: 'top',
        }],
    },
    // L10 Meetings (/eos/meetings): Restart is now an Admin → Feature Access
    // row (admins by default) instead of one team's button.
    {
        id: 'eos-meetings.new.restart',
        kind: 'new',
        routes: ['/eos/meetings'],
        title: 'New in L10 Meetings',
        helpTopic: 'eos',
        requires: 'l10_meeting_restart',
        since: '2026-10-28',
        steps: [
            {
                id: 'restart',
                title: 'Restart a live meeting',
                body: 'You can now restart any meeting you run while it’s in progress. Restart on its card takes everyone back to the top of the agenda with a fresh timer and keeps the notes and check-ins already captured. The Exec team’s L10 can’t be restarted.',
                target: 'eos-meetings-list',
                placement: 'top',
            },
        ],
    },

    // L10 session (/eos/meetings/:meetingId), run live in front of the team
    {
        id: 'eos-l10.intro',
        kind: 'intro',
        routes: ['/eos/meetings/:meetingId'],
        title: 'L10 meeting tour',
        helpTopic: 'eos',
        autoStart: false,
        steps: [
            {
                id: 'purpose',
                title: 'Your live L10',
                body: 'This is the weekly meeting itself. Everyone follows the facilitator: when they move to the next section, every screen moves with them.',
                target: 'eos-l10-header',
                placement: 'bottom',
            },
            {
                id: 'pace',
                title: 'Meeting pace',
                body: 'The pace bar maps the whole 90-minute meeting and shows the time left, and whether you’re on pace, ahead or over.',
                target: 'eos-l10-pace',
                placement: 'bottom',
            },
            {
                id: 'agenda',
                title: 'The agenda',
                body: 'The sidebar shows the clock for this section and the agenda, from Check-in to Conclude. Only the facilitator moves the meeting on, with Next section at the bottom.',
                target: 'eos-l10-agenda',
                placement: 'right',
            },
            {
                id: 'pitch-in',
                title: 'Everyone can pitch in',
                body: 'As you talk, anyone can enter this week’s numbers, update Rocks, check off to-dos and add issues. If your screen falls behind the room, Jump to or Resync now brings it back.',
                target: 'eos-l10-footer',
                placement: 'top',
            },
            {
                id: 'rabbit',
                title: 'Gone off topic?',
                body: 'Click Rabbit hole and everyone in the meeting sees the rabbit, a friendly nudge to get back on track.',
                target: 'eos-l10-rabbit',
                placement: 'right',
            },
            {
                id: 'exit',
                title: 'Leaving the meeting',
                body: 'The left navigation is hidden while a meeting is open, so a stray click can’t pull you out. Use Exit meeting to go back to your meetings.',
                target: 'eos-l10-exit',
                placement: 'right',
            },
        ],
    },

    // Core Processes (/eos/processes)
    {
        id: 'eos-processes.intro',
        kind: 'intro',
        routes: ['/eos/processes'],
        title: 'Core Processes tour',
        helpTopic: 'eos',
        steps: [
            {
                id: 'purpose',
                title: 'Your company’s core processes',
                body: 'Core Processes is where your company documents the way it runs. FBA stands for Followed By All.',
                target: 'eos-page-header',
                placement: 'bottom',
            },
            {
                id: 'list',
                title: 'Read a process',
                body: 'Each row shows a process and its owner. Click Open to read its document, where one is linked.',
                target: 'eos-processes-list',
                placement: 'top',
            },
            {
                id: 'new-process',
                title: 'Document a process',
                body: 'New Process adds one with its name and, if you have them, an owner and a link to its document.',
                target: 'eos-processes-new',
                placement: 'bottom',
            },
        ],
    },

    // Executive EOS Dashboard (/eos/dashboard), Exec team and admins
    {
        id: 'eos-dashboard.intro',
        kind: 'intro',
        routes: ['/eos/dashboard'],
        title: 'Exec dashboard tour',
        helpTopic: 'eos',
        steps: [
            {
                id: 'purpose',
                title: 'Every team’s EOS at a glance',
                body: 'The Executive EOS Dashboard rolls up meetings, Rocks, To-Dos and Issues across departments, so you can see which teams need attention.',
                target: 'eos-page-header',
                placement: 'bottom',
            },
            {
                id: 'filters',
                title: 'Choose scope and period',
                body: 'Scope narrows the view to one department, or shows them all under Executive View. Period switches to another quarter.',
                target: 'eos-dashboard-filters',
                placement: 'bottom',
            },
            {
                id: 'health',
                title: 'Overall health',
                body: 'The Overall Health score, out of 100, blends meeting ratings, Rock health and to-do completion. The cards beside it show each one on its own.',
                target: 'eos-dashboard-health',
                placement: 'bottom',
            },
            {
                id: 'teams',
                title: 'Compare teams',
                body: 'Each team gets a health score, meeting rating, Rock health, to-do completion and average issue age. Click a row to drill into that team.',
                target: 'eos-dashboard-teams',
                placement: 'top',
            },
            {
                id: 'trends',
                title: 'Spot the trends',
                body: 'The trend charts follow meeting health, Rock status, issues opened per week and the share of issues solved each week.',
                target: 'eos-dashboard-trends',
                placement: 'top',
            },
        ],
    },
];
