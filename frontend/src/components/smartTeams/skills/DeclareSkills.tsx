// Declaring a skill about yourself, against the curated taxonomy.
//
// OFF-TAXONOMY IS A REFUSAL, AND THE UI HAS TO MATCH THAT
// ------------------------------------------------------
// `POST /skills/declare` rejects any key outside the ACTIVE taxonomy — 400 for
// the WHOLE batch, nothing written, with `skills.UNKNOWN_SKILL_DENIED` as the
// detail. The router is explicit about why it is a refusal rather than a
// coercion: a free-text skill is a claim the Team Builder can never match on,
// and snapping it to the nearest curated key attributes work the person never
// touched. So there is no free-text field on this panel. Every row below is a
// taxonomy row the server sent, which is the only place a valid key can come
// from, and the "nothing matches" state names the path forward (ask an admin, and
// it becomes declarable for everyone at once) instead of offering a box.
//
// If a declare 400s anyway, the detail is shown VERBATIM and the tray is left
// exactly as it was — the whole batch was rejected, so re-typing the other nine
// picks would be punishing somebody for the server's answer.
//
// `seeded` IS A DIFFERENT SCREEN FROM "NO MATCHES"
// -----------------------------------------------
// The taxonomy table is filled by `skill_taxonomy.seed_taxonomy` at the top of
// every sweep, so before the first one lands this endpoint honestly returns an
// empty list. `[]` alone cannot tell "the vocabulary has not been built yet"
// apart from "your search found nothing", and rendering the first as the second
// would tell the whole company their search was bad on day one.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Loader2, Plus, Search, X } from 'lucide-react';

import type { SkillLevel, SkillTaxonomyResponse } from '../../../api/smartTeams';
import { searchSkillTaxonomy, smartTeamsError } from '../../../api/smartTeams';
import { PrimaryButton } from '../shared';
import { QuietEmpty, SectionCard } from './skillsShared';
import { levelLabel } from './skillsFormat';

/** Long enough that typing "data privacy" is one request, short enough that the
 *  list does not feel stale. */
const DEBOUNCE_MS = 250;

export interface StagedSkill {
    name: string;
    level: SkillLevel | null;
}

interface DeclareSkillsProps {
    /** Keys already confirmed on the reader's profile — offered as "already
     *  yours" rather than hidden, so searching for one does not look like the
     *  taxonomy is missing it. */
    confirmedKeys: Set<string>;
    /** Keys the reader has denied. Still declarable: an explicit statement about
     *  yourself beats an earlier rejection of somebody else's guess, and the
     *  server lifts the tombstone and reports it in `undenied`. */
    deniedKeys: Set<string>;
    staged: Record<string, StagedSkill>;
    onStage: (skillKey: string, name: string, level: SkillLevel | null) => void;
    onUnstage: (skillKey: string) => void;
    onSubmit: () => void;
    submitting: boolean;
    /** The server's refusal, verbatim. Null when the last submit was fine. */
    submitError: string | null;
}

export const DeclareSkills: React.FC<DeclareSkillsProps> = ({
    confirmedKeys,
    deniedKeys,
    staged,
    onStage,
    onUnstage,
    onSubmit,
    submitting,
    submitError,
}) => {
    const [query, setQuery] = useState('');
    const [cluster, setCluster] = useState('');
    const [taxonomy, setTaxonomy] = useState<SkillTaxonomyResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    // Which search the rendered list belongs to, so an in-flight refetch does not
    // make the current list look wrong while it settles.
    const requestRef = useRef(0);

    const load = useCallback(async (q: string, restrictTo: string): Promise<void> => {
        const ticket = ++requestRef.current;
        setLoading(true);
        try {
            const next = await searchSkillTaxonomy({ q, cluster: restrictTo });
            if (ticket !== requestRef.current) return;
            setTaxonomy(next);
            setError(null);
        } catch (e) {
            if (ticket !== requestRef.current) return;
            setError(
                smartTeamsError(e, 'Cubie could not load the skills list.').message,
            );
        } finally {
            if (ticket === requestRef.current) setLoading(false);
        }
    }, []);

    useEffect(() => {
        const timer = window.setTimeout(() => void load(query.trim(), cluster), DEBOUNCE_MS);
        return () => window.clearTimeout(timer);
    }, [cluster, load, query]);

    const stagedList = useMemo(
        () =>
            Object.entries(staged).sort((a, b) => a[1].name.localeCompare(b[1].name)),
        [staged],
    );

    const groups = useMemo(() => {
        const rows = taxonomy?.skills ?? [];
        const byGroup = new Map<string, typeof rows>();
        for (const row of rows) {
            const key = row.cluster ?? 'Other';
            const list = byGroup.get(key);
            if (list) list.push(row);
            else byGroup.set(key, [row]);
        }
        return [...byGroup.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    }, [taxonomy]);

    const levels: SkillLevel[] = taxonomy?.levels ?? [];

    return (
        <SectionCard
            title="Add a skill"
            icon={<Plus size={17} />}
            subtitle={
                <p>
                    Pick what you would be happy to be asked about on a project, and how deep you
                    are on each. This is what the Team Builder matches on — it is not a rating of
                    you, and it never becomes one.
                </p>
            }
        >
            {/* ── The tray ─────────────────────────────────────────────────── */}
            {stagedList.length > 0 && (
                <div className="mb-4 rounded-lg border border-brand/30 bg-brand-tint/40 p-3">
                    <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-brand-hover">
                        Ready to add ({stagedList.length})
                    </p>
                    <ul className="flex flex-wrap gap-2">
                        {stagedList.map(([key, entry]) => (
                            <li key={key}>
                                <span className="inline-flex items-center gap-1.5 rounded-full border border-brand/30 bg-white px-2.5 py-1 text-xs text-brand-hover">
                                    {entry.name}
                                    <span className="text-gray-400">·</span>
                                    {levelLabel(entry.level)}
                                    {deniedKeys.has(key) && (
                                        <span
                                            className="text-gray-400"
                                            title="You had said no to this. Adding it lifts that."
                                        >
                                            · was declined
                                        </span>
                                    )}
                                    <button
                                        type="button"
                                        onClick={() => onUnstage(key)}
                                        aria-label={`Remove ${entry.name} from the list to add`}
                                        className="rounded-full p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700"
                                    >
                                        <X size={12} />
                                    </button>
                                </span>
                            </li>
                        ))}
                    </ul>
                    <div className="mt-3 flex flex-wrap items-center gap-3">
                        <PrimaryButton onClick={onSubmit} busy={submitting}>
                            <Plus size={15} />
                            Add {stagedList.length}{' '}
                            {stagedList.length === 1 ? 'skill' : 'skills'} to my profile
                        </PrimaryButton>
                        <p className="text-xs text-gray-600">
                            Colleagues can see these as soon as they are added.
                        </p>
                    </div>
                </div>
            )}

            {/* A refusal, in the server's own words. `role="alert"` because a
                failed declare must never be a toast that slides away — the whole
                batch was rejected and nothing was written, and the person needs
                to read why. */}
            {submitError && (
                <div
                    role="alert"
                    className="mb-4 flex items-start gap-2.5 rounded-lg border border-red-200 bg-red-50 p-3"
                >
                    <AlertTriangle className="mt-0.5 flex-shrink-0 text-red-500" size={16} />
                    <div>
                        <p className="text-sm font-medium text-red-900">
                            Nothing was added.
                        </p>
                        <p className="mt-1 text-xs leading-relaxed text-red-800">{submitError}</p>
                        <p className="mt-1 text-xs text-red-700">
                            Your picks are still in the list above — fix the one Cubie named, or
                            take it out, and try again.
                        </p>
                    </div>
                </div>
            )}

            {/* ── Search ───────────────────────────────────────────────────── */}
            <div className="flex flex-wrap items-center gap-2">
                <div className="relative min-w-[14rem] flex-1">
                    <label htmlFor="st-skill-taxonomy-search" className="sr-only">
                        Search the skills list
                    </label>
                    <Search
                        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
                        size={15}
                    />
                    <input
                        id="st-skill-taxonomy-search"
                        type="search"
                        value={query}
                        onChange={event => setQuery(event.target.value)}
                        placeholder="Search — “gdpr”, “audit”, “six sigma”…"
                        className="block w-full rounded-md border border-gray-300 py-2 pl-9 pr-3 text-sm shadow-sm outline-none focus:border-brand focus:ring-1 focus:ring-brand"
                    />
                </div>
                <div>
                    <label htmlFor="st-skill-cluster" className="sr-only">
                        Filter by area
                    </label>
                    <select
                        id="st-skill-cluster"
                        value={cluster}
                        onChange={event => setCluster(event.target.value)}
                        className="rounded-md border border-gray-300 py-2 pl-3 pr-8 text-sm shadow-sm outline-none focus:border-brand focus:ring-1 focus:ring-brand"
                    >
                        <option value="">Every area</option>
                        {(taxonomy?.clusters ?? []).map(name => (
                            <option key={name} value={name}>
                                {name}
                            </option>
                        ))}
                    </select>
                </div>
                {loading && (
                    <span className="inline-flex items-center gap-1.5 text-xs text-gray-500">
                        <Loader2 className="animate-spin" size={13} />
                        Searching…
                    </span>
                )}
            </div>
            <p className="mt-1.5 text-xs text-gray-500">
                Search matches the everyday names too, so “gdpr” finds “Data privacy (GDPR /
                CCPA)”.
            </p>

            {/* ── Results ──────────────────────────────────────────────────── */}
            <div className="mt-4">
                {error ? (
                    <div
                        role="alert"
                        className="flex items-start gap-2.5 rounded-lg border border-red-200 bg-red-50 p-3"
                    >
                        <AlertTriangle className="mt-0.5 flex-shrink-0 text-red-500" size={16} />
                        <div>
                            <p className="text-sm font-medium text-red-900">
                                Cubie could not load the skills list.
                            </p>
                            <p className="mt-1 text-xs text-red-800">{error}</p>
                            <button
                                type="button"
                                onClick={() => void load(query.trim(), cluster)}
                                className="mt-2 rounded-md border border-red-300 bg-white px-2.5 py-1 text-xs font-medium text-red-800 hover:bg-red-100"
                            >
                                Try again
                            </button>
                        </div>
                    </div>
                ) : !taxonomy ? (
                    <div className="flex items-center gap-2 py-6 text-sm text-gray-500">
                        <Loader2 className="animate-spin" size={16} />
                        Loading the skills list…
                    </div>
                ) : !taxonomy.seeded ? (
                    /* Not a search that missed. The vocabulary itself has not been
                       built — it is filled by the overnight sweep — so there is
                       genuinely nothing to pick from, and saying "no matches"
                       here would blame the person's search for our job. */
                    <QuietEmpty title="The skills list isn’t loaded.">
                        <p>
                            Cubie matches project teams against a curated list of skills, and
                            that list is not in place on this environment yet, so there is
                            nothing to pick from.
                        </p>
                        <p>
                            This does not fix itself overnight — it is a setup step, so it is
                            worth telling an admin rather than waiting. Nothing you have already
                            done is affected, and your profile is not incomplete because of it.
                        </p>
                    </QuietEmpty>
                ) : groups.length === 0 ? (
                    <QuietEmpty
                        title={
                            query.trim()
                                ? `Nothing in the list matches “${query.trim()}”.`
                                : 'Nothing in that area yet.'
                        }
                    >
                        <p>
                            The list is curated so the Team Builder can actually match on it,
                            which means Cubie will not take a skill that is not on it. If
                            something real is missing, tell an admin — adding it makes it
                            declarable for everyone at once.
                        </p>
                    </QuietEmpty>
                ) : (
                    <div className="space-y-4">
                        {groups.map(([groupName, rows]) => (
                            <div key={groupName}>
                                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                                    {groupName}
                                </h3>
                                <ul className="space-y-2">
                                    {rows.map(row => {
                                        const already = confirmedKeys.has(row.skill_key);
                                        const stagedLevel = staged[row.skill_key];
                                        const fieldName = `st-declare-${row.skill_key}`;
                                        return (
                                            <li key={row.skill_key}>
                                                {already ? (
                                                    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 p-3">
                                                        <span className="text-sm text-gray-700">
                                                            {row.name}
                                                        </span>
                                                        <span className="text-xs text-gray-500">
                                                            Already on your profile — change or
                                                            remove it above.
                                                        </span>
                                                    </div>
                                                ) : (
                                                    /* One radiogroup per skill. "Not
                                                       adding" is an option rather than
                                                       a hidden default, so taking a
                                                       skill back out is a choice you
                                                       can make with the keyboard. */
                                                    <fieldset className="rounded-lg border border-gray-200 bg-white p-3">
                                                        <legend className="px-1 text-sm font-medium text-gray-900">
                                                            {row.name}
                                                            {deniedKeys.has(row.skill_key) && (
                                                                <span className="ml-2 text-xs font-normal text-gray-500">
                                                                    you said no to this before —
                                                                    adding it lifts that
                                                                </span>
                                                            )}
                                                        </legend>
                                                        <div className="mt-2 flex flex-wrap gap-2">
                                                            <label
                                                                htmlFor={`${fieldName}-none`}
                                                                className={`cursor-pointer rounded-md border px-2.5 py-1 text-xs focus-within:ring-2 focus-within:ring-brand focus-within:ring-offset-1 ${
                                                                    !stagedLevel
                                                                        ? 'border-gray-400 bg-gray-100 text-gray-700'
                                                                        : 'border-gray-300 bg-white text-gray-500 hover:border-brand'
                                                                }`}
                                                            >
                                                                <input
                                                                    id={`${fieldName}-none`}
                                                                    type="radio"
                                                                    name={fieldName}
                                                                    checked={!stagedLevel}
                                                                    onChange={() =>
                                                                        onUnstage(row.skill_key)
                                                                    }
                                                                    className="sr-only"
                                                                    aria-label={`${row.name}: not adding`}
                                                                />
                                                                Not adding
                                                            </label>
                                                            {levels.map(level => (
                                                                <label
                                                                    key={level}
                                                                    htmlFor={`${fieldName}-${level}`}
                                                                    className={`cursor-pointer rounded-md border px-2.5 py-1 text-xs focus-within:ring-2 focus-within:ring-brand focus-within:ring-offset-1 ${
                                                                        stagedLevel?.level === level
                                                                            ? 'border-brand bg-brand text-white'
                                                                            : 'border-gray-300 bg-white text-gray-700 hover:border-brand hover:bg-brand-tint'
                                                                    }`}
                                                                >
                                                                    <input
                                                                        id={`${fieldName}-${level}`}
                                                                        type="radio"
                                                                        name={fieldName}
                                                                        checked={
                                                                            stagedLevel?.level ===
                                                                            level
                                                                        }
                                                                        onChange={() =>
                                                                            onStage(
                                                                                row.skill_key,
                                                                                row.name,
                                                                                level,
                                                                            )
                                                                        }
                                                                        className="sr-only"
                                                                        aria-label={`${row.name}: ${levelLabel(level)}`}
                                                                    />
                                                                    {levelLabel(level)}
                                                                </label>
                                                            ))}
                                                            {/* `null` is a real value the
                                                                API accepts — "I do this, at
                                                                no stated level" — not the
                                                                absence of an answer. */}
                                                            <label
                                                                htmlFor={`${fieldName}-unstated`}
                                                                className={`cursor-pointer rounded-md border px-2.5 py-1 text-xs focus-within:ring-2 focus-within:ring-brand focus-within:ring-offset-1 ${
                                                                    stagedLevel &&
                                                                    stagedLevel.level === null
                                                                        ? 'border-brand bg-brand text-white'
                                                                        : 'border-gray-300 bg-white text-gray-700 hover:border-brand hover:bg-brand-tint'
                                                                }`}
                                                            >
                                                                <input
                                                                    id={`${fieldName}-unstated`}
                                                                    type="radio"
                                                                    name={fieldName}
                                                                    checked={
                                                                        !!stagedLevel &&
                                                                        stagedLevel.level === null
                                                                    }
                                                                    onChange={() =>
                                                                        onStage(
                                                                            row.skill_key,
                                                                            row.name,
                                                                            null,
                                                                        )
                                                                    }
                                                                    className="sr-only"
                                                                    aria-label={`${row.name}: add without stating a level`}
                                                                />
                                                                Add, no level
                                                            </label>
                                                        </div>
                                                    </fieldset>
                                                )}
                                            </li>
                                        );
                                    })}
                                </ul>
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </SectionCard>
    );
};
