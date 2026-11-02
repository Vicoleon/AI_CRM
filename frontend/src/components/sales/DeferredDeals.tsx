import React, { useEffect, useState } from 'react';
import { CalendarClock, ExternalLink } from 'lucide-react';
import { cockpitError } from '../../api/cockpit';
import { fetchDeferredDeals, undeferDeal } from '../../api/dealDefer';
import type { DeferredDeal, DeferTarget } from '../../api/dealDefer';
import { toast } from '../eos/Toast';
import { DeferDealModal } from './DeferDealModal';

// My Day → "Deferred deals": the rep's own deferred deals, soonest back first.
//
// This is where Undefer lives. A deferred deal is off every other list the rep
// sees, so without this section it could not be found again before its date.
// It also shows a HubSpot task that did not land or is not confirmed, with a
// Retry that re-sends the same date and note (the server reuses the defer and
// retries only the task, keyed so it can never write two; a claim still held
// by an attempt that may have landed is refused rather than retried).
//
// Last on My Day, below the five blocks, Loose Ends and the Daily rhythm: My
// Day's block order is the feature and is not touched. Always rendered for people on the
// `deal_defer` roster — an empty one says how to defer, which is also the
// anchor the page tour points at.

const LIST_CARD = 'bg-white rounded-lg border border-gray-200 shadow-sm divide-y divide-gray-100';
const SECTION = 'mt-10 border-t border-gray-200 pt-8';

interface RetryTarget {
    target: DeferTarget;
    date: string;
    note: string;
}

export const DeferredDeals: React.FC<{
    /** Bumped by the parent after a defer elsewhere on the page. */
    reloadKey: number;
    /** After an Undefer: the deal is back, so the parent refetches its blocks. */
    onChanged: () => void;
}> = ({ reloadKey, onChanged }) => {
    const [items, setItems] = useState<DeferredDeal[] | null>(null);
    const [failed, setFailed] = useState(false);
    const [localKey, setLocalKey] = useState(0);
    const [busy, setBusy] = useState<string | null>(null);
    const [retry, setRetry] = useState<RetryTarget | null>(null);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const fresh = await fetchDeferredDeals();
                if (cancelled) return;
                setItems(fresh);
                setFailed(false);
            } catch {
                if (!cancelled) setFailed(true);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [reloadKey, localKey]);

    const undefer = async (deal: DeferredDeal): Promise<void> => {
        if (busy) return;
        setBusy(deal.deal_id);
        try {
            const res = await undeferDeal(deal.deal_id);
            toast.success(res.message);
            setLocalKey(k => k + 1);
            onChanged();
        } catch (err) {
            toast.error(cockpitError(err, 'The deal could not be brought back.').message);
        } finally {
            setBusy(null);
        }
    };

    if (failed && items === null) {
        return (
            <section className={SECTION} data-tour="my-day-deferred">
                <h2 className="mb-1 text-lg font-semibold text-gray-900">Deferred deals</h2>
                <p className="px-1 text-sm text-gray-500">
                    Your deferred deals could not be loaded just now. They are still deferred; refresh
                    to try again.
                </p>
            </section>
        );
    }
    if (items === null) return null;

    return (
        <section className={SECTION} data-tour="my-day-deferred">
            <div className="mb-3">
                <h2 className="text-lg font-semibold text-gray-900">
                    Deferred deals
                    {items.length > 0 && (
                        <span className="ml-2 text-sm font-normal text-gray-500">{items.length}</span>
                    )}
                </h2>
                <p className="mt-0.5 text-xs text-gray-500">
                    Off your lists until the follow-up date, then back by themselves. Your manager still
                    sees them.
                </p>
            </div>

            {items.length === 0 ? (
                <p className="px-1 text-sm text-gray-500">
                    Nothing deferred. Use “Defer” on a deal row to set a long-term deal aside until a
                    follow-up date.
                </p>
            ) : (
                <div className={LIST_CARD}>
                    {items.map(d => (
                        <div
                            key={d.deal_id}
                            className="flex flex-col gap-2 px-5 py-4 sm:flex-row sm:items-start sm:justify-between"
                        >
                            <div className="min-w-0 flex-1">
                                <p className="text-base font-semibold text-gray-900">
                                    {d.deal_name || `Deal ${d.deal_id}`}
                                </p>
                                <p className="mt-0.5 flex items-center gap-1.5 text-sm text-gray-600">
                                    <CalendarClock size={15} aria-hidden="true" />
                                    Back on {d.follow_up_on}
                                    <span className="text-gray-400">· deferred {d.deferred_on}</span>
                                </p>
                                <p className="mt-1 text-sm text-gray-700">{d.note}</p>
                                {d.hubspot_task_status === 'failed' && (
                                    <p className="mt-2 text-xs text-amber-800" role="status">
                                        The HubSpot follow-up task was not created
                                        {d.hubspot_task_error ? `: ${d.hubspot_task_error}` : '.'}
                                    </p>
                                )}
                                {d.hubspot_task_status === 'pending' && (
                                    <p className="mt-2 text-xs text-amber-800" role="status">
                                        {d.hubspot_task_error ||
                                            'The HubSpot follow-up task is not confirmed yet.'}
                                    </p>
                                )}
                            </div>
                            <div className="flex flex-shrink-0 flex-wrap items-center gap-2 sm:justify-end">
                                {d.hubspot_url && (
                                    <a
                                        href={d.hubspot_url}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="inline-flex items-center gap-1 px-2 py-1 text-sm font-medium text-gray-500 hover:text-gray-800"
                                    >
                                        HubSpot
                                        <ExternalLink size={14} aria-hidden="true" />
                                    </a>
                                )}
                                {d.hubspot_task_status !== 'written' && (
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setRetry({
                                                target: { deal_id: d.deal_id, deal_name: d.deal_name },
                                                date: d.follow_up_on,
                                                note: d.note,
                                            })
                                        }
                                        className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
                                    >
                                        Retry the HubSpot task
                                    </button>
                                )}
                                <button
                                    type="button"
                                    onClick={() => void undefer(d)}
                                    disabled={busy !== null}
                                    className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                                >
                                    {busy === d.deal_id ? 'Bringing back…' : 'Undefer'}
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            <DeferDealModal
                key={retry ? `${retry.target.deal_id}:${retry.date}` : 'closed'}
                target={retry?.target ?? null}
                initialDate={retry?.date}
                initialNote={retry?.note}
                onClose={() => setRetry(null)}
                onDeferred={() => setLocalKey(k => k + 1)}
            />
        </section>
    );
};
