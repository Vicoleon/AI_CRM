// Suggestions Cubie has made about the person reading, and not yet answered.
//
// A SUGGESTION IS A QUESTION, NOT A SKILL
// ---------------------------------------
// Everything about this section is built to keep that true. It is a separate
// section from the confirmed list, in a different colour, with cards rather than
// chips, and every card states in words that the skill is not on the profile and
// that no colleague can see it. None of those four is load-bearing on its own —
// together they mean there is no reading of this screen where Cubie's guess
// looks like something the person claimed.
//
// The two buttons are one click each and both are terminal. "Yes" is
// `POST /skills/{key}/confirm`, which flips the existing row; it never creates
// one, which is why there is nothing here that looks like adding a skill. "No"
// is `POST /skills/{key}/deny`, which writes a tombstone the sweep reads — so
// the copy says the suggestion will not come back, because it will not.
import React from 'react';
import { Check, Loader2, Sparkles, X } from 'lucide-react';

import type { PrivateSkill } from '../../../api/smartTeams';
import { EvidenceList } from './EvidenceList';
import { QuietEmpty, SectionCard } from './skillsShared';
import { levelLabel } from './skillsFormat';

interface SuggestionQueueProps {
    suggestions: PrivateSkill[];
    /** `_own_payload.suggestion_notice`, verbatim. It says where suggestions come
     *  from and that nobody else can see one — the first question anybody asks,
     *  answered in the server's words rather than a paraphrase written here. */
    notice: string;
    /** Skill keys with a confirm or deny in flight. */
    busyKeys: Set<string>;
    onConfirm: (skillKey: string) => void;
    onDeny: (skillKey: string) => void;
    /** Fire a fresh sweep of the reader's own activity, and re-read afterwards. */
    onRefresh: () => void;
    refreshing: boolean;
    /** True once a refresh has been dispatched this visit. There is no per-run
     *  poll on the API, so this is the only honest thing to say about it. */
    refreshDispatched: boolean;
    /** Re-read `GET /skills`. The refresh is a background job with no progress to
     *  watch, so checking again is a deliberate action rather than a spinner. */
    onReload: () => void;
    reloading: boolean;
}

export const SuggestionQueue: React.FC<SuggestionQueueProps> = ({
    suggestions,
    notice,
    busyKeys,
    onConfirm,
    onDeny,
    onRefresh,
    refreshing,
    refreshDispatched,
    onReload,
    reloading,
}) => (
    <SectionCard
        title="Cubie has a few suggestions for you"
        icon={<Sparkles size={17} />}
        count={suggestions.length}
        tone="suggestion"
        subtitle={
            notice.trim() ? (
                <p>{notice}</p>
            ) : (
                <p>
                    These are suggestions from your own internal activity. Nothing here is on
                    your profile, and no colleague can see a suggestion until you say yes.
                </p>
            )
        }
        right={
            <div data-tour="smart-teams-skills-suggestions" className="flex flex-shrink-0 flex-wrap items-center gap-2">
                <button
                    type="button"
                    onClick={onRefresh}
                    disabled={refreshing}
                    className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-60"
                >
                    {refreshing && <Loader2 className="animate-spin" size={12} />}
                    Take a fresh look
                </button>
                <button
                    type="button"
                    onClick={onReload}
                    disabled={reloading}
                    className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-60"
                >
                    {reloading && <Loader2 className="animate-spin" size={12} />}
                    Check for new ones
                </button>
            </div>
        }
    >
        {/* The refresh endpoint returns 202 and there is no run to poll, so this
            says exactly that instead of implying a result is arriving. */}
        {refreshDispatched && (
            <p className="mb-3 rounded-md border border-indigo-200 bg-white p-3 text-xs leading-relaxed text-gray-700">
                Cubie is taking a fresh look at your own internal activity. That runs in the
                background and there is nothing to watch — come back in a few minutes and press
                “Check for new ones”. If nothing new turns up, there was nothing new to find:
                Cubie does not re-suggest anything you have already said no to, and an unchanged
                set of meetings and documents produces the same answer.
            </p>
        )}

        {suggestions.length === 0 ? (
            /* Not an error and not a to-do. A brand-new account has had no sweep
               yet, and a long-standing one can legitimately have nothing new —
               the sweep skips a person whose evidence is unchanged since the last
               run. Both are finished states. */
            <QuietEmpty title="Nothing waiting on you.">
                <p>
                    Cubie has no unanswered suggestions for you. It looks at meetings you were
                    in and documents you uploaded, and
                    finding nothing to suggest is a perfectly ordinary result — as is having
                    already answered everything it found.
                </p>
                <p>
                    You do not need suggestions to have skills on your profile. Add your own
                    below whenever you like.
                </p>
            </QuietEmpty>
        ) : (
            <ul className="space-y-3">
                {suggestions.map(skill => {
                    const busy = busyKeys.has(skill.skill_key);
                    return (
                        <li
                            key={skill.skill_key}
                            className="rounded-lg border border-indigo-200 bg-white p-4"
                        >
                            <div className="flex flex-wrap items-start justify-between gap-3">
                                <div className="min-w-0">
                                    <p className="flex flex-wrap items-center gap-2">
                                        <span className="text-sm font-medium text-gray-900">
                                            {skill.name}
                                        </span>
                                        {/* The word "suggestion" is on the card,
                                            not only in the section heading —
                                            somebody skim-reading one row must
                                            still be able to tell. */}
                                        <span className="inline-flex items-center gap-1 rounded-full border border-indigo-300 bg-indigo-100 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-indigo-900">
                                            <Sparkles size={10} />
                                            Suggestion
                                        </span>
                                    </p>
                                    <p className="mt-1 text-xs text-gray-500">
                                        {skill.cluster ?? 'Other'}
                                        {skill.level ? ` · ${levelLabel(skill.level)}` : ''}
                                    </p>
                                    <p className="mt-1.5 text-xs leading-relaxed text-gray-600">
                                        Not on your profile. Nobody else can see this unless you
                                        say yes.
                                    </p>
                                    <EvidenceList
                                        items={skill.evidence}
                                        rationale={skill.rationale}
                                        skillName={skill.name}
                                    />
                                </div>
                                <div className="flex flex-shrink-0 items-center gap-2">
                                    <button
                                        type="button"
                                        onClick={() => onConfirm(skill.skill_key)}
                                        disabled={busy}
                                        className="inline-flex items-center gap-1.5 rounded-md bg-brand px-3 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-1 disabled:opacity-60"
                                    >
                                        {busy ? (
                                            <Loader2 className="animate-spin" size={13} />
                                        ) : (
                                            <Check size={13} />
                                        )}
                                        Yes, that&rsquo;s me
                                        <span className="sr-only"> — confirm {skill.name}</span>
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => onDeny(skill.skill_key)}
                                        disabled={busy}
                                        className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-1 disabled:opacity-60"
                                    >
                                        {busy ? (
                                            <Loader2 className="animate-spin" size={13} />
                                        ) : (
                                            <X size={13} />
                                        )}
                                        No
                                        <span className="sr-only">
                                            {' '}
                                            — Cubie will not suggest {skill.name} again
                                        </span>
                                    </button>
                                </div>
                            </div>
                        </li>
                    );
                })}
            </ul>
        )}
    </SectionCard>
);
