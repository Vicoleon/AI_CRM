import React, { useEffect, useMemo, useState } from 'react';
import { formatInstantDate } from '../../utils/datetime';
import { AlertCircle, CheckCircle2, ChevronDown, ChevronRight, Loader2, Power, PowerOff, RotateCcw, Save, Users } from 'lucide-react';
import {
    type FeatureAccessEntry,
    type FeatureMember,
    type FeaturePolicy,
    fetchFeatureAccess,
    previewFeaturePolicy,
    resetFeaturePolicy,
    saveFeaturePolicy,
} from '../../api/featureAccess';

/**
 * Admin → Feature Access — onboard a department to a feature without a deploy.
 *
 * One card per registered feature (rubik_core/features/registry.py). Each
 * shows the roster in force, whether it is the shipped default or an admin
 * override, the env kill switch (read-only — "switched off here" is a
 * different fact from "nobody onboarded"), and WHO the roster admits today,
 * with the door each person came through. Editing a roster previews the new
 * member list before Save, so opening X-Ray to Marketing shows the six names
 * it adds rather than surprising a support inbox.
 */

/** The server's `detail` string from an axios error, if there is one. */
const apiError = (e: unknown): string | null => {
    const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return typeof detail === 'string' ? detail : null;
};

const slugify = (label: string) =>
    label.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');

const VIA_LABEL: Record<string, string> = {
    admin: 'Admin',
    exec: 'Exec team',
    named: 'Named',
    everyone: 'Everyone',
    department: 'Department',
    'managed team': 'Manages team',
};

const sameList = (a: string[], b: string[]) =>
    a.length === b.length && [...a].sort().every((v, i) => v === [...b].sort()[i]);

const samePolicy = (a: FeaturePolicy, b: FeaturePolicy) =>
    sameList(a.departments, b.departments) &&
    sameList(a.extra_emails, b.extra_emails) &&
    a.exec_allowed === b.exec_allowed &&
    a.admin_allowed === b.admin_allowed &&
    a.everyone === b.everyone;

const MemberList: React.FC<{ members: FeatureMember[]; emptyText: string }> = ({ members, emptyText }) => {
    const [open, setOpen] = useState(false);
    if (members.length === 0) {
        return <p className="text-xs text-amber-700 flex items-center gap-1"><AlertCircle size={13} /> {emptyText}</p>;
    }
    return (
        <div>
            <button
                type="button"
                onClick={() => setOpen(o => !o)}
                className="text-xs text-gray-700 hover:text-brand inline-flex items-center gap-1"
            >
                {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                <Users size={13} />
                {members.length} {members.length === 1 ? 'person has' : 'people have'} access
            </button>
            {open && (
                <ul className="mt-2 grid gap-1 sm:grid-cols-2">
                    {members.map(m => (
                        <li key={m.email} className="text-xs text-gray-700 flex items-baseline justify-between gap-2 rounded bg-gray-50 px-2 py-1">
                            <span className="truncate">
                                <span className="font-medium">{m.name}</span>
                                {m.department && <span className="text-gray-400"> · {m.department}</span>}
                            </span>
                            <span className="shrink-0 text-[10px] uppercase tracking-wide text-gray-500">{VIA_LABEL[m.via] ?? m.via}</span>
                        </li>
                    ))}
                </ul>
            )}
        </div>
    );
};

const FeatureCard: React.FC<{
    entry: FeatureAccessEntry;
    departments: string[];
    onChanged: (next: FeatureAccessEntry) => void;
}> = ({ entry, departments, onChanged }) => {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState<FeaturePolicy>(entry.policy);
    const [emailsText, setEmailsText] = useState(entry.policy.extra_emails.join(', '));
    const [note, setNote] = useState('');
    const [preview, setPreview] = useState<FeatureMember[] | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Departments the picker offers: the live list, plus any slug already on
    // the roster that no live department slugifies to (so a stale roster is
    // visible rather than silently dropped on save).
    const options = useMemo(() => {
        const bySlug = new Map<string, string>();
        departments.forEach(d => bySlug.set(slugify(d), d));
        draft.departments.forEach(s => { if (!bySlug.has(s)) bySlug.set(s, s); });
        return [...bySlug.entries()].sort((a, b) => a[1].localeCompare(b[1]));
    }, [departments, draft.departments]);

    const startEdit = () => {
        setDraft(entry.policy);
        setEmailsText(entry.policy.extra_emails.join(', '));
        setNote('');
        setPreview(null);
        setError(null);
        setEditing(true);
    };

    const currentDraft = (): FeaturePolicy => ({
        ...draft,
        extra_emails: emailsText.split(/[,\s;]+/).map(e => e.trim().toLowerCase()).filter(Boolean),
    });

    // Live what-if: the member list under the roster being edited, debounced
    // so a run of checkbox clicks costs one request.
    useEffect(() => {
        if (!editing) return;
        const body = currentDraft();
        const t = setTimeout(() => {
            previewFeaturePolicy(entry.key, body)
                .then(res => setPreview(res.members))
                .catch(() => setPreview(null));
        }, 300);
        return () => clearTimeout(t);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [editing, draft, emailsText, entry.key]);

    const toggleDept = (slug: string) =>
        setDraft(d => ({
            ...d,
            departments: d.departments.includes(slug)
                ? d.departments.filter(s => s !== slug)
                : [...d.departments, slug],
        }));

    const save = async () => {
        setBusy(true);
        setError(null);
        try {
            const next = await saveFeaturePolicy(entry.key, { ...currentDraft(), note });
            onChanged(next);
            setEditing(false);
        } catch (e) {
            setError(apiError(e) ?? 'Could not save. Try again.');
        } finally {
            setBusy(false);
        }
    };

    const reset = async () => {
        if (!window.confirm(`Reset "${entry.label}" to the roster it shipped with?`)) return;
        setBusy(true);
        setError(null);
        try {
            const next = await resetFeaturePolicy(entry.key);
            onChanged(next);
            setEditing(false);
        } catch (e) {
            setError(apiError(e) ?? 'Could not reset. Try again.');
        } finally {
            setBusy(false);
        }
    };

    const dirty = editing && !samePolicy(currentDraft(), entry.policy);
    const closedDoor =
        !entry.policy.everyone &&
        entry.policy.departments.length === 0 &&
        entry.policy.extra_emails.length === 0 &&
        !entry.policy.exec_allowed &&
        !entry.policy.admin_allowed;

    return (
        <div className="bg-white rounded-lg border border-gray-200 p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                        <h3 className="text-sm font-semibold text-gray-900">{entry.label}</h3>
                        {entry.customized ? (
                            <span className="text-[10px] uppercase tracking-wide rounded-full bg-brand/10 text-brand px-2 py-0.5">Customized</span>
                        ) : (
                            <span className="text-[10px] uppercase tracking-wide rounded-full bg-gray-100 text-gray-600 px-2 py-0.5">Default</span>
                        )}
                        {entry.kill_switch && (
                            <span
                                title={`${entry.kill_switch.name} — an environment setting; change it in the deploy, not here.`}
                                className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 inline-flex items-center gap-1 ${
                                    entry.kill_switch.enabled ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-800'
                                }`}
                            >
                                {entry.kill_switch.enabled ? <Power size={11} /> : <PowerOff size={11} />}
                                {entry.kill_switch.enabled ? 'Switched on' : 'Switched off in this environment'}
                            </span>
                        )}
                    </div>
                    <p className="text-sm text-gray-600 mt-1">{entry.description}</p>
                    <p className="text-xs text-gray-400 mt-1">Where: {entry.surface}</p>
                </div>
                {!editing && (
                    <div className="flex items-center gap-2 shrink-0">
                        {entry.customized && (
                            <button type="button" onClick={reset} disabled={busy} className="text-xs text-gray-600 hover:text-gray-900 inline-flex items-center gap-1 px-2 py-1 rounded border border-gray-200">
                                <RotateCcw size={13} /> Reset to default
                            </button>
                        )}
                        <button type="button" onClick={startEdit} className="text-xs text-white bg-brand hover:bg-brand-hover px-3 py-1.5 rounded">
                            Edit access
                        </button>
                    </div>
                )}
            </div>

            {/* The roster in force, as a sentence an executive could read. */}
            {!editing && (
                <div className="mt-3 space-y-2">
                    <div className="flex flex-wrap items-center gap-1.5 text-xs">
                        {entry.policy.everyone ? (
                            <span className="rounded-full bg-emerald-50 text-emerald-700 px-2 py-0.5">Everyone</span>
                        ) : (
                            <>
                                {entry.policy.departments.length === 0 && <span className="text-gray-500">No departments</span>}
                                {entry.policy.departments.map(s => (
                                    <span key={s} className="rounded-full bg-gray-100 text-gray-800 px-2 py-0.5">
                                        {departments.find(d => slugify(d) === s) ?? s}
                                        {entry.membership === 'managed_only' && <span className="text-gray-400"> (managers)</span>}
                                    </span>
                                ))}
                            </>
                        )}
                        {entry.policy.exec_allowed && <span className="rounded-full bg-indigo-50 text-indigo-700 px-2 py-0.5">Exec team</span>}
                        {entry.policy.admin_allowed && <span className="rounded-full bg-gray-100 text-gray-600 px-2 py-0.5">Admins</span>}
                        {entry.policy.extra_emails.map(e => (
                            <span key={e} className="rounded-full bg-amber-50 text-amber-800 px-2 py-0.5">{e}</span>
                        ))}
                    </div>
                    {closedDoor && (
                        <p className="text-xs text-red-700 flex items-center gap-1"><AlertCircle size={13} /> Closed to everyone — nobody can reach this feature.</p>
                    )}
                    <MemberList members={entry.members} emptyText="Nobody currently qualifies." />
                    {entry.customized && (
                        <p className="text-[11px] text-gray-400">
                            Changed by {entry.updated_by ?? 'an admin'}
                            {entry.updated_at
                                ? ` on ${formatInstantDate(entry.updated_at)}`
                                : ''}
                            {entry.note ? ` — “${entry.note}”` : ''}
                        </p>
                    )}
                    {entry.notes && <p className="text-xs text-gray-500 leading-relaxed border-l-2 border-gray-200 pl-3">{entry.notes}</p>}
                </div>
            )}

            {editing && (
                <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_1fr]">
                    <div className="space-y-4">
                        <label className="flex items-start gap-2 text-sm">
                            <input type="checkbox" className="mt-0.5" checked={draft.everyone} onChange={e => setDraft(d => ({ ...d, everyone: e.target.checked }))} />
                            <span>
                                <span className="font-medium text-gray-900">Everyone</span>
                                <span className="block text-xs text-gray-500">Every signed-in teammate. Department choices below are ignored while this is on.</span>
                            </span>
                        </label>
                        <fieldset disabled={draft.everyone} className={draft.everyone ? 'opacity-50' : ''}>
                            <legend className="text-xs font-medium text-gray-700 mb-1">
                                Departments{entry.membership === 'managed_only' ? ' — judged on the teams a person MANAGES' : ''}
                            </legend>
                            <div className="grid grid-cols-2 gap-1">
                                {options.map(([slug, label]) => (
                                    <label key={slug} className="flex items-center gap-2 text-sm text-gray-800">
                                        <input type="checkbox" checked={draft.departments.includes(slug)} onChange={() => toggleDept(slug)} />
                                        {label}
                                    </label>
                                ))}
                            </div>
                        </fieldset>
                        <div className="grid gap-2">
                            <label className="flex items-center gap-2 text-sm text-gray-800">
                                <input type="checkbox" checked={draft.exec_allowed} onChange={e => setDraft(d => ({ ...d, exec_allowed: e.target.checked }))} />
                                Exec team members
                            </label>
                            <label className="flex items-center gap-2 text-sm text-gray-800">
                                <input type="checkbox" checked={draft.admin_allowed} onChange={e => setDraft(d => ({ ...d, admin_allowed: e.target.checked }))} />
                                Cubie admins (to support the feature)
                            </label>
                        </div>
                        <div>
                            <label className="block text-xs font-medium text-gray-700 mb-1">Named people (emails, comma-separated)</label>
                            <input
                                type="text"
                                value={emailsText}
                                onChange={e => setEmailsText(e.target.value)}
                                placeholder="name@company.com"
                                className="w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
                            />
                        </div>
                        <div>
                            <label className="block text-xs font-medium text-gray-700 mb-1">Why (kept with the change)</label>
                            <input
                                type="text"
                                value={note}
                                onChange={e => setNote(e.target.value)}
                                maxLength={500}
                                placeholder="e.g. Marketing onboarded after the September review"
                                className="w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
                            />
                        </div>
                        {error && <p className="text-xs text-red-700 flex items-center gap-1"><AlertCircle size={13} /> {error}</p>}
                        <div className="flex items-center gap-2">
                            <button type="button" onClick={save} disabled={busy || !dirty} className="inline-flex items-center gap-1 text-sm text-white bg-brand hover:bg-brand-hover disabled:opacity-50 px-3 py-1.5 rounded">
                                {busy ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />} Save
                            </button>
                            <button type="button" onClick={() => setEditing(false)} disabled={busy} className="text-sm text-gray-600 hover:text-gray-900 px-3 py-1.5">
                                Cancel
                            </button>
                        </div>
                    </div>
                    <div className="rounded border border-dashed border-gray-300 p-3 bg-gray-50/60">
                        <p className="text-xs font-medium text-gray-700 mb-2">Who would have access</p>
                        {preview === null ? (
                            <p className="text-xs text-gray-400 inline-flex items-center gap-1"><Loader2 size={12} className="animate-spin" /> Working it out…</p>
                        ) : (
                            <>
                                <p className="text-xs text-gray-600 mb-2">
                                    {preview.length} {preview.length === 1 ? 'person' : 'people'}
                                    {(() => {
                                        const before = new Set(entry.members.map(m => m.email.toLowerCase()));
                                        const after = new Set(preview.map(m => m.email.toLowerCase()));
                                        const added = [...after].filter(e => !before.has(e)).length;
                                        const removed = [...before].filter(e => !after.has(e)).length;
                                        if (!added && !removed) return ' — no change from today.';
                                        return ` — ${added ? `+${added} gained` : ''}${added && removed ? ', ' : ''}${removed ? `−${removed} lose access` : ''}.`;
                                    })()}
                                </p>
                                <ul className="max-h-64 overflow-y-auto space-y-1">
                                    {preview.map(m => {
                                        const isNew = !entry.members.some(x => x.email.toLowerCase() === m.email.toLowerCase());
                                        return (
                                            <li key={m.email} className="text-xs text-gray-700 flex items-baseline justify-between gap-2">
                                                <span className="truncate">
                                                    {isNew && <span className="text-emerald-700 font-medium">+ </span>}
                                                    {m.name}
                                                    {m.department && <span className="text-gray-400"> · {m.department}</span>}
                                                </span>
                                                <span className="shrink-0 text-[10px] uppercase tracking-wide text-gray-500">{VIA_LABEL[m.via] ?? m.via}</span>
                                            </li>
                                        );
                                    })}
                                    {entry.members
                                        .filter(m => !preview.some(x => x.email.toLowerCase() === m.email.toLowerCase()))
                                        .map(m => (
                                            <li key={`lost-${m.email}`} className="text-xs text-red-700 line-through truncate">− {m.name}</li>
                                        ))}
                                </ul>
                            </>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
};

export const FeatureAccessTab: React.FC = () => {
    const [data, setData] = useState<{ features: FeatureAccessEntry[]; departments: string[] } | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [savedFlash, setSavedFlash] = useState<string | null>(null);

    useEffect(() => {
        fetchFeatureAccess()
            .then(setData)
            .catch(() => setError('Could not load feature access.'));
    }, []);

    const onChanged = (next: FeatureAccessEntry) => {
        setData(d => d ? { ...d, features: d.features.map(f => (f.key === next.key ? next : f)) } : d);
        setSavedFlash(next.key);
        setTimeout(() => setSavedFlash(null), 2500);
    };

    const groups = useMemo(() => {
        const order: string[] = [];
        const by = new Map<string, FeatureAccessEntry[]>();
        (data?.features ?? []).forEach(f => {
            if (!by.has(f.group)) { by.set(f.group, []); order.push(f.group); }
            by.get(f.group)!.push(f);
        });
        return order.map(g => [g, by.get(g)!] as const);
    }, [data]);

    return (
        <div>
            <div className="mb-5">
                <h2 className="text-lg font-semibold text-gray-900">Feature Access</h2>
                <p className="text-sm text-gray-600 mt-1 max-w-3xl">
                    Which teams can reach each Cubie feature. Onboarding a department here takes effect the next
                    time people load the app — no code change, no deploy. Each card shows the roster in force
                    and exactly who it admits today; editing previews who gains or loses access before you save.
                </p>
                <p className="text-xs text-gray-500 mt-2 max-w-3xl">
                    A <span className="font-medium">switched off</span> badge means the feature is turned off in this
                    environment by a deploy setting — the roster still decides who will see it once it is switched on.
                </p>
            </div>

            {error && <p className="text-sm text-red-700 flex items-center gap-1 mb-4"><AlertCircle size={15} /> {error}</p>}
            {!data && !error && <p className="text-sm text-gray-500 inline-flex items-center gap-2"><Loader2 size={15} className="animate-spin" /> Loading…</p>}

            {savedFlash && (
                <p className="text-xs text-emerald-700 inline-flex items-center gap-1 mb-3"><CheckCircle2 size={13} /> Saved. Teammates see the change on their next page load.</p>
            )}

            <div className="space-y-8">
                {groups.map(([group, features]) => (
                    <section key={group}>
                        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-3">{group}</h3>
                        <div className="space-y-3">
                            {features.map(f => (
                                <FeatureCard key={f.key} entry={f} departments={data?.departments ?? []} onChanged={onChanged} />
                            ))}
                        </div>
                    </section>
                ))}
            </div>
        </div>
    );
};
