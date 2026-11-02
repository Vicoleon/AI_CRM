import React from 'react';
import { AskLink, Callout, SubHeading } from '../kit';

// Help → Sales calls (call capture). Served at /help/sales-calls; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.
//
// Call capture and the cockpit are roster-gated AND flag-gated, and the
// backend mirror in rubik_core/help/content.py cannot express that
// gate at all (`requires` only knows "xray" and "crm_sync"), so both
// homes state it in the COPY instead — the smart-teams shape. Every
// switched-off part is described as coming soon rather than as
// available: telling a reader a feature works when its flag is off is
// worse than not documenting it.
//
// Both sections lead with the whole-feature switch (CCS_ENABLED /
// COCKPIT_ENABLED, off by default) and the admin's roster. A salesperson
// who has heard about this needs "not on for you yet, and here is what it
// does", not a blank page. The narrower coming-soon callouts are a
// different kind of blocked (a Google grant, a HubSpot grant), so both
// facts are stated.

export const SalesCalls: React.FC = () => (
    <>
        <Callout tone="warn">
            <span className="font-medium">Not on for you? Ask your admin.</span> Sales call
            capture has to be switched on for your workspace, and your admin decides who
            gets it. If you don&apos;t see{' '}
            <span className="font-medium">Today&apos;s Calls</span> under{' '}
            <span className="font-medium">My Work → Sales</span> in your left rail, it
            isn&apos;t on for you: nothing of yours is being captured today. The rest of this
            section is what it does once it is, so you know what&apos;s coming.
        </Callout>
        <p>
            After a client call, Cubie reads that call&apos;s own transcript and proposes the
            HubSpot updates it heard — a vendor field, a task, an SOW request, a deal-stage
            signal — and you accept or reject them on one screen, the{' '}
            <span className="font-medium">wrap-up</span>. Every proposal quotes the exact
            sentence from the call it came from, and{' '}
            <span className="font-medium">nothing reaches HubSpot until you approve it.</span>
        </p>
        <Callout tone="info">
            Your workspace admin can choose a compatible AI provider for reading call transcripts
            into wrap-up suggestions. If that provider is unavailable, Cubie shows the extraction
            failure and lets you retry after your admin fixes the connection. If only part
            of a call was analyzed, the wrap-up warns you so you can review it manually.
            Cubie does not switch providers or write unapproved updates to HubSpot.
        </Callout>
        <SubHeading id="it-all-starts-from-your-calendar">It all starts from your calendar</SubHeading>
        <p>
            Under{' '}
            <span className="font-medium">My Work → Sales → Today&apos;s Calls</span>, Cubie lists today&apos;s
            client meetings — the ones with someone outside your company on them — and for each one
            shows who is on the invite, the <span className="font-medium">Meet link from the
            invite itself</span>, and the company it thinks the call is with. You start the
            call from that row. There is nothing to set up and no second meeting to create:
            if it&apos;s on your calendar, it&apos;s on the list.
        </p>
        <SubHeading id="confirming-the-company">Confirming the company</SubHeading>
        <p>
            <span className="font-medium">The one thing worth your attention is the
            company.</span> Everything the wrap-up later proposes lands on that
            company&apos;s record, so the row asks you to confirm it{' '}
            <span className="font-medium">before</span> the call rather than guessing after.
            Cubie reads the email addresses on the invite and offers the company they point
            at — and when it isn&apos;t sure, it says so and asks instead of choosing.
            If a selected HubSpot record is no longer available to this workspace, Cubie
            stops before starting capture. Refresh the calls list and choose a current record.
        </p>
        <ul className="list-disc pl-5 space-y-1.5">
            <li>
                <span className="font-medium">One clear match</span> — filled in for you, and
                still changeable.
            </li>
            <li>
                <span className="font-medium">More than one possible match</span> — Cubie
                lists them and you pick. It never picks for you.
            </li>
            <li>
                <span className="font-medium">A personal address</span> (gmail, yahoo, an
                internet-provider address) — the call is listed as normal; the address just
                doesn&apos;t say which company, so you pick.
            </li>
            <li>
                <span className="font-medium">Nothing found</span> — usually means the company
                isn&apos;t in HubSpot yet, or has no deal in this workspace&apos;s chosen
                pipeline. Link it to a workspace deal in HubSpot before Cubie can
                update it.
            </li>
            <li>
                <span className="font-medium">A long guest list</span> — when the invite is
                big enough that Google trims it, Cubie treats the company as uncertain rather
                than trusting the few names it can see.
            </li>
        </ul>
        <Callout tone="info">
            A room booking on the invite is never mistaken for a company, and an internal-only
            meeting never appears on the list at all.
        </Callout>
        <SubHeading id="transcription">Transcription</SubHeading>
        <p>
            <span className="font-medium">Cubie tries, before the call, and tells you what
            happened.</span> When you start a meeting from its row, Cubie asks
            Meet to switch that meeting&apos;s transcription on. It can only do that{' '}
            <span className="font-medium">before the meeting begins</span> — there is no way
            to start transcribing a call that&apos;s already running. Three situations where
            it can&apos;t, and all three are normal:
        </p>
        <ul className="list-disc pl-5 space-y-1.5">
            <li>
                <span className="font-medium">The meeting has already started.</span> Too late
                to switch it on. Start it from the row anyway if you want the call on the
                record against the company — just expect an empty wrap-up.
            </li>
            <li>
                <span className="font-medium">The client sent the invite.</span> It&apos;s
                their meeting on their system, and nobody here can change its settings. Ask
                the host to turn transcription on, or take your own notes.
            </li>
            <li>
                <span className="font-medium">It&apos;s a recurring meeting.</span> Every
                occurrence of a repeating meeting shares one Meet room, so switching
                transcription on switches it on for{' '}
                <span className="font-medium">all the future ones too</span>. The row tells
                you that before you click, and the choice stays yours.
            </li>
        </ul>
        <p>
            Once in a while Google itself turns the request down. The row then says{' '}
            <span className="font-medium">Couldn&apos;t turn transcription on</span> and
            gives Google&apos;s answer. Join with the calendar link and switch transcription
            on from inside Meet if you want a transcript — the call is still tied to the
            company.
        </p>
        <Callout tone="ok">
            <span className="font-medium">Every row says which of these it is, in plain
            words — and a row Cubie can&apos;t switch transcription on for is not
            broken.</span> Confirming the company is the half that matters most, and that
            half always works. Nothing is ever left looking fine when it isn&apos;t, and no
            call is ever shown as being transcribed when it isn&apos;t.
        </Callout>
        <Callout tone="warn">
            Even when Cubie does switch it on, <span className="font-medium">a transcript
            isn&apos;t guaranteed</span> — whether one gets produced is Google&apos;s call,
            not ours. If none arrives, the wrap-up still opens and says so, rather than
            leaving you wondering.
        </Callout>
        <SubHeading id="a-call-that-isnt-on-your-calendar">A call that isn&apos;t on your calendar</SubHeading>
        <p>
            A Meet you open yourself is still noticed and logged against the company (usually
            within fifteen minutes), so a call is never invisible. Cubie can&apos;t switch
            transcription on for it, though, so unless transcription was already running
            there&apos;s nothing for it to read. If a client call matters, put it on your
            calendar.
        </p>
        <SubHeading id="confidential-calls">Confidential calls</SubHeading>
        <p>
            Starting a call from its row carries a <span className="font-medium">Confidential</span> toggle, and
            it&apos;s worth knowing what it does before you need it. A confidential call is{' '}
            <span className="font-medium">logged</span> — it still counts as a call against
            the company — and is <span className="font-medium">never transcribed</span>: no
            transcript, no AI notes, no extraction, no wrap-up, nothing kept to read back.
        </p>
        <Callout tone="warn">
            A confidential call <span className="font-medium">cannot be un-suppressed
            later.</span> There&apos;s no undo and no way to recover what was said, which is
            why Cubie asks you to confirm once before starting one.
        </Callout>
        <SubHeading id="the-wrap-up">The wrap-up</SubHeading>
        <p>
            It appears at the top of{' '}
            <span className="font-medium">My Day</span>, usually within fifteen minutes of
            the call ending, and it&apos;s built to be finished in well under two minutes.
        </p>
        <ul className="list-disc pl-5 space-y-1.5">
            <li>
                <span className="font-medium">Every row arrives already ticked.</span>{' '}
                Approving is one click; rejecting is the deliberate act — untick anything you
                don&apos;t want, then <span className="font-medium">Approve</span>.
            </li>
            <li>
                <span className="font-medium">The quote under each row</span> is the exact
                sentence from the call that produced that proposal, so you&apos;re checking
                Cubie against the call rather than against your memory.
            </li>
            <li>
                <span className="font-medium">A row flagged as a change</span> would replace
                something HubSpot already holds. It shows you the value HubSpot has now and
                needs its <span className="font-medium">own Confirm</span> — it can&apos;t
                ride along on &ldquo;Approve all&rdquo;, and the Approve button stays held
                until you&apos;ve either confirmed it or unticked it. Cubie never silently
                overwrites a field that already has something in it.
            </li>
        </ul>
        <Callout tone="ok">
            <span className="font-medium">&ldquo;Nothing here&rdquo; is an answer, not a
            cancel.</span> It records that you reviewed the call and there was nothing worth
            filing: the call <span className="font-medium">counts as complete</span>, it
            doesn&apos;t come back unless a later transcript needs analysis or adds suggestions,
            and the answer is used to make the next call&apos;s proposals better.
        </Callout>
        <p>
            If more of a transcript arrives after you finished the wrap-up, Cubie analyzes the
            new entries and puts any new suggestions back in My Day for your review. If that
            analysis fails, the wrapped call stays there so you can retry it. A HubSpot write
            that failed stays available to retry even when the newer analysis finds nothing.
            The call remains wrapped, and its original completion time stays on the record.
            Nothing new is written to HubSpot until you approve it.
        </p>
        <Callout tone="ok">
            <span className="font-medium">Fathom-recorded calls count too.</span> If Fathom
            recorded your client call, Cubie picks the recording up within minutes of it
            finishing and treats it like any captured call: it appears under{' '}
            <span className="font-medium">Today&apos;s Calls</span> with a wrap-up, the SOW clock
            starts if the prospect asked for a proposal, and{' '}
            <span className="font-medium">Draft proposal</span> is offered. The company is matched
            from the attendees&apos; email domains; if Cubie can&apos;t tell, the wrap-up asks.
            Internal-only meetings are ignored, and a call Cubie already captured through Meet is
            not duplicated by its Fathom recording.
        </Callout>
        <p>
            <span className="font-medium">An empty wrap-up is normal.</span> Transcription may
            have been off, Cubie may not have been able to switch it on, the call may have run
            under a minute, or it was confidential. The wrap-up still opens, says which of
            those happened, and the call stays on the record — a call is never invisible even
            when what was said is.
        </p>
        <SubHeading id="who-can-read-what-was-said">Who can read what was said</SubHeading>
        <Callout tone="info">
            You can read your own calls; your manager can read the team&apos;s. The{' '}
            <span className="font-medium">Nudge Worklist</span> and the{' '}
            <span className="font-medium">Exec Roll-up</span> never show call text at all —
            they can see that something is outstanding, never what was said on the call.
        </Callout>
        <SubHeading id="the-checklist">The checklist — what to ask on this call</SubHeading>
        <Callout tone="info">
            <p>
                Before a call, pick the product or products it is about. Cubie shows that
                product&apos;s checklist on the row as soon as you tick it, including on your
                first call with a client: the facts the call needs to establish so the record
                is complete and an SOW can actually be built from it — how many sessions,
                how many hours each, the rate, when it starts. Cubie remembers your pick for each
                client, so next time those products are already ticked.
            </p>
            <p className="mt-2">
                Keep it on screen while you talk. Cubie is{' '}
                <span className="font-medium">not</span> listening and will not interrupt you;
                it is a list of what to ask, not a prompter.
            </p>
        </Callout>
        <Callout tone="ok">
            <span className="font-medium">If you miss something, the wrap-up tells you.</span>{' '}
            Anything on the checklist the call did not cover comes back as the question you
            did not ask, not as a blank field — so you can chase it while the call is fresh
            instead of hearing about it days later when someone is trying to write the SOW.
        </Callout>
        <Callout tone="info">
            <span className="font-medium">More than one product in one call.</span> Pick all
            of them and you get one checklist section per product, scored separately. If a
            product comes up that you did not pick, the wrap-up says so and offers to split
            it into its own lead — because a lead is one product, and a call covering three
            of them is three leads.
        </Callout>
        <Callout tone="warn">
            <span className="font-medium">No checklist for your product yet?</span> The call
            runs exactly as it does today. Checklists are written by the people who own the
            products rather than by engineering, so they arrive product by product.
        </Callout>
        <SubHeading id="the-checklist-inside-google-meet">The checklist inside Google Meet</SubHeading>
        <Callout tone="info">
            <p>
                Cubie also has a Google Meet add-on, so the same checklist can sit beside the
                call instead of in another tab. Once you&apos;re in the call, open{' '}
                <span className="font-medium">Activities</span> (the shapes icon, bottom right)
                and choose <span className="font-medium">Cubie</span>. It already knows which
                call you&apos;re on, so it opens on that client with the products you picked in
                Cubie — nothing to choose.
            </p>
            <p className="mt-2">
                You can tick questions off as you ask them and add another product&apos;s
                checklist if one comes up; those ticks stay in the panel, and Cubie still
                isn&apos;t listening. The first time, you sign in with Google inside the panel,
                because Meet keeps it separate from your Cubie tab.
            </p>
        </Callout>
        <Callout tone="warn">
            <span className="font-medium">It has to be opened by you.</span> Google does not let
            any add-on open itself when a call starts, so that one click in Activities is always
            yours. Joining from Today&apos;s Calls opens Meet on your work account, which is where
            the add-on is installed. If you don&apos;t see Cubie under Activities, it hasn&apos;t
            been installed for your account yet.
        </Callout>
        <SubHeading id="leads-from-a-call">Leads from a call</SubHeading>
        <Callout tone="info">
            The wrap-up can also propose a HubSpot{' '}
            <span className="font-medium">lead</span>, attached to the contact on the call,
            with a <span className="font-medium">lead temperature</span> (hot / warm / cold)
            when the call made it clear. Like every other proposal it quotes the call and
            writes nothing until you approve it. Product interest on the lead is still set in
            HubSpot by hand for now.
        </Callout>
        <ul className="space-y-1.5">
            <li><AskLink q="How does Cubie know which company a call is with?" /></li>
            <li><AskLink q="Why couldn't Cubie turn transcription on for my meeting?" /></li>
            <li><AskLink q="What happens after I end a client call?" /></li>
            <li><AskLink q="What does the confidential toggle on a call do?" /></li>
        </ul>
    </>
);
