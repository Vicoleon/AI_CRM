// The Team Builder ask.
//
// WHAT THE FORM CAN AND CANNOT ASK FOR
// ------------------------------------
// `POST /teams` takes skills (free-text phrases, resolved EXACTLY against the
// curated taxonomy), an optional nice-to-have list, a size, a date window,
// a style mix, strengths clusters, and the ask in the person's own words. It does
// NOT take a person, a department, a seniority, or a `requested_by` — the
// requester is the authenticated caller, which is what makes running Team
// Builder as somebody else unsayable rather than merely refused.
//
// The skill picker reads `GET /teams/skills`, which returns SKILLS AND NEVER
// PEOPLE. Which colleague holds one is the other endpoint's answer and is not a
// directory this page may browse.
//
// FREE TEXT IS ALLOWED AND THAT IS DELIBERATE
// -------------------------------------------
// A phrase that does not resolve comes back in `unrecognised` — reported, never
// coerced onto a neighbouring skill, "which is how somebody ends up staffed onto
// work they have never touched". So the box takes any phrase and the answer says
// which ones Cubie does not track. That also covers the state where the taxonomy
// has not been seeded yet: `/teams/skills` has no `seeded` flag (only
// `/skills/taxonomy` does), so an empty list here is ambiguous and the form says
// so rather than rendering a search box over nothing.
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, Plus, Search, X } from 'lucide-react';

import {
    getTeamSkillVocabulary,
    smartTeamsError,
    type TeamAskBody,
    type TeamSkillVocabularyResponse,
    type TeamStyleMix,
} from '../../../api/smartTeams';
import { PrimaryButton } from '../shared';

const STYLE_MIX_LABELS: Record<TeamStyleMix, { label: string; hint: string }> = {
    unspecified: {
        label: 'No preference',
        hint: 'Cubie reports the mix the group happens to have.',
    },
    complementary: {
        label: 'A mix of working styles',
        hint: 'Prefer a group whose described working styles differ from each other.',
    },
    aligned: {
        label: 'Similar working styles',
        hint: 'Prefer a group whose described working styles are close to each other.',
    },
};

const styleMixLabel = (value: TeamStyleMix) =>
    STYLE_MIX_LABELS[value]?.label ?? value;

export interface TeamAskFormProps {
    onSubmit: (body: TeamAskBody) => void;
    submitting: boolean;
}

interface PickedSkill {
    phrase: string;
    /** True when it came from the curated list, so it will resolve. A typed
     *  phrase is submitted as typed and may come back unrecognised. */
    curated: boolean;
}

export const TeamAskForm: React.FC<TeamAskFormProps> = ({ onSubmit, submitting }) => {
    const [vocab, setVocab] = useState<TeamSkillVocabularyResponse | null>(null);
    const [vocabError, setVocabError] = useState<string | null>(null);
    const [vocabLoading, setVocabLoading] = useState(true);

    const [query, setQuery] = useState('');
    const [matches, setMatches] = useState<TeamSkillVocabularyResponse['skills']>([]);
    const [searching, setSearching] = useState(false);

    const [required, setRequired] = useState<PickedSkill[]>([]);
    const [niceToHave, setNiceToHave] = useState<PickedSkill[]>([]);
    const [size, setSize] = useState('');
    const [startsOn, setStartsOn] = useState('');
    const [endsOn, setEndsOn] = useState('');
    const [styleMix, setStyleMix] = useState<TeamStyleMix>('unspecified');
    const [clusters, setClusters] = useState<string[]>([]);
    const [ask, setAsk] = useState('');
    const [localError, setLocalError] = useState<string | null>(null);

    const requestRef = useRef(0);

    // The initial vocabulary load doubles as the clusters + limits source.
    useEffect(() => {
        let live = true;
        setVocabLoading(true);
        getTeamSkillVocabulary({ limit: 200 })
            .then(data => {
                if (!live) return;
                setVocab(data);
                setMatches(data.skills);
                setVocabError(null);
            })
            .catch(e => {
                if (!live) return;
                setVocabError(
                    smartTeamsError(e, 'Cubie could not load the skill list.').message,
                );
            })
            .finally(() => {
                if (live) setVocabLoading(false);
            });
        return () => {
            live = false;
        };
    }, []);

    // `/teams/skills` matches on name and alias server-side, so search there
    // rather than filtering the first page in the browser: "gdpr" finds
    // "Data privacy (GDPR / CCPA)" only if the server does the matching.
    useEffect(() => {
        if (!vocab) return;
        const token = ++requestRef.current;
        setSearching(true);
        const timer = setTimeout(() => {
            getTeamSkillVocabulary({ query: query.trim() || undefined, limit: 200 })
                .then(data => {
                    if (requestRef.current !== token) return;
                    setMatches(data.skills);
                })
                .catch(() => {
                    if (requestRef.current === token) setMatches([]);
                })
                .finally(() => {
                    if (requestRef.current === token) setSearching(false);
                });
        }, 250);
        return () => clearTimeout(timer);
        // `vocab` only gates the first run; re-querying on it would double-fetch.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [query, !!vocab]);

    const maxSkills = vocab?.max_skills_per_ask ?? 8;
    const maxSize = vocab?.max_team_size ?? 8;
    const styleValues: TeamStyleMix[] = vocab?.style_mix_values?.length
        ? vocab.style_mix_values
        : ['unspecified', 'complementary', 'aligned'];

    const pickedPhrases = useMemo(
        () =>
            new Set(
                [...required, ...niceToHave].map(item => item.phrase.trim().toLowerCase()),
            ),
        [niceToHave, required],
    );

    const total = required.length + niceToHave.length;
    const atLimit = total >= maxSkills;

    const add = (phrase: string, curated: boolean, into: 'required' | 'nice') => {
        const clean = phrase.trim();
        if (!clean) return;
        if (pickedPhrases.has(clean.toLowerCase())) return;
        if (atLimit) {
            setLocalError(
                `An ask can name at most ${maxSkills} skills. Covering each one takes at least one person.`,
            );
            return;
        }
        setLocalError(null);
        const entry = { phrase: clean, curated };
        if (into === 'required') setRequired(list => [...list, entry]);
        else setNiceToHave(list => [...list, entry]);
    };

    const remove = (phrase: string, from: 'required' | 'nice') => {
        setLocalError(null);
        if (from === 'required')
            setRequired(list => list.filter(item => item.phrase !== phrase));
        else setNiceToHave(list => list.filter(item => item.phrase !== phrase));
    };

    const toggleCluster = (cluster: string) =>
        setClusters(list =>
            list.includes(cluster)
                ? list.filter(item => item !== cluster)
                : [...list, cluster],
        );

    const submit = (event: React.FormEvent) => {
        event.preventDefault();
        if (total === 0) {
            setLocalError(
                'Name at least one skill the work needs. Cubie matches against the skills people have confirmed in Smart Teams.',
            );
            return;
        }
        if (startsOn && endsOn && endsOn < startsOn) {
            setLocalError('The window ends before it starts.');
            return;
        }
        setLocalError(null);
        onSubmit({
            skills: required.map(item => item.phrase),
            nice_to_have: niceToHave.map(item => item.phrase),
            size: size ? Number(size) : null,
            starts_on: startsOn || null,
            ends_on: endsOn || null,
            style_mix: styleMix,
            clusters,
            ask: ask.trim(),
        });
    };

    const Chip: React.FC<{ item: PickedSkill; from: 'required' | 'nice' }> = ({
        item,
        from,
    }) => (
        <li>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-brand/30 bg-white px-2.5 py-1 text-xs text-brand-hover">
                {item.phrase}
                {!item.curated && (
                    <span
                        className="text-gray-400"
                        title="Typed by you — Cubie will say if it does not track a skill by this name"
                    >
                        (typed)
                    </span>
                )}
                <button
                    type="button"
                    onClick={() => remove(item.phrase, from)}
                    aria-label={`Remove ${item.phrase}`}
                    className="rounded-full p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand"
                >
                    <X size={12} aria-hidden="true" />
                </button>
            </span>
        </li>
    );

    return (
        <form onSubmit={submit} className="space-y-4">
            {/* ── Skills ────────────────────────────────────────────────────── */}
            <fieldset data-tour="smart-teams-team-builder-skills" className="rounded-xl border border-gray-200 bg-white p-5">
                <legend className="px-1 text-sm font-semibold text-gray-900">
                    What does the work need?
                </legend>
                <p className="mt-1 text-xs leading-relaxed text-gray-500">
                    Cubie matches against the skills people have confirmed about
                    themselves in Smart Teams. It does not read job titles, project
                    history, or how busy anybody is.
                </p>

                {(required.length > 0 || niceToHave.length > 0) && (
                    <div className="mt-4 space-y-3">
                        {required.length > 0 && (
                            <div>
                                <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-gray-500">
                                    Needed
                                </p>
                                <ul className="flex flex-wrap gap-2">
                                    {required.map(item => (
                                        <Chip
                                            key={item.phrase}
                                            item={item}
                                            from="required"
                                        />
                                    ))}
                                </ul>
                            </div>
                        )}
                        {niceToHave.length > 0 && (
                            <div>
                                <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-gray-500">
                                    Nice to have
                                </p>
                                <ul className="flex flex-wrap gap-2">
                                    {niceToHave.map(item => (
                                        <Chip key={item.phrase} item={item} from="nice" />
                                    ))}
                                </ul>
                            </div>
                        )}
                    </div>
                )}

                <div className="relative mt-4">
                    <Search
                        size={16}
                        aria-hidden="true"
                        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
                    />
                    <input
                        id="team-skill-search"
                        type="text"
                        value={query}
                        onChange={event => setQuery(event.target.value)}
                        placeholder="Search skills, or type one Cubie may not track"
                        aria-label="Search skills"
                        autoComplete="off"
                        className="w-full rounded-md border border-gray-300 py-2 pl-9 pr-3 text-sm text-gray-900 placeholder:text-gray-400 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                    />
                </div>

                <div className="mt-3" aria-live="polite">
                    {vocabLoading && (
                        <p className="flex items-center gap-2 text-sm text-gray-500">
                            <Loader2
                                className="animate-spin"
                                size={14}
                                aria-hidden="true"
                            />
                            Loading the skill list…
                        </p>
                    )}

                    {!vocabLoading && vocabError && (
                        <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs leading-relaxed text-amber-900">
                            {vocabError} You can still type skill names below and Cubie will
                            tell you which ones it does not track.
                        </p>
                    )}

                    {!vocabLoading && !vocabError && vocab?.skills.length === 0 && (
                        <p className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs leading-relaxed text-gray-600">
                            Cubie has no skill list to offer right now. The curated
                            vocabulary is loaded as a setup step, so this is what a
                            missing list looks like rather than a company with no skills
                            — worth telling an admin rather than waiting it out. You can
                            still type skill names and Cubie will report which ones it
                            does not track.
                        </p>
                    )}

                    {!vocabLoading && matches.length > 0 && (
                        <ul className="max-h-60 divide-y divide-gray-100 overflow-y-auto rounded-lg border border-gray-200">
                            {matches
                                .filter(
                                    row => !pickedPhrases.has(row.name.trim().toLowerCase()),
                                )
                                .slice(0, 40)
                                .map(row => (
                                    <li
                                        key={row.skill}
                                        className="flex flex-wrap items-center justify-between gap-2 px-3 py-2"
                                    >
                                        <span className="min-w-0 text-sm text-gray-800">
                                            {row.name}
                                            {row.cluster && (
                                                <span className="ml-2 text-xs text-gray-400">
                                                    {row.cluster}
                                                </span>
                                            )}
                                        </span>
                                        <span className="flex flex-shrink-0 gap-1.5">
                                            <button
                                                type="button"
                                                disabled={atLimit}
                                                onClick={() =>
                                                    add(row.name, true, 'required')
                                                }
                                                className="rounded-md border border-gray-300 bg-white px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand disabled:opacity-40"
                                            >
                                                Needed
                                            </button>
                                            <button
                                                type="button"
                                                disabled={atLimit}
                                                onClick={() => add(row.name, true, 'nice')}
                                                className="rounded-md border border-gray-300 bg-white px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand disabled:opacity-40"
                                            >
                                                Nice to have
                                            </button>
                                        </span>
                                    </li>
                                ))}
                        </ul>
                    )}

                    {!vocabLoading &&
                        !searching &&
                        matches.length === 0 &&
                        !!vocab?.skills.length &&
                        query.trim() && (
                            <p className="text-sm text-gray-500">
                                Cubie does not track a skill by that name.
                            </p>
                        )}

                    {query.trim() && !pickedPhrases.has(query.trim().toLowerCase()) && (
                        <button
                            type="button"
                            disabled={atLimit}
                            onClick={() => add(query, false, 'required')}
                            className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-dashed border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand disabled:opacity-40"
                        >
                            <Plus size={12} aria-hidden="true" />
                            Ask for &ldquo;{query.trim()}&rdquo; anyway
                        </button>
                    )}
                </div>

                <p className="mt-3 text-xs text-gray-500">
                    {total} of {maxSkills} named.
                </p>
            </fieldset>

            {/* ── Size and window ──────────────────────────────────────────── */}
            <fieldset data-tour="smart-teams-team-builder-when" className="rounded-xl border border-gray-200 bg-white p-5">
                <legend className="px-1 text-sm font-semibold text-gray-900">
                    How many, and when
                </legend>
                <div className="mt-3 grid gap-4 sm:grid-cols-3">
                    <div>
                        <label
                            htmlFor="team-size"
                            className="block text-xs font-medium text-gray-700"
                        >
                            Group size (optional)
                        </label>
                        <input
                            id="team-size"
                            type="number"
                            min={1}
                            max={maxSize}
                            value={size}
                            onChange={event => setSize(event.target.value)}
                            className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                        />
                    </div>
                    <div>
                        <label
                            htmlFor="team-starts"
                            className="block text-xs font-medium text-gray-700"
                        >
                            Starts (optional)
                        </label>
                        <input
                            id="team-starts"
                            type="date"
                            value={startsOn}
                            onChange={event => setStartsOn(event.target.value)}
                            className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                        />
                    </div>
                    <div>
                        <label
                            htmlFor="team-ends"
                            className="block text-xs font-medium text-gray-700"
                        >
                            Ends (optional)
                        </label>
                        <input
                            id="team-ends"
                            type="date"
                            value={endsOn}
                            onChange={event => setEndsOn(event.target.value)}
                            className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                        />
                    </div>
                </div>
                <p className="mt-2 text-xs leading-relaxed text-gray-500">
                    Dates are used for booked time off only. Cubie deliberately does not
                    consider workload, meetings, or other projects.
                </p>
            </fieldset>

            {/* ── Style mix ────────────────────────────────────────────────── */}
            <fieldset data-tour="smart-teams-team-builder-styles" className="rounded-xl border border-gray-200 bg-white p-5">
                <legend className="px-1 text-sm font-semibold text-gray-900">
                    Working styles
                </legend>
                <div className="mt-3 space-y-2" role="radiogroup" aria-label="Working styles">
                    {styleValues.map(value => (
                        <label
                            key={value}
                            className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-gray-200 p-3 hover:bg-gray-50 has-[:checked]:border-brand has-[:checked]:bg-brand-tint/40"
                        >
                            <input
                                type="radio"
                                name="team-style-mix"
                                value={value}
                                checked={styleMix === value}
                                onChange={() => setStyleMix(value)}
                                className="mt-0.5 h-4 w-4 border-gray-300 text-brand focus:ring-brand"
                            />
                            <span>
                                <span className="block text-sm font-medium text-gray-900">
                                    {styleMixLabel(value)}
                                </span>
                                {STYLE_MIX_LABELS[value]?.hint && (
                                    <span className="block text-xs text-gray-500">
                                        {STYLE_MIX_LABELS[value].hint}
                                    </span>
                                )}
                            </span>
                        </label>
                    ))}
                </div>
            </fieldset>

            {/* ── Clusters ─────────────────────────────────────────────────── */}
            {!!vocab?.clusters?.length && (
                <fieldset className="rounded-xl border border-gray-200 bg-white p-5">
                    <legend className="px-1 text-sm font-semibold text-gray-900">
                        Kinds of contribution to aim for (optional)
                    </legend>
                    <div className="mt-3 flex flex-wrap gap-2">
                        {vocab.clusters.map(cluster => (
                            <label
                                key={cluster}
                                className="inline-flex cursor-pointer items-center gap-2 rounded-full border border-gray-300 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 has-[:checked]:border-brand has-[:checked]:bg-brand-tint/60 has-[:checked]:text-brand-hover"
                            >
                                <input
                                    type="checkbox"
                                    checked={clusters.includes(cluster)}
                                    onChange={() => toggleCluster(cluster)}
                                    className="h-3.5 w-3.5 rounded border-gray-300 text-brand focus:ring-brand"
                                />
                                {cluster}
                            </label>
                        ))}
                    </div>
                </fieldset>
            )}

            {/* ── The ask in their own words ───────────────────────────────── */}
            <fieldset className="rounded-xl border border-gray-200 bg-white p-5">
                <legend className="px-1 text-sm font-semibold text-gray-900">
                    What is the work? (optional)
                </legend>
                <label htmlFor="team-ask" className="mt-1 block text-xs text-gray-500">
                    A sentence in your own words. Cubie uses it to write up the answer; it
                    does not use it to pick anybody.
                </label>
                <textarea
                    id="team-ask"
                    value={ask}
                    onChange={event => setAsk(event.target.value)}
                    rows={3}
                    className="mt-2 w-full rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                />
            </fieldset>

            {localError && (
                <p
                    role="alert"
                    className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800"
                >
                    {localError}
                </p>
            )}

            <PrimaryButton type="submit" busy={submitting} disabled={total === 0}>
                Find a group
            </PrimaryButton>
        </form>
    );
};

export default TeamAskForm;
