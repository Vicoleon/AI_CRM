import React, { useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Eye, Sparkles } from 'lucide-react';
import { insightsError, summarizeDashboard } from '../../api/insights';
import type { InsightsSummary } from '../../api/insights';
import type { InsightsFilters } from '../../pages/insights/insightsFilters';
import { formatDateTime } from '../../pages/insights/insightsFormat';

type State =
    | { status: 'idle' }
    | { status: 'loading' }
    | { status: 'done'; summary: InsightsSummary }
    | { status: 'error'; message: string };

const BUTTON =
    'inline-flex items-center gap-1.5 rounded-md border border-brand/25 bg-white px-3 py-1.5 text-sm font-medium text-brand shadow-sm transition-colors hover:bg-brand hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30 disabled:cursor-not-allowed disabled:opacity-60';

/**
 * "Summarize this view": asked for, never automatic, because each summary is a
 * model call with its own line on AI Cost. The server sends the model the
 * department-level numbers on this page and nothing else — no names — and
 * drops any sentence quoting a number that isn't on the page.
 *
 * Remount it (key it on dashboard + filters) when the view changes: a summary
 * belongs to the numbers it was written from.
 */
export const AiSummaryCard: React.FC<{
    slug: string;
    filters: InsightsFilters;
    /** The page has no numbers yet (first load, error). */
    disabled?: boolean;
}> = ({ slug, filters, disabled = false }) => {
    const [state, setState] = useState<State>({ status: 'idle' });

    const run = async () => {
        setState({ status: 'loading' });
        try {
            const summary = await summarizeDashboard(slug, filters);
            setState({ status: 'done', summary });
        } catch (e) {
            setState({ status: 'error', message: insightsError(e).message });
        }
    };

    const done = state.status === 'done' && state.summary.available ? state.summary : null;

    return (
        <section
            data-tour="insights-ai-summary"
            aria-label="AI summary"
            className="rounded-lg border border-brand/15 bg-gradient-to-br from-brand-tint/70 via-white to-white p-4 shadow-sm sm:px-5"
        >
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-2.5">
                    <span className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-brand/10 text-brand">
                        <Sparkles size={16} aria-hidden="true" />
                    </span>
                    <div className="min-w-0">
                        <h2 className="text-sm font-semibold text-gray-900">AI summary</h2>
                        <p className="text-xs text-gray-500">
                            {state.status === 'idle'
                                ? 'A short read of the numbers below: what moved, and what is worth a look.'
                                : 'AI-generated from the numbers on this page. Check anything important against the charts.'}
                        </p>
                    </div>
                </div>
                {state.status !== 'loading' && (
                    <button type="button" onClick={() => void run()} disabled={disabled} className={BUTTON}>
                        <Sparkles size={14} aria-hidden="true" />
                        {state.status === 'idle' ? 'Summarize this view' : 'Summarize again'}
                    </button>
                )}
            </div>

            <div aria-live="polite" aria-busy={state.status === 'loading'}>
                <AnimatePresence mode="wait" initial={false}>
                    {state.status === 'loading' && (
                        <motion.div
                            key="loading"
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            exit={{ opacity: 0 }}
                            className="mt-4 space-y-2.5"
                        >
                            <span className="sr-only">Writing a summary…</span>
                            <div className="h-4 w-2/3 animate-pulse rounded bg-brand/10 motion-reduce:animate-none" />
                            <div className="h-3 w-full animate-pulse rounded bg-gray-200/70 motion-reduce:animate-none" />
                            <div className="h-3 w-11/12 animate-pulse rounded bg-gray-200/70 motion-reduce:animate-none" />
                            <div className="h-3 w-4/5 animate-pulse rounded bg-gray-200/70 motion-reduce:animate-none" />
                        </motion.div>
                    )}
                    {done && (
                        <motion.div
                            key="done"
                            initial={{ opacity: 0, y: 4 }}
                            animate={{ opacity: 1, y: 0 }}
                            exit={{ opacity: 0 }}
                            className="mt-4 space-y-3"
                        >
                            <p className="text-[15px] font-medium leading-snug text-gray-900">{done.headline}</p>
                            {done.bullets.length > 0 && (
                                <ul className="list-disc space-y-1 pl-5 text-sm leading-relaxed text-gray-700 marker:text-brand/60">
                                    {done.bullets.map((b, i) => (
                                        <li key={i}>{b}</li>
                                    ))}
                                </ul>
                            )}
                            {done.watch.length > 0 && (
                                <div className="rounded-md border border-amber-200/70 bg-amber-50/60 px-3 py-2">
                                    <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-amber-800">
                                        <Eye size={13} aria-hidden="true" />
                                        Worth watching
                                    </p>
                                    <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-amber-900">
                                        {done.watch.map((w, i) => (
                                            <li key={i}>{w}</li>
                                        ))}
                                    </ul>
                                </div>
                            )}
                            <p className="text-[11px] text-gray-400">
                                Written {formatDateTime(done.generated_at)}
                                {done.cached ? ' · from cache' : ''}
                            </p>
                        </motion.div>
                    )}
                    {state.status === 'done' && !state.summary.available && (
                        <motion.p
                            key="unavailable"
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            exit={{ opacity: 0 }}
                            className="mt-3 text-sm text-gray-600"
                        >
                            {/* The server's reason is already a full sentence. */}
                            {state.summary.reason || 'The AI summary isn\u2019t available right now.'} The numbers below are
                            unaffected.
                        </motion.p>
                    )}
                    {state.status === 'error' && (
                        <motion.p
                            key="error"
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            exit={{ opacity: 0 }}
                            className="mt-3 text-sm text-red-700"
                        >
                            Couldn&apos;t write a summary: {state.message}
                        </motion.p>
                    )}
                </AnimatePresence>
            </div>
        </section>
    );
};
