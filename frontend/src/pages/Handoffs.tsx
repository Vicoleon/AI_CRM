import React, { useCallback, useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
    AlertTriangle,
    Archive,
    Check,
    ChevronDown,
    ChevronRight,
    Clock,
    ExternalLink,
    History,
    RotateCcw,
    UserMinus,
    X,
} from 'lucide-react';
import client from '../api/client';

/**
 * Handoffs — open work inherited from departed teammates.
 *
 * One record per offboarded person the signed-in user succeeded (admins and
 * org-wide execs see every record). Each record carries the AI handoff
 * dossier from the exit sweep plus the item checklist; open items also keep
 * surfacing in the successor's daily AI Priorities until closed here or from
 * the priority card itself.
 */

interface HandoffItem {
    id: string;
    kind: string;
    label: string;
    detail: string;
    due: string | null;
    urgency: string;
    source_links: string[];
    status: string;
    resolved_by: string | null;
}

interface HandoffRecord {
    id: string;
    user_email: string;
    user_name: string;
    department: string | null;
    successor_email: string;
    offboarded_at: string;
    sweep_status: string;
    status: string;
    open_items: number;
    total_items: number;
    dossier_md?: string | null;
    items?: HandoffItem[];
}

interface DepartedCard {
    id: string;
    date: string;
    category: string;
    content_md: string;
}

const KIND_LABELS: Record<string, string> = {
    l10: 'L10 meetings',
    todo: 'EOS to-dos',
    rock: 'Rocks',
    milestone: 'Milestones',
    scorecard: 'Scorecard measurables',
    deal: 'HubSpot deals',
    commitment: 'Commitments',
    google_commitment: 'Commitments',
    google_signal_commitment: 'Commitments',
};

const SWEEP_BADGES: Record<string, { text: string; cls: string }> = {
    pending: { text: 'sweep running…', cls: 'bg-blue-50 text-blue-700 border-blue-200' },
    ok: { text: 'full sweep', cls: 'bg-green-50 text-green-700 border-green-200' },
    degraded: { text: 'partial sweep — Google account was already suspended', cls: 'bg-amber-50 text-amber-800 border-amber-200' },
    failed: { text: 'sweep failed', cls: 'bg-red-50 text-red-700 border-red-200' },
    skipped: { text: 'no sweep run', cls: 'bg-gray-50 text-gray-600 border-gray-200' },
};

export default function Handoffs() {
    const [records, setRecords] = useState<HandoffRecord[]>([]);
    const [loaded, setLoaded] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        client
            .get('/api/offboarding/records')
            .then(res => { if (!cancelled) setRecords(res.data?.records ?? []); })
            .catch(() => { if (!cancelled) setError('Could not load handoffs.'); })
            .finally(() => { if (!cancelled) setLoaded(true); });
        return () => { cancelled = true; };
    }, []);

    return (
        <div className="max-w-4xl mx-auto px-4 py-8">
            <div data-tour="handoffs-header" className="flex items-center gap-3 mb-1">
                <UserMinus className="text-brand" size={26} />
                <h1 className="text-2xl font-bold text-gray-900">Handoffs</h1>
            </div>
            <p className="text-gray-500 mb-6">
                Open work inherited from departed teammates. Items stay in your daily
                AI Priorities until you mark them done or dismissed here or on the card.
            </p>

            {!loaded && <div className="text-gray-400 py-12 text-center">Loading…</div>}
            {loaded && error && <div className="text-red-600 py-6">{error}</div>}
            {loaded && !error && records.length === 0 && (
                <div className="text-center py-16 border border-dashed border-gray-200 rounded-xl text-gray-400">
                    Nothing here — nobody has handed work off to you.
                </div>
            )}

            <div data-tour="handoffs-list" className="space-y-4">
                {records.map(r => (
                    <RecordCard key={r.id} record={r} />
                ))}
            </div>
        </div>
    );
}

const RecordCard: React.FC<{ record: HandoffRecord }> = ({ record }) => {
    const [expanded, setExpanded] = useState(false);
    const [detail, setDetail] = useState<HandoffRecord | null>(null);
    const [detailError, setDetailError] = useState<string | null>(null);
    const [history, setHistory] = useState<DepartedCard[] | null>(null);
    const [historyOpen, setHistoryOpen] = useState(false);

    const loadDetail = useCallback(() => {
        client
            .get(`/api/offboarding/records/${record.id}`)
            .then(res => setDetail(res.data?.record ?? null))
            .catch(() => setDetailError('Could not load this handoff.'));
    }, [record.id]);

    const toggle = () => {
        const next = !expanded;
        setExpanded(next);
        if (next && detail === null) loadDetail();
    };

    const loadHistory = () => {
        const next = !historyOpen;
        setHistoryOpen(next);
        if (next && history === null) {
            client
                .get(`/api/offboarding/departed/${encodeURIComponent(record.user_email)}/priorities`)
                .then(res => setHistory(res.data?.cards ?? []))
                .catch(() => setHistory([]));
        }
    };

    const setItemStatus = (item: HandoffItem, status: string) => {
        client
            .post(`/api/offboarding/items/${item.id}/status`, { status })
            .then(() => loadDetail())
            .catch(() => setDetailError('Could not update the item.'));
    };

    const badge = SWEEP_BADGES[record.sweep_status] ?? SWEEP_BADGES.pending;
    const shown = detail ?? record;

    return (
        <div className="border border-gray-200 rounded-xl bg-white shadow-sm">
            <button
                onClick={toggle}
                className="w-full flex items-center justify-between px-5 py-4 text-left"
            >
                <div>
                    <div className="flex items-center gap-2">
                        <span className="font-semibold text-gray-900">{record.user_name}</span>
                        <span className="text-sm text-gray-400">{record.department || ''}</span>
                        <span className={`text-xs px-2 py-0.5 rounded-full border ${badge.cls}`}>{badge.text}</span>
                    </div>
                    <div className="text-sm text-gray-500 mt-0.5">
                        Offboarded {record.offboarded_at.slice(0, 10)} · {record.open_items} of {record.total_items} item{record.total_items === 1 ? '' : 's'} still open
                    </div>
                </div>
                {expanded ? <ChevronDown size={18} className="text-gray-400" /> : <ChevronRight size={18} className="text-gray-400" />}
            </button>

            {expanded && (
                <div className="border-t border-gray-100 px-5 py-4 space-y-5">
                    {detailError && <div className="text-sm text-red-600">{detailError}</div>}
                    {!detail && !detailError && <div className="text-sm text-gray-400">Loading dossier…</div>}

                    {detail?.sweep_status === 'degraded' && (
                        <div className="flex items-start gap-2 text-sm rounded-lg border border-amber-200 bg-amber-50 p-3 text-amber-900">
                            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
                            <span>
                                Their Google account was already suspended when the sweep ran, so email and
                                calendar commitments may be missing — check the Workspace-transferred mailbox
                                and files too.
                            </span>
                        </div>
                    )}

                    {detail?.dossier_md && (
                        <div className="prose prose-sm max-w-none prose-headings:text-gray-900 prose-a:text-brand">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{detail.dossier_md}</ReactMarkdown>
                        </div>
                    )}

                    {detail?.items && detail.items.length > 0 && (
                        <ItemChecklist items={detail.items} onSetStatus={setItemStatus} />
                    )}

                    <div className="pt-1 border-t border-gray-100">
                        <button
                            onClick={loadHistory}
                            className="inline-flex items-center gap-2 text-sm text-brand hover:underline mt-3"
                        >
                            <History size={15} />
                            {historyOpen ? 'Hide' : 'View'} {shown.user_name.split(' ')[0]}&rsquo;s past priorities (read-only)
                        </button>
                        {historyOpen && (
                            <div className="mt-3 space-y-3">
                                {history === null && <div className="text-sm text-gray-400">Loading…</div>}
                                {history !== null && history.length === 0 && (
                                    <div className="text-sm text-gray-400">No stored priority cards.</div>
                                )}
                                {history?.map(c => (
                                    <details key={c.id} className="border border-gray-100 rounded-lg px-4 py-2 bg-gray-50/50">
                                        <summary className="cursor-pointer text-sm font-medium text-gray-700">
                                            {c.date} — {c.category === 'next_week_report' ? 'next-week look-ahead' : 'daily briefing'}
                                        </summary>
                                        <div className="prose prose-sm max-w-none mt-2">
                                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{c.content_md}</ReactMarkdown>
                                        </div>
                                    </details>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
};

const ItemChecklist: React.FC<{
    items: HandoffItem[];
    onSetStatus: (item: HandoffItem, status: string) => void;
}> = ({ items, onSetStatus }) => {
    const byKind = items.reduce<Record<string, HandoffItem[]>>((acc, i) => {
        (acc[i.kind] = acc[i.kind] ?? []).push(i);
        return acc;
    }, {});

    return (
        <div className="space-y-4">
            {Object.entries(byKind).map(([kind, group]) => (
                <div key={kind}>
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">
                        {KIND_LABELS[kind] ?? kind}
                    </h3>
                    <ul className="space-y-2">
                        {group.map(item => (
                            <li
                                key={item.id}
                                className={`flex items-start gap-3 rounded-lg border px-3 py-2 ${
                                    item.status === 'open'
                                        ? 'border-gray-200 bg-white'
                                        : 'border-gray-100 bg-gray-50 opacity-70'
                                }`}
                            >
                                <div className="mt-0.5">
                                    {item.status === 'open' && item.urgency === 'high' && (
                                        <span title="High urgency"><AlertTriangle size={15} className="text-red-500" /></span>
                                    )}
                                    {item.status === 'open' && item.urgency !== 'high' && (
                                        <Clock size={15} className="text-gray-400" />
                                    )}
                                    {item.status === 'done' && <Check size={15} className="text-green-600" />}
                                    {item.status === 'dismissed' && <Archive size={15} className="text-gray-400" />}
                                </div>
                                <div className="flex-1 min-w-0">
                                    <div className={`text-sm font-medium ${item.status === 'open' ? 'text-gray-900' : 'text-gray-500 line-through'}`}>
                                        {item.label}
                                        {item.due && <span className="ml-2 text-xs font-normal text-gray-400">due {item.due}</span>}
                                    </div>
                                    {item.detail && <div className="text-xs text-gray-500 mt-0.5">{item.detail}</div>}
                                    {item.source_links.length > 0 && (
                                        <div className="flex flex-wrap gap-2 mt-1">
                                            {item.source_links.map((l, i) => (
                                                <a
                                                    key={i}
                                                    href={l}
                                                    target="_blank"
                                                    rel="noreferrer"
                                                    className="inline-flex items-center gap-1 text-xs text-brand hover:underline"
                                                >
                                                    <ExternalLink size={11} /> source{item.source_links.length > 1 ? ` ${i + 1}` : ''}
                                                </a>
                                            ))}
                                        </div>
                                    )}
                                </div>
                                <div className="flex items-center gap-1 shrink-0">
                                    {item.status === 'open' ? (
                                        <>
                                            <button
                                                onClick={() => onSetStatus(item, 'done')}
                                                title="Mark done"
                                                className="p-1.5 rounded-md text-green-700 hover:bg-green-50"
                                            >
                                                <Check size={16} />
                                            </button>
                                            <button
                                                onClick={() => onSetStatus(item, 'dismissed')}
                                                title="Dismiss (not needed)"
                                                className="p-1.5 rounded-md text-gray-400 hover:bg-gray-100"
                                            >
                                                <X size={16} />
                                            </button>
                                        </>
                                    ) : (
                                        <button
                                            onClick={() => onSetStatus(item, 'open')}
                                            title="Reopen"
                                            className="p-1.5 rounded-md text-gray-400 hover:bg-gray-100"
                                        >
                                            <RotateCcw size={15} />
                                        </button>
                                    )}
                                </div>
                            </li>
                        ))}
                    </ul>
                </div>
            ))}
        </div>
    );
};
