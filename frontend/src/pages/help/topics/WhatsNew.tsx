import React from 'react';
import { Callout, SubHeading, TopicLink } from '../kit';

// Help → What's new (the drawer of Cubie's release notes). Served at /help/whats-new; Chat
// cites the same id from rubik_core/help/content.py, so a change here travels there in the
// same PR. docs/whats_new/README.md is the feature's design contract: when a rule changes
// there (what is shown to whom, what is counted, when the drawer teases), it changes here too.

export const WhatsNew: React.FC = () => (
    <>
        <p>
            <span className="font-medium">What&apos;s new</span> is the list of Cubie&apos;s release
            notes: each change to Cubie that matters to you, explained in a sentence or two.
            RubikSoft, the team that makes Cubie, writes each one. Open it from the{' '}
            <span className="font-medium">What&apos;s new</span> button at the bottom of the
            left-hand menu, just above your name. It opens from the right on whatever page you are
            on, and closes back to the same page.
        </p>

        <SubHeading id="the-drawer">The button and the drawer</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">The button</span> — shows how many updates are new since your last visit</li>
            <li><span className="font-medium">The list</span> — the updates, newest first. Each has its date, a title and a short description; a dot marks the ones you have not seen yet. When there is nothing to read yet, the drawer says &ldquo;No updates yet.&rdquo;</li>
            <li><span className="font-medium">Try it</span> — many updates end with a link that takes you straight to the place it describes</li>
            <li><span className="font-medium">For a team</span> — an update about a feature only some teams have says so, for example &ldquo;For Loose Ends users&rdquo;. You only see news about features you can use, and nothing about a feature that is switched off here</li>
            <li><span className="font-medium">Mark all as seen</span> — clears the count, and the drawer then says &ldquo;You&apos;re all caught up.&rdquo; Closing the drawer after reading it does the same</li>
        </ul>
        <p>
            &ldquo;New&rdquo; means published since your last visit, within the last two weeks.
            Cubie remembers what you have seen in this browser, so on another computer the same
            updates show as new once more.
        </p>

        <SubHeading id="the-weekly-tease">Once a week, it opens by itself</SubHeading>
        <p>
            The first time you open Cubie in a week, if there is something new since your last
            visit, the drawer slides open by itself for about five seconds and then closes again.
            It doesn&apos;t cover the page, take the cursor or count as reading anything. Point at
            it, or tab into it, and it waits; click anything inside it and it stays open.
        </p>
        <p>
            It never does this on a live L10 meeting page, on a narrow screen, when you arrived from
            a link to one update, after you have already opened the drawer, while another window is
            open over the page, or while an admin is viewing Cubie as you. If Cubie is open in a
            browser tab you are not looking at, it waits until you come back to it. An admin can
            switch it off for everyone (see For admins, below).
        </p>

        <SubHeading id="the-spotlight-bar">On your Daily Briefing</SubHeading>
        <p>
            If you have the <TopicLink id="daily-briefing">Daily Briefing</TopicLink>, a one-line
            bar under its heading, <span className="font-medium">New since your last visit</span>,
            names the newest update you have not seen and how many more there are.{' '}
            <span className="font-medium">See what&apos;s new</span> opens the drawer; the{' '}
            <span className="font-medium">×</span> hides the bar until the next update arrives. A{' '}
            <span className="font-medium">What&apos;s new</span> button next to Refresh opens the
            drawer too.
        </p>

        <SubHeading id="slack">The morning post in Slack</SubHeading>
        <p>
            When an admin has turned it on, each weekday morning Cubie posts one message in the
            chosen Slack channel listing the updates published since the last post, one line each:
            the title, a sentence or two and a <span className="font-medium">See it</span> link. A
            morning with nothing new posts nothing.
        </p>
        <p>
            The link opens Cubie with the drawer on that update, marked &ldquo;You came here from
            Slack&rdquo;. If you were signed out, you land on it after signing in. If the update is
            about a feature your team does not have, the drawer says &ldquo;This update is for
            another team.&rdquo; instead of showing it.
        </p>

        <Callout tone="info">
            <span className="font-medium">Counts of people, never who.</span> Cubie counts how many
            people opened each update from Slack or with Try it, once per person, and keeps a
            scrambled code for each instead of your name, only so it can count each person once.
            No screen shows who opened what.
        </Callout>

        <SubHeading id="for-admins">For admins</SubHeading>
        <p>
            <span className="font-medium">Admin → What&apos;s New</span>, in the Administration
            section (admins only), holds the two settings that apply to everyone. Nobody here
            writes or approves the updates: they come from RubikSoft.
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Post new updates in Slack each weekday morning</span> — the morning post described above, with its channel (#announcements unless you change it) and a line saying whether Cubie is in it. To turn it on, create the channel in Slack and type <span className="font-medium">/invite @Cubie</span> in it, then Save. Save checks with Slack that the channel exists, is not archived and has Cubie in it, and says exactly what to fix if not</li>
            <li><span className="font-medium">Tease the drawer the first time someone opens Cubie each week</span> — the weekly slide-open described above</li>
        </ul>
        <p>
            The tab shows who last changed it and when, and every save is recorded in the audit
            log. Nothing needs a new release.
        </p>
        <p className="text-sm text-gray-500">
            Open to everyone by default; an admin can change it under Administration → Feature
            Access. An admin viewing Cubie as someone else does not see the button.
        </p>
    </>
);
