import React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';

interface BackButtonProps {
    /**
     * Where to send the user when there is no in-app history to pop — i.e. the
     * page was opened via a hard refresh, a bookmarked deep link, or a new tab.
     * Should be the natural parent of this detail view (e.g. the list/tab it is
     * usually reached from).
     */
    fallback: string;
    /** Button label. Defaults to "Back". */
    label?: string;
    /** Extra classes appended to the default styling. */
    className?: string;
}

/**
 * Back control for drill-in detail pages.
 *
 * The live app chrome (components/Layout.tsx) has only a coarse left sidebar —
 * no top bar, breadcrumb, or global back button. So any page a user reaches by
 * clicking INTO something (Meeting Prep, Customer X-Ray, an L10 session, …) has
 * to offer its own way back, or the user is stranded on the detail view with no
 * way to return to the exact list/tab/scroll position they came from.
 *
 * Behaviour:
 *  - If there is SPA history to pop (the common case — the user navigated here
 *    from within the app), go back one entry. This returns them to precisely
 *    where they were, regardless of which entry point they used.
 *  - Otherwise (react-router marks the first entry's key as "default"), route to
 *    `fallback` so we never dump the user out of the app or leave them stuck.
 */
export const BackButton: React.FC<BackButtonProps> = ({ fallback, label = 'Back', className }) => {
    const navigate = useNavigate();
    const location = useLocation();
    // react-router assigns a unique key to every navigation; the very first
    // entry on a fresh page load keeps the sentinel key "default". So a non-
    // "default" key means there is an in-app entry we can safely pop back to.
    const canGoBack = location.key !== 'default';

    return (
        <button
            type="button"
            onClick={() => (canGoBack ? navigate(-1) : navigate(fallback))}
            className={
                'inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-50 hover:text-slate-900' +
                (className ? ` ${className}` : '')
            }
        >
            <ArrowLeft size={16} />
            {label}
        </button>
    );
};

export default BackButton;
