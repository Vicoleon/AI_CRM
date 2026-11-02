// One section of a Meeting Prep brief that the person can change in place.
//
// The section keeps the look it always had (a card, or the inset box the
// objections sit in) and adds, on the LATEST version only:
//
//   * Edit — the body swaps, right where it was, for an editor in the
//     section's own shape: markdown for prose, one item per line for lists,
//     a small row editor for the stakeholder map. Save writes a new version.
//   * Rewrite — Cubie rewrites just this section, optionally to an
//     instruction ("shorter", "focus on pricing"), as a new version.
//
// A provenance badge says who last changed the section and in which version.
// Every control is `.no-print`; while editing, the printed page still gets
// the section's read view. The parent owns the network calls and the one
// write that may be in flight at a time (`locked`): two saves racing on the
// same version would otherwise each build on the old one, and the second
// would quietly drop the first.
//
// An open editor follows the version on screen (`runId`): a clean draft
// re-seeds when the section changes under it, a draft with edits keeps them,
// says the section changed, and asks once before Save replaces the newer
// text. The rules, and why, are in `pages/meetingPrep/draft.ts`.
import React, { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { AlertTriangle, Check, Loader2, Pencil, Plus, Sparkles, Trash2, X } from 'lucide-react';

import type { SectionValue } from '../../api/meetingPrep';
import { confirmDialog } from '../eos/ConfirmDialog';
import {
    emptyStakeholder,
    formatVersionWhen,
    provenanceLabel,
    sectionKind,
    type SectionKey,
    type SectionMetaEntry,
    type Stakeholder,
} from '../../pages/meetingPrep/sections';
import {
    draftValue,
    isDirty,
    openEditing,
    rebaseEditing,
    syncEditing,
    withId,
    type Draft,
    type Editing,
    type Row,
} from '../../pages/meetingPrep/draft';
import { SectionCard, type SectionIcon } from './SectionCard';
import { cn } from './cn';

// ─── Small pieces ─────────────────────────────────────────────────────────

const AutoGrowTextarea: React.FC<
    React.TextareaHTMLAttributes<HTMLTextAreaElement> & { value: string; focusAtEnd?: boolean }
> = ({ value, className, focusAtEnd = false, ...rest }) => {
    const ref = useRef<HTMLTextAreaElement>(null);
    // Read once: the editor opens with the caret after the existing text, so
    // typing adds to it rather than in front of it.
    const focusOnMount = useRef(focusAtEnd);
    useEffect(() => {
        const el = ref.current;
        if (!focusOnMount.current || !el) return;
        el.focus();
        el.setSelectionRange(el.value.length, el.value.length);
    }, []);
    useLayoutEffect(() => {
        const el = ref.current;
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = `${el.scrollHeight + 2}px`;
    }, [value]);
    return (
        <textarea
            ref={ref}
            value={value}
            className={cn(
                'w-full resize-none overflow-hidden rounded-xl border border-slate-300 bg-white px-3.5 py-3 text-sm leading-relaxed text-slate-800 outline-none transition-shadow focus:border-brand focus:ring-4 focus:ring-brand/10',
                className,
            )}
            {...rest}
        />
    );
};

const IconButton: React.FC<{
    label: string;
    onClick: () => void;
    disabled?: boolean;
    pressed?: boolean;
    dark?: boolean;
    children: React.ReactNode;
}> = ({ label, onClick, disabled, pressed, dark, children }) => (
    <button
        type="button"
        aria-label={label}
        title={label}
        aria-pressed={pressed}
        onClick={onClick}
        disabled={disabled}
        className={cn(
            'rounded-lg p-1.5 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 disabled:cursor-not-allowed disabled:opacity-40',
            dark
                ? 'text-slate-400 hover:bg-white/10 hover:text-white'
                : 'text-slate-400 hover:bg-brand/10 hover:text-brand',
            pressed && (dark ? 'bg-white/10 text-white' : 'bg-brand/10 text-brand'),
        )}
    >
        {children}
    </button>
);

const INPUT =
    'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-brand focus:ring-4 focus:ring-brand/10';

const StakeholderEditor: React.FC<{
    rows: Row[];
    replacesText: boolean;
    onChange: (rows: Row[]) => void;
}> = ({ rows, replacesText, onChange }) => {
    const update = (rowId: string, patch: Partial<Stakeholder>) =>
        onChange(rows.map(r => (r.rowId === rowId ? { ...r, ...patch } : r)));
    const remove = (rowId: string) => onChange(rows.filter(r => r.rowId !== rowId));
    const add = () => onChange([...rows, withId(emptyStakeholder())]);

    return (
        <div className="space-y-3">
            {replacesText && (
                <p className="text-xs text-amber-700">
                    This map was written as free text. Saving replaces it with the people below.
                </p>
            )}
            {rows.map((r, i) => (
                <div key={r.rowId} className="space-y-2 rounded-xl border border-slate-200 bg-slate-50/60 p-3">
                    <div className="flex items-start gap-2">
                        <div className="grid flex-1 grid-cols-1 gap-2 sm:grid-cols-2">
                            <input
                                aria-label={`Person ${i + 1}: name`}
                                placeholder="Name"
                                value={r.name}
                                autoFocus={i === 0}
                                onChange={e => update(r.rowId, { name: e.target.value })}
                                className={INPUT}
                            />
                            <input
                                aria-label={`Person ${i + 1}: role`}
                                placeholder="Role (title or function)"
                                value={r.role}
                                onChange={e => update(r.rowId, { role: e.target.value })}
                                className={INPUT}
                            />
                        </div>
                        <button
                            type="button"
                            aria-label={`Remove ${r.name.trim() || `person ${i + 1}`}`}
                            title="Remove this person"
                            onClick={() => remove(r.rowId)}
                            className="mt-1 rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-red-50 hover:text-red-600"
                        >
                            <Trash2 className="h-4 w-4" />
                        </button>
                    </div>
                    <AutoGrowTextarea
                        aria-label={`Person ${i + 1}: context`}
                        placeholder="Their agenda and influence in this meeting"
                        value={r.context}
                        rows={2}
                        onChange={e => update(r.rowId, { context: e.target.value })}
                        className="rounded-lg px-3 py-2"
                    />
                    {r.email && <p className="truncate text-[11px] font-medium text-slate-400">{r.email}</p>}
                </div>
            ))}
            <button
                type="button"
                onClick={add}
                className="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-slate-300 px-3 py-1.5 text-xs font-bold text-slate-500 transition-colors hover:border-brand/40 hover:text-brand"
            >
                <Plus className="h-3.5 w-3.5" />
                Add person
            </button>
        </div>
    );
};

// ─── The section ──────────────────────────────────────────────────────────

export interface EditableSectionProps {
    sectionKey: SectionKey;
    icon: SectionIcon;
    title: string;
    /** The section's stored value (prose string, list, or stakeholder JSON). */
    value: unknown;
    /** The version on screen — an open editor notices when it changes. */
    runId: string | null;
    version?: number | null;
    /** `section_meta[key]` for the version on screen. */
    meta?: SectionMetaEntry | null;
    /** Latest version and the person's own brief. */
    canEdit: boolean;
    /** Some write is in flight (this section's or another's). */
    locked: boolean;
    /** This section's save is in flight. */
    saving?: boolean;
    /** This section's rewrite is in flight. */
    rewriting?: boolean;
    /** Resolve true when the new version is in, false to keep the editor open. */
    onSave: (value: SectionValue) => Promise<boolean>;
    onRewrite: (instruction: string) => Promise<boolean>;
    /** Open with the editor showing (a section added from empty). */
    defaultEditing?: boolean;
    onEditingChange?: (editing: boolean) => void;
    /** The editor holds changes nobody has saved (false again on save,
     *  cancel or unmount). The page asks before swapping the version on
     *  screen under it — e.g. when a background build lands. */
    onDirtyChange?: (dirty: boolean) => void;
    variant?: 'card' | 'inset';
    className?: string;
    dark?: boolean;
    /** Shown under the body in both modes (e.g. recommended products). */
    footer?: React.ReactNode;
    /** The read view. */
    children: React.ReactNode;
}

export const EditableSection: React.FC<EditableSectionProps> = ({
    sectionKey,
    icon: Icon,
    title,
    value,
    runId,
    version = null,
    meta,
    canEdit,
    locked,
    saving = false,
    rewriting = false,
    onSave,
    onRewrite,
    defaultEditing = false,
    onEditingChange,
    onDirtyChange,
    variant = 'card',
    className,
    dark = false,
    footer,
    children,
}) => {
    const [editing, setEditing] = useState<Editing | null>(() =>
        defaultEditing ? openEditing(sectionKey, value, runId) : null,
    );
    const [rewriteOpen, setRewriteOpen] = useState(false);
    const [instruction, setInstruction] = useState('');
    const instructionId = useId();

    // The version under an open editor changed (409, Regenerate, Restore). A
    // clean draft follows it — adjusted while rendering, React's pattern for
    // state that tracks a prop, so there is no effect and no stale paint.
    const sync = editing ? syncEditing(sectionKey, editing, runId, value) : 'in-step';
    if (sync === 'reseed') setEditing(openEditing(sectionKey, value, runId));
    const conflict = sync === 'conflict';

    const showEditor = canEdit && editing !== null;
    const dirty = editing ? isDirty(sectionKey, editing.seed, editing.draft) : false;

    // Tell the page, which keeps a tally rather than state (it only reads it
    // when a background build lands). Reported clean again on unmount, so a
    // section that disappears never leaves a stale "unsaved" behind.
    const reportDirty = useRef(onDirtyChange);
    useEffect(() => {
        reportDirty.current = onDirtyChange;
    }, [onDirtyChange]);
    const unsaved = showEditor && dirty;
    useEffect(() => {
        reportDirty.current?.(unsaved);
    }, [unsaved]);
    useEffect(() => () => reportDirty.current?.(false), []);

    const startEdit = () => {
        setEditing(openEditing(sectionKey, value, runId));
        setRewriteOpen(false);
        onEditingChange?.(true);
    };

    const close = () => {
        setEditing(null);
        onEditingChange?.(false);
    };

    const cancel = async () => {
        if (saving) return;
        if (
            dirty &&
            !(await confirmDialog({
                title: 'Discard your changes?',
                message: `Your edits to ${title} have not been saved.`,
                confirmLabel: 'Discard',
                cancelLabel: 'Keep editing',
                destructive: true,
            }))
        ) {
            return;
        }
        close();
    };

    const save = async () => {
        if (!editing || !dirty || locked) return;
        if (conflict) {
            const ok = await confirmDialog({
                title: `Replace the newer ${title}?`,
                message: `${title} changed in ${version ? `v${version}` : 'a newer version'} since you started editing. Saving replaces it with your edit; that version stays in the history.`,
                confirmLabel: 'Save my edit',
                cancelLabel: 'Keep editing',
            });
            if (!ok) return;
            // Agreed once: the editor now builds on the version on screen.
            setEditing(e => (e ? rebaseEditing(e, value, runId) : e));
        }
        if (await onSave(draftValue(sectionKey, editing.draft))) close();
    };

    const submitRewrite = async () => {
        if (locked) return;
        if (await onRewrite(instruction.trim())) {
            setRewriteOpen(false);
            setInstruction('');
        }
    };

    const setDraft = (draft: Draft) => setEditing(e => (e ? { ...e, draft } : e));

    // ── Header: provenance + actions ──
    const badge = provenanceLabel(meta);
    const badgeTitle = !meta
        ? undefined
        : meta.origin === 'rewritten'
          ? `Cubie rewrote this section${meta.version ? ` in v${meta.version}` : ''}${
                meta.instruction ? ` — “${meta.instruction}”` : ' (no instruction)'
            }`
          : `You edited this section${meta.version ? ` in v${meta.version}` : ''}${
                meta.at ? ` · ${formatVersionWhen(meta.at)}` : ''
            }`;

    const headerRight =
        badge || (canEdit && !showEditor) ? (
            <>
                {badge && (
                    <span
                        title={badgeTitle}
                        className={cn(
                            'no-print inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-semibold',
                            dark ? 'bg-white/10 text-slate-200' : 'bg-brand/10 text-brand',
                        )}
                    >
                        {meta?.origin === 'rewritten' ? (
                            <Sparkles className="h-3 w-3" />
                        ) : (
                            <Pencil className="h-3 w-3" />
                        )}
                        {badge}
                    </span>
                )}
                {canEdit && !showEditor && (
                    <span data-tour={sectionKey === 'executive_summary' ? 'meeting-prep-section-edit' : undefined} className="no-print flex items-center gap-0.5">
                        <IconButton
                            label={`Edit ${title}`}
                            onClick={startEdit}
                            disabled={rewriting || saving}
                            dark={dark}
                        >
                            <Pencil className="h-4 w-4" />
                        </IconButton>
                        <IconButton
                            label={`Rewrite ${title} with Cubie`}
                            onClick={() => setRewriteOpen(o => !o)}
                            disabled={rewriting}
                            pressed={rewriteOpen}
                            dark={dark}
                        >
                            <Sparkles className="h-4 w-4" />
                        </IconButton>
                    </span>
                )}
            </>
        ) : null;

    // ── Rewrite panel ──
    const rewritePanel =
        canEdit && rewriteOpen && !rewriting && !showEditor ? (
            <div
                className={cn(
                    'no-print mb-4 rounded-xl border p-3',
                    dark ? 'border-white/15 bg-white/5' : 'border-brand/15 bg-brand/5',
                )}
            >
                <label
                    htmlFor={instructionId}
                    className={cn(
                        'flex items-center gap-1.5 text-xs font-bold',
                        dark ? 'text-slate-200' : 'text-brand',
                    )}
                >
                    <Sparkles className="h-3.5 w-3.5" />
                    Rewrite this section with Cubie
                </label>
                <div className="mt-2 flex flex-col gap-2 sm:flex-row">
                    <input
                        id={instructionId}
                        type="text"
                        autoFocus
                        maxLength={1000}
                        value={instruction}
                        placeholder="Optional — e.g. shorter, focus on pricing"
                        onChange={e => setInstruction(e.target.value)}
                        onKeyDown={e => {
                            if (e.key === 'Enter') {
                                e.preventDefault();
                                void submitRewrite();
                            } else if (e.key === 'Escape') {
                                e.preventDefault();
                                setRewriteOpen(false);
                            }
                        }}
                        className={cn(INPUT, 'flex-1')}
                    />
                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={() => void submitRewrite()}
                            disabled={locked}
                            title={locked ? 'Wait for the current change to finish' : undefined}
                            className="inline-flex items-center gap-1.5 rounded-lg bg-brand px-3.5 py-2 text-sm font-bold text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-50"
                        >
                            <Sparkles className="h-4 w-4" />
                            Rewrite
                        </button>
                        <button
                            type="button"
                            aria-label="Close"
                            onClick={() => setRewriteOpen(false)}
                            className={cn(
                                'rounded-lg p-2 transition-colors',
                                dark ? 'text-slate-400 hover:bg-white/10' : 'text-slate-400 hover:bg-slate-100',
                            )}
                        >
                            <X className="h-4 w-4" />
                        </button>
                    </div>
                </div>
                <p className={cn('mt-1.5 text-[11px]', dark ? 'text-slate-400' : 'text-slate-500')}>
                    Cubie rewrites only this section, from the same sources, and saves it as a new version. Earlier
                    versions stay in the history.
                </p>
            </div>
        ) : null;

    // ── Body ──
    let body: React.ReactNode;
    if (showEditor && editing) {
        const kind = sectionKind(sectionKey);
        const draft = editing.draft;
        body = (
            <>
                {/* Printing mid-edit still prints the section, not an empty box. */}
                <div className="hidden print:block">{children}</div>
                <div
                    className="no-print"
                    onKeyDown={e => {
                        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                            e.preventDefault();
                            void save();
                        } else if (e.key === 'Escape') {
                            e.preventDefault();
                            e.stopPropagation();
                            void cancel();
                        }
                    }}
                >
                    {conflict && (
                        <p
                            role="status"
                            className="mb-3 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs font-medium text-amber-900"
                        >
                            <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-amber-600" />
                            This section changed in {version ? `v${version}` : 'a newer version'} since you started
                            editing — saving will replace it.
                        </p>
                    )}
                    {draft.kind === 'people' ? (
                        <StakeholderEditor
                            rows={draft.rows}
                            replacesText={draft.replacesText}
                            onChange={rows => setDraft({ ...draft, rows })}
                        />
                    ) : (
                        <>
                            <AutoGrowTextarea
                                aria-label={`Edit ${title}`}
                                focusAtEnd
                                rows={kind === 'list' ? 5 : 4}
                                value={draft.text}
                                onChange={e => setDraft({ kind: 'text', text: e.target.value })}
                            />
                            <p className="mt-1 text-[11px] text-slate-400">
                                {kind === 'list'
                                    ? 'One item per line. Blank lines are dropped; markdown works inside an item.'
                                    : 'Markdown works here: **bold**, lists with "- ", links.'}
                            </p>
                        </>
                    )}
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                        <button
                            type="button"
                            onClick={() => void save()}
                            disabled={!dirty || locked}
                            title={!dirty ? 'Nothing has changed yet' : locked && !saving ? 'Wait for the current change to finish' : undefined}
                            className="inline-flex items-center gap-1.5 rounded-lg bg-brand px-3.5 py-2 text-sm font-bold text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-50"
                        >
                            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                            {saving ? 'Saving…' : 'Save'}
                        </button>
                        <button
                            type="button"
                            onClick={() => void cancel()}
                            disabled={saving}
                            className={cn(
                                'rounded-lg border px-3.5 py-2 text-sm font-bold transition-colors disabled:opacity-50',
                                dark
                                    ? 'border-white/20 text-slate-200 hover:bg-white/10'
                                    : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50',
                            )}
                        >
                            Cancel
                        </button>
                        <span className="text-[11px] text-slate-400">
                            ⌘/Ctrl+Enter to save · Esc to cancel
                        </span>
                    </div>
                </div>
            </>
        );
    } else {
        body = (
            <div className={cn('relative', rewriting && 'min-h-[5rem]')} aria-busy={rewriting || undefined}>
                {children}
                {rewriting && (
                    <div
                        className={cn(
                            'no-print absolute inset-0 flex items-center justify-center rounded-xl backdrop-blur-[1px]',
                            dark ? 'bg-slate-900/70' : 'bg-white/75',
                        )}
                    >
                        <span
                            className={cn(
                                'flex items-center gap-2 rounded-full px-3.5 py-1.5 text-xs font-bold shadow-sm',
                                dark ? 'bg-white/10 text-white' : 'border border-brand/15 bg-white text-brand',
                            )}
                        >
                            <Loader2 className="h-4 w-4 animate-spin" />
                            Cubie is rewriting this section…
                        </span>
                    </div>
                )}
            </div>
        );
    }

    if (variant === 'inset') {
        return (
            <div className={cn('mt-6 rounded-xl border border-brand/10 bg-white/80 p-4', className)}>
                <div className="mb-3 flex items-center gap-2">
                    <h4 className="flex min-w-0 items-center gap-2 text-sm font-bold text-brand">
                        <Icon className="h-4 w-4 shrink-0" />
                        {title}
                    </h4>
                    {headerRight && <div className="ml-auto flex shrink-0 items-center gap-1.5">{headerRight}</div>}
                </div>
                {rewritePanel}
                {body}
                {footer}
            </div>
        );
    }

    return (
        <SectionCard icon={Icon} title={title} className={className} dark={dark} headerRight={headerRight}>
            {rewritePanel}
            {body}
            {footer}
        </SectionCard>
    );
};
