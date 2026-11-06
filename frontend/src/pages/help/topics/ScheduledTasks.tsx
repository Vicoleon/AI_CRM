import React from 'react';
import { AskLink, Callout, SubHeading } from '../kit';

// Help → Scheduled tasks. Served at /help/scheduled-tasks; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const ScheduledTasks: React.FC = () => (
    <>
        <p>
            Ask Cubie in <span className="font-medium">Chat</span> to do something on a
            repeating schedule and it will — <span className="italic">&ldquo;every weekday at
            8am send me my important emails&rdquo;</span>, <span className="italic">&ldquo;each
            Monday summarize last week&apos;s meetings&rdquo;</span>, <span className="italic">
            &ldquo;on the 1st, remind me to review the pipeline&rdquo;</span>. Cubie confirms
            the schedule back to you in plain English, then runs it at that time in
            <span className="font-medium"> your own timezone</span> — no browser open, nothing
            to click.
        </p>
        <SubHeading id="where-results-arrive">Where results arrive</SubHeading>
        <p>
            Every run arrives as its <span className="font-medium">own new conversation</span>,
            in a chat folder called <span className="font-medium">Scheduled tasks</span>, named
            with the task and the date — plus a <span className="font-medium">Slack DM</span>.
            Open the conversation and keep asking questions about the result, exactly like any
            other chat. Ask for email and Cubie sends it there too, where that has been
            switched on.
        </p>
        <p>
            <span className="font-medium">You will not have to go looking for it.</span> When
            a run finishes, a lavender dot appears on <span className="font-medium">Chat</span> in
            the left sidebar and on the conversation itself, and the{' '}
            <span className="font-medium">Scheduled tasks</span> folder shows how many results
            you have not opened yet. Opening the conversation clears its dot. If a task fails
            often enough that Cubie pauses it, the dot appears on{' '}
            <span className="font-medium">Settings → Scheduled tasks</span> instead — there is
            no result to read, only a task to look at. Turn these off under{' '}
            <span className="font-medium">Settings → General → Notifications</span>.
        </p>
        <Callout tone="info">
            <span className="font-medium">What a scheduled run can and cannot do.</span>{' '}
            It reads your email, calendar, meeting transcripts, HubSpot, Slack, the knowledge
            base and your EOS data — the same sources Chat uses, as you, with your own
            permissions. It <span className="font-medium">cannot</span> send an email or book
            a meeting: nobody is there to approve those, so a scheduled run
            prepares work and never commits it. It can still leave a draft in your Gmail
            drafts.
        </Callout>
        <SubHeading id="write-the-request-so-it-stands-on-its-own">Write the request so it stands on its own</SubHeading>
        <p>
            A run has no memory of the conversation you set it up in, so &ldquo;the same as
            yesterday&rdquo; gives it nothing to work from. Cubie turns what you say into a
            full instruction and shows it to you, so you can correct it.
        </p>
        <p>
            Before it saves a new task, or a change to one, Cubie shows you a card with the
            task&apos;s name, the <span className="font-medium">full instruction</span>, when it
            runs and where results go, and saves it only when you click{' '}
            <span className="font-medium">Yes, go ahead</span>. Read the instruction: it runs
            later with your access and nobody watching, so say no to anything you did not ask
            for. Pausing, resuming and running a task now need no OK; deleting one does.
        </p>
        <SubHeading id="managing-your-tasks">Managing your tasks</SubHeading>
        <p>
            To see or change what you have scheduled, ask <AskLink q="What have I scheduled?" />
            {' '}in Chat, or open <span className="font-medium">Settings &rarr; Scheduled
            tasks</span>. From there you can <span className="font-medium">pause</span> a task
            (a vacation, say), resume it, <span className="font-medium">run it once
            immediately</span>, or delete it.
        </p>
        <p>
            Pausing keeps everything and just stops the runs; deleting is permanent, though
            results you already received stay in your chat history. There is a cap on how many
            tasks one person can keep, because every task is a full Cubie run each time it fires.
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li>Timing is accurate to about five minutes: <span className="font-medium">8:00</span> means between 8:00 and 8:05.</li>
            <li>If a task fails several times in a row, Cubie <span className="font-medium">pauses it and tells you</span> rather than failing quietly forever.</li>
            <li>If a run has nothing to report — a genuinely quiet inbox — it says so rather than inventing something.</li>
            <li>Unread results also show in the <span className="font-medium">bell</span> in the left sidebar; the chat conversation and the Slack DM are still where you read the full result.</li>
        </ul>
        <p className="text-sm text-gray-500">
            Scheduled tasks are rolling out team by team, so the option appears in Chat only
            once your team has it.
        </p>
    </>
);
