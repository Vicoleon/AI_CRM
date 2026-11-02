import React from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle, Building2, Hourglass, Info, Lock, PowerOff, RefreshCw, SearchX } from 'lucide-react';
import type { InsightsScope } from '../../api/insights';
import { plural } from '../../pages/insights/insightsFormat';

// The designed states every Insights page shares: switched off, not for you,
// coming soon, not found, and a calm error with a retry. None of them is a
// red wall for something the reader can't fix.

const CARD = 'mx-auto max-w-xl rounded-lg border border-gray-200 bg-white px-6 py-10 text-center shadow-sm';

const ICON_WRAP = 'mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-full';

const ACTION_PRIMARY =
    'inline-flex items-center gap-1.5 rounded-md bg-brand px-3.5 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2';
const ACTION_SECONDARY =
    'inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3.5 py-2 text-sm font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30';

export const StateCard: React.FC<{
    icon: React.ReactNode;
    tone?: 'brand' | 'gray' | 'amber';
    title: string;
    children?: React.ReactNode;
    actions?: React.ReactNode;
}> = ({ icon, tone = 'gray', title, children, actions }) => (
    <div className={CARD} role="status">
        <div
            className={`${ICON_WRAP} ${
                tone === 'brand' ? 'bg-brand-tint text-brand' : tone === 'amber' ? 'bg-amber-50 text-amber-600' : 'bg-gray-100 text-gray-500'
            }`}
            aria-hidden="true"
        >
            {icon}
        </div>
        <h2 className="text-base font-semibold text-gray-900">{title}</h2>
        {children && <div className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-gray-600">{children}</div>}
        {actions && <div className="mt-5 flex flex-wrap items-center justify-center gap-2">{actions}</div>}
    </div>
);

/** The kill switch is off (every endpoint 404s). Nothing to retry. */
export const NotEnabledState: React.FC = () => (
    <StateCard icon={<PowerOff size={20} />} title="Insights isn’t switched on yet">
        A Cubie admin turns the dashboards on. Nothing is wrong with your account, and there is nothing
        to retry.
    </StateCard>
);

/** A 403: the feature, or the department asked for, is not this reader's. */
export const ForbiddenState: React.FC<{
    message?: string | null;
    /** A department filter is set; offer to drop it. */
    onClearDepartments?: (() => void) | null;
    /** The person profile has its own sentence. */
    personView?: boolean;
}> = ({ message, onClearDepartments, personView = false }) => (
    <StateCard
        icon={<Lock size={20} />}
        tone="amber"
        title={
            personView
                ? 'Person profiles need the people view'
                : onClearDepartments
                  ? 'That department is outside what you can see'
                  : 'Insights isn’t open to you yet'
        }
        actions={
            onClearDepartments ? (
                <button type="button" onClick={onClearDepartments} className={ACTION_PRIMARY}>
                    <Building2 size={15} aria-hidden="true" />
                    Show all my departments
                </button>
            ) : personView ? (
                <Link to="/insights" className={ACTION_SECONDARY}>
                    Back to Insights
                </Link>
            ) : null
        }
    >
        {personView ? (
            <>
                Opening one person&apos;s numbers is a separate permission from the dashboards. A Cubie admin
                can grant it in Admin → Feature Access.
            </>
        ) : onClearDepartments ? (
            <>
                Insights shows the departments you manage, plus your own. Admins and company-wide executives
                see every department.
            </>
        ) : (
            <>
                Insights opens to admins and executives first. A Cubie admin can add a department in Admin →
                Feature Access.
            </>
        )}
        {message && <p className="mt-2 text-xs text-gray-500">The server said: {message}</p>}
    </StateCard>
);

export const ComingSoonState: React.FC<{ title: string; description?: string | null; overviewHref: string }> = ({
    title,
    description,
    overviewHref,
}) => (
    <StateCard
        icon={<Hourglass size={20} />}
        tone="brand"
        title={`${title} is coming soon`}
        actions={
            <Link to={overviewHref} className={ACTION_SECONDARY}>
                Go to the Overview
            </Link>
        }
    >
        {description && <p>{description}</p>}
        <p className={description ? 'mt-2' : ''}>
            This dashboard ships in a later release. The live dashboards are listed in the panel on the left.
        </p>
    </StateCard>
);

export const NotFoundState: React.FC<{ title: string; children?: React.ReactNode; href: string; hrefLabel: string }> = ({
    title,
    children,
    href,
    hrefLabel,
}) => (
    <StateCard
        icon={<SearchX size={20} />}
        title={title}
        actions={
            <Link to={href} className={ACTION_SECONDARY}>
                {hrefLabel}
            </Link>
        }
    >
        {children}
    </StateCard>
);

export const ErrorRetryState: React.FC<{ message: string; onRetry: () => void; onReset?: (() => void) | null }> = ({
    message,
    onRetry,
    onReset,
}) => (
    <div className="mx-auto max-w-xl rounded-lg border border-red-200 bg-red-50/60 px-5 py-4" role="alert">
        <div className="flex items-start gap-3">
            <AlertCircle size={18} className="mt-0.5 flex-shrink-0 text-red-500" aria-hidden="true" />
            <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-red-900">The numbers couldn&apos;t be loaded.</p>
                <p className="mt-1 text-sm text-red-800/90">{message}</p>
                <div className="mt-3 flex flex-wrap gap-2">
                    <button type="button" onClick={onRetry} className={ACTION_SECONDARY}>
                        <RefreshCw size={14} aria-hidden="true" />
                        Try again
                    </button>
                    {onReset && (
                        <button type="button" onClick={onReset} className={ACTION_SECONDARY}>
                            Reset the filters
                        </button>
                    )}
                </div>
            </div>
        </div>
    </div>
);

/** Coverage caveats from the server ("Slack delivery is tracked from Oct 9"). */
export const NotesList: React.FC<{ notes: readonly string[] }> = ({ notes }) => {
    if (notes.length === 0) return null;
    return (
        <ul className="space-y-1.5" aria-label="Notes about this data">
            {notes.map((n, i) => (
                <li
                    key={i}
                    className="flex items-start gap-2 rounded-md border border-slate-200 bg-slate-50/80 px-3 py-2 text-[13px] leading-relaxed text-slate-600"
                >
                    <Info size={14} aria-hidden="true" className="mt-0.5 flex-shrink-0 text-slate-400" />
                    <span>{n}</span>
                </li>
            ))}
        </ul>
    );
};

/** Whose numbers these are: "All departments · 63 people", or the list. */
export const ScopeChips: React.FC<{ scope: InsightsScope }> = ({ scope }) => (
    <div className="flex flex-wrap items-center gap-1.5 text-xs" aria-label="Scope of these numbers">
        <Building2 size={13} aria-hidden="true" className="text-gray-400" />
        {scope.all || scope.labels.length === 0 ? (
            <span className="rounded-full bg-white px-2 py-0.5 font-medium text-gray-700 ring-1 ring-inset ring-gray-200">
                All departments
            </span>
        ) : (
            scope.labels.map(l => (
                <span key={l} className="rounded-full bg-brand-tint px-2 py-0.5 font-medium text-brand">
                    {l}
                </span>
            ))
        )}
        <span className="text-gray-500 tabular-nums">· {plural(scope.people, 'person', 'people')} in scope</span>
    </div>
);
