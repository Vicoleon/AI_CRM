import React from 'react';
import { Callout } from '../kit';

// Help → Handoffs (departed teammates). Served at /help/handoffs; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const Handoffs: React.FC = () => (
    <>
        <p>
            When a teammate leaves your company, their open work doesn&apos;t vanish — it lands with a
            named successor, usually their manager. If that&apos;s you, a{' '}
            <span className="font-medium">Handoffs</span> item appears under{' '}
            <span className="font-medium">My Work → HR &amp; Culture</span>.
        </p>
        <ul className="list-disc pl-5 space-y-1.5">
            <li>
                <span className="font-medium">The dossier.</span> The moment someone is
                offboarded, Cubie inventories everything they left open — EOS to-dos, rocks and
                milestones, L10 meetings they ran, scorecard numbers they entered, HubSpot deals
                they owned, and commitments they made in recent email — and writes you a short
                handoff brief. If IT had already suspended their Google account, the dossier says
                so: the email/calendar side may be incomplete, so also check the mailbox and
                files Google Workspace transferred to you.
            </li>
            <li>
                <span className="font-medium">The checklist.</span> Each inherited item can be
                marked done, dismissed (not needed), or reopened. Links point at the original
                thread, deal, or document wherever one exists.
            </li>
            <li>
                <span className="font-medium">Cubie keeps following up.</span> Open inherited
                items appear in your daily AI Priorities as &ldquo;Inherited from …&rdquo; cards
                until you close them — closing the card and ticking the checklist are the same
                action, whichever you do first.
            </li>
            <li>
                <span className="font-medium">Their past priorities.</span> From the handoff you
                can read the departed person&apos;s old priority cards, read-only — useful for
                &ldquo;what was on their plate last week?&rdquo;. These views are logged.
                Org-wide executives see every handoff, not just their own.
            </li>
        </ul>
        <p>
            HubSpot deal details and inherited cards appear only while Cubie can confirm those
            deals still belong to your workspace. If access changes, an older item or part of
            the handoff may disappear; a past card or email commitment without enough source
            information stays hidden. Email and calendar commitments link to the source Cubie
            verified during the sweep.
        </p>
        <Callout tone="warn">
            Admins: run the offboard <span className="font-medium">before</span> IT suspends the
            Google account. Cubie&apos;s last pass over the person&apos;s email and calendar only
            works while the account can still be read. The plain{' '}
            <span className="font-medium">Delete</span> button is for junk or test accounts only;
            if one has leftover priorities cards, Delete shows how many and asks a second time
            before removing it.
        </Callout>
    </>
);
