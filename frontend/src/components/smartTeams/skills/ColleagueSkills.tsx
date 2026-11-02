// Look up one colleague's confirmed skills, for staffing.
//
// WHAT THIS COMPONENT CANNOT DO, BY TYPE
// --------------------------------------
// It renders `PublicSkill[]` — `skills.ConfirmedSkill` projected through
// `public_dict` — and that shape has no evidence field at all. Not an omitted
// one: `confirmed_skills_for` returns DTOs of primitives rather than ORM rows, so
// the meeting titles and transcript excerpts behind a suggestion are not on the
// object this screen was handed. There is therefore nothing for this file to
// choose to hide, and no `EvidenceList` import.
//
// It also shows nothing unanswered. `GET /skills/people/{email}` narrows on
// `confirmed.is_(True)`, which excludes a pending suggestion and a denial in one
// predicate — so a person's half-answered queue is not visible here, and neither
// is the fact that they have one.
//
// ONE PERSON AT A TIME, AND NO JUDGEMENT
// --------------------------------------
// No side-by-side, no ordering of people, no "best fit". Multi-person reasoning
// is the Team Builder's job and its prose is authored server-side; a comparison
// assembled in the browser would be a per-person judgement nobody reviewed.
//
// The lookup is an email box rather than a directory picker on purpose:
// `/api/directory/search` sits behind the copilot gate (`require_full_app_access`),
// and this screen follows Smart Teams' own audience. A picker would work for
// people with the copilot and quietly fail for everybody else.
import React, { useState } from 'react';
import { AlertTriangle, Loader2, Search, UserSearch } from 'lucide-react';

import type { ColleagueSkillsResponse } from '../../../api/smartTeams';
import { AdvisoryNotice } from '../shared';
import { OriginBadge, QuietEmpty, SectionCard } from './skillsShared';
import { byCluster, levelLabel } from './skillsFormat';

interface ColleagueSkillsProps {
    onLookup: (email: string) => void;
    loading: boolean;
    /** A sentence for the person. 404 means "not a colleague at this company". */
    error: string | null;
    result: ColleagueSkillsResponse | null;
}

export const ColleagueSkills: React.FC<ColleagueSkillsProps> = ({
    onLookup,
    loading,
    error,
    result,
}) => {
    const [email, setEmail] = useState('');

    return (
        <SectionCard
            title="Look someone up"
            icon={<UserSearch size={17} />}
            subtitle={
                <p>
                    See what a colleague has confirmed they can be asked about, before you ask
                    them onto a project. You see what they chose to publish — never why, never
                    anything they have not answered.
                </p>
            }
        >
            <form
                data-tour="smart-teams-skills-lookup"
                onSubmit={event => {
                    event.preventDefault();
                    const trimmed = email.trim();
                    if (trimmed) onLookup(trimmed);
                }}
                className="flex flex-wrap items-end gap-2"
            >
                <div className="min-w-[16rem] flex-1">
                    <label
                        htmlFor="st-colleague-email"
                        className="mb-1 block text-xs font-medium text-gray-700"
                    >
                        Their work email address
                    </label>
                    <div className="relative">
                        <Search
                            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
                            size={15}
                        />
                        <input
                            id="st-colleague-email"
                            type="email"
                            value={email}
                            onChange={event => setEmail(event.target.value)}
                            placeholder="firstname@company.com"
                            autoComplete="off"
                            className="block w-full rounded-md border border-gray-300 py-2 pl-9 pr-3 text-sm shadow-sm outline-none focus:border-brand focus:ring-1 focus:ring-brand"
                        />
                    </div>
                </div>
                <button
                    type="submit"
                    disabled={loading || !email.trim()}
                    className="inline-flex items-center gap-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                >
                    {loading && <Loader2 className="animate-spin" size={15} />}
                    Look up
                </button>
            </form>

            {error && (
                <div
                    role="alert"
                    className="mt-4 flex items-start gap-2.5 rounded-lg border border-red-200 bg-red-50 p-3"
                >
                    <AlertTriangle className="mt-0.5 flex-shrink-0 text-red-500" size={16} />
                    <p className="text-xs leading-relaxed text-red-800">{error}</p>
                </div>
            )}

            {result && !error && (
                <div className="mt-4 space-y-3">
                    <p className="text-sm font-medium text-gray-900">{result.subject_email}</p>

                    {result.skills.length === 0 ? (
                        /* A legitimate answer, not a dead end. Most of the company
                           will look like this until they have picked something, and
                           reading it as "no skills" would be exactly wrong. */
                        <QuietEmpty title="They haven’t confirmed any skills yet.">
                            <p>
                                That is not a statement about what they can do — declaring skills
                                is optional, and plenty of people have not got round to it. Ask
                                them.
                            </p>
                        </QuietEmpty>
                    ) : (
                        <div className="space-y-4">
                            {byCluster(result.skills).map(([cluster, rows]) => (
                                <div key={cluster}>
                                    <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                                        {cluster}
                                    </h3>
                                    <ul className="space-y-1.5">
                                        {rows.map(skill => (
                                            <li
                                                key={skill.skill_key}
                                                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-gray-200 bg-white px-3 py-2"
                                            >
                                                <span className="flex flex-wrap items-baseline gap-x-2">
                                                    <span className="text-sm font-medium text-gray-900">
                                                        {skill.name}
                                                    </span>
                                                    <span className="text-xs text-gray-500">
                                                        {levelLabel(skill.level)}
                                                    </span>
                                                </span>
                                                <OriginBadge origin={skill.origin} />
                                            </li>
                                        ))}
                                    </ul>
                                </div>
                            ))}
                        </div>
                    )}

                    {/* Server copy, verbatim: a short list is not a complete
                        picture of somebody, and the working behind a suggestion is
                        private to them. */}
                    {result.scope_notice.trim() && (
                        <p className="rounded-md border border-gray-200 bg-gray-50 p-3 text-xs leading-relaxed text-gray-600">
                            {result.scope_notice}
                        </p>
                    )}
                    <AdvisoryNotice text={result.advisory_notice} />
                </div>
            )}
        </SectionCard>
    );
};
