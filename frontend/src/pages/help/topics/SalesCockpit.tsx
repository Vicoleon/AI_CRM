import React from 'react';
import { AskLink, Callout, SubHeading, TopicLink } from '../kit';

// Help → Sales cockpit (My Day). Served at /help/sales-cockpit; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const SalesCockpit: React.FC = () => (
    <>
        <Callout tone="warn">
            <span className="font-medium">Not on for you? Ask your admin.</span> The cockpit
            has to be switched on for your workspace. Once it is, My Day is open to everyone;
            the team roll-up and the nudge worklist go to the teams your admin adds, and the
            exec roll-up to the Exec team. If{' '}
            <span className="font-medium">My Work → Sales → My Day</span> isn&apos;t on your
            rail, it isn&apos;t on for you and no My Day is being built for you. What follows
            is what it does.
        </Callout>
        <p>
            <span className="font-medium">My Work → Sales → My Day</span> is
            your sales morning in the order you already work it, and the order is the
            feature — nothing is sorted by urgency across the page. Five blocks, always the
            same five:
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Wrap-ups waiting</span> — calls to close out, first because they decay. A failed AI analysis stays here with a retry status, even after a call was wrapped; open the wrap-up after the admin fixes the provider connection. A wrapped call also returns for later suggestions or an earlier HubSpot write that still needs a retry. Its original completion time stays intact</li>
            <li><span className="font-medium">Leads</span> — waiting on a first touch</li>
            <li><span className="font-medium">Pipeline hygiene</span> — where HubSpot&apos;s deal stage disagrees with what the call said. Your admin lists the workspace&apos;s pipeline stages in order under Admin → Access → Google Workspace → HubSpot settings; until then this block stays empty</li>
            <li><span className="font-medium">Deals</span> — the ones running against a clock</li>
            <li><span className="font-medium">No contact in 90 days</span> — quiet accounts, oldest first. A deal whose client heard from us inside the 90 days (an email to someone not on the deal is filed on the client&apos;s company) still shows, titled &ldquo;No contact logged on this deal&rdquo;, after the deals whose whole client has gone quiet: logging that email on the deal clears it</li>
        </ul>
        <SubHeading id="three-at-a-time">Three at a time, on purpose</SubHeading>
        <p>
            Each block shows at most three items and the rest appear as{' '}
            <span className="font-medium">&ldquo;+4 more&rdquo;</span>, which{' '}
            <span className="font-medium">links</span> to the full list for that block instead
            of expanding it. A list of twenty is a list of zero, so the remainder surfaces as
            you clear what&apos;s in front of you.
        </p>
        <Callout tone="info">
            <span className="font-medium">An empty block isn&apos;t always &ldquo;you&apos;re
            caught up&rdquo;.</span> When Cubie couldn&apos;t look — call capture switched
            off, HubSpot unreadable, or no HubSpot owner matching your Cubie address — the
            block says so in its own words instead of showing a clean slate. If it says
            nothing, the block really is empty.
        </Callout>
        <SubHeading id="age-chip">The age chip</SubHeading>
        <p>
            Any row with a clock carries a small chip, and the number is always printed next to
            the colour — so a screenshot pasted into Slack still says what it says.
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Grey</span> — open, still inside its time</li>
            <li><span className="font-medium">Amber</span> — at risk, roughly the last third of the clock</li>
            <li><span className="font-medium">Red</span> — breached, and it says how far over (&ldquo;9d · 2d over&rdquo;)</li>
            <li><span className="font-medium">Red with a person&apos;s name</span> — escalated: it&apos;s now on somebody else&apos;s plate, and naming them is the information</li>
            <li>A plain chip that only counts days since the call claims <span className="font-medium">no</span> lateness — that row has no agreed clock</li>
            <li><span className="font-medium">&ldquo;Age unknown&rdquo;</span> means there&apos;s no usable start date on the record. Deliberately not shown as &ldquo;0d&rdquo;, because zero would hide exactly the stall worth catching</li>
        </ul>
        <SubHeading id="business-hours">The clock is in business hours</SubHeading>
        <p>
            A business day is eight hours in the company&apos;s business time zone and weekends
            don&apos;t count, so a Friday-afternoon request isn&apos;t late on Monday morning.
            Holidays and PTO are not subtracted yet — a stated limitation, not a bug.
        </p>
        <Callout tone="warn">
            <span className="font-medium">The SLA numbers are placeholders right now.</span>{' '}
            They shipped provisional — 3 business days for a standard SOW, 10 for a
            non-standard one, 1 business day to finish a wrap-up, 2 business days for a
            lead&apos;s first touch. Nobody has agreed them yet (an admin confirms them), so
            the app marks a placeholder threshold with a dotted underline that says{' '}
            <span className="font-medium">&ldquo;Placeholder — not yet confirmed by an
            admin.&rdquo;</span> when you hover it. Read a breach against a placeholder as a
            prompt, not a verdict.
        </Callout>
        <SubHeading id="setting-an-eta">Setting an ETA</SubHeading>
        <p>
            On a deal row, <span className="font-medium">Set expected date</span> takes a date plus a short
            reason. A date <span className="font-medium">later</span> than the standard SLA
            moves the clock to your date, so a three-week RFP stops reading as late on day
            four — that&apos;s the main reason to use it. A date{' '}
            <span className="font-medium">earlier</span> than the SLA never shortens the
            clock, so promising a client something tight can&apos;t create an internal breach
            against you. Your reason is stored with the date.
        </p>
        <SubHeading id="accepting-a-breach">Accepting a breach</SubHeading>
        <p>
            Where something is genuinely late and that&apos;s genuinely fine, accept it and write
            why. The reason is required and it&apos;s kept — it&apos;s the audit trail that&apos;s
            kept by hand today, and it stops the row being the loudest thing on your
            manager&apos;s screen every fifteen minutes.
        </p>
        <SubHeading id="deferring-a-deal">Deferring a deal</SubHeading>
        <p>
            A long-term deal with no real close date can be set aside instead of showing up every
            morning. Press <span className="font-medium">Defer</span> on its row — in Pipeline
            hygiene, Deals, No contact in 90 days or the Daily rhythm checks — or{' '}
            <span className="font-medium">Defer the deal…</span> on its card in the Daily
            Briefing. Pick a <span className="font-medium">follow-up date</span> (after today, at
            most 180 days out) and write a <span className="font-medium">note</span> saying why;
            both are required.
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li>Until that date the deal is off your My Day, your Daily rhythm checks, your Daily Briefing cards and the morning Slack message about them.</li>
            <li>On the date it comes back by itself — nothing to remember.</li>
            <li>Cubie adds one follow-up <span className="font-medium">task</span> on the deal in HubSpot, due that morning, with your note as the task. The deal&apos;s stage and close date are not touched.</li>
            <li><span className="font-medium">Deferred deals</span>, at the bottom of My Day, lists what you have set aside. <span className="font-medium">Undefer</span> brings one back early (its HubSpot task stays; close it there if you no longer need it). If the HubSpot task could not be created, the row says why and offers a retry.</li>
        </ul>
        <Callout tone="info">
            <span className="font-medium">Your manager still sees every deferred deal.</span>{' '}
            The Team Roll-up and the weekly close-date roll-up list each one with its owner, the
            follow-up date and your note, and its late commitments still count there. Write the
            note for them as much as for yourself.
        </Callout>
        <SubHeading id="what-a-nudge-does">What a nudge does</SubHeading>
        <p>
            On the <span className="font-medium">Nudge Worklist</span>,{' '}
            <span className="font-medium">Nudge</span> does three things and Cubie tells you
            which of them landed: it creates a <span className="font-medium">HubSpot task</span>{' '}
            for the owner, sends them a <span className="font-medium">Slack DM</span>, and{' '}
            <span className="font-medium">logs</span> the nudge itself. If the owner
            isn&apos;t in the Slack workspace the DM is skipped and Cubie says so rather than
            claiming it sent one. Two nudges with no answer trigger an{' '}
            <span className="font-medium">escalation</span> rather than a third nudge —
            chasing louder isn&apos;t the answer, changing who is accountable is.
        </p>
        <SubHeading id="the-other-three-views">The other three views</SubHeading>
        <p>
            They appear under <span className="font-medium">My Work → Sales</span> beside My Day,
            but only for the people they belong to: your admin adds the team each one is for,
            and the exec roll-up goes to the Exec team.
        </p>
        <p>
            <span className="font-medium">Team Roll-up</span> is a manager&apos;s view: who is
            outstanding, the reasons people gave, and every deal a rep deferred, with the
            date and the note.{' '}
            <span className="font-medium">Nudge Worklist</span> is the delivery team&apos;s view and is
            metadata only — what&apos;s outstanding and who owns it, never the text of a call.{' '}
            <span className="font-medium">Exec Roll-up</span> is org-level counts and a trend
            with no per-person drill-down, deliberately, so it can&apos;t turn into a
            leaderboard.
        </p>
        <Callout tone="warn">
            <span className="font-medium">Coming soon</span> —
            these stay off even where the cockpit is on: the daily Slack digest and the
            Monday aging roll-up (off on purpose, so the cockpit is read on screen for a while
            before it starts messaging anyone); and filtering leads by temperature.{' '}
            <span className="font-medium">Nothing from the cockpit arrives in Slack today.</span>{' '}
            Loose Ends is separate: it has its own panel in My Day, and its once-a-day post to
            #sales stays off until a Cubie admin turns it on (see{' '}
            <TopicLink id="loose-ends">Loose ends</TopicLink>).
        </Callout>
        <ul className="space-y-1.5">
            <li><AskLink q="What does an amber age chip mean in My Day?" /></li>
            <li><AskLink q="How do I set an ETA so a long RFP is not late?" /></li>
            <li><AskLink q="How do I defer a deal until a follow-up date?" /></li>
        </ul>
    </>
);
