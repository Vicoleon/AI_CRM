import React from 'react';
import { Callout, SubHeading } from '../kit';

// Help → Smart Teams. Served at /help/smart-teams; Chat cites the same id from
// rubik_core/help/content.py, so a change here travels there in the same PR.
//
// Smart Teams is company-wide, so it is deliberately NOT in the
// gating filter above — but Compare and Team Builder inside it are
// copilot-gated, and the copy says who can use them rather than
// leaving a reader wondering why they have no such panel entry.
// The backend mirror in rubik_core/help/content.py says the same,
// word for word in substance, because Chat answers how-to
// questions from that copy and a fact in only one home is a
// half-shipped feature. Anything not reachable is still called out
// as such — an unbuilt feature described as available is worse than
// a gap.

export const SmartTeams: React.FC = () => (
    <>
        <p>
            <span className="font-medium">Smart Teams</span> is your work-style profile —
            a “How to work with me” guide built from your own answers, so colleagues can
            get the best out of working with you without having to guess.
        </p>
        <p>
            It starts with the <span className="font-medium">Work Style Interview</span>{' '}
            under <span className="font-medium">Teams</span> in the left rail. Part of it is
            quick statements you rate on a scale (you can answer those straight from the
            keyboard — press the number, and it jumps to the next one), and part of it is
            a short conversation where you tell Cubie about work you enjoy, in your own
            words. It takes a little over half an hour end to end.
        </p>
        <SubHeading id="taking-the-interview">Taking the interview</SubHeading>
        <p>
            <span className="font-medium">You do not have to do it in one sitting.</span>{' '}
            Every answer is saved as you give it. Close the tab, come back tomorrow, or
            get signed out half way through — Cubie puts you back on the statement you
            were on, with everything you had already answered still filled in. If a save
            ever fails you will see it said out loud, with a Retry button; answers are
            never quietly dropped.
        </p>
        <p>
            When you finish, Cubie works through your answers in the background. That
            takes a few minutes and you can leave the page — your profile is not ready
            until it says it is, and the page will tell you plainly rather than showing
            you an empty one.
        </p>
        <SubHeading id="my-guide">My Guide — writing it, and deciding who sees it</SubHeading>
        <p>
            Once your answers are scored, open <span className="font-medium">My Guide</span>{' '}
            under <span className="font-medium">Teams</span> in the left rail and press
            Generate. Cubie writes a “How to work with me” guide from your own answers — how
            to reach you, what gets the best out of you, what drains you, how to give you
            feedback — and it takes a couple of minutes.
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li>
                <span className="font-medium">Nobody sees the draft but you.</span> It stays
                private until you press Approve, which is what publishes it. Not even an
                admin can publish it for you — approving is your consent, and there
                is deliberately no path that lets anyone else give it.
            </li>
            <li>
                <span className="font-medium">Unpublish whenever you want.</span> Colleagues
                stop being able to open it the moment you do. Approving also lets Cubie Chat
                answer from it (“how should I approach a first call with…”); removing it
                from Chat is a background step, so for a short while after you unpublish
                Chat may still quote it. The page tells you which state it is in rather than
                claiming it is done.
            </li>
            <li>
                <span className="font-medium">Download it as a Word document</span> if you
                want to keep or share a copy yourself.
            </li>
            <li>
                <span className="font-medium">Retake, and regenerate.</span> A retake makes a
                new version, which marks your published guide out of date — the page says so
                and offers Regenerate. Your published wording is never overwritten
                underneath you; the new one needs approving again.
            </li>
            <li>
                <span className="font-medium">Reading a colleague&apos;s guide</span> works
                the same way in reverse: you can open anyone&apos;s guide once they have
                published it, and you cannot tell whether someone has an unpublished draft.
            </li>
        </ul>
        <SubHeading id="my-skills">My Skills — what you can be asked about</SubHeading>
        <p>
            <span className="font-medium">My Skills</span> is the other half: a short list
            of things people can come to you for. You add them from a curated list (search
            it — “gdpr” finds “Data privacy (GDPR / CCPA)”) and optionally say how deep
            you are on each one. Cubie also suggests skills from{' '}
            <span className="font-medium">your own activity at work</span> — meetings
            you were in and documents you uploaded — and nothing outside your company is ever
            looked at. Each suggestion shows what it was based
            on, and that reasoning is private to you.
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li>A suggestion is invisible to colleagues until you confirm it.</li>
            <li>
                Say no and it stays no — Cubie will not suggest it again. That is also how
                you take back a skill you added yourself.
            </li>
            <li>
                Colleagues see only what you confirmed, with no evidence attached. “Check
                for suggestions” asks Cubie to look again at your recent work.
            </li>
        </ul>
        <SubHeading id="official-assessment-report">Already have an official assessment report?</SubHeading>
        <p>
            Optional, and most people don&apos;t have one. If you hold an official DISC
            report, you can upload the PDF from My Guide and Cubie reads the scores out of
            it. It never changes anything on its own: it shows you what it found, you
            decide, and confirming replaces just that one part of your profile and leaves
            the rest alone.
        </p>
        <p>
            Because your profile then changes, your published guide comes
            out of company view until you regenerate and approve it — the page says so at
            the point you confirm. Nothing is waiting on you if you never upload anything.
        </p>
        <SubHeading id="compare-and-team-builder">Compare and Team Builder</SubHeading>
        <p>
            Compare, Team Builder and <span className="font-medium">Find People</span> come
            with the copilot, which everyone has unless an admin has narrowed it. If they do
            not appear in your <span className="font-medium">Teams</span> panel, the copilot
            isn&apos;t on for you; everything above is yours either way.
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li>
                <span className="font-medium">Compare</span> gives you a working-relationship
                guide about one colleague, written to you: where you two will click, where
                you will grate, and what to do about it. It needs{' '}
                <span className="font-medium">both</span> of you to have published your own
                guide first — including you. It is only ever about you and one other person;
                there is no way to ask for a comparison of two other people, by design.
            </li>
            <li>
                <span className="font-medium">Team Builder</span> takes the skills a piece of
                work needs, plus dates if it matters, and proposes a group: who covers what,
                who is away, and what the group&apos;s mix of working styles adds up to. It
                never ranks or scores anybody, and an honest “nobody has confirmed that
                skill” is an answer it will give. You can also just ask Chat for a team.
            </li>
            <li>
                <span className="font-medium">Find people by skill</span> is the other
                direction: name what a project needs and see everyone who told us they do
                it. Everyday wording works (&ldquo;gdpr&rdquo; finds data privacy), up to
                six skills at a time. It lists <span className="font-medium">everybody</span>{' '}
                who said yes, in name order — not a shortlist, and with no &ldquo;best
                match&rdquo;, because it is not a ranking of people. It shows only what
                people confirmed about themselves, and if you type something the skills list
                does not know it says so rather than showing an empty result —
                &ldquo;nobody has this&rdquo; and &ldquo;we do not track this&rdquo; are
                different answers.
            </li>
        </ul>
        <SubHeading id="seeing-where-people-overlap">Seeing where people overlap</SubHeading>
        <p>
            Three screens now draw the picture rather than only describing it.{' '}
            <span className="font-medium">My Guide</span> shows your own shape on
            each scale — and because it is your own profile, it shows your actual
            numbers.{' '}
            <span className="font-medium">Compare</span> puts you and one colleague
            side by side and marks each dimension as{' '}
            <span className="font-medium">Same</span>,{' '}
            <span className="font-medium">Close</span> or{' '}
            <span className="font-medium">Apart</span>, so you can see at a glance
            where you will click and where you are most likely to grate.{' '}
            <span className="font-medium">Team Builder</span> adds a who-covers-what
            grid, which shows where a team doubles up and where one person is
            carrying a skill alone.
        </p>
        <p>
            All three also show a <span className="font-medium">map</span> — a circle
            with the four DISC letters on it, and a dot for each person. Two axes: how
            much someone pushes for progress, and how much they work through people. The
            dot is placed from <span className="font-medium">both</span> your working
            style and your temperament, because both of those describe the same two
            things. What drives you and your Enneagram type are deliberately left out —
            they are not positions on these axes, and your motivations in particular only
            mean something next to your own others rather than next to a colleague's.
        </p>
        <p>
            On a team map, dots bunched together mean the
            group will agree quickly and miss the same things; spread out means more
            friction and better coverage. There is no ideal spot on the circle, nobody is
            ranked by where they land, and how far out a dot sits carries no meaning — only
            which direction. Two people with the same style sit on the same spot, which is
            the point.
        </p>
        <ul className="list-disc pl-5 space-y-1">
            <li>
                On Compare you see <span className="font-medium">words, not
                numbers</span> — yours and theirs. Your figures stay yours, and
                theirs stay theirs.
            </li>
            <li>
                A hatched <span className="font-medium">Not measured</span> cell is
                not agreement. It means one of you has nothing reportable on that
                dimension, so there was nothing to compare — a different thing from
                the two of you matching.
            </li>
            <li>
                The written guide ends with{' '}
                <span className="font-medium">What this guide cannot speak to</span> —
                the questions Cubie could not answer because one or both profiles are
                missing the part behind them. It is listed so a gap does not read as
                &quot;nothing found here&quot;, and it names somebody only when they
                have something they can actually finish: a section closed because it
                was declined, or because it is derived from another section, leaves
                nothing to do and so names nobody.
            </li>
            <li>
                There is <span className="font-medium">no compatibility score</span>
                , and there never will be. The grid counts dimensions and names
                them; it does not add them up into a verdict about a relationship.
            </li>
            <li>
                <span className="font-medium">What drives you</span> appears only on
                your own guide. It is an order rather than a level — which of your
                motivations wins when two of them pull against each other — so it
                only means something next to your own others, and comparing it
                between two people would not be a comparison at all.
            </li>
            <li>
                The <span className="font-medium">coloured ring</span> around a dot is
                what that person brings to a piece of work — Sense-Making, Momentum,
                Connection or Stewardship. Each one always sits in the same place on the
                ring (top, right, bottom, left), so you can read it without relying on
                the colour. No ring at all means their strengths section is closed, not
                that they cover none of them.
            </li>
            <li>
                Somebody with no dot is{' '}
                <span className="font-medium">not placed</span>, not in the middle. Their
                answers did not support a working style, so there is no position to draw —
                they are still listed beside the map.
            </li>
            <li>
                Compare&apos;s grid needs <span className="font-medium">both</span>{' '}
                of you to have published your own guide, the same as the written
                guide. It does not need anyone to have generated that guide first,
                so you can look before you ask for one.
            </li>
        </ul>
        <SubHeading id="privacy-and-how-it-is-used">Privacy and how it is used</SubHeading>
        <Callout tone="info">
            <span className="font-medium">Your raw answers stay yours.</span> The
            interview transcript and the numeric scores behind it are visible only to you
            and to the admins who support Smart Teams — every admin unless your company
            has handed support to one team's admins in Admin → Feature Access (Smart Teams
            support view) — and never to your manager or the Exec team for being senior.
            They get there from a{' '}
            <span className="font-medium">Support: raw profile lookup</span> link on My
            Work Style: it is read-only, every lookup is logged against the admin who ran
            it, and it is there to fix a problem{' '}
            <span className="font-medium">with</span> the person it belongs to — not to
            read about them. Your guide becomes company-visible only after you have read it and
            approved it, and you can unpublish it at any time. Colleagues never see a
            number, a percentile, or a raw score of yours anywhere in Smart Teams. A
            Compare guide is never searchable in Chat and has no download, because a saved
            file would outlive the moment either of you unpublishes.
        </Callout>
        <Callout tone="warn">
            <span className="font-medium">Advisory only.</span> Smart Teams exists to help
            people work together. It is not used for performance reviews, promotion,
            compensation, or deciding whether someone is right for a role, and it never
            will be. It is also self-given only — Cubie does not go looking for
            information about colleagues to build these, and nobody can add a skill, a
            guide, or an answer on your behalf.
        </Callout>
        <SubHeading id="missing-a-skill">Missing a skill from the list?</SubHeading>
        <p>
            The admins who support Smart Teams can add one — there is a{' '}
            <span className="font-medium">Curate the list</span> link on My Skills for them
            — and it is usable straight away, without waiting for a release. Ask rather than
            picking the nearest wrong thing: the list is shared, so a skill stored eleven
            different ways stops Find People and Team Builder working. Skills can be retired
            but never deleted, because somebody may already have claimed them.
        </p>
        <SubHeading id="limits-worth-knowing">Limits worth knowing</SubHeading>
        <p>
            The curated skills list is loaded as a setup step, so if it looks empty it is
            not in place rather than having no match for you — the page says which, and it
            is worth telling an admin rather than waiting. Skill suggestions are worked out
            overnight and whenever you press <span className="font-medium">Take a fresh
            look</span>. The nightly pass only re-reads people whose meetings or
            documents actually changed, so a quiet week legitimately adds nothing — press
            the button if you want it looked at now.
        </p>
        <p>
            Only official <span className="font-medium">DISC</span>{' '}
            reports can be read today; other assessments are recognised and told to you,
            but cannot be merged. And a Team Builder answer is not saved — it is the answer
            to the question you just asked, so copy anything you want to keep.
        </p>
    </>
);
