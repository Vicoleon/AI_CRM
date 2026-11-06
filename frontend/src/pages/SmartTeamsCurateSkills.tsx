// ST-8 — Curate the skill list (support admins).
//
// WHY THIS SCREEN EXISTS
// ----------------------
// The skill vocabulary ships in `skill_taxonomy.py` and is released with the
// app, which is right for something the whole company shares — and too slow for
// the case that keeps coming up: somebody needs one more skill now, and it does
// not justify a deploy.
//
// TWO THINGS THE UI MUST GET RIGHT, BOTH ABOUT PERMANENCE
// ------------------------------------------------------
// 1. `managed_in_code` rows CANNOT be edited here. The seeder overwrites name,
//    cluster and aliases from the release on every run, so an edit to a shipped
//    skill would appear to work and then silently revert. The server 409s; this
//    page must not offer the button in the first place, because a control that
//    always fails is worse than no control.
//
// 2. THERE IS NO DELETE. `st_person_skills` holds a RESTRICT foreign key, so a
//    skill somebody has claimed cannot be removed — only retired. The screen
//    says so before the admin adds anything, and shows `claims` on every row so
//    "retire" is an informed choice rather than a surprise.
//
// VALIDATION ERRORS ARE A LIST, AND ALL OF IT GETS RENDERED
// --------------------------------------------------------
// `POST` returns 400 with `detail.problems` — every problem found, not the
// first, specifically so filling in this form is not four round trips. Showing
// only the first would throw that away, so `problemsFrom` digs the array out of
// the error body rather than falling back to a generic message.
//
// Gated in App.tsx and again by the server (403 for execs and Team Managers, not
// just for non-admins — seniority over people is not authority over the
// vocabulary those people get described in).
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Plus, ShieldCheck, Undo2, Wrench } from 'lucide-react';

import { toast } from '../components/eos/Toast';
import { confirmDialog } from '../components/eos/ConfirmDialog';
import { ErrorState, LoadingState } from './eos/_shared';
import { AdvisoryNotice, PrimaryButton, SecondaryButton } from '../components/smartTeams/shared';
import type { CuratedSkillRow, CuratedTaxonomyResponse } from '../api/smartTeams';
import {
    createCuratedSkill,
    editCuratedSkill,
    listCuratedSkills,
    smartTeamsError,
} from '../api/smartTeams';

/** Pull `detail.problems` out of a 400, or fall back to one message.
 *
 *  The server deliberately returns every problem at once; a UI that renders only
 *  `smartTeamsError().message` would show the generic fallback instead, because
 *  `detail` here is an object with no `message` key. */
function problemsFrom(err: unknown, fallback: string): string[] {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (detail && typeof detail === 'object' && Array.isArray((detail as { problems?: unknown }).problems)) {
        const problems = (detail as { problems: unknown[] }).problems
            .filter((p): p is string => typeof p === 'string');
        if (problems.length) return problems;
    }
    return [smartTeamsError(err, fallback).message];
}

/** `skill_key` has to be a slug — the interview validates declared keys against
 *  the same shape and would refuse anything else. Suggesting one from the name
 *  saves the admin learning that rule from an error message. */
const slugify = (name: string): string =>
    name
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '_')
        .replace(/^_+|_+$/g, '')
        .slice(0, 64);

export const SmartTeamsCurateSkills: React.FC = () => {
    const [data, setData] = useState<CuratedTaxonomyResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const [showForm, setShowForm] = useState(false);
    const [name, setName] = useState('');
    const [key, setKey] = useState('');
    const [keyTouched, setKeyTouched] = useState(false);
    const [cluster, setCluster] = useState('');
    const [aliasText, setAliasText] = useState('');
    const [problems, setProblems] = useState<string[]>([]);
    const [saving, setSaving] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            setData(await listCuratedSkills());
            setError(null);
        } catch (err) {
            setError(smartTeamsError(err, 'Cubie could not load the skill list.').message);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    const resetForm = useCallback(() => {
        setName('');
        setKey('');
        setKeyTouched(false);
        setCluster('');
        setAliasText('');
        setProblems([]);
    }, []);

    const submit = useCallback(async () => {
        setSaving(true);
        setProblems([]);
        try {
            const created = await createCuratedSkill({
                skill_key: keyTouched ? key : slugify(name),
                name,
                cluster,
                aliases: aliasText
                    .split(',')
                    .map(a => a.trim())
                    .filter(Boolean),
            });
            toast.success(created.notice);
            resetForm();
            setShowForm(false);
            await load();
        } catch (err) {
            setProblems(problemsFrom(err, 'Cubie could not add that skill.'));
        } finally {
            setSaving(false);
        }
    }, [aliasText, cluster, key, keyTouched, name, load, resetForm]);

    const setActive = useCallback(
        async (row: CuratedSkillRow, next: boolean) => {
            if (!next) {
                const ok = await confirmDialog({
                    title: `Retire “${row.name}”?`,
                    message:
                        row.claims > 0
                            ? `${row.claims} ${row.claims === 1 ? 'person has' : 'people have'} this on file. Retiring hides it from the picker and from Cubie's suggestions — what they already claimed stays on their profile. It cannot be deleted.`
                            : 'It disappears from the picker and from Cubie’s suggestions. You can bring it back at any time.',
                    confirmLabel: 'Retire',
                });
                if (!ok) return;
            }
            try {
                await editCuratedSkill(row.skill_key, { is_active: next });
                toast.success(next ? `“${row.name}” is back.` : `“${row.name}” retired.`);
                await load();
            } catch (err) {
                toast.error(smartTeamsError(err, 'Cubie could not change that.').message);
            }
        },
        [load],
    );

    /** Admin-added rows first: they are the only ones anything can be done to,
     *  and burying them under 75 shipped skills would hide the whole point. */
    const { added, shipped } = useMemo(() => {
        const rows = data?.skills ?? [];
        return {
            added: rows.filter(r => !r.managed_in_code),
            shipped: rows.filter(r => r.managed_in_code),
        };
    }, [data]);

    if (loading) return <LoadingState label="Loading the skill list…" />;
    if (error) return <ErrorState message={error} />;
    if (!data) return null;

    return (
        <div className="mx-auto max-w-4xl px-4 py-8">
            <header data-tour="smart-teams-curate-header" className="mb-6 flex items-start justify-between gap-4">
                <div className="flex items-start gap-3">
                    <div className="flex items-center justify-center rounded-md bg-brand p-2.5 text-white">
                        <Wrench size={20} />
                    </div>
                    <div>
                        <h1 className="text-2xl font-semibold text-gray-900">Curate skills</h1>
                        <p className="mt-1 text-sm text-gray-500">
                            The vocabulary everyone declares, searches and gets staffed on.
                        </p>
                    </div>
                </div>
                {!showForm && (
                    <PrimaryButton onClick={() => setShowForm(true)}>
                        <Plus size={14} className="mr-1.5 inline" />
                        Add a skill
                    </PrimaryButton>
                )}
            </header>

            <div className="mb-6 flex items-start gap-2.5 rounded-lg border border-gray-200 bg-gray-50 p-4">
                <ShieldCheck size={16} className="mt-0.5 flex-shrink-0 text-gray-500" />
                <p className="text-sm leading-relaxed text-gray-700">{data.curation_notice}</p>
            </div>

            {showForm && (
                <section className="mb-6 rounded-lg border border-brand/30 bg-white p-5">
                    <h2 className="mb-4 text-sm font-semibold text-gray-900">New skill</h2>
                    <div className="space-y-4">
                        <div>
                            <label htmlFor="sk-name" className="mb-1 block text-sm font-medium text-gray-700">
                                Name
                            </label>
                            <input
                                id="sk-name"
                                type="text"
                                value={name}
                                onChange={e => setName(e.target.value)}
                                placeholder="e.g. Vendor scorecards"
                                className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                            />
                            <p className="mt-1 text-xs text-gray-500">
                                Name the subject, not how well somebody knows it — people pick their
                                own level when they declare it.
                            </p>
                        </div>
                        <div>
                            <label htmlFor="sk-key" className="mb-1 block text-sm font-medium text-gray-700">
                                Key
                            </label>
                            <input
                                id="sk-key"
                                type="text"
                                value={keyTouched ? key : slugify(name)}
                                onChange={e => {
                                    setKeyTouched(true);
                                    setKey(e.target.value);
                                }}
                                placeholder="vendor_scorecards"
                                className="w-full rounded-md border border-gray-300 px-3 py-2 font-mono text-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                            />
                            <p className="mt-1 text-xs text-gray-500">
                                Permanent — it is what every declaration is stored against.
                            </p>
                        </div>
                        <div>
                            <label htmlFor="sk-cluster" className="mb-1 block text-sm font-medium text-gray-700">
                                Cluster
                            </label>
                            <select
                                id="sk-cluster"
                                value={cluster}
                                onChange={e => setCluster(e.target.value)}
                                className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                            >
                                <option value="">Choose one…</option>
                                {data.clusters.map(c => (
                                    <option key={c} value={c}>
                                        {c}
                                    </option>
                                ))}
                            </select>
                        </div>
                        <div>
                            <label htmlFor="sk-aliases" className="mb-1 block text-sm font-medium text-gray-700">
                                Other ways people say it
                            </label>
                            <input
                                id="sk-aliases"
                                type="text"
                                value={aliasText}
                                onChange={e => setAliasText(e.target.value)}
                                placeholder="vendor scorecard, vendor review"
                                className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                            />
                            <p className="mt-1 text-xs text-gray-500">
                                Comma-separated. These are what Cubie looks for in meetings and
                                documents — so the more natural the phrasing, the better it
                                works. The name itself is always searchable.
                            </p>
                        </div>

                        {problems.length > 0 && (
                            <ul className="space-y-1.5 rounded-md border border-red-200 bg-red-50 p-3">
                                {problems.map(p => (
                                    <li key={p} className="text-sm leading-relaxed text-red-800">
                                        {p}
                                    </li>
                                ))}
                            </ul>
                        )}

                        <div className="flex items-center gap-2">
                            <PrimaryButton
                                onClick={submit}
                                disabled={saving || !name.trim() || !cluster}
                            >
                                {saving ? 'Adding…' : 'Add skill'}
                            </PrimaryButton>
                            <SecondaryButton
                                onClick={() => {
                                    resetForm();
                                    setShowForm(false);
                                }}
                            >
                                Cancel
                            </SecondaryButton>
                        </div>
                    </div>
                </section>
            )}

            <section data-tour="smart-teams-curate-added" className="mb-6">
                <h2 className="mb-2 text-sm font-semibold text-gray-900">
                    Added here ({added.length})
                </h2>
                {added.length === 0 ? (
                    <p className="rounded-lg border border-dashed border-gray-300 bg-white p-4 text-sm text-gray-500">
                        Nothing yet. Skills added here survive every release.
                    </p>
                ) : (
                    <ul className="divide-y divide-gray-100 rounded-lg border border-gray-200 bg-white">
                        {added.map(row => (
                            <li key={row.skill_key} className="flex items-start gap-3 px-5 py-3">
                                <div className="min-w-0 flex-1">
                                    <div className="flex flex-wrap items-center gap-2">
                                        <span
                                            className={`text-sm font-medium ${
                                                row.is_active ? 'text-gray-900' : 'text-gray-400'
                                            }`}
                                        >
                                            {row.name}
                                        </span>
                                        {!row.is_active && (
                                            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-600">
                                                Retired
                                            </span>
                                        )}
                                    </div>
                                    <p className="mt-0.5 text-xs text-gray-500">
                                        {row.cluster}
                                        {row.claims > 0 &&
                                            ` · ${row.claims} ${
                                                row.claims === 1 ? 'person' : 'people'
                                            } on file`}
                                    </p>
                                </div>
                                {row.is_active ? (
                                    <SecondaryButton onClick={() => setActive(row, false)}>
                                        Retire
                                    </SecondaryButton>
                                ) : (
                                    <SecondaryButton onClick={() => setActive(row, true)}>
                                        <Undo2 size={13} className="mr-1 inline" />
                                        Restore
                                    </SecondaryButton>
                                )}
                            </li>
                        ))}
                    </ul>
                )}
            </section>

            <section data-tour="smart-teams-curate-shipped" className="mb-6">
                <h2 className="mb-1 text-sm font-semibold text-gray-900">
                    Ships with Cubie ({shipped.length})
                </h2>
                {/* Read-only, and the reason is stated rather than left to be
                    discovered: the seeder rewrites these from the release. */}
                <p className="mb-2 text-xs leading-relaxed text-gray-500">
                    These are part of the app and cannot be changed here — a release would overwrite
                    the edit. Ask RubikSoft to change one in the code.
                </p>
                <div className="flex flex-wrap gap-1.5">
                    {shipped.map(row => (
                        <span
                            key={row.skill_key}
                            title={`${row.cluster ?? ''}${row.claims ? ` · ${row.claims} on file` : ''}`}
                            className={`rounded-full border px-2.5 py-1 text-xs ${
                                row.is_active
                                    ? 'border-gray-200 bg-white text-gray-700'
                                    : 'border-gray-200 bg-gray-50 text-gray-400 line-through'
                            }`}
                        >
                            {row.name}
                        </span>
                    ))}
                </div>
            </section>

            <AdvisoryNotice text={data.advisory_notice} />
        </div>
    );
};

export default SmartTeamsCurateSkills;
