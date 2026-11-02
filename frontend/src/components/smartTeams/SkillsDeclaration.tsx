// The declared-skills step.
//
// Every skill offered here is a row in `st_skills_taxonomy` — a free-text box
// would be friendlier to type into and useless to the Team Builder, which
// matches on curated keys. The server rejects a slug that is not in the
// taxonomy, so this list is the only source of them and there is no way for the
// UI to invent one.
//
// `payload.skills` is EMPTY until ST-8 seeds that taxonomy, which is a state
// this component has to say out loud: an empty list rendered as a search box
// over nothing reads as a broken page. Declaring nothing is allowed either way —
// someone who would rather not be volunteered for projects on the strength of a
// checkbox should be able to finish the interview without one, and a required
// field here would just produce padded answers.
//
// The level vocabulary (`payload.levels`) arrives as the bare `SKILL_LEVELS`
// strings, so the display capitalisation below is presentation of a server
// vocabulary — not instrument copy, which is always rendered verbatim.
import React, { useMemo, useState } from 'react';
import { Check, Search, X } from 'lucide-react';

import type { SkillLevel, SkillPayload } from '../../api/smartTeams';
import { PREFIX_SKILL } from '../../api/smartTeams';
import { PrimaryButton } from './shared';

const LEVEL_LABELS: Record<SkillLevel, string> = {
    familiar: 'Familiar',
    working: 'Working knowledge',
    strong: 'Strong',
    expert: 'Expert',
};

const levelLabel = (level: SkillLevel): string => LEVEL_LABELS[level] ?? level;

interface SkillsDeclarationProps {
    payload: SkillPayload;
    /** Keyed by full answer key (`skill:<slug>`), as stored. */
    answers: Record<string, SkillLevel | null>;
    onDeclare: (skillKey: string, level: SkillLevel | null) => void;
    onComplete: () => void;
    completing: boolean;
}

export const SkillsDeclaration: React.FC<SkillsDeclarationProps> = ({
    payload,
    answers,
    onDeclare,
    onComplete,
    completing,
}) => {
    const [query, setQuery] = useState('');

    const declared = useMemo(
        () => payload.skills.filter(skill => !!answers[`${PREFIX_SKILL}${skill.skill_key}`]),
        [answers, payload.skills],
    );

    // Grouped by taxonomy category, filtered by a plain substring match. No
    // fuzzy search: with a curated list the exact word is what people type, and
    // a near-miss match on a skills list is how someone declares the wrong thing.
    const groups = useMemo(() => {
        const needle = query.trim().toLowerCase();
        const matches = payload.skills.filter(
            skill =>
                !needle ||
                skill.name.toLowerCase().includes(needle) ||
                (skill.cluster ?? '').toLowerCase().includes(needle),
        );
        const byCluster = new Map<string, typeof matches>();
        for (const skill of matches) {
            const key = skill.cluster ?? 'Other';
            const list = byCluster.get(key);
            if (list) list.push(skill);
            else byCluster.set(key, [skill]);
        }
        return [...byCluster.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    }, [payload.skills, query]);

    return (
        <div className="space-y-4">
            <div className="rounded-lg border border-gray-200 bg-white p-4">
                <p className="text-sm leading-relaxed text-gray-600">
                    Pick the skills you would be happy to be asked about on a project, and how
                    deep you are on each. This is what Cubie matches against when someone asks
                    for a team — it is not a rating of you, and it never becomes one.
                </p>
                <p className="mt-2 text-xs text-gray-500">
                    You can add to this later from your Smart Teams page, and you can finish the
                    interview without declaring any.
                </p>
            </div>

            {declared.length > 0 && (
                <div className="rounded-lg border border-brand-tint bg-brand-tint/40 p-4">
                    <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-brand-hover">
                        Declared ({declared.length})
                    </p>
                    <ul className="flex flex-wrap gap-2">
                        {declared.map(skill => {
                            const key = `${PREFIX_SKILL}${skill.skill_key}`;
                            const level = answers[key];
                            const shown = level ? levelLabel(level) : '';
                            return (
                                <li key={skill.skill_key}>
                                    <span className="inline-flex items-center gap-1.5 rounded-full border border-brand/30 bg-white px-2.5 py-1 text-xs text-brand-hover">
                                        {skill.name}
                                        <span className="text-gray-400">·</span>
                                        {shown}
                                        <button
                                            type="button"
                                            onClick={() => onDeclare(skill.skill_key, null)}
                                            aria-label={`Remove ${skill.name}`}
                                            className="rounded-full p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700"
                                        >
                                            <X size={12} />
                                        </button>
                                    </span>
                                </li>
                            );
                        })}
                    </ul>
                </div>
            )}

            {payload.skills.length === 0 ? (
                /* Empty is a real state, not a rendering failure: the taxonomy
                   is seeded by ST-8. A search box over nothing would read as a
                   broken page, so this says what is actually going on and lets
                   the person move on — declaring no skills is allowed. */
                <div className="rounded-lg border border-dashed border-gray-300 bg-white p-6">
                    <p className="text-sm font-medium text-gray-700">
                        The skills list isn&rsquo;t switched on yet.
                    </p>
                    <p className="mt-1 text-xs leading-relaxed text-gray-500">
                        Cubie matches project teams against a curated list of skills, and that
                        list has not been set up yet — so there is nothing to pick from here
                        today. Finish the interview without it; you will be able to add your
                        skills from your Smart Teams page once the list is in.
                    </p>
                </div>
            ) : (
                <>
                <div>
                    <label htmlFor="st-skill-search" className="sr-only">
                        Search skills
                    </label>
                    <div className="relative">
                        <Search
                            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
                            size={15}
                        />
                        <input
                            id="st-skill-search"
                            type="search"
                            value={query}
                            onChange={event => setQuery(event.target.value)}
                            placeholder="Search the skills list…"
                            className="block w-full rounded-md border border-gray-300 py-2 pl-9 pr-3 text-sm shadow-sm outline-none focus:border-brand focus:ring-1 focus:ring-brand"
                        />
                    </div>
                </div>

                {groups.length === 0 ? (
                    <div className="rounded-lg border border-dashed border-gray-300 p-8 text-center">
                        <p className="text-sm font-medium text-gray-700">
                            Nothing in the list matches “{query}”.
                        </p>
                        <p className="mt-1 text-xs text-gray-500">
                            The list is curated so the Team Builder can match on it. If something is
                            missing, tell an admin and it can be added.
                        </p>
                    </div>
                ) : (
                    <div className="space-y-4">
                        {groups.map(([category, skills]) => (
                            <div key={category}>
                                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                                    {category}
                                </h3>
                                <ul className="space-y-2">
                                    {skills.map(skill => {
                                        const key = `${PREFIX_SKILL}${skill.skill_key}`;
                                        const current = answers[key] ?? null;
                                        return (
                                            <li key={skill.skill_key}>
                                                {/* One radiogroup per skill: the levels are
                                                    mutually exclusive, and "not declared" is
                                                    one of the options rather than a hidden
                                                    default, so clearing a skill is a choice
                                                    you can make with the keyboard. */}
                                                <fieldset className="rounded-lg border border-gray-200 bg-white p-3">
                                                    <legend className="px-1 text-sm font-medium text-gray-900">
                                                        {skill.name}
                                                    </legend>
                                                    <div className="mt-2 flex flex-wrap gap-2">
                                                        <label
                                                            htmlFor={`st-skill-${skill.skill_key}-none`}
                                                            className={`cursor-pointer rounded-md border px-2.5 py-1 text-xs focus-within:ring-2 focus-within:ring-brand focus-within:ring-offset-1 ${
                                                                current === null
                                                                    ? 'border-gray-400 bg-gray-100 text-gray-700'
                                                                    : 'border-gray-300 bg-white text-gray-500 hover:border-brand'
                                                            }`}
                                                        >
                                                            <input
                                                                id={`st-skill-${skill.skill_key}-none`}
                                                                type="radio"
                                                                name={key}
                                                                checked={current === null}
                                                                onChange={() =>
                                                                    onDeclare(skill.skill_key, null)
                                                                }
                                                                className="sr-only"
                                                                aria-label={`${skill.name}: not declared`}
                                                            />
                                                            Not declared
                                                        </label>
                                                        {payload.levels.map(level => (
                                                            <label
                                                                key={level}
                                                                htmlFor={`st-skill-${skill.skill_key}-${level}`}
                                                                className={`cursor-pointer rounded-md border px-2.5 py-1 text-xs focus-within:ring-2 focus-within:ring-brand focus-within:ring-offset-1 ${
                                                                    current === level
                                                                        ? 'border-brand bg-brand text-white'
                                                                        : 'border-gray-300 bg-white text-gray-700 hover:border-brand hover:bg-brand-tint'
                                                                }`}
                                                            >
                                                                <input
                                                                    id={`st-skill-${skill.skill_key}-${level}`}
                                                                    type="radio"
                                                                    name={key}
                                                                    checked={current === level}
                                                                    onChange={() =>
                                                                        onDeclare(
                                                                            skill.skill_key,
                                                                            level,
                                                                        )
                                                                    }
                                                                    className="sr-only"
                                                                    aria-label={`${skill.name}: ${levelLabel(level)}`}
                                                                />
                                                                {levelLabel(level)}
                                                            </label>
                                                        ))}
                                                    </div>
                                                </fieldset>
                                            </li>
                                        );
                                    })}
                                </ul>
                            </div>
                        ))}
                    </div>
                )}
                </>
            )}

            <div className="flex items-center justify-between border-t border-gray-200 pt-4">
                <p className="text-xs text-gray-500">
                    {payload.skills.length === 0
                        ? 'Nothing to declare yet.'
                        : declared.length === 0
                          ? 'No skills declared — that is fine.'
                          : `${declared.length} declared.`}
                </p>
                <PrimaryButton onClick={onComplete} busy={completing}>
                    <Check size={15} />
                    Finish this section
                </PrimaryButton>
            </div>
        </div>
    );
};
