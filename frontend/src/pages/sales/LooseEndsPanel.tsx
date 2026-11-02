// My Day → "Loose ends": the three oldest open items from the morning
// loop-closure scan, across all three kinds (README §1, surface 2).
//
// Sits BELOW the AE's five blocks, never in place of them — My Day's order is
// load-bearing and this section is additive, like the Daily rhythm under it.
// The worklist rules hold here too: at most three rows, the rest as a "+N
// more" LINK to the full page (never an inline expander), and a one-line
// footer that names what the scan actually looked at.
//
// The panel acts only where acting is a link: a row whose primary action is
// `open_link` opens it; every other row says "Review" and goes to the full
// page, where "Log it in HubSpot" shows the AE's own sent emails before
// logging them and "Draft a reply" lives next to "Not a real miss". A HubSpot
// write is never one click away from a panel that cannot show what it would
// write.
//
// Switched off (404) or not on the roster (403): the section is simply not
// there. My Day above it is unaffected.
import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ChevronRight, ExternalLink } from 'lucide-react';
import {
    coverageGaps,
    coverageLine,
    fetchLooseEnds,
    isForbidden,
    isNotEnabled,
    looseEndKindLabel,
    looseEndsError,
} from '../../api/looseEnds';
import type { LooseEnd, LooseEndsError, LooseEndsMine } from '../../api/looseEnds';
import { AgeChip, MAX_INAPP_ITEMS } from '../../components/sales';
import { ErrorState, LoadingState } from '../eos/_shared';
import { parseInstant } from '../../utils/datetime';

const LIST_CARD = 'bg-white rounded-lg border border-gray-200 shadow-sm divide-y divide-gray-100';
const PAGE = '/sales/loose-ends';
const ROW_LINK =
    'inline-flex items-center gap-1 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50';

/** Oldest first: by the event's own time, then by days open. Sorting ITEMS by
 *  age is the point of the panel; nothing here ranks people. */
const byOldest = (a: LooseEnd, b: LooseEnd): number => {
    const at = parseInstant(a.opened_at)?.getTime();
    const bt = parseInstant(b.opened_at)?.getTime();
    if (at != null && bt != null && at !== bt) return at - bt;
    return (b.days_open ?? 0) - (a.days_open ?? 0);
};

export const LooseEndsPanel: React.FC = () => {
    const [data, setData] = useState<LooseEndsMine | null>(null);
    const [error, setError] = useState<LooseEndsError | null>(null);
    const [now, setNow] = useState(() => new Date());

    // Every setState sits inside the async callback (`react-hooks/set-state-in-
    // effect` is an error in this repo); the spinner is derived below.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const fresh = await fetchLooseEnds();
                if (cancelled) return;
                setData(fresh);
                setError(null);
                setNow(new Date());
            } catch (err) {
                if (!cancelled) setError(looseEndsError(err));
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    if (error && (isNotEnabled(error) || isForbidden(error))) return null;

    let body: React.ReactNode;
    let footer: string | null = null;
    let openCount: number | null = null;

    if (data === null && error === null) {
        body = <LoadingState label="Checking your loose ends…" />;
    } else if (!data) {
        body = <ErrorState message={error?.message ?? 'Loose Ends could not be reached.'} />;
    } else if (!data.scanned_at || (!data.coverage && data.groups.every(g => g.items.length === 0))) {
        body = (
            <p className="px-1 text-sm text-gray-500">
                The first Loose Ends scan runs on the next weekday morning. Your list will show up here
                after it.
            </p>
        );
    } else {
        const hidden = data.content_hidden === true;
        const items = data.groups.flatMap(g => g.items);
        const oldest = [...items].sort(byOldest).slice(0, MAX_INAPP_ITEMS);
        openCount = data.totals?.open ?? items.length;
        const more = Math.max(0, (openCount ?? 0) - oldest.length);
        const gaps = coverageGaps(data.coverage);
        const line = coverageLine(data.coverage, data.scanned_at, now);
        footer = [
            line,
            gaps.length > 0
                ? `${gaps.length === 1 ? 'One source' : `${gaps.length} sources`} could not be read, so this may not be complete — see Loose Ends.`
                : '',
        ]
            .filter(Boolean)
            .join(' ');

        body =
            oldest.length === 0 ? (
                <p className="px-1 text-sm text-gray-500">
                    {gaps.length > 0
                        ? 'No loose ends found — but not every source could be read, so this is not an all-clear.'
                        : 'Nothing loose: everything Cubie checked is logged, answered or done.'}
                </p>
            ) : (
                <>
                    <ul className={LIST_CARD}>
                        {oldest.map(item => {
                            const who = [item.counterparty, item.company].filter(Boolean).join(' · ');
                            const action = item.primary_action;
                            const href = action?.kind === 'open_link' ? action.href : null;
                            return (
                                <li
                                    key={item.id}
                                    className="flex flex-col gap-2 px-5 py-3 sm:flex-row sm:items-center sm:justify-between"
                                >
                                    <div className="min-w-0">
                                        <p className="truncate text-sm font-medium text-gray-900">
                                            {hidden ? (
                                                <span className="font-normal italic text-gray-500">
                                                    Hidden while viewing as someone else
                                                </span>
                                            ) : (
                                                item.title || '(no subject)'
                                            )}
                                        </p>
                                        <p className="mt-0.5 text-xs text-gray-500">
                                            {looseEndKindLabel(item.kind)}
                                            {who ? ` · ${who}` : ''}
                                        </p>
                                    </div>
                                    <div className="flex flex-shrink-0 items-center gap-2">
                                        {item.age && <AgeChip state={item.age.state} label={item.age.label} />}
                                        {action && href ? (
                                            <a href={href} target="_blank" rel="noopener noreferrer" className={ROW_LINK}>
                                                {action.label}
                                                <ExternalLink size={13} aria-hidden="true" />
                                            </a>
                                        ) : (
                                            <Link to={PAGE} className={ROW_LINK}>
                                                Review
                                            </Link>
                                        )}
                                    </div>
                                </li>
                            );
                        })}
                    </ul>
                    {more > 0 && (
                        <div className="mt-2 px-1">
                            <Link
                                to={PAGE}
                                className="inline-flex items-center gap-1 text-sm font-medium text-brand hover:underline"
                            >
                                +{more} more in Loose Ends
                                <ChevronRight size={15} aria-hidden="true" />
                            </Link>
                        </div>
                    )}
                </>
            );
    }

    return (
        <section className="mt-10 border-t border-gray-200 pt-8" aria-labelledby="loose-ends-panel-heading">
            <div className="mb-4 flex flex-wrap items-end justify-between gap-3" data-tour="my-day-loose-ends">
                <div>
                    <h2 id="loose-ends-panel-heading" className="text-lg font-semibold text-gray-900">
                        Loose ends
                        {openCount !== null && (
                            <span className="ml-2 text-sm font-normal text-gray-500">{openCount} open</span>
                        )}
                    </h2>
                    <p className="mt-0.5 text-xs text-gray-500">
                        The oldest things that slipped through: not in HubSpot, waiting on your reply, or
                        promised and not done.
                    </p>
                </div>
                <Link to={PAGE} className="inline-flex items-center gap-1 text-sm font-medium text-brand hover:underline">
                    See all
                    <ChevronRight size={15} aria-hidden="true" />
                </Link>
            </div>
            {body}
            {footer && <p className="mt-3 px-1 text-xs text-gray-500">{footer}</p>}
        </section>
    );
};
