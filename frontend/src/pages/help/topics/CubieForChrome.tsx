import React from 'react';
import { Link } from 'react-router-dom';
import { SubHeading, TopicLink } from '../kit';

// Help → Cubie for Chrome (the extension that puts your day on Chrome's new
// tab). Served at /help/cubie-for-chrome; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.
// Gated on `chrome_extension` (helpTopics.ts), so only people who can connect
// it read this page.

export const CubieForChrome: React.FC = () => (
    <>
        <p>
            <span className="font-medium">Cubie for Chrome</span> is a small Chrome extension that
            shows your day each time you open a new tab: first the promises you made and have not
            kept yet, then this morning&apos;s top priorities. Each line opens the right page in
            Cubie, and you can tick a promise or a priority off right there. There are no pop-ups
            and no sounds.
        </p>

        <SubHeading id="what-it-shows">What the new tab shows</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li>
                <span className="font-medium">Promises past due</span> — your To-Dos from L10
                meetings that are due today or late, the items in your{' '}
                <TopicLink id="my-todos">My To-Dos</TopicLink> past their date, and the promises
                Cubie heard you make on calls and in email. Each says how many days late it is. A
                promise that sits in two of those places shows once.
            </li>
            <li>
                <span className="font-medium">Today&apos;s priorities</span> — the top of the AI
                Priorities on your <TopicLink id="daily-briefing">Daily Briefing</TopicLink>, with
                the one-line headline Cubie wrote for your day. If this morning&apos;s list is not
                ready yet, the tab says so and links to Cubie.
            </li>
        </ul>
        <p>
            What you see follows your other access: the priorities and the promises Cubie heard
            need the Cubie copilot, and the My To-Dos promises need My To-Dos.
        </p>

        <SubHeading id="connect">Connecting it</SubHeading>
        <p>
            Once your company installs the extension from the Chrome Web Store, it arrives on its
            own. Until then it is added by hand: ask your Cubie admin for the Cubie for Chrome
            package, built for your Cubie address, and unzip it. In Chrome, open{' '}
            <span className="font-medium">chrome://extensions</span>, turn on{' '}
            <span className="font-medium">Developer mode</span>, click{' '}
            <span className="font-medium">Load unpacked</span> and pick the folder you unzipped.
        </p>
        <p>
            Then open the extension, or go to{' '}
            <Link to="/chrome/connect" className="text-brand hover:underline">Connect Cubie for Chrome</Link>{' '}
            in Cubie, and press <span className="font-medium">Connect this browser</span>. Cubie
            says what the extension will show and what it can&apos;t see before you agree. Connect
            each browser you use once.
        </p>
        <p>
            If a browser was connected before workspace-bound keys were introduced, open{' '}
            <Link to="/chrome/connect" className="text-brand hover:underline">Connect Cubie for Chrome</Link>{' '}
            and press <span className="font-medium">Connect this browser</span> again.
            The earlier browser key no longer works.
        </p>

        <SubHeading id="toolbar-number">The number on the Cubie icon</SubHeading>
        <p>
            The Cubie icon in Chrome&apos;s toolbar shows how many of your promises are past due
            (9+ for more than nine). Nothing past due, no number.
        </p>

        <SubHeading id="out-of-your-way">Keeping it out of your way</SubHeading>
        <p>
            Right-click the Cubie icon and choose <span className="font-medium">Options</span>:
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li>Shrink the new tab to <span className="font-medium">one line</span> under the search box, or show your promises only</li>
            <li>Turn off the number on the Cubie icon</li>
            <li>Blur the details until you point at them, for when you share your screen</li>
        </ul>
        <p>
            <span className="font-medium">Hide for today</span>, at the bottom of the new tab,
            hides Cubie until tomorrow.
        </p>

        <SubHeading id="ask-cubie">Ask Cubie from anywhere in Chrome</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li>
                <span className="font-medium">From the address bar</span> — type{' '}
                <span className="font-medium">cubie</span>, then a space, then your question, and
                press Enter. <TopicLink id="chat">Ask Cubie</TopicLink> opens and asks it.
            </li>
            <li>
                <span className="font-medium">About something on a page</span> — select the text,
                right-click and choose <span className="font-medium">Ask Cubie about…</span>.
            </li>
        </ul>
        <p>
            The question is sent as a chat message, exactly as if you had typed it, and is kept in
            your chat history like any other. These two shortcuts are only in the version loaded
            by hand. The version your company installs from the Chrome Web Store leaves them out
            for now: Chrome Web Store rules let a new tab extension add only the new tab and its
            toolbar button.
        </p>

        <SubHeading id="what-it-cannot-see">What it can&apos;t see</SubHeading>
        <p>
            It only shows what Cubie sends it. It cannot read your email, your browsing history or
            any other website. A selected-text question goes to Cubie only when you choose Ask
            Cubie.
        </p>
        <p>
            It never asks Cubie to make new priorities: the new tab reads the list Cubie already
            made this morning, so opening as many tabs as you like costs nothing.
        </p>

        <SubHeading id="disconnect">Disconnecting</SubHeading>
        <p>
            Every connected browser is listed in{' '}
            <Link to="/settings?tab=general" className="text-brand hover:underline">Settings → General</Link>,
            under <span className="font-medium">Cubie for Chrome</span>, with when it was connected
            and last used. <span className="font-medium">Disconnect</span> turns one off at once.
            A connection also lapses after 90 days (connect that browser again to keep it going),
            and every connection ends when someone leaves your company.
        </p>
    </>
);
