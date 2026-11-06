import React from 'react';
import { AskLink, Callout, SubHeading, TopicLink } from '../kit';

// Help → Loose ends (the AE loop-closure scan). Served at /help/loose-ends; Chat cites the
// same id from rubik_core/help/content.py, so a change here travels there in the same PR.
// Every rule on this page comes from docs/loose_ends/README.md, the feature's design
// contract: when a threshold or a privacy rule changes there, it changes here too.

export const LooseEnds: React.FC = () => (
    <>
        <p>
            Every weekday morning Cubie checks each account executive&apos;s email, Slack and
            HubSpot for the client follow-through that slips quietly: a client conversation
            that never made it into HubSpot, a client email still waiting on your answer, and
            something you said you would do that is now late. What it finds is your list of{' '}
            <span className="font-medium">loose ends</span>. The list carries over from one day
            to the next, so each item keeps its real age until it is closed.
        </p>

        <SubHeading id="the-three-kinds">The three kinds, and what clears each one</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Not in HubSpot</span> — you emailed a client (a HubSpot contact, or someone at a company that has a deal in HubSpot) more than one business day ago, or said in Slack that you had a call or meeting with a client on one of your open deals, and nothing has been logged in HubSpot since: no email, call, meeting or note on that contact, their company or any of their deals. It clears when one of those is logged, dated from the day before your email onward, or when you log it from Cubie</li>
            <li><span className="font-medium">Waiting on your reply</span> — a client wrote to you directly (you are in the To line, not only Cc or a mailing list) and you have not answered in two business days. It clears when the thread shows your reply (or a colleague&apos;s reply on the same thread), or when a follow-up is logged in HubSpot on that contact or company after their message. A reply you only saved as a draft does not count</li>
            <li><span className="font-medium">You said you would</span> — a promise Cubie heard you make, in an email, in Slack or in a meeting, is five days past its date (five days after you said it, if you gave no date), or a HubSpot task of yours is five or more days overdue (only tasks that went overdue in the last 30 days; older ones are left alone). Something less than five days overdue is not on the list yet. It clears when the promise is kept (Cubie finds a sign it happened, or you tick it on your briefing&apos;s &ldquo;You said you would&rdquo; card), dropped or handed off, or when the task is completed, deleted or reassigned in HubSpot. When the date is moved later (you snooze the promise or move the task), it leaves the list as rescheduled, which is never counted as done; if the new date also passes, it comes back</li>
        </ul>
        <Callout tone="info">
            <span className="font-medium">You can&apos;t tick an item off here.</span> There is no
            Done button on this list. Cubie checks HubSpot and your Gmail each morning, and a
            loose end clears itself when the record shows it: your reply in the thread, the
            activity in HubSpot, the task marked complete. Nothing closes just because it fell
            out of the dates Cubie looks at.
        </Callout>

        <SubHeading id="where-it-shows-up">Where it shows up</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">My Work → Sales → Loose Ends</span> — your own list, in the three groups above, oldest first, with one real action on every row. Sales leaders also get a <span className="font-medium">Team</span> tab (below); a leader who has only the Team tab, and no list of their own, lands straight on it</li>
            <li><span className="font-medium">My Day</span> — if you have it, a Loose Ends panel under the five usual blocks shows your three oldest. My Day itself is off until your company switches it on (see the <TopicLink id="sales-cockpit">Sales cockpit</TopicLink> page)</li>
            <li><span className="font-medium">Slack</span> — there can be one post a day to #sales, but it is off until a Cubie admin turns it on in Admin → Loose Ends; the admin can change the channel there. When it is on, a day with nothing to report posts nothing, and anyone Cubie could not check that morning is named on a line of their own and is not counted as scanned</li>
            <li><span className="font-medium">The scorecard, when EOS is enabled</span> — the Team tab then shows who needs a weekly &ldquo;Loose ends over 7 days&rdquo; row on the Sales scorecard and lets eligible leaders add missing rows. Cubie fills each row weekly, with a goal of zero. When EOS is off for your workspace, the Scorecard card and setup action are hidden; the Team list and health view stay available. If Cubie could not read someone&apos;s mailbox, or their scan failed, their number is left as it was rather than written as zero</li>
            <li><span className="font-medium">Ask Cubie</span> — ask about your own list, or, with the Team tab, about your team&apos;s</li>
        </ul>
        <ul className="space-y-1.5">
            <li><AskLink q="What loose ends do I have?" /></li>
            <li><AskLink q="Which loose ends on my team are over 7 days?" /></li>
        </ul>

        <SubHeading id="the-buttons">The buttons on each row</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Log it in HubSpot</span> (Not in HubSpot, emails only) — puts your actual email, full text, on the contact, company and deal in HubSpot as an email, the same as HubSpot&apos;s Gmail extension, so the contact&apos;s last-contacted date moves too. First it shows you each email you sent in that thread (who it went to, when, and the start of the text), each with a tick box, and nothing is written until you confirm. Only the emails you sent to that contact start ticked; one you sent to someone else, such as a forward to a partner, is logged only if you tick it, and a forward to a personal address like Gmail is never offered. An email HubSpot already has is shown greyed out and is not added again. A very long email is cut at 65,000 characters, the most HubSpot holds, and one whose formatting is too long for HubSpot is logged as plain text. Attachments are not included. Cubie never writes notes (call notes come from Fathom) and never writes AI text into HubSpot. The row clears once the emails are logged, and clicking it a second time, or trying again after an error, does not log them twice. When Cubie can&apos;t tell which HubSpot contact the email went to, the row opens HubSpot instead so you can log it yourself</li>
            <li><span className="font-medium">Open in HubSpot</span> (Not in HubSpot, from Slack) — a call or meeting you mentioned in Slack has no email to log, so there is no log button: the row opens the company or deal in HubSpot, with the Slack message next to it, so you can log the conversation there</li>
            <li><span className="font-medium">Draft a reply</span> (Waiting on your reply) — Cubie reads the thread in your own mailbox and saves a reply as a Gmail draft in that thread, set up as a reply to the client&apos;s message so it stays in the same conversation on their side too. It never sends anything. The row stays until you actually send a reply</li>
            <li><span className="font-medium">Open</span> (You said you would) — opens the email thread or the HubSpot task. A promise heard in a meeting with no saved record has nothing to open, and the button says so</li>
            <li><span className="font-medium">Not a real miss</span> — for a row that should never have been flagged. Pick a reason (not a client, automated, internal, handled elsewhere, not a promise, or other) and the row leaves your list for good: the same item is never flagged again. You can also mute that sender, or their whole email domain, for you only; your mutes are listed on the page and you can remove one any time</li>
        </ul>
        <p>
            &ldquo;Not a real miss&rdquo; is the only way to take an item off the list by hand,
            and it is counted: the share of flags marked that way is how the team tells whether
            the check is finding real misses or noise.
        </p>
        <Callout tone="warn">
            <span className="font-medium">An admin viewing Cubie as you</span> sees your list
            without email subjects or summaries, and can&apos;t log to HubSpot, draft a reply,
            mark anything not a real miss or remove a mute for you.
        </Callout>

        <SubHeading id="what-the-team-sees">What your team sees, and what it never sees</SubHeading>
        <p>
            The Team tab, the Slack post, the scorecard when EOS is enabled, and a team question in Ask Cubie show
            only <span className="font-medium">who</span>,{' '}
            <span className="font-medium">which kind</span>,{' '}
            <span className="font-medium">which company or contact</span> (the names in
            HubSpot only: a name taken from an email or a meeting is never shown) and{' '}
            <span className="font-medium">how long</span>. They never show an email subject, a
            message preview or an AI summary. Those are shown to you alone.
        </p>
        <p>
            The Team tab lists everyone A to Z, each person&apos;s items oldest first, all in
            the same colour. There is no ranking and no &ldquo;worst&rdquo; list. For each
            person it shows how many are open, by kind and by age (0 to 2 days, 3 to 7, 8 to
            14, 15 or more), how many are 7 days or older, how many closed this week, and
            whether Cubie could look at each of their sources. A health panel covers the loose ends
            flagged in the last 30 days, with each number out of its total, for example &ldquo;3
            of 41 flags were marked not a real miss&rdquo;, plus how many of those have closed
            and why, the typical days to close, and a weekly trend. Rescheduled ones are listed
            but never counted as closed.
        </p>
        <Callout tone="warn">
            <span className="font-medium">Who has it.</span> Loose Ends starts open to
            everyone, once your company switches it on, and everyone who has it gets the morning
            check of their Gmail and Slack. Your admin can narrow it to the team that works client
            threads, or to people by name; admins and the Exec team are then checked only if they
            are on that list. The Team tab is for the managers and leaders added to it. Admins
            change either list from Admin → Feature Access.
        </Callout>

        <SubHeading id="the-time-limits">The time limits</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">1 business day</span> to log an email you sent before it counts as Not in HubSpot</li>
            <li><span className="font-medium">2 business days</span> to answer a client before it counts as Waiting on your reply</li>
            <li><span className="font-medium">5 days</span> past a promise&apos;s date, or <span className="font-medium">5 days</span> after you said it if it had no date; a HubSpot task <span className="font-medium">5 or more days</span> overdue, counting only tasks that went overdue in the last <span className="font-medium">30 days</span>. A task&apos;s age counts calendar days: due on the 8th is 5 days overdue on the 13th, whatever the hour</li>
            <li><span className="font-medium">7 days</span> — the age chip on a row is amber until the item is 7 days old and red from then on, with the number printed beside the colour (&ldquo;4 days&rdquo;). When EOS is enabled, the scorecard row counts the ones at 7 days or more</li>
            <li><span className="font-medium">60 days</span> — anything still open after 60 days drops off as aged out. It is counted separately and never as fixed</li>
            <li><span className="font-medium">Moved dates</span> — a promise or HubSpot task whose date is moved later, so it is no longer five days overdue, leaves as rescheduled, also never counted as fixed</li>
        </ul>
        <p>
            The age counts from when it happened (the email you sent, the message you
            received, the date you promised), not from when Cubie first noticed. A business day
            is counted the way My Day counts it: eight working hours in the company&apos;s
            business time zone, with weekends left out. Holidays and PTO are not subtracted yet.
        </p>

        <SubHeading id="when-cubie-could-not-look">When Cubie could not look</SubHeading>
        <p>
            An empty list isn&apos;t always &ldquo;you&apos;re caught up&rdquo;. When a source
            could not be read, the page says so in its own words, and that source neither adds
            nor clears anything that morning:
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Slack is not connected</span> — your Slack messages were not checked. Connect it in <TopicLink id="settings">Settings</TopicLink> → Slack</li>
            <li><span className="font-medium">Your mailbox could not be read</span> — nothing from email was flagged or cleared that morning</li>
            <li><span className="font-medium">Your mailbox was only partly read</span> — there was more email in the last two weeks than Cubie reads in one morning (300 threads each for the inbox and sent mail), so the oldest threads were not checked</li>
            <li><span className="font-medium">Slack could not be read</span> — for example, the Slack connection is missing permission to search your messages. Reconnect it in <TopicLink id="settings">Settings</TopicLink> → Slack</li>
            <li><span className="font-medium">No HubSpot owner matches your Cubie address</span> — your clients&apos; records are still checked, but your overdue HubSpot tasks are not. An admin can fix the match</li>
            <li><span className="font-medium">HubSpot could not be read</span> — no Not in HubSpot items were flagged and nothing was cleared from HubSpot that morning</li>
            <li><span className="font-medium">HubSpot answered only in part</span> — a very busy week can return more activity than HubSpot hands back at once, or an activity in a shared portal may not be provably yours. Cubie does not flag an email as Not in HubSpot when the proof could be in the part it could not check</li>
            <li><span className="font-medium">The AI check didn&apos;t run</span> — no new Waiting on your reply or Slack items that morning; the ones already on your list are still re-checked</li>
            <li><span className="font-medium">The AI check could not get to everything</span> — more possible loose ends were waiting than it reads in one morning; the rest are checked on the next mornings. Ones it already turned down are not asked about again</li>
            <li><span className="font-medium">Your scan could not finish</span> — nothing was added or cleared that morning; what is on your list is from earlier scans</li>
        </ul>
        <p>
            Every count says what it is out of, and the page says what was checked (&ldquo;checked
            52 email threads, 31 Slack messages&rdquo;) and when.
        </p>

        <SubHeading id="what-cubie-reads">What Cubie reads</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Gmail</span> — the last 14 days of your inbox and sent mail, leaving out chats, promotions, social and forum mail. For each message the morning check reads the sender, the recipients, the date, the subject and Gmail&apos;s short preview line, not the full email or its attachments. Your colleagues, automated and no-reply senders, and anyone you muted are left out. Only when you click Log it in HubSpot does Cubie read the full text of the emails you sent in that one thread, from your own mailbox, to show you what will be logged</li>
            <li><span className="font-medium">Slack</span> — your own messages from the last 7 days, through the Slack connection in Settings</li>
            <li><span className="font-medium">HubSpot</span> — contacts, companies, deals, the emails, calls, meetings and notes logged on them, and your open tasks. It only writes to HubSpot when you click Log it in HubSpot, and then only the emails you sent, exactly as you sent them</li>
            <li><span className="font-medium">One AI check per person each morning</span> decides which unanswered emails really expect a reply and which Slack messages describe a call or meeting with a client. Its one-line summary of a row is shown to you only</li>
        </ul>
        <p>
            The Sales <TopicLink id="sales-rhythm">daily rhythm</TopicLink> still answers
            &ldquo;what haven&apos;t I answered today?&rdquo; in the moment. Loose Ends is the
            record that keeps what stayed open, day after day, until it is closed.
        </p>

        <SubHeading id="what-it-cant-measure">What it can&apos;t measure</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">How many real misses it catches.</span> That needs a hand check: pick one account executive, go through one day of their email and HubSpot by hand, and compare it with what Cubie flagged</li>
            <li><span className="font-medium">Time saved.</span> That is an estimate for the team to make, not something Cubie counts</li>
            <li><span className="font-medium">A conversation that left no trace</span> in your email, Slack or HubSpot. A phone call nobody logged is invisible to it</li>
        </ul>
        <p>
            What it does measure is noise: how many of its flags were marked &ldquo;Not a real
            miss&rdquo;, out of how many it raised. If that runs high in the first weeks, the
            two-business-day reply window is the first thing to change.
        </p>
    </>
);
