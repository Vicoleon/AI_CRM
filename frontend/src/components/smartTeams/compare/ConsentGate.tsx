// The consent gate, explained.
//
// THIS IS THE STATE MOST PEOPLE WILL HIT, AND IT IS A DEAD END ON PURPOSE
// ----------------------------------------------------------------------
// A pair guide needs a PUBLISHED personal working guide from both people.
// `comparison.refuse_generation` is the rule and its docstring is the argument:
// generating writes one person's profile content into a cached row a colleague
// may read, and the only consent Cubie holds for describing somebody is that
// they published their own guide.
//
// So this screen has exactly two jobs: say plainly why there is nothing to read,
// and — where the missing piece is the reader's OWN — offer the one action that
// is theirs to take.
//
// WHAT IT DELIBERATELY DOES NOT DO
// -------------------------------
//  1. **No nudge.** No "remind them", no "ask them to publish", no mailto, no
//     Slack hand-off, and no wording that suggests the reader go and do it by
//     other means. Somebody's decision not to publish a description of
//     themselves is not a task item, and a button here would turn every
//     unpublished profile into social pressure from a colleague.
//  2. **No accusation.** Nothing here says or implies the other person is
//     withholding. The sentence is about what Cubie holds, not about a choice
//     anyone made.
//  3. **No disclosure of the other person's state.** The API hands this page
//     enough to work it out — a successful read with `can_generate: false` and
//     the reader's own guide published leaves only one explanation, and
//     `current_profile_versions` names both people's profile versions outright.
//     None of that is rendered. The reader is told about the reader; the joint
//     requirement is stated as a joint fact. Where somebody else is with their
//     own profile is theirs to say, and inferring it out loud would make this
//     screen a way to probe colleagues one address at a time.
import React from 'react';
import { Link } from 'react-router-dom';
import { Lock } from 'lucide-react';

// ── What the caller knows about ITSELF ───────────────────────────────────────

export interface OwnReadiness {
    /** `getOwnGuide().guide?.published`. `null` when the own-guide read has not
     *  landed or failed — then this screen says nothing about the reader either,
     *  which is the honest default. */
    published: boolean | null;
    /** `getOwnGuide().state === 'none'` — nothing generated at all yet. */
    noGuideYet: boolean;
    /** `getOwnGuide().current_profile_version === null` — the interview has not
     *  been scored, so there is nothing to write a guide from. */
    profileMissing: boolean;
}

export interface ConsentGateProps {
    /** Display name for the colleague, from the API's `display_names` where
     *  there is one, otherwise the address the reader typed. */
    otherName: string;
    /** Why this screen is showing.
     *
     *  `read_blocked` — the 403 from `GET /comparison/pair/{other}`. The read
     *  gate exempts the reader's own publish state, so this one is about the
     *  colleague's.
     *
     *  `cannot_generate` — the read was allowed and `can_generate` is false. The
     *  generate gate is the stricter one and includes the reader. */
    reason: 'read_blocked' | 'cannot_generate';
    /** The server's own refusal sentence
     *  (`authz.SHARED_GUIDE_NOT_PUBLISHED_DENIED`), rendered verbatim when
     *  there is one. */
    refusal?: string | null;
    own: OwnReadiness;
}

export const ConsentGate: React.FC<ConsentGateProps> = ({
    otherName,
    reason,
    refusal,
    own,
}) => {
    // The reader's own missing piece, if any. Ordered: no scored profile is
    // upstream of no guide, which is upstream of an unpublished one.
    const ownAction = own.profileMissing
        ? 'interview'
        : own.noGuideYet
          ? 'generate'
          : own.published === false
            ? 'publish'
            : null;

    return (
        <div className="space-y-4">
            <div className="rounded-xl border border-gray-200 bg-white p-5">
                <div className="mb-2 flex items-center gap-2">
                    <Lock size={16} aria-hidden="true" className="text-gray-500" />
                    <h2 className="text-base font-semibold text-gray-900">
                        There is no guide to read for you and {otherName}
                    </h2>
                </div>

                <p className="text-sm leading-relaxed text-gray-700">
                    A working-relationship guide is written from two published working
                    guides. Cubie does not have a published guide from both of you, so it
                    has not written one.
                </p>

                {refusal && (
                    <p className="mt-3 rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm leading-relaxed text-gray-700">
                        {refusal}
                    </p>
                )}

                <p className="mt-3 text-sm leading-relaxed text-gray-600">
                    Publishing a working guide is each person's own decision, and Cubie
                    does not report where anyone else is with theirs. Nothing about this
                    is a judgement of anybody, and there is nothing on this screen that
                    can change it for somebody else.
                </p>

                {reason === 'cannot_generate' && (
                    <p className="mt-3 text-sm leading-relaxed text-gray-600">
                        Writing one has a stricter rule than reading one: it needs a
                        published guide from both people, including you.
                    </p>
                )}
            </div>

            {/* The reader's own half, and only the reader's own half. */}
            {ownAction && (
                <div className="rounded-xl border border-brand/25 bg-brand-tint/40 p-5">
                    <h3 className="mb-1.5 text-sm font-semibold text-brand-hover">
                        Your side of it
                    </h3>
                    {ownAction === 'interview' && (
                        <>
                            <p className="text-sm leading-relaxed text-gray-700">
                                You have not finished the Work Style Interview, so there is
                                nothing for Cubie to write your working guide from yet.
                            </p>
                            <Link
                                to="/smart-teams/interview"
                                className="mt-3 inline-flex items-center rounded-md bg-brand px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2"
                            >
                                Go to the interview
                            </Link>
                        </>
                    )}
                    {ownAction === 'generate' && (
                        <>
                            <p className="text-sm leading-relaxed text-gray-700">
                                You have not generated your own working guide yet. Nobody
                                sees it until you approve it.
                            </p>
                            <Link
                                to="/smart-teams/guide"
                                className="mt-3 inline-flex items-center rounded-md bg-brand px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2"
                            >
                                Go to My Guide
                            </Link>
                        </>
                    )}
                    {ownAction === 'publish' && (
                        <>
                            <p className="text-sm leading-relaxed text-gray-700">
                                Your own working guide is written but not published.
                                Publishing it is what lets Cubie write comparisons that
                                include you — and you can unpublish it again at any time.
                            </p>
                            <Link
                                to="/smart-teams/guide"
                                className="mt-3 inline-flex items-center rounded-md bg-brand px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2"
                            >
                                Go to My Guide
                            </Link>
                        </>
                    )}
                </div>
            )}
        </div>
    );
};

export default ConsentGate;
