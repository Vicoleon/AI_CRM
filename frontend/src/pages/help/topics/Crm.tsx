import React from 'react';
import { AlertCircle, CheckCircle2 } from 'lucide-react';
import { Callout, NotYetRow, QCard, SubHeading } from '../kit';

// Help → Ask Cubie about CRM. Served at /help/crm; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

const CRM_LIVE: { metric: string; qs: string[] }[] = [
    { metric: 'Deals won', qs: ['How many deals did we close this month?', 'Summary of deals won last quarter'] },
    { metric: 'New deals created', qs: ['How many new deals were created this week?'] },
    { metric: 'Win rate', qs: ["What's our win rate this quarter?"] },
    { metric: 'Average deal value', qs: ["What's the average deal size this year?"] },
    { metric: 'Sales-cycle length', qs: ['How long are our deals taking to close?'] },
    { metric: 'Open pipeline value', qs: ["What's our total open pipeline worth?", 'How much does Jordan have open?'] },
    { metric: 'Meetings booked & held', qs: ['How many meetings did we book and complete this week?'] },
    { metric: 'New contacts', qs: ['How many new contacts did we add this month?'] },
    { metric: 'Activities logged', qs: ['How many calls and emails were logged last week?'] },
    { metric: 'Turn times by product', qs: ['What are the turn times by product?'] },
    { metric: 'Deals expected to close (forecast)', qs: ['What deals are expected to close this month for Maya?', "What's closing next quarter?", "What's the expected-to-close rate on our open deals?"] },
    { metric: 'Last touch with a client, contact, or deal', qs: ['When was the last touch with Northwind Traders?', 'How long since anyone contacted Acme Corp?', "When did we last reach out on the Globex renewal?"] },
    { metric: 'Deals going cold (outreach recency)', qs: ["Of the deals closing this month, which haven't I contacted in 10 days?", 'Which of my open deals are going cold?', "Which won clients haven't I checked in with since closing?"] },
    { metric: 'A specific deal’s stage, amount, or owner', qs: ['What stage is the Globex renewal in?', 'Who owns the Northwind support contract deal?', "What's the amount on the Initech onboarding deal?"] },
    { metric: 'Deal lists', qs: ['Show me my open deals', 'List the deals we lost last month'] },
    { metric: 'Find a contact', qs: ["What's Sam Rivera's email?", 'Find the contact record for Alex Morgan'] },
];

const CRM_NOT_YET: { name: string; why: string }[] = [
    { name: 'ARR / recurring revenue', why: 'Needs MRR & contract-term fields populated on deals in HubSpot.' },
    { name: 'Churn rate', why: 'Needs a churn date + active/churned status field on companies.' },
    { name: 'Upsell / cross-sell rate', why: 'Needs an Upsell/Cross-Sell flag on deals.' },
    { name: 'Revenue per client', why: 'Coming — needs the deal-to-company link wired up for per-client totals.' },
    { name: 'Quotes / proposals sent', why: "HubSpot Quotes aren't used; needs a “proposal sent” stage chosen to count." },
    { name: 'Stage conversion rate', why: 'Needs the two stages to compare (e.g. Legal Review → SOW Sent) defined.' },
    { name: 'Lead response time', why: 'Needs first-activity tracking — better as a scheduled report than a live answer.' },
    { name: 'Forecasting score', why: 'No forecast field exists yet — ask “expected to close” instead.' },
    { name: 'Follow-up ratio', why: 'Needs its definition pinned down (contacts vs. closes — which counts?).' },
];

export const Crm: React.FC = () => (
    <>
        <p>
            Cubie reads your <span className="font-medium">live HubSpot pipeline</span>, so these
            answers are current — not a stale copy. Phrase questions however feels natural.
        </p>
        <p>
            Your workspace admin must connect HubSpot first. If that connection is removed,
            CRM answers pause until it is restored.
        </p>
        <p>
            Cubie reads deals only in the pipelines platform support assigned to this workspace.
            Company and contact answers require a link to one of those deals. A prospect
            with no linked deal is unavailable here until your team links it in HubSpot.
        </p>
        <div className="flex flex-wrap gap-2 my-2">
            <span className="text-xs bg-gray-100 border border-gray-200 rounded-full px-3 py-1 text-gray-700">
                Add a timeframe — <span className="text-brand font-medium">this week, this month, last quarter, this year</span>
            </span>
            <span className="text-xs bg-gray-100 border border-gray-200 rounded-full px-3 py-1 text-gray-700">
                Scope to a rep — <span className="text-brand font-medium">“…for Maya”, “…by Jordan”</span>
            </span>
            <span className="text-xs bg-gray-100 border border-gray-200 rounded-full px-3 py-1 text-gray-700">
                No timeframe = <span className="text-brand font-medium">all-time</span>
            </span>
            <span className="text-xs bg-gray-100 border border-gray-200 rounded-full px-3 py-1 text-gray-700">
                <span className="text-brand font-medium">Click any question below</span> to ask it in Chat
            </span>
        </div>

        <SubHeading id="live-now">
            <span className="inline-flex items-center gap-2">
                <CheckCircle2 size={16} className="text-green-700" />
                Ask away — live now
            </span>
        </SubHeading>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {CRM_LIVE.map((c) => (
                <QCard key={c.metric} metric={c.metric} qs={c.qs} />
            ))}
        </div>

        <SubHeading id="not-yet">
            <span className="inline-flex items-center gap-2">
                <AlertCircle size={16} className="text-yellow-700" />
                Not yet — coming once HubSpot is set up
            </span>
        </SubHeading>
        <p className="text-gray-500">Cubie will tell you it can&apos;t answer these yet rather than guess a number.</p>
        <div className="mt-1">
            {CRM_NOT_YET.map((r) => (
                <NotYetRow key={r.name} name={r.name} why={r.why} />
            ))}
        </div>
        <SubHeading id="how-cubie-reads-hubspot">How Cubie reads HubSpot</SubHeading>
        <p>
            <span className="font-medium">How long since a deal&apos;s last contact</span> reads the
            deal and its client. HubSpot only puts an email on a deal when the person you wrote to
            is on that deal; email someone else at the client and it is filed on the client&apos;s
            company instead. So when the company heard from us more recently than the deal shows,
            the answer gives both dates rather than calling the deal cold.
        </p>
        <p>
            <span className="font-medium">Turn times by product</span> read two deal fields your
            company created in HubSpot: how long a deal took to turn around, and what the deal was
            for. An admin tells Cubie once which fields those are; until then Cubie says turn
            times aren&apos;t set up rather than guessing.
        </p>
        <Callout tone="info">
            Cubie covers your <span className="font-medium">CRM</span> — deals, contacts, companies,
            meetings and activities in HubSpot — not invoicing or accounting. If a number looks off,
            it&apos;s reading HubSpot live, so it usually means the deal data in HubSpot needs a
            tidy-up.
        </Callout>
    </>
);
