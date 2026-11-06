import React from 'react';
import { Callout, SubHeading, TopicLink } from '../kit';

// Help → Insights dashboards. Served at /help/insights; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR. Every rule
// on this page comes from docs/insights/README.md, the feature's contract: when who sees
// what, a filter or a data source changes there, it changes here too.

export const Insights: React.FC = () => (
    <>
        <p>
            Insights is one place to see how Cubie is used and what it produces: who uses it and
            how often, how much people rely on Ask Cubie, what the AI Priorities caught each
            morning and what people did with them, whether loose ends and promises are closing,
            what Cubie wrote into HubSpot, how many proposals and meeting briefs it helped
            produce, what the AI costs, whether the knowledge base is
            growing and being used, and how far people trust what Cubie writes. Every dashboard
            can be filtered by department and by date, and every number explains itself.
        </p>
        <p>
            Open it from <span className="font-medium">Insights</span> in the left rail. The panel
            lists the dashboards under four headings: Company, Work, Sales and Platform.
        </p>

        <SubHeading id="the-dashboards">The dashboards, and what each one answers</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Overview</span> — is Cubie working for the company? The headline number from each dashboard, with a department comparison you can click to filter to one department. Each tile opens the dashboard that owns the number</li>
            <li><span className="font-medium">Adoption &amp; Engagement</span> — who uses Cubie, how often, and which features. Daily, weekly and monthly active people out of everyone who has Cubie, active people by department, which features each department uses out of the ones it has, the busiest days and hours, newly active people, and the people who have Cubie but have not used it in 14 days or more</li>
            <li><span className="font-medium">Ask Cubie</span> — how much people rely on chat and whether it helps: questions asked, conversations, people asking out of those who have chat, how fast answers come back, how often a question fails, and the ratings people give answers. Counts only: no question is ever shown</li>
            <li><span className="font-medium">AI Priorities</span> — what Cubie caught each morning and what people did with it: surfaced, acted on, resolved on their own, dismissed, snoozed and still open, by source and by department, how long it takes to act, and how often a kind of priority is dismissed (a sign it is noise)</li>
            <li><span className="font-medium">Follow-through</span> — are loose ends and promises closing? For Loose Ends: how many were flagged, how many closed because the record shows the work was done, how many were marked &ldquo;not a real miss&rdquo; (the noise rate, which is how the team judges whether the check is accurate), how long closing takes and how old the open ones are. For My To-Dos: promises kept out of those kept or dropped, promises slipping now, and promises turned into EOS To-Dos. Sales Cockpit commitments missed out of those due appear per department only. People are listed A to Z, never ranked</li>
            <li><span className="font-medium">CRM &amp; HubSpot</span> — what Cubie wrote into HubSpot, always after someone approved it: CRM Sync suggestions, call wrap-ups, Cockpit nudges, deal stage corrections and emails logged from Loose Ends. Whether people trust it: CRM Sync suggestions approved out of those decided, by kind of change, how long approval takes, and how often a write to HubSpot failed</li>
            <li><span className="font-medium">Proposals &amp; Prep</span> — whether the work gets produced: proposals started, confirmed, drafted, revised and linked to a deal; Meeting Prep briefs, how often one is edited or rewritten, deep prep and failures; and Customer X-Ray snapshots</li>
            <li><span className="font-medium">AI Spend</span> — what Cubie&apos;s AI costs: the total, per active person, by feature (named as in Admin → AI Cost, with the smaller ones together as &ldquo;Other features&rdquo;), by model and by department, and what one result costs: a daily briefing, an Ask Cubie answer, a proposal drafted. The whole-company view counts every dollar; a department view counts only the spend tied to a person in it, which is complete from the day Insights went live. Costs include the models&apos; thinking (see Admin → AI Cost)</li>
            <li><span className="font-medium">Knowledge &amp; Content</span> — is the knowledge base growing and being used? Documents added, new versions uploaded, meeting transcripts added (department totals only, never per person), knowledge entries Cubie proposed from email and how many were published, and how many Ask Cubie answers cited a document</li>
            <li><span className="font-medium">Quality &amp; Trust</span> — where Cubie&apos;s AI is strongest and weakest. One trust scorecard across Ask Cubie, AI Priorities, Meeting Prep, Proposals, CRM Sync, Loose Ends and Customer X-Ray: how often each was rated useful, answers that failed, cards dismissed, the briefing quality score, meeting briefs rewritten, proposals revised after the first draft, CRM Sync suggestions approved and loose ends dismissed as noise, each with what it is out of and the change from the period before</li>
        </ul>
        <p>All ten are listed in the panel under their headings. For how teams are running EOS, open the EOS Dashboard.</p>

        <SubHeading id="reading-the-numbers">Reading the numbers</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Every percentage shows what it is out of.</span> &ldquo;33%&rdquo; is always printed with its &ldquo;212 of 640&rdquo;, so two rates over different groups of people can never be mistaken for each other</li>
            <li><span className="font-medium">The change chip</span> compares the period you picked with the same number of days just before it (&ldquo;vs previous 30 days&rdquo;). It is green when the number moved the way that is good for it and red when it moved the other way, so a drop in response time is green. Rates move in points: 29% to 33% reads &ldquo;+4 pts&rdquo;</li>
            <li><span className="font-medium">The ⓘ beside a number</span> says what it counts, what it is out of, and which way is better. <span className="font-medium">Definitions</span>, at the top of each dashboard, lists them all</li>
            <li><span className="font-medium">The small line under a number</span> is its trend across the period, the latest day marked in violet</li>
            <li><span className="font-medium">A dash (—)</span> means there was nothing to work it out from, for example a rate when nobody was in the group. It is never the same as zero</li>
            <li><span className="font-medium">View as table</span> on any chart shows the same numbers as a table, and <span className="font-medium">CSV</span> downloads them. <span className="font-medium">Export</span> at the top downloads the headline numbers</li>
            <li><span className="font-medium">&ldquo;Data as of 10:42&rdquo;</span> is when the numbers were worked out. They are kept for up to 10 minutes; the refresh button beside it works them out again now</li>
        </ul>

        <SubHeading id="filters-and-sharing">Filters, and sharing a view</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Departments</span> — pick one or more; none picked means every department you can see. Clicking a department in a chart or a comparison row filters the whole dashboard to it, and the chips under the filters take one back out</li>
            <li><span className="font-medium">Date range</span> — the last 7, 30 or 90 days, quarter to date, year to date, or a custom range of up to 366 days. Days are counted in the company&apos;s business time zone. Up to 31 days the charts show each day; longer ranges show weeks starting on Monday</li>
            <li><span className="font-medium">Your filters follow you.</span> Moving from one dashboard to another keeps the departments and dates you picked, and Insights remembers your last filters for next time</li>
            <li><span className="font-medium">Share a view</span> with <span className="font-medium">Copy link</span>. The link carries the dashboard, the departments and the dates, so a colleague who opens it sees the same view, as far as their own access allows</li>
        </ul>

        <SubHeading id="who-sees-what">Who sees what</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Admins and company-wide executives</span> see every department</li>
            <li><span className="font-medium">A department executive</span> sees the departments they manage, plus their own. Asking for any other department shows a short note instead of numbers, with a button back to your own</li>
            <li><span className="font-medium">Tables that name people</span>, and the person profile below, are a separate permission from the dashboards. Without it you see the department totals only</li>
            <li><span className="font-medium">Never shown, to anyone:</span> the text of any message, email, chat question or meeting transcript (Insights counts, it does not read), including a loose end&apos;s subject or summary and a proposal&apos;s wording; anyone&apos;s Smart Teams results; and per-person numbers about recorded client calls (call wrap-ups written to HubSpot, call scripts, Sales Cockpit commitments), which appear only as department totals, and not at all for a department where fewer than 3 people record calls</li>
        </ul>
        <Callout tone="info">
            <span className="font-medium">Who has it.</span> Insights opens to admins and executives
            first. A Cubie admin can add a department, or the person view, in Admin → Feature
            Access. See <TopicLink id="admins">For admins &amp; execs</TopicLink> for how access works
            across Cubie.
        </Callout>

        <SubHeading id="person-profile">One person&apos;s profile</SubHeading>
        <p>
            With the person view, a name in a people table opens that person&apos;s profile: their
            department and the last day they used Cubie, a 90-day activity calendar (one square per
            day, darker for busier days, with the exact count when you point at a square), their
            headline numbers, a card per dashboard, and the features they can use next to the ones
            they actually used in the period. Each profile you open is recorded in the audit log.
        </p>
        <p>
            A profile is a picture of how Cubie fits someone&apos;s work, not a performance review:
            use it to spot a feature someone has and hasn&apos;t found yet, or a team that needs a
            walkthrough.
        </p>

        <SubHeading id="ai-summary">The AI summary</SubHeading>
        <p>
            <span className="font-medium">Summarize this view</span>, above the numbers, asks Cubie
            for a short read of the dashboard you are looking at: a headline, what moved, and what is
            worth watching. It runs only when you click it. Cubie is given the department-level
            numbers on the page and nothing else (no names), every number it quotes must be one of
            them, and a sentence that quotes a number the page doesn&apos;t show is dropped. It is
            marked as AI-generated: check anything important against the charts. When the AI
            isn&apos;t available, the card says so and the numbers are unaffected.
        </p>

        <SubHeading id="where-the-data-starts">Where the data starts</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Active</span> means at least one recorded use of Cubie that day: signing in, a chat, an AI request made for them, a change to a priority, a meeting brief, a proposal, a CRM Sync decision, a tracked promise and so on. Those records go back a long way, so Adoption has history from the start</li>
            <li><span className="font-medium">Page-by-page use</span> of each part of Cubie, and the <span className="font-medium">full history of what people did with each priority</span> (and the Slack messages that delivered them), are recorded from the day Insights went live. Before that, AI Priorities knows what was surfaced each morning and where each priority stands now, not every step in between</li>
            <li><span className="font-medium">Newer features start later.</span> Loose Ends counts only from the day your company turned it on, and Meeting Prep has recorded edits, rewrites and failures only since its versions arrived. Their numbers before then are empty, not zero, and the change chip is left out where the previous period has no history</li>
            <li><span className="font-medium">AI spend by department or person</span> is complete from the day Insights went live, when the overnight AI Priorities run started recording whose briefing it was. Before that, those figures read low; the whole-company total was always complete</li>
            <li><span className="font-medium">Meeting brief rewrites</span> are known from the day Meeting Prep started keeping every version of a brief; the Quality dashboard's note gives that day</li>
            <li>Each dashboard says in a note at its foot where any of its sources start, so a short history is never mistaken for a quiet period</li>
        </ul>
    </>
);
