import React from 'react';
import { Link } from 'react-router-dom';
import { SubHeading } from '../kit';

// Help → Voice commands. Served at /help/voice-commands; Chat cites the same id
// from rubik_core/help/content.py, so a change here travels there in the same PR.

export const VoiceCommands: React.FC = () => (
    <>
        <p>
            Run Cubie by speaking. Press <span className="font-medium">Voice</span> (the microphone above
            Notifications in the sidebar), or the keyboard shortcut — <span className="font-medium">Option+Command
            (⌥⌘)</span> on a Mac, <span className="font-medium">Ctrl+Alt</span> on a PC — and say what you want.
            It works on every page, including during an L10.
        </p>
        <SubHeading id="giving-a-command">Giving a command</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Tap</span> the shortcut (or click Voice), speak, then tap it again or press <span className="font-medium">Enter</span>.</li>
            <li>Or <span className="font-medium">hold</span> the shortcut while you talk and let go when you are done.</li>
            <li><span className="font-medium">Esc</span> throws it away. A recording stops by itself after 30 seconds and nothing is done with it.</li>
        </ul>
        <SubHeading id="what-you-can-say">What you can say</SubHeading>
        <ul className="list-disc pl-5 space-y-1">
            <li><span className="font-medium">Open a page</span> — <em>&ldquo;Open my work&rdquo;</em>, <em>&ldquo;take me to Rocks&rdquo;</em>, <em>&ldquo;open today&apos;s calls&rdquo;</em>, or what you want to do: <em>&ldquo;I want to write a proposal&rdquo;</em>. Cubie only opens pages in your own menu.</li>
            <li><span className="font-medium">Open one record</span> — say what it is about: <em>&ldquo;open Acme&apos;s X-Ray&rdquo;</em>, <em>&ldquo;open Thursday&apos;s Sales L10&rdquo;</em>, <em>&ldquo;prep for my 2pm&rdquo;</em>, <em>&ldquo;open the Globex proposal&rdquo;</em>, <em>&ldquo;open my chat about pricing&rdquo;</em>, <em>&ldquo;open Jordan&apos;s work style guide&rdquo;</em>. Only records you can already open; when several fit, Cubie lists them. An L10 always asks first, because opening it starts the meeting.</li>
            <li><span className="font-medium">Ask a question</span> — <em>&ldquo;What did Jordan promise Globex last week?&rdquo;</em> goes to <Link to="/chat" className="text-brand underline">Ask Cubie</Link> and is sent as your message. So does anything else Ask Cubie does for you: a draft email, a meeting, or your own My To-Dos.</li>
            <li><span className="font-medium">Change EOS</span> — add a Rock (<em>&ldquo;Add a Rock called Launch the client portal for Q4&rdquo;</em>), mark a Rock on track, off track or done, add a To-Do (<em>&ldquo;Add a To-Do for Jordan to send the deck by Friday&rdquo;</em>), mark a To-Do done, add an Issue, or record a Scorecard number (<em>&ldquo;Log 92 percent for utilization this week&rdquo;</em>).</li>
        </ul>
        <SubHeading id="confirming-a-change">Confirming a change</SubHeading>
        <p>
            Nothing in EOS changes until you say so. Cubie shows a card with
            exactly what it will do — the Rock, its quarter, owner and team; the To-Do&apos;s owner and due
            date; and the number and, if one is already there, the number it replaces. Press <span className="font-medium">Enter</span> or the
            button to go ahead, or press the shortcut again and say <span className="font-medium">&ldquo;yes&rdquo;</span>.
            <span className="font-medium"> Esc</span> or &ldquo;no&rdquo; cancels.
        </p>
        <p>
            The change is made exactly as the page&apos;s own button makes it, as you, so you can only change
            what you could change by clicking. When your words fit more than one Rock, To-Do, measurable or
            person, Cubie lists them and asks which one; when they fit none, it says so instead of guessing.
            Unless you say otherwise, a Rock or To-Do is yours, a To-Do is due a week from today, an Issue or
            a To-Do goes to the team the page&apos;s own form picks first (the Exec team, for an exec), and a Rock
            goes to its owner&apos;s team. Say the team (&ldquo;…for the Sales team&rdquo;) to choose another.
            Scorecard numbers are weekly ones only; a measurable kept every other week is recorded on the
            Scorecard page.
        </p>
        <SubHeading id="your-shortcut">Your shortcut</SubHeading>
        <p>
            Change it, or turn it off, in{' '}
            <Link to="/settings?tab=general" className="text-brand underline">Settings → General</Link>: press
            <span className="font-medium"> Change</span> and then the keys you want. A Mac and a PC each keep their
            own. If another app on your computer already uses your keys, pick different ones.
        </p>
        <SubHeading id="privacy">Privacy</SubHeading>
        <p>
            The recording is never stored; only what it leads to is kept (the chat message, or the change
            you confirmed). A recording in which nobody spoke never reaches the AI. If Cubie takes too long
            to work out a command, it says so and nothing is done; just say it again.
        </p>
    </>
);
