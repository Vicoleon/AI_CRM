// Shared chrome for the Smart Teams surfaces.
//
// Nothing here invents copy about what the profile means. The advisory framing,
// the privacy rule and every hedge are strings the instruments author
// (`disc.ADVISORY_NOTICE`, each layer's own refusal sentence) and the server
// hands over — a paraphrase written in the browser would be a promise nobody
// reviewed. Components take that text as a prop.
import { Link } from 'react-router-dom';
import React from 'react';
import { AlertTriangle, Check, Info, Loader2, Lock, RefreshCw } from 'lucide-react';

import type { SaveState } from './interviewFlow';
import { saveStateLabel } from './interviewFlow';

// ── Notices ──────────────────────────────────────────────────────────────────

/** The advisory-use notice, verbatim from the instrument modules.
 *
 *  It is on every Smart Teams screen on purpose: the plan calls advisory-only
 *  framing non-negotiable and states it must appear in-product, because a
 *  work-style profile that drifts into a performance record is the legal
 *  exposure the whole feature is built to avoid. */
export const AdvisoryNotice: React.FC<{ text?: string | null; className?: string }> = ({
    text,
    className,
}) => {
    // An empty amber box is worse than no box: it looks like a notice that says
    // nothing. The notice is server-supplied, so "missing" is a bug on the wire,
    // and the honest render is nothing at all plus a console line for whoever is
    // looking. (`test_smart_teams_api_contract.py` proves the real response
    // carries both notices, non-empty, so this branch should never fire.)
    if (!text || !text.trim()) {
        if (import.meta.env.DEV) {
            console.warn('[smart-teams] advisory notice missing from the API response');
        }
        return null;
    }
    return (
    <div
        className={`flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 p-3 ${className ?? ''}`}
    >
        <Info className="mt-0.5 flex-shrink-0 text-amber-600" size={16} />
        <p className="text-xs leading-relaxed text-amber-900">{text}</p>
    </div>
    );
};

/** Who can see what. Server-supplied for the same reason as the notice above. */
export const PrivacyNotice: React.FC<{ text?: string | null; className?: string }> = ({
    text,
    className,
}) => {
    if (!text || !text.trim()) {
        if (import.meta.env.DEV) {
            console.warn('[smart-teams] privacy notice missing from the API response');
        }
        return null;
    }
    return (
    <div
        className={`flex items-start gap-2.5 rounded-lg border border-gray-200 bg-gray-50 p-3 ${className ?? ''}`}
    >
        <Lock className="mt-0.5 flex-shrink-0 text-gray-500" size={16} />
        <p className="text-xs leading-relaxed text-gray-600">{text}</p>
    </div>
    );
};

// ── Save state ───────────────────────────────────────────────────────────────

/** The quiet "your answers are on the server" affordance.
 *
 *  Present because the alternative is a person who does not know whether it is
 *  safe to close the tab thirty items into a bank. */
export const SaveIndicator: React.FC<{ state: SaveState; unsavedCount: number }> = ({
    state,
    unsavedCount,
}) => {
    const label = saveStateLabel(state, unsavedCount);
    const tone =
        state === 'failed'
            ? 'text-red-700'
            : state === 'saving'
              ? 'text-gray-500'
              : 'text-green-700';
    return (
        <span
            className={`inline-flex items-center gap-1.5 text-xs font-medium ${tone}`}
            aria-live="polite"
        >
            {state === 'saving' ? (
                <Loader2 className="animate-spin" size={13} />
            ) : state === 'failed' ? (
                <AlertTriangle size={13} />
            ) : (
                <Check size={13} />
            )}
            {label}
        </span>
    );
};

/** A save that did not land, said out loud with a way to fix it.
 *
 *  `role="alert"` rather than a toast: a toast that auto-dismisses turns
 *  "your last nine answers are not on the server" into something a person can
 *  miss, and this is the one failure on this page that must never be quiet. */
export const UnsavedBanner: React.FC<{
    unsavedCount: number;
    message: string;
    onRetry: () => void;
    retrying: boolean;
}> = ({ unsavedCount, message, onRetry, retrying }) => (
    <div
        role="alert"
        className="mb-4 flex flex-col gap-3 rounded-lg border border-red-200 bg-red-50 p-4 sm:flex-row sm:items-center sm:justify-between"
    >
        <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 flex-shrink-0 text-red-500" size={18} />
            <div>
                <p className="text-sm font-medium text-red-900">
                    {unsavedCount === 1
                        ? "1 answer hasn't reached Cubie yet."
                        : `${unsavedCount} answers haven't reached Cubie yet.`}
                </p>
                <p className="mt-1 text-xs text-red-700">{message}</p>
                <p className="mt-1 text-xs text-red-700">
                    They are still on this page and in your browser's local copy — keep this
                    tab open and retry, or sign in again if you were signed out.
                </p>
            </div>
        </div>
        <button
            type="button"
            onClick={onRetry}
            disabled={retrying}
            className="inline-flex flex-shrink-0 items-center gap-2 rounded-md border border-red-300 bg-white px-3 py-2 text-sm font-medium text-red-800 hover:bg-red-100 disabled:opacity-60"
        >
            {retrying ? (
                <Loader2 className="animate-spin" size={14} />
            ) : (
                <RefreshCw size={14} />
            )}
            Retry now
        </button>
    </div>
);

// ── Progress ─────────────────────────────────────────────────────────────────

/** The interview header: which section, how far in, how much is left.
 *
 *  `percent` is the server's time-weighted figure (`interview.Progress`), not a
 *  count of sections — nine sections range from 75 seconds to ten minutes, so a
 *  section-weighted bar would tell a person half-way through the 50-item Big
 *  Five bank that they had not moved. */
export const ProgressHeader: React.FC<{
    percent: number;
    stepsDone: number;
    stepsTotal: number;
    minutesRemaining: number;
    right?: React.ReactNode;
}> = ({ percent, stepsDone, stepsTotal, minutesRemaining, right }) => (
    <div className="mb-5">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-gray-500">
                <span className="font-medium text-gray-700">{percent}%</span> through ·{' '}
                {stepsDone} of {stepsTotal} answers ·{' '}
                {minutesRemaining <= 1 ? 'under a minute left' : `about ${minutesRemaining} min left`}
            </p>
            {right}
        </div>
        <div
            className="h-2 w-full overflow-hidden rounded-full bg-gray-200"
            role="progressbar"
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Interview progress"
        >
            <div
                className="h-full rounded-full bg-brand transition-all duration-300"
                style={{ width: `${Math.min(Math.max(percent, 0), 100)}%` }}
            />
        </div>
    </div>
);

// ── Profile rendering: deliberately absent ───────────────────────────────────
//
// There is no LayerCard, no ConfidenceBadge and no DerivedType component here
// any more, and that is a statement about the API rather than a deletion of
// working code. `/interview` and `/interview/status` return a profile VERSION
// stub — id, version, status, origin, scored_at, provenance — and nothing that
// summarises a framework. So there is nothing to render a layer from, and the
// previous components rendered fields (`layers`, `headline`, `letters`,
// `qualifier`, `hedge`) that no endpoint sends.
//
// When ST-5 publishes a layer-summary endpoint, two rules come back with it: no
// raw score reaches a non-subject viewer, and a four-letter type is never
// rendered without the qualifier that says it is derived and not an MBTI result
// (`type16.NOT_AN_MBTI_RESULT`). Until then no field in `api/smartTeams.ts`
// carries either, which is the strongest form of that guarantee available.

// ── Small building blocks ────────────────────────────────────────────────────

/** A working screen this section owns, as a card on the section index.
 *
 *  The counterpart of {@link ComingSoon}, and it exists because the absence of
 *  it caused a real problem: the landing page kept rendering ComingSoon cards
 *  after the pages shipped, so the sidebar linked four working screens while the
 *  section's own front door said the guide generator was not switched on. A
 *  person finished the 30-minute interview, landed there, and concluded nothing
 *  had been built.
 *
 *  Solid border and darker text rather than ComingSoon's dashed grey, so the two
 *  states are distinguishable at a glance instead of by reading the badge. */
export const SectionLink: React.FC<{
    to: string;
    icon: React.ReactNode;
    title: string;
    children: React.ReactNode;
}> = ({ to, icon, title, children }) => (
    <Link
        to={to}
        className="block rounded-lg border border-gray-200 bg-white p-5 transition hover:border-brand/40 hover:bg-brand/[0.02] focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
    >
        <div className="mb-1.5 flex items-center gap-2">
            <span className="text-brand">{icon}</span>
            <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        </div>
        <div className="space-y-2 text-sm leading-relaxed text-gray-600">{children}</div>
    </Link>
);

/** A feature that is planned but not built. Says so plainly, with the reason,
 *  and offers no button. Everywhere this appears, the Help page says the same
 *  thing — an unbuilt feature described as available is worse than a gap. */
export const ComingSoon: React.FC<{
    icon: React.ReactNode;
    title: string;
    children: React.ReactNode;
}> = ({ icon, title, children }) => (
    <div className="rounded-lg border border-dashed border-gray-300 bg-white p-5">
        <div className="mb-1.5 flex items-center gap-2">
            <span className="text-gray-400">{icon}</span>
            <h3 className="text-sm font-semibold text-gray-700">{title}</h3>
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-600">
                Coming soon
            </span>
        </div>
        <div className="space-y-2 text-sm leading-relaxed text-gray-500">{children}</div>
    </div>
);

export const PrimaryButton: React.FC<{
    onClick?: () => void;
    disabled?: boolean;
    busy?: boolean;
    type?: 'button' | 'submit';
    children: React.ReactNode;
}> = ({ onClick, disabled, busy, type = 'button', children }) => (
    <button
        type={type}
        onClick={onClick}
        disabled={disabled || busy}
        aria-disabled={disabled || busy}
        className="inline-flex items-center justify-center gap-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
    >
        {busy && <Loader2 className="animate-spin" size={15} />}
        {children}
    </button>
);

export const SecondaryButton: React.FC<{
    onClick?: () => void;
    disabled?: boolean;
    busy?: boolean;
    children: React.ReactNode;
}> = ({ onClick, disabled, busy, children }) => (
    <button
        type="button"
        onClick={onClick}
        disabled={disabled || busy}
        aria-disabled={disabled || busy}
        className="inline-flex items-center justify-center gap-2 rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
    >
        {busy && <Loader2 className="animate-spin" size={15} />}
        {children}
    </button>
);
