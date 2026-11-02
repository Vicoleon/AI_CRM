import React from 'react';
import { SubHeading } from '../kit';

// Help → Meeting prep. Served at /help/meeting-prep; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const MeetingPrep: React.FC = () => (
    <>
        <p>
            Cubie can build a briefing for an upcoming meeting — reach it from a “Generate prep”
            action on an AI Priority or a calendar event. You get an executive summary, the meeting
            objective, a client profile, who&apos;s involved, and a suggested agenda.
        </p>
        <p>
            <span className="font-medium">No more waiting on the page.</span> Clicking{' '}
            <span className="font-medium">Get Ready</span> (or{' '}
            <span className="font-medium">Regenerate</span> on a briefing you already have)
            starts building the briefing <span className="font-medium">in the background</span>.
            You can keep working, open something else, or leave the page altogether. When it&apos;s
            ready, Cubie sends you a <span className="font-medium">notification with a link</span>{' '}
            straight to it: a pop-up if you&apos;re anywhere in Cubie, and an entry in the new{' '}
            <span className="font-medium">bell</span> in the left sidebar, which keeps it until you
            open it.
        </p>
        <p>
            If Cubie can&apos;t build the briefing — or it takes far longer than it should — you
            get a notification saying so too, and opening it lets you try again. While a new
            version is being built, the version you already have stays open and readable.
            Clicking the button again while a briefing is on its way doesn&apos;t start a second
            one; you simply get the one already being built. Cubie builds a few briefings for you
            at a time; if you start more than that, it asks you to wait for one to finish.
            Rewriting a single section still happens right there on the page. You can mute these
            notifications in <span className="font-medium">Settings → General</span>.
        </p>
        <p>
            Add a <span className="font-medium">meeting intent</span> (for example, “Push for pilot
            approval”) to focus the briefing, or print it to PDF — the saved PDF is automatically
            named after the meeting.
        </p>
        <p>
            <span className="font-medium">Your briefing is saved, and every change is a new
            version.</span> Opening a meeting&apos;s prep now shows your latest saved version
            straight away instead of rebuilding it every time you open the page.{' '}
            <span className="font-medium">Regenerate</span> builds a fresh version from the newest
            information, and keeps any section you edited yourself exactly as you wrote it — untick{' '}
            <span className="font-medium">Keep my edits</span> if you want those rebuilt too. If
            Cubie can&apos;t build the new version, it sends you a notification saying so and the
            version you had stays as it was.
        </p>
        <p>
            If your workspace loses access to a client in HubSpot, a briefing or saved Asset
            based on that client becomes unavailable. Opening Meeting Prep starts a fresh
            briefing using sources you can currently access.
        </p>
        <SubHeading id="earlier-versions">Go back to any earlier version</SubHeading>
        <p>
            The version menu lists every version of the briefing — v1, v2, v3 and so on — with
            when it was made and what changed. Open one to read it, and{' '}
            <span className="font-medium">Restore</span> adds it back as the newest version.
            Nothing is overwritten or thrown away, so you can always go back again. Older versions
            are read-only; you edit the newest one.
        </p>
        <SubHeading id="edit-on-the-page">Edit right on the page</SubHeading>
        <p>
            Every section of the briefing has <span className="font-medium">Edit</span>, to change
            the words yourself and save, and <span className="font-medium">Rewrite</span>, to have
            Cubie redo just that section — with an optional instruction such as “shorter” or “focus
            on pricing” — without touching the rest. Each section shows whether you edited it or
            Cubie rewrote it, and every save, rewrite or restore becomes its own version.
        </p>
        <SubHeading id="hand-it-off">Need to hand it off?</SubHeading>
        <p>
            <span className="font-medium">Word</span> downloads the briefing as an editable Word
            document (opens in Microsoft Word or Google Docs) so you can tweak it before a client
            call, and <span className="font-medium">Save to Assets</span> puts it on your Assets
            page. From there you can <span className="font-medium">share it with a
            teammate</span> (they get access plus a notification) or have Cubie
            <span className="font-medium"> draft a Gmail</span> with the document attached — Cubie
            never sends it; you review the draft and hit send yourself.
        </p>
        <p>
            Word, PDF and Save to Assets always use{' '}
            <span className="font-medium">the version you have open</span>, and the Word file and
            the Assets copy carry its version number in their name.
        </p>
        <SubHeading id="what-a-briefing-draws-on">What a briefing draws on</SubHeading>
        <p>
            Briefings stay <span className="font-medium">grounded in your records</span> — Cubie
            sticks to what your transcripts, emails and documents actually say and cites its
            sources. It won&apos;t guess whether a past deal was won or lost; where the records are
            thin, it tells you so instead of filling in the blanks.
        </p>
        <p>
            <span className="font-medium">Built from more of what Cubie knows.</span> For meetings
            with clients, the briefing includes the client&apos;s{' '}
            <span className="font-medium">HubSpot account</span>: open deals with their stage,
            amount and close date, past wins, and when the client was last contacted — both on the
            deal and on the company. If you have Customer X-Ray access, it also draws on the
            company&apos;s latest <span className="font-medium">Customer X-Ray</span>. If you use{' '}
            <span className="font-medium">My To-Dos</span>, the to-dos you still owe the people on
            the invite feed the <span className="font-medium">“What You Owe Them”</span> list.
        </p>
        <p>
            The briefing also recalls what was agreed in your last meetings with them, and picks
            up anything today&apos;s AI Priorities flagged about the meeting, the people on it or
            the client. For internal meetings, it flags teammates on the invite who have{' '}
            <span className="font-medium">time off</span> in the next two weeks (only the dates they
            are out). A <span className="font-medium">Built from</span> strip at the top shows which
            sources informed the briefing, so you can see what it did and didn&apos;t have to go on.
        </p>
        <p>
            <span className="font-medium">Internal meetings and 1:1s get real context now.</span>{' '}
            Cubie reads your recent email with the people on the invite — colleagues included, not
            just outside companies — and adds a <span className="font-medium">“What You Owe Them”</span>{' '}
            list when a thread is still sitting on your reply. So a 1:1 briefing arrives already
            knowing what your counterpart is waiting on from you, instead of saying it found no
            history.
        </p>
        <p>
            <span className="font-medium">It knows when you actually last met.</span> Every
            briefing checks your real meeting history with the people on the invite — the
            meetings you attended together, when they happened, and who was in the room — from
            attendance records rather than guesswork, so “last time we spoke” comes with a date.
        </p>
        <SubHeading id="know-whos-in-the-room">Know who&apos;s in the room</SubHeading>
        <p>
            For meetings with people outside your company, briefings now include an{' '}
            <span className="font-medium">Attendee Intelligence</span> panel: each outside
            attendee&apos;s actual position and career trajectory, the posts, articles and talks
            they have published, the themes they keep returning to, and a read on how they
            communicate and decide — plus openers built from something they actually said, so
            you don&apos;t walk in with a generic pleasantry. Every profile and post is a link
            you can open and check before you quote it.
        </p>
        <p>
            Two things to know about that panel. Each style read is labelled with how much
            evidence sits behind it — <span className="font-medium">Well evidenced</span>,{' '}
            <span className="font-medium">Partly evidenced</span> or{' '}
            <span className="font-medium">Thin evidence</span> — so you can tell a read built on
            a dozen posts from one built on a job title, and Cubie says plainly when it found
            nothing rather than inventing a personality. And it only ever researches{' '}
            <span className="font-medium">people outside your company</span>: your colleagues are never
            profiled, shared inboxes and meeting rooms are skipped, and the research sticks to
            public professional material — what someone publishes about their work, never
            anything personal.
        </p>
        <p>
            Every post or talk the panel lists links to that item itself, not just a website&apos;s
            front page, and Cubie refers to people by name rather than guessing he or she. If
            Cubie runs out of time researching someone, the briefing says so under{' '}
            <span className="font-medium">Intelligence Gaps</span> instead of leaving their card
            out without a word.
        </p>
        <SubHeading id="deep-prep">Need the deep version?</SubHeading>
        <p>
            <span className="font-medium">Generate deep prep</span> on a calendar event runs a more
            thorough, multi-step review in the background and
            <span className="font-medium"> emails you a PDF</span> when it&apos;s ready — no waiting
            on screen. The prep is tailored to your team (Sales gets a sales-focused brief; other
            departments get one written for their work). Deep prep is a separate emailed report,
            so it doesn&apos;t appear in the version menu — the regular briefing is the one that
            lands in Cubie with a notification.
        </p>
        <SubHeading id="only-meetings-you-accepted">Only meetings you actually accepted</SubHeading>
        <p>
            Cubie no longer tells you to prep for invites you never responded to — those are
            usually calls you don&apos;t need to be on, and they were burying the meetings you did
            commit to. Instead you get one line at the top of AI Priorities listing the invites
            still waiting on your answer, so nothing goes missing:{' '}
            <span className="italic">accept or decline and it stops showing up</span>.
        </p>
        <p>
            A <span className="font-medium">maybe</span> still gets prepped (you might have to show
            up), and meetings you <span className="font-medium">declined</span> are dropped
            entirely — no prep card, and no “Get Ready” button on your calendar.
        </p>
        <p>
            <span className="font-medium">This now includes meetings you organize yourself</span>:
            Google lets you answer <span className="font-medium">No</span> to your own meeting, and
            Cubie used to ignore that, so a manager who had stood down from their own recurring
            meeting kept being prepped for it every week. Leaving your own invite{' '}
            <span className="font-medium">unanswered</span> still counts as going, and solo blocks
            are unaffected. You can still hit <span className="font-medium">Get Ready</span> on an
            unanswered invite whenever you want — prepping to decide is fair game; Cubie just
            won&apos;t nag you about it.
        </p>
        <SubHeading id="briefing-feedback">Briefing feedback</SubHeading>
        <p>
            At the bottom of a briefing, tell Cubie whether it was helpful with the
            <span className="font-medium"> 👍 / 😐 / 👎</span> buttons (add a quick note if you
            like) — your ratings guide future briefings, while older briefing text and comments
            are kept out of new prompts.
        </p>
    </>
);
