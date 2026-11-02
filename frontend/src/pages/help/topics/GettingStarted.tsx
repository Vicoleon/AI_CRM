import React from 'react';
import { SubHeading } from '../kit';

// Help → Getting started. Served at /help/getting-started; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const GettingStarted: React.FC = () => (
    <>
        <p>
            Cubie lives in your browser and answers questions about your work — your pipeline,
            documents, meetings, email and calendar — all in one place. You don&apos;t pick a
            data source; you ask a question and Cubie decides where to look and cites what it
            used.
        </p>
        <p>
            Most things work the moment you log in. A few features depend on connected accounts:
            <span className="font-medium"> Gmail, Calendar and Drive</span> come automatically
            from your Google Workspace sign-in, and <span className="font-medium">Slack</span>{' '}
            you connect once in Settings.
        </p>
        <p>
            Sign in with your company Google Workspace account. If Cubie says your workspace is
            not set up, ask your company administrator to set it up. If you used a personal
            Google account, choose <span className="font-medium">Use another account</span> and
            sign in with your company account. A paused workspace needs its administrator to
            restore access.
        </p>
        <p>
            In a developer&apos;s local preview, <span className="font-medium">Enter local preview</span>{' '}
            signs in to a locally provisioned test workspace. That option appears only in the
            development build and requires local mock authentication on the server.
        </p>
        <SubHeading id="getting-around">Getting around</SubHeading>
        <p>
            The left edge of Cubie has two parts. The narrow{' '}
            <span className="font-medium">rail</span> of icons lists the areas you have access to,
            top to bottom: <span className="font-medium">Today</span>,{' '}
            <span className="font-medium">Work</span>, <span className="font-medium">EOS</span> when your workspace has it,{' '}
            <span className="font-medium">Ask</span>, <span className="font-medium">Teams</span>,{' '}
            <span className="font-medium">Insights</span>, <span className="font-medium">Settings</span>,{' '}
            <span className="font-medium">Help</span> and, for admins,{' '}
            <span className="font-medium">Admin</span>. Click one and the{' '}
            <span className="font-medium">panel</span> beside it shows just that area&apos;s pages,
            so you only ever read the handful of destinations that belong to where you are.
        </p>
        <p>
            Click the area you are <em>already</em> in to collapse the panel and give the page
            more room; clicking that same icon again brings it back. (Clicking a{' '}
            <em>different</em> icon takes you to that area.) Cubie remembers which way you
            left it.
        </p>
        <p>
            An area with a single page &mdash; Chat &mdash; skips the panel
            altogether and gives the room to the page, since it would hold nothing but the name
            already under the icon. Not every area appears for everyone &mdash; the rail shows
            only what your access actually opens. If your workspace has not enabled EOS, its
            pages and rail area stay hidden; a shorter rail is normal, not a fault.
        </p>
        <SubHeading id="finding-help">Finding help</SubHeading>
        <p>
            Click <span className="font-medium">Help</span> in the rail and its panel lists every
            help topic, filed under the same headings as the app itself &mdash;{' '}
            <span className="font-medium">Start here</span>, <span className="font-medium">Ask
            Cubie</span>, <span className="font-medium">My Work</span>,{' '}
            <span className="font-medium">Sales</span>, <span className="font-medium">EOS &amp;
            Teams</span> and <span className="font-medium">Account</span>. Each topic is a short page of its own;
            <span className="font-medium"> Help home</span> at the top of the panel lists them
            all with a line on what each covers, and a search box to find one by name.
        </p>
        <SubHeading id="page-tours">Page tours</SubHeading>
        <p>
            The first time you open a page, Cubie may walk you through it with a few short
            cards, each pointing at the part of the page it describes. Use{' '}
            <span className="font-medium">Next</span> and <span className="font-medium">Back</span>{' '}
            to move through them, or <span className="font-medium">Skip tour</span> (or the Esc
            key) to close it. A tour you finish or skip doesn&apos;t come back by itself, on this
            computer or any other. When a page you already know gets something new, a card about
            just that feature shows the next time you open it.
        </p>
        <p>
            To see a page&apos;s tour again, open the menu under your name at the bottom left and
            choose <span className="font-medium">Replay this page&apos;s tour</span> (during a live
            L10, where the menu is hidden, use <span className="font-medium">Tour of this
            page</span> under <span className="font-medium">Exit meeting</span>; that tour never
            starts by itself, so it can&apos;t interrupt the meeting). To stop tours
            from starting by themselves, or to bring every tour back, go to{' '}
            <span className="font-medium">Settings &rarr; General &rarr; Page tours</span> (if you
            don&apos;t have Settings, the same menu under your name has{' '}
            <span className="font-medium">Turn off page tours</span>).
        </p>
    </>
);
