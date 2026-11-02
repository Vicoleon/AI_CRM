import React from 'react';
import { SubHeading, TopicLink } from '../kit';

// Help → Settings. Served at /help/settings; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.

export const Settings: React.FC = () => (
    <>
        <p>
            If you have <span className="font-medium">Settings</span> in your left rail, click
            it to personalize how Cubie works for you. Its sections open in the panel beside
            the rail, so you move between them from there rather than from inside the page.
            (It is also still in the account menu at the bottom-left, if that is where you
            look first.)
        </p>
        <p>
            Settings comes with the copilot, which everyone has unless an admin has narrowed
            it — if you don&apos;t see it, nothing below applies to you.
        </p>

        <SubHeading id="general">General</SubHeading>
        <p>
            Your <span className="font-medium">timezone</span> (so your AI Priorities and daily
            briefing line up with your actual day), your{' '}
            <span className="font-medium">working hours</span> (Cubie keeps focus blocks and
            free-slot suggestions inside them), and which{' '}
            <span className="font-medium">notifications</span> reach the bell in the left sidebar
            (mute the types you don&apos;t want).
        </p>
        <p>
            If page tours are on for your team, <span className="font-medium">Page tours</span>{' '}
            is here too: untick it to stop tours from starting by themselves, or click{' '}
            <span className="font-medium">Show every tour again</span> to see each page&apos;s tour
            the next time you open it. Both take effect at once, without the Save button.
        </p>
        <p>
            If <TopicLink id="cubie-for-chrome">Cubie for Chrome</TopicLink> is on for your team,
            its card lists every browser connected to your account, with when it was connected
            and last used. <span className="font-medium">Disconnect</span> turns one off at once,
            and <span className="font-medium">Connect this browser</span> adds the one you are
            using.
        </p>

        <SubHeading id="ai-personalization">AI Personalization</SubHeading>
        <p>
            Tell Cubie who you are and how you like to work, and it shapes your Chat answers,
            AI Priorities, and meeting prep. Set your <span className="font-medium">role</span>{' '}
            and a short <span className="font-medium">“about you &amp; focus”</span>, choose
            your preferred <span className="font-medium">answer length</span> and{' '}
            <span className="font-medium">reply language</span>, and tune{' '}
            <span className="font-medium">AI Priorities</span> — a standing focus, how many
            cards, which sources to draw from, and topics to mute. You can also{' '}
            <span className="font-medium">reopen</span> priorities you previously dismissed or
            snoozed.
        </p>

        <SubHeading id="email-preferences">Email Preferences</SubHeading>
        <p>
            Set the voice Cubie uses for every email and meeting brief it drafts (tone, writing
            style, preferred greeting). “Preview my style” shows a sample without saving or
            sending. Your Google Workspace signature — including the confidentiality disclaimer
            — is added automatically to every draft Cubie creates, so you don&apos;t set it
            here.
        </p>
        <p>
            Your workspace&apos;s Sales email model also powers this preview. If that selected
            provider cannot answer, the preview shows an error so your admin can check its
            connection in Admin → Workspace.
        </p>
        <p>
            If your workspace admin enables additional Gmail accounts, connect a second work or
            personal Gmail mailbox here. Your main work mailbox remains the default. In
            Productivity → Email, choose an account to open that mailbox. Connected mailboxes
            are included automatically in your own AI Priorities and scheduled summaries, with
            the mailbox address labeled on items that cite email. Personal mail stays out of team and
            manager views. Your workspace&apos;s selected AI provider processes included mail
            to create those priorities. If your admin changes that model, automatic use of
            your extra mailbox pauses and you receive a notification. Open this card to
            review the new provider and allow inclusion again. You can still open the
            mailbox manually or disconnect it here at any time. If the selected provider
            is unavailable, automatic use stays paused until your admin restores it.
        </p>

        <SubHeading id="slack">Slack</SubHeading>
        <p>
            Connect or disconnect your Slack account. Once connected, Cubie surfaces your Slack
            @-mentions in AI Priorities and can answer Slack questions in Chat. Cubie connects{' '}
            <span className="font-medium">as you</span>, so it only ever sees the channels and
            direct messages you&apos;re already in — private channels you&apos;re not a member
            of stay hidden, and your Slack messages are never copied into the shared knowledge
            base.
        </p>
        <p>
            Connect your account here before using Cubie&apos;s <span className="font-medium">/todo</span>{' '}
            or <span className="font-medium">/ids</span> Slack commands. If your company&apos;s
            Workspace domain is removed, the connection stops working while access is unavailable.
            It works again if an admin restores that domain.
        </p>
    </>
);
