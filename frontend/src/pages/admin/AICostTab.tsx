import React, { useEffect, useMemo, useState } from 'react';
import { motion } from 'framer-motion';
import {
    DollarSign,
    Coins,
    Activity,
    TrendingUp,
    RefreshCw,
    AlertCircle,
    Download,
    ChevronDown,
    ChevronRight,
    Info,
} from 'lucide-react';
import client from '../../api/client';
import { parseInstant } from '../../utils/datetime';

// ─── Types matching backend/routers/admin.py → GET /api/admin/ai-cost ────────

interface Totals {
    cost_usd: number;
    total_tokens: number;
    prompt_tokens: number;
    completion_tokens: number;
    // The models' thinking: priced into cost_usd at the output rate, not part
    // of completion_tokens. Only rows recorded since it was captured carry it.
    thinking_tokens: number;
    cached_tokens: number;
    calls: number;
    generate_calls: number;
    thinking_measured_calls: number;
}

interface FeatureRow {
    feature: string;
    label: string;
    cost_usd: number;
    total_tokens: number;
    prompt_tokens: number;
    completion_tokens: number;
    thinking_tokens: number;
    calls: number;
}

interface ModelRow {
    model: string;
    cost_usd: number;
    total_tokens: number;
    calls: number;
}

interface ProviderRow {
    provider: string;
    billing_owner: string;
    cost_usd: number;
    total_tokens: number;
    calls: number;
}

interface DailyRow {
    date: string;
    cost_usd: number;
    total_tokens: number;
    calls: number;
}

/** One code path that produced untagged spend, so "Unattributed" is never a
 *  dead end — `origin` is a module.function:lineno you can go tag. */
interface UnattributedOrigin {
    origin: string;
    cost_usd: number;
    total_tokens: number;
    calls: number;
}

interface AICostSummary {
    range_days: number;
    since: string;
    generated_at: string;
    tracking_enabled: boolean;
    table_ready?: boolean;
    totals: Totals;
    features: FeatureRow[];
    providers: ProviderRow[];
    models: ModelRow[];
    daily: DailyRow[];
    unattributed_origins?: UnattributedOrigin[];
}

interface RateRow {
    model: string;
    input_per_1m: number;
    output_per_1m: number;
    cached_input_per_1m: number;
}

interface RateCard {
    estimated: boolean;
    unit: string;
    rates: RateRow[];
    fallback: { input_per_1m: number; output_per_1m: number; cached_input_per_1m: number };
}

// ─── Formatting helpers ──────────────────────────────────────────────────────

const fmtUsd = (n: number): string => {
    if (!n) return '$0.00';
    if (n < 0.01) return `$${n.toFixed(4)}`;
    if (n < 100) return `$${n.toFixed(2)}`;
    return `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
};

const fmtTokens = (n: number): string => {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return String(n ?? 0);
};

const fmtInt = (n: number): string => (n ?? 0).toLocaleString();

// The daily series sends bare 'YYYY-MM-DD'. `new Date('2026-07-21')` parses as
// UTC midnight, which renders as the PREVIOUS day in negative-offset zones
// (e.g. US Eastern) — `parseInstant` reads a date-only string as LOCAL midnight
// instead. It used to carry its own copy of that rule; the rule now lives in
// one place, alongside the mirror-image fix for timestamps that ARE instants.
const parseLocalDate = (s: string): Date => parseInstant(s) ?? new Date(s);

const RANGES = [
    { days: 7, label: '7 days' },
    { days: 30, label: '30 days' },
    { days: 90, label: '90 days' },
];

// A stable violet→gold ramp so each feature bar has a distinct but on-brand hue.
const BAR_COLORS = [
    '#7c3aed', '#8b5cf6', '#a78bfa', '#d98c3f', '#e0a458',
    '#5b21b6', '#c4b5fd', '#8a5a2b', '#4a5568', '#718096',
];

export const AICostTab: React.FC = () => {
    const [rangeDays, setRangeDays] = useState(30);
    const [data, setData] = useState<AICostSummary | null>(null);
    const [rateCard, setRateCard] = useState<RateCard | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [showModels, setShowModels] = useState(false);
    const [showRates, setShowRates] = useState(false);
    const [showOrigins, setShowOrigins] = useState(false);

    const load = async (days: number) => {
        setLoading(true);
        setError(null);
        try {
            const [summaryRes, rateRes] = await Promise.all([
                client.get('/api/admin/ai-cost', { params: { days } }),
                client.get('/api/admin/ai-cost/rate-card'),
            ]);
            setData(summaryRes.data);
            setRateCard(rateRes.data);
        } catch (err) {
            console.error('Failed to load AI cost data:', err);
            setError('Failed to load AI cost data. Check that you have admin access and the server is reachable.');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        load(rangeDays);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [rangeDays]);

    const totals = data?.totals;
    const maxDaily = useMemo(
        () => Math.max(0, ...(data?.daily ?? []).map((d) => d.cost_usd)),
        [data],
    );
    const maxFeatureCost = useMemo(
        () => Math.max(0, ...(data?.features ?? []).map((f) => f.cost_usd)),
        [data],
    );
    const avgPerDay = totals && data ? totals.cost_usd / Math.max(1, data.range_days) : 0;

    const exportCsv = () => {
        if (!data) return;
        const header = ['Feature', 'Calls', 'Prompt Tokens', 'Completion Tokens', 'Thinking Tokens', 'Total Tokens', 'Cost (USD)'];
        const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`;
        const lines = [
            header.map(esc).join(','),
            ...data.features.map((f) =>
                [f.label, f.calls, f.prompt_tokens, f.completion_tokens, f.thinking_tokens, f.total_tokens, f.cost_usd.toFixed(6)]
                    .map(esc)
                    .join(','),
            ),
        ];
        const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `ai-cost-by-feature-${data.range_days}d.csv`;
        a.click();
        URL.revokeObjectURL(url);
    };

    return (
        <motion.div
            key="aicost"
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 10 }}
        >
            {/* Header */}
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-6">
                <div>
                    <h3 className="text-lg font-semibold text-gray-900">AI Cost</h3>
                    <p className="text-sm text-gray-500">
                        Token usage and estimated dollar cost by feature and provider. Client-owned providers bill your account directly.
                    </p>
                </div>
                <div className="flex gap-2 items-center">
                    <div className="flex rounded-lg border border-gray-200 overflow-hidden">
                        {RANGES.map((r) => (
                            <button
                                key={r.days}
                                onClick={() => setRangeDays(r.days)}
                                className={`px-3 py-2 text-sm font-medium transition-colors ${
                                    rangeDays === r.days
                                        ? 'bg-brand text-white'
                                        : 'bg-white text-gray-600 hover:bg-gray-50'
                                }`}
                            >
                                {r.label}
                            </button>
                        ))}
                    </div>
                    <button
                        onClick={() => load(rangeDays)}
                        className="flex items-center gap-2 border border-gray-200 bg-white hover:bg-gray-50 text-gray-700 px-3 py-2 rounded-lg text-sm font-medium transition-colors"
                        title="Refresh"
                    >
                        <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                    </button>
                    <button
                        onClick={exportCsv}
                        disabled={!data || data.features.length === 0}
                        className="flex items-center gap-2 bg-brand hover:bg-brand-hover disabled:opacity-50 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
                    >
                        <Download className="h-4 w-4" />
                        Export CSV
                    </button>
                </div>
            </div>

            {/* Tracking-disabled / not-ready banners */}
            {data && data.tracking_enabled === false && (
                <div className="mb-4 p-3 rounded-lg bg-amber-50 text-amber-800 text-sm flex items-center gap-2 border border-amber-100">
                    <AlertCircle className="h-4 w-4 flex-shrink-0" />
                    Cost tracking is currently <strong>disabled</strong> (AI_COST_TRACKING_ENABLED=false). New usage is not being recorded.
                </div>
            )}
            {data && data.table_ready === false && (
                <div className="mb-4 p-3 rounded-lg bg-blue-50 text-blue-800 text-sm flex items-center gap-2 border border-blue-100">
                    <Info className="h-4 w-4 flex-shrink-0" />
                    The usage ledger table isn't available yet — run the database migration to start collecting data.
                </div>
            )}

            {loading ? (
                <div className="flex items-center justify-center py-16">
                    <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-brand" />
                </div>
            ) : error ? (
                <div className="bg-red-50 text-red-700 p-4 rounded-lg flex items-center gap-3">
                    <AlertCircle className="h-5 w-5" /> {error}
                </div>
            ) : !data || totals?.calls === 0 ? (
                <div className="py-16 text-center text-gray-400">
                    <Coins className="h-10 w-10 mx-auto mb-3 opacity-40" />
                    <p className="italic">No AI usage recorded in this period yet.</p>
                    <p className="text-xs mt-1">
                        Costs appear here as features (chat, priorities, meeting prep, …) make model calls.
                    </p>
                </div>
            ) : (
                <>
                    {/* Summary stat cards */}
                    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
                        <StatCard
                            icon={<DollarSign className="h-5 w-5" />}
                            label={`Total cost (${data.range_days}d)`}
                            value={fmtUsd(totals!.cost_usd)}
                            accent
                        />
                        <StatCard
                            icon={<TrendingUp className="h-5 w-5" />}
                            label="Avg / day"
                            value={fmtUsd(avgPerDay)}
                        />
                        <StatCard
                            icon={<Coins className="h-5 w-5" />}
                            label="Total tokens"
                            value={fmtTokens(totals!.total_tokens)}
                            sub={`${fmtTokens(totals!.prompt_tokens)} in · ${fmtTokens(totals!.completion_tokens)} out · ${fmtTokens(totals!.thinking_tokens)} thinking`}
                        />
                        <StatCard
                            icon={<Activity className="h-5 w-5" />}
                            label="Model calls"
                            value={fmtInt(totals!.calls)}
                        />
                    </div>

                    {/* Rows recorded before thinking was counted are priced without it. */}
                    {totals!.thinking_measured_calls < totals!.generate_calls && (
                        <div className="-mt-4 mb-8 flex items-start gap-2 text-xs text-amber-800 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2">
                            <Info className="h-4 w-4 flex-shrink-0 mt-0.5" />
                            <span>
                                Thinking was counted on {fmtInt(totals!.thinking_measured_calls)} of{' '}
                                {fmtInt(totals!.generate_calls)} model calls in this period. The other{' '}
                                {fmtInt(totals!.generate_calls - totals!.thinking_measured_calls)} were recorded before
                                Cubie counted the models' thinking, so their cost leaves it out and reads low.
                            </span>
                        </div>
                    )}

                    {/* Daily trend */}
                    {data.daily.length > 0 && (
                        <div className="mb-8">
                            <h4 className="text-sm font-semibold text-gray-700 mb-3">Daily cost</h4>
                            <DailyChart daily={data.daily} max={maxDaily} />
                        </div>
                    )}

                    {/* Per-feature breakdown */}
                    <div className="mb-8">
                        <h4 className="text-sm font-semibold text-gray-700 mb-3">Cost by feature</h4>
                        <div className="bg-white border border-gray-100 rounded-lg overflow-hidden">
                            <table className="w-full text-left">
                                <thead className="bg-gray-50/95">
                                    <tr className="text-gray-500 text-xs uppercase tracking-wider border-b border-gray-100">
                                        <th className="py-2.5 px-4 font-semibold">Feature</th>
                                        <th className="py-2.5 px-4 font-semibold text-right">Calls</th>
                                        <th className="py-2.5 px-4 font-semibold text-right">Tokens</th>
                                        <th className="py-2.5 px-4 font-semibold text-right">Thinking</th>
                                        <th className="py-2.5 px-4 font-semibold text-right">Cost</th>
                                        <th className="py-2.5 px-4 font-semibold w-40">Share</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-gray-50">
                                    {data.features.map((f, i) => {
                                        const share = totals!.cost_usd > 0 ? (f.cost_usd / totals!.cost_usd) * 100 : 0;
                                        const barPct = maxFeatureCost > 0 ? (f.cost_usd / maxFeatureCost) * 100 : 0;
                                        // "Unattributed" is the one row a number alone can't explain, so it
                                        // expands into the code paths that produced it.
                                        const origins = f.feature === 'unattributed' ? (data.unattributed_origins ?? []) : [];
                                        const expandable = origins.length > 0;
                                        return (
                                            <React.Fragment key={f.feature}>
                                            <tr
                                                className={`transition-colors ${expandable ? 'cursor-pointer hover:bg-gray-50' : 'hover:bg-gray-50/50'}`}
                                                onClick={expandable ? () => setShowOrigins((v) => !v) : undefined}
                                            >
                                                <td className="py-3 px-4 text-sm font-medium text-gray-900">
                                                    <span className="inline-flex items-center gap-2">
                                                        <span
                                                            className="inline-block h-2.5 w-2.5 rounded-full"
                                                            style={{ backgroundColor: BAR_COLORS[i % BAR_COLORS.length] }}
                                                        />
                                                        {f.label}
                                                        {expandable && (
                                                            <span className="text-[11px] font-normal text-gray-400">
                                                                {showOrigins ? '▾' : '▸'} {origins.length} source{origins.length === 1 ? '' : 's'}
                                                            </span>
                                                        )}
                                                    </span>
                                                </td>
                                                <td className="py-3 px-4 text-sm text-gray-600 text-right tabular-nums">{fmtInt(f.calls)}</td>
                                                <td className="py-3 px-4 text-sm text-gray-600 text-right tabular-nums">{fmtTokens(f.total_tokens)}</td>
                                                <td className="py-3 px-4 text-sm text-gray-600 text-right tabular-nums">{fmtTokens(f.thinking_tokens)}</td>
                                                <td className="py-3 px-4 text-sm font-semibold text-gray-900 text-right tabular-nums">{fmtUsd(f.cost_usd)}</td>
                                                <td className="py-3 px-4">
                                                    <div className="flex items-center gap-2">
                                                        <div className="flex-1 h-2 bg-gray-100 rounded-full overflow-hidden">
                                                            <div
                                                                className="h-full rounded-full"
                                                                style={{ width: `${barPct}%`, backgroundColor: BAR_COLORS[i % BAR_COLORS.length] }}
                                                            />
                                                        </div>
                                                        <span className="text-[11px] text-gray-400 w-10 text-right tabular-nums">{share.toFixed(0)}%</span>
                                                    </div>
                                                </td>
                                            </tr>
                                            {expandable && showOrigins && origins.map((o) => (
                                                <tr key={o.origin} className="bg-gray-50/40">
                                                    <td className="py-2 pl-10 pr-4">
                                                        <code className="text-[11px] text-gray-600 break-all">{o.origin}</code>
                                                    </td>
                                                    <td className="py-2 px-4 text-xs text-gray-500 text-right tabular-nums">{fmtInt(o.calls)}</td>
                                                    <td className="py-2 px-4 text-xs text-gray-500 text-right tabular-nums">{fmtTokens(o.total_tokens)}</td>
                                                    <td />
                                                    <td className="py-2 px-4 text-xs font-medium text-gray-700 text-right tabular-nums">{fmtUsd(o.cost_usd)}</td>
                                                    <td />
                                                </tr>
                                            ))}
                                            {expandable && showOrigins && (
                                                <tr className="bg-gray-50/40">
                                                    <td colSpan={6} className="pb-3 pl-10 pr-4 text-[11px] text-gray-400">
                                                        Each source is the code location that made an untagged model call — tag it
                                                        with <code>@attribute_to("&lt;feature&gt;")</code> to move its spend into a real
                                                        feature. "(no origin recorded)" means the call predates origin tracking.
                                                    </td>
                                                </tr>
                                            )}
                                            </React.Fragment>
                                        );
                                    })}
                                </tbody>
                                <tfoot>
                                    <tr className="border-t border-gray-200 bg-gray-50/60">
                                        <td className="py-2.5 px-4 text-sm font-bold text-gray-900">Total</td>
                                        <td className="py-2.5 px-4 text-sm font-bold text-gray-900 text-right tabular-nums">{fmtInt(totals!.calls)}</td>
                                        <td className="py-2.5 px-4 text-sm font-bold text-gray-900 text-right tabular-nums">{fmtTokens(totals!.total_tokens)}</td>
                                        <td className="py-2.5 px-4 text-sm font-bold text-gray-900 text-right tabular-nums">{fmtTokens(totals!.thinking_tokens)}</td>
                                        <td className="py-2.5 px-4 text-sm font-bold text-gray-900 text-right tabular-nums">{fmtUsd(totals!.cost_usd)}</td>
                                        <td />
                                    </tr>
                                </tfoot>
                            </table>
                        </div>
                    </div>

                    {/* Per-model breakdown (collapsible) */}
                    {data.providers?.length > 0 && (
                        <div className="mb-8">
                            <h4 className="text-sm font-semibold text-gray-700 mb-3">Estimated cost by provider</h4>
                            <div className="bg-white border border-gray-100 rounded-lg overflow-hidden">
                                <table className="w-full text-left">
                                    <thead className="bg-gray-50/95 text-xs uppercase tracking-wider text-gray-500">
                                        <tr><th className="py-2.5 px-4">Provider</th><th className="py-2.5 px-4">Billing account</th><th className="py-2.5 px-4 text-right">Calls</th><th className="py-2.5 px-4 text-right">Tokens</th><th className="py-2.5 px-4 text-right">Estimate</th></tr>
                                    </thead>
                                    <tbody className="divide-y divide-gray-50">
                                        {data.providers.map(row => <tr key={`${row.provider}:${row.billing_owner}`}>
                                            <td className="py-3 px-4 text-sm font-medium">{row.provider.replaceAll('_', ' ')}</td>
                                            <td className="py-3 px-4 text-sm">{row.billing_owner === 'client' ? 'Your provider account' : 'Cubie'}</td>
                                            <td className="py-3 px-4 text-sm text-right tabular-nums">{fmtInt(row.calls)}</td>
                                            <td className="py-3 px-4 text-sm text-right tabular-nums">{fmtTokens(row.total_tokens)}</td>
                                            <td className="py-3 px-4 text-sm font-semibold text-right tabular-nums">{fmtUsd(row.cost_usd)}</td>
                                        </tr>)}
                                    </tbody>
                                </table>
                            </div>
                            <p className="mt-2 text-xs text-gray-500">These are usage estimates, not the provider invoice. Check your provider account for actual charges.</p>
                        </div>
                    )}

                    <Collapsible
                        open={showModels}
                        onToggle={() => setShowModels((v) => !v)}
                        title={`Cost by model (${data.models.length})`}
                    >
                        <div className="bg-white border border-gray-100 rounded-lg overflow-hidden mt-2">
                            <table className="w-full text-left">
                                <thead className="bg-gray-50/95">
                                    <tr className="text-gray-500 text-xs uppercase tracking-wider border-b border-gray-100">
                                        <th className="py-2.5 px-4 font-semibold">Model</th>
                                        <th className="py-2.5 px-4 font-semibold text-right">Calls</th>
                                        <th className="py-2.5 px-4 font-semibold text-right">Tokens</th>
                                        <th className="py-2.5 px-4 font-semibold text-right">Cost</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-gray-50">
                                    {data.models.map((m) => (
                                        <tr key={m.model} className="hover:bg-gray-50/50 transition-colors">
                                            <td className="py-3 px-4 text-sm font-mono text-xs text-gray-800">{m.model}</td>
                                            <td className="py-3 px-4 text-sm text-gray-600 text-right tabular-nums">{fmtInt(m.calls)}</td>
                                            <td className="py-3 px-4 text-sm text-gray-600 text-right tabular-nums">{fmtTokens(m.total_tokens)}</td>
                                            <td className="py-3 px-4 text-sm font-semibold text-gray-900 text-right tabular-nums">{fmtUsd(m.cost_usd)}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </Collapsible>

                    {/* Rate card (collapsible) */}
                    {rateCard && (
                        <Collapsible
                            open={showRates}
                            onToggle={() => setShowRates((v) => !v)}
                            title="Rate card (how costs are estimated)"
                        >
                            <div className="mt-2">
                                <div className="mb-3 p-3 rounded-lg bg-blue-50 text-blue-800 text-xs flex items-start gap-2 border border-blue-100">
                                    <Info className="h-4 w-4 flex-shrink-0 mt-0.5" />
                                    <span>
                                        Estimated provider pricing in <strong>{rateCard.unit}</strong>. These are best-effort
                                        rates — correct them in <code className="bg-blue-100 px-1 rounded">rubik_core/cost/pricing.py</code>{' '}
                                        or the <code className="bg-blue-100 px-1 rounded">LLM_PRICING_JSON</code> env var. A model's
                                        thinking tokens bill at its output rate. Historical costs keep the rate that was live when
                                        they were recorded.
                                    </span>
                                </div>
                                <div className="bg-white border border-gray-100 rounded-lg overflow-hidden">
                                    <table className="w-full text-left">
                                        <thead className="bg-gray-50/95">
                                            <tr className="text-gray-500 text-xs uppercase tracking-wider border-b border-gray-100">
                                                <th className="py-2.5 px-4 font-semibold">Model</th>
                                                <th className="py-2.5 px-4 font-semibold text-right">Input</th>
                                                <th className="py-2.5 px-4 font-semibold text-right">Output</th>
                                                <th className="py-2.5 px-4 font-semibold text-right">Cached input</th>
                                            </tr>
                                        </thead>
                                        <tbody className="divide-y divide-gray-50">
                                            {rateCard.rates.map((r) => (
                                                <tr key={r.model} className="hover:bg-gray-50/50">
                                                    <td className="py-2.5 px-4 text-sm font-mono text-xs text-gray-800">{r.model}</td>
                                                    <td className="py-2.5 px-4 text-sm text-gray-600 text-right tabular-nums">${r.input_per_1m.toFixed(3)}</td>
                                                    <td className="py-2.5 px-4 text-sm text-gray-600 text-right tabular-nums">${r.output_per_1m.toFixed(3)}</td>
                                                    <td className="py-2.5 px-4 text-sm text-gray-600 text-right tabular-nums">${r.cached_input_per_1m.toFixed(3)}</td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </Collapsible>
                    )}

                    <p className="text-[11px] text-gray-400 mt-6">
                        Since {new Date(data.since).toLocaleDateString()} · generated {new Date(data.generated_at).toLocaleTimeString()}.
                        Dollar figures are estimates; see the rate card above.
                    </p>
                </>
            )}
        </motion.div>
    );
};

// ─── Sub-components ───────────────────────────────────────────────────────────

const StatCard: React.FC<{
    icon: React.ReactNode;
    label: string;
    value: string;
    sub?: string;
    accent?: boolean;
}> = ({ icon, label, value, sub, accent }) => (
    <div
        className={`rounded-2xl border p-4 ${
            accent ? 'bg-brand border-brand text-white' : 'bg-white border-gray-100 text-gray-900'
        }`}
    >
        <div className={`flex items-center gap-2 text-xs font-medium ${accent ? 'text-white/80' : 'text-gray-500'}`}>
            <span className={accent ? 'text-white' : 'text-brand'}>{icon}</span>
            {label}
        </div>
        <div className="mt-2 text-2xl font-bold tabular-nums">{value}</div>
        {sub && <div className={`mt-0.5 text-[11px] ${accent ? 'text-white/70' : 'text-gray-400'}`}>{sub}</div>}
    </div>
);

const DailyChart: React.FC<{ daily: DailyRow[]; max: number }> = ({ daily, max }) => {
    // Inline SVG bar chart — no external chart lib, fully theme-safe.
    const W = 100; // viewBox width units (percentage-based, responsive)
    const H = 120;
    const n = daily.length;
    // Slot-based layout: each bar owns a W/n slot and fills 80% of it (20% gap).
    // This stays positive for any n — the old (W - gap*(n-1))/n went negative
    // past ~50 bars (the 90-day range), which hid the whole chart.
    const step = n > 0 ? W / n : 0;
    const barW = Math.max(0.2, step * 0.8);
    const [hover, setHover] = useState<number | null>(null);

    return (
        <div className="bg-white border border-gray-100 rounded-lg p-4">
            <div className="relative">
                <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="w-full h-32">
                    {daily.map((d, i) => {
                        const h = max > 0 ? (d.cost_usd / max) * (H - 4) : 0;
                        const x = i * step + (step - barW) / 2;
                        const isHover = hover === i;
                        return (
                            <rect
                                key={d.date}
                                x={x}
                                y={H - h}
                                width={barW}
                                height={Math.max(h, d.cost_usd > 0 ? 1 : 0)}
                                rx={0.5}
                                className={isHover ? 'fill-brand-hover' : 'fill-brand'}
                                opacity={hover === null || isHover ? 1 : 0.55}
                                onMouseEnter={() => setHover(i)}
                                onMouseLeave={() => setHover(null)}
                            />
                        );
                    })}
                </svg>
                {hover !== null && daily[hover] && (
                    <div className="absolute -top-1 left-1/2 -translate-x-1/2 bg-gray-900 text-white text-[11px] rounded px-2 py-1 pointer-events-none whitespace-nowrap">
                        {parseLocalDate(daily[hover].date).toLocaleDateString()} · {fmtUsd(daily[hover].cost_usd)} · {fmtInt(daily[hover].calls)} calls
                    </div>
                )}
            </div>
            <div className="flex justify-between mt-2 text-[11px] text-gray-400">
                <span>{daily[0] && parseLocalDate(daily[0].date).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</span>
                <span>{daily[daily.length - 1] && parseLocalDate(daily[daily.length - 1].date).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</span>
            </div>
        </div>
    );
};

const Collapsible: React.FC<{
    open: boolean;
    onToggle: () => void;
    title: string;
    children: React.ReactNode;
}> = ({ open, onToggle, title, children }) => (
    <div className="mb-6">
        <button
            onClick={onToggle}
            className="flex items-center gap-2 text-sm font-semibold text-gray-700 hover:text-brand transition-colors"
        >
            {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            {title}
        </button>
        {open && children}
    </div>
);
