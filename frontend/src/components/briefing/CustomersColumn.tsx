// UC-45 — the "Customers" tab body on the Productivity page.
// Typeahead company search + a grid of recent X-Rays, each with Fast / Deep
// launch buttons that navigate to the CustomerXRay page (which runs it).
import React, { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Building2, Search, Zap, Telescope, Loader2, ArrowRight } from 'lucide-react';
import {
    searchCustomers,
    recentXrays,
    type CustomerRow,
    CHURN_BAND_COLOR,
} from '../../api/xray';

const ChurnBadge: React.FC<{ band?: string; score?: number }> = ({ band, score }) => {
    if (!band) return null;
    const color = CHURN_BAND_COLOR[band] || '#6b7280';
    return (
        <span
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold"
            style={{ color, backgroundColor: `${color}1a` }}
        >
            {band}
            {typeof score === 'number' ? ` · ${score}` : ''}
        </span>
    );
};

const CustomerCard: React.FC<{ row: CustomerRow }> = ({ row }) => {
    const navigate = useNavigate();
    const go = (run?: 'fast' | 'deep') =>
        navigate(`/customers/${row.company_id}/xray`, {
            state: { name: row.name, domain: row.domain, run },
        });
    return (
        <div className="rounded-2xl border border-gray-200 bg-white p-4 shadow-sm transition-all hover:shadow-md">
            <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                    <div className="flex items-center gap-2">
                        <Building2 size={16} className="shrink-0 text-brand" />
                        <h3 className="truncate font-semibold text-gray-900">
                            {row.name || row.domain || row.company_id}
                        </h3>
                    </div>
                    <p className="mt-0.5 truncate text-xs text-gray-500">
                        {row.domain}
                        {row.csm ? ` · CSM: ${row.csm}` : ''}
                        {row.client_status ? ` · ${row.client_status}` : ''}
                    </p>
                </div>
                {row.latest_snapshot && (
                    <ChurnBadge
                        band={row.latest_snapshot.churn_band}
                        score={row.latest_snapshot.churn_score}
                    />
                )}
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                    onClick={() => go('fast')}
                    className="inline-flex items-center gap-1 rounded-lg bg-brand px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-brand-hover"
                >
                    <Zap size={13} /> Fast Research
                </button>
                <button
                    onClick={() => go('deep')}
                    className="inline-flex items-center gap-1 rounded-lg border border-brand px-3 py-1.5 text-xs font-medium text-brand transition-colors hover:bg-brand/5"
                >
                    <Telescope size={13} /> Deep Research
                </button>
                {row.latest_snapshot && (
                    <button
                        onClick={() => go()}
                        className="inline-flex items-center gap-1 rounded-lg px-2 py-1.5 text-xs font-medium text-gray-500 hover:text-gray-800"
                    >
                        Open v{row.latest_snapshot.version} <ArrowRight size={13} />
                    </button>
                )}
            </div>
        </div>
    );
};

export const CustomersColumn: React.FC = () => {
    const [q, setQ] = useState('');
    const [rows, setRows] = useState<CustomerRow[]>([]);
    const [recent, setRecent] = useState<CustomerRow[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const debounce = useRef<number | null>(null);

    useEffect(() => {
        recentXrays()
            .then(setRecent)
            .catch(() => {
                /* non-fatal */
            });
    }, []);

    useEffect(() => {
        if (debounce.current) window.clearTimeout(debounce.current);
        if (!q.trim()) {
            setRows([]);
            setError(null);
            return;
        }
        setLoading(true);
        debounce.current = window.setTimeout(() => {
            searchCustomers(q.trim())
                .then((r) => {
                    setRows(r);
                    setError(
                        r.length
                            ? null
                            : 'No customers found. HubSpot search may be unavailable.',
                    );
                })
                .catch(() => setError('Search failed. Try again.'))
                .finally(() => setLoading(false));
        }, 350);
        return () => {
            if (debounce.current) window.clearTimeout(debounce.current);
        };
    }, [q]);

    const showing = q.trim() ? rows : recent;

    return (
        <div className="p-6">
            <div className="relative mb-5 max-w-xl">
                <Search
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
                />
                <input
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder="Search a customer by name…"
                    className="w-full rounded-xl border border-gray-200 py-2.5 pl-9 pr-3 text-sm outline-none focus:border-brand"
                />
                {loading && (
                    <Loader2
                        size={16}
                        className="absolute right-3 top-1/2 -translate-y-1/2 animate-spin text-gray-400"
                    />
                )}
            </div>

            {error && <p className="mb-3 text-sm text-amber-600">{error}</p>}

            {!q.trim() && (
                <h2 className="mb-3 text-sm font-semibold text-gray-500">
                    {recent.length
                        ? 'Recent X-Rays'
                        : 'Search a customer to generate an X-Ray'}
                </h2>
            )}

            {showing.length === 0 && !loading && !q.trim() && (
                <div className="flex flex-col items-center justify-center py-16 text-center text-gray-400">
                    <Building2 size={32} className="mb-2 opacity-50" />
                    <p className="text-sm">No X-Rays yet.</p>
                    <p className="mt-1 max-w-md text-xs">
                        Search for a customer above, then run a <b>Fast Research</b> (seconds)
                        or commission <b>Deep Research</b> (a few minutes) — churn score,
                        relationship timeline, commercial health, and expansion
                        opportunities.
                    </p>
                </div>
            )}

            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                {showing.map((row) => (
                    <CustomerCard key={row.company_id} row={row} />
                ))}
            </div>
        </div>
    );
};

export default CustomersColumn;
