import React from 'react';
import { Callout, SubHeading, TopicLink } from '../kit';

// Help → Daily Briefing (your home page). Served at /help/daily-briefing; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const DailyBriefing: React.FC = () => (
    <>
        <p>
            When you open Cubie you land on your <span className="font-medium">Daily Briefing</span>.
            It greets you by name and leads with <span className="font-medium">AI Priorities</span> —
            a short, auto-generated list of what needs your attention today, pulled across your
            email, calendar, meeting transcripts, HubSpot, Slack and recently-updated Google
            Drive documents you&apos;ve labeled as important.
        </p>
        <Callout tone="info">
            If automatic processing is paused for your workspace, AI Priorities says so.
            Daily and weekly priorities and scheduled summaries stay paused, and Refresh
            and &ldquo;Get me other important things&rdquo; cannot start a new run.
            You can still open Productivity to read your email and calendar.
        </Callout>
        <p>
            Before showing a saved briefing, Cubie checks every HubSpot deal that was used to
            build it against your workspace&apos;s current access. If any deal loses access, the
            saved briefing stays hidden. Older briefings without enough source information stay
            hidden until a fresh one is built.
        </p>
        <p>
            When automatic processing is enabled, your briefing is generated overnight so
            it&apos;s ready when you arrive. If the
            overnight run hits a snag for your account, Cubie retries automatically and, failing
            that, rebuilds the list the moment you open this page — a quiet day may show fewer
            items, but you&apos;ll never be handed an empty briefing while there was real work to
            surface.
        </p>
        <p>
            If your admin allows additional Gmail accounts and you connect one in Settings,
            its messages are included automatically in your own AI Priorities and scheduled
            summaries. Email cards show the mailbox address so you know which account they came
            from. Personal mail stays out of team and manager views.
        </p>
        <SubHeading id="where-your-priorities-come-from">Where your priorities come from</SubHeading>
        <p>
            <span className="font-medium">Labeling a Drive document:</span> when you mark a Google
            Drive file with a label your admin has flagged as important (e.g.{' '}
            <span className="font-medium">Important</span>, <span className="font-medium">Cubie</span>,
            or <span className="font-medium">Project</span>) and then update it, Cubie surfaces it on
            your home page so the active, high-priority work doesn&apos;t get lost. Cubie reads what&apos;s
            actually inside the flagged file — not just its name — so the reason it surfaces reflects the
            document&apos;s real content (it understands Docs, Sheets, Slides, PDFs and Office files). Only
            recently-touched labeled files appear — old labeled files won&apos;t clutter your list. Admins
            manage which labels count, and how strongly, under Administration → Drive Labels.
        </p>
        <p>
            <span className="font-medium">Your Traction world, too:</span> AI Priorities now
            surface your EOS commitments alongside your email and calendar — your{' '}
            <span className="font-medium">Rocks</span>, <span className="font-medium">To-Dos</span>,{' '}
            <span className="font-medium">Issues (IDS)</span> and{' '}
            <span className="font-medium">Rock milestones</span> — so nothing from your quarterly
            plan slips through the cracks. If you manage a team, or you&apos;re on the leadership
            team, your Priorities also flag your team members&apos; at-risk items — for example, a
            direct report&apos;s to-do that&apos;s about to come due — so you can step in before it
            turns into a fire.
        </p>
        <SubHeading id="how-the-cards-work">How the cards work</SubHeading>
        <p>
            <span className="font-medium">It stays honest:</span> when you finish something — mark
            a to-do done, solve an issue, complete a milestone, or finish a Rock — it clears from
            your Priorities automatically, and from your manager&apos;s too, so the list always
            reflects reality.
        </p>
        <p>
            And from a team member&apos;s item you can send them a quick{' '}
            <span className="font-medium">Slack follow-up</span> right from the card — Cubie drafts
            the nudge and you preview and edit it before it sends. If they have several items
            running late, one nudge covers all of them, and once it&apos;s sent the card steps
            aside for three days so you&apos;re not chasing the same person every morning. It
            comes back if something new of theirs falls behind.
        </p>
        <p>Each priority card shows an urgency badge, why it matters, a suggested next step, and badges for where it came from. On any card you can:</p>
        <ul className="list-disc pl-5 space-y-1">
            <li>Mark it done (✓) or dismiss it (✗). For work that <span className="font-medium">finishes</span> — a to-do, a rock, an email you answered — done is permanent: it won&apos;t come back on its own, so use ✗ or snooze for &ldquo;not right now&rdquo;. For a situation that can come <span className="font-medium">back</span> — a deal whose close date has stopped looking realistic — done means &ldquo;handled for now&rdquo; and Cubie re-checks in a couple of weeks, because the same deal can drift again in November after you fixed it in September. Everything you mark done, dismiss or snooze is listed under <span className="font-medium">Settings → AI Personalization → Handled priorities</span>, where you can reopen it any time.</li>
            <li>A dismissed card stays away until something new <span className="font-medium">actually happens</span> on what it cites: a new reply on the email thread, a new message, a change to the To-Do, a client contact logged on the deal. A meeting time, a meeting starting or a due date does not bring it back, so dismissing the prep card for one meeting keeps it away for that meeting.</li>
            <li>Two exceptions. Dismissing a &lsquo;close date at risk&rsquo; deal card hides it for 90 days, and then Cubie checks the date again. A dismissed To-Do or Rock with nothing new around it stays away until you reopen it under Handled priorities.</li>
            <li>On a <span className="font-medium">recurring</span> meeting, a calendar-with-an-✗ button appears next to dismiss: <span className="font-medium">“Never prep this again”</span>. Dismissing with ✗ only clears today&apos;s occurrence — this one stops Cubie prepping that meeting for good. It asks you to confirm first, and you can undo it any time from Settings → AI Personalization.</li>
            <li>Snooze it (tomorrow, in 3 days, next week, or a custom date)</li>
            <li>Schedule a 30-minute focus block — Cubie finds a free slot for you</li>
            <li>Take the obvious next action — open the deal in HubSpot, reply to an email, generate prep for a meeting, or draft a follow-up</li>
            <li>On a card about one of your deals, <span className="font-medium">Defer the deal…</span> sets the whole deal aside until a follow-up date you pick, with a note — wider than snoozing one card. Every card about that deal stays away until then, and Cubie adds a HubSpot task for that day (see <TopicLink id="sales-cockpit">Deferring a deal</TopicLink>)</li>
        </ul>
        <p>
            “Get me other important things” surfaces more items, and the thumbs buttons on
            cards without personal Gmail context (👍 / 😐 / 👎) record what&apos;s useful and guide future answers. Older card text and
            comments are never replayed into a new answer. The strip at the top shows your unread
            email count, your next meeting, and how many free slots you have left today — click it
            to jump into Productivity.
        </p>
        <p>
            <span className="font-medium">Cards know whose job it is.</span> A meeting card names the
            person you&apos;re actually meeting — read off the invite, so a 1:1 titled with both your
            names can no longer come back as “your 1:1 with <em>you</em>”. And an email you were only{' '}
            <span className="font-medium">Cc&apos;d</span> on is no longer handed to you as your own
            deadline: Cubie checks whether you were the one asked, so an “ACTION NEEDED” aimed at a
            colleague shows up as awareness of what <em>they</em> owe, not a task on your plate.
        </p>
        <p>
            <span className="font-medium">Long-dead CRM records stop shouting.</span> A deal that
            hasn&apos;t moved in months used to look <em>more</em> urgent the longer it sat — a deal
            stalled 254 days was being cited as proof that an invoice was critical, when in truth it
            had closed long ago and HubSpot just never caught up. Now, past roughly three months of
            silence Cubie treats the record as probably out of date: it can suggest you tidy it up,
            but it will never call it urgent or use it to justify another card. Past six months it
            drops out of Priorities entirely.
        </p>
        <SubHeading id="next-week-look-ahead">Next week look-ahead (Fridays)</SubHeading>
        <p>
            On Friday
            afternoons Cubie runs a look-ahead over your <span className="font-medium">next</span>{' '}
            week and adds those items to the same list, each wearing a violet{' '}
            <span className="font-medium">Next week</span> badge. They&apos;re prep you can knock
            out before Monday — most importantly, if you have a prospect or client call next week,
            a nudge to connect with your subject-matter expert beforehand and align on strategy.
            They respond to the same done / dismiss / snooze actions as everything else, and they
            drop off on their own once that week actually starts.
        </p>
        <SubHeading id="in-slack-too">In Slack, too</SubHeading>
        <p>
            If your department is enrolled
            (or you&apos;re on the exec team), Cubie DMs you your top priorities every weekday
            morning — the same picks as your home page, each with a one-tap link to act on it.
            On Friday afternoons a second DM carries only the next-week look-ahead, under a
            header naming the week it covers. You never get the same morning list twice in one
            day. Nothing to set up; admins turn it on per department under Administration → AI
            Priorities.
        </p>
        <p>
            <span className="font-medium">How old is this list?</span> Both the Slack DM and the
            AI Priorities card on your home page now say when Cubie last compiled them &mdash;
            &ldquo;Compiled 6:04 AM EDT&rdquo;, in your own timezone &mdash; right next to the
            <span className="font-medium"> Refresh</span> button. The list is built overnight, so
            by mid-afternoon it can be several hours old; if something important has happened
            since, hit Refresh and Cubie rebuilds it from scratch. The stamp is when Cubie
            finished thinking, not a claim that every mailbox and CRM record was re-read at that
            exact moment.
        </p>
        <SubHeading id="whats-on-your-plate">What&apos;s On Your Plate</SubHeading>
        <p>
            Teams switched on
            for this (Administration → Feature Access) get a briefing about what is
            <span className="font-medium"> waiting on them</span>, not only what is coming up:
            a message you owe someone an answer on, a message you are still waiting on, a
            request nobody came back to you about, and a commitment whose date has quietly
            stopped being believable because the other side has gone quiet. For a sales team
            that is a deal whose close date nobody has tested against three weeks of client
            silence; for an operations team it is the same question about a report
            promised for a date, or an approval nobody has given back.
        </p>
        <p>
            Nothing new is
            collected — these are the same mailboxes, calendars and Traction items Cubie
            already reads for you, and it is a personal view: nobody sees anyone else&apos;s
            plate.
        </p>
        <p>
            <span className="font-medium">A quiet deal, or a quiet client?</span> HubSpot only
            puts an email on a deal when the person you wrote to is on that deal; email someone
            else at the client and it is filed on the client&apos;s company instead. So a deal
            card reads both: when the company heard from us more recently than the deal shows,
            the card gives both numbers (&ldquo;nothing logged on this deal in 82 days, though
            the bank was contacted 13 days ago&rdquo;) and weighs less than a deal whose whole
            client has gone quiet. Which deals get a card does not change, because that newer
            email may have been about a different deal with the same client.
        </p>
        <SubHeading id="you-said-you-would">&ldquo;You said you would…&rdquo;</SubHeading>
        <p>
            Cubie now
            listens for the things <span className="font-medium">you</span> say you will do — in
            meetings you were in, in your L10s, in the email you send and in your Slack messages —
            and, once the date passes (or a few days go by when no date was given), checks your
            sent email, Slack, Drive and EOS To-Dos for a sign it happened. If there is none, it
            shows up in your briefing with <span className="font-medium">your own words</span>, when
            and where you said them, and exactly what Cubie looked at, plus a link back to the
            meeting or message.
        </p>
        <p>
            It says <span className="italic">&ldquo;no sign of it yet&rdquo;</span>,
            never &ldquo;you didn&apos;t do it&rdquo;: calls and other tools are invisible to Cubie, so
            tick the card if it&apos;s done and it is marked kept. Only your own promises are heard,
            only from meetings you attended, and nothing is ever marked done on its own.
        </p>
        <p>
            <span className="font-medium">Ask about it in Chat.</span> Ask{' '}
            <span className="italic">&ldquo;What have I promised people this week?&rdquo;</span> or, if
            you manage people,{' '}
            <span className="italic">&ldquo;Who did my sales team promise a follow-up, quote or resource
            to this week and hasn&apos;t sent it?&rdquo;</span> Cubie answers grouped by the contact who is
            waiting: who promised it, their words, when and where, the due date, and whether there is any
            sign it happened. A manager sees the promises of the people who report to them and the teams
            they lead; an org-wide exec sees every team. Cubie reads overnight, so something said today shows up
            tomorrow.
        </p>
        <SubHeading id="close-date-roll-up">The close-date roll-up</SubHeading>
        <p>
            Separately, a
            short weekly Slack message can go to whoever reviews the sales pipeline,
            listing the deals whose estimated close date is hardest to believe — the date,
            who owns it, and how long since anyone logged talking to the client, on the deal
            and on the client&apos;s company record. When the client heard from us more
            recently than the deal shows, the row gives both, and that deal is listed after
            the ones whose whole client has gone quiet.
        </p>
        <p>
            It is the
            review a sales leader does by hand, for the whole team at once, and it also
            catches two things a rep&apos;s own briefing cannot: deals that are only
            starting to drift, and deals whose owner has left.
        </p>
        <p>
            It is read-only — no
            buttons, and it links to HubSpot rather than back into Cubie, because the
            reps&apos; own &ldquo;done&rdquo; marks are shared per deal and a reviewer
            acting on the list would silence the rep who owns it. Recipients are named
            individually under Administration → Feature Access.
        </p>
        <SubHeading id="already-answered-it">Already answered it?</SubHeading>
        <p>
            A Slack question you&apos;ve
            replied to &mdash; in its thread, or later in the same DM &mdash; is treated as handled
            and won&apos;t come back as a priority. Email works the same way: when the newest message
            in a thread is your reply to someone else&apos;s request &mdash; an &ldquo;action needed&rdquo;
            note you answered, a decision you confirmed &mdash; Cubie counts that thread as done. It only
            nudges you about a thread when <em>you</em> are the one still waiting on an answer. Only asks
            you haven&apos;t responded to make the list.
        </p>
        <SubHeading id="on-pto">On PTO?</SubHeading>
        <p>
            Cubie reads your calendar&apos;s
            out-of-office and knows when you&apos;re off. Instead of a work-day briefing it
            holds your priorities and greets you with a simple &ldquo;enjoy your time off&rdquo; —
            on the home page and in your Slack DM — surfacing something only if it genuinely
            can&apos;t wait for your return.
        </p>
        <p>
            <span className="font-medium">Going away soon?</span> In the week before your
            time off starts, Cubie puts a hand-off reminder at the <em>top</em> of your
            priorities. It lists what actually lands inside your absence — L10s you manage
            that meet while you&apos;re gone, to-dos and rock milestones due those days,
            rocks already off track, and scorecard numbers you&apos;re the one who enters —
            and suggests who could cover each, drawn from your direct reports, your own
            manager and your teammates.
        </p>
        <p>
            Anyone whose own time off overlaps yours is left
            out of the suggestions. It repeats each day until you leave, because the
            deadline is the day you go. Handing off the L10s matters most: once you&apos;re
            out, only your team&apos;s leader or an admin can re-point them.
        </p>
        <SubHeading id="left-the-company">Left the company?</SubHeading>
        <p>
            When an admin offboards
            someone (Administration → Users → Offboard), Cubie stops their daily run, hands
            their uploaded docs to a named successor, and sweeps everything they left open —
            EOS to-dos, rocks, L10s they ran, HubSpot deals, commitments made in email — into
            a handoff dossier on the successor&apos;s <span className="font-medium">Handoffs</span>{' '}
            page. Open inherited items keep appearing in the successor&apos;s daily priorities,
            marked &ldquo;Inherited from&rdquo;, until each is done or dismissed. A departed
            teammate whose account is simply deactivated (no offboard run) is still dropped
            from the daily run and team digest automatically.
        </p>
        <Callout tone="warn">
            The very first load (or a refresh) can take up to a couple of minutes while Cubie
            indexes your priorities — it updates on its own when it&apos;s ready.
        </Callout>
    </>
);
