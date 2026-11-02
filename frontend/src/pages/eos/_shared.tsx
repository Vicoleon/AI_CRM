import React from 'react';
import { Loader2, AlertCircle } from 'lucide-react';
import type { TeamScope } from './_utils';

// ─── Page chrome ────────────────────────────────────────────────────────────

interface EosPageHeaderProps {
    icon: React.ReactNode;
    title: string;
    subtitle?: string;
    /** Optional right-side controls (filters, refresh button). 04.3 wires action buttons here. */
    right?: React.ReactNode;
}

export const EosPageHeader: React.FC<EosPageHeaderProps> = ({ icon, title, subtitle, right }) => (
    <header className="mb-6 flex items-start justify-between gap-4" data-tour="eos-page-header">
        <div className="flex items-center gap-3">
            <div className="bg-brand text-white p-2.5 rounded-md flex items-center justify-center">
                {icon}
            </div>
            <div>
                <h1 className="text-2xl font-semibold text-gray-900">{title}</h1>
                {subtitle && <p className="text-sm text-gray-500 mt-1">{subtitle}</p>}
            </div>
        </div>
        {right && <div className="flex items-center gap-2">{right}</div>}
    </header>
);

// ─── State pieces (loading / error / empty) ─────────────────────────────────

export const LoadingState: React.FC<{ label?: string }> = ({ label }) => (
    <div className="flex items-center justify-center py-16 text-gray-500 gap-3">
        <Loader2 className="animate-spin" size={20} />
        <span className="text-sm">{label ?? 'Loading…'}</span>
    </div>
);

export const ErrorState: React.FC<{ message: string }> = ({ message }) => (
    <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3">
        <AlertCircle className="text-red-500 flex-shrink-0 mt-0.5" size={18} />
        <div>
            <p className="text-sm font-medium text-red-900">Failed to load.</p>
            <p className="text-xs text-red-700 mt-1">{message}</p>
        </div>
    </div>
);

export const EmptyState: React.FC<{ title: string; hint?: string }> = ({ title, hint }) => (
    <div className="bg-white rounded-lg border border-gray-200 p-12 text-center">
        <p className="text-base font-medium text-gray-700">{title}</p>
        {hint && <p className="text-sm text-gray-500 mt-2">{hint}</p>}
    </div>
);

/**
 * Standard EOS page frame.
 *
 * `wide` raises the cap for pages whose content is a WIDE TABLE rather than
 * prose. The Scorecard is 133rem of grid at 13 weeks; capping it at max-w-6xl
 * (72rem) meant a 27" monitor showed no more of it than a laptop, which is what
 * "can we expand the right border on a larger screen?" was about. The cap is
 * kept (rather than dropped entirely) so the page header and dialogs don't
 * stretch across an ultrawide; 2200px is the full grid plus its padding.
 */
export const PageContainer: React.FC<{
    children: React.ReactNode;
    wide?: boolean;
}> = ({ children, wide = false }) => (
    <div className={`${wide ? 'max-w-[2200px]' : 'max-w-6xl'} mx-auto p-8`}>{children}</div>
);

// ─── Small typed badges (status, priority, visibility) ──────────────────────

export const StatusBadge: React.FC<{ value: string; tone?: 'green' | 'amber' | 'blue' | 'red' | 'gray' }> = ({ value, tone = 'gray' }) => {
    const toneClasses: Record<string, string> = {
        green: 'bg-green-50 text-green-700 border-green-200',
        amber: 'bg-amber-50 text-amber-700 border-amber-200',
        blue: 'bg-blue-50 text-blue-700 border-blue-200',
        red: 'bg-red-50 text-red-700 border-red-200',
        gray: 'bg-gray-100 text-gray-700 border-gray-200',
    };
    return (
        <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border ${toneClasses[tone]}`}>
            {value}
        </span>
    );
};

// ─── Team filter (Exec-team confidentiality) ─────────────────────────────────
//
// One dropdown with the same semantics on every EOS list page: 'all' = every
// team the caller can see, '__exec__' = the Exec/Leadership team, else a
// department name. The scopes come from /api/eos/me/meeting-scopes, which
// only offers the Exec option to Exec members — and the server enforces the
// boundary regardless, so the dropdown is a view, not the gate. Hidden for
// single-team users (nothing to filter).

export const TeamFilter: React.FC<{
    scopes: TeamScope[];
    value: string;
    onChange: (v: string) => void;
    allLabel?: string;
}> = ({ scopes, value, onChange, allLabel = 'All my teams' }) => {
    if (scopes.length < 2) return null;
    return (
        <select
            value={value}
            onChange={e => onChange(e.target.value)}
            className="border border-gray-300 rounded-md text-sm px-2 py-2 bg-white text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand/30"
            aria-label="Filter by team"
            title="Filter by team"
        >
            <option value="all">{allLabel}</option>
            {scopes.map(s => (
                <option key={s.value} value={s.value}>
                    {s.label}
                </option>
            ))}
        </select>
    );
};

// Violet "Exec" pill marking confidential Exec-team items (matches the badge
// on the Exec L10 meeting card).
export const ExecBadge: React.FC = () => (
    <span className="text-[10px] font-bold uppercase tracking-wider bg-brand text-white px-2 py-0.5 rounded-full">
        Exec
    </span>
);

// ─── KPI summary strip (shared across the three EOS list pages) ──────────────
//
// A responsive grid of small stat tiles rendered above each page's list so all
// three EOS tools open with the same rhythm. Values are always the CURRENT
// figure only — we have no historical baseline, so no trend arrows/deltas.

// Full literal class strings so Tailwind's compiler can see every column count
// (interpolated fragments would be purged from the build).
const KPI_STRIP_COLS: Record<2 | 3 | 4, string> = {
    2: 'md:grid-cols-2',
    3: 'md:grid-cols-3',
    4: 'md:grid-cols-4',
};

export const KpiStrip: React.FC<{ children: React.ReactNode; cols?: 2 | 3 | 4 }> = ({
    children,
    cols = 4,
}) => (
    <div className={`grid grid-cols-2 ${KPI_STRIP_COLS[cols]} gap-3 mb-6`}>{children}</div>
);

export const KpiCard: React.FC<{
    label: string;
    value: React.ReactNode;
    hint?: string;
    leading?: React.ReactNode;
    valueClass?: string;
}> = ({ label, value, hint, leading, valueClass }) => (
    <div
        className="bg-white border border-gray-200 rounded-lg px-4 py-3.5 shadow-sm flex items-center gap-3.5"
        title={hint}
    >
        {leading}
        <div>
            <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                {label}
            </div>
            <div
                className={`text-2xl font-bold tracking-tight text-gray-900 leading-tight mt-0.5 ${valueClass ?? ''}`}
            >
                {value}
            </div>
        </div>
    </div>
);

// Small SVG progress ring. `value` is a 0..100 percentage; the arc starts at
// the top (rotated -90°) and fills clockwise.
export const Donut: React.FC<{
    value: number;
    color: string;
    track?: string;
    size?: number;
    strokeWidth?: number;
}> = ({ value, color, track = '#e5e7eb', size = 46, strokeWidth = 7 }) => {
    const clamped = Math.max(0, Math.min(100, value));
    const r = (size - strokeWidth) / 2;
    const c = 2 * Math.PI * r;
    const filled = (clamped / 100) * c;
    return (
        <svg aria-hidden="true" width={size} height={size} className="flex-shrink-0">
            <circle
                cx={size / 2}
                cy={size / 2}
                r={r}
                fill="none"
                stroke={track}
                strokeWidth={strokeWidth}
            />
            <circle
                cx={size / 2}
                cy={size / 2}
                r={r}
                fill="none"
                stroke={color}
                strokeWidth={strokeWidth}
                strokeLinecap="round"
                strokeDasharray={filled + ' ' + (c - filled)}
                transform={'rotate(-90 ' + size / 2 + ' ' + size / 2 + ')'}
            />
        </svg>
    );
};

// Avatar (initials) + name chip for showing an item's owner on a list row.
export const OwnerChip: React.FC<{ name: string; size?: 'sm' | 'md' }> = ({ name, size = 'sm' }) => {
    const initials =
        name
            .trim()
            .split(/\s+/)
            .filter(Boolean)
            .slice(0, 2)
            .map(w => w[0]?.toUpperCase() ?? '')
            .join('') || '?';
    const circleClass =
        size === 'md'
            ? 'w-7 h-7 rounded-full bg-brand-tint text-brand inline-flex items-center justify-center text-[10px] font-bold tracking-wide flex-shrink-0'
            : 'w-6 h-6 rounded-full bg-brand-tint text-brand inline-flex items-center justify-center text-[9px] font-bold tracking-wide flex-shrink-0';
    return (
        <span
            className={`inline-flex items-center gap-2 ${size === 'md' ? 'text-sm' : 'text-xs'} text-gray-600 font-medium`}
        >
            <span className={circleClass}>{initials}</span>
            {name}
        </span>
    );
};

// Shared types + utilities live in `./_utils` (kept separate so this file
// can satisfy `react-refresh/only-export-components`).
