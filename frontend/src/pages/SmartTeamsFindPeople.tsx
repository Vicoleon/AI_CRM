// ST-8 — Find people by skill.
//
// THE DIRECTION THIS PAGE EXISTS FOR
// ----------------------------------
// `GET /skills/people/{email}` answers "what has Casey confirmed", which only
// helps once you already suspect Casey. Staffing does not start from a name you
// have to guess — it starts from the work. This page asks the inverse: name the
// skills, get whoever said they have them.
//
// THIS IS NOT A RANKING, AND THE UI MUST NOT INVENT ONE
// ----------------------------------------------------
// The server returns holders in NAME order and gives no per-person score,
// deliberately — `SkillHolder` has no field capable of carrying one. So this page
// does not sort by level, does not sort by how many of the asked-for skills
// somebody holds, and does not put a "best match" first. It lists people under
// each skill and lets the reader decide. Any of those orderings would render as a
// judgement about colleagues, which is exactly what the advisory boundary
// forbids.
//
// "NOBODY HAS THIS" AND "WE DON'T TRACK THIS" ARE DIFFERENT ANSWERS
// -----------------------------------------------------------------
// `unknown` carries the terms the taxonomy could not resolve. A UI that shows an
// empty list for both tells the reader the wrong one — they conclude the company
// has no GDPR knowledge when in fact they typed something the vocabulary does
// not know. So unresolved terms get their own block, above the results.
//
// Gated on core access in App.tsx, like Compare and Team Builder (plan.md
// Decision 4): reading about your own skills is company-wide, fanning out over
// colleagues is not.
import React, { useCallback, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Search, Users, X } from 'lucide-react';

import { EmptyState, ErrorState, LoadingState } from './eos/_shared';
import { AdvisoryNotice, PrimaryButton } from '../components/smartTeams/shared';
import { OriginBadge } from '../components/smartTeams/skills/skillsShared';
import { levelLabel, shortDate } from '../components/smartTeams/skills/skillsFormat';
import type { SkillHoldersResponse } from '../api/smartTeams';
import { findSkillHolders, smartTeamsError } from '../api/smartTeams';

/** Mirrors `MAX_HOLDER_TERMS` in backend/routers/smart_teams_skills.py. Each term
 *  is a fan-out over the roster; somebody who needs more is describing a team,
 *  which Team Builder answers better — so the copy points there rather than
 *  just refusing. */
const MAX_TERMS = 6;

export const SmartTeamsFindPeople: React.FC = () => {
    const [draft, setDraft] = useState('');
    const [terms, setTerms] = useState<string[]>([]);
    const [result, setResult] = useState<SkillHoldersResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const atLimit = terms.length >= MAX_TERMS;

    const addTerm = useCallback(() => {
        const value = draft.trim();
        if (!value || atLimit) return;
        // Case-insensitive dedupe: the server folds phrases anyway, and two
        // chips that differ only in case would each cost a roster read.
        if (terms.some(t => t.toLowerCase() === value.toLowerCase())) {
            setDraft('');
            return;
        }
        setTerms(prev => [...prev, value]);
        setDraft('');
    }, [draft, terms, atLimit]);

    const search = useCallback(async (which: string[]) => {
        if (!which.length) return;
        setLoading(true);
        setError(null);
        try {
            setResult(await findSkillHolders(which));
        } catch (err) {
            // The 400s name the path forward (empty list, too many terms), so
            // show the server's words rather than a generic failure.
            setError(smartTeamsError(err, 'Cubie could not run that search.').message);
            setResult(null);
        } finally {
            setLoading(false);
        }
    }, []);

    /** Chips plus anything still sitting in the box, so a reader who types and
     *  hits Search without pressing Enter gets what they meant. */
    const pending = useMemo(() => {
        const value = draft.trim();
        if (!value) return terms;
        if (terms.some(t => t.toLowerCase() === value.toLowerCase())) return terms;
        return [...terms, value].slice(0, MAX_TERMS);
    }, [draft, terms]);

    const totalHolders = useMemo(
        () => (result?.matched ?? []).reduce((n, m) => n + m.holders.length, 0),
        [result],
    );

    return (
        <div className="mx-auto max-w-4xl px-4 py-8">
            <header data-tour="smart-teams-find-people-header" className="mb-6 flex items-start gap-3">
                <div className="flex items-center justify-center rounded-md bg-brand p-2.5 text-white">
                    <Users size={20} />
                </div>
                <div>
                    <h1 className="text-2xl font-semibold text-gray-900">Find people by skill</h1>
                    <p className="mt-1 text-sm text-gray-500">
                        Name the work. Cubie lists everyone who told us they do it.
                    </p>
                </div>
            </header>

            <div data-tour="smart-teams-find-people-search" className="mb-6 rounded-lg border border-gray-200 bg-white p-5">
                <label htmlFor="skill-term" className="mb-2 block text-sm font-medium text-gray-700">
                    Skills to look for
                </label>
                <div className="flex gap-2">
                    <input
                        id="skill-term"
                        type="text"
                        value={draft}
                        onChange={e => setDraft(e.target.value)}
                        onKeyDown={e => {
                            if (e.key === 'Enter') {
                                e.preventDefault();
                                addTerm();
                            }
                        }}
                        placeholder={atLimit ? `${MAX_TERMS} is the limit` : 'e.g. GDPR, six sigma, okrs'}
                        disabled={atLimit}
                        className="flex-1 rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand disabled:bg-gray-50"
                    />
                    <PrimaryButton onClick={() => search(pending)} disabled={!pending.length || loading}>
                        <Search size={14} className="mr-1.5 inline" />
                        Search
                    </PrimaryButton>
                </div>

                {terms.length > 0 && (
                    <div className="mt-3 flex flex-wrap gap-2">
                        {terms.map(term => (
                            <span
                                key={term}
                                className="inline-flex items-center gap-1.5 rounded-full border border-brand/30 bg-brand-tint px-2.5 py-1 text-xs font-medium text-brand-hover"
                            >
                                {term}
                                <button
                                    type="button"
                                    aria-label={`Remove ${term}`}
                                    onClick={() => setTerms(prev => prev.filter(t => t !== term))}
                                    className="rounded-full p-0.5 hover:bg-brand/10"
                                >
                                    <X size={11} />
                                </button>
                            </span>
                        ))}
                    </div>
                )}

                <p className="mt-3 text-xs leading-relaxed text-gray-500">
                    Everyday wording works — &ldquo;gdpr&rdquo; finds data privacy. Up to {MAX_TERMS}{' '}
                    at a time; for a whole project team, use{' '}
                    <Link to="/smart-teams/team-builder" className="text-brand underline">
                        Team Builder
                    </Link>
                    .
                </p>
            </div>

            {loading && <LoadingState label="Looking across the company…" />}
            {error && !loading && <ErrorState message={error} />}

            {result && !loading && !error && (
                <div className="space-y-6">
                    {/* Above the results on purpose: a reader who does not see
                        this concludes the company has no such knowledge, when in
                        fact the vocabulary did not recognise what they typed. */}
                    {result.unknown.length > 0 && (
                        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
                            <h2 className="text-sm font-semibold text-amber-900">
                                Not in the skill list
                            </h2>
                            <p className="mt-1 text-sm leading-relaxed text-amber-800">
                                Cubie does not track {result.unknown.map(t => `“${t}”`).join(', ')}, so
                                this is not the same as nobody having it. A support admin can add a
                                skill to the list.
                            </p>
                        </div>
                    )}

                    {result.matched.length === 0 && result.unknown.length > 0 ? null : totalHolders ===
                      0 ? (
                        <EmptyState
                            title="Nobody has confirmed these yet"
                            hint="Cubie only lists skills people have said yes to about themselves. A suggestion nobody has answered is a question, not an answer."
                        />
                    ) : (
                        result.matched.map(match => (
                            <section
                                key={match.skill_key}
                                className="rounded-lg border border-gray-200 bg-white"
                            >
                                <header className="flex items-baseline justify-between gap-3 border-b border-gray-100 px-5 py-3">
                                    <h2 className="text-sm font-semibold text-gray-900">
                                        {match.name}
                                    </h2>
                                    <span className="flex-shrink-0 text-xs text-gray-500">
                                        {match.holders.length === 0
                                            ? 'nobody yet'
                                            : `${match.holders.length} ${
                                                  match.holders.length === 1 ? 'person' : 'people'
                                              }`}
                                    </span>
                                </header>
                                {match.holders.length === 0 ? (
                                    <p className="px-5 py-4 text-sm text-gray-500">
                                        In the skill list, but nobody has confirmed it about
                                        themselves.
                                    </p>
                                ) : (
                                    <ul className="divide-y divide-gray-100">
                                        {match.holders.map(holder => (
                                            <li
                                                key={holder.email}
                                                className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-5 py-3"
                                            >
                                                <Link
                                                    to={`/smart-teams/guide/${encodeURIComponent(
                                                        holder.email,
                                                    )}`}
                                                    className="text-sm font-medium text-gray-900 underline decoration-gray-300 hover:decoration-brand"
                                                >
                                                    {holder.display_name}
                                                </Link>
                                                {holder.level && (
                                                    <span className="text-xs text-gray-600">
                                                        {levelLabel(holder.level)}
                                                    </span>
                                                )}
                                                <OriginBadge origin={holder.origin} />
                                                {shortDate(holder.confirmed_at) && (
                                                    <span className="ml-auto flex-shrink-0 text-xs text-gray-400">
                                                        since {shortDate(holder.confirmed_at)}
                                                    </span>
                                                )}
                                            </li>
                                        ))}
                                    </ul>
                                )}
                            </section>
                        ))
                    )}

                    <p className="text-xs leading-relaxed text-gray-500">{result.scope_notice}</p>
                    <AdvisoryNotice text={result.advisory_notice} />
                </div>
            )}
        </div>
    );
};

export default SmartTeamsFindPeople;
