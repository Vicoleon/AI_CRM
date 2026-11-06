import React from 'react';

// Help → CRM Sync. Served at /help/crm-sync; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const CrmSync: React.FC = () => (
    <>
        <p>
            <span className="font-medium">CRM Sync</span> reads your recent external call
            transcripts, compares what was said against what HubSpot actually holds, and
            suggests the updates your CRM is missing — a contact who isn't in there yet, a
            deal amount or close date that moved on the call.
        </p>
        <p>
            Click <span className="font-medium">Scan my calls</span> and Cubie works through
            your last week of meetings in the background (a couple of minutes — you can
            leave the page). Each suggestion arrives as a card showing the exact
            before → after change, a quote from the call as evidence, and how confident
            Cubie is.
        </p>
        <p>
            If your workspace uses its own AI provider and that provider fails, the scan
            shows an error. Ask a workspace admin to check the connection under Admin →
            Access → Google Workspace → AI provider credentials, then try again.
        </p>
        <p className="font-medium">Nothing is ever written to HubSpot on its own.</p>
        <p>
            You approve each item individually, and only what you approve is written. You
            can <span className="font-medium">Edit</span> a value first if Cubie got the
            details slightly wrong, or <span className="font-medium">Dismiss</span> it — a
            dismissed suggestion stays out of your way for a month. If a deal moved in
            HubSpot since the scan, Cubie refuses the change and re-shows the card against
            the current state, so you're never approving a stale picture.
        </p>
        <p>
            A few limits worth knowing: only <span className="font-medium">your own</span>{' '}
            meetings are read (the same permissions as everywhere else in Cubie), internal
            meetings are skipped entirely, deal changes are limited to deals you own in
            the company's own pipelines (and wait until whoever runs your Cubie deployment has
            named which HubSpot pipelines may be written to), and Cubie only ever touches a short
            list of fields — names, phone, job title, deal amount, stage, and close date. It
            never logs notes or engagements on a record. Rate suggestions with the{' '}
            <span className="font-medium">👍 / 😐 / 👎</span> buttons to make future ones
            sharper.
        </p>
    </>
);
