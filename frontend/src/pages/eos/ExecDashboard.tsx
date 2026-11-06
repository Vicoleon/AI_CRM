/**
 * Executive EOS Dashboard.
 *
 * Bloom Growth–style insights view:
 *   - 4 KPI cards (overall health, meeting rating, rock health, todo %).
 *   - Per-team rollup table (drill-in scopes the view to that dept).
 *   - 3 weekly time-series charts (meeting health, rock status,
 *     issue trend).
 *
 * Single round-trip to ``/api/eos/dashboard/executive`` — the backend
 * pre-aggregates everything plus the available filter options so this
 * component never fans out.
 *
 * Gated server-side AND in this component on ``isAdmin || isExec``;
 * non-exec users see the explanatory empty-state instead of a 403.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
    BarChart3,
    RefreshCw,
    Target,
    AlertCircle,
    ListTodo,
    TrendingUp,
    TrendingDown,
    Minus,
    Activity,
} from 'lucide-react';
import { Bar, Line } from 'react-chartjs-2';
import type { TooltipItem } from 'chart.js';

import client from '../../api/client';
import { useAuth } from '../../context/AuthContext';
import {
    EosPageHeader,
    ErrorState,
    LoadingState,
    PageContainer,
} from './_shared';
import { EXEC_SCOPE_LABEL } from './_utils';
import { CHART_OPTIONS_BASE } from '../../components/insights/chartTheme';
import { getBrandColor } from '../../brand/colors';

// Chart.js registration and the shared chart chrome live in one module, so
// this page and the Insights dashboards draw alike and register once.

// ── Types matching backend response ────────────────────────────────────

interface QuarterOption {
    id: string;
    name: string;
    start_date: string;
    end_date: string;
    is_current: boolean;
}

interface KPIDelta {
    delta: number | null;
}

interface MeetingKPI extends KPIDelta {
    avg: number | null;
    target: number;
    samples: number;
}
interface RockKPI extends KPIDelta {
    pct: number | null;
    label: string;
    done: number;
    not_done: number;
    on_track: number;
    off_track: number;
    total: number;
}
interface TodoKPI extends KPIDelta {
    pct: number | null;
    target: number;
    done: number;
    total: number;
}

interface TeamRow {
    team_name: string;
    initials: string;
    color_seed: number;
    health_score: number;
    meeting_rating: number | null;
    rock_health_pct: number | null;
    todo_completion_pct: number | null;
    avg_issue_age_days: number | null;
    trend: 'up' | 'down' | 'flat';
}

interface MeetingHealthPoint {
    week_start: string;
    label: string;
    avg_rating: number | null;
    samples: number;
}
interface RockStatusPoint {
    week_start: string;
    label: string;
    on_track: number;
    off_track: number;
    done: number;
}
interface IssueTrendPoint {
    week_start: string;
    label: string;
    opened: number;
}
interface IssueSolvedPoint {
    week_start: string;
    label: string;
    solved: number;
    load: number;
    pct: number | null;
}

interface DashboardResponse {
    scope: { kind: 'company' | 'department'; department_name: string | null };
    period: {
        id: string;
        name: string;
        start_date: string;
        end_date: string;
        is_current: boolean;
        prev_id: string | null;
        available_quarters: QuarterOption[];
    };
    available_departments: string[];
    health_summary: {
        overall_score: number;
        overall_label: string;
        meeting_rating: MeetingKPI;
        rock_health: RockKPI;
        todo_completion: TodoKPI;
    };
    team_performance: TeamRow[];
    trends: {
        meeting_health_over_time: MeetingHealthPoint[];
        rock_status_over_time: RockStatusPoint[];
        issue_trend: IssueTrendPoint[];
        issue_solved_rate: IssueSolvedPoint[];
    };
}

// ── Visual helpers ─────────────────────────────────────────────────────

// Tailwind palette for the team initials chip. Backend hands us a 0..7
// color_seed derived from the team name so the chip color is stable. These are
// categorical hues, not the brand, so they stay fixed under a workspace accent.
const TEAM_PALETTE = [
    'bg-purple-100 text-purple-700',
    'bg-rose-100 text-rose-700',
    'bg-blue-100 text-blue-700',
    'bg-emerald-100 text-emerald-700',
    'bg-amber-100 text-amber-700',
    'bg-indigo-100 text-indigo-700',
    'bg-pink-100 text-pink-700',
    'bg-teal-100 text-teal-700',
];

const HEALTH_TONE: Record<string, { text: string; bg: string; ring: string }> = {
    Healthy: { text: 'text-emerald-700', bg: 'bg-emerald-50', ring: 'ring-emerald-200' },
    Watch: { text: 'text-amber-700', bg: 'bg-amber-50', ring: 'ring-amber-200' },
    'Needs attention': { text: 'text-red-700', bg: 'bg-red-50', ring: 'ring-red-200' },
    '—': { text: 'text-gray-600', bg: 'bg-gray-50', ring: 'ring-gray-200' },
};

const SCORE_TONE = (score: number): { text: string; pill: string } => {
    if (score >= 80) return { text: 'text-emerald-700', pill: 'bg-emerald-100 text-emerald-700' };
    if (score >= 60) return { text: 'text-amber-700', pill: 'bg-amber-100 text-amber-700' };
    return { text: 'text-red-700', pill: 'bg-red-100 text-red-700' };
};

const DeltaChip: React.FC<{ delta: number | null; unit?: string; invert?: boolean }> = ({
    delta,
    unit = '',
    invert = false,
}) => {
    if (delta == null) {
        return (
            <span className="inline-flex items-center gap-1 text-xs text-gray-500">
                <Minus size={12} /> No change
            </span>
        );
    }
    // `invert=true` for metrics where DOWN is good (e.g. issue age).
    const positive = invert ? delta < 0 : delta > 0;
    const negative = invert ? delta > 0 : delta < 0;
    const sign = delta > 0 ? '+' : '';
    return (
        <span
            className={`inline-flex items-center gap-1 text-xs font-medium ${
                positive ? 'text-emerald-600' : negative ? 'text-red-600' : 'text-gray-500'
            }`}
        >
            {positive ? <TrendingUp size={12} /> : negative ? <TrendingDown size={12} /> : <Minus size={12} />}
            {sign}
            {delta}
            {unit} vs last period
        </span>
    );
};

const TrendIcon: React.FC<{ trend: TeamRow['trend'] }> = ({ trend }) => {
    if (trend === 'up') return <TrendingUp size={16} className="text-emerald-600" />;
    if (trend === 'down') return <TrendingDown size={16} className="text-red-600" />;
    return <Minus size={16} className="text-gray-400" />;
};

// ── KPI Cards ───────────────────────────────────────────────────────────

const OverallHealthCard: React.FC<{
    score: number;
    label: string;
    meeting: MeetingKPI;
    rocks: RockKPI;
    todos: TodoKPI;
}> = ({ score, label, meeting, rocks, todos }) => {
    const tone = SCORE_TONE(score);
    // Stacked bar segments scaled to a 0..100 axis.
    const meetingSeg = meeting.avg != null ? (meeting.avg / 10) * 100 : 0;
    const rocksSeg = rocks.pct ?? 0;
    const todosSeg = todos.pct ?? 0;
    return (
        <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
            <div className="flex items-center justify-between mb-2">
                <p className="text-[10px] uppercase tracking-wider text-gray-500 font-semibold">
                    Overall Health
                </p>
                <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ${tone.pill}`}>
                    {label}
                </span>
            </div>
            <p className="text-xs text-gray-500 mb-3">Execution Health Index</p>
            <div className="flex items-baseline gap-2">
                <span className={`text-4xl font-bold tabular-nums ${tone.text}`}>{score}</span>
                <span className="text-sm text-gray-400">/ 100</span>
            </div>
            <div className="mt-4 flex gap-0.5 h-1.5 rounded-full overflow-hidden bg-gray-100">
                <div className="bg-blue-400" style={{ width: `${meetingSeg / 3}%` }} title={`Meetings ${meetingSeg.toFixed(0)}%`} />
                <div className="bg-emerald-400" style={{ width: `${rocksSeg / 3}%` }} title={`Rocks ${rocksSeg}%`} />
                <div className="bg-amber-400" style={{ width: `${todosSeg / 3}%` }} title={`To-dos ${todosSeg}%`} />
            </div>
            <div className="mt-2 flex justify-between text-[10px] text-gray-500">
                <span><span className="inline-block w-2 h-2 rounded-full bg-blue-400 mr-1" />Meetings</span>
                <span><span className="inline-block w-2 h-2 rounded-full bg-emerald-400 mr-1" />Rocks</span>
                <span><span className="inline-block w-2 h-2 rounded-full bg-amber-400 mr-1" />To-dos</span>
            </div>
        </div>
    );
};

const MeetingRatingCard: React.FC<{ data: MeetingKPI }> = ({ data }) => (
    <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
        <p className="text-[10px] uppercase tracking-wider text-gray-500 font-semibold mb-2">
            Avg Meeting Rating
        </p>
        <div className="flex items-baseline gap-2">
            <span className="text-4xl font-bold tabular-nums text-gray-900">
                {data.avg ?? '—'}
            </span>
            <span className="text-sm text-gray-400">/ 10</span>
            <span className="text-xs text-gray-500 ml-2">
                Target = {data.target}
            </span>
        </div>
        <p className="text-[10px] text-gray-400 mt-1">
            {data.samples} session{data.samples === 1 ? '' : 's'} this period
        </p>
        <div className="mt-3">
            <DeltaChip delta={data.delta} />
        </div>
    </div>
);

const RockHealthCard: React.FC<{ data: RockKPI }> = ({ data }) => {
    const tone = HEALTH_TONE[data.label] ?? HEALTH_TONE['—'];
    return (
        <div className={`bg-white rounded-lg border border-gray-200 shadow-sm p-4 ring-1 ${tone.ring}`}>
            <div className="flex items-center justify-between mb-2">
                <p className="text-[10px] uppercase tracking-wider text-gray-500 font-semibold">
                    Rock Health
                </p>
                <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ${tone.bg} ${tone.text}`}>
                    {data.label}
                </span>
            </div>
            <div className="flex items-baseline gap-2">
                <span className={`text-4xl font-bold tabular-nums ${tone.text}`}>
                    {data.pct ?? '—'}
                </span>
                {data.pct !== null && <span className="text-sm text-gray-400">%</span>}
            </div>
            <div className="mt-3 flex gap-0.5 h-2 rounded-full overflow-hidden bg-red-100">
                <div className="bg-emerald-500 h-full" style={{ width: `${data.pct ?? 0}%` }} />
            </div>
            <div className="mt-1.5 flex justify-between text-[10px] text-gray-500">
                <span>
                    <span className="inline-block w-2 h-2 rounded-full bg-emerald-500 mr-1" />
                    {data.done + data.on_track} on/done
                </span>
                <span>
                    <span className="inline-block w-2 h-2 rounded-full bg-red-400 mr-1" />
                    {data.off_track} off track
                </span>
            </div>
            <div className="mt-2">
                <DeltaChip delta={data.delta} unit=" pts" />
            </div>
        </div>
    );
};

const TodoCompletionCard: React.FC<{ data: TodoKPI }> = ({ data }) => (
    <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
        <p className="text-[10px] uppercase tracking-wider text-gray-500 font-semibold mb-2">
            To-Do Completion Rate
        </p>
        <div className="flex items-baseline gap-2">
            <span className="text-4xl font-bold tabular-nums text-gray-900">
                {data.pct ?? '—'}
            </span>
            {data.pct !== null && <span className="text-sm text-gray-400">%</span>}
            <span className="text-xs text-gray-500 ml-2">Target = {data.target}%</span>
        </div>
        <p className="text-[10px] text-gray-400 mt-1">
            {data.done} of {data.total} done
        </p>
        <div className="mt-3">
            <DeltaChip delta={data.delta} unit=" pts" />
        </div>
    </div>
);

// ── Team Performance Table ─────────────────────────────────────────────

const TeamPerformanceTable: React.FC<{
    rows: TeamRow[];
    onDrillIn: (team: string) => void;
}> = ({ rows, onDrillIn }) => {
    if (rows.length === 0) {
        return (
            <section className="bg-white rounded-lg border border-gray-200 shadow-sm p-6 text-sm text-gray-500 italic">
                No teams have EOS activity in this period.
            </section>
        );
    }
    return (
        <section className="bg-white rounded-lg border border-gray-200 shadow-sm" data-tour="eos-dashboard-teams">
            <header className="px-5 py-3 border-b border-gray-100 flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <Activity size={16} className="text-brand" />
                    <h3 className="text-sm font-semibold text-gray-900">Team performance</h3>
                </div>
                <span className="text-xs text-gray-400 italic">Click a row to drill in</span>
            </header>
            <div className="overflow-x-auto">
                <table className="w-full text-sm">
                    <thead className="bg-gray-50 text-[10px] uppercase tracking-wider text-gray-500">
                        <tr>
                            <th className="px-5 py-2 text-left font-semibold">Team</th>
                            <th className="px-3 py-2 text-center font-semibold">Health Score</th>
                            <th className="px-3 py-2 text-center font-semibold">Meeting Rating</th>
                            <th className="px-3 py-2 text-left font-semibold">Rock Health</th>
                            <th className="px-3 py-2 text-left font-semibold">To-do Completion</th>
                            <th className="px-3 py-2 text-right font-semibold">Avg Issue Age</th>
                            <th className="px-3 py-2 text-center font-semibold">Trend</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                        {rows.map(r => {
                            const tone = SCORE_TONE(r.health_score);
                            const paletteCls = TEAM_PALETTE[r.color_seed % TEAM_PALETTE.length];
                            return (
                                <tr
                                    key={r.team_name}
                                    onClick={() => onDrillIn(r.team_name)}
                                    className="cursor-pointer hover:bg-gray-50"
                                >
                                    <td className="px-5 py-3">
                                        <div className="flex items-center gap-2.5">
                                            <span
                                                className={`shrink-0 w-7 h-7 rounded-md text-[10px] font-bold flex items-center justify-center ${paletteCls}`}
                                            >
                                                {r.initials}
                                            </span>
                                            <span className="font-medium text-gray-900">
                                                {r.team_name}
                                            </span>
                                        </div>
                                    </td>
                                    <td className="px-3 py-3 text-center">
                                        <span className={`inline-flex items-center justify-center min-w-[2.5rem] px-2 py-0.5 rounded-full text-xs font-semibold ${tone.pill}`}>
                                            {r.health_score}
                                        </span>
                                    </td>
                                    <td className="px-3 py-3 text-center tabular-nums">
                                        {r.meeting_rating ?? '—'}
                                    </td>
                                    <td className="px-3 py-3">
                                        {r.rock_health_pct == null ? (
                                            <span className="text-xs text-gray-400">—</span>
                                        ) : (
                                            <div className="flex items-center gap-2">
                                                <div className="flex-1 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                                                    <div
                                                        className={`h-full ${r.rock_health_pct >= 80 ? 'bg-emerald-500' : r.rock_health_pct >= 60 ? 'bg-amber-500' : 'bg-red-500'}`}
                                                        style={{ width: `${r.rock_health_pct}%` }}
                                                    />
                                                </div>
                                                <span className="text-xs tabular-nums w-10 text-right">
                                                    {r.rock_health_pct}%
                                                </span>
                                            </div>
                                        )}
                                    </td>
                                    <td className="px-3 py-3">
                                        {r.todo_completion_pct == null ? (
                                            <span className="text-xs text-gray-400">—</span>
                                        ) : (
                                            <div className="flex items-center gap-2">
                                                <div className="flex-1 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                                                    <div
                                                        className="h-full bg-amber-400"
                                                        style={{ width: `${r.todo_completion_pct}%` }}
                                                    />
                                                </div>
                                                <span className="text-xs tabular-nums w-10 text-right">
                                                    {r.todo_completion_pct}%
                                                </span>
                                            </div>
                                        )}
                                    </td>
                                    <td className="px-3 py-3 text-right tabular-nums text-xs text-gray-600">
                                        {r.avg_issue_age_days != null ? `${r.avg_issue_age_days} days` : '—'}
                                    </td>
                                    <td className="px-3 py-3 text-center">
                                        <TrendIcon trend={r.trend} />
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
        </section>
    );
};

// ── Charts ─────────────────────────────────────────────────────────────
// The shared base options (CHART_OPTIONS_BASE) come from
// components/insights/chartTheme.ts.

const MeetingHealthChart: React.FC<{ points: MeetingHealthPoint[] }> = ({ points }) => {
    const data = {
        labels: points.map(p => p.label),
        datasets: [
            {
                label: 'Avg rating',
                data: points.map(p => p.avg_rating),
                borderColor: getBrandColor('brand'),
                backgroundColor: getBrandColor('brand', 0.08),
                tension: 0.35,
                fill: true,
                spanGaps: true,
                pointRadius: 3,
                pointHoverRadius: 5,
            },
        ],
    };
    return (
        <div className="h-48">
            <Line
                data={data}
                options={{
                    ...CHART_OPTIONS_BASE,
                    scales: {
                        ...CHART_OPTIONS_BASE.scales,
                        y: { ...CHART_OPTIONS_BASE.scales.y, suggestedMin: 4, suggestedMax: 10 },
                    },
                }}
            />
        </div>
    );
};

const RockStatusChart: React.FC<{ points: RockStatusPoint[] }> = ({ points }) => {
    const data = {
        labels: points.map(p => p.label),
        datasets: [
            {
                label: 'On track',
                data: points.map(p => p.on_track),
                backgroundColor: '#10b981',
                stack: 'rocks',
            },
            {
                label: 'Done',
                data: points.map(p => p.done),
                backgroundColor: '#3b82f6',
                stack: 'rocks',
            },
            {
                label: 'Off track',
                data: points.map(p => p.off_track),
                backgroundColor: '#ef4444',
                stack: 'rocks',
            },
        ],
    };
    return (
        <div className="h-48">
            <Bar
                data={data}
                options={{
                    ...CHART_OPTIONS_BASE,
                    plugins: {
                        ...CHART_OPTIONS_BASE.plugins,
                        legend: { display: true, position: 'bottom', labels: { font: { size: 10 }, boxWidth: 10 } },
                    },
                    scales: {
                        ...CHART_OPTIONS_BASE.scales,
                        x: { ...CHART_OPTIONS_BASE.scales.x, stacked: true },
                        y: { ...CHART_OPTIONS_BASE.scales.y, stacked: true, ticks: { ...CHART_OPTIONS_BASE.scales.y.ticks, stepSize: 1 } },
                    },
                }}
            />
        </div>
    );
};

const IssueTrendChart: React.FC<{ points: IssueTrendPoint[] }> = ({ points }) => {
    const data = {
        labels: points.map(p => p.label),
        datasets: [
            {
                label: 'Issues opened',
                data: points.map(p => p.opened),
                backgroundColor: '#ef4444',
            },
        ],
    };
    return (
        <div className="h-48">
            <Bar
                data={data}
                options={{
                    ...CHART_OPTIONS_BASE,
                    scales: {
                        ...CHART_OPTIONS_BASE.scales,
                        y: { ...CHART_OPTIONS_BASE.scales.y, ticks: { ...CHART_OPTIONS_BASE.scales.y.ticks, stepSize: 1 } },
                    },
                }}
            />
        </div>
    );
};

const IssueSolvedChart: React.FC<{ points: IssueSolvedPoint[] }> = ({ points }) => {
    const data = {
        labels: points.map(p => p.label),
        datasets: [
            {
                label: '% solved',
                data: points.map(p => p.pct),
                borderColor: getBrandColor('brand'),
                backgroundColor: getBrandColor('brand', 0.08),
                tension: 0.35,
                fill: true,
                spanGaps: false,
                pointRadius: 3,
                pointHoverRadius: 5,
            },
        ],
    };
    return (
        <div className="h-48">
            <Line
                data={data}
                options={{
                    ...CHART_OPTIONS_BASE,
                    plugins: {
                        ...CHART_OPTIONS_BASE.plugins,
                        tooltip: {
                            ...CHART_OPTIONS_BASE.plugins.tooltip,
                            callbacks: {
                                label: (ctx: TooltipItem<'line'>) => {
                                    const p = points[ctx.dataIndex];
                                    if (!p || p.pct == null) return 'No open issues that week';
                                    return `${p.solved} of ${p.load} solved (${p.pct}%)`;
                                },
                            },
                        },
                    },
                    scales: {
                        ...CHART_OPTIONS_BASE.scales,
                        y: {
                            ...CHART_OPTIONS_BASE.scales.y,
                            min: 0,
                            max: 100,
                            ticks: {
                                ...CHART_OPTIONS_BASE.scales.y.ticks,
                                callback: (value: number | string) => `${value}%`,
                            },
                        },
                    },
                }}
            />
        </div>
    );
};

// ── Main page ──────────────────────────────────────────────────────────

export const ExecDashboard: React.FC = () => {
    const { isAdmin, isExec } = useAuth();
    const allowed = isAdmin || isExec;

    const [data, setData] = useState<DashboardResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    // Filter state. Empty string = the unnarrowed rollup / current quarter
    // (server default). The picker labels that default EXEC_SCOPE_LABEL —
    // this page is exec-gated, so it reads as the executive's view rather
    // than something everyone gets — but the request is unchanged: no
    // department_name param, and the server still rolls up every department.
    const [scopeDept, setScopeDept] = useState<string>('');
    const [quarterId, setQuarterId] = useState<string>('');

    const fetchData = async (overrideDept?: string, overrideQuarter?: string) => {
        try {
            setError(null);
            setLoading(true);
            const params: Record<string, string> = {};
            const dept = overrideDept ?? scopeDept;
            const qid = overrideQuarter ?? quarterId;
            if (dept) params.department_name = dept;
            if (qid) params.quarter_id = qid;
            const res = await client.get<DashboardResponse>(
                '/api/eos/dashboard/executive',
                Object.keys(params).length > 0 ? { params } : undefined,
            );
            setData(res.data);
        } catch (err) {
            const msg =
                (err as { response?: { data?: { detail?: string } } }).response?.data
                    ?.detail ?? (err instanceof Error ? err.message : 'Unknown error');
            setError(msg);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        // Re-fetch when allowed flips or filters change.
        if (allowed) void fetchData();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [allowed, scopeDept, quarterId]);

    const headerRight = useMemo(() => {
        if (!data) return null;
        const quarters = data.period.available_quarters;
        const depts = data.available_departments;
        return (
            <div className="flex items-center gap-2" data-tour="eos-dashboard-filters">
                <label className="text-xs text-gray-500 flex items-center gap-1.5">
                    Scope
                    <select
                        value={scopeDept}
                        onChange={e => setScopeDept(e.target.value)}
                        className="border border-gray-300 rounded-md px-2 py-1 text-sm bg-white"
                    >
                        <option value="">{EXEC_SCOPE_LABEL}</option>
                        {depts.map(d => (
                            <option key={d} value={d}>{d}</option>
                        ))}
                    </select>
                </label>
                <label className="text-xs text-gray-500 flex items-center gap-1.5">
                    Period
                    <select
                        value={quarterId || data.period.id}
                        onChange={e => setQuarterId(e.target.value)}
                        className="border border-gray-300 rounded-md px-2 py-1 text-sm bg-white"
                    >
                        {quarters.map(q => (
                            <option key={q.id} value={q.id}>
                                {q.name}{q.is_current ? ' (current)' : ''}
                            </option>
                        ))}
                    </select>
                </label>
                <button
                    type="button"
                    onClick={() => fetchData()}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-white border border-gray-300 hover:border-brand hover:text-brand text-gray-700 rounded-md text-sm font-medium"
                >
                    <RefreshCw size={13} /> Refresh
                </button>
            </div>
        );
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [data, scopeDept, quarterId]);

    if (!allowed) {
        return (
            <PageContainer>
                <EosPageHeader
                    icon={<BarChart3 size={20} />}
                    title="Executive EOS Dashboard"
                    subtitle="Restricted to the executive team"
                />
                <div className="bg-amber-50 border border-amber-200 rounded-md p-4 text-sm text-amber-800">
                    The executive dashboard is gated on the <strong>is_exec</strong> flag.
                    Ask an admin to grant access, or use the per-team pages for the
                    department you're on.
                </div>
            </PageContainer>
        );
    }

    return (
        <PageContainer>
            <EosPageHeader
                icon={<BarChart3 size={20} />}
                title="Executive EOS Dashboard"
                subtitle={
                    data
                        ? `${data.period.name} · ${new Date(data.period.start_date).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} – ${new Date(data.period.end_date).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}`
                        : 'Cross-department rollup of rocks, scorecard, issues, and to-dos'
                }
                right={headerRight}
            />

            {loading && <LoadingState label="Loading dashboard…" />}
            {error && <ErrorState message={error} />}

            {!loading && !error && data && (
                <div className="space-y-6">
                    {/* ── 4 KPI cards ──────────────────────────────────── */}
                    <section data-tour="eos-dashboard-health">
                        <div className="flex items-center gap-2 mb-3">
                            <BarChart3 size={14} className="text-gray-400" />
                            <h2 className="text-xs uppercase tracking-wider font-semibold text-gray-500">
                                Health summary
                            </h2>
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
                            <OverallHealthCard
                                score={data.health_summary.overall_score}
                                label={data.health_summary.overall_label}
                                meeting={data.health_summary.meeting_rating}
                                rocks={data.health_summary.rock_health}
                                todos={data.health_summary.todo_completion}
                            />
                            <MeetingRatingCard data={data.health_summary.meeting_rating} />
                            <RockHealthCard data={data.health_summary.rock_health} />
                            <TodoCompletionCard data={data.health_summary.todo_completion} />
                        </div>
                    </section>

                    {/* ── Team performance ────────────────────────────── */}
                    <TeamPerformanceTable
                        rows={data.team_performance}
                        onDrillIn={team => setScopeDept(team)}
                    />

                    {/* ── Trends row ──────────────────────────────────── */}
                    <section data-tour="eos-dashboard-trends">
                        <div className="flex items-center gap-2 mb-3">
                            <TrendingUp size={14} className="text-gray-400" />
                            <h2 className="text-xs uppercase tracking-wider font-semibold text-gray-500">
                                Execution trends
                            </h2>
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
                            <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                                <div className="flex items-center gap-2 mb-2">
                                    <Target size={14} className="text-brand" />
                                    <h3 className="text-sm font-semibold text-gray-900">Meeting health over time</h3>
                                </div>
                                <MeetingHealthChart points={data.trends.meeting_health_over_time} />
                            </div>
                            <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                                <div className="flex items-center gap-2 mb-2">
                                    <ListTodo size={14} className="text-brand" />
                                    <h3 className="text-sm font-semibold text-gray-900">Rock status over time</h3>
                                </div>
                                <RockStatusChart points={data.trends.rock_status_over_time} />
                            </div>
                            <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                                <div className="flex items-center gap-2 mb-2">
                                    <AlertCircle size={14} className="text-brand" />
                                    <h3 className="text-sm font-semibold text-gray-900">Issues opened per week</h3>
                                </div>
                                <IssueTrendChart points={data.trends.issue_trend} />
                            </div>
                            <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-4">
                                <div className="flex items-center gap-2 mb-1">
                                    <AlertCircle size={14} className="text-brand" />
                                    <h3 className="text-sm font-semibold text-gray-900">% of issues solved / week</h3>
                                </div>
                                <p className="text-[11px] text-gray-400 mb-2">
                                    Share of each week's open issues that were resolved
                                </p>
                                <IssueSolvedChart points={data.trends.issue_solved_rate ?? []} />
                            </div>
                        </div>
                    </section>
                </div>
            )}
        </PageContainer>
    );
};
