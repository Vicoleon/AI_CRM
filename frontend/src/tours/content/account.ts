// Page tours for Settings and for connecting Cubie for Chrome, and the card
// that tells someone how to see a tour again. See ../types.ts for the shape
// and the rules.
import type { PageTour, TourStep } from '../types.ts';

/** Added to the end of the very first tour a person sees (TourProvider), so
 *  everyone learns once where tours live without a card on every page. */
export const REPLAY_HINT: TourStep = {
    id: 'replay-hint',
    title: 'See a tour again',
    body: 'Open the menu under your name, at the bottom left, and choose Replay this page’s tour. You can skip any tour with Skip tour or the Esc key.',
    target: 'account-menu',
    placement: 'right',
};

export const tours: readonly PageTour[] = [
    // Settings
    {
        id: 'settings.intro',
        kind: 'intro',
        routes: ['/settings'],
        title: 'Settings tour',
        helpTopic: 'settings',
        steps: [
            {
                id: 'purpose',
                title: 'Make Cubie work your way',
                body: 'Settings holds your own preferences: your timezone and working hours, how Cubie writes and answers for you, and your connected Slack and Gmail accounts when your workspace allows them.',
                target: 'settings-header',
                placement: 'bottom',
            },
            {
                id: 'sections',
                title: 'One section at a time',
                body: 'Pick a section in the panel on the left: General, AI Personalization, Email, Slack and more. Email is where you connect another Gmail account and review AI model changes before personal mail is included again.',
                target: 'settings-sections',
                placement: 'right',
            },
            {
                id: 'page-tours',
                title: 'Tours on your terms',
                body: 'Under General, Page tours lets you stop tours from starting by themselves, or bring every tour back to see them again.',
                target: 'settings-page-tours',
                placement: 'bottom',
                requires: 'page_tours',
            },
            {
                id: 'chrome-extension',
                title: 'Your browsers, for Cubie for Chrome',
                body: 'Under General, Cubie for Chrome lists each browser that shows your day on its new tab. Disconnect one there, or connect this browser.',
                target: 'settings-chrome-extension',
                placement: 'top',
                requires: 'chrome_extension',
            },
            {
                id: 'replay',
                title: 'Replay a tour any time',
                body: 'Open the menu under your name, at the bottom left, and choose Replay this page’s tour to see a page’s tour again.',
                target: 'account-menu',
                placement: 'right',
            },
        ],
    },
    {
        id: 'settings.new.multiple-gmail',
        kind: 'new',
        routes: ['/settings'],
        query: { tab: 'email' },
        title: 'More than one Gmail account',
        helpTopic: 'settings',
        since: '2026-10-30',
        steps: [
            {
                id: 'gmail-accounts',
                title: 'Connect a second mailbox if enabled',
                body: 'Connect another mailbox here, then choose it in Productivity → Email. It joins your private AI Priorities and scheduled summaries automatically, labeled when cited. Your workspace’s selected AI provider processes included mail. Your work inbox remains the default.',
                target: 'settings-gmail-accounts',
                placement: 'bottom',
            },
        ],
    },
    {
        id: 'settings.new.gmail-provider-review',
        kind: 'new',
        routes: ['/settings'],
        query: { tab: 'email' },
        title: 'Review personal Gmail AI access',
        helpTopic: 'settings',
        since: '2026-10-30',
        steps: [
            {
                id: 'review',
                title: 'You choose after a model change',
                body: 'If your admin changes the AI Priorities model, Cubie pauses automatic use of your extra Gmail until you review the new provider here. You can still open the mailbox manually.',
                target: 'settings-gmail-accounts',
                placement: 'bottom',
            },
        ],
    },
    {
        id: 'settings.new.workspace-slack-access',
        kind: 'new',
        routes: ['/settings'],
        title: 'Slack connection for this workspace',
        helpTopic: 'settings',
        since: '2026-10-30',
        steps: [
            {
                id: 'slack-access',
                title: 'Connect before using Slack commands',
                body: 'Connect your Slack account here before using /todo or /ids. If your company’s Workspace domain is removed, the connection pauses until your admin restores access.',
                target: 'settings-slack',
                placement: 'bottom',
            },
        ],
    },
    // Settings (/settings): the Cubie for Chrome card under General, added 2026-10-15.
    {
        id: 'settings.new.chrome-extension',
        kind: 'new',
        routes: ['/settings'],
        title: 'New in Settings',
        helpTopic: 'cubie-for-chrome',
        requires: 'chrome_extension',
        since: '2026-10-15',
        steps: [
            {
                id: 'connected-browsers',
                title: 'Browsers showing your day',
                body: 'Cubie for Chrome puts your promises past due and today’s priorities on every new tab. Each connected browser is listed here, and Disconnect turns one off at once.',
                target: 'settings-chrome-extension',
                placement: 'top',
            },
        ],
    },
    // Connect Cubie for Chrome (/chrome/connect), where the extension sends
    // someone to connect a browser.
    {
        id: 'chrome-connect.intro',
        kind: 'intro',
        routes: ['/chrome/connect'],
        title: 'Cubie for Chrome tour',
        helpTopic: 'cubie-for-chrome',
        requires: 'chrome_extension',
        steps: [
            {
                id: 'connect',
                title: 'Connect this browser once',
                body: 'Connect this browser and every new tab shows your promises past due, then today’s priorities. If the extension is not in this browser yet, this card shows how to add it.',
                target: 'chrome-connect-card',
                placement: 'bottom',
            },
            {
                id: 'privacy',
                title: 'Only your day, nothing else',
                body: 'It shows what Cubie already made for you this morning. It cannot read your email, your browsing or any other site. Disconnect it any time in Settings, under General.',
            },
        ],
    },
];
