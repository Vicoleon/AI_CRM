// The skills on the reader's profile — the ones colleagues can see.
//
// TWO ORIGINS, ONE LIST, AND THE BADGE SAYS WHICH
// -----------------------------------------------
// `declared` and `accepted_suggestions` are both confirmed and both
// colleague-visible, so they belong in one list: splitting them into two boxes
// here would suggest they carry different weight to the company, and they do not.
// What differs is where each came from, which is what `OriginBadge` says on every
// row. Nothing unconfirmed appears in this list at all — a pending suggestion
// lives in `SuggestionQueue`, on the other side of a click.
//
// Evidence IS available on these rows (they are `PrivateSkill`, this is the
// subject's own screen), and it is shown only on the ones Cubie suggested. On a
// skill the person typed there is nothing to show: `rationale` is `''` and
// `evidence` is empty by construction, so an "explain this" affordance there
// would open onto nothing.
//
// The only write is "Take this off my profile", which is `POST /{key}/deny` —
// the same tombstone the suggestion queue writes. There is no delete endpoint
// and that is deliberate: a delete would let the next sweep read the same
// meeting, find nothing on file and suggest it again.
import React from 'react';
import { BadgeCheck, Loader2, X } from 'lucide-react';

import type { PrivateSkill } from '../../../api/smartTeams';
import { EvidenceList } from './EvidenceList';
import { OriginBadge, QuietEmpty, SectionCard } from './skillsShared';
import { byCluster, levelLabel, shortDate } from './skillsFormat';

interface ConfirmedSkillsProps {
    /** `declared` and `accepted_suggestions` concatenated by the page. */
    skills: PrivateSkill[];
    busyKeys: Set<string>;
    onRemove: (skill: PrivateSkill) => void;
}

export const ConfirmedSkills: React.FC<ConfirmedSkillsProps> = ({
    skills,
    busyKeys,
    onRemove,
}) => (
    <SectionCard
        title="On your profile"
        icon={<BadgeCheck size={17} />}
        count={skills.length}
        subtitle={
            <p>
                These are the skills colleagues can see when they look you up or when the Team
                Builder is asked for someone. They see the name and the level, and nothing about
                why — what a suggestion was based on stays private to you.
            </p>
        }
    >
        {skills.length === 0 ? (
            /* Day one for everybody. Nothing here is a warning: a person with no
               declared skills is not incomplete, they simply have not been
               volunteered for anything by a checkbox. */
            <QuietEmpty title="Nothing on your profile yet.">
                <p>
                    Add the things you would be happy to be asked about on a project, below. It
                    is entirely optional — leaving this empty is a legitimate choice, and no
                    part of Cubie treats it as a gap.
                </p>
            </QuietEmpty>
        ) : (
            <div className="space-y-4">
                {byCluster(skills).map(([cluster, rows]) => (
                    <div key={cluster}>
                        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                            {cluster}
                        </h3>
                        <ul className="space-y-2">
                            {rows.map(skill => {
                                const busy = busyKeys.has(skill.skill_key);
                                const since = shortDate(skill.confirmed_at);
                                const suggested = skill.origin === 'inferred';
                                return (
                                    <li
                                        key={skill.skill_key}
                                        className="rounded-lg border border-gray-200 bg-white p-3"
                                    >
                                        <div className="flex flex-wrap items-start justify-between gap-3">
                                            <div className="min-w-0">
                                                <p className="flex flex-wrap items-center gap-2">
                                                    <span className="text-sm font-medium text-gray-900">
                                                        {skill.name}
                                                    </span>
                                                    <OriginBadge origin={skill.origin} />
                                                </p>
                                                <p className="mt-0.5 text-xs text-gray-500">
                                                    {levelLabel(skill.level)}
                                                    {since ? ` · since ${since}` : ''}
                                                </p>
                                                {/* Only on a suggestion the person
                                                    accepted. A skill they typed has
                                                    no rationale and no citations —
                                                    an "explain this" toggle there
                                                    would open onto nothing. */}
                                                {suggested && (
                                                    <EvidenceList
                                                        items={skill.evidence}
                                                        rationale={skill.rationale}
                                                        skillName={skill.name}
                                                    />
                                                )}
                                            </div>
                                            <button
                                                type="button"
                                                onClick={() => onRemove(skill)}
                                                disabled={busy}
                                                className="inline-flex flex-shrink-0 items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-xs font-medium text-gray-700 shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-1 disabled:opacity-60"
                                            >
                                                {busy ? (
                                                    <Loader2 className="animate-spin" size={12} />
                                                ) : (
                                                    <X size={12} />
                                                )}
                                                Take off my profile
                                                <span className="sr-only"> — {skill.name}</span>
                                            </button>
                                        </div>
                                    </li>
                                );
                            })}
                        </ul>
                    </div>
                ))}
            </div>
        )}
    </SectionCard>
);
