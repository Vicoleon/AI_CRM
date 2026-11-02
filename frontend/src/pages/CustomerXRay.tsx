// UC-45 — Customer X-Ray full page. Modeled on MeetingPrep's
// structure/styling: SectionCards, print CSS, partial-errors ("Intelligence
// Gaps") card. Renders whatever `sections` contains, so Fast and Deep share one
// component. Deep runs show a per-agent progress skeleton driven by 5s polling
// of /runs/{id} (no SSE/WS: a long-lived stream holds a database connection).
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
    Activity,
    AlertCircle,
    ArrowDownRight,
    ArrowRight,
    ArrowUpRight,
    Building2,
    CalendarClock,
    ChevronDown,
    ExternalLink,
    FileDown,
    FolderPlus,
    Gauge,
    History as HistoryIcon,
    Lightbulb,
    Loader2,
    Newspaper,
    Package,
    Printer,
    Telescope,
    TrendingUp,
    Wallet,
    X,
    Zap,
} from 'lucide-react';
import { motion } from 'framer-motion';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { formatInstant, formatInstantDate, instantMs } from '../utils/datetime';
import {
    CHURN_BAND_COLOR,
    downloadXrayDocx,
    getHistory,
    getLatest,
    getRun,
    getTimeline,
    runDeep,
    runFast,
    saveXrayToAssets,
    type HistoryRow,
    type TimelineEvent,
    type XraySnapshot,
} from '../api/xray';
import { FeedbackButtons } from '../components/FeedbackButtons';

const SOURCE_FILTERS: { key: string; label: string }[] = [
    { key: '', label: 'All' },
    { key: 'hubspot_deal', label: 'Deals' },
    { key: 'hubspot_activity', label: 'Activity' },
    { key: 'gmail', label: 'Email' },
    { key: 'meeting', label: 'Meetings' },
    { key: 'news', label: 'News' },
    { key: 'status_change', label: 'Status' },
];

const SectionCard: React.FC<{
    icon: React.FC<{ className?: string }>;
    title: string;
    children: React.ReactNode;
    asOf?: string;
}> = ({ icon: Icon, title, children, asOf }) => (
    <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        className="SectionCard rounded-2xl border border-slate-200 bg-white p-6 shadow-sm"
    >
        <div className="mb-4 flex items-center gap-2">
            <div className="rounded-lg bg-brand/10 p-2">
                <Icon className="h-5 w-5 text-brand" />
            </div>
            <h3 className="text-lg font-bold text-slate-900">{title}</h3>
            {asOf && <span className="ml-auto text-xs text-slate-400">as of {asOf}</span>}
        </div>
        {children}
    </motion.div>
);

// A SectionCard whose body collapses behind a clickable header (default closed).
const CollapsibleSection: React.FC<{
    icon: React.FC<{ className?: string }>;
    title: string;
    subtitle?: string;
    children: React.ReactNode;
    defaultOpen?: boolean;
}> = ({ icon: Icon, title, subtitle, children, defaultOpen = false }) => {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            className="SectionCard rounded-2xl border border-slate-200 bg-white p-6 shadow-sm"
        >
            <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                aria-expanded={open}
                className="flex w-full items-center gap-2 text-left"
            >
                <div className="rounded-lg bg-brand/10 p-2">
                    <Icon className="h-5 w-5 text-brand" />
                </div>
                <h3 className="text-lg font-bold text-slate-900">{title}</h3>
                {subtitle && (
                    <span className="text-xs font-normal text-slate-400">{subtitle}</span>
                )}
                <ChevronDown
                    className={`ml-auto h-5 w-5 shrink-0 text-slate-400 transition-transform ${
                        open ? 'rotate-180' : ''
                    }`}
                />
            </button>
            {open && <div className="mt-4">{children}</div>}
        </motion.div>
    );
};

const Prose: React.FC<{ md?: string }> = ({ md }) =>
    md ? (
        <div className="prose prose-sm max-w-none text-slate-700">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown>
        </div>
    ) : (
        <p className="text-sm text-slate-400">Not generated.</p>
    );

const ChurnGauge: React.FC<{ score?: number; band?: string }> = ({ score, band }) => {
    const s = typeof score === 'number' ? score : 0;
    const color = CHURN_BAND_COLOR[band || ''] || '#6b7280';
    const r = 56;
    const circ = Math.PI * r; // semicircle arc length
    const pct = Math.max(0, Math.min(100, s)) / 100;
    return (
        <div className="flex flex-col items-center">
            <svg width="130" height="80" viewBox="0 0 130 80">
                <path
                    d="M9 74 A56 56 0 0 1 121 74"
                    fill="none"
                    stroke="#e5e7eb"
                    strokeWidth="12"
                    strokeLinecap="round"
                />
                <path
                    d="M9 74 A56 56 0 0 1 121 74"
                    fill="none"
                    stroke={color}
                    strokeWidth="12"
                    strokeLinecap="round"
                    strokeDasharray={`${circ}`}
                    strokeDashoffset={`${circ * (1 - pct)}`}
                />
                <text
                    x="65"
                    y="66"
                    textAnchor="middle"
                    className="fill-slate-900"
                    fontSize="26"
                    fontWeight="700"
                >
                    {s}
                </text>
            </svg>
            <span className="text-sm font-semibold" style={{ color }}>
                {band || 'Unknown'} · churn risk
            </span>
        </div>
    );
};

const SignalTile: React.FC<{
    icon: React.FC<{ className?: string }>;
    label: string;
    value: React.ReactNode;
}> = ({ icon: Icon, label, value }) => (
    <div className="flex flex-col rounded-xl border border-slate-200 bg-white p-3">
        <div className="mb-1 flex items-center gap-1 text-xs text-slate-400">
            <Icon className="h-3.5 w-3.5" />
            {label}
        </div>
        <div className="text-lg font-bold text-slate-900">{value}</div>
    </div>
);

// The snapshot embeds only the newest 100 events, so a long-standing customer's
// older moments (a 2024 contract amendment, say) fall off the end — and the
// source chips used to filter *within* that same 100, which meant "Deals" could
// hide deals that are on file. Filtering and paging both go to the server now:
// picking a chip re-queries that source, and "Load older" walks back with a
// `before` cursor until the history is exhausted.
//
// `asOf` is set when an earlier version is open: its `events` were already
// fetched below that version's run, and the chips start below it too, so the
// older brief never shows a moment from after it was written.
const TIMELINE_PAGE = 100;

const Timeline: React.FC<{
    companyId: string;
    events: TimelineEvent[];
    asOf?: string;
}> = ({ companyId, events, asOf }) => {
    const [filter, setFilter] = useState('');
    const [rows, setRows] = useState<TimelineEvent[]>(events);
    const [busy, setBusy] = useState(false);
    const [exhausted, setExhausted] = useState(events.length < TIMELINE_PAGE);
    const [error, setError] = useState<string | null>(null);

    const selectSource = async (key: string) => {
        setFilter(key);
        setError(null);
        if (!key) {
            setRows(events);
            setExhausted(events.length < TIMELINE_PAGE);
            return;
        }
        setBusy(true);
        try {
            const page = await getTimeline(companyId, {
                source: key,
                limit: TIMELINE_PAGE,
                before: asOf,
            });
            setRows(page);
            setExhausted(page.length < TIMELINE_PAGE);
        } catch {
            setError('Could not load this filter — showing what was already loaded.');
        } finally {
            setBusy(false);
        }
    };

    const loadOlder = async () => {
        // Page from the oldest DATED row: undated events sort last and carry no
        // cursor, so they would otherwise pin `before` and re-fetch forever.
        // An earlier version with an empty first page still pages from its cutoff.
        const oldest = [...rows].reverse().find((e) => e.ts)?.ts ?? asOf;
        if (!oldest) {
            setExhausted(true);
            return;
        }
        setBusy(true);
        setError(null);
        try {
            const page = await getTimeline(companyId, {
                source: filter || undefined,
                limit: TIMELINE_PAGE,
                before: oldest,
            });
            const seen = new Set(rows.map((e) => e.id));
            const fresh = page.filter((e) => !seen.has(e.id));
            setRows((prev) => [...prev, ...fresh]);
            setExhausted(page.length < TIMELINE_PAGE || fresh.length === 0);
        } catch {
            setError('Could not load older events. Try again.');
        } finally {
            setBusy(false);
        }
    };

    return (
        <div>
            <div className="no-print mb-4 flex flex-wrap items-center gap-1.5">
                {SOURCE_FILTERS.map((f) => (
                    <button
                        key={f.key}
                        onClick={() => selectSource(f.key)}
                        disabled={busy}
                        className={`rounded-full px-2.5 py-1 text-xs font-medium disabled:opacity-50 ${
                            filter === f.key
                                ? 'bg-brand text-white'
                                : 'bg-slate-100 text-slate-500 hover:bg-slate-200'
                        }`}
                    >
                        {f.label}
                    </button>
                ))}
                {busy && <Loader2 size={14} className="animate-spin text-slate-400" />}
            </div>
            {error && <p className="mb-3 text-xs text-amber-600">{error}</p>}
            {rows.length === 0 ? (
                <p className="text-sm text-slate-400">No events for this filter.</p>
            ) : (
                <ol className="relative ml-2 border-l-2 border-slate-100">
                    {rows.map((e) => (
                        <li key={e.id} className="mb-4 ml-4">
                            <span className="absolute -left-[7px] mt-1 h-3 w-3 rounded-full bg-brand" />
                            <div className="text-xs text-slate-400">
                                {formatInstantDate(e.ts, 'undated')} ·{' '}
                                {e.source.replace('hubspot_', '')}
                            </div>
                            <div className="text-sm font-medium text-slate-800">{e.title}</div>
                            {e.detail && <div className="text-xs text-slate-500">{e.detail}</div>}
                            {e.evidence && e.evidence.startsWith('http') && (
                                <a
                                    href={e.evidence}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="inline-flex items-center gap-0.5 text-xs text-brand"
                                >
                                    source <ExternalLink className="h-3 w-3" />
                                </a>
                            )}
                        </li>
                    ))}
                </ol>
            )}
            <div className="no-print mt-2">
                {exhausted ? (
                    rows.length > 0 && (
                        <p className="text-xs text-slate-400">
                            That&rsquo;s the whole history on file for this filter.
                        </p>
                    )
                ) : (
                    <button
                        onClick={loadOlder}
                        disabled={busy}
                        className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                    >
                        {busy && <Loader2 size={12} className="animate-spin" />} Load older
                        events
                    </button>
                )}
            </div>
        </div>
    );
};

// Deep Research fans a multi-agent pipeline out over five channels and bills
// real Vertex tokens on every run, where a Fast X-Ray is a single call. Early
// use showed people reaching for the more impressive-sounding button by
// default, so the spend is named before it is committed — not left in a tooltip.
const DeepCostConfirm: React.FC<{
    companyName: string;
    onCancel: () => void;
    onConfirm: () => void;
}> = ({ companyName, onCancel, onConfirm }) => {
    // Escape closes; the confirm button takes focus so Enter runs it.
    const confirmRef = useRef<HTMLButtonElement | null>(null);
    useEffect(() => {
        confirmRef.current?.focus();
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onCancel();
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [onCancel]);

    return (
        <div
            className="no-print fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
            onClick={onCancel}
            role="dialog"
            aria-modal="true"
            aria-labelledby="deep-cost-title"
        >
            <div
                className="w-full max-w-md overflow-hidden rounded-2xl bg-white shadow-xl"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="flex items-start justify-between gap-3 border-b border-gray-100 px-5 py-4">
                    <h2
                        id="deep-cost-title"
                        className="flex items-center gap-2 text-lg font-semibold text-gray-900"
                    >
                        <Wallet size={18} className="text-brand" />
                        This one costs money
                    </h2>
                    <button
                        onClick={onCancel}
                        aria-label="Cancel"
                        className="text-gray-400 transition-colors hover:text-gray-600"
                    >
                        <X size={20} />
                    </button>
                </div>

                <div className="space-y-3 px-5 py-4 text-sm text-slate-700">
                    <p>
                        <b>Deep Research</b> commissions a full investigation of{' '}
                        <b>{companyName || 'this customer'}</b> across HubSpot, email,
                        meetings, documents and the web, then writes and emails you a PDF
                        brief. It takes a few minutes and bills AI usage every time.
                    </p>
                    <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
                        Worth it when something is riding on it — a renewal, a QBR, a save.
                        For a quick look, <b>Fast Research</b> is instant and covers the same
                        churn score, signals and timeline.
                    </p>
                </div>

                <div className="flex justify-end gap-2 border-t border-gray-100 px-5 py-3">
                    <button
                        onClick={onCancel}
                        className="rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50"
                    >
                        Cancel
                    </button>
                    <button
                        ref={confirmRef}
                        onClick={onConfirm}
                        className="inline-flex items-center gap-1 rounded-lg bg-brand px-3 py-2 text-sm font-medium text-white hover:bg-brand-hover"
                    >
                        <Telescope size={14} /> Yes, run Deep Research
                    </button>
                </div>
            </div>
        </div>
    );
};

// Every run is stored as its own numbered version, failed and unfinished ones
// included, so the ready versions listed here can skip numbers (v1, v3, v4).
// Same tab pattern as the Proposal Generator, oldest first. "Latest" is the
// run that finished most recently (persistence.latest_ready orders by
// updated_at), which is usually but not always the highest number: a Deep run
// that lands after a later Fast refresh is still the latest.
const VersionTabs: React.FC<{
    versions: HistoryRow[];
    latestId: string;
    selectedId: string | null;
    onSelect: (row: HistoryRow) => void;
}> = ({ versions, latestId, selectedId, onSelect }) => (
    <div
        className="no-print mt-2 flex flex-wrap items-center gap-1"
        role="tablist"
        aria-label="X-Ray versions"
        data-tour="xray-versions"
    >
        <HistoryIcon size={13} className="mr-0.5 text-slate-400" aria-hidden />
        {versions.map((v) => {
            const active = v.id === selectedId;
            const latest = v.id === latestId;
            const detail = [
                v.kind === 'deep' ? 'Deep Research' : 'Fast Research',
                formatInstant(v.created_at, {}, ''),
                v.churn.band ? `${v.churn.band} (${v.churn.score ?? '—'})` : '',
                latest ? 'latest' : '',
            ]
                .filter(Boolean)
                .join(' · ');
            return (
                <button
                    key={v.id}
                    type="button"
                    role="tab"
                    aria-selected={active}
                    title={detail}
                    onClick={() => onSelect(v)}
                    className={`rounded-md px-2 py-0.5 text-xs font-medium focus:outline-none focus:ring-2 focus:ring-brand ${
                        active
                            ? 'bg-brand text-white'
                            : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                    }`}
                >
                    v{v.version}
                    {latest && (
                        <span className={`ml-1 font-normal ${active ? 'text-white/80' : 'text-slate-400'}`}>
                            latest
                        </span>
                    )}
                </button>
            );
        })}
    </div>
);

/** Where an earlier version's timeline stops: the moment its data was pulled. */
const versionCutoff = (snap: XraySnapshot): string | undefined =>
    snap.covered_through || snap.updated_at || snap.created_at || undefined;

/** Client-side cut, only for when the server-side one could not be fetched. */
const eventsUpTo = (events: TimelineEvent[], asOf: string | undefined) => {
    const cutoff = instantMs(asOf);
    if (cutoff === null) return events;
    return events.filter((e) => {
        const at = instantMs(e.ts);
        return at !== null && at <= cutoff;
    });
};

const PRINT_CSS = `
@media print {
  .no-print { display: none !important; }
  .SectionCard { break-inside: avoid; box-shadow: none !important; }
  aside, nav { display: none !important; }
}
`;

export const CustomerXRay: React.FC = () => {
    const { companyId = '' } = useParams();
    const location = useLocation();
    const navigate = useNavigate();
    const state = (location.state || {}) as {
        name?: string;
        domain?: string;
        run?: 'fast' | 'deep';
    };

    // Always the LATEST ready version. What is on screen is `shown`, below.
    const [snapshot, setSnapshot] = useState<XraySnapshot | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    // Word-export / save-to-Assets actions (act on the on-screen snapshot).
    const [docBusy, setDocBusy] = useState<'word' | 'save' | null>(null);
    const [docMsg, setDocMsg] = useState<string | null>(null);

    // ── Versions ────────────────────────────────────────────────────────────
    // An earlier version opens read-only from `?version=N`, so it survives a
    // refresh, can be pasted to a teammate, and the browser's Back button
    // returns to the latest.
    const [params, setParams] = useSearchParams();
    const [history, setHistory] = useState<{ rows: HistoryRow[]; failed: boolean } | null>(
        null,
    );
    const historySeq = useRef(0);
    // Earlier versions already opened, by snapshot id, so flipping tabs is instant.
    const [versionCache, setVersionCache] = useState<Record<string, XraySnapshot>>({});
    /** Snapshot id of the version that failed to open; the next pick clears it. */
    const [versionError, setVersionError] = useState<string | null>(null);

    const rawVersion = params.get('version');
    const versionParam = rawVersion && /^\d+$/.test(rawVersion) ? Number(rawVersion) : null;
    // The latest's own number in the URL is simply the latest: no banner.
    const requested =
        versionParam !== null && versionParam !== snapshot?.version ? versionParam : null;
    const readyVersions = useMemo(
        () =>
            (history?.rows ?? [])
                .filter((r) => r.status === 'ready')
                .sort((a, b) => a.version - b.version),
        [history],
    );
    const requestedRow =
        requested === null ? null : (readyVersions.find((r) => r.version === requested) ?? null);
    const requestedId = requestedRow?.id ?? null;
    const older = requestedId ? (versionCache[requestedId] ?? null) : null;
    // Loading and "can't open" are derived, never stored, so the fetch effect
    // below sets no state synchronously (react-hooks/set-state-in-effect; same
    // shape as SmartTeamsSupport).
    const versionUnavailable =
        requested === null || history === null
            ? null
            : history.failed
              ? 'Could not load the saved versions of this X-Ray.'
              : !requestedRow
                ? `There is no finished v${requested} to open. A run that failed or never finished keeps its number but is not saved as a version.`
                : versionError === requestedRow.id
                  ? `Could not open v${requested}. Please try again.`
                  : null;
    const versionLoading = requested !== null && !older && !versionUnavailable;
    /** What is on screen: the latest, or the earlier version the URL names. */
    const shown = requested === null ? snapshot : older;
    const viewingOlder = requested !== null && older !== null;

    useEffect(() => {
        if (!requestedId || older || versionError === requestedId) return;
        let cancelled = false;
        getRun(requestedId)
            .then(async (res) => {
                if (res.status !== 'ready' || !res.snapshot) throw new Error('not ready');
                const snap = res.snapshot;
                const asOf = versionCutoff(snap);
                // Re-read below the cutoff rather than filtering what came back:
                // the embedded events are today's newest 100, and for an older run
                // they can all post-date it.
                const timeline = await getTimeline(companyId, {
                    limit: TIMELINE_PAGE,
                    before: asOf,
                }).catch(() => eventsUpTo(snap.timeline || [], asOf));
                if (!cancelled) {
                    setVersionCache((c) => ({ ...c, [snap.id]: { ...snap, timeline } }));
                }
            })
            .catch(() => {
                if (!cancelled) setVersionError(requestedId);
            });
        return () => {
            cancelled = true;
        };
    }, [companyId, requestedId, older, versionError]);

    const refreshHistory = useCallback(() => {
        // Sequenced: the read after a new run must not lose to a slower, older
        // read that is missing it.
        const seq = ++historySeq.current;
        getHistory(companyId)
            .then((rows) => {
                if (seq === historySeq.current) setHistory({ rows, failed: false });
            })
            .catch(() => {
                if (seq === historySeq.current) setHistory({ rows: [], failed: true });
            });
    }, [companyId]);

    /** `null` is the latest. Re-picking a version that failed to open retries it. */
    const openVersion = useCallback(
        (version: number | null) => {
            setDocMsg(null);
            setVersionError(null);
            const next = version === null ? null : String(version);
            if (next === rawVersion) return;
            // `run` is dropped from the router state on purpose: refreshing an
            // open version must not re-run the Fast Research that led here.
            // Only `version` changes: every other param stays.
            setParams(
                (prev) => {
                    const updated = new URLSearchParams(prev);
                    if (next === null) updated.delete('version');
                    else updated.set('version', next);
                    return updated;
                },
                { state: { name: state.name, domain: state.domain } },
            );
        },
        [rawVersion, setParams, state.name, state.domain],
    );

    const downloadWord = async () => {
        if (!shown?.id) return;
        setDocBusy('word');
        setDocMsg(null);
        try {
            const res = await downloadXrayDocx(shown.id);
            const name = (shown.company_name || 'customer')
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, '-')
                .replace(/^-+|-+$/g, '');
            const url = URL.createObjectURL(res.data);
            const a = document.createElement('a');
            a.href = url;
            // Matches the server's Content-Disposition name (with the version).
            a.download = `customer-xray-${name || 'customer'}-v${shown.version}.docx`;
            a.click();
            URL.revokeObjectURL(url);
        } catch {
            setDocMsg('Word download failed — please try again.');
        } finally {
            setDocBusy(null);
        }
    };

    const saveToAssets = async () => {
        if (!shown?.id) return;
        setDocBusy('save');
        setDocMsg(null);
        try {
            const res = await saveXrayToAssets(shown.id);
            setDocMsg(
                res.data?.created
                    ? 'Saved to your Assets page — share it with a teammate from there.'
                    : 'Already on your Assets page.',
            );
        } catch {
            setDocMsg('Save to Assets failed — please try again.');
        } finally {
            setDocBusy(null);
        }
    };
    // Deep Research is delivered by EMAIL (a PDF), like Deep Meeting Prep — it
    // does NOT render on screen. deepNotice drives a small status card.
    const [deepNotice, setDeepNotice] = useState<{
        phase: 'queued' | 'running' | 'ready' | 'failed' | 'conflict' | 'timeout';
        delivery?: string | null;
        error?: string;
    } | null>(null);
    const pollRef = useRef<number | null>(null);
    const didInit = useRef(false);
    // Every route into a Deep run passes through this flag, so the cost warning
    // cannot be skipped by arriving from the Customers grid's Deep button (which
    // just navigates here with state.run === 'deep').
    const [confirmDeep, setConfirmDeep] = useState(false);

    const stopPolling = useCallback(() => {
        if (pollRef.current !== null) {
            window.clearInterval(pollRef.current);
            pollRef.current = null;
        }
    }, []);
    useEffect(() => stopPolling, [stopPolling]);

    // Poll only to update the status line (queued → running → emailed/failed).
    // The brief itself is never auto-rendered — it arrives as an emailed PDF.
    const pollDeepStatus = useCallback(
        (snapshotId: string) => {
            stopPolling();
            let polls = 0;
            const MAX = 90; // ~7.5 min at 5s
            const tick = async () => {
                polls += 1;
                try {
                    const res = await getRun(snapshotId);
                    if (res.status === 'ready') {
                        stopPolling();
                        setDeepNotice({
                            phase: 'ready',
                            delivery: (res.progress || {}).delivery || null,
                        });
                        return;
                    }
                    if (res.status === 'failed') {
                        stopPolling();
                        setDeepNotice({
                            phase: 'failed',
                            error: res.error ===
                                'Selected AI provider unavailable for X-Ray analysis. Check its connection in Admin.'
                                ? res.error
                                : undefined,
                        });
                        return;
                    }
                    setDeepNotice({ phase: 'running' });
                } catch {
                    /* transient — keep polling */
                }
                if (polls >= MAX) {
                    stopPolling();
                    setDeepNotice({ phase: 'timeout' });
                }
            };
            tick();
            pollRef.current = window.setInterval(tick, 5000);
        },
        [stopPolling],
    );

    const startFast = useCallback(async () => {
        // A refresh always builds a new latest, so leave any earlier version.
        openVersion(null);
        setDeepNotice(null);
        stopPolling();
        setLoading(true);
        setError(null);
        try {
            const snap = await runFast(companyId, {
                company_name: state.name,
                company_domain: state.domain,
            });
            // A failed snapshot must surface the error banner (which renders on
            // `error && !snapshot`), not a blank page — so don't set it as the
            // active snapshot.
            if (snap.status === 'failed') {
                setError(snap.error || 'Could not build the X-Ray.');
            } else {
                setSnapshot(snap);
                refreshHistory();
            }
        } catch {
            setError('Fast Research failed. Please try again.');
        } finally {
            setLoading(false);
        }
    }, [companyId, state.name, state.domain, stopPolling, openVersion, refreshHistory]);

    // Ask before spending: opens the cost warning. Confirming calls startDeep.
    const requestDeep = useCallback(() => setConfirmDeep(true), []);

    const startDeep = useCallback(async () => {
        // Fire-and-forget: kick off the async run, then show the email
        // confirmation. Nothing renders the brief on screen.
        setConfirmDeep(false);
        openVersion(null);
        setLoading(false);
        setError(null);
        setSnapshot(null);
        setDeepNotice({ phase: 'queued' });
        try {
            const { snapshot_id } = await runDeep(companyId, {
                company_name: state.name,
                company_domain: state.domain,
            });
            pollDeepStatus(snapshot_id);
        } catch (e) {
            const status = (e as { response?: { status?: number } })?.response?.status;
            setDeepNotice({ phase: status === 409 ? 'conflict' : 'failed' });
        }
    }, [companyId, state.name, state.domain, pollDeepStatus, openVersion]);

    const loadLatest = useCallback(() => {
        setDeepNotice(null);
        stopPolling();
        setLoading(true);
        setError(null);
        refreshHistory();
        getLatest(companyId)
            .then((snap) => setSnapshot(snap))
            .catch((e) =>
                setError(
                    e?.response?.status === 404
                        ? 'No X-Ray generated for this customer yet.'
                        : 'Could not load the X-Ray.',
                ),
            )
            .finally(() => setLoading(false));
    }, [companyId, stopPolling, refreshHistory]);

    useEffect(() => {
        if (didInit.current) return;
        didInit.current = true;
        if (state.run === 'fast') {
            startFast();
        } else if (state.run === 'deep') {
            // Arrived from the Customers grid's Deep button — confirm the spend
            // before making it. Nothing loads behind the dialog; cancelling falls
            // back to whatever X-Ray already exists.
            setLoading(false);
            requestDeep();
        } else {
            loadLatest();
        }
    }, [state.run, startFast, requestDeep, loadLatest]);

    const sections = shown?.sections || {};
    const signals = shown?.signals || {};
    const churn = signals.churn;
    const trendIcon =
        signals.activity_trend?.direction === 'up'
            ? ArrowUpRight
            : signals.activity_trend?.direction === 'down'
              ? ArrowDownRight
              : ArrowRight;
    // One finished X-Ray is not a choice; a Deep run in flight replaces the page.
    const showSwitcher = !deepNotice && !!snapshot && readyVersions.length > 1;

    return (
        <div className="h-full overflow-auto bg-gray-50">
            <style dangerouslySetInnerHTML={{ __html: PRINT_CSS }} />
            {confirmDeep && (
                <DeepCostConfirm
                    companyName={snapshot?.company_name || state.name || ''}
                    onCancel={() => {
                        setConfirmDeep(false);
                        if (!snapshot && !deepNotice) loadLatest();
                    }}
                    onConfirm={startDeep}
                />
            )}
            <div className="mx-auto max-w-[1400px] px-6 py-6">
                {/* Header */}
                <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
                    <div className="flex items-center gap-2" data-tour="xray-header">
                        <Building2 className="h-6 w-6 text-brand" />
                        <div>
                            <h1 className="text-2xl font-bold text-slate-900">
                                {shown?.company_name ||
                                    snapshot?.company_name ||
                                    state.name ||
                                    'Customer X-Ray'}
                            </h1>
                            <p className="text-xs text-slate-500">
                                {signals.csm ? `CSM: ${signals.csm}` : ''}
                                {signals.client_status ? ` · ${signals.client_status}` : ''}
                                {shown ? ` · v${shown.version} · ${shown.kind}` : ''}
                            </p>
                            {showSwitcher && snapshot && (
                                <VersionTabs
                                    versions={readyVersions}
                                    latestId={snapshot.id}
                                    selectedId={requested === null ? snapshot.id : requestedId}
                                    onSelect={(row) =>
                                        openVersion(row.id === snapshot.id ? null : row.version)
                                    }
                                />
                            )}
                        </div>
                    </div>
                    <div className="no-print flex items-center gap-2">
                        <button
                            data-tour="xray-research"
                            onClick={startFast}
                            disabled={loading}
                            className="inline-flex items-center gap-1 rounded-lg bg-brand px-3 py-2 text-sm font-medium text-white hover:bg-brand-hover disabled:opacity-50"
                        >
                            <Zap size={14} /> Fast Research
                        </button>
                        <button
                            onClick={requestDeep}
                            disabled={loading}
                            title="Commissions a full multi-source brief — a few minutes, and it costs AI usage"
                            className="inline-flex items-center gap-1 rounded-lg border border-brand px-3 py-2 text-sm font-medium text-brand hover:bg-brand/5 disabled:opacity-50"
                        >
                            <Telescope size={14} /> Deep Research
                        </button>
                        <button
                            data-tour="xray-export"
                            onClick={() => {
                                // Browsers name the saved PDF after document.title
                                // (otherwise "Cubie"); use the customer name, then restore.
                                const prev = document.title;
                                const name = shown?.company_name || snapshot?.company_name || state.name;
                                const version = shown ? ` v${shown.version}` : '';
                                document.title = name ? `Customer X-Ray - ${name}${version}` : prev;
                                window.addEventListener('afterprint', () => { document.title = prev; }, { once: true });
                                window.print();
                            }}
                            className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50"
                        >
                            <Printer size={14} /> PDF
                        </button>
                        {shown?.id && (
                            <>
                                <button
                                    onClick={downloadWord}
                                    disabled={docBusy !== null}
                                    title="Download as an editable Word document"
                                    className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                                >
                                    {docBusy === 'word' ? (
                                        <Loader2 size={14} className="animate-spin" />
                                    ) : (
                                        <FileDown size={14} />
                                    )}{' '}
                                    Word
                                </button>
                                <button
                                    onClick={saveToAssets}
                                    disabled={docBusy !== null}
                                    title="Save to your Assets page to share it with teammates"
                                    className="inline-flex items-center gap-1 whitespace-nowrap rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                                >
                                    {docBusy === 'save' ? (
                                        <Loader2 size={14} className="animate-spin" />
                                    ) : (
                                        <FolderPlus size={14} />
                                    )}{' '}
                                    Save to Assets
                                </button>
                            </>
                        )}
                        <button
                            onClick={() => navigate('/customers')}
                            className="rounded-lg px-2 py-2 text-sm text-slate-500 hover:text-slate-800"
                        >
                            ← Back
                        </button>
                    </div>
                </div>

                {docMsg && (
                    <div className="no-print mb-4 flex items-center gap-2 rounded-xl border border-brand/20 bg-brand/5 px-4 py-2.5 text-sm font-medium text-brand">
                        <FolderPlus size={15} className="shrink-0" />
                        {docMsg}
                    </div>
                )}

                {/* An earlier version is read-only. The note prints with it, so a
                    saved PDF still says it is not the latest; the button does not. */}
                {(viewingOlder || versionUnavailable) && (
                    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800">
                        <HistoryIcon size={15} className="shrink-0" />
                        <span className="min-w-0 flex-1">
                            {viewingOlder && shown ? (
                                <>
                                    You&rsquo;re viewing <b>v{shown.version}</b>, a{' '}
                                    {shown.kind === 'deep' ? 'Deep' : 'Fast'} Research X-Ray from{' '}
                                    {formatInstantDate(shown.created_at, 'an earlier run')}. It is
                                    read-only
                                    {snapshot ? (
                                        <>
                                            {' '}
                                            — the latest is <b>v{snapshot.version}</b>
                                        </>
                                    ) : null}
                                    .
                                </>
                            ) : (
                                versionUnavailable
                            )}
                        </span>
                        <button
                            onClick={() => openVersion(null)}
                            className="no-print rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-hover"
                        >
                            Back to latest
                        </button>
                    </div>
                )}

                {/* Deep Research — delivered as an emailed PDF (not on screen). */}
                {deepNotice && (
                    <div className="rounded-2xl border border-brand/20 bg-brand/5 p-6">
                        <div className="flex items-center gap-2 text-brand">
                            {['queued', 'running'].includes(deepNotice.phase) ? (
                                <Loader2 className="h-5 w-5 animate-spin" />
                            ) : (
                                <Telescope className="h-5 w-5" />
                            )}
                            <span className="font-semibold">Deep Research</span>
                        </div>
                        <p className="mt-2 text-sm text-slate-700">
                            {deepNotice.phase === 'conflict'
                                ? 'A Deep Research run is already in progress for this customer — we’ll email the PDF when it’s done.'
                                : deepNotice.phase === 'failed'
                                  ? deepNotice.error || 'Deep Research failed. Please try again.'
                                  : deepNotice.phase === 'timeout'
                                    ? 'Still working — this one’s taking a little longer. The PDF will be emailed to you when it’s ready; you can close this page.'
                                    : deepNotice.phase === 'ready'
                                      ? deepNotice.delivery === 'emailed'
                                          ? 'Done — the full brief has been emailed to you as a PDF. 📧'
                                          : 'Done — your brief is ready. (Email delivery is off in this environment; open the saved X-Ray below.)'
                                      : 'Started. We’re compiling the full brief and will email it to you as a PDF in a few minutes — you can close this page.'}
                        </p>
                        <div className="no-print mt-3 flex gap-2">
                            {deepNotice.phase === 'ready' && (
                                <button
                                    onClick={loadLatest}
                                    className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white"
                                >
                                    View the saved X-Ray
                                </button>
                            )}
                            <button
                                onClick={() => navigate('/customers')}
                                className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600"
                            >
                                Back to Customers
                            </button>
                        </div>
                    </div>
                )}

                {versionLoading && (
                    <div className="rounded-2xl border border-slate-200 bg-white p-8">
                        <div className="flex items-center gap-2 text-slate-600">
                            <Loader2 className="h-5 w-5 animate-spin text-brand" />
                            <span className="font-medium">Opening v{requested}…</span>
                        </div>
                    </div>
                )}

                {/* Fast X-Ray builds inline; show a spinner while it runs. */}
                {loading && !snapshot && !deepNotice && requested === null && (
                    <div className="rounded-2xl border border-slate-200 bg-white p-8">
                        <div className="flex items-center gap-2 text-slate-600">
                            <Loader2 className="h-5 w-5 animate-spin text-brand" />
                            <span className="font-medium">Building the X-Ray…</span>
                        </div>
                    </div>
                )}

                {error && !snapshot && requested === null && (
                    <div className="rounded-2xl border border-amber-200 bg-amber-50 p-6 text-sm text-amber-700">
                        <div className="mb-2 flex items-center gap-2 font-semibold">
                            <AlertCircle className="h-4 w-4" /> {error}
                        </div>
                        <div className="no-print mt-3 flex gap-2">
                            <button
                                onClick={startFast}
                                className="rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white"
                            >
                                Generate Fast Research
                            </button>
                            <button
                                onClick={requestDeep}
                                className="rounded-lg border border-brand px-3 py-1.5 text-xs font-medium text-brand"
                            >
                                Deep Research
                            </button>
                        </div>
                    </div>
                )}

                {shown && shown.status === 'ready' && (
                    <div className="space-y-5">
                        {/* Signal row */}
                        <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
                            <div className="col-span-2 flex items-center justify-center rounded-xl border border-slate-200 bg-white p-3 md:col-span-1">
                                <ChurnGauge score={churn?.score} band={churn?.band} />
                            </div>
                            <SignalTile
                                icon={CalendarClock}
                                label="Last touch"
                                value={
                                    signals.days_since_last_touch != null
                                        ? `${signals.days_since_last_touch}d`
                                        : '—'
                                }
                            />
                            <SignalTile
                                icon={Package}
                                label="Products"
                                value={signals.active_products_count ?? 0}
                            />
                            <SignalTile
                                icon={TrendingUp}
                                label="Open pipeline"
                                value={
                                    signals.open_pipeline_value
                                        ? `$${Math.round(signals.open_pipeline_value).toLocaleString()}`
                                        : '—'
                                }
                            />
                            <SignalTile
                                icon={CalendarClock}
                                label="Renewal in"
                                value={
                                    signals.renewal_countdown_days != null
                                        ? `${signals.renewal_countdown_days}d`
                                        : '—'
                                }
                            />
                            <SignalTile
                                icon={trendIcon}
                                label="Activity 90d"
                                value={
                                    signals.activity_trend
                                        ? `${signals.activity_trend.last_90d}/${signals.activity_trend.prior_90d}`
                                        : '—'
                                }
                            />
                        </div>

                        {/* Intelligence gaps */}
                        {shown.partial_errors?.length > 0 && (
                            <div className="rounded-2xl border border-amber-100 bg-amber-50 p-4">
                                <div className="mb-1 flex items-center gap-2 text-sm font-bold text-amber-700">
                                    <AlertCircle className="h-4 w-4" /> Intelligence Gaps
                                </div>
                                <ul className="list-disc pl-5 text-xs text-amber-600">
                                    {shown.partial_errors.map((e, i) => (
                                        <li key={i}>{e}</li>
                                    ))}
                                </ul>
                            </div>
                        )}

                        {sections.executive_summary && (
                            <SectionCard icon={Activity} title="Executive Summary">
                                <Prose md={sections.executive_summary} />
                            </SectionCard>
                        )}

                        {sections.relationship_health?.narrative_md && (
                            <SectionCard icon={TrendingUp} title="Relationship Health">
                                <Prose md={sections.relationship_health.narrative_md} />
                                <div className="mt-3 flex flex-wrap gap-2 text-xs text-slate-500">
                                    {(
                                        ['cadence', 'breadth', 'responsiveness', 'sentiment_trend'] as const
                                    ).map((k) =>
                                        sections.relationship_health?.[k] ? (
                                            <span key={k} className="rounded-full bg-slate-100 px-2 py-1">
                                                {k.replace('_', ' ')}:{' '}
                                                {String(sections.relationship_health[k])}
                                            </span>
                                        ) : null,
                                    )}
                                </div>
                            </SectionCard>
                        )}

                        {sections.commercial_health && (
                            <SectionCard icon={TrendingUp} title="Commercial Health">
                                <Prose md={sections.commercial_health} />
                            </SectionCard>
                        )}

                        {/* Churn factor table */}
                        {churn?.factors && churn.factors.length > 0 && (
                            <SectionCard icon={Gauge} title="Churn Factors">
                                {churn.rationale_md && <Prose md={churn.rationale_md} />}
                                <div className="mt-3 overflow-x-auto">
                                    <table className="w-full text-left text-xs">
                                        <thead className="text-slate-400">
                                            <tr>
                                                <th className="py-1 pr-3">Factor</th>
                                                <th className="py-1 pr-3">Value</th>
                                                <th className="py-1 pr-3">Risk</th>
                                                <th className="py-1">Evidence</th>
                                            </tr>
                                        </thead>
                                        <tbody className="text-slate-700">
                                            {churn.factors.map((f) => (
                                                <tr key={f.name} className="border-t border-slate-100">
                                                    <td className="py-1.5 pr-3 font-medium">
                                                        {f.name.replace(/_/g, ' ')}
                                                    </td>
                                                    <td className="py-1.5 pr-3">{f.value}</td>
                                                    <td className="py-1.5 pr-3">{Math.round(f.points)}</td>
                                                    <td className="py-1.5 text-slate-500">{f.evidence}</td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            </SectionCard>
                        )}

                        {/* Opportunities */}
                        {sections.opportunities && sections.opportunities.length > 0 && (
                            <SectionCard icon={Lightbulb} title="Expansion Opportunities">
                                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                                    {sections.opportunities.map((o, i) => (
                                        <div key={i} className="rounded-xl border border-slate-200 p-3">
                                            <div className="flex items-center justify-between gap-2">
                                                <h4 className="font-semibold text-slate-800">{o.title}</h4>
                                                {o.entity && (
                                                    <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500">
                                                        {o.entity}
                                                    </span>
                                                )}
                                            </div>
                                            {o.why_now && (
                                                <p className="mt-1 text-xs text-slate-500">{o.why_now}</p>
                                            )}
                                            {o.play && (
                                                <p className="mt-1 text-xs text-slate-600">
                                                    <b>Play:</b> {o.play}
                                                </p>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            </SectionCard>
                        )}

                        {/* Recent news */}
                        {sections.recent_news && sections.recent_news.length > 0 && (
                            <SectionCard
                                icon={Newspaper}
                                title="Recent News"
                                asOf={sections._as_of?.recent_news}
                            >
                                <ul className="space-y-3">
                                    {sections.recent_news.map((n, i) => (
                                        <li key={i} className="border-l-2 border-slate-100 pl-3">
                                            <a
                                                href={n.url}
                                                target="_blank"
                                                rel="noopener noreferrer"
                                                className="inline-flex items-center gap-1 text-sm font-medium text-brand"
                                            >
                                                {n.headline} <ExternalLink className="h-3 w-3" />
                                            </a>
                                            <div className="text-xs text-slate-400">{n.date}</div>
                                            {n.why_it_matters && (
                                                <p className="text-xs text-slate-600">{n.why_it_matters}</p>
                                            )}
                                        </li>
                                    ))}
                                </ul>
                            </SectionCard>
                        )}

                        {/* Evidence */}
                        {sections.source_evidence && sections.source_evidence.length > 0 && (
                            <SectionCard icon={ExternalLink} title="Sources">
                                <ul className="list-disc pl-5 text-xs text-slate-500">
                                    {sections.source_evidence.map((s, i) => (
                                        <li key={i}>{s}</li>
                                    ))}
                                </ul>
                            </SectionCard>
                        )}

                        {/* Relationship timeline — full history, after the analysis
                            and collapsed by default (click to expand). */}
                        <CollapsibleSection
                            icon={CalendarClock}
                            title="Relationship Timeline"
                            subtitle={`${(shown.timeline || []).length}${
                                (shown.timeline || []).length >= TIMELINE_PAGE ? '+' : ''
                            } events — click to expand`}
                        >
                            <Timeline
                                key={shown.id}
                                companyId={companyId}
                                events={shown.timeline || []}
                                asOf={viewingOlder ? versionCutoff(shown) : undefined}
                            />
                        </CollapsibleSection>

                        {/* Feedback — trains future X-Rays (WGLL). Latest only: a
                            rating on a superseded brief would teach the wrong one. */}
                        {!viewingOlder && (
                            <div className="no-print flex flex-col items-center gap-2 border-t border-slate-200/60 pt-6">
                                <p className="text-sm font-semibold text-slate-500">
                                    Was this X-Ray useful?
                                </p>
                                <FeedbackButtons
                                    surface="xray"
                                    proposalId={shown.company_id}
                                    proposalText={[shown.company_name, sections.executive_summary]
                                        .filter(Boolean)
                                        .join('\n\n')}
                                    proposalMetadata={{
                                        snapshot_id: shown.id,
                                        kind: shown.kind,
                                        version: shown.version,
                                        churn_band: churn?.band,
                                        origin: 'xray',
                                    }}
                                    hideLabel
                                />
                            </div>
                        )}

                        {shown.covered_through && (
                            <p className="pb-8 text-center text-xs text-slate-400">
                                Coverage through{' '}
                                {formatInstantDate(shown.covered_through)} · generated{' '}
                                {formatInstant(shown.created_at, {}, '')}
                            </p>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
};

export default CustomerXRay;
