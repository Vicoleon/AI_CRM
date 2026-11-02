import React from 'react';
import { Callout, SubHeading } from '../kit';

// Help → Productivity. Served at /help/productivity; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const Productivity: React.FC = () => (
    <>
        <p>
            The Productivity page gives you full-page views of your day, with a Refresh button to
            pull the latest. Tabs include:
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Email</span> — your <span className="font-medium">unread</span> mail <span className="font-medium">addressed to you</span>: messages where you&apos;re in To: come first, Cc&apos;d mail is tagged and shown below. Distribution-list and bulk/newsletter mail is hidden, and anything your own Gmail filters route out of your inbox stays out — so the view mirrors your real inbox, not every list you&apos;re on. Once you&apos;ve read everything it shows <span className="font-medium">&ldquo;You&apos;re all caught up&rdquo;</span> rather than replaying old mail, and Cubie flags the messages that likely need your attention with a <span className="font-medium">&ldquo;May be important&rdquo;</span> badge</li>
            <li><span className="font-medium">Calendar</span> — upcoming events plus free-slot suggestions</li>
            <li><span className="font-medium">Slack</span> — your unread channels and the @-mentions &amp; DMs aimed at you. <span className="font-medium">Hover a channel</span> for an on-demand AI summary of its unread messages — Cubie reads them (plus a little context) and tells you what&apos;s being discussed, so you don&apos;t have to open Slack (connect Slack in Settings)</li>
            <li><span className="font-medium">Documents</span> — your recent Google Drive files, plus a one-click way to add any of them to the knowledge base</li>
            <li><span className="font-medium">Team Folders</span> — your department&apos;s shared documents</li>
        </ul>
        <p>If the selected AI provider cannot check which messages are important, the inbox still shows the mail and labels the AI triage as unavailable. The &ldquo;May be important&rdquo; badges return when the provider works again.</p>

        <SubHeading id="email-mailboxes">Choose an email mailbox</SubHeading>
        <p>
            Your work mailbox is the default. If your admin allows extra Gmail accounts and you
            connected one in Settings → Email, choose it from the Mailbox menu on this tab. The
            inbox changes to that selected account; Refresh updates the account you are viewing.
            All connected mailboxes are included automatically in your own AI Priorities and
            scheduled summaries, and each email item shows its mailbox address. Personal mail
            stays out of team and manager views.
        </p>

        <SubHeading id="why-it-opens-instantly">Why it opens instantly</SubHeading>
        <p>
            As soon as your AI Priorities load on the home page, Cubie quietly gets your Email,
            Calendar, Slack and Documents ready in the background, so the Productivity tabs usually
            open with everything already there. Anything more than a minute old is refreshed
            automatically while you look at it (the Refresh button spins while that happens), and
            Refresh pulls the latest straight away.
        </p>

        <SubHeading id="documents-tab">More on the Documents tab</SubHeading>
        <p>
            It shows the files you&apos;ve touched in Google Drive over the last week — your own
            files plus anything that&apos;s been shared with you (a small people-icon marks shared
            files). It mirrors your Drive exactly: a personal document you haven&apos;t shared with
            anyone stays visible only to you. Use the <span className="font-medium">Folders</span>{' '}
            button to narrow the list to specific Drive folders; your choice is remembered for next
            time.
        </p>
        <SubHeading id="adding-a-document">Adding a document to the knowledge base</SubHeading>
        <p>
            Each row has a <span className="font-medium">+ Knowledge</span> button that copies that
            file into Cubie&apos;s shared knowledge base, so Chat can start answering questions from
            it. To use it:
        </p>
        <ol className="list-decimal pl-5 space-y-1">
            <li>Click <span className="font-medium">+ Knowledge</span> on the file&apos;s row.</li>
            <li>Choose a <span className="font-medium">Topic</span> and <span className="font-medium">Department</span> (both required), and tick <span className="font-medium">Company-Wide</span> if everyone should be able to find it.</li>
            <li>Confirm. Cubie pulls the file in — Google Docs, Sheets and Slides are converted to Word, Excel and PowerPoint automatically — the row switches to <span className="font-medium">Added</span>, and the document is now searchable in Chat and listed on the <span className="font-medium">Assets</span> page.</li>
        </ol>
        <Callout tone="warn">
            Adding a document to the knowledge base <span className="font-medium">shares it with
            your team.</span> Everyone in the Department you choose (or everyone, if you tick
            Company-Wide) can then find it in Chat and on the Assets page — <span className="font-medium">even
            if the original file is private in Google Drive.</span> Only add documents you mean to
            share. Adding to the knowledge base needs upload permission — if the
            <span className="font-medium"> Upload a document</span> and
            <span className="font-medium"> Knowledge</span> buttons are greyed out, your account
            doesn&apos;t have it yet; ask an admin to turn it on.
        </Callout>
        <Callout tone="info">
            <span className="font-medium">Your Drive and Slack stay private by default.</span> The
            Documents tab only ever shows files your own Google account can already open, and the
            Slack tab only shows the channels and DMs you&apos;re a member of — Cubie reads both as
            you, so private channels you&apos;re not in stay hidden. Nothing becomes visible to
            colleagues unless you deliberately add a Drive file to the knowledge base.
        </Callout>
    </>
);
