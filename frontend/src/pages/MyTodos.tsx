import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    AlertTriangle,
    CalendarPlus,
    Check,
    ChevronDown,
    Clock,
    ExternalLink,
    FileText,
    ListChecks,
    Mail,
    MoreHorizontal,
    Plus,
    Sparkles,
    Users,
    X,
} from 'lucide-react';
import client from '../api/client';
import {
    acceptTodo,
    createTodo,
    dropTodo,
    fetchTodos,
    keptTodo,
    notYetTodo,
    parseTodo,
    patchTodo,
    promoteTodoToEos,
    rejectTodo,
    reopenTodo,
    snoozeTodo,
    type ParsedTodo,
    type Todo,
    type TodoGroup,
    type TodosPage,
} from '../api/todos';
import { EmptyState, EosPageHeader, LoadingState, PageContainer } from './eos/_shared';
import { formatISODate, todayISO } from './eos/_utils';
import { toast } from '../components/eos/Toast';

/**
 * My To-Dos — the personal promises ledger.
 *
 * A to-do here is a PROMISE: something you said you would do, for someone, by
 * when. The page is ordered by what needs a decision from you — suggestions
 * to accept (Phase 1), evidence to confirm (Phase 1), then what you owe
 * grouped by WHO is waiting — and every card's primary button does the first
 * part of the work. Design: docs/todos_promises_design.md.
 *
 * Dates are calendar days (YYYY-MM-DD in the owner's timezone) — rendered with
 * formatISODate, never parseInstant.
 */

/** The server's `detail` when there is one, else the error's message. */
const errDetail = (e: unknown, fallback: string): string => {
    const r = e as { response?: { data?: { detail?: unknown }; status?: number }; message?: string } | null;
    const d = r?.response?.data?.detail;
    return typeof d === 'string' && d ? d : r?.message || fallback;
};
const errStatus = (e: unknown): number | undefined =>
    (e as { response?: { status?: number } } | null)?.response?.status;

type Verb = { label: string; icon: React.ReactNode; run: () => void | Promise<void> };

const dayDiff = (iso: string, today: string): number => {
    const [y, m, d] = iso.split('-').map(Number);
    const [ty, tm, td] = today.split('-').map(Number);
    return Math.round((new Date(y, m - 1, d).getTime() - new Date(ty, tm - 1, td).getTime()) / 86_400_000);
};

const dueLabel = (iso: string | null, today: string): { text: string; tone: 'late' | 'soon' | 'later' | 'none' } => {
    if (!iso) return { text: 'no date', tone: 'none' };
    const diff = dayDiff(iso, today);
    if (diff < 0) return { text: `${-diff === 1 ? '1 day' : `${-diff} days`} late · was ${formatISODate(iso)}`, tone: 'late' };
    if (diff === 0) return { text: 'today', tone: 'soon' };
    if (diff === 1) return { text: 'tomorrow', tone: 'soon' };
    if (diff <= 7) return { text: formatISODate(iso), tone: 'soon' };
    return { text: formatISODate(iso), tone: 'later' };
};

const DUE_TONE: Record<string, string> = {
    late: 'text-red-700 bg-red-50 border-red-200',
    soon: 'text-amber-800 bg-amber-50 border-amber-200',
    later: 'text-gray-600 bg-gray-50 border-gray-200',
    none: 'text-gray-400 bg-gray-50 border-gray-200',
};

/** The button that does the first 80% of the work, from what the promise is
 *  about. Mirrors the UC-39 verb table on the priority card. */
function primaryVerb(t: Todo, navigate: ReturnType<typeof useNavigate>, setBusy: (m: string | null) => void): Verb {
    const links = ((t.source_ref?.source_links as string[] | undefined) ?? []).filter(Boolean);
    const open = (url: string): void => {
        window.open(url, '_blank', 'noopener,noreferrer');
    };
    if (t.object_kind === 'event' && t.object_id) {
        return { label: 'Prep for it', icon: <FileText size={13} />, run: () => navigate(`/meeting-prep/${encodeURIComponent(t.object_id!)}`) };
    }
    if (t.object_kind === 'document' && t.object_id) {
        return { label: 'Open the doc', icon: <ExternalLink size={13} />, run: () => open(`https://drive.google.com/file/d/${encodeURIComponent(t.object_id!)}/view`) };
    }
    if (t.object_kind === 'thread' && t.object_id) {
        return { label: 'Reply in Gmail', icon: <Mail size={13} />, run: () => open(`https://mail.google.com/mail/u/0/#all/${encodeURIComponent(t.object_id!)}`) };
    }
    if (t.object_kind === 'transcript' && t.object_id) {
        return {
            label: 'Draft the follow-up',
            icon: <Mail size={13} />,
            run: async () => {
                try {
                    const res = await client.post('/api/email/drafts/from-transcript', { transcript_id: t.object_id });
                    const url = res.data?.gmail_draft_url;
                    if (url) open(url);
                } catch (e: unknown) {
                    setBusy(errDetail(e, 'Draft failed'));
                }
            },
        };
    }
    if (t.object_kind === 'deal' && links[0]) {
        return { label: 'Open in HubSpot', icon: <ExternalLink size={13} />, run: () => open(links[0]) };
    }
    if (t.counterparty_kind === 'person' && t.counterparty_id && t.counterparty_id.includes('@')) {
        const first = (t.counterparty_label || '').split(' ')[0];
        return {
            label: `Email ${first || 'them'}`,
            icon: <Mail size={13} />,
            run: () => open(`https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(t.counterparty_id!)}&su=${encodeURIComponent(t.title)}`),
        };
    }
    if (links[0]) {
        return { label: 'Open the source', icon: <ExternalLink size={13} />, run: () => open(links[0]) };
    }
    return {
        label: 'Block 30 min',
        icon: <CalendarPlus size={13} />,
        run: async () => {
            try {
                const res = await client.post('/api/calendar/focus-block', { title: `Focus: ${t.title}`, duration_minutes: 30 });
                const link = res.data?.html_link;
                if (link) open(link);
                else toast.success('Focus block added to your calendar.');
            } catch (e: unknown) {
                const detail = errDetail(e, 'Focus block failed');
                setBusy(detail === 'no_free_slot' ? 'No free 30-min slot in the next 24h.' : detail);
            }
        },
    };
}

// ─── Capture bar ─────────────────────────────────────────────────────────────

const CaptureBar: React.FC<{ onAdded: () => void }> = ({ onAdded }) => {
    const [text, setText] = useState('');
    const [parsing, setParsing] = useState(false);
    const [preview, setPreview] = useState<ParsedTodo | null>(null);
    const [saving, setSaving] = useState(false);

    const parse = async (e: React.FormEvent) => {
        e.preventDefault();
        const t = text.trim();
        if (!t) return;
        setParsing(true);
        try {
            setPreview(await parseTodo(t));
        } catch (err: unknown) {
            toast.error(errDetail(err, 'Could not read that — try again.'));
        } finally {
            setParsing(false);
        }
    };

    const save = async () => {
        if (!preview) return;
        setSaving(true);
        try {
            await createTodo({
                title: preview.title,
                detail: preview.detail,
                due_local: preview.due_local,
                counterparty_kind: preview.counterparty_kind,
                counterparty_id: preview.counterparty_id ?? null,
                counterparty_label: preview.counterparty_label,
                object_kind: preview.object_kind,
                object_label: preview.object_label,
                source: 'page',
            });
            toast.success('Added to your To-Dos.');
            setText('');
            setPreview(null);
            onAdded();
        } catch (err: unknown) {
            toast.error(errDetail(err, 'Could not save that to-do.'));
        } finally {
            setSaving(false);
        }
    };

    const inputCls = 'border border-gray-300 rounded-md px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand/30';

    return (
        <div data-tour="todos-capture" className="bg-white rounded-lg border border-gray-200 p-4 mb-6">
            <form onSubmit={parse} className="flex gap-2">
                <input
                    value={text}
                    onChange={e => setText(e.target.value)}
                    placeholder="Say it the way you'd say it out loud — “send Derek the revised SOW Thursday”"
                    className={`flex-1 ${inputCls}`}
                    disabled={parsing || !!preview}
                />
                <button
                    type="submit"
                    disabled={parsing || !text.trim() || !!preview}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-brand text-sm font-medium text-white hover:bg-brand-hover disabled:opacity-50"
                >
                    {parsing ? <Sparkles size={14} className="animate-pulse" /> : <Plus size={14} />}
                    {parsing ? 'Reading…' : 'Add'}
                </button>
            </form>
            {preview && (
                <div className="mt-3 pt-3 border-t border-gray-100">
                    <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-tight mb-2 inline-flex items-center gap-1">
                        <Sparkles size={11} /> Cubie read it as {preview.parser === 'fallback' && <span className="normal-case font-normal text-gray-400">(basic read — check the date)</span>}
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-[1fr_180px_160px] gap-2">
                        <input value={preview.title} onChange={e => setPreview({ ...preview, title: e.target.value })} className={inputCls} aria-label="What" />
                        <input
                            value={preview.counterparty_label ?? ''}
                            onChange={e => setPreview({ ...preview, counterparty_label: e.target.value || null, counterparty_kind: e.target.value ? (preview.counterparty_kind ?? 'person') : null, counterparty_id: null })}
                            placeholder="For whom (optional)"
                            className={inputCls}
                            aria-label="For whom"
                        />
                        <input type="date" value={preview.due_local ?? ''} onChange={e => setPreview({ ...preview, due_local: e.target.value || null })} className={inputCls} aria-label="By when" />
                    </div>
                    <div className="mt-2 flex items-center gap-2">
                        <button onClick={save} disabled={saving || !preview.title.trim()} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-brand text-sm font-medium text-white hover:bg-brand-hover disabled:opacity-50">
                            <Check size={14} /> {saving ? 'Saving…' : 'Looks right, add it'}
                        </button>
                        <button onClick={() => setPreview(null)} className="inline-flex items-center gap-1 px-2 py-1.5 rounded-md text-sm text-gray-600 hover:bg-gray-100">
                            <X size={14} /> Cancel
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
};

// ─── Card ────────────────────────────────────────────────────────────────────

const SmallBtn: React.FC<{ onClick: () => void; children: React.ReactNode; tone?: 'default' | 'good' | 'danger'; disabled?: boolean; title?: string }> = ({ onClick, children, tone = 'default', disabled, title }) => {
    const cls = tone === 'good'
        ? 'text-green-700 hover:bg-green-50'
        : tone === 'danger'
            ? 'text-red-700 hover:bg-red-50'
            : 'text-gray-700 hover:bg-gray-100';
    return (
        <button onClick={onClick} disabled={disabled} title={title} className={`inline-flex items-center gap-1 px-2 py-1 rounded text-xs disabled:opacity-50 ${cls}`}>
            {children}
        </button>
    );
};

const TodoCard: React.FC<{ todo: Todo; today: string; onChanged: () => void; showWho?: boolean }> = ({ todo: t, today, onChanged, showWho }) => {
    const navigate = useNavigate();
    const [busy, setBusy] = useState<string | null>(null);
    const [menu, setMenu] = useState<'snooze' | 'more' | 'drop' | 'redate' | null>(null);
    const [dateInput, setDateInput] = useState(t.due_local ?? todayISO());
    const due = dueLabel(t.due_local, today);
    const verb = primaryVerb(t, navigate, setBusy);

    const run = async (fn: () => Promise<unknown>, ok?: string) => {
        setBusy(null);
        try {
            await fn();
            if (ok) toast.success(ok);
            setMenu(null);
            onChanged();
        } catch (e: unknown) {
            setBusy(errDetail(e, 'That did not work.'));
        }
    };

    const isSuggested = t.status === 'suggested';
    const isEvidence = t.status === 'evidence_found';
    const isClosed = t.status === 'kept' || t.status === 'dropped';

    return (
        <div className={`bg-white rounded-lg border p-3 ${t.status === 'slipping' ? 'border-amber-300' : 'border-gray-200'}`}>
            <div className="flex items-start gap-3">
                <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className={`font-medium text-sm ${isClosed ? 'line-through text-gray-400' : 'text-gray-900'}`}>{t.title}</span>
                        {t.status === 'slipping' && (
                            <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-tight text-amber-800 bg-amber-50 border border-amber-200 rounded px-1.5 py-0.5">
                                <AlertTriangle size={10} /> slipping
                            </span>
                        )}
                        {t.eos_todo_id && <span className="text-[10px] font-semibold uppercase tracking-tight text-gray-500 bg-gray-50 border border-gray-200 rounded px-1.5 py-0.5">also a team To-Do</span>}
                        {t.is_private && <span className="text-[10px] font-semibold uppercase tracking-tight text-gray-500 bg-gray-50 border border-gray-200 rounded px-1.5 py-0.5">private</span>}
                    </div>
                    <div className="mt-1 flex items-center gap-2 flex-wrap text-xs text-gray-500">
                        {showWho && t.counterparty_label && <span className="inline-flex items-center gap-1"><Users size={11} /> for {t.counterparty_label}</span>}
                        <span className={`inline-flex items-center gap-1 border rounded px-1.5 py-0.5 ${DUE_TONE[due.tone]}`}><Clock size={10} /> {due.text}</span>
                        {t.object_label && <span className="truncate">about {t.object_label}</span>}
                        {t.snooze_count > 0 && <span className="text-gray-400">moved {t.snooze_count}×</span>}
                    </div>
                    {t.detail && <div className="mt-1 text-xs text-gray-600 line-clamp-2">{t.detail}</div>}
                    {typeof t.source_ref?.quote === 'string' && (
                        <div className="mt-1 text-xs text-gray-500 italic">“{t.source_ref.quote as string}”</div>
                    )}
                    {busy && <div className="mt-1 text-xs text-red-700">{busy}</div>}
                </div>

                {!isClosed && !isSuggested && !isEvidence && (
                    <button
                        onClick={() => void verb.run()}
                        className="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium border border-brand/30 text-brand hover:bg-brand/5"
                    >
                        {verb.icon} {verb.label}
                    </button>
                )}
            </div>

            <div className="mt-2 flex items-center gap-1 flex-wrap" onClick={e => e.stopPropagation()}>
                {isSuggested && (
                    <>
                        <SmallBtn tone="good" onClick={() => run(() => acceptTodo(t.id), 'Added to your list.')}><Check size={12} /> Yes, add it</SmallBtn>
                        <SmallBtn onClick={() => run(() => rejectTodo(t.id))}><X size={12} /> Not mine</SmallBtn>
                    </>
                )}
                {isEvidence && (
                    <>
                        <SmallBtn tone="good" onClick={() => run(() => keptTodo(t.id), 'Kept.')}><Check size={12} /> Kept</SmallBtn>
                        <SmallBtn onClick={() => run(() => notYetTodo(t.id))}><X size={12} /> Not yet</SmallBtn>
                    </>
                )}
                {isClosed && (
                    <SmallBtn onClick={() => run(() => reopenTodo(t.id), 'Back on your list.')}>Reopen</SmallBtn>
                )}
                {!isClosed && !isSuggested && !isEvidence && (
                    <>
                        <SmallBtn tone="good" onClick={() => run(() => keptTodo(t.id), 'Kept.')}><Check size={12} /> Kept</SmallBtn>
                        <SmallBtn onClick={() => setMenu(menu === 'snooze' ? null : 'snooze')}><Clock size={12} /> Snooze <ChevronDown size={10} /></SmallBtn>
                        <SmallBtn onClick={() => setMenu(menu === 'more' ? null : 'more')}><MoreHorizontal size={12} /></SmallBtn>
                    </>
                )}
            </div>

            {menu === 'snooze' && (
                <div className="mt-2 flex items-center gap-1 flex-wrap text-xs">
                    <SmallBtn onClick={() => run(() => snoozeTodo(t.id, { preset: 'tomorrow' }), 'Moved to tomorrow.')}>Tomorrow</SmallBtn>
                    <SmallBtn onClick={() => run(() => snoozeTodo(t.id, { preset: '3d' }), 'Moved 3 days.')}>In 3 days</SmallBtn>
                    <SmallBtn onClick={() => run(() => snoozeTodo(t.id, { preset: 'week' }), 'Moved a week.')}>Next week</SmallBtn>
                    <span className="text-gray-300">|</span>
                    <input type="date" value={dateInput} min={today} onChange={e => setDateInput(e.target.value)} className="border border-gray-300 rounded px-1.5 py-0.5 text-xs" aria-label="Pick a date" />
                    <SmallBtn onClick={() => dateInput && run(() => snoozeTodo(t.id, { until: dateInput }), `Moved to ${formatISODate(dateInput)}.`)}>Move</SmallBtn>
                </div>
            )}
            {menu === 'more' && (
                <div className="mt-2 flex items-center gap-1 flex-wrap text-xs">
                    <SmallBtn onClick={() => setMenu('redate')}>Change the date</SmallBtn>
                    <SmallBtn onClick={() => run(() => patchTodo(t.id, { is_private: !t.is_private }))}>{t.is_private ? 'Make shareable' : 'Make private'}</SmallBtn>
                    {!t.eos_todo_id && (
                        <SmallBtn onClick={() => run(() => promoteTodoToEos(t.id), 'Also on your team’s L10 list now.')}>Make it a team To-Do</SmallBtn>
                    )}
                    <SmallBtn tone="danger" onClick={() => setMenu('drop')}>Drop</SmallBtn>
                </div>
            )}
            {menu === 'redate' && (
                <div className="mt-2 flex items-center gap-1 flex-wrap text-xs">
                    <input type="date" value={dateInput} onChange={e => setDateInput(e.target.value)} className="border border-gray-300 rounded px-1.5 py-0.5 text-xs" aria-label="New date" />
                    <SmallBtn onClick={() => run(() => patchTodo(t.id, { due_local: dateInput || null, clear_due: !dateInput }))}>Save</SmallBtn>
                    {t.due_local && <SmallBtn onClick={() => run(() => patchTodo(t.id, { clear_due: true }))}>No date</SmallBtn>}
                    <SmallBtn onClick={() => setMenu(null)}>Cancel</SmallBtn>
                </div>
            )}
            {menu === 'drop' && (
                <div className="mt-2 flex items-center gap-2 flex-wrap text-xs">
                    <span className="text-gray-600">
                        {t.counterparty_label ? `Drop it? ${t.counterparty_label} is waiting on this.` : 'Drop it? It leaves your list for good (you can reopen it today).'}
                    </span>
                    <SmallBtn tone="danger" onClick={() => run(() => dropTodo(t.id), 'Dropped.')}>Yes, drop</SmallBtn>
                    <SmallBtn onClick={() => setMenu(null)}>Keep it</SmallBtn>
                </div>
            )}
        </div>
    );
};

// ─── Sections ────────────────────────────────────────────────────────────────

const SectionHead: React.FC<{ icon: React.ReactNode; title: string; count?: number; hint?: string }> = ({ icon, title, count, hint }) => (
    <div className="flex items-baseline gap-2 mb-2 mt-6">
        <h2 className="text-sm font-semibold text-gray-900 inline-flex items-center gap-1.5">{icon} {title}</h2>
        {typeof count === 'number' && <span className="text-xs text-gray-400">({count})</span>}
        {hint && <span className="text-xs text-gray-400">· {hint}</span>}
    </div>
);

const GroupBlock: React.FC<{ group: TodoGroup; today: string; onChanged: () => void }> = ({ group, today, onChanged }) => (
    <div className="mb-4">
        <div className="text-xs font-semibold text-gray-500 mb-1.5 inline-flex items-center gap-1.5">
            {group.key === '__you__' ? null : <Users size={12} />} {group.label} <span className="text-gray-400 font-normal">({group.items.length})</span>
        </div>
        <div className="space-y-2">
            {group.items.map(t => <TodoCard key={t.id} todo={t} today={today} onChanged={onChanged} />)}
        </div>
    </div>
);

export const MyTodos: React.FC = () => {
    const [page, setPage] = useState<TodosPage | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loaded, setLoaded] = useState(false);

    const reload = useCallback(() => {
        fetchTodos()
            .then(setPage)
            .catch((e: unknown) => setError(errStatus(e) === 403 ? 'My To-Dos is not open to your team yet.' : 'Could not load your To-Dos.'));
    }, []);

    useEffect(() => {
        let cancelled = false;
        fetchTodos()
            .then(d => { if (!cancelled) setPage(d); })
            .catch((e: unknown) => { if (!cancelled) setError(errStatus(e) === 403 ? 'My To-Dos is not open to your team yet.' : 'Could not load your To-Dos.'); })
            .finally(() => { if (!cancelled) setLoaded(true); });
        return () => { cancelled = true; };
    }, []);

    const today = page?.today ?? todayISO();
    const openCount = useMemo(() => (page ? page.waiting.reduce((n, g) => n + g.items.length, 0) : 0), [page]);
    const subtitle = page
        ? page.people_waiting > 0
            ? `${page.people_waiting} ${page.people_waiting === 1 ? 'person is' : 'people are'} waiting on you · ${openCount} open`
            : openCount > 0
                ? `${openCount} open, nobody waiting but you`
                : 'Nothing open. Say what you promised and Cubie keeps track.'
        : 'What you said you would do, for whom, by when.';

    return (
        <PageContainer>
            <EosPageHeader icon={<ListChecks size={20} className="text-brand" />} title="My To-Dos" subtitle={subtitle} />

            <CaptureBar onAdded={reload} />

            {!loaded && <LoadingState />}
            {loaded && error && <div className="text-sm text-red-700">{error}</div>}

            {page && (
                <>
                    {page.suggested.length > 0 && (
                        <>
                            <SectionHead icon={<Sparkles size={14} />} title="Cubie heard you promise…" count={page.suggested.length} hint="accept to add, nothing counts until you do" />
                            <div data-tour="todos-heard" className="space-y-2">{page.suggested.map(t => <TodoCard key={t.id} todo={t} today={today} onChanged={reload} showWho />)}</div>
                        </>
                    )}
                    {page.confirm.length > 0 && (
                        <>
                            <SectionHead icon={<Check size={14} />} title="Looks done — confirm?" count={page.confirm.length} />
                            <div data-tour="todos-confirm" className="space-y-2">{page.confirm.map(t => <TodoCard key={t.id} todo={t} today={today} onChanged={reload} showWho />)}</div>
                        </>
                    )}

                    <SectionHead icon={<Users size={14} />} title="Waiting on you" count={openCount} />
                    {page.waiting.length === 0 ? (
                        <EmptyState title="Your list is clear." hint="Type a promise above, say “remind me to…” in Chat, use /todo in Slack, or add one from a Daily Briefing card." />
                    ) : (
                        page.waiting.map(g => <GroupBlock key={g.key} group={g} today={today} onChanged={reload} />)
                    )}

                    {page.read_through.eos_todos.length > 0 && (
                        <>
                            <SectionHead icon={<ListChecks size={14} />} title="Your team’s To-Dos (EOS)" count={page.read_through.eos_todos.length} hint="managed in EOS" />
                            <div className="bg-white rounded-lg border border-gray-200 divide-y divide-gray-100">
                                {page.read_through.eos_todos.map(r => (
                                    <a key={r.id} href={r.link} className="flex items-center gap-3 px-3 py-2 text-sm hover:bg-gray-50">
                                        <span className="flex-1 min-w-0 truncate text-gray-900">{r.title}</span>
                                        <span className="text-xs text-gray-500">{r.team}</span>
                                        <span className={`text-xs border rounded px-1.5 py-0.5 ${DUE_TONE[dueLabel(r.due_local, today).tone]}`}>{dueLabel(r.due_local, today).text}</span>
                                        <ExternalLink size={12} className="text-gray-400" />
                                    </a>
                                ))}
                            </div>
                        </>
                    )}
                    {page.read_through.commitments.length > 0 && (
                        <>
                            <SectionHead icon={<Users size={14} />} title="Customer commitments (Sales Cockpit)" count={page.read_through.commitments.length} hint="managed in My Day" />
                            <div className="bg-white rounded-lg border border-gray-200 divide-y divide-gray-100">
                                {page.read_through.commitments.map(r => (
                                    <a key={r.id} href={r.link} className="flex items-center gap-3 px-3 py-2 text-sm hover:bg-gray-50">
                                        <span className="flex-1 min-w-0 truncate text-gray-900">{r.title}</span>
                                        <span className="text-xs text-gray-500">{r.state.replace('_', ' ')}</span>
                                        {r.breach_at && <span className="text-xs text-gray-500">by {formatISODate(r.breach_at)}</span>}
                                        <ExternalLink size={12} className="text-gray-400" />
                                    </a>
                                ))}
                            </div>
                        </>
                    )}

                    {page.recent.length > 0 && (
                        <>
                            <SectionHead icon={<Check size={14} />} title="Closed today" count={page.recent.length} />
                            <div className="space-y-2 opacity-80">{page.recent.map(t => <TodoCard key={t.id} todo={t} today={today} onChanged={reload} showWho />)}</div>
                        </>
                    )}

                    <div data-tour="todos-ledger" className="mt-8 pt-4 border-t border-gray-200 text-xs text-gray-500 flex items-center gap-3 flex-wrap">
                        <span className="font-medium text-gray-700">This week</span>
                        <span>{page.ledger.kept} kept</span>
                        <span>·</span>
                        <span>{page.ledger.dropped} dropped</span>
                        <span>·</span>
                        <span>{page.ledger.open} open{page.ledger.slipping > 0 ? `, ${page.ledger.slipping} slipping` : ''}</span>
                        <span className="ml-auto text-gray-400">Dates in {page.timezone}</span>
                    </div>
                </>
            )}
        </PageContainer>
    );
};

export default MyTodos;
