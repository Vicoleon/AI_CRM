import React, { useEffect, useMemo, useState } from 'react';
import {
    AlertCircle,
    AlertTriangle,
    BarChart3,
    CheckCircle2,
    Download,
    Edit2,
    Loader2,
    Plus,
    Trash2,
    Upload,
} from 'lucide-react';
import client from '../../api/client';
import { templateFilename } from '../../utils/download';
import { useVoiceChanges } from '../../components/voice/useVoiceChanges';
import {
    EmptyState,
    ErrorState,
    EosPageHeader,
    LoadingState,
    PageContainer,
} from './_shared';
import {
    avgOf,
    formatPerson,
    scopeToTeam,
    formatScorecardGoal,
    fromEditableScorecardValue,
    getPastWeeks,
    parseNumeric,
    scorecardSaveError,
    toEditableScorecardValue,
    weekKey,
    VALUE_TYPES,
    COMPARATORS,
    matchesTeamFilter,
    useTeamFilter,
} from './_utils';
import type { PersonLite, TeamScope, ScorecardValueType } from './_utils';
import { Modal, Field, inputCls, SubmitButton } from '../../components/eos/Modal';
import { ScorecardGrid } from '../../components/eos/ScorecardGrid';
import { toast } from '../../components/eos/Toast';

interface Metric {
    id: string;
    name: string;
    description: string | null;
    goal_description: string | null;
    target_value: string | null;
    unit: string | null;
    value_type: ScorecardValueType;
    comparator: string | null;
    // "weekly" (default) or "biweekly" — a team whose measurables are all
    // non-weekly gets a cadence-aware axis (see `teamWeeks` below).
    frequency: string | null;
    owner_id: string;
    department_name: string | null;
    is_exec: boolean;
    on_track: boolean;
    // Display position within the owner's block; null = not yet reordered, so
    // the grid falls back to alphabetical. Set by dragging (POST /metrics/reorder).
    sort_order: number | null;
}

interface Entry {
    id: string;
    metric_id: string;
    week_start_date: string;
    value: string | null;
    note: string | null;
}

/** The slice of an L10 meeting the scorecard needs: which weekday a team
 * meets, so a biweekly team's columns can be dated by its meeting day. */
interface L10Lite {
    department_name: string | null;
    is_exec_meeting: boolean;
    day_of_week: number | null; // 0=Mon .. 6=Sun (backend convention)
}

interface FormState {
    name: string;
    description: string;
    goal_description: string;
    target_value: string;
    value_type: ScorecardValueType;
    comparator: string;
    owner_id: string;
    team: string; // Meeting-scope value (department name or __exec__)
}

const EMPTY_FORM: FormState = {
    name: '',
    description: '',
    goal_description: '',
    target_value: '',
    value_type: 'number',
    comparator: '',
    owner_id: '',
    team: '',
};

// Allowed weeks-window sizes for the selector at the top of the page.
// 4 = the legacy "last month" view; 13 = a quarter (the EOS retro
// cadence); 26 = a half. Bigger windows are paginated by horizontal
// scroll so they don't break the page layout.
const WEEK_OPTIONS = [4, 13, 26, 52, 104, 156] as const;
type WeekWindow = (typeof WEEK_OPTIONS)[number];


interface UploadSummary {
    rows: number;
    metrics_created: number;
    metrics_created_names?: string[];
    metrics_updated: number;
    entries_created: number;
    entries_updated: number;
    warnings?: string[];
    ignored_columns?: string[];
}

/** The shared axios client isn't generically typed, so read a rejected
 * request's body defensively instead of casting the error. */
const errorBody = (err: unknown): unknown =>
    (err as { response?: { data?: unknown } } | null)?.response?.data;

/** An axios error body arrives as a Blob when the request asked for one
 * (the template download), so the server's JSON `detail` has to be read out
 * of it before it can be shown. */
const detailFromBlob = async (blob: unknown): Promise<string | null> => {
    if (!(blob instanceof Blob)) return null;
    try {
        const parsed = JSON.parse(await blob.text());
        return typeof parsed?.detail === 'string' ? parsed.detail : null;
    } catch {
        return null;
    }
};

/**
 * Bulk import for a team's weekly numbers — same recipe as the VTO uploader:
 * download an Excel template, fill it in, upload it back.
 *
 * The template is **blank** (header row, week columns, dropdowns), so the
 * download never carries scorecard content — see the endpoint's docstring for
 * why. Rows are matched to existing measurables by name + owner + team, so the
 * summary below names the measurables it *created*: that's how a typo shows up
 * as "added" instead of quietly becoming a near-duplicate row on the page.
 * Permissions are enforced server-side per row (the same rules that govern
 * editing a cell here); this panel only reports what came back.
 */
const ScorecardImporter: React.FC<{
    team: string;
    teamLabel: string;
    weeks: number;
    onImported: () => void;
}> = ({ team, teamLabel, weeks, onImported }) => {
    const [file, setFile] = useState<File | null>(null);
    const [uploading, setUploading] = useState(false);
    const [downloading, setDownloading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [rowErrors, setRowErrors] = useState<string[]>([]);
    const [summary, setSummary] = useState<UploadSummary | null>(null);

    const reset = () => {
        setError(null);
        setRowErrors([]);
        setSummary(null);
    };

    const downloadTemplate = async () => {
        setDownloading(true);
        reset();
        try {
            const res = await client.get('/api/eos/scorecard/template', {
                params: { ...(team ? { team } : {}), weeks },
                responseType: 'blob',
            });
            // Honour the filename the server chose (it names the team).
            const url = URL.createObjectURL(res.data as Blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = templateFilename(res.headers?.['content-disposition'], 'Scorecard');
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
        } catch (err: unknown) {
            setError(
                (await detailFromBlob(errorBody(err))) ??
                    'Could not download the template. Please try again.',
            );
        } finally {
            setDownloading(false);
        }
    };

    const upload = async () => {
        if (!file) return;
        setUploading(true);
        reset();
        try {
            const formData = new FormData();
            formData.append('file', file);
            const { data } = await client.post<UploadSummary>(
                '/api/eos/scorecard/upload',
                formData,
                { headers: { 'Content-Type': 'multipart/form-data' } },
            );
            setSummary(data);
            setFile(null);
            const input = document.getElementById('scorecard-file-input') as HTMLInputElement | null;
            if (input) input.value = '';
            onImported();
        } catch (err: unknown) {
            const detail = (errorBody(err) as { detail?: unknown } | undefined)?.detail;
            if (detail && typeof detail === 'object') {
                // Per-row rejection: nothing was imported, so list what to fix.
                const body = detail as { message?: string; errors?: unknown };
                setError(body.message ?? 'Nothing was imported.');
                setRowErrors(Array.isArray(body.errors) ? (body.errors as string[]) : []);
            } else {
                setError(
                    typeof detail === 'string'
                        ? detail
                        : 'Upload failed. Make sure you uploaded the filled-in .xlsx template.',
                );
            }
        } finally {
            setUploading(false);
        }
    };

    return (
        <div className="mb-5 bg-white border border-gray-200 rounded-lg shadow-sm p-4">
            <div className="flex items-start justify-between gap-4 flex-wrap">
                <div>
                    <h3 className="text-sm font-bold text-gray-800">
                        Import weekly numbers from Excel
                    </h3>
                    <p className="text-xs text-gray-500 mt-0.5 max-w-2xl">
                        The template is a <span className="font-medium">blank sheet</span> with one
                        column per week (the last {weeks}) and dropdowns for owner and team
                        {teamLabel ? `, generated for ${teamLabel}` : ''} — type a row per
                        measurable and upload it back. A measurable whose name, owner and team
                        already exist is updated in place; anything else is added. Blank cells are
                        left alone (an import never clears a value), and percents are typed as
                        whole percents.
                    </p>
                </div>
                <button
                    onClick={downloadTemplate}
                    disabled={downloading}
                    className="inline-flex items-center gap-1.5 text-sm font-medium text-brand hover:text-brand-hover disabled:opacity-50"
                >
                    {downloading ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
                    Download template
                </button>
            </div>

            <div className="mt-3 flex items-center gap-3 flex-wrap">
                <input
                    id="scorecard-file-input"
                    type="file"
                    accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    onChange={e => {
                        setFile(e.target.files?.[0] ?? null);
                        reset();
                    }}
                    className="block text-sm text-gray-600 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:text-sm file:font-medium file:bg-gray-100 file:text-gray-700 hover:file:bg-gray-200"
                />
                <button
                    onClick={upload}
                    disabled={!file || uploading}
                    className="inline-flex items-center gap-1.5 bg-brand text-white text-sm font-medium px-4 py-1.5 rounded-md hover:bg-brand-hover disabled:opacity-50 disabled:cursor-not-allowed"
                >
                    {uploading ? <Loader2 size={15} className="animate-spin" /> : <Upload size={15} />}
                    {uploading ? 'Importing…' : 'Upload & apply'}
                </button>
            </div>

            {summary && (
                <div className="mt-3 flex items-start gap-2 text-xs text-green-800 bg-green-50 border border-green-200 rounded-md px-3 py-2">
                    <CheckCircle2 size={14} className="flex-shrink-0 mt-0.5" />
                    <div className="space-y-1">
                        <div>
                            Imported {summary.rows} row{summary.rows === 1 ? '' : 's'} —{' '}
                            {summary.metrics_created} measurable
                            {summary.metrics_created === 1 ? '' : 's'} added,{' '}
                            {summary.metrics_updated} updated, {summary.entries_created} weekly value
                            {summary.entries_created === 1 ? '' : 's'} added,{' '}
                            {summary.entries_updated} changed.
                        </div>
                        {(summary.metrics_created_names ?? []).length > 0 && (
                            <div>
                                Added:{' '}
                                <span className="font-medium">
                                    {(summary.metrics_created_names ?? []).join(', ')}
                                </span>
                                . If one of those was meant to be an existing measurable, its
                                name/owner/team didn&apos;t match — delete it and re-upload with the
                                exact name.
                            </div>
                        )}
                        {(summary.warnings ?? []).map((w, i) => (
                            <div key={i} className="text-amber-800">
                                {w}
                            </div>
                        ))}
                        {(summary.ignored_columns ?? []).length > 0 && (
                            <div className="text-amber-800">
                                Ignored unrecognized column(s):{' '}
                                {(summary.ignored_columns ?? []).join(', ')}.
                            </div>
                        )}
                    </div>
                </div>
            )}
            {error && (
                <div className="mt-3 flex items-start gap-2 text-xs text-red-800 bg-red-50 border border-red-200 rounded-md px-3 py-2">
                    <AlertCircle size={14} className="flex-shrink-0 mt-0.5" />
                    <div className="space-y-1">
                        <div>{error}</div>
                        {rowErrors.length > 0 && (
                            <ul className="list-disc pl-4 space-y-0.5">
                                {rowErrors.map((e, i) => (
                                    <li key={i}>{e}</li>
                                ))}
                            </ul>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
};

export const Scorecard: React.FC = () => {
    const [metrics, setMetrics] = useState<Metric[]>([]);
    const [entries, setEntries] = useState<Entry[]>([]);
    const [people, setPeople] = useState<PersonLite[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const [modalOpen, setModalOpen] = useState(false);
    const [editing, setEditing] = useState<Metric | null>(null);
    const [form, setForm] = useState<FormState>(EMPTY_FORM);
    const [submitting, setSubmitting] = useState(false);
    const [teamScopes, setTeamScopes] = useState<TeamScope[]>([]);
    // Roster-scoped L10 list — only used to read each team's meeting weekday.
    const [meetings, setMeetings] = useState<L10Lite[]>([]);
    // Which team's scorecard is in view. Multi-team users (Exec /
    // admins / integrators managing >1 dept) get a picker so the Exec team
    // and each of their departments are each viewable; single-team users
    // never see it and just get their team's numbers.
    //
    // Opens on the caller's OWN department and remembers any change — see
    // `useTeamFilter`. This picker offers no "All my teams" entry, so the old
    // `''` default matched no <option>: the control rendered blank while the
    // grid showed every team's metrics at once. `useTeamFilter` can still
    // answer 'all' (a stored choice, or nobody flagged primary), so coerce
    // that to a real scope rather than back to the blank state.
    const [rawTeam, setSelectedTeam] = useTeamFilter(teamScopes, 'eos.teamFilter.scorecard');
    const selectedTeam =
        rawTeam !== 'all' ? rawTeam : (teamScopes[0]?.value ?? '');

    // Rolling window selector. Default to 13 weeks (a quarter) so
    // the page shows full retro-cycle history out of the box.
    const [weekWindow, setWeekWindow] = useState<WeekWindow>(13);
    const weeks = useMemo(() => getPastWeeks(weekWindow), [weekWindow]);

    // Excel import panel: collapsed by default (the grid is the main event),
    // and `refreshKey` re-runs the fetch below once an import lands.
    const [importOpen, setImportOpen] = useState(false);
    const [refreshKey, setRefreshKey] = useState(0);
    // …and a voice command that records a number here does the same.
    const voiceChanges = useVoiceChanges('/eos/scorecard');

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const [mRes, eRes, pRes, sRes, lRes] = await Promise.all([
                    client.get<Metric[]>('/api/eos/scorecard/metrics'),
                    // Pull enough history to fill the largest possible
                    // average window regardless of the current visible
                    // weekWindow — so switching windows doesn't trigger a
                    // refetch. 26 weeks (not 13) so a biweekly team's
                    // 13-period average also has real data behind it.
                    client.get<Entry[]>('/api/eos/scorecard/entries', {
                        params: { weeks: Math.max(weekWindow, 26) },
                    }),
                    client.get<PersonLite[]>('/api/eos/people/'),
                    client.get<{ scopes: TeamScope[] }>('/api/eos/me/meeting-scopes'),
                    // Meeting weekdays for the cadence-aware axis. Roster-scoped
                    // and merely cosmetic, so a failure must not sink the page.
                    client.get<L10Lite[]>('/api/eos/l10/').catch(() => ({ data: [] as L10Lite[] })),
                ]);
                if (cancelled) return;
                setMetrics(mRes.data);
                setEntries(eRes.data);
                setPeople(pRes.data);
                setMeetings(Array.isArray(lRes.data) ? lRes.data : []);
                const scopes = sRes.data.scopes ?? [];
                setTeamScopes(scopes);
                setError(null);
                // No defaulting here any more: `useTeamFilter` derives the
                // selection from `teamScopes` each render, so the old
                // "set it once the fetch lands" line is both redundant and
                // the set-state-in-effect the hook exists to avoid.
            } catch (err) {
                if (!cancelled) setError(err instanceof Error ? err.message : 'Unknown error');
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [weekWindow, refreshKey, voiceChanges]);

    const peopleById = useMemo(() => new Map(people.map(p => [p.id, p])), [people]);

    /** Lookup: metric_id → week date (YYYY-MM-DD) → { id, value, note }. */
    const entryIndex = useMemo(() => {
        type Cell = { id: string; value: string; note: string | null };
        const map = new Map<string, Map<string, Cell>>();
        for (const e of entries) {
            const inner = map.get(e.metric_id) ?? new Map<string, Cell>();
            // Key by the stored ISO date prefix (parse-free) so it matches the
            // column key `weekKey(weekDate)` regardless of local timezone.
            const key = e.week_start_date.slice(0, 10);
            inner.set(key, { id: e.id, value: e.value ?? '', note: e.note ?? null });
            map.set(e.metric_id, inner);
        }
        return map;
    }, [entries]);

    // When the user belongs to >1 team, scope the visible rows to the
    // selected team — same semantics as the L10 grid: the Exec team shows
    // is_exec metrics, a department shows that department's (non-exec) metrics.
    // Single-team users see every metric they're allowed to read (unchanged).
    const showTeamPicker = teamScopes.length > 1;
    const teamFiltered = useMemo(() => {
        if (!showTeamPicker || !selectedTeam) return metrics;
        // Slug-equal on the department (see `matchesTeamFilter`): the picker
        // offers one canonical spelling, the rows carry either case-variant.
        return metrics.filter(m => matchesTeamFilter(selectedTeam, m));
    }, [metrics, showTeamPicker, selectedTeam]);

    // The selected team's L10 weekday (0=Mon..6=Sun). The meetings list is
    // roster-scoped, so a viewer outside the team simply falls back to the
    // week-start (Sunday) column labels.
    const meetingDay = useMemo(() => {
        if (!selectedTeam) return null;
        const meeting = meetings.find(
            mt =>
                mt.day_of_week != null &&
                matchesTeamFilter(selectedTeam, {
                    department_name: mt.department_name,
                    is_exec: mt.is_exec_meeting,
                }),
        );
        return meeting?.day_of_week ?? null;
    }, [meetings, selectedTeam]);

    // Cadence-aware axis: a team whose measurables are ALL non-weekly (e.g.
    // a team on a biweekly L10) shows only the periods that actually hold data —
    // no alternating blank columns — plus the next period once its week
    // arrives. Weekly (or mixed) teams keep the classic full weekly axis.
    const biweeklyTeam =
        teamFiltered.length > 0 &&
        teamFiltered.every(m => (m.frequency ?? 'weekly') !== 'weekly');

    const teamWeeks = useMemo(() => {
        if (!biweeklyTeam) return weeks;
        const hasData = (w: Date) =>
            teamFiltered.some(m => entryIndex.get(m.id)?.has(weekKey(w)));
        const withData = weeks.filter(hasData);
        if (!withData.length) return weeks;
        // `weeks` runs newest → oldest, so withData[0] is the latest recorded
        // period. Surface the NEXT period once its week arrives so the team
        // can type the new numbers; until then, recorded periods only.
        const latest = withData[0];
        const next = new Date(latest.getFullYear(), latest.getMonth(), latest.getDate() + 14);
        const due = weeks.find(w => weekKey(w) === weekKey(next));
        return due ? [due, ...withData] : withData;
    }, [biweeklyTeam, weeks, teamFiltered, entryIndex]);

    // Label columns by the team's meeting day (Sunday start + 4 = Thursday).
    // Display only — cell keys and storage stay on the week's Sunday.
    const labelOffsetDays =
        biweeklyTeam && meetingDay != null ? (meetingDay + 1) % 7 : 0;

    // Group rows by owner so the table reads like the source spreadsheet —
    // each leader's measurables clustered, owner name shown once per block.
    // Within an owner's block the team's own drag order (`sort_order`) wins;
    // measurables never reordered fall back to alphabetical (nulls last), so a
    // block reads sensibly before anyone drags and exactly as set after.
    const orderedMetrics = useMemo(() => {
        const nameOf = (m: Metric) => formatPerson(peopleById.get(m.owner_id), '~');
        const posOf = (m: Metric) =>
            m.sort_order == null ? Number.MAX_SAFE_INTEGER : m.sort_order;
        return [...teamFiltered].sort(
            (a, b) =>
                nameOf(a).localeCompare(nameOf(b)) ||
                posOf(a) - posOf(b) ||
                a.name.localeCompare(b.name),
        );
    }, [teamFiltered, peopleById]);

    // What the target being typed will MEAN once stored — the same string the
    // Goal column and the L10 card read back. Percent targets are typed in
    // percent points, so this is where a factor-of-100 slip ("0.8" for 80%, or
    // a whole 8000) shows itself before it's saved. Empty when the target
    // can't colour anything (no comparator, or nothing typed).
    const targetPreview = useMemo(() => {
        const canonical = fromEditableScorecardValue(form.target_value, form.value_type);
        if (!canonical || !form.comparator) return '';
        return formatScorecardGoal({
            target_value: canonical,
            comparator: form.comparator,
            value_type: form.value_type,
        });
    }, [form.target_value, form.value_type, form.comparator]);

    const openNew = () => {
        setEditing(null);
        setForm({
            ...EMPTY_FORM,
            owner_id: people[0]?.id ?? '',
            // Pre-select the team currently in view so a new measurable lands
            // on the scorecard the user is looking at.
            team: selectedTeam || teamScopes[0]?.value || '',
        });
        setModalOpen(true);
    };

    const openEdit = (metric: Metric) => {
        setEditing(metric);
        setForm({
            name: metric.name,
            description: metric.description ?? '',
            goal_description: metric.goal_description ?? '',
            // Same units as the weekly cells: a percent target stored as
            // 0.0925 is edited as "9.25".
            target_value: toEditableScorecardValue(metric.target_value, metric.value_type),
            value_type: metric.value_type ?? 'number',
            comparator: metric.comparator ?? '',
            owner_id: metric.owner_id,
            team: '', // not editable after create
        });
        setModalOpen(true);
    };

    const closeModal = () => {
        setModalOpen(false);
        setEditing(null);
    };

    const onSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setSubmitting(true);
        try {
            const payload = {
                name: form.name,
                description: form.description || null,
                goal_description: form.goal_description || null,
                // Percent targets are typed in percent points ("50") and stored
                // as the canonical fraction ("0.5") so they compare directly
                // against the weekly values.
                target_value:
                    fromEditableScorecardValue(form.target_value, form.value_type) || null,
                value_type: form.value_type,
                comparator: form.comparator || null,
                owner_id: form.owner_id,
            };
            if (editing) {
                const res = await client.patch<Metric>(
                    `/api/eos/scorecard/metrics/${editing.id}`,
                    payload,
                );
                setMetrics(prev => prev.map(m => (m.id === editing.id ? res.data : m)));
            } else {
                // Tag the measurable with its team so it shows in that team's L10.
                const res = await client.post<Metric>('/api/eos/scorecard/metrics', {
                    ...payload,
                    ...scopeToTeam(form.team),
                });
                setMetrics(prev => [...prev, res.data]);
            }
            closeModal();
        } catch (err) {
            console.error('Failed to save metric', err);
            toast.error('Failed to save measurable.');
        } finally {
            setSubmitting(false);
        }
    };

    const onDelete = async (metric: Metric) => {
        if (!confirm(`Delete measurable: "${metric.name}"?`)) return;
        try {
            await client.delete(`/api/eos/scorecard/metrics/${metric.id}`);
            setMetrics(prev => prev.filter(m => m.id !== metric.id));
            // Also drop entries from local state so the table cells re-render.
            setEntries(prev => prev.filter(e => e.metric_id !== metric.id));
        } catch (err) {
            console.error('Delete failed', err);
            toast.error('Failed to delete measurable.');
        }
    };

    /**
     * Persist a drag-reorder of one owner's block. `orderedIds` is that owner's
     * measurables in their new order. Optimistic: the block reorders instantly,
     * and we revert to the pre-drag snapshot if the save fails.
     */
    const onReorder = async (orderedIds: string[]) => {
        const snapshot = metrics;
        const posById = new Map(orderedIds.map((id, i) => [id, i] as const));
        setMetrics(prev =>
            prev.map(m =>
                posById.has(m.id) ? { ...m, sort_order: posById.get(m.id)! } : m,
            ),
        );
        try {
            const res = await client.post<Metric[]>(
                '/api/eos/scorecard/metrics/reorder',
                { ordered_ids: orderedIds },
            );
            // Trust the server echo for the reordered block.
            const byId = new Map(res.data.map(m => [m.id, m] as const));
            setMetrics(prev => prev.map(m => byId.get(m.id) ?? m));
        } catch (err) {
            console.error('Reorder failed', err);
            toast.error('Failed to save the new order.');
            setMetrics(snapshot); // revert
        }
    };

    /**
     * Save a single weekly cell. POST if no entry yet exists for this week,
     * PATCH otherwise. The cell edits the raw stored value, so we persist it
     * verbatim (canonical: percent as a fraction, currency/number plain).
     */
    const saveEntry = async (metricId: string, weekDate: Date, newValue: string) => {
        const trimmed = newValue.trim();
        const existing = entryIndex.get(metricId)?.get(weekKey(weekDate));

        // Empty + nothing existing → noop.
        if (!trimmed && !existing) return;

        try {
            if (existing) {
                if (trimmed === existing.value) return; // unchanged
                const res = await client.patch<Entry>(
                    `/api/eos/scorecard/entries/${existing.id}`,
                    { value: trimmed || null },
                );
                setEntries(prev => prev.map(e => (e.id === existing.id ? res.data : e)));
            } else {
                const res = await client.post<Entry>('/api/eos/scorecard/entries', {
                    metric_id: metricId,
                    week_start_date: weekDate.toISOString(),
                    value: trimmed,
                });
                setEntries(prev => [...prev, res.data]);
            }
        } catch (err) {
            console.error('Failed to save entry', err);
            toast.error(scorecardSaveError(err, 'Failed to save weekly value.'));
        }
    };

    /**
     * Save a cell's note — what the number is made of ("3 renewals, 2
     * upsells"), the spreadsheet cell comment this replaces. Uses the same
     * POST/PATCH split as the value, and `upsert` is deliberately NOT used:
     * the page always knows the entry id when one exists.
     *
     * A note on a week with no value creates the entry (value null) so the
     * annotation isn't lost while someone waits on the number.
     */
    const saveNote = async (metricId: string, weekDate: Date, note: string) => {
        const trimmed = note.trim();
        const existing = entryIndex.get(metricId)?.get(weekKey(weekDate));
        if (!trimmed && !existing) return;
        try {
            if (existing) {
                if (trimmed === (existing.note ?? '')) return; // unchanged
                const res = await client.patch<Entry>(
                    `/api/eos/scorecard/entries/${existing.id}`,
                    { note: trimmed || null },
                );
                setEntries(prev => prev.map(e => (e.id === existing.id ? res.data : e)));
            } else {
                const res = await client.post<Entry>('/api/eos/scorecard/entries', {
                    metric_id: metricId,
                    week_start_date: weekDate.toISOString(),
                    value: null,
                    note: trimmed,
                });
                setEntries(prev => [...prev, res.data]);
            }
        } catch (err) {
            console.error('Failed to save note', err);
            toast.error(scorecardSaveError(err, 'Failed to save the cell note.'));
        }
    };

    return (
        // `wide`: the grid is the page, and it is far wider than the prose cap.
        <PageContainer wide>
            <EosPageHeader
                icon={<BarChart3 size={20} />}
                title="Scorecard"
                subtitle="Weekly high-level numbers"
                right={
                    <div className="flex items-center gap-2">
                        {/* Team selector — Exec-team members and
                            multi-department managers switch between the Exec
                            team scorecard and their department(s). */}
                        {showTeamPicker && (
                            <label className="text-xs text-gray-500 flex items-center gap-2">
                                Team
                                <select
                                    value={selectedTeam}
                                    onChange={e => setSelectedTeam(e.target.value)}
                                    className="border border-gray-300 rounded-md px-2 py-1 text-sm bg-white"
                                >
                                    {teamScopes.map(s => (
                                        <option key={s.value} value={s.value}>
                                            {s.label}
                                        </option>
                                    ))}
                                </select>
                            </label>
                        )}
                        {/* Rolling-window selector. The Last column
                            adapts to whichever window is selected. */}
                        <label className="text-xs text-gray-500 flex items-center gap-2">
                            Show last
                            <select
                                value={weekWindow}
                                onChange={e => setWeekWindow(Number(e.target.value) as WeekWindow)}
                                className="border border-gray-300 rounded-md px-2 py-1 text-sm bg-white"
                            >
                                {WEEK_OPTIONS.map(n => (
                                    <option key={n} value={n}>
                                        {n} weeks
                                    </option>
                                ))}
                            </select>
                        </label>
                        <button
                            type="button"
                            onClick={() => setImportOpen(o => !o)}
                            aria-expanded={importOpen}
                            data-tour="eos-scorecard-import"
                            className={`flex items-center gap-2 px-3 py-2 rounded-md text-sm font-medium border transition-colors ${
                                importOpen
                                    ? 'border-brand text-brand bg-brand/5'
                                    : 'border-gray-300 text-gray-700 hover:bg-gray-50'
                            }`}
                        >
                            <Upload size={16} /> Import
                        </button>
                        <button
                            type="button"
                            onClick={openNew}
                            className="flex items-center gap-2 px-3 py-2 bg-brand hover:bg-brand-hover text-white rounded-md text-sm font-medium transition-colors"
                            data-tour="eos-scorecard-add"
                        >
                            <Plus size={16} /> Add Measurable
                        </button>
                    </div>
                }
            />

            {importOpen && (
                <ScorecardImporter
                    team={selectedTeam}
                    teamLabel={
                        teamScopes.find(s => s.value === selectedTeam)?.label ?? ''
                    }
                    weeks={weekWindow}
                    onImported={() => setRefreshKey(k => k + 1)}
                />
            )}

            {loading && <LoadingState label="Loading scorecard…" />}
            {error && <ErrorState message={error} />}

            {!loading && !error && orderedMetrics.length === 0 && (
                <EmptyState
                    title={
                        showTeamPicker && metrics.length > 0
                            ? 'No measurables for this team yet.'
                            : 'No measurables defined yet.'
                    }
                    hint='Click "Add Measurable" to track your first weekly number.'
                />
            )}

            {!loading && !error && orderedMetrics.length > 0 && (
                <ScorecardGrid
                    metrics={orderedMetrics}
                    weeks={teamWeeks}
                    labelOffsetDays={labelOffsetDays}
                    peopleById={peopleById}
                    valueAt={(metricId, week) =>
                        entryIndex.get(metricId)?.get(week)?.value ?? ''
                    }
                    noteAt={(metricId, week) =>
                        entryIndex.get(metricId)?.get(week)?.note ?? null
                    }
                    // Weekly teams walk 13 calendar weeks (deliberately wider
                    // than a 4-week view). A biweekly team averages its last
                    // recorded PERIODS instead, so its "4w avg" reflects its
                    // last 4 meetings — not 2 meetings and 2 blanks.
                    averagesFor={metricId => {
                        const row = entryIndex.get(metricId);
                        const axisKeys = biweeklyTeam
                            ? [...(row?.keys() ?? [])].sort().reverse().slice(0, 13)
                            : getPastWeeks(13).map(w => weekKey(w));
                        const numericByWeek = axisKeys.map(k =>
                            parseNumeric(row?.get(k)?.value ?? null),
                        );
                        return {
                            avg4: avgOf(numericByWeek.slice(0, 4)),
                            avg13: avgOf(numericByWeek),
                        };
                    }}
                    onSaveCell={saveEntry}
                    onSaveNote={saveNote}
                    onReorder={onReorder}
                    rowActions={m => (
                        <div className="flex justify-end gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                            <button
                                type="button"
                                onClick={() => openEdit(m as Metric)}
                                className="p-1.5 text-gray-400 hover:text-gray-700 hover:bg-gray-100 rounded"
                                aria-label="Edit measurable"
                            >
                                <Edit2 size={14} />
                            </button>
                            <button
                                type="button"
                                onClick={() => onDelete(m as Metric)}
                                className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded"
                                aria-label="Delete measurable"
                            >
                                <Trash2 size={14} />
                            </button>
                        </div>
                    )}
                />
            )}

            <Modal
                isOpen={modalOpen}
                onClose={closeModal}
                title={editing ? 'Edit Measurable' : 'New Measurable'}
            >
                <form onSubmit={onSubmit} className="space-y-4">
                    <Field label="Measurable name">
                        <input
                            required
                            type="text"
                            value={form.name}
                            onChange={e => setForm({ ...form, name: e.target.value })}
                            className={inputCls}
                            autoFocus
                            placeholder='e.g. "Weekly Closed Deals"'
                        />
                    </Field>
                    <Field label="Description (shown on hover in the grid)">
                        <textarea
                            value={form.description}
                            onChange={e => setForm({ ...form, description: e.target.value })}
                            className={inputCls}
                            rows={3}
                            placeholder="What this measurable tracks and how it's counted"
                        />
                    </Field>
                    <Field label="Goal (shown in the Goal column)">
                        <input
                            type="text"
                            value={form.goal_description}
                            onChange={e => setForm({ ...form, goal_description: e.target.value })}
                            className={inputCls}
                            placeholder='e.g. "> 33,000 usd"'
                        />
                    </Field>
                    <div className="grid grid-cols-3 gap-3">
                        <Field label="Value type">
                            <select
                                value={form.value_type}
                                onChange={e =>
                                    setForm({ ...form, value_type: e.target.value as ScorecardValueType })
                                }
                                className={inputCls}
                            >
                                {VALUE_TYPES.map(vt => (
                                    <option key={vt.value} value={vt.value}>
                                        {vt.label}
                                    </option>
                                ))}
                            </select>
                        </Field>
                        <Field label="Compare">
                            <select
                                value={form.comparator}
                                onChange={e => setForm({ ...form, comparator: e.target.value })}
                                className={inputCls}
                            >
                                <option value="">— none —</option>
                                {COMPARATORS.map(c => (
                                    <option key={c} value={c}>
                                        {c}
                                    </option>
                                ))}
                            </select>
                        </Field>
                        <Field
                            label={
                                form.value_type === 'percent'
                                    ? 'Target % (for colour)'
                                    : 'Target (for colour)'
                            }
                        >
                            <input
                                type="text"
                                value={form.target_value}
                                onChange={e => setForm({ ...form, target_value: e.target.value })}
                                className={inputCls}
                                placeholder={form.value_type === 'percent' ? '98' : '33000'}
                            />
                        </Field>
                    </div>
                    {form.value_type === 'percent' && (
                        <p className="text-xs text-gray-400 -mt-2">
                            Enter percents as whole numbers — type <span className="font-mono">98</span> for
                            98%, <span className="font-mono">9.25</span> for 9.25%. (Stored as a fraction, so{' '}
                            <span className="font-mono">0.5</span> means half a percent.)
                        </p>
                    )}
                    {/* What the target will MEAN, in the units the grid and the
                        L10 read it back in. A percent target typed as a
                        fraction ("0.8" for 80%) is a factor-of-100 mistake that
                        used to be invisible until it showed up in a meeting as
                        "8,000%" — here it's visible while you type. */}
                    {targetPreview ? (
                        <p className="text-xs text-gray-500 -mt-2">
                            Colours a week green when it&apos;s{' '}
                            <span className="font-medium text-gray-700">{targetPreview}</span>.
                        </p>
                    ) : (
                        /* The counterpart warning. Colouring needs BOTH halves,
                           but only the Goal above appears in the grid — so a
                           measurable could read as having a goal and colour
                           nothing, with the form saying nothing about it. That
                           is how the Exec scorecard ended up entirely grey. */
                        <p className="text-xs text-amber-600 -mt-2 flex items-start gap-1">
                            <AlertTriangle size={13} className="shrink-0 mt-0.5" />
                            <span>
                                Weeks stay grey until this measurable has both a Compare and
                                a Target — the Goal above is only the label shown in the
                                column. If the Goal reads as a goal on its own (
                                <span className="font-mono">&gt; 98%</span>,{' '}
                                <span className="font-mono">&lt;10,000 usd</span>), we&apos;ll
                                fill the missing half in from it on save.
                            </span>
                        </p>
                    )}
                    <Field label="Owner">
                        <select
                            required
                            value={form.owner_id}
                            onChange={e => setForm({ ...form, owner_id: e.target.value })}
                            className={inputCls}
                        >
                            <option value="" disabled>Select owner…</option>
                            {people.map(p => (
                                <option key={p.id} value={p.id}>
                                    {p.first_name} {p.last_name}
                                </option>
                            ))}
                        </select>
                    </Field>
                    {/* Which team's L10 this measurable appears in. */}
                    {!editing && teamScopes.length > 1 && (
                        <Field label="Team (shows in this team's L10)">
                            <select
                                value={form.team}
                                onChange={e => setForm({ ...form, team: e.target.value })}
                                className={inputCls}
                            >
                                {teamScopes.map(s => (
                                    <option key={s.value} value={s.value}>
                                        {s.label}
                                    </option>
                                ))}
                            </select>
                        </Field>
                    )}
                    <SubmitButton disabled={submitting} onCancel={closeModal}>
                        {editing ? 'Save changes' : 'Create measurable'}
                    </SubmitButton>
                </form>
            </Modal>
        </PageContainer>
    );
};
