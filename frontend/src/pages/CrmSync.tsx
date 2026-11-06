// UC-46 — CRM Sync review page. Cubie reads your recent external call
// transcripts, compares them against HubSpot, and proposes record updates.
// Nothing is written until you approve that specific item.
//
// Scans run on a Cloud Run Job; this polls /runs/{id} every 5s (no SSE/WS: a
// long-lived stream holds a database connection the budget cannot spare).
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
    AlertCircle,
    ArrowRight,
    Building2,
    CalendarDays,
    Check,
    ExternalLink,
    FileText,
    Loader2,
    Pencil,
    Quote,
    RefreshCw,
    Sparkles,
    UserPlus,
    X,
} from 'lucide-react';
import {
    approveLabel,
    approveSuggestion,
    canApprove,
    canEdit,
    dismissSuggestion,
    getRun,
    hubspotUrl,
    listSuggestions,
    startScan,
    type ChangeType,
    type CrmSuggestion,
    type PropertyDiff,
    type ScanRun,
} from '../api/crmSync';
import { FeedbackButtons } from '../components/FeedbackButtons';
import { formatInstant } from '../utils/datetime';

const POLL_MS = 5000;
// The scan's own ceiling is 900s; stop polling a little after that rather than
// spinning forever if the run row never reaches a terminal state.
const MAX_POLLS = 200;

const CHANGE_META: Record<ChangeType, { label: string; icon: React.FC<{ className?: string }>; tone: string }> = {
    create_contact: { label: 'New contact', icon: UserPlus, tone: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
    update_contact: { label: 'Update contact', icon: Pencil, tone: 'bg-blue-50 text-blue-700 border-blue-200' },
    create_deal: { label: 'New deal', icon: Building2, tone: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
    update_deal: { label: 'Update deal', icon: ArrowRight, tone: 'bg-amber-50 text-amber-800 border-amber-200' },
};

const PROPERTY_LABELS: Record<string, string> = {
    firstname: 'First name',
    lastname: 'Last name',
    email: 'Email',
    phone: 'Phone',
    mobilephone: 'Mobile',
    jobtitle: 'Job title',
    company: 'Company',
    dealname: 'Deal name',
    dealstage: 'Stage',
    amount: 'Amount',
    closedate: 'Close date',
    pipeline: 'Pipeline',
};

// The router's `detail` when the request reached it, else a plain fallback.
function errorDetail(e: unknown, fallback: string): string {
    const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return typeof detail === 'string' && detail ? detail : fallback;
}

function propertyLabel(name: string): string {
    return PROPERTY_LABELS[name] || name;
}

function confidenceBand(value: number | null): { label: string; tone: string } | null {
    if (value === null || value === undefined) return null;
    if (value >= 0.8) return { label: 'High confidence', tone: 'text-emerald-700 bg-emerald-50' };
    if (value >= 0.65) return { label: 'Medium confidence', tone: 'text-amber-700 bg-amber-50' };
    return { label: 'Low confidence', tone: 'text-slate-600 bg-slate-100' };
}

function meetingLabel(s: CrmSuggestion): string {
    const title = s.meeting.title || 'Untitled meeting';
    if (!s.meeting.date) return title;
    return `${title} · ${formatInstant(s.meeting.date, { month: 'short', day: 'numeric' })}`;
}

const DiffRow: React.FC<{ name: string; diff: PropertyDiff }> = ({ name, diff }) => (
    <div className="flex flex-wrap items-baseline gap-2 py-1 text-sm">
        <span className="w-28 shrink-0 text-slate-500">{propertyLabel(name)}</span>
        {diff.before ? (
            <span className="text-slate-400 line-through">{diff.before}</span>
        ) : (
            <span className="italic text-slate-400">empty</span>
        )}
        <ArrowRight className="h-3.5 w-3.5 shrink-0 text-slate-400" />
        <span className="font-medium text-slate-900">{diff.after}</span>
    </div>
);

const EditModal: React.FC<{
    suggestion: CrmSuggestion;
    onCancel: () => void;
    onSave: (values: Record<string, string>) => void;
    saving: boolean;
}> = ({ suggestion, onCancel, onSave, saving }) => {
    const [values, setValues] = useState<Record<string, string>>(() =>
        Object.fromEntries(Object.entries(suggestion.properties).map(([k, v]) => [k, v.after])),
    );
    const fieldClass =
        'mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm ' +
        'focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand';

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
            <div className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl">
                <h3 className="text-lg font-semibold text-slate-900">Edit before approving</h3>
                <p className="mt-1 text-sm text-slate-500">
                    Only these fields can be changed on this record.
                </p>
                <div className="mt-4 space-y-3">
                    {Object.entries(values).map(([name, value]) => (
                        <label key={name} className="block">
                            <span className="text-sm font-medium text-slate-700">{propertyLabel(name)}</span>
                            <input
                                className={fieldClass}
                                value={value}
                                onChange={e => setValues({ ...values, [name]: e.target.value })}
                            />
                        </label>
                    ))}
                </div>
                <div className="mt-5 flex justify-end gap-2">
                    <button
                        onClick={onCancel}
                        className="rounded-md px-3 py-2 text-sm text-slate-600 hover:bg-slate-100"
                        disabled={saving}
                    >
                        Cancel
                    </button>
                    <button
                        onClick={() => onSave(values)}
                        disabled={saving}
                        className="inline-flex items-center gap-2 rounded-md bg-brand px-4 py-2 text-sm
                                   font-medium text-white hover:bg-brand-hover disabled:opacity-60"
                    >
                        {saving && <Loader2 className="h-4 w-4 animate-spin" />}
                        Approve with changes
                    </button>
                </div>
            </div>
        </div>
    );
};

const SuggestionCard: React.FC<{
    suggestion: CrmSuggestion;
    portalId: string | null;
    busy: boolean;
    onApprove: (edited?: Record<string, string>) => void;
    onDismiss: () => void;
}> = ({ suggestion, portalId, busy, onApprove, onDismiss }) => {
    const [editing, setEditing] = useState(false);
    const meta = CHANGE_META[suggestion.change_type];
    const Icon = meta?.icon ?? FileText;
    const band = confidenceBand(suggestion.confidence);
    const link = hubspotUrl(portalId, suggestion.hubspot_object_type, suggestion.hubspot_object_id);
    const actionable = suggestion.status === 'pending' || suggestion.status === 'failed';
    const approvable = canApprove(suggestion);
    const editable = canEdit(suggestion);

    return (
        <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
            <div className="flex flex-wrap items-center gap-2">
                <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${meta?.tone}`}>
                    <Icon className="h-3.5 w-3.5" />
                    {meta?.label ?? suggestion.change_type}
                </span>
                {band && (
                    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${band.tone}`}>{band.label}</span>
                )}
                {suggestion.status === 'applied' && (
                    <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
                        Applied
                    </span>
                )}
                {suggestion.status === 'dismissed' && (
                    <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
                        Dismissed
                    </span>
                )}
                {link && (
                    <a
                        href={link}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="ml-auto inline-flex items-center gap-1 text-xs text-slate-500 hover:text-brand"
                    >
                        Open in HubSpot <ExternalLink className="h-3 w-3" />
                    </a>
                )}
            </div>

            {suggestion.contact_email && (
                <p className="mt-3 text-sm font-medium text-slate-900">{suggestion.contact_email}</p>
            )}

            <div className="mt-3">
                {Object.entries(suggestion.properties).map(([name, diff]) => (
                    <DiffRow key={name} name={name} diff={diff} />
                ))}
            </div>

            {suggestion.rationale && (
                <p className="mt-3 text-sm text-slate-600">{suggestion.rationale}</p>
            )}

            {suggestion.evidence?.quote && (
                <blockquote className="mt-3 flex gap-2 border-l-2 border-slate-200 pl-3 text-sm italic text-slate-500">
                    <Quote className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span>
                        “{suggestion.evidence.quote}”
                        {suggestion.evidence.speaker && (
                            <span className="not-italic"> — {suggestion.evidence.speaker}</span>
                        )}
                    </span>
                </blockquote>
            )}

            {suggestion.error && (
                <p className="mt-3 flex items-start gap-2 rounded-md bg-red-50 p-2.5 text-sm text-red-700">
                    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                    {suggestion.error}
                </p>
            )}

            {actionable && (
                <div className="mt-4 flex items-center gap-2">
                    <button
                        onClick={() => onApprove()}
                        disabled={busy || !approvable}
                        className="inline-flex items-center gap-1.5 rounded-md bg-brand px-3 py-1.5 text-sm
                                   font-medium text-white hover:bg-brand-hover disabled:cursor-not-allowed
                                   disabled:opacity-60"
                    >
                        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                        {approveLabel(suggestion)}
                    </button>
                    {editable && (
                        <button
                            onClick={() => setEditing(true)}
                            disabled={busy}
                            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-3 py-1.5
                                       text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-60"
                        >
                            <Pencil className="h-3.5 w-3.5" /> Edit
                        </button>
                    )}
                    <button
                        onClick={onDismiss}
                        disabled={busy}
                        className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-slate-500
                                   hover:bg-slate-100 disabled:opacity-60"
                    >
                        <X className="h-3.5 w-3.5" /> Dismiss
                    </button>
                    <div className="ml-auto">
                        <FeedbackButtons
                            surface="crm_sync"
                            proposalId={suggestion.id}
                            proposalText={`${suggestion.change_type}: ${JSON.stringify(suggestion.properties)}`}
                            proposalMetadata={{ change_type: suggestion.change_type, confidence: suggestion.confidence }}
                        />
                    </div>
                </div>
            )}

            {editing && (
                <EditModal
                    suggestion={suggestion}
                    saving={busy}
                    onCancel={() => setEditing(false)}
                    onSave={values => {
                        setEditing(false);
                        onApprove(values);
                    }}
                />
            )}
        </div>
    );
};

const CrmSync: React.FC = () => {
    const [suggestions, setSuggestions] = useState<CrmSuggestion[]>([]);
    const [portalId, setPortalId] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);
    const [run, setRun] = useState<ScanRun | null>(null);
    const [scanning, setScanning] = useState(false);
    const [busyId, setBusyId] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [showHistory, setShowHistory] = useState(false);
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

    const refresh = useCallback(async () => {
        try {
            const data = await listSuggestions('all');
            setSuggestions(data.suggestions);
            setPortalId(data.hubspot_portal_id);
            setError(null);
        } catch (e: unknown) {
            setError(errorDetail(e, 'Could not load your CRM suggestions.'));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        refresh();
        return () => {
            if (pollRef.current) clearInterval(pollRef.current);
        };
    }, [refresh]);

    const beginScan = async () => {
        setScanning(true);
        setError(null);
        try {
            const { run_id } = await startScan();
            let polls = 0;
            pollRef.current = setInterval(async () => {
                polls += 1;
                try {
                    const status = await getRun(run_id);
                    setRun(status);
                    if (status.status !== 'pending' || polls >= MAX_POLLS) {
                        if (pollRef.current) clearInterval(pollRef.current);
                        setScanning(false);
                        refresh();
                    }
                } catch {
                    if (pollRef.current) clearInterval(pollRef.current);
                    setScanning(false);
                }
            }, POLL_MS);
        } catch (e: unknown) {
            setScanning(false);
            setError(errorDetail(e, 'Could not start the scan.'));
        }
    };

    const act = async (id: string, fn: () => Promise<CrmSuggestion>) => {
        setBusyId(id);
        setError(null);
        try {
            const updated = await fn();
            setSuggestions(prev => prev.map(s => (s.id === id ? updated : s)));
        } catch (e: unknown) {
            setError(errorDetail(e, 'That action did not go through.'));
            refresh(); // a 409 means our copy is stale
        } finally {
            setBusyId(null);
        }
    };

    const pending = suggestions.filter(s => s.status === 'pending' || s.status === 'failed');
    const handled = suggestions.filter(s => ['applied', 'dismissed'].includes(s.status));

    const grouped = pending.reduce<Record<string, CrmSuggestion[]>>((acc, s) => {
        const key = meetingLabel(s);
        (acc[key] ||= []).push(s);
        return acc;
    }, {});

    return (
        <div className="mx-auto max-w-4xl px-4 py-6">
            <div className="flex flex-wrap items-center gap-3">
                <div data-tour="crm-sync-header">
                    <h1 className="text-2xl font-semibold text-slate-900">CRM Sync</h1>
                    <p className="mt-1 text-sm text-slate-500">
                        Cubie reads your recent external calls and suggests HubSpot updates.
                        Nothing is written until you approve it.
                    </p>
                </div>
                <button
                    data-tour="crm-sync-scan"
                    onClick={beginScan}
                    disabled={scanning}
                    className="ml-auto inline-flex items-center gap-2 rounded-md bg-brand px-4 py-2 text-sm
                               font-medium text-white hover:bg-brand-hover disabled:opacity-60"
                >
                    {scanning ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                    {scanning ? 'Scanning your calls…' : 'Scan my calls'}
                </button>
            </div>

            {scanning && (
                <p className="mt-4 flex items-center gap-2 rounded-md bg-slate-50 p-3 text-sm text-slate-600">
                    <Sparkles className="h-4 w-4 text-brand" />
                    Reading your recent call transcripts and comparing them to HubSpot. This takes
                    a couple of minutes — you can leave this page and come back.
                </p>
            )}

            {!scanning && run?.status === 'ready' && (
                <p className="mt-4 rounded-md bg-emerald-50 p-3 text-sm text-emerald-800">
                    Scanned {run.stats.transcripts_scanned ?? 0} transcript(s) and found{' '}
                    {run.stats.suggestions_created ?? 0} suggested update(s).
                </p>
            )}

            {!scanning && run?.status === 'failed' && (
                <p className="mt-4 rounded-md bg-red-50 p-3 text-sm text-red-700">
                    {run.error || 'That scan did not finish.'}
                </p>
            )}

            {error && (
                <p className="mt-4 flex items-start gap-2 rounded-md bg-red-50 p-3 text-sm text-red-700">
                    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                    {error}
                </p>
            )}

            {loading ? (
                <div className="mt-10 flex justify-center">
                    <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
                </div>
            ) : pending.length === 0 ? (
                <div className="mt-10 rounded-lg border border-dashed border-slate-300 p-10 text-center">
                    <Sparkles className="mx-auto h-8 w-8 text-slate-300" />
                    <p className="mt-3 font-medium text-slate-700">Nothing to review</p>
                    <p className="mt-1 text-sm text-slate-500">
                        Your CRM matches what your recent calls said. Run a scan after your next
                        few meetings.
                    </p>
                </div>
            ) : (
                <div className="mt-6 space-y-8" data-tour="crm-sync-suggestions">
                    {Object.entries(grouped).map(([meeting, items]) => (
                        <section key={meeting}>
                            <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-700">
                                <CalendarDays className="h-4 w-4 text-slate-400" />
                                {meeting}
                                <span className="font-normal text-slate-400">
                                    · {items.length} suggestion{items.length === 1 ? '' : 's'}
                                </span>
                            </h2>
                            {items[0]?.evidence?.truncated && (
                                <p className="mt-1 text-xs text-amber-700">
                                    This call was long enough that Cubie read only the first part of
                                    the transcript.
                                </p>
                            )}
                            <div className="mt-3 space-y-3">
                                {items.map(s => (
                                    <SuggestionCard
                                        key={s.id}
                                        suggestion={s}
                                        portalId={portalId}
                                        busy={busyId === s.id}
                                        onApprove={edited => act(s.id, () => approveSuggestion(s.id, edited))}
                                        onDismiss={() => act(s.id, () => dismissSuggestion(s.id))}
                                    />
                                ))}
                            </div>
                        </section>
                    ))}
                </div>
            )}

            {handled.length > 0 && (
                <div className="mt-10">
                    <button
                        onClick={() => setShowHistory(v => !v)}
                        className="text-sm font-medium text-slate-500 hover:text-slate-700"
                    >
                        {showHistory ? 'Hide' : 'Show'} history ({handled.length})
                    </button>
                    {showHistory && (
                        <div className="mt-3 space-y-3">
                            {handled.map(s => (
                                <SuggestionCard
                                    key={s.id}
                                    suggestion={s}
                                    portalId={portalId}
                                    busy={false}
                                    onApprove={() => undefined}
                                    onDismiss={() => undefined}
                                />
                            ))}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
};

export default CrmSync;
