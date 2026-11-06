import React from 'react';
import { SubHeading } from '../kit';

// Help → Customer X-Ray. Served at /help/customer-xray; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const CustomerXray: React.FC = () => (
    <>
        <p>
            On <span className="font-medium">My Work → Sales → Customer X-Ray</span>, search a
            customer and generate an X-Ray — a single view of the whole relationship. Open to
            everyone unless an admin has narrowed it. Two flavors:
        </p>
        <ul className="ml-5 list-disc space-y-1">
            <li>
                <span className="font-medium">Fast Research</span> (seconds) — a churn-risk
                score with an explainable factor breakdown, a relationship timeline,
                commercial health, and the top expansion opportunities: products and
                services from your company&apos;s catalog that the customer hasn&apos;t
                bought yet. Cubie judges what they already have
                from their won deals in HubSpot, so a service that is only on an open or
                lost deal still counts as an opportunity.
            </li>
            <li>
                <span className="font-medium">Deep Research</span> (a few minutes) — adds
                communication-health metrics from email and past meetings, documents, and
                recent online news about the customer, researched live. It runs in the
                background and emails you the finished brief as a PDF.
            </li>
        </ul>
        <p>
            <span className="font-medium">Deep Research costs money every time it runs</span>,
            so Cubie asks you to confirm before starting one. Save it for the moments that
            warrant it — a renewal, a QBR, a save. For everything else, Fast Research is
            instant and covers the same churn score, signals and timeline.
        </p>
        <p>
            Your workspace admin can choose a provider for X-Ray analysis. Deep Research&apos;s
            live web search still uses managed Vertex. If the selected analysis provider is
            unavailable, Cubie reports the failure instead of switching accounts.
        </p>
        <p>
            When several workspaces share one HubSpot portal, an X-Ray is available only for
            a company whose deals all belong to your workspace. A company linked to another
            workspace&apos;s deals stays unavailable, including older saved briefs, until its
            ownership can be confirmed.
        </p>

        <SubHeading id="your-companys-own-fields">
            Renewal date, account status and the CSM
        </SubHeading>
        <p>
            X-Ray reads HubSpot&apos;s standard fields for every customer. A contract&apos;s
            renewal date, whether it auto-renews, the account&apos;s status, its
            customer-success manager and the deal field that says what a customer bought are
            fields your company created in HubSpot, under names of its own, so an admin tells
            Cubie once which fields those are. Until then the churn score leaves out the
            renewal and status factors (it never counts the unknown as healthy), the status and
            CSM tiles stay empty, and X-Ray suggests no expansion opportunity, because it
            can&apos;t yet tell what the customer already owns. An admin can also list more
            fields for the write-up to take into account.
        </p>

        <SubHeading id="cant-find-an-older-event">
            Can&apos;t find an older event on the timeline?
        </SubHeading>
        <p>
            The timeline opens on the 100 most recent moments. Use{' '}
            <span className="font-medium">Load older events</span> at the bottom to keep
            walking back, and the source chips (Deals, Activity, Email…) to search the
            customer&apos;s full history for just that kind — not only what fits in the
            first page. If a slice of HubSpot was too large to pull in full, the{' '}
            <span className="font-medium">Intelligence Gaps</span> box at the top says so
            explicitly, so a missing record is a stated limit rather than a mystery.
        </p>

        <SubHeading id="meeting-history">Meeting history comes from attendance records</SubHeading>
        <p>
            Deep Research works out when you actually met a customer — and who from their
            side was in the room — from the attendance record of each meeting, rather than
            by searching transcripts for the company&apos;s name. A meeting still counts even
            if nobody said the company name during it. You only ever see meetings you
            attended yourself.
        </p>

        <SubHeading id="sharing-with-your-team">Sharing with your team</SubHeading>
        <p>
            Every X-Ray you run also writes a link back to the customer&apos;s HubSpot
            record, in the <span className="font-medium">Cubie Research Link</span> field
            on the company. A teammate who lives in HubSpot can see that Cubie has
            researched this account and open the latest brief in one click, without
            knowing Cubie had it. The link always opens the newest research for that
            customer, so it never goes stale — and the usual access rules still apply
            when they click it.
        </p>
        <p>
            X-Rays are a <span className="font-medium">shared team asset</span> — once
            generated, your teammates can open the latest one, and a refresh only adds
            what's new since last time. The churn score is computed from CRM data (never
            made up); the write-up explains it. Rate an X-Ray with the
            <span className="font-medium"> 👍 / 😐 / 👎</span> buttons to train future ones.
            Admins can narrow who has X-Ray — if you can see this section, it's
            enabled for you.
        </p>

        <SubHeading id="every-x-ray-is-kept">Every X-Ray is kept</SubHeading>
        <p>
            Once a customer has more than one, version tabs under the name (v1, v2…) list
            each finished run and mark the latest. Click an earlier one to read it as it was
            — the churn score, signals and write-up from that run, with the timeline stopping
            on the day it ran — and <span className="font-medium">Back to latest</span>{' '}
            returns you to the newest.
        </p>
        <p>
            An earlier version is read-only: you can print it, download it or save it to
            Assets, but rating stays on the latest. Its link reopens that same version, so you
            can send it to a teammate.
        </p>
        <p>
            Numbers can skip (v2, then v4) because a run that failed or never finished still
            uses a number, and &ldquo;latest&rdquo; means the most recently finished run,
            which is usually but not always the highest number.
        </p>
        <p>
            To pass one along: <span className="font-medium">PDF</span> prints it (named after the
            customer and version), <span className="font-medium">Word</span> downloads an editable
            copy (its file name carries the version too), and
            <span className="font-medium"> Save to Assets</span> puts it on your Assets page — from
            there you can share it with a teammate or have Cubie draft (never send) a Gmail with the
            document attached.
        </p>
    </>
);
