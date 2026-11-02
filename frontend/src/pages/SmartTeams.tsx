// ST-3 — the Smart Teams landing page: "where am I with my work-style profile?"
//
// FOUR STATES, AND THE PAGE MUST NOT BLUR THEM
// --------------------------------------------
//   never started        → what this is, how long it honestly takes, start
//   part-way through     → resume, on the exact section they stopped at
//   answered, scoring    → a truthful in-progress state, polled
//   scored               → the profile version that came back, and a retake
//
// The one that is easy to get wrong is the third. Scoring runs on the
// cubie-worker Cloud Run Job (Cloud Run throttles CPU after the response, so
// fire-and-forget on the service dies silently), which means there is a real
// window where the answers are in and the profile is not. Rendering an empty
// profile there would read as "the interview lost my answers", so the page polls
// and says what is actually happening.
//
// THE SCORING VOCABULARY IS THE ROUTER'S, NOT A GUESS
// ---------------------------------------------------
// `scoring.status` is one of `not_started | pending | delayed | ready`. There is
// no `queued`, no `running` and no `failed`: `st_interviews` has no failure
// column, so a run that never came back is reported as `delayed` BY AGE, with
// the copy to show in `scoring.message`. The previous cut of this page polled for
// `state === 'running'` and branched on `state === 'failed'` — two fields and
// three values that do not exist — which is an infinite spinner for anybody whose
// scoring run dies. `delayed` is now a visible, actionable card.
//
// There is no overview endpoint: this page is `GET /interview` plus the cheap
// `GET /interview/status` poll. Nothing here renders a raw score or a four-letter
// type, because no response it reads carries either.
//
// Polling, not push: 5s with a hard ceiling, the same shape as CRM Sync and
// Customer X-Ray — no SSE/WS, which would hold a database connection open.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    AlertTriangle,
    ArrowRight,
    CheckCircle2,
    ClipboardList,
    FileText,
    Loader2,
    PlayCircle,
    RefreshCw,
    ShieldCheck,
    Sparkles,
    Users,
    Wrench,
} from 'lucide-react';

import { useAuth } from '../context/AuthContext';
import { formatInstantDate } from '../utils/datetime';
import { confirmDialog } from '../components/eos/ConfirmDialog';
import { toast } from '../components/eos/Toast';
import { EmptyState, ErrorState, LoadingState } from './eos/_shared';
import {
    AdvisoryNotice,
    SectionLink,
    PrimaryButton,
    PrivacyNotice,
    SecondaryButton,
} from '../components/smartTeams/shared';
import type { InterviewView } from '../api/smartTeams';
import {
    abandonInterview,
    getInterview,
    getInterviewStatus,
    retakeInterview,
    smartTeamsError,
    startInterview,
} from '../api/smartTeams';

/** 5s, like every other polled surface here. */
const POLL_MS = 5000;
/** ~7.5 minutes. A scoring run that has not finished by then is a job to chase,
 *  not something to keep asking about — the page says so and offers a refresh
 *  rather than spinning for ever. */
const MAX_POLLS = 90;

const Card: React.FC<{ children: React.ReactNode; className?: string }> = ({
    children,
    className,
}) => (
    <div className={`rounded-lg border border-gray-200 bg-white p-5 ${className ?? ''}`}>
        {children}
    </div>
);

export const SmartTeams: React.FC = () => {
    const navigate = useNavigate();
    // Both flags already exist in AuthContext — the page reads them and never
    // re-derives a gate of its own.
    const { canAccessFullApp, canViewSmartTeamsRawData } = useAuth();

    const [state, setState] = useState<InterviewView | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [denied, setDenied] = useState(false);
    const [busy, setBusy] = useState(false);
    const [pollTimedOut, setPollTimedOut] = useState(false);

    const pollRef = useRef<number | null>(null);
    const pollsRef = useRef(0);

    const load = useCallback(async (): Promise<void> => {
        try {
            const next = await getInterview();
            setState(next);
            setError(null);
            setDenied(false);
        } catch (e) {
            const err = smartTeamsError(e, 'Cubie could not load your Smart Teams page.');
            // A 403 never reaches the axios interceptor's redirect (only a 401
            // does), so the denial state is ours to render.
            if (err.status === 403) setDenied(true);
            else setError(err.message);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    const stopPolling = useCallback(() => {
        if (pollRef.current) {
            window.clearInterval(pollRef.current);
            pollRef.current = null;
        }
    }, []);

    // `pending` is the ONLY in-flight value. Poll while it holds, stop the
    // instant it does not — including on unmount, so leaving the page does not
    // leak an interval.
    const scoringPhase =
        state && state.status === 'completed' ? state.scoring.status : undefined;
    useEffect(() => {
        if (scoringPhase !== 'pending') {
            stopPolling();
            return;
        }
        if (pollRef.current) return;
        pollsRef.current = 0;
        setPollTimedOut(false);
        pollRef.current = window.setInterval(async () => {
            pollsRef.current += 1;
            try {
                const status = await getInterviewStatus();
                if (status.status !== 'not_started' && status.scoring.status !== 'pending') {
                    // ready or delayed: either way this is terminal for the poll,
                    // and a full re-read gets the profile stub with it.
                    stopPolling();
                    await load();
                    return;
                }
            } catch {
                // Keep polling through a transient blip; the ceiling below is
                // what stops this, not one failed request.
            }
            if (pollsRef.current >= MAX_POLLS) {
                stopPolling();
                setPollTimedOut(true);
            }
        }, POLL_MS);
        return stopPolling;
    }, [load, scoringPhase, stopPolling]);

    useEffect(() => stopPolling, [stopPolling]);

    const start = useCallback(async () => {
        setBusy(true);
        try {
            await startInterview();
            navigate('/smart-teams/interview');
        } catch (e) {
            toast.error(smartTeamsError(e, 'Cubie could not open your interview.').message);
        } finally {
            setBusy(false);
        }
    }, [navigate]);

    /** Set this attempt aside and begin again. Two real calls — abandon, then
     *  start — because there is no "restart" endpoint and inventing one in the
     *  client is how the last cut of this file ended up calling a path that did
     *  not exist. The abandoned row is KEPT (status `abandoned`), so the copy
     *  below does not promise a deletion the server does not perform. */
    const startOver = useCallback(async () => {
        const confirmed = await confirmDialog({
            title: 'Start the interview again?',
            message:
                'Cubie sets this attempt aside and opens a fresh one from the first section — your earlier answers are kept on the old attempt but no longer count towards your profile. Any profile you have already published stays as it is.',
            destructive: true,
        });
        if (!confirmed) return;
        setBusy(true);
        try {
            await abandonInterview();
            await startInterview();
            navigate('/smart-teams/interview');
        } catch (e) {
            toast.error(smartTeamsError(e, 'Cubie could not restart your interview.').message);
        } finally {
            setBusy(false);
        }
    }, [navigate]);

    /** Take it again. A NEW interview row: the finished one and the profile it
     *  produced are left exactly as they are, and scoring the new one retires the
     *  old profile version by the writer's own rules. */
    const retake = useCallback(async () => {
        const confirmed = await confirmDialog({
            title: 'Take the interview again?',
            message:
                'This starts a new version. Your finished interview and the profile scored from it stay exactly as they are until the new one is scored.',
        });
        if (!confirmed) return;
        setBusy(true);
        try {
            await retakeInterview();
            navigate('/smart-teams/interview');
        } catch (e) {
            toast.error(smartTeamsError(e, 'Cubie could not start a retake.').message);
        } finally {
            setBusy(false);
        }
    }, [navigate]);

    if (loading) {
        return (
            <div className="mx-auto max-w-4xl p-8">
                <LoadingState label="Loading your work-style profile…" />
            </div>
        );
    }

    if (denied) {
        return (
            <div className="mx-auto max-w-4xl p-8">
                <EmptyState
                    title="Smart Teams isn't switched on for this account"
                    hint="It's open to everyone with a work account at your company. If that's you and you're seeing this, tell your Cubie admin."
                />
            </div>
        );
    }

    if (error || !state) {
        return (
            <div className="mx-auto max-w-4xl p-8 space-y-3">
                <ErrorState message={error ?? 'Nothing came back.'} />
                <SecondaryButton onClick={() => void load()}>
                    <RefreshCw size={14} />
                    Try again
                </SecondaryButton>
            </div>
        );
    }

    return (
        <div className="mx-auto max-w-4xl p-8">
            <header data-tour="smart-teams-header" className="mb-6 flex items-start gap-3">
                <div className="flex items-center justify-center rounded-md bg-brand p-2.5 text-white">
                    <Users size={20} />
                </div>
                <div>
                    <h1 className="text-2xl font-semibold text-gray-900">Smart Teams</h1>
                    <p className="mt-1 text-sm text-gray-500">
                        Your work-style profile — how you like to work, and how colleagues can
                        get the best out of working with you.
                    </p>
                </div>
            </header>

            <div className="space-y-4">
                {/* ── Never started ────────────────────────────────────────── */}
                {state.status === 'not_started' && (
                    <Card>
                        <h2 className="flex items-center gap-2 text-base font-semibold text-gray-900">
                            <ClipboardList className="text-brand" size={18} />
                            The Cubie Work Style Interview
                        </h2>
                        <p className="mt-2 text-sm leading-relaxed text-gray-700">
                            A mix of quick statements to rate and a short conversation in your
                            own words. Cubie turns it into a “How to work with me” guide that you
                            review, edit and approve before anyone else sees a word of it.
                        </p>
                        {/* The honest figure the server measured
                            (`duration_report().required_minutes`), not the target
                            somebody hoped for. */}
                        <p className="mt-2 text-sm text-gray-600">
                            It takes{' '}
                            <span className="font-medium text-gray-900">
                                about {Math.round(state.estimated_minutes)} minutes
                            </span>{' '}
                            end to end, in {state.sections.length} sections. You do not have to
                            do it in one sitting — it saves as you go and picks up exactly where
                            you stopped.
                        </p>
                        <div data-tour="smart-teams-start" className="mt-4">
                            <PrimaryButton onClick={() => void start()} busy={busy}>
                                <PlayCircle size={16} />
                                Start the interview
                            </PrimaryButton>
                        </div>
                    </Card>
                )}

                {/* ── Part-way through ─────────────────────────────────────── */}
                {state.status === 'in_progress' && (
                    <Card>
                        <div className="flex items-start justify-between gap-3">
                            <div>
                                <h2 className="flex items-center gap-2 text-base font-semibold text-gray-900">
                                    <ClipboardList className="text-brand" size={18} />
                                    {state.progress.is_complete
                                        ? 'Every section answered'
                                        : 'Interview in progress'}
                                </h2>
                                <p className="mt-1 text-sm text-gray-600">
                                    {state.progress.sections_done} of{' '}
                                    {state.progress.sections_total} sections done ·{' '}
                                    {state.progress.steps_done} of {state.progress.steps_total}{' '}
                                    answers ·{' '}
                                    {state.progress.minutes_remaining <= 1
                                        ? 'under a minute left'
                                        : `about ${state.progress.minutes_remaining} min left`}
                                    .
                                </p>
                            </div>
                            <span className="flex-shrink-0 text-2xl font-semibold text-brand">
                                {state.progress.percent}%
                            </span>
                        </div>
                        <div
                            className="mt-3 h-2 w-full overflow-hidden rounded-full bg-gray-200"
                            role="progressbar"
                            aria-valuenow={state.progress.percent}
                            aria-valuemin={0}
                            aria-valuemax={100}
                            aria-label="Interview progress"
                        >
                            <div
                                className="h-full rounded-full bg-brand"
                                style={{ width: `${state.progress.percent}%` }}
                            />
                        </div>
                        {state.progress.is_complete ? (
                            <p className="mt-3 text-sm leading-relaxed text-gray-700">
                                Nothing has been scored yet — your answers are saved and waiting
                                for you to send them.
                            </p>
                        ) : (
                            state.resume_section_key && (
                                <p className="mt-3 text-sm text-gray-600">
                                    You stopped in{' '}
                                    <span className="font-medium text-gray-900">
                                        {state.sections.find(
                                            s => s.key === state.resume_section_key,
                                        )?.title ?? 'the next section'}
                                    </span>
                                    . Cubie will put you back on the statement you were on.
                                </p>
                            )
                        )}
                        <div className="mt-4 flex flex-wrap items-center gap-2">
                            <PrimaryButton onClick={() => navigate('/smart-teams/interview')}>
                                {state.progress.is_complete
                                    ? 'Send my answers for scoring'
                                    : 'Pick up where I left off'}
                                <ArrowRight size={15} />
                            </PrimaryButton>
                            <SecondaryButton onClick={() => void startOver()} busy={busy}>
                                Start over
                            </SecondaryButton>
                        </div>
                    </Card>
                )}

                {/* ── Answered: scoring, or scored ─────────────────────────── */}
                {state.status === 'completed' && (
                    <>
                        {state.scoring.status === 'pending' && (
                            <Card className="border-brand-tint bg-brand-tint/40">
                                <h2 className="flex items-center gap-2 text-base font-semibold text-brand-hover">
                                    <Loader2 className="animate-spin" size={18} />
                                    Cubie is scoring your answers
                                </h2>
                                <p className="mt-2 text-sm leading-relaxed text-brand-hover">
                                    This runs in the background and takes a few minutes. You can
                                    leave this page and come back — nothing is lost, and your
                                    profile is not ready until this finishes.
                                </p>
                                {pollTimedOut && (
                                    <p className="mt-2 text-sm font-medium text-brand-hover">
                                        It is taking longer than expected. Cubie has stopped
                                        checking automatically — press Check now to look again,
                                        and tell your Cubie admin if it stays like this.
                                    </p>
                                )}
                                <div className="mt-4">
                                    <SecondaryButton onClick={() => void load()}>
                                        <RefreshCw size={14} />
                                        Check now
                                    </SecondaryButton>
                                </div>
                            </Card>
                        )}

                        {/* `delayed` is the only failure signal the schema can
                            carry — there is no failure column, so this is "it has
                            not come back and it has been a while", said plainly
                            in the server's own words rather than left spinning. */}
                        {state.scoring.status === 'delayed' && (
                            <Card className="border-amber-200 bg-amber-50">
                                <h2 className="flex items-center gap-2 text-base font-semibold text-amber-900">
                                    <AlertTriangle size={18} />
                                    Your profile hasn&apos;t come back yet
                                </h2>
                                <p className="mt-2 text-sm leading-relaxed text-amber-900">
                                    {state.scoring.message ??
                                        'Your answers are saved, but the scoring run has not returned a profile. Your Cubie admin can pick this up — nothing you did is lost.'}
                                </p>
                                <div className="mt-4">
                                    <SecondaryButton onClick={() => void load()}>
                                        <RefreshCw size={14} />
                                        Check again
                                    </SecondaryButton>
                                </div>
                            </Card>
                        )}

                        {state.scoring.status === 'not_started' && (
                            <Card className="border-amber-200 bg-amber-50">
                                <h2 className="text-base font-semibold text-amber-900">
                                    No scoring run recorded
                                </h2>
                                <p className="mt-2 text-sm leading-relaxed text-amber-900">
                                    Your interview is finished and your answers are saved, but
                                    Cubie has no scoring run against it. Tell your Cubie admin — you do
                                    not need to answer anything again.
                                </p>
                            </Card>
                        )}

                        {state.scoring.status === 'ready' && state.profile && (
                            <Card>
                                <h2 className="flex items-center gap-2 text-base font-semibold text-gray-900">
                                    <CheckCircle2 className="text-green-600" size={18} />
                                    Your profile is scored
                                </h2>
                                <p className="mt-2 text-sm leading-relaxed text-gray-700">
                                    Version {state.profile.version}
                                    {state.profile.scored_at
                                        ? `, scored ${formatInstantDate(state.profile.scored_at)}`
                                        : ''}
                                    . What it says about you is written into the “How to work
                                    with me” guide, which is yours to review and approve before
                                    anybody else can read it.
                                </p>
                                <div className="mt-4 flex flex-wrap items-center gap-2">
                                    <SecondaryButton onClick={() => void retake()} busy={busy}>
                                        <RefreshCw size={14} />
                                        Retake the interview
                                    </SecondaryButton>
                                </div>
                                <p className="mt-2 text-xs text-gray-500">
                                    A retake is a new version — it does not overwrite this one,
                                    and your published guide stays as it is until you approve
                                    the new one.
                                </p>
                            </Card>
                        )}
                    </>
                )}

                {/* ── Where to go next ──────────────────────────────────────── */}
                {/* These were ComingSoon cards, and they stayed that way after the
                    pages shipped — so the sidebar linked four working screens
                    while this index, the section's own front door, told people the
                    generator "isn't switched on yet". Somebody finished the
                    30-minute interview, landed here, and reasonably concluded
                    nothing had been built. A landing page that contradicts the nav
                    beside it is worse than no landing page. */}
                <SectionLink
                    to="/smart-teams/guide"
                    icon={<FileText size={16} />}
                    title="Your “How to work with me” guide"
                >
                    Cubie turns your answers into a guide. You read it and approve it before
                    any colleague can, and you can unpublish it later.
                </SectionLink>

                <SectionLink
                    to="/smart-teams/skills"
                    icon={<Wrench size={16} />}
                    title="Your skills"
                >
                    What you are happy to be asked about on a project. This is what Team
                    Builder matches on — it is not a rating of you.
                </SectionLink>

                {canAccessFullApp && (
                    <div data-tour="smart-teams-copilot-links" className="grid gap-3 sm:grid-cols-2">
                        <SectionLink
                            to="/smart-teams/compare"
                            icon={<Sparkles size={16} />}
                            title="Compare two people"
                        >
                            A side-by-side guide for a pair. Needs both of you to have
                            published your own guide first.
                        </SectionLink>
                        <SectionLink
                            to="/smart-teams/team-builder"
                            icon={<Wrench size={16} />}
                            title="Team Builder"
                        >
                            Ask for a group by the skills and the mix of styles a piece of work
                            needs.
                        </SectionLink>
                        {/* The inverse of "your skills": name the work, get the
                            people. Team Builder proposes a whole group; this
                            answers a narrower question without one. */}
                        <SectionLink
                            to="/smart-teams/find-people"
                            icon={<Users size={16} />}
                            title="Find people by skill"
                        >
                            Name what a project needs and see everyone who told us they do it.
                            Everybody who said yes, in name order — not a shortlist.
                        </SectionLink>
                    </div>
                )}

                {/* The screen this card used to promise. It stayed "coming soon"
                    long after the five support endpoints shipped, which meant the
                    only people who could use them were being told they could not.
                    `canViewSmartTeamsRawData` decides whether the LINK appears;
                    the four reads behind it are what actually enforce. */}
                {canViewSmartTeamsRawData && (
                    <SectionLink
                        to="/smart-teams/support"
                        icon={<ShieldCheck size={16} />}
                        title="Support: raw profile lookup"
                    >
                        Debug one person&rsquo;s interview, skills, guide and uploads with
                        them — what is stuck and who it is waiting on. Read-only, and every
                        lookup is logged.
                    </SectionLink>
                )}

                {/* Verbatim from the server. `disc.ADVISORY_NOTICE` is the
                    advisory-use promise every other surface makes, and the privacy
                    notice states what `authz.can_view_raw_profile` enforces — so
                    neither is paraphrased here, and a blank one renders as nothing
                    rather than as an empty box that looks like a notice. */}
                <PrivacyNotice text={state.privacy_notice} />
                <AdvisoryNotice text={state.advisory_notice} />
            </div>
        </div>
    );
};

export default SmartTeams;
