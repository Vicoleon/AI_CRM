import React from 'react';
import { AskLink, Callout, SubHeading, TopicLink } from '../kit';

// Help → Sales daily rhythm. Served at /help/sales-rhythm; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const SalesRhythm: React.FC = () => (
    <>
        <p>
            A daily routine for sellers — a morning, a mid-day and an end-of-day checklist —
            built into Cubie. Each step is a question Cubie answers from HubSpot
            (or your email, Slack and calendar) about <span className="font-medium">your
            own</span> deals, so the routine takes a click per step instead of a search
            per step.
        </p>

        <SubHeading id="where-you-find-it">Where you find it</SubHeading>
        <p>
            In <span className="font-medium">Ask Cubie</span>, a new chat opens with a{' '}
            <span className="font-medium">Your sales rhythm</span> row for the time of day
            (before 11:00 is the morning, before 15:00 mid-day, then the end of the day) —
            switch between the three with the tabs beside it. If you have{' '}
            <span className="font-medium">My Day</span>, the same checks also appear in its{' '}
            <span className="font-medium">Daily rhythm</span> section, below the five usual
            blocks, as short lists with the next step on every row.
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Morning</span> — messages you have not answered, follow-ups you promised, prep for today&apos;s external meetings, and the deals closing this month that need you today</li>
            <li><span className="font-medium">Mid-day</span> — proposals waiting on the client, close dates that keep slipping, big deals hanging on one contact, and lost deals worth a check-in</li>
            <li><span className="font-medium">End of day</span> — records you touched today with a missing field, overdue tasks and past-due close dates, and a drafted recap of your day for your manager</li>
        </ul>

        <SubHeading id="how-each-check-decides">How each check decides</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Needs action this month</span> — closing this month with no client contact in a week (logged, emailed or met), no next step booked (an upcoming meeting with the client counts), a close date that has already moved, or one that has passed</li>
            <li><span className="font-medium">Waiting on the client</span> — in a proposal, SOW, legal-review or contract stage, your last email or logged contact is three or more business days old, and the decision-maker has not replied since — no email back, no logged reply, no meeting. The decision-maker is whoever HubSpot marks with the <span className="font-medium">Decision Maker</span> buying role; when nobody is marked, the row says so. Your admin can name the late-stage IDs or labels under Admin → Access → Google Workspace → HubSpot settings; until then, Cubie goes by the stage names</li>
            <li><span className="font-medium">Slipping</span> — the close date has been moved later three or more times, or there has been no client contact in seven days (logged, emailed or met)</li>
            <li><span className="font-medium">Single-threaded</span> — open deals of $30,000 or more with one contact or none</li>
            <li><span className="font-medium">Worth a check-in</span> — lost 12 to 24 months ago over budget or timing (the Pricing loss reason, or a note like &ldquo;they will pass this year&rdquo;), where the contact&apos;s email has not bounced and they are still linked to the company. HubSpot cannot confirm someone still works there, so glance at LinkedIn before you write</li>
            <li><span className="font-medium">Missing fields</span> — a contact with no job title or buying role, or a deal with no next step booked (a meeting on your calendar with the client counts), no amount or no close date</li>
        </ul>

        <SubHeading id="contact-logged-on-the-client">Contact logged on the client, not the deal</SubHeading>
        <p>
            A deal&apos;s own HubSpot record only shows contact attached to that deal. Email
            someone at the client who isn&apos;t on the deal and HubSpot files it on the
            client&apos;s company instead.
        </p>
        <p>
            Cubie checks that company record too: when the client heard from us more
            recently than the deal shows, the row gives both (&ldquo;no logged client
            contact in 13 days, none on this deal itself in 82 days&rdquo;), and deals whose
            whole client has gone quiet are listed first. The company&apos;s date may be
            about a different deal with the same client, so the deal&apos;s own date is
            always shown too. To keep the deal current, add the person you emailed to the
            deal in HubSpot.
        </p>
        <Callout tone="info">
            <span className="font-medium">Two fields Cubie reads its own way.</span> Cubie reads
            a contact&apos;s <span className="font-medium">Buying role</span> in place of
            HubSpot&apos;s Persona field: Buying role comes with the same options in every
            HubSpot account, while each company sets up its Personas differently, or not at
            all. And HubSpot has no &ldquo;Next steps&rdquo; field, so a{' '}
            <span className="font-medium">booked next activity</span> is what counts as a
            deal&apos;s next step.
        </Callout>

        <SubHeading id="every-row-has-one-next-step">Every row has one next step</SubHeading>
        <p>
            Either it opens the record in HubSpot, where you book the step or fill the
            field, or it opens Ask Cubie with a draft request ready — a follow-up or a
            check-in email, saved as a <span className="font-medium">Gmail draft</span>.
            Nothing is sent for you. Like the rest of My Day, each list shows three rows;
            &ldquo;+4 more&rdquo; opens Ask Cubie with the whole list, and every count says
            what it is out of (&ldquo;2 of 14 open deals&rdquo;).
        </p>
        <p>
            A deal row also has <span className="font-medium">Defer</span>, for a long-term deal
            you want out of the way until a follow-up date. A deal you deferred is left out of
            every check, here and in Ask Cubie, until that date, and the list says how many it
            left out. See{' '}
            <TopicLink id="sales-cockpit">Deferring a deal</TopicLink>.
        </p>

        <SubHeading id="hubspot-plus-your-own-email-and-calendar">HubSpot, plus your own email and calendar</SubHeading>
        <p>
            For your own deals Cubie also checks your Gmail and your calendar, read-only: an
            email you sent or received from the client, or a meeting with them, counts as
            contact even if you never logged it, and a meeting on your calendar counts as a
            booked next step.
        </p>
        <p>
            From your email it reads only dates, plus — for the end-of-day recap — the
            recipients and subject of each email you sent that day; from your calendar,
            meeting times, titles and who was invited.
        </p>
        <p>
            Drafts never count as sent, out-of-office replies and newsletters never count as
            the client answering, and a meeting the client declined is not contact. A phone
            call only counts if it is logged in HubSpot or on your calendar.
        </p>
        <p>
            Counting an unlogged email as contact keeps a deal from looking quiet here, but it
            doesn&apos;t put the email in HubSpot. Emails and client conversations from past
            days that are still not logged in HubSpot are on your{' '}
            <TopicLink id="loose-ends">Loose ends</TopicLink> list, which keeps each one until it
            is logged.
        </p>

        <SubHeading id="the-team-view">The team view</SubHeading>
        <p>
            Sales leaders an admin adds by name get the team version of the prompts
            instead: the team&apos;s top 10 deals closing this month that need action, the
            records the team touched today with a missing field, and the same mid-day checks
            across everyone&apos;s deals, with each deal&apos;s owner named. It lists deals,
            not people — there are no per-rep counts or rankings, and colleagues&apos;
            overdue tasks and activity logs are left out.
        </p>
        <p>
            It also <span className="font-medium">never reads anyone&apos;s email or
            calendar</span>: it counts only what is logged in HubSpot, on the deal and on the
            client&apos;s company record, and every answer says so, because a deal a rep has
            been emailing without logging it anywhere can look quiet there.
        </p>
        <Callout tone="warn">
            <span className="font-medium">Who has it.</span> The Ask Cubie prompts are on for
            everyone. The My Day section also needs My Day itself, which has to be switched
            on for your workspace. Admins can narrow either from Admin → Feature Access.
        </Callout>

        <ul className="space-y-1.5">
            <li><AskLink q="Which 3 deals closing this month need action from me today to keep them from slipping?" /></li>
            <li><AskLink q="List my active deals over $30k that only have one contact." /></li>
        </ul>
    </>
);
