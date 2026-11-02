import React from 'react';
import { RefreshCw } from 'lucide-react';
import { formatInstantTime } from '../../utils/datetime';

/**
 * "Data as of 10:42 · cached" and a refresh. Figures are cached on the server
 * for up to ten minutes; the refresh button recomputes them now.
 */
export const FreshnessBadge: React.FC<{
    generatedAt: string | null | undefined;
    cached: boolean;
    refreshing: boolean;
    onRefresh: () => void;
}> = ({ generatedAt, cached, refreshing, onRefresh }) => {
    const time = generatedAt ? formatInstantTime(generatedAt) : null;
    return (
        <div className="inline-flex items-center gap-1 rounded-md border border-gray-200 bg-white pl-2.5 text-xs text-gray-500 shadow-sm">
            <span
                className="tabular-nums"
                title={cached ? 'Served from the 10-minute cache. Refresh to recompute now.' : 'Computed just now.'}
            >
                {time ? (
                    <>
                        Data as of <span className="font-medium text-gray-700">{time}</span>
                        {cached ? ' · cached' : ''}
                    </>
                ) : (
                    'Loading…'
                )}
            </span>
            <button
                type="button"
                onClick={onRefresh}
                disabled={refreshing}
                aria-label="Refresh the numbers"
                title="Refresh the numbers"
                className="ml-1 rounded-r-md border-l border-gray-200 px-2 py-1.5 text-gray-500 transition-colors hover:bg-gray-50 hover:text-brand focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30 disabled:cursor-wait"
            >
                <RefreshCw size={14} aria-hidden="true" className={refreshing ? 'animate-spin motion-reduce:animate-none' : ''} />
            </button>
        </div>
    );
};
