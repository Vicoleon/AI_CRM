/** Proposal Generator — confirm the brief, generate, revise, download.
 *
 *  Three steps on one screen, left to right, in the order the product insists
 *  on:
 *
 *  1. THE BRIEF (left). What Cubie heard on the call (the copy follows the
 *     origin, see briefCopy.ts), as a form the AE corrects and confirms: who
 *     it is for, the service, the scope items that were discussed, the
 *     outcomes the PROSPECT stated (each with their words), any pricing that
 *     was said, notes, and the open questions. Items Cubie
 *     inferred rather than heard are flagged; a pricing line with no price is
 *     flagged. Nothing is generated until the AE clicks Confirm — an AE's
 *     experience is that "all the information is not always confirmed on the
 *     call" and a draft built on a guessed price is the one you redo three
 *     times.
 *  2. THE DOCUMENT (right). Generated from the confirmed brief. The preview is
 *     the same structure the PDF prints — Executive Summary, Scope of Work,
 *     Desired Outcomes & Key Deliverables, Pricing & Investment — and the
 *     pricing table is the brief's, never the model's.
 *  3. REVISE (right, under the document). Type what to change; a new version
 *     appears and the old one stays. Any version downloads as PDF or Word.
 *
 *  Cubie never sends the document. The AE downloads it and sends it.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
    AlertTriangle,
    CheckCircle2,
    Download,
    FileSignature,
    FileText,
    Plus,
    Quote,
    Sparkles,
    Trash2,
} from 'lucide-react';
import {
    downloadProposal,
    emptyOutcome,
    emptyPricingLine,
    emptyScopeItem,
    fetchProposal,
    fetchProposalTemplates,
    generateProposal,
    isNotEnabled,
    latestVersion,
    proposalsError,
    reviseProposal,
    saveBrief,
    type Brief,
    type Proposal as ProposalData,
    type ProposalTemplate,
    type ProposalVersion,
} from '../../api/proposals';
import { BackButton } from '../../components/BackButton';
import { ProposalTemplatePicker } from '../../components/calls/ProposalTemplatePicker';
import { briefCopy, proposalOrigin, type BriefCopy } from '../../components/proposals/briefCopy';
import { toast } from '../../components/eos/Toast';
import { PrimaryButton, SecondaryButton } from '../../components/smartTeams/shared';
import { parseInstant } from '../../utils/datetime';
import { EosPageHeader, ErrorState, LoadingState, PageContainer } from '../eos/_shared';

// ─── styling ─────────────────────────────────────────────────────────────────

const CARD = 'rounded-lg border border-gray-200 bg-white shadow-sm';
const LABEL = 'block text-xs font-medium text-gray-600 mb-1';
const INPUT =
    'w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-sm text-gray-900 shadow-sm focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand';
const TEXTAREA = `${INPUT} min-h-[64px] resize-y`;
const FLAG = 'mt-1 inline-flex items-center gap-1 text-[11px] text-amber-700';
const SECTION_TITLE = 'text-xs font-semibold uppercase tracking-wide text-brand';
const ICON_BTN =
    'inline-flex h-7 w-7 items-center justify-center rounded-md text-gray-400 hover:bg-gray-100 hover:text-red-600 focus:outline-none focus:ring-2 focus:ring-brand';

const shortWhen = (iso: string | null): string => {
    if (!iso) return '';
    const d = parseInstant(iso);
    return d ? d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '';
};

function saveBlob(data: Blob, filename: string): void {
    const url = URL.createObjectURL(data);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
}

const deepCopy = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

// ─── small pieces ────────────────────────────────────────────────────────────

const SectionHead: React.FC<{ title: string; hint?: string; onAdd?: () => void; addLabel?: string }> = ({
    title,
    hint,
    onAdd,
    addLabel,
}) => (
    <div className="mb-2 flex items-start justify-between gap-3">
        <div>
            <h3 className={SECTION_TITLE}>{title}</h3>
            {hint && <p className="mt-0.5 text-xs text-gray-500">{hint}</p>}
        </div>
        {onAdd && (
            <button
                type="button"
                onClick={onAdd}
                className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-brand hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-brand"
            >
                <Plus size={13} />
                {addLabel ?? 'Add'}
            </button>
        )}
    </div>
);

const RemoveButton: React.FC<{ onClick: () => void; label: string }> = ({ onClick, label }) => (
    <button type="button" onClick={onClick} className={ICON_BTN} aria-label={label} title={label}>
        <Trash2 size={14} />
    </button>
);

const InferredFlag: React.FC<{ text: string }> = ({ text }) => (
    <span className={FLAG}>
        <AlertTriangle size={12} />
        {text}
    </span>
);

// ─── the brief form ──────────────────────────────────────────────────────────

const BriefForm: React.FC<{
    brief: Brief;
    onChange: (next: Brief) => void;
    disabled: boolean;
    /** The sentences that depend on where the proposal came from. */
    copy: BriefCopy;
    /** Marketing's templates; null while loading. */
    templates: ProposalTemplate[] | null;
}> = ({ brief, onChange, disabled, copy, templates }) => {
    const set = (patch: Partial<Brief>) => onChange({ ...brief, ...patch });
    const setFor = (patch: Partial<Brief['prepared_for']>) =>
        set({ prepared_for: { ...brief.prepared_for, ...patch } });

    return (
        <fieldset disabled={disabled} className="space-y-6">
            <div>
                <label className={LABEL} htmlFor="service_title">
                    Service title
                </label>
                <input
                    id="service_title"
                    className={INPUT}
                    value={brief.service_title}
                    onChange={e => set({ service_title: e.target.value })}
                    placeholder="e.g. Quarterly Operations Review"
                />
            </div>

            <ProposalTemplatePicker brief={brief} templates={templates} onChange={set} />

            <div>
                <SectionHead title="Prepared for" />
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                    <div>
                        <label className={LABEL}>Name</label>
                        <input className={INPUT} value={brief.prepared_for.name} onChange={e => setFor({ name: e.target.value })} />
                    </div>
                    <div>
                        <label className={LABEL}>Title</label>
                        <input className={INPUT} value={brief.prepared_for.title} onChange={e => setFor({ title: e.target.value })} />
                    </div>
                    <div>
                        <label className={LABEL}>Company</label>
                        <input
                            className={INPUT}
                            value={brief.prepared_for.company ?? ''}
                            onChange={e => setFor({ company: e.target.value })}
                        />
                    </div>
                </div>
            </div>

            <div>
                <label className={LABEL}>What they need, in two sentences</label>
                <textarea
                    className={TEXTAREA}
                    value={brief.engagement_summary}
                    onChange={e => set({ engagement_summary: e.target.value })}
                />
            </div>

            <div>
                <SectionHead
                    title="Scope of work"
                    hint={copy.scopeHint}
                    onAdd={() => set({ scope_items: [...brief.scope_items, emptyScopeItem()] })}
                    addLabel="Add item"
                />
                <ol className="space-y-3">
                    {brief.scope_items.map((item, i) => (
                        <li key={item.id} className="rounded-md border border-gray-200 p-3">
                            <div className="flex items-start gap-2">
                                <span className="mt-1.5 w-5 flex-shrink-0 text-xs font-semibold text-gray-400">{i + 1}.</span>
                                <div className="flex-1 space-y-2">
                                    <input
                                        className={INPUT}
                                        value={item.title}
                                        placeholder="Deliverable name"
                                        onChange={e =>
                                            set({
                                                scope_items: brief.scope_items.map(s =>
                                                    s.id === item.id ? { ...s, title: e.target.value } : s,
                                                ),
                                            })
                                        }
                                    />
                                    <textarea
                                        className={TEXTAREA}
                                        value={item.detail}
                                        placeholder="What it covers"
                                        onChange={e =>
                                            set({
                                                scope_items: brief.scope_items.map(s =>
                                                    s.id === item.id ? { ...s, detail: e.target.value } : s,
                                                ),
                                            })
                                        }
                                    />
                                    {!item.confirmed && (
                                        <InferredFlag text={item.quote ? copy.scopeInferredQuoted : copy.scopeUnconfirmed} />
                                    )}
                                    {item.quote && (
                                        <p className="flex items-start gap-1 text-[11px] italic text-gray-500">
                                            <Quote size={11} className="mt-0.5 flex-shrink-0" />
                                            {item.quote}
                                        </p>
                                    )}
                                </div>
                                <RemoveButton
                                    label="Remove scope item"
                                    onClick={() => set({ scope_items: brief.scope_items.filter(s => s.id !== item.id) })}
                                />
                            </div>
                        </li>
                    ))}
                </ol>
                {brief.scope_items.length === 0 && (
                    <p className="text-xs text-gray-500">No scope items yet — add what your company would do.</p>
                )}
            </div>

            <div>
                <SectionHead
                    title="Desired outcomes"
                    hint={copy.outcomesHint}
                    onAdd={() => set({ desired_outcomes: [...brief.desired_outcomes, emptyOutcome()] })}
                    addLabel="Add outcome"
                />
                <ul className="space-y-3">
                    {brief.desired_outcomes.map(item => (
                        <li key={item.id} className="rounded-md border border-gray-200 p-3">
                            <div className="flex items-start gap-2">
                                <div className="flex-1 space-y-1.5">
                                    <input
                                        className={INPUT}
                                        value={item.outcome}
                                        placeholder="What they want to achieve"
                                        onChange={e =>
                                            set({
                                                desired_outcomes: brief.desired_outcomes.map(o =>
                                                    o.id === item.id ? { ...o, outcome: e.target.value } : o,
                                                ),
                                            })
                                        }
                                    />
                                    {item.quote ? (
                                        <p className="flex items-start gap-1 text-[11px] italic text-gray-500">
                                            <Quote size={11} className="mt-0.5 flex-shrink-0" />
                                            “{item.quote}”
                                            {item.entry_sequence != null && (
                                                <span className="not-italic text-gray-400">— line {item.entry_sequence}</span>
                                            )}
                                        </p>
                                    ) : (
                                        <InferredFlag text={copy.outcomeAdded} />
                                    )}
                                </div>
                                <RemoveButton
                                    label="Remove outcome"
                                    onClick={() =>
                                        set({ desired_outcomes: brief.desired_outcomes.filter(o => o.id !== item.id) })
                                    }
                                />
                            </div>
                        </li>
                    ))}
                </ul>
                {brief.desired_outcomes.length === 0 && (
                    <p className="text-xs text-gray-500">{copy.outcomesEmpty}</p>
                )}
            </div>

            <div>
                <SectionHead
                    title="Pricing"
                    hint={copy.pricingHint}
                    onAdd={() => set({ pricing: [...brief.pricing, emptyPricingLine()] })}
                    addLabel="Add line"
                />
                <ul className="space-y-3">
                    {brief.pricing.map(line => (
                        <li key={line.id} className="rounded-md border border-gray-200 p-3">
                            <div className="flex items-start gap-2">
                                <div className="flex-1 space-y-2">
                                    <input
                                        className={INPUT}
                                        value={line.description}
                                        placeholder="What this line prices"
                                        onChange={e =>
                                            set({
                                                pricing: brief.pricing.map(p =>
                                                    p.id === line.id ? { ...p, description: e.target.value } : p,
                                                ),
                                            })
                                        }
                                    />
                                    <div className="grid grid-cols-2 gap-2">
                                        <div>
                                            <label className={LABEL}>Standard rate (optional)</label>
                                            <input
                                                className={INPUT}
                                                value={line.standard_rate}
                                                placeholder="$6,500"
                                                onChange={e =>
                                                    set({
                                                        pricing: brief.pricing.map(p =>
                                                            p.id === line.id ? { ...p, standard_rate: e.target.value } : p,
                                                        ),
                                                    })
                                                }
                                            />
                                        </div>
                                        <div>
                                            <label className={LABEL}>Proposal price</label>
                                            <input
                                                className={INPUT}
                                                value={line.proposal_price}
                                                placeholder="$5,500"
                                                onChange={e =>
                                                    set({
                                                        pricing: brief.pricing.map(p =>
                                                            p.id === line.id
                                                                ? { ...p, proposal_price: e.target.value, confirmed: Boolean(e.target.value) }
                                                                : p,
                                                        ),
                                                    })
                                                }
                                            />
                                        </div>
                                    </div>
                                    {!line.proposal_price && <InferredFlag text={copy.priceMissing} />}
                                </div>
                                <RemoveButton
                                    label="Remove pricing line"
                                    onClick={() => set({ pricing: brief.pricing.filter(p => p.id !== line.id) })}
                                />
                            </div>
                        </li>
                    ))}
                </ul>
            </div>

            <div>
                <SectionHead
                    title="Notes"
                    hint="Travel, optional add-ons, terms — anything that belongs under the pricing table."
                    onAdd={() => set({ notes: [...brief.notes, ''] })}
                    addLabel="Add note"
                />
                <ul className="space-y-2">
                    {brief.notes.map((note, i) => (
                        <li key={i} className="flex items-start gap-2">
                            <input
                                className={INPUT}
                                value={note}
                                onChange={e => set({ notes: brief.notes.map((n, j) => (j === i ? e.target.value : n)) })}
                            />
                            <RemoveButton label="Remove note" onClick={() => set({ notes: brief.notes.filter((_, j) => j !== i) })} />
                        </li>
                    ))}
                </ul>
            </div>

            <div>
                <label className={LABEL}>Timing (optional)</label>
                <input
                    className={INPUT}
                    value={brief.timeline}
                    placeholder="e.g. Onsite review in Q4; report within sixty business days"
                    onChange={e => set({ timeline: e.target.value })}
                />
            </div>

            {brief.open_questions.length > 0 && (
                <div className="rounded-md border border-amber-200 bg-amber-50 p-3">
                    <h3 className="flex items-center gap-1.5 text-xs font-semibold text-amber-900">
                        <AlertTriangle size={13} />
                        {copy.openQuestionsTitle}
                    </h3>
                    <ul className="mt-2 space-y-1.5">
                        {brief.open_questions.map((q, i) => (
                            <li key={i} className="flex items-start gap-2 text-xs text-amber-900">
                                <span className="flex-1">{q}</span>
                                <button
                                    type="button"
                                    className="text-[11px] font-medium text-amber-800 underline-offset-2 hover:underline"
                                    onClick={() => set({ open_questions: brief.open_questions.filter((_, j) => j !== i) })}
                                >
                                    Settled
                                </button>
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </fieldset>
    );
};

// ─── the document preview ────────────────────────────────────────────────────

const DocumentPreview: React.FC<{ version: ProposalVersion; templates: ProposalTemplate[] | null }> = ({
    version,
    templates,
}) => {
    const { brief, content } = version;
    const tpl = templates?.find(t => t.key === brief.template?.key) ?? null;
    // Same rule as render.py: a standard rate prints only where it differs from the price.
    const hasStd = brief.pricing.some(p => p.standard_rate.trim() && p.standard_rate.trim() !== p.proposal_price.trim());
    return (
        <article className="space-y-4 text-sm leading-relaxed text-gray-800">
            <header>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-brand">{content.document_title || 'Services Proposal'}</p>
                <h2 className="text-xl font-semibold text-gray-900">{content.headline || brief.service_title}</h2>
                <p className="mt-1 text-xs text-gray-500">
                    Prepared for {[brief.prepared_for.name, brief.prepared_for.title, brief.prepared_for.company].filter(Boolean).join(', ') || '—'}
                    {' · '}
                    {brief.date}
                    {brief.ref_code ? ` · ${brief.ref_code}` : ''}
                </p>
            </header>
            {content.executive_summary && (
                <section>
                    <h3 className={SECTION_TITLE}>Executive summary</h3>
                    {content.executive_summary.split(/\n\s*\n/).map((p, i) => (
                        <p key={i} className="mt-1">
                            {p}
                        </p>
                    ))}
                </section>
            )}
            {content.scope.length > 0 && (
                <section>
                    <h3 className={SECTION_TITLE}>Scope of work</h3>
                    {content.scope_intro && <p className="mt-1">{content.scope_intro}</p>}
                    <ol className="mt-1 space-y-1.5">
                        {content.scope.map((s, i) => (
                            <li key={s.id}>
                                <span className="font-medium text-gray-900">
                                    {i + 1}. {s.heading}
                                </span>
                                {s.body && <p className="ml-4 text-gray-700">{s.body}</p>}
                            </li>
                        ))}
                    </ol>
                </section>
            )}
            {content.outcomes.length > 0 && (
                <section>
                    <h3 className={SECTION_TITLE}>Desired outcomes &amp; key deliverables</h3>
                    {content.outcomes_intro && <p className="mt-1">{content.outcomes_intro}</p>}
                    <ul className="mt-1 space-y-1.5">
                        {content.outcomes.map(o => (
                            <li key={o.id}>
                                <span className="font-medium text-gray-900">{o.heading}:</span> {o.body}
                            </li>
                        ))}
                    </ul>
                </section>
            )}
            {tpl && (
                <section className="rounded-md border border-dashed border-gray-300 bg-gray-50 p-3">
                    <h3 className={SECTION_TITLE}>From the {tpl.name} template</h3>
                    <p className="mt-1 text-xs text-gray-600">
                        The download prints the template’s write-up here, word for word
                        {tpl.headings.length > 0 ? `: ${tpl.headings.join(' · ')}` : ''}.
                    </p>
                </section>
            )}
            {brief.pricing.length > 0 && (
                <section>
                    <h3 className={SECTION_TITLE}>Pricing &amp; investment</h3>
                    {content.pricing_intro && <p className="mt-1">{content.pricing_intro}</p>}
                    <div className="mt-2 overflow-x-auto">
                        <table className="w-full text-xs">
                            <thead>
                                <tr className="bg-red-50 text-left text-[10px] font-semibold uppercase tracking-wide text-brand">
                                    <th className="px-2 py-1.5">Service description</th>
                                    {hasStd && <th className="px-2 py-1.5 text-right">Standard rate</th>}
                                    <th className="px-2 py-1.5 text-right">Proposal price</th>
                                </tr>
                            </thead>
                            <tbody>
                                {brief.pricing.map(p => (
                                    <tr key={p.id} className="border-b border-gray-200">
                                        <td className="px-2 py-1.5">{p.description}</td>
                                        {hasStd && <td className="px-2 py-1.5 text-right">{p.standard_rate || '—'}</td>}
                                        <td className="px-2 py-1.5 text-right font-semibold">{p.proposal_price || '—'}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                    {(content.notes.length > 0 || brief.notes.length > 0) && (
                        <ul className="mt-2 list-disc space-y-0.5 pl-5 text-xs text-gray-700">
                            {(content.notes.length ? content.notes : brief.notes).map((n, i) => (
                                <li key={i}>{n}</li>
                            ))}
                        </ul>
                    )}
                </section>
            )}
            {brief.timeline && (
                <section>
                    <h3 className={SECTION_TITLE}>Timing</h3>
                    <p className="mt-1">{brief.timeline}</p>
                </section>
            )}
            {content.closing && <p className="italic text-gray-700">{content.closing}</p>}
        </article>
    );
};

// ─── the page ────────────────────────────────────────────────────────────────

export const Proposal: React.FC = () => {
    const { proposalId } = useParams<{ proposalId: string }>();
    const [proposal, setProposal] = useState<ProposalData | null>(null);
    const [draft, setDraft] = useState<Brief | null>(null);
    const [dirty, setDirty] = useState(false);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);
    const [generating, setGenerating] = useState(false);
    const [revising, setRevising] = useState(false);
    const [downloading, setDownloading] = useState<'pdf' | 'docx' | null>(null);
    const [instruction, setInstruction] = useState('');
    const [selectedNo, setSelectedNo] = useState<number | null>(null);
    const [templates, setTemplates] = useState<ProposalTemplate[] | null>(null);

    useEffect(() => {
        let cancelled = false;
        fetchProposalTemplates()
            .then(t => {
                if (!cancelled) setTemplates(t);
            })
            // Without the listing the form still works; it just offers no template.
            .catch(() => {
                if (!cancelled) setTemplates([]);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    const adopt = useCallback((p: ProposalData) => {
        setProposal(p);
        setDraft(deepCopy(p.brief));
        setDirty(false);
    }, []);

    useEffect(() => {
        if (!proposalId) return;
        let cancelled = false;
        fetchProposal(proposalId)
            .then(p => {
                if (cancelled) return;
                adopt(p);
                const last = latestVersion(p);
                setSelectedNo(last ? last.version_no : null);
            })
            .catch(e => {
                if (cancelled) return;
                const err = proposalsError(e);
                setLoadError(
                    isNotEnabled(err)
                        ? 'Proposal drafting is not switched on in this environment.'
                        : err.status === 403
                          ? 'This proposal belongs to another AE.'
                          : err.status === 404
                            ? 'This proposal no longer exists.'
                            : err.message,
                );
            });
        return () => {
            cancelled = true;
        };
    }, [proposalId, adopt]);

    const versions = useMemo(() => proposal?.versions ?? [], [proposal]);
    const selected = useMemo(
        () => versions.find(v => v.version_no === selectedNo) ?? latestVersion(proposal),
        [versions, selectedNo, proposal],
    );
    const confirmed = Boolean(proposal?.brief_confirmed_at);
    const loading = !proposal && !loadError;
    const copy = briefCopy(proposalOrigin(proposal));

    const onBriefChange = (next: Brief) => {
        setDraft(next);
        setDirty(true);
    };

    const confirm = async () => {
        if (!proposal || !draft) return;
        setSaving(true);
        try {
            adopt(await saveBrief(proposal.id, draft));
            toast.success('Brief confirmed.');
        } catch (e) {
            toast.error(proposalsError(e, 'The brief could not be saved.').message);
        } finally {
            setSaving(false);
        }
    };

    const generate = async () => {
        if (!proposal) return;
        setGenerating(true);
        try {
            const out = await generateProposal(proposal.id);
            adopt(out.proposal);
            setSelectedNo(out.version.version_no);
            toast.success(`Version ${out.version.version_no} is ready.`);
        } catch (e) {
            toast.error(proposalsError(e, 'Cubie could not generate the proposal.').message);
        } finally {
            setGenerating(false);
        }
    };

    const revise = async () => {
        if (!proposal || !instruction.trim()) return;
        setRevising(true);
        try {
            const out = await reviseProposal(proposal.id, instruction.trim());
            adopt(out.proposal);
            setSelectedNo(out.version.version_no);
            setInstruction('');
            toast.success(out.version.change_summary || `Version ${out.version.version_no} is ready.`);
        } catch (e) {
            toast.error(proposalsError(e, 'Cubie could not apply that change.').message);
        } finally {
            setRevising(false);
        }
    };

    const download = async (format: 'pdf' | 'docx') => {
        if (!proposal || !selected) return;
        setDownloading(format);
        try {
            const { blob, filename } = await downloadProposal(proposal.id, selected.version_no, format);
            saveBlob(blob, filename);
        } catch (e) {
            toast.error(proposalsError(e, 'The download failed.').message);
        } finally {
            setDownloading(null);
        }
    };

    // Where "back" goes when there is no in-app history to pop (a refresh, a
    // pasted link): the wrap-up of the call the proposal was drafted from, or
    // the call picker once that call is gone.
    const back = proposal?.call_session_id
        ? { to: `/calls/${encodeURIComponent(proposal.call_session_id)}/wrapup`, label: 'Wrap-up' }
        : { to: '/calls/start', label: 'Wrap-up' };

    return (
        <PageContainer>
            <EosPageHeader
                icon={<FileSignature size={20} />}
                title={proposal?.title || 'Proposal'}
                subtitle={
                    proposal
                        ? [proposal.company_name, versions.length ? `${versions.length} version${versions.length === 1 ? '' : 's'}` : 'not generated yet']
                              .filter(Boolean)
                              .join(' · ')
                        : undefined
                }
                right={<BackButton fallback={back.to} label={back.label} />}
            />

            {loading && <LoadingState label="Opening the proposal…" />}
            {loadError && <ErrorState message={loadError} />}

            {proposal && draft && (
                <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                    {/* Step 1 — the brief */}
                    <section className={`${CARD} p-5`} data-tour="proposal-brief">
                        <header className="mb-4 flex items-start justify-between gap-3">
                            <div>
                                <h2 className="text-base font-semibold text-gray-900">1. Confirm the brief</h2>
                                <p className="mt-0.5 text-xs text-gray-500">{copy.intro}</p>
                            </div>
                            {confirmed && !dirty ? (
                                <span className="inline-flex items-center gap-1 whitespace-nowrap rounded-full bg-green-50 px-2 py-0.5 text-[11px] font-medium text-green-800">
                                    <CheckCircle2 size={12} />
                                    Confirmed {shortWhen(proposal.brief_confirmed_at)}
                                </span>
                            ) : (
                                <span className="whitespace-nowrap rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800">
                                    {dirty ? 'Unsaved changes' : 'Not confirmed yet'}
                                </span>
                            )}
                        </header>

                        <BriefForm
                            brief={draft}
                            onChange={onBriefChange}
                            disabled={saving || generating || revising}
                            copy={copy}
                            templates={templates}
                        />

                        {proposal.gaps.length > 0 && !dirty && (
                            <ul className="mt-4 space-y-1 text-xs text-amber-800">
                                {proposal.gaps.map((g, i) => (
                                    <li key={i} className="flex items-start gap-1.5">
                                        <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" />
                                        {g}
                                    </li>
                                ))}
                            </ul>
                        )}

                        <div className="mt-5 flex flex-wrap items-center gap-2">
                            <PrimaryButton onClick={() => void confirm()} busy={saving} disabled={generating || revising}>
                                <CheckCircle2 size={15} />
                                {confirmed ? 'Save & re-confirm' : 'Confirm brief'}
                            </PrimaryButton>
                            {dirty && (
                                <SecondaryButton onClick={() => adopt(proposal)} disabled={saving}>
                                    Discard changes
                                </SecondaryButton>
                            )}
                        </div>
                    </section>

                    {/* Steps 2 & 3 — the document */}
                    <section className={`${CARD} p-5`} data-tour="proposal-document">
                        <header className="mb-4">
                            <h2 className="text-base font-semibold text-gray-900">2. The document</h2>
                            <p className="mt-0.5 text-xs text-gray-500">
                                Generated from the confirmed brief. Cubie cannot add a service or a number that is not on
                                it. You send it; Cubie never does.
                            </p>
                        </header>

                        {versions.length === 0 ? (
                            <div className="rounded-md border border-dashed border-gray-300 p-6 text-center">
                                <FileText size={22} className="mx-auto text-gray-300" />
                                <p className="mt-2 text-sm text-gray-700">Nothing generated yet.</p>
                                <p className="mt-1 text-xs text-gray-500">
                                    {confirmed && !dirty
                                        ? 'The brief is confirmed. Generating takes about half a minute.'
                                        : 'Confirm the brief on the left first.'}
                                </p>
                                <div className="mt-4">
                                    <PrimaryButton onClick={() => void generate()} busy={generating} disabled={!confirmed || dirty || saving}>
                                        <Sparkles size={15} />
                                        {generating ? 'Writing…' : 'Generate proposal'}
                                    </PrimaryButton>
                                </div>
                            </div>
                        ) : (
                            <>
                                <div className="mb-3 flex flex-wrap items-center gap-2">
                                    <div className="flex flex-wrap gap-1" role="tablist" aria-label="Versions">
                                        {versions.map(v => (
                                            <button
                                                key={v.id}
                                                type="button"
                                                role="tab"
                                                aria-selected={selected?.version_no === v.version_no}
                                                title={v.instruction ? `“${v.instruction}”` : 'Generated from the brief'}
                                                onClick={() => setSelectedNo(v.version_no)}
                                                className={`rounded-md px-2.5 py-1 text-xs font-medium focus:outline-none focus:ring-2 focus:ring-brand ${
                                                    selected?.version_no === v.version_no
                                                        ? 'bg-brand text-white'
                                                        : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                                                }`}
                                            >
                                                v{v.version_no}
                                            </button>
                                        ))}
                                    </div>
                                    <div className="ml-auto flex flex-wrap gap-2">
                                        <SecondaryButton onClick={() => void download('pdf')} busy={downloading === 'pdf'} disabled={!selected}>
                                            <Download size={14} />
                                            PDF
                                        </SecondaryButton>
                                        <SecondaryButton onClick={() => void download('docx')} busy={downloading === 'docx'} disabled={!selected}>
                                            <Download size={14} />
                                            Word
                                        </SecondaryButton>
                                    </div>
                                </div>

                                {selected && (
                                    <>
                                        {(selected.instruction || selected.change_summary) && (
                                            <p className="mb-3 rounded-md bg-gray-50 px-3 py-2 text-xs text-gray-600">
                                                {selected.instruction && (
                                                    <>
                                                        <span className="font-medium text-gray-800">You asked:</span> “{selected.instruction}”{' '}
                                                    </>
                                                )}
                                                {selected.change_summary && (
                                                    <>
                                                        <span className="font-medium text-gray-800">Cubie:</span> {selected.change_summary}
                                                    </>
                                                )}
                                            </p>
                                        )}
                                        <div className="rounded-md border border-gray-200 p-5">
                                            <DocumentPreview version={selected} templates={templates} />
                                        </div>
                                        <p className="mt-1.5 text-right text-[11px] text-gray-400">
                                            v{selected.version_no} · {shortWhen(selected.created_at)}
                                        </p>
                                    </>
                                )}

                                <div className="mt-5 border-t border-gray-200 pt-4">
                                    <h3 className="text-sm font-semibold text-gray-900">3. Revise</h3>
                                    <p className="mt-0.5 text-xs text-gray-500">
                                        Say what to change. Each change is a new version; the earlier ones stay.
                                    </p>
                                    <textarea
                                        className={`${TEXTAREA} mt-2`}
                                        value={instruction}
                                        placeholder="e.g. Tighten the summary to one paragraph. Change the price to $4,800. Drop scope item 3."
                                        onChange={e => setInstruction(e.target.value)}
                                        onKeyDown={e => {
                                            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') void revise();
                                        }}
                                        disabled={revising || generating}
                                    />
                                    <div className="mt-2 flex flex-wrap items-center gap-2">
                                        <PrimaryButton onClick={() => void revise()} busy={revising} disabled={!instruction.trim() || generating}>
                                            <Sparkles size={15} />
                                            {revising ? 'Revising…' : 'Revise'}
                                        </PrimaryButton>
                                        <SecondaryButton
                                            onClick={() => void generate()}
                                            busy={generating}
                                            disabled={!confirmed || dirty || revising}
                                        >
                                            Generate again from the brief
                                        </SecondaryButton>
                                        <span className="text-[11px] text-gray-400">⌘/Ctrl+Enter to revise</span>
                                    </div>
                                </div>
                            </>
                        )}
                    </section>
                </div>
            )}
        </PageContainer>
    );
};

export default Proposal;
