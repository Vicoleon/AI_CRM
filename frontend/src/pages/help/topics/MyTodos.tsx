import React from 'react';
import { Link } from 'react-router-dom';
import { SubHeading } from '../kit';

// Help → My To-Dos. Served at /help/my-todos; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const MyTodos: React.FC = () => (
    <>
        <p>
            <span className="font-medium">My To-Dos</span> (My Work → My To-Dos) is your own list of
            what you have said you would do, <span className="font-medium">for whom</span>, and
            <span className="font-medium"> by when</span>. It is different from a team&apos;s EOS
            To-Dos: those are team work reviewed in an L10; this is your personal ledger, and only you
            can change it. It is not hidden from your manager, though: see Who else sees it below.
        </p>
        <SubHeading id="adding-one">Adding one</SubHeading>
        <p>Four ways, all landing on the same list:</p>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Type it</span> in plain words at the top of the page — <em>&ldquo;send Sam the revised SOW Thursday&rdquo;</em>. Cubie reads out the task, who is waiting and the date; you check it and confirm.</li>
            <li><span className="font-medium">Chat</span> — say <em>&ldquo;remind me to…&rdquo;</em> or <em>&ldquo;add a to-do…&rdquo;</em>. Cubie repeats back the date it understood so a misheard day gets fixed on the spot.</li>
            <li><span className="font-medium">Slack</span> — <span className="font-medium">/todo</span> now asks <span className="font-medium">Where</span>: <span className="font-medium">My list</span> (the default) or <span className="font-medium">Team L10</span>, which files an EOS To-Do exactly as before.</li>
            <li><span className="font-medium">A Daily Briefing card</span> — open <span className="font-medium">More actions</span> and choose <span className="font-medium">Add to my To-Dos</span> to turn Cubie&apos;s suggestion into a commitment you own.</li>
        </ul>
        <SubHeading id="using-your-list">Using your list</SubHeading>
        <p>
            <span className="font-medium">The list is grouped by who is waiting on you</span>, with
            your own items under <span className="font-medium">Just you</span>. Each card has a
            next-step button that does the first part of the work — draft the email, open the
            document or deal, prep for the meeting, or block 30 minutes on your calendar — plus
            <span className="font-medium"> Kept</span> when it is done and <span className="font-medium">Snooze</span> (tomorrow,
            3 days, next week, or a date you pick). The <span className="font-medium">…</span> menu holds
            <span className="font-medium"> Change the date</span>, <span className="font-medium">Make private</span>,
            <span className="font-medium"> Make it a team To-Do</span> (which files it into your team&apos;s EOS
            list as well) and <span className="font-medium">Drop</span>.
        </p>
        <p>
            An item whose date has passed is marked <span className="font-medium">slipping</span> so it never
            sits quietly overdue. Open items that are overdue, due within the next 7 days, or have no date
            are also sent to your <Link to="/" className="text-brand underline">Daily Briefing</Link> as
            <span className="font-medium"> Your To-Do</span> cards, at most 12 at a time with the overdue ones
            first, and Cubie weighs them against everything else in your day. An item due later than that
            waits until it is a week out. Ticking the card and marking the item kept are the same action.
        </p>
        <p>
            Also on the page: your open <span className="font-medium">team To-Dos from EOS</span> and, for
            account executives, your open <span className="font-medium">Sales Cockpit commitments</span> — read
            from those systems, not copied, with their own links. The footer keeps this week&apos;s tally:
            how many you kept and dropped since Monday, and how many are open now (and how many of those
            are slipping).
        </p>
        <SubHeading id="cubie-heard-you-promise">Cubie heard you promise…</SubHeading>
        <p>
            Promises Cubie hears you make in meetings, L10s, sent email and Slack now land in a tray at
            the top of the page, each with your exact words and a link to where you said them. Accept
            one to add it to your list, or reject it if it wasn&apos;t really a promise (it won&apos;t be
            suggested again).
        </p>
        <p>
            Once its date passes, Cubie checks your sent email, Slack, Drive and EOS To-Dos; if it finds
            the email or file that keeps the promise, the item moves to{' '}
            <span className="font-medium">Looks done, confirm?</span> — Cubie never marks anything kept
            on its own.
        </p>
        <SubHeading id="who-else-sees-it">Who else sees it</SubHeading>
        <p>
            Only you can change your list, but it is not hidden from your manager. A manager can ask
            Chat about the promises of the people who report to them and the teams they lead, including
            what is on My To-Dos and what Cubie heard them promise; executives who oversee the whole
            company can ask about every team. To keep one item out of that, open its{' '}
            <span className="font-medium">…</span> menu and choose{' '}
            <span className="font-medium">Make private</span>. A private item also stays out of your
            Meeting Prep briefs, because a brief can be saved to Assets.
        </p>
        <p>
            Separately, <span className="font-medium">Insights → Follow-through</span> counts how many
            To-Dos each person kept or dropped, for the people allowed to open Insights. It shows counts
            only, never what a To-Do says, and a private item is counted like any other.
        </p>
    </>
);
