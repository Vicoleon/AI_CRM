// What the Meeting Prep page shows while Cubie builds a brief in the
// background.
//
// * `BuildingPanel` — the first time, when there is no version to show yet.
//   Says what Cubie is doing, how long it has been at it, and the one thing
//   the reader most needs to hear: they do not have to wait here.
// * `BuildBanner` — a Regenerate: the current version stays on screen and a
//   banner says a new one is on its way (or, once it has landed while the
//   reader had an unsaved edit open, offers to show it).
//
// The bar is time-based, not real progress — the server reports no steps —
// so it eases toward full over the usual minute or two and never claims done.
// The elapsed clock sits outside the live region: a screen reader hears the
// status change, not a tick every second.
import React, { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
    ArrowLeft,
    Bell,
    Building2,
    Calendar,
    CheckCircle2,
    FolderOpen,
    History,
    Loader2,
    Mail,
    Sparkles,
    X,
} from 'lucide-react';

import {
    LEAVE_MESSAGE,
    TYPICAL_DURATION,
    buildStateLabel,
    formatElapsed,
    waitingBarFraction,
    type BuildState,
} from '../../pages/meetingPrep/build';
import { instantMs } from '../../utils/datetime';

/** Milliseconds since `startedAt` (the server's clock), ticking every second.
 *  Null until the first tick. Falls back to "since this mounted" when the
 *  server sent no start time. */
function useElapsed(startedAt: string | null | undefined): number | null {
    const [clock, setClock] = useState<{ mountedAt: number; now: number } | null>(null);
    useEffect(() => {
        const tick = () => setClock(c => ({ mountedAt: c?.mountedAt ?? Date.now(), now: Date.now() }));
        const first = window.setTimeout(tick, 0);
        const every = window.setInterval(tick, 1_000);
        return () => {
            window.clearTimeout(first);
            window.clearInterval(every);
        };
    }, []);
    if (!clock) return null;
    const start = instantMs(startedAt) ?? clock.mountedAt;
    return Math.max(0, clock.now - start);
}

const SOURCES = [
    { Icon: Mail, label: 'Email' },
    { Icon: Calendar, label: 'Calendar' },
    { Icon: FolderOpen, label: 'Drive' },
    { Icon: Building2, label: 'HubSpot' },
    { Icon: History, label: 'Past meetings' },
];

const WaitingBar: React.FC<{ elapsed: number | null; label: string }> = ({ elapsed, label }) => (
    <div
        role="progressbar"
        aria-label={label}
        className="relative h-2 w-full overflow-hidden rounded-full bg-brand/10"
    >
        <div
            className="absolute inset-y-0 left-0 rounded-full bg-brand transition-[width] duration-1000 ease-linear motion-reduce:transition-none"
            style={{ width: `${Math.round(waitingBarFraction(elapsed) * 100)}%` }}
        />
        <div className="absolute inset-0 animate-pulse bg-white/20 motion-reduce:animate-none" aria-hidden="true" />
    </div>
);

/** The first build: nothing to read yet, and no reason to wait for it here. */
export const BuildingPanel: React.FC<{
    title?: string | null;
    status: BuildState;
    startedAt: string | null;
}> = ({ title, status, startedAt }) => {
    const elapsed = useElapsed(startedAt);
    const navigate = useNavigate();
    const location = useLocation();
    // Same rule as BackButton: pop back to wherever they came from (the
    // Daily Briefing or the Productivity calendar); a fresh tab goes to the
    // calendar.
    const back = () => (location.key !== 'default' ? navigate(-1) : navigate('/productivity?tab=calendar'));

    return (
        <section
            aria-labelledby="meeting-prep-building-title"
            className="relative mx-auto mt-6 max-w-2xl overflow-hidden rounded-[2rem] border border-slate-200/60 bg-white/80 p-8 shadow-2xl shadow-slate-200/50 backdrop-blur-xl sm:p-10"
        >
            <div className="absolute left-0 top-0 h-full w-2 bg-brand" aria-hidden="true" />

            <div className="flex items-center gap-2.5 text-[11px] font-bold uppercase tracking-[0.2em] text-brand">
                <div className="h-[1px] w-8 bg-brand/30" />
                <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                Meeting Prep
            </div>
            <h1
                id="meeting-prep-building-title"
                className="mt-3 text-3xl font-black leading-tight tracking-tight text-slate-900"
            >
                Cubie is building your briefing
            </h1>
            {title && <p className="mt-1.5 text-base font-semibold text-slate-600">{title}</p>}

            <div className="mt-7 space-y-2.5">
                <div className="flex items-center justify-between gap-3 text-xs font-semibold">
                    <span role="status" className="inline-flex items-center gap-1.5 text-brand">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                        {buildStateLabel(status)}
                        {status === 'queued' ? ' — starting in a moment' : ''}
                    </span>
                    <span className="tabular-nums text-slate-500" aria-hidden="true">
                        {elapsed === null ? '' : `${formatElapsed(elapsed)} elapsed`}
                    </span>
                </div>
                <WaitingBar elapsed={elapsed} label="Building your briefing" />
                <p className="text-xs text-slate-400">{TYPICAL_DURATION}</p>
            </div>

            <div className="mt-7">
                <p className="text-sm leading-relaxed text-slate-600">
                    Cubie is reading what you have with these attendees, then writing a briefing you can edit:
                </p>
                <ul className="mt-3 flex flex-wrap gap-2">
                    {SOURCES.map(({ Icon, label }) => (
                        <li
                            key={label}
                            className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold text-slate-600"
                        >
                            <Icon className="h-3.5 w-3.5 text-brand" aria-hidden="true" />
                            {label}
                        </li>
                    ))}
                </ul>
            </div>

            <div className="mt-7 flex items-start gap-3 rounded-2xl border border-brand/15 bg-brand/5 px-4 py-3.5">
                <Bell className="mt-0.5 h-4 w-4 shrink-0 text-brand" aria-hidden="true" />
                <p className="text-sm font-semibold leading-snug text-brand">{LEAVE_MESSAGE}</p>
            </div>

            <div className="mt-7 flex flex-wrap gap-3">
                <button
                    type="button"
                    onClick={back}
                    className="inline-flex items-center gap-2 rounded-xl bg-brand px-5 py-2.5 text-sm font-bold text-white shadow-lg shadow-brand/20 transition-colors hover:bg-brand-hover focus:outline-none focus-visible:ring-4 focus-visible:ring-brand/20"
                >
                    <ArrowLeft className="h-4 w-4" aria-hidden="true" />
                    Back to calendar
                </button>
            </div>
        </section>
    );
};

/** A Regenerate in flight (or landed while an edit was open). */
export const BuildBanner: React.FC<
    | { state: 'building'; status: BuildState; startedAt: string | null }
    | {
          state: 'ready';
          version: number | null;
          /** Why it was not switched to on its own (an unsaved edit, an older
           *  version on screen). */
          note?: string | null;
          onShow: () => void;
          onDismiss: () => void;
      }
> = props => {
    if (props.state === 'ready') {
        const v = props.version ? `v${props.version}` : 'The new version';
        return (
            <div className="no-print flex flex-wrap items-center gap-3 rounded-2xl border border-emerald-200 bg-emerald-50 px-5 py-3 text-sm text-emerald-900">
                <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" aria-hidden="true" />
                <span role="status" className="min-w-0">
                    <strong>{v} is ready.</strong>
                    {props.note ? ` ${props.note}` : ''}
                </span>
                <div className="ml-auto flex items-center gap-2">
                    <button
                        type="button"
                        onClick={props.onShow}
                        className="inline-flex items-center gap-1.5 rounded-xl bg-brand px-3.5 py-2 text-xs font-bold text-white transition-colors hover:bg-brand-hover"
                    >
                        Show {props.version ? `v${props.version}` : 'it'}
                    </button>
                    <button
                        type="button"
                        onClick={props.onDismiss}
                        aria-label="Dismiss"
                        className="rounded-lg p-1.5 text-emerald-700 transition-colors hover:bg-emerald-100"
                    >
                        <X className="h-4 w-4" />
                    </button>
                </div>
            </div>
        );
    }
    return <BuildingBanner status={props.status} startedAt={props.startedAt} />;
};

const BuildingBanner: React.FC<{ status: BuildState; startedAt: string | null }> = ({ status, startedAt }) => {
    const elapsed = useElapsed(startedAt);
    return (
        <div className="no-print rounded-2xl border border-brand/20 bg-brand/5 px-5 py-3 text-sm text-brand">
            <div className="flex flex-wrap items-center gap-3">
                <Loader2 className="h-4 w-4 shrink-0 animate-spin" aria-hidden="true" />
                <span role="status" className="min-w-0 flex-1 font-medium">
                    Building a new version in the background — you can keep reading or leave; Cubie will notify
                    you.
                </span>
                <span className="text-xs font-semibold tabular-nums text-brand/70" aria-hidden="true">
                    {status === 'queued' ? 'Queued' : elapsed === null ? '' : formatElapsed(elapsed)}
                </span>
            </div>
            <div className="mt-2.5">
                <WaitingBar elapsed={elapsed} label="Building a new version" />
            </div>
        </div>
    );
};
