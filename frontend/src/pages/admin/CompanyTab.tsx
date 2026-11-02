// Admin → Company setup: who the company is, what it sells, and the write-ups
// its proposals can carry.
//
// Three sections, each saved on its own:
// - Profile: the name, letterhead facts and a short description of the
//   business. Every AI prompt that needs to know what the company does reads
//   it; with nothing saved the prompts say "the company".
// - Catalog: the products and services, uploaded as a CSV. An upload with any
//   error changes nothing and lists every bad row.
// - Proposal templates: a named write-up plus the catalog rows that price it.
//
// Hook rules (react-hooks/set-state-in-effect is an error in this repo): the
// only effect is the first load, and every setState in it sits inside the
// async callback behind a cancelled flag.
import React, { useEffect, useState } from 'react';
import { AlertCircle, CheckCircle2, FileSpreadsheet, Pencil, Plus, RefreshCw, Save, Trash2, Upload } from 'lucide-react';
import {
    CATALOG_CSV_HEADER,
    companyAdminError,
    createProposalTemplate,
    deleteProposalTemplate,
    emptyTemplateDraft,
    fetchCatalog,
    fetchCompanyProfile,
    fetchProposalTemplates,
    importCatalog,
    productsToText,
    saveCompanyProfile,
    textToProducts,
    updateProposalTemplate,
} from '../../api/company';
import type { CatalogItem, CompanyProfile, ProposalTemplateDraft, ProposalTemplateRow } from '../../api/company';

const CARD = 'rounded-xl border border-gray-200 bg-white p-5';
const LABEL = 'mb-1 block text-xs font-medium text-gray-600';
const INPUT =
    'w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-sm text-gray-900 shadow-sm transition-colors hover:border-brand-soft focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20';
const PRIMARY =
    'inline-flex items-center gap-1.5 rounded-md bg-brand px-3 py-1.5 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-1 active:bg-ink disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-brand';
const SECONDARY =
    'inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 transition-colors hover:border-brand hover:text-brand-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 active:bg-brand-tint disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-gray-300 disabled:hover:text-gray-700';

const SECTION_IDS = { profile: 'company-profile', catalog: 'company-catalog', templates: 'company-templates' } as const;

const Notice: React.FC<{ kind: 'ok' | 'error'; children: React.ReactNode }> = ({ kind, children }) => (
    <p className={`mt-3 flex items-start gap-1.5 text-sm ${kind === 'ok' ? 'text-emerald-800' : 'text-red-700'}`}>
        {kind === 'ok' ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" /> : <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />}
        <span>{children}</span>
    </p>
);

const SectionLink: React.FC<{ to: keyof typeof SECTION_IDS; children: React.ReactNode }> = ({ to, children }) => (
    <a href={`#${SECTION_IDS[to]}`} className="rounded-sm font-medium text-brand-hover underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 active:text-ink">
        {children}
    </a>
);

// ── Profile ──────────────────────────────────────────────────────────────────

const PROFILE_FIELDS: { key: Exclude<keyof CompanyProfile, 'products'>; label: string; hint: string; long?: boolean }[] = [
    { key: 'name', label: 'Company name', hint: 'How people refer to the company, e.g. Acme Corp.' },
    { key: 'legal_name', label: 'Legal name', hint: 'Printed on proposals, e.g. Acme Corporation LLC.' },
    { key: 'website', label: 'Website', hint: 'Printed on proposals.' },
    { key: 'address', label: 'Address', hint: 'Printed on proposals. One line per line of the address.', long: true },
    { key: 'industry', label: 'Industry', hint: 'One or two words, e.g. Industrial cleaning.' },
    { key: 'offering', label: 'What the company sells', hint: 'A sentence or two.', long: true },
    { key: 'customers', label: 'Who it sells to', hint: 'The kind of customer, in a sentence.', long: true },
    { key: 'sales_process', label: 'How it sells', hint: 'The steps from first call to signed deal, in a few lines.', long: true },
    { key: 'blurb', label: 'Short description', hint: 'One sentence about the company.', long: true },
];

const ProfileSection: React.FC<{ initial: CompanyProfile; onSaved: (p: CompanyProfile) => void }> = ({ initial, onSaved }) => {
    const [draft, setDraft] = useState<CompanyProfile>(initial);
    const [productsText, setProductsText] = useState(productsToText(initial.products));
    const [saving, setSaving] = useState(false);
    const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);

    const save = async () => {
        setSaving(true);
        setMessage(null);
        try {
            const saved = await saveCompanyProfile({ ...draft, products: textToProducts(productsText) });
            setDraft(saved);
            setProductsText(productsToText(saved.products));
            onSaved(saved);
            setMessage({ kind: 'ok', text: 'Saved. AI answers, drafts and proposals use it from now on.' });
        } catch (err) {
            setMessage({ kind: 'error', text: companyAdminError(err, 'Could not save the company profile.') });
        } finally {
            setSaving(false);
        }
    };

    return (
        <section id={SECTION_IDS.profile} data-tour="company-profile" className={CARD}>
            <h3 className="text-base font-semibold text-gray-900">Profile</h3>
            <p className="mt-1 text-sm text-gray-600">
                Every AI feature that needs to know what the company does reads this: chat, call wrap-ups, CRM Sync,
                Customer X-Ray, Meeting Prep and proposals. Leave a field blank rather than guess; blank fields are simply left out.
            </p>
            <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
                {PROFILE_FIELDS.map(field => (
                    <div key={field.key} className={field.long ? 'md:col-span-2' : ''}>
                        <label className={LABEL} htmlFor={`company_${field.key}`}>
                            {field.label}
                        </label>
                        {field.long ? (
                            <textarea
                                id={`company_${field.key}`}
                                rows={2}
                                className={INPUT}
                                value={draft[field.key]}
                                onChange={e => setDraft({ ...draft, [field.key]: e.target.value })}
                            />
                        ) : (
                            <input
                                id={`company_${field.key}`}
                                className={INPUT}
                                value={draft[field.key]}
                                onChange={e => setDraft({ ...draft, [field.key]: e.target.value })}
                            />
                        )}
                        <p className="mt-1 text-xs text-gray-500">{field.hint}</p>
                    </div>
                ))}
                <div className="md:col-span-2">
                    <label className={LABEL} htmlFor="company_products">
                        Main products or services
                    </label>
                    <textarea
                        id="company_products"
                        rows={3}
                        className={INPUT}
                        value={productsText}
                        onChange={e => setProductsText(e.target.value)}
                    />
                    <p className="mt-1 text-xs text-gray-500">
                        One per line, a few words each. Prices belong in the <SectionLink to="catalog">catalog</SectionLink>.
                    </p>
                </div>
            </div>
            <div className="mt-4">
                <button type="button" className={PRIMARY} disabled={saving} onClick={save}>
                    <Save className="h-4 w-4" /> {saving ? 'Saving…' : 'Save profile'}
                </button>
            </div>
            {message && <Notice kind={message.kind}>{message.text}</Notice>}
        </section>
    );
};

// ── Catalog ──────────────────────────────────────────────────────────────────

const CatalogSection: React.FC<{ items: CatalogItem[]; onReplaced: (items: CatalogItem[]) => void }> = ({ items, onReplaced }) => {
    const [uploading, setUploading] = useState(false);
    const [errors, setErrors] = useState<string[]>([]);
    const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);

    const upload = async (file: File) => {
        setUploading(true);
        setErrors([]);
        setMessage(null);
        try {
            const result = await importCatalog(await file.text());
            if (result.errors.length > 0) {
                setErrors(result.errors);
                setMessage({ kind: 'error', text: 'Nothing was changed. Fix these rows and upload the file again.' });
                return;
            }
            onReplaced(await fetchCatalog());
            setMessage({ kind: 'ok', text: `The catalog now has ${result.created} item${result.created === 1 ? '' : 's'}.` });
        } catch (err) {
            setMessage({ kind: 'error', text: companyAdminError(err, 'Could not upload the catalog.') });
        } finally {
            setUploading(false);
        }
    };

    return (
        <section id={SECTION_IDS.catalog} data-tour="company-catalog" className={CARD}>
            <h3 className="text-base font-semibold text-gray-900">Catalog</h3>
            <p className="mt-1 text-sm text-gray-600">
                The products and services the company sells. Upload a CSV whose first row is{' '}
                <code className="rounded bg-gray-100 px-1 text-xs">{CATALOG_CSV_HEADER}</code>. Only the name is required; leave
                the price blank for anything priced per job. An upload replaces the whole catalog.
            </p>
            <label className={`${SECONDARY} mt-4 cursor-pointer`}>
                <Upload className="h-4 w-4" /> {uploading ? 'Uploading…' : 'Upload CSV'}
                <input
                    type="file"
                    accept=".csv,text/csv"
                    className="sr-only"
                    disabled={uploading}
                    onChange={e => {
                        const file = e.target.files?.[0];
                        e.target.value = '';
                        if (file) void upload(file);
                    }}
                />
            </label>
            {message && <Notice kind={message.kind}>{message.text}</Notice>}
            {errors.length > 0 && (
                <ul className="mt-2 list-disc space-y-0.5 pl-5 text-sm text-red-700">
                    {errors.map(error => (
                        <li key={error}>{error}</li>
                    ))}
                </ul>
            )}
            {items.length === 0 ? (
                <p className="mt-4 flex items-center gap-2 rounded-md bg-gray-50 p-3 text-sm text-gray-600">
                    <FileSpreadsheet className="h-4 w-4 shrink-0" /> No catalog yet. Customer X-Ray finds no cross-sell
                    opportunities and proposals carry no list prices until one is uploaded.
                </p>
            ) : (
                <div className="mt-4 overflow-x-auto">
                    <table className="w-full text-left text-sm">
                        <thead className="border-b border-gray-200 text-xs uppercase text-gray-500">
                            <tr>
                                <th className="py-2 pr-3">Name</th>
                                <th className="py-2 pr-3">Category</th>
                                <th className="py-2 pr-3">Price</th>
                                <th className="py-2">Description</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100">
                            {items.map(item => (
                                <tr key={item.name}>
                                    <td className="py-2 pr-3 font-medium text-gray-900">{item.name}</td>
                                    <td className="py-2 pr-3 text-gray-600">{item.category || '—'}</td>
                                    <td className="py-2 pr-3 text-gray-600">{item.price_label || '—'}</td>
                                    <td className="py-2 text-gray-600">{item.description || '—'}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </section>
    );
};

// ── Proposal templates ───────────────────────────────────────────────────────

const TemplateEditor: React.FC<{
    initial: ProposalTemplateDraft;
    catalog: CatalogItem[];
    saving: boolean;
    onSave: (draft: ProposalTemplateDraft) => void;
    onCancel: () => void;
}> = ({ initial, catalog, saving, onSave, onCancel }) => {
    const [draft, setDraft] = useState<ProposalTemplateDraft>(initial);
    const picked = new Set(draft.catalog_item_names);
    const toggle = (name: string, on: boolean) =>
        setDraft({
            ...draft,
            catalog_item_names: on ? [...draft.catalog_item_names, name] : draft.catalog_item_names.filter(n => n !== name),
        });

    return (
        <div className="mt-4 space-y-3 rounded-lg border border-gray-200 bg-gray-50 p-4">
            <div>
                <label className={LABEL} htmlFor="template_name">
                    Name
                </label>
                <input id="template_name" className={INPUT} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} />
            </div>
            <div>
                <label className={LABEL} htmlFor="template_body">
                    Write-up
                </label>
                <textarea
                    id="template_body"
                    rows={8}
                    className={`${INPUT} font-mono`}
                    value={draft.body_md}
                    onChange={e => setDraft({ ...draft, body_md: e.target.value })}
                />
                <p className="mt-1 text-xs text-gray-500">
                    Printed after the proposal&apos;s scope of work, word for word. Start a line with # for a heading and - for a
                    bullet. Leave prices out: they come from the catalog items below.
                </p>
            </div>
            <div>
                <p className={LABEL}>Catalog items that price it</p>
                {catalog.length === 0 ? (
                    <p className="text-xs text-gray-500">
                        Upload a <SectionLink to="catalog">catalog</SectionLink> to price this template.
                    </p>
                ) : (
                    <ul className="grid grid-cols-1 gap-1 md:grid-cols-2">
                        {catalog.map(item => (
                            <li key={item.name}>
                                <label className="flex items-center gap-2 text-sm text-gray-800">
                                    <input
                                        type="checkbox"
                                        className="h-4 w-4 rounded border-gray-300 text-brand focus:ring-2 focus:ring-brand/40 focus:ring-offset-0"
                                        checked={picked.has(item.name)}
                                        onChange={e => toggle(item.name, e.target.checked)}
                                    />
                                    {item.name}
                                    {item.price_label && <span className="text-xs text-gray-500">{item.price_label}</span>}
                                </label>
                            </li>
                        ))}
                    </ul>
                )}
            </div>
            <label className="flex items-center gap-2 text-sm text-gray-800">
                <input
                    type="checkbox"
                    className="h-4 w-4 rounded border-gray-300 text-brand focus:ring-2 focus:ring-brand/40 focus:ring-offset-0"
                    checked={draft.active}
                    onChange={e => setDraft({ ...draft, active: e.target.checked })}
                />
                Offer it on new proposals
            </label>
            <div className="flex gap-2">
                <button type="button" className={PRIMARY} disabled={saving || !draft.name.trim()} onClick={() => onSave(draft)}>
                    <Save className="h-4 w-4" /> {saving ? 'Saving…' : 'Save template'}
                </button>
                <button type="button" className={SECONDARY} disabled={saving} onClick={onCancel}>
                    Cancel
                </button>
            </div>
        </div>
    );
};

const TemplatesSection: React.FC<{
    templates: ProposalTemplateRow[];
    catalog: CatalogItem[];
    onChanged: (templates: ProposalTemplateRow[]) => void;
}> = ({ templates, catalog, onChanged }) => {
    // 'new' = the create form; a number = the template being edited.
    const [editing, setEditing] = useState<'new' | number | null>(null);
    const [saving, setSaving] = useState(false);
    const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);

    const save = async (draft: ProposalTemplateDraft) => {
        setSaving(true);
        setMessage(null);
        try {
            if (editing === 'new') await createProposalTemplate(draft);
            else if (typeof editing === 'number') await updateProposalTemplate(editing, draft);
            onChanged(await fetchProposalTemplates());
            setEditing(null);
            setMessage({ kind: 'ok', text: `Saved “${draft.name.trim()}”.` });
        } catch (err) {
            setMessage({ kind: 'error', text: companyAdminError(err, 'Could not save the template.') });
        } finally {
            setSaving(false);
        }
    };

    const remove = async (row: ProposalTemplateRow) => {
        if (!window.confirm(`Delete the template “${row.name}”? Proposals already drafted with it print without it.`)) return;
        setMessage(null);
        try {
            await deleteProposalTemplate(row.id);
            onChanged(await fetchProposalTemplates());
        } catch (err) {
            setMessage({ kind: 'error', text: companyAdminError(err, 'Could not delete the template.') });
        }
    };

    return (
        <section id={SECTION_IDS.templates} data-tour="company-templates" className={CARD}>
            <div className="flex items-start justify-between gap-3">
                <div>
                    <h3 className="text-base font-semibold text-gray-900">Proposal templates</h3>
                    <p className="mt-1 text-sm text-gray-600">
                        A standard write-up of one service that a proposal can carry, plus the catalog items that price it. The AI
                        picks the template that fits the call; the account executive can change it.
                    </p>
                </div>
                {editing === null && (
                    <button type="button" className={SECONDARY} onClick={() => setEditing('new')}>
                        <Plus className="h-4 w-4" /> New template
                    </button>
                )}
            </div>
            {editing === 'new' && (
                <TemplateEditor initial={emptyTemplateDraft()} catalog={catalog} saving={saving} onSave={save} onCancel={() => setEditing(null)} />
            )}
            {message && <Notice kind={message.kind}>{message.text}</Notice>}
            {templates.length === 0 && editing === null ? (
                <p className="mt-4 rounded-md bg-gray-50 p-3 text-sm text-gray-600">
                    No templates yet. Proposals are written from the call alone, with prices only from what was said on it.
                </p>
            ) : (
                <ul className="mt-4 divide-y divide-gray-100">
                    {templates.map(row => (
                        <li key={row.id} className="py-3">
                            {editing === row.id ? (
                                <TemplateEditor
                                    initial={{
                                        name: row.name,
                                        body_md: row.body_md,
                                        catalog_item_names: row.catalog_item_names,
                                        active: row.active,
                                    }}
                                    catalog={catalog}
                                    saving={saving}
                                    onSave={save}
                                    onCancel={() => setEditing(null)}
                                />
                            ) : (
                                <div className="flex items-start justify-between gap-3">
                                    <div>
                                        <p className="text-sm font-medium text-gray-900">
                                            {row.name}
                                            {!row.active && <span className="ml-2 text-xs font-normal text-gray-500">(not offered)</span>}
                                        </p>
                                        <p className="text-xs text-gray-500">
                                            {row.catalog_item_names.length > 0
                                                ? `Priced by: ${row.catalog_item_names.join(', ')}`
                                                : 'No catalog prices'}
                                        </p>
                                    </div>
                                    <div className="flex gap-2">
                                        <button type="button" className={SECONDARY} onClick={() => setEditing(row.id)} disabled={editing !== null}>
                                            <Pencil className="h-4 w-4" /> Edit
                                        </button>
                                        <button type="button" className={SECONDARY} onClick={() => void remove(row)} disabled={editing !== null}>
                                            <Trash2 className="h-4 w-4" /> Delete
                                        </button>
                                    </div>
                                </div>
                            )}
                        </li>
                    ))}
                </ul>
            )}
        </section>
    );
};

// ── The tab ──────────────────────────────────────────────────────────────────

interface Loaded {
    profile: CompanyProfile;
    catalog: CatalogItem[];
    templates: ProposalTemplateRow[];
}

export const CompanyTab: React.FC = () => {
    const [data, setData] = useState<Loaded | null>(null);
    const [loadError, setLoadError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const [profile, catalog, templates] = await Promise.all([
                    fetchCompanyProfile(),
                    fetchCatalog(),
                    fetchProposalTemplates(),
                ]);
                if (!cancelled) setData({ profile, catalog, templates });
            } catch (err) {
                if (!cancelled) setLoadError(companyAdminError(err, 'Could not load the company setup.'));
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    if (data === null && loadError === null) {
        return (
            <div className="flex items-center gap-2 p-6 text-sm text-gray-500">
                <RefreshCw className="h-4 w-4 animate-spin" /> Loading the company setup…
            </div>
        );
    }
    if (data === null) {
        return (
            <div className="flex items-center gap-2 p-6 text-sm text-red-700">
                <AlertCircle className="h-5 w-5" /> {loadError}
            </div>
        );
    }

    const missing = [
        !data.profile.name && !data.profile.offering ? { to: 'profile' as const, label: 'the profile' } : null,
        data.catalog.length === 0 ? { to: 'catalog' as const, label: 'the catalog' } : null,
        data.templates.length === 0 ? { to: 'templates' as const, label: 'a proposal template' } : null,
    ].filter((m): m is { to: keyof typeof SECTION_IDS; label: string } => m !== null);

    return (
        <div className="space-y-5">
            <div>
                <h2 className="text-lg font-semibold text-gray-900">Company setup</h2>
                <p className="mt-1 text-sm text-gray-600">
                    Tell Cubie about the company so its answers, drafts and proposals speak for it.
                </p>
                {missing.length > 0 && (
                    <p className="mt-2 text-sm text-gray-700">
                        Still to add:{' '}
                        {missing.map((m, i) => (
                            <React.Fragment key={m.to}>
                                {i > 0 && ', '}
                                <SectionLink to={m.to}>{m.label}</SectionLink>
                            </React.Fragment>
                        ))}
                        .
                    </p>
                )}
            </div>
            <ProfileSection initial={data.profile} onSaved={profile => setData(prev => prev && { ...prev, profile })} />
            <CatalogSection items={data.catalog} onReplaced={catalog => setData(prev => prev && { ...prev, catalog })} />
            <TemplatesSection
                templates={data.templates}
                catalog={data.catalog}
                onChanged={templates => setData(prev => prev && { ...prev, templates })}
            />
        </div>
    );
};
