// Admin · Feedback (WGLL) — a simple list of every rating users gave Cubie's
// proposals/messages, with surface/rating filters and a JSON export. This is the
// reviewable "what good looks like" dataset that also auto-feeds Cubie's prompts.
import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Download, ThumbsUp, ThumbsDown, Meh, MessageSquare, ListChecks, ClipboardList } from 'lucide-react';
import client from '../../api/client';
import { formatInstant } from '../../utils/datetime';

interface FeedbackRow {
    id: string;
    surface: string;
    proposal_id: string;
    proposal_text: string;
    proposal_metadata?: Record<string, any> | null;
    rating: string;
    comment?: string | null;
    user_email: string;
    created_at: string;
    updated_at: string;
}

interface ListResponse {
    items: FeedbackRow[];
    total: number;
    counts: Record<string, number>;
}

const PAGE_SIZE = 50;

const RATING_BADGE: Record<string, { label: string; cls: string; Icon: typeof ThumbsUp }> = {
    useful: { label: 'Useful', cls: 'bg-green-100 text-green-700', Icon: ThumbsUp },
    meh: { label: 'Meh', cls: 'bg-amber-100 text-amber-700', Icon: Meh },
    not_useful: { label: 'Not useful', cls: 'bg-red-100 text-red-700', Icon: ThumbsDown },
};

function fmtDate(iso: string): string {
    return formatInstant(
        iso,
        { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' },
        iso,
    );
}

export const FeedbackTab: React.FC = () => {
    const [data, setData] = useState<ListResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [surface, setSurface] = useState<'' | 'priorities' | 'chat' | 'meeting_prep'>('');
    const [rating, setRating] = useState<'' | 'useful' | 'meh' | 'not_useful'>('');
    const [offset, setOffset] = useState(0);
    const [exporting, setExporting] = useState(false);

    const fetchData = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const params: Record<string, any> = { limit: PAGE_SIZE, offset };
            if (surface) params.surface = surface;
            if (rating) params.rating = rating;
            const res = await client.get<ListResponse>('/api/feedback/admin', { params });
            setData(res.data);
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Failed to load feedback');
        } finally {
            setLoading(false);
        }
    }, [surface, rating, offset]);

    useEffect(() => { fetchData(); }, [fetchData]);

    // Reset to page 1 whenever a filter changes.
    useEffect(() => { setOffset(0); }, [surface, rating]);

    const handleExport = async () => {
        setExporting(true);
        try {
            const params: Record<string, any> = {};
            if (surface) params.surface = surface;
            const res = await client.get('/api/feedback/admin/export', { params, responseType: 'blob' });
            const url = window.URL.createObjectURL(new Blob([res.data], { type: 'application/json' }));
            const a = document.createElement('a');
            a.href = url;
            a.download = `cubie-feedback${surface ? `-${surface}` : ''}.json`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            window.URL.revokeObjectURL(url);
        } catch (e) {
            console.error('Export failed', e);
        } finally {
            setExporting(false);
        }
    };

    const counts = data?.counts || {};
    const total = data?.total ?? 0;
    const items = data?.items || [];

    return (
        <div className="space-y-4">
            {/* Header + summary */}
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <h2 className="text-lg font-semibold text-gray-900">Feedback (WGLL)</h2>
                    <p className="text-sm text-gray-500">
                        Ratings users gave Cubie's proposals & chat answers. This dataset is exportable and
                        also auto-feeds Cubie's prompts as "what good looks like" examples.
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <button
                        onClick={fetchData}
                        className="inline-flex items-center gap-1.5 text-sm px-3 py-1.5 rounded border border-gray-200 text-gray-600 hover:bg-gray-50"
                    >
                        <RefreshCw className="h-4 w-4" /> Refresh
                    </button>
                    <button
                        onClick={handleExport}
                        disabled={exporting}
                        className="inline-flex items-center gap-1.5 text-sm px-3 py-1.5 rounded bg-brand text-white hover:bg-brand-hover disabled:opacity-50"
                    >
                        <Download className="h-4 w-4" /> Export JSON
                    </button>
                </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
                <span className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded bg-green-50 text-green-700">
                    <ThumbsUp className="h-3.5 w-3.5" /> {counts.useful || 0} useful
                </span>
                <span className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded bg-amber-50 text-amber-700">
                    <Meh className="h-3.5 w-3.5" /> {counts.meh || 0} meh
                </span>
                <span className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded bg-red-50 text-red-700">
                    <ThumbsDown className="h-3.5 w-3.5" /> {counts.not_useful || 0} not useful
                </span>
            </div>

            {/* Filters */}
            <div className="flex flex-wrap items-center gap-2">
                <select
                    value={surface}
                    onChange={(e) => setSurface(e.target.value as any)}
                    className="text-sm border border-gray-200 rounded px-2 py-1.5"
                >
                    <option value="">All surfaces</option>
                    <option value="priorities">Priorities</option>
                    <option value="chat">Chat</option>
                    <option value="meeting_prep">Meeting Prep</option>
                </select>
                <select
                    value={rating}
                    onChange={(e) => setRating(e.target.value as any)}
                    className="text-sm border border-gray-200 rounded px-2 py-1.5"
                >
                    <option value="">All ratings</option>
                    <option value="useful">Useful</option>
                    <option value="meh">Meh</option>
                    <option value="not_useful">Not useful</option>
                </select>
            </div>

            {error && (
                <div className="text-sm text-red-600 bg-red-50 border border-red-100 rounded px-3 py-2">{error}</div>
            )}

            {/* Table */}
            {loading ? (
                <div className="flex items-center justify-center py-12">
                    <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-brand" />
                </div>
            ) : items.length === 0 ? (
                <div className="text-center text-gray-400 py-12 text-sm">No feedback yet.</div>
            ) : (
                <div className="overflow-x-auto border border-gray-100 rounded-lg">
                    <table className="min-w-full text-sm">
                        <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-tight">
                            <tr>
                                <th className="text-left px-3 py-2 font-medium">Rating</th>
                                <th className="text-left px-3 py-2 font-medium">Surface</th>
                                <th className="text-left px-3 py-2 font-medium">Proposal / message</th>
                                <th className="text-left px-3 py-2 font-medium">Why (note)</th>
                                <th className="text-left px-3 py-2 font-medium">User</th>
                                <th className="text-left px-3 py-2 font-medium">When</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-50">
                            {items.map((row) => {
                                const badge = RATING_BADGE[row.rating] || { label: row.rating, cls: 'bg-gray-100 text-gray-600', Icon: Meh };
                                const Icon = badge.Icon;
                                return (
                                    <tr key={row.id} className="align-top hover:bg-gray-50/50">
                                        <td className="px-3 py-2 whitespace-nowrap">
                                            <span className={`inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded ${badge.cls}`}>
                                                <Icon className="h-3 w-3" /> {badge.label}
                                            </span>
                                        </td>
                                        <td className="px-3 py-2 whitespace-nowrap text-gray-500">
                                            <span className="inline-flex items-center gap-1">
                                                {row.surface === 'chat'
                                                    ? <MessageSquare className="h-3.5 w-3.5" />
                                                    : row.surface === 'meeting_prep'
                                                    ? <ClipboardList className="h-3.5 w-3.5" />
                                                    : <ListChecks className="h-3.5 w-3.5" />}
                                                {row.surface}
                                            </span>
                                        </td>
                                        <td className="px-3 py-2 text-gray-700 max-w-md">
                                            <div className="line-clamp-3 whitespace-pre-wrap">{row.proposal_text}</div>
                                        </td>
                                        <td className="px-3 py-2 text-gray-600 max-w-xs">
                                            {row.comment ? <span className="italic">"{row.comment}"</span> : <span className="text-gray-300">—</span>}
                                        </td>
                                        <td className="px-3 py-2 whitespace-nowrap text-gray-500">{row.user_email}</td>
                                        <td className="px-3 py-2 whitespace-nowrap text-gray-400">{fmtDate(row.updated_at)}</td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>
            )}

            {/* Pagination */}
            {total > PAGE_SIZE && (
                <div className="flex items-center justify-between text-sm text-gray-500">
                    <span>
                        {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
                    </span>
                    <div className="flex items-center gap-2">
                        <button
                            disabled={offset === 0}
                            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                            className="px-3 py-1.5 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-50"
                        >
                            Prev
                        </button>
                        <button
                            disabled={offset + PAGE_SIZE >= total}
                            onClick={() => setOffset(offset + PAGE_SIZE)}
                            className="px-3 py-1.5 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-50"
                        >
                            Next
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
};

export default FeedbackTab;
