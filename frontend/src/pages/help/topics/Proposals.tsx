import React from 'react';
import { AskLink, Callout, SubHeading, TopicLink } from '../kit';

// Help → Draft a proposal from a call. Served at /help/proposals; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const Proposals: React.FC = () => (
    <>
        <p>
            When a prospect asks for a proposal on a call, Cubie can draft it for you from that
            call — the short one- or two-page document you send to gauge interest before anyone
            writes an SOW. It is the same shape the Sales team has been sending by hand:{' '}
            <span className="font-medium">Executive Summary</span>,{' '}
            <span className="font-medium">Scope of Work</span>,{' '}
            <span className="font-medium">Desired Outcomes &amp; Key Deliverables</span>,{' '}
            <span className="font-medium">Pricing &amp; Investment</span> and notes, on your
            company&apos;s letterhead from <TopicLink id="company-setup">Company setup</TopicLink>.
        </p>
        <Callout tone="info">
            Your workspace admin can choose a compatible AI provider for proposal drafting and
            revisions. If the selected provider fails, Cubie shows the error and leaves the
            proposal for you to retry; it does not send the call or brief to another provider.
        </Callout>
        <SubHeading id="the-proposals-page">The Proposals page</SubHeading>
        <p>
            <span className="font-medium">My Work → Sales → Proposals</span> lists your proposals and
            the captured calls of the last two weeks you could draft one from, Fathom-recorded calls
            included, each with <span className="font-medium">Open</span> or{' '}
            <span className="font-medium">Draft proposal</span>. Admins can switch to everyone&apos;s
            for support.
        </p>
        <SubHeading id="drafting-a-proposal">Drafting a proposal</SubHeading>
        <p>
            <span className="font-medium">Where.</span> Open the call&apos;s wrap-up under{' '}
            <span className="font-medium">My Work → Sales → Today&apos;s Calls</span> and click{' '}
            <span className="font-medium">Draft proposal</span>. It appears on any wrapped call you
            hosted, and is highlighted when Cubie heard the prospect ask for one. Only the AE on the
            call can draft its proposal. If the call was recorded by Fathom, it also works from the{' '}
            <span className="font-medium">Sales Coach</span>: its Continue in Cubie button opens the
            same proposal here.
        </p>
        <ol className="list-decimal space-y-2 pl-5">
            <li>
                <span className="font-medium">Confirm the brief.</span> Cubie reads the transcript and
                the HubSpot company, contact and deal, and fills a short form: who it is for, the
                service, the scope items that were actually discussed, the outcomes the{' '}
                <span className="font-medium">prospect</span> stated (each with the words they used),
                any pricing that was said, notes, and <span className="font-medium">open questions</span>{' '}
                — the things that were not settled on the call, such as a price that was never quoted.
                Anything Cubie is unsure of is flagged. Fix what is wrong, fill what is missing, and
                click <span className="font-medium">Confirm</span>. Nothing is generated until you do.
            </li>
            <li>
                <span className="font-medium">Generate.</span> Cubie writes the proposal from the
                confirmed brief. It cannot add a service, a deliverable or a number that is not on
                the brief: the pricing table is printed straight from what you confirmed, and any
                scope item the model tries to invent is discarded before you see it. Outcomes only
                list goals the prospect actually said.
            </li>
            <li>
                <span className="font-medium">Revise.</span> Type what to change —{' '}
                <span className="italic">&ldquo;tighten the summary&rdquo;</span>,{' '}
                <span className="italic">&ldquo;change the price to 5,500&rdquo;</span>,{' '}
                <span className="italic">&ldquo;drop item 3&rdquo;</span> — and Cubie produces a new
                version. Every version is kept; open any of them and download it as a branded{' '}
                <span className="font-medium">PDF</span> or an editable{' '}
                <span className="font-medium">Word</span> file.
            </li>
        </ol>
        <Callout tone="info">
            <span className="font-medium">Cubie never sends the proposal.</span> You download it and
            send it yourself, from your own email, after reading it. It does not write SOWs or
            contract language.
        </Callout>
        <SubHeading id="proposal-templates">Proposal templates</SubHeading>
        <p>
            An admin writes your company&apos;s proposal templates in{' '}
            <TopicLink id="company-setup">Company setup</TopicLink>: a standard write-up of one
            service, plus the catalog items that price it. Cubie picks the one that fits the call;
            change it under <span className="font-medium">Template</span> on the brief, or pick{' '}
            <span className="font-medium">None</span> for the AI-written sections only. With no
            templates set up, every proposal is written from the call alone.
        </p>
        <p>
            After your scope of work the download prints the template&apos;s write-up, word for word.
            Its catalog prices start the pricing table, where you can change them, and its list-price
            buttons add a catalog price as a standard rate for you to set the proposal price against.
            The AI never writes a price into the text: every figure comes from the brief you confirmed.
        </p>
        <p className="text-xs text-gray-500">
            Only your own calls, and a call with no transcript has nothing to read. Each step takes
            about half a minute. Each proposal costs a few dollars of model time, visible to admins under
            AI Cost.
        </p>
        <p className="text-xs font-medium text-gray-700 mt-2">Ask Cubie this in Chat</p>
        <ul className="space-y-1.5">
            <li><AskLink q="How do I draft a proposal from a sales call?" /></li>
            <li><AskLink q="Can Cubie add a service to a proposal that we didn't discuss?" /></li>
            <li><AskLink q="How do I change the price on a proposal Cubie drafted?" /></li>
            <li><AskLink q="How do proposal templates work?" /></li>
        </ul>
    </>
);
