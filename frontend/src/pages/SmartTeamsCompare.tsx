// Compare — the pair working-relationship guide (ST-7). Gated on core access.
//
// One document, written TO the signed-in reader ABOUT one colleague. Its prose is
// authored server-side by `rubik_core/smart_teams/comparison.py`; this page
// fetches it, decides which of the six real states it is in, and hands the body
// to `PairGuideDocumentView` to render. It writes no sentence about either
// person.
//
// THE SIX STATES, AND WHY EACH ONE IS A SCREEN
// --------------------------------------------
//  1. Nobody picked yet                → the colleague picker.
//  2. 403 on the read                  → the consent gate. A dead end, by design.
//  3. 200, `state: 'none'`             → nothing written yet; offer Generate when
//                                        `can_generate`, otherwise the consent
//                                        gate's stricter half.
//  4. `status: 'pending'`              → being written; poll the read.
//  5. `status: 'failed'`               → the row's own error sentence, plus retry.
//  6. `status: 'ready'`                → the document, and a Regenerate when the
//                                        server says `stale`.
//
// (1) and (2) are the states most people will actually hit, which is why they get
// as much care as (6).
//
// WHAT THIS PAGE DOES NOT DO
// --------------------------
//  * It does not recompute `stale` from the two profile versions. The server
//    decides (`comparison.is_stale_pair`) and the flag is rendered as sent.
//  * It does not render `current_profile_versions` for the colleague, or infer
//    out loud that the colleague is the missing half of the consent gate. See
//    the header of `ConsentGate.tsx`.
//  * There is no approve, no unpublish, no .docx and no Save-to-Assets, because
//    there is no route for any of them — a pair guide is never published and
//    never enters the RAG corpus, and `published` / `rag_ingested` come back
//    false so a client can see that rather than take it on trust.
//  * There is no "compare two other people" affordance. The route has no
//    parameter for the reader, so it is unsayable rather than merely refused.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { AlertTriangle, ArrowLeft, GitCompare, Loader2, RefreshCw } from 'lucide-react';

import {
    generatePairGuide,
    getOwnGuide,
    getPairGuide,
    getPairHeatmap,
    smartTeamsError,
    type PairGuideResponse,
    type PairHeatmapResponse,
    type SmartTeamsError,
} from '../api/smartTeams';
import { useAuth } from '../context/AuthContext';
import { AdvisoryNotice, PrimaryButton, SecondaryButton } from '../components/smartTeams/shared';
import ColleaguePicker from '../components/smartTeams/compare/ColleaguePicker';
import ConsentGate, {
    type OwnReadiness,
} from '../components/smartTeams/compare/ConsentGate';
import PairGuideDocumentView from '../components/smartTeams/compare/PairGuideDocumentView';
import { PairHeatmap } from '../components/smartTeams/heatmap/Heatmap';
import CopilotOnly from '../components/smartTeams/compare/CopilotOnly';

/** How often to re-read while a guide is being written. The job is a Gemini call
 *  per section behind a cubie-worker dispatch, so seconds rather than
 *  milliseconds; four is what the interview's own polling settled on. */
const POLL_MS = 4000;

/** Stop polling eventually. A row wedged in `pending` is a job that died, and a
 *  page that spins forever tells the reader nothing — after this it says so and
 *  offers a manual re-read. */
const POLL_LIMIT = 75; // ≈5 minutes at POLL_MS

const UNKNOWN_READINESS: OwnReadiness = {
    published: null,
    noGuideYet: false,
    profileMissing: false,
};

export const SmartTeamsCompare: React.FC = () => {
    const { email: emailParam } = useParams<{ email: string }>();
    const navigate = useNavigate();
    const { user, canAccessSmartTeams, canAccessFullApp } = useAuth();

    const otherEmail = useMemo(
        () => (emailParam ? decodeURIComponent(emailParam).trim() : ''),
        [emailParam],
    );

    const [pickedName, setPickedName] = useState('');
    const [pair, setPair] = useState<PairGuideResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<SmartTeamsError | null>(null);
    const [generating, setGenerating] = useState(false);
    const [own, setOwn] = useState<OwnReadiness>(UNKNOWN_READINESS);
    const [pollExhausted, setPollExhausted] = useState(false);
    const pollCountRef = useRef(0);

    // The consent gate and the core-access gate are both 403s, but they cannot
    // both reach this page: the comparison router carries only
    // `require_smart_teams_access` (the SCREEN is what plan.md gates on core
    // access), so a 403 from these two calls is always the consent gate.
    const consentBlocked = error?.status === 403;

    /** The reader's own readiness — fetched ONLY when a gate is closed, because
     *  in the ordinary case it is a wasted request. Failures are swallowed on
     *  purpose: not knowing means `ConsentGate` says nothing about the reader,
     *  which is better than guessing. */
    const loadOwnReadiness = useCallback(async () => {
        try {
            const response = await getOwnGuide();
            setOwn({
                published: response.guide ? !!response.guide.published : false,
                noGuideYet: response.state === 'none' || !response.guide,
                profileMissing: response.current_profile_version === null,
            });
        } catch {
            setOwn(UNKNOWN_READINESS);
        }
    }, []);

    const load = useCallback(
        async (opts?: { quiet?: boolean }) => {
            if (!otherEmail) return;
            if (!opts?.quiet) {
                setLoading(true);
                setError(null);
            }
            try {
                const response = await getPairGuide(otherEmail);
                setPair(response);
                setError(null);
                if (!response.can_generate) void loadOwnReadiness();
            } catch (e) {
                const flat = smartTeamsError(
                    e,
                    'Cubie could not load this working-relationship guide.',
                );
                setPair(null);
                setError(flat);
                if (flat.status === 403) void loadOwnReadiness();
            } finally {
                if (!opts?.quiet) setLoading(false);
            }
        },
        [loadOwnReadiness, otherEmail],
    );

    // The overlap grid is computed live from the two profiles, so it does NOT
    // wait on a generated document — which is the whole point of it: it answers
    // before anybody has spent an LLM call, exactly when somebody is deciding
    // whether to ask for one. Its own read, its own failure: a grid that 403s or
    // 500s leaves the guide beside it working, and the absence is the only
    // failure state it has.
    const [heat, setHeat] = useState<PairHeatmapResponse | null>(null);

    const loadHeat = useCallback(async () => {
        if (!otherEmail) return;
        try {
            setHeat(await getPairHeatmap(otherEmail));
        } catch {
            setHeat(null);
        }
    }, [otherEmail]);

    // A new colleague is a new document: drop everything from the last one so no
    // state bleeds across (a stale `pending` would keep polling the new pair).
    useEffect(() => {
        setPair(null);
        setError(null);
        setOwn(UNKNOWN_READINESS);
        setPollExhausted(false);
        pollCountRef.current = 0;
        if (otherEmail) void load();
    }, [load, otherEmail]);

    // Keyed to the colleague like everything else, and cleared by the same
    // identity check the render does — `heat` for the previous person must never
    // be drawn under this person's name.
    useEffect(() => {
        void loadHeat();
    }, [loadHeat]);

    const status = pair?.guide?.status ?? null;
    const isPending = status === 'pending' || generating;

    // Poll while the row is being written. `getPairGuide` is the only read there
    // is — there is no progress endpoint and nothing to show a percentage from.
    useEffect(() => {
        if (status !== 'pending' || pollExhausted) return;
        const timer = setTimeout(() => {
            pollCountRef.current += 1;
            if (pollCountRef.current > POLL_LIMIT) {
                setPollExhausted(true);
                return;
            }
            void load({ quiet: true });
        }, POLL_MS);
        return () => clearTimeout(timer);
    }, [load, pollExhausted, status, pair]);

    const onGenerate = async () => {
        if (!otherEmail) return;
        setGenerating(true);
        setError(null);
        setPollExhausted(false);
        pollCountRef.current = 0;
        try {
            const response = await generatePairGuide(otherEmail);
            if (!response.enqueued) {
                // A cache hit: this exact pair of profile versions already has a
                // finished document, body included. Nothing was dispatched.
                setPair(current => ({
                    state: response.guide.status,
                    guide: response.guide,
                    current_profile_versions:
                        current?.current_profile_versions ??
                        response.guide.current_profile_versions,
                    display_names:
                        current?.display_names ?? response.guide.display_names,
                    can_generate: current?.can_generate ?? true,
                    advisory_notice: response.advisory_notice,
                }));
            } else {
                // Enqueued: the row comes back WITHOUT a body. Re-read, then poll.
                await load({ quiet: true });
            }
        } catch (e) {
            const flat = smartTeamsError(
                e,
                'Cubie could not start writing this guide.',
            );
            setError(flat);
            if (flat.status === 403) void loadOwnReadiness();
        } finally {
            setGenerating(false);
        }
    };

    // ── Gates ────────────────────────────────────────────────────────────────

    if (!canAccessSmartTeams || !canAccessFullApp) {
        return (
            <div className="mx-auto max-w-4xl p-6">
                <CopilotOnly surface="Compare" />
            </div>
        );
    }

    const displayName =
        (otherEmail && pair?.display_names?.[otherEmail.toLowerCase()]) ||
        (otherEmail && pair?.display_names?.[otherEmail]) ||
        pickedName ||
        otherEmail;

    const advisory = pair?.advisory_notice ?? null;

    return (
        <div className="mx-auto max-w-4xl p-6">
            <h1 data-tour="smart-teams-compare-header" className="mb-1 flex items-center gap-2 text-xl font-semibold text-gray-900">
                <GitCompare size={20} className="text-brand" aria-hidden="true" />
                Compare
            </h1>
            <p className="mb-5 text-sm leading-relaxed text-gray-600">
                How you and one colleague have each described the way you work, and where
                those descriptions line up or pull apart. Advisory only — nothing here is
                used for performance review, promotion, compensation, or deciding whether
                someone is right for a role.
            </p>

            {!otherEmail && (
                <div className="space-y-4">
                    <ColleaguePicker
                        selfEmail={user?.email ?? ''}
                        onPick={(email, name) => {
                            setPickedName(name);
                            navigate(`/smart-teams/compare/${encodeURIComponent(email)}`);
                        }}
                    />
                    <p className="px-1 text-xs leading-relaxed text-gray-500">
                        Cubie only writes this guide to you about someone else. There is no
                        way to ask for a comparison of two other people, and no way to read
                        the copy written to them about you — it is addressed to them, in
                        the second person.
                    </p>
                </div>
            )}

            {otherEmail && (
                <div className="space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                        <Link
                            to="/smart-teams/compare"
                            className="inline-flex items-center gap-1.5 rounded text-sm text-gray-600 hover:text-brand focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2"
                        >
                            <ArrowLeft size={14} aria-hidden="true" />
                            Pick someone else
                        </Link>
                        {pair?.guide?.stale && !isPending && (
                            <span className="rounded-full bg-amber-100 px-2.5 py-1 text-xs font-medium text-amber-900">
                                Out of date — one of you has retaken the interview
                            </span>
                        )}
                    </div>

                    {loading && (
                        <div
                            className="flex items-center gap-2 rounded-xl border border-gray-200 bg-white p-5 text-sm text-gray-600"
                            aria-live="polite"
                        >
                            <Loader2 className="animate-spin" size={16} aria-hidden="true" />
                            Loading the guide for you and {displayName}…
                        </div>
                    )}

                    {/* 403 — the consent gate. A dead end with an explanation. */}
                    {!loading && consentBlocked && (
                        <ConsentGate
                            otherName={displayName}
                            reason="read_blocked"
                            refusal={error?.message ?? null}
                            own={own}
                        />
                    )}

                    {/* Any other failure: retryable, and never silent. */}
                    {!loading && error && !consentBlocked && (
                        <div
                            role="alert"
                            className="rounded-xl border border-red-200 bg-red-50 p-5"
                        >
                            <div className="flex items-start gap-2.5">
                                <AlertTriangle
                                    size={18}
                                    aria-hidden="true"
                                    className="mt-0.5 flex-shrink-0 text-red-500"
                                />
                                <div>
                                    <p className="text-sm font-medium text-red-900">
                                        {error.message}
                                    </p>
                                    {error.status === 404 && (
                                        <p className="mt-1 text-xs text-red-700">
                                            Cubie writes these only between two people at your
                                            company, and not between you and yourself.
                                        </p>
                                    )}
                                </div>
                            </div>
                            {error.status !== 404 && (
                                <div className="mt-3">
                                    <SecondaryButton onClick={() => void load()}>
                                        <RefreshCw size={14} aria-hidden="true" />
                                        Try again
                                    </SecondaryButton>
                                </div>
                            )}
                        </div>
                    )}

                    {/* 200, nothing written yet. Suppressed the moment a
                        generate is in flight: until the re-read lands, `pair`
                        still says `none`, and showing "nothing written yet"
                        beside "Cubie is writing this" is two answers at once. */}
                    {!loading && !error && !isPending && pair?.state === 'none' && (
                        <>
                            {pair.can_generate ? (
                                <div className="rounded-xl border border-gray-200 bg-white p-5">
                                    <h2 className="text-base font-semibold text-gray-900">
                                        Nothing has been written for you and {displayName}{' '}
                                        yet
                                    </h2>
                                    <p className="mt-1.5 text-sm leading-relaxed text-gray-700">
                                        You have both published a working guide, so Cubie
                                        can write one. It reads the parts of each profile
                                        that are reported for both of you, and says nothing
                                        from a part only one of you has.
                                    </p>
                                    <p className="mt-2 text-xs leading-relaxed text-gray-500">
                                        This copy is written to you. {displayName} is not
                                        told that you asked for it, and does not get to read
                                        this one.
                                    </p>
                                    <div className="mt-4">
                                        <PrimaryButton
                                            onClick={() => void onGenerate()}
                                            busy={generating}
                                        >
                                            Write the guide
                                        </PrimaryButton>
                                    </div>
                                </div>
                            ) : (
                                <ConsentGate
                                    otherName={displayName}
                                    reason="cannot_generate"
                                    own={own}
                                />
                            )}
                        </>
                    )}

                    {/* Being written. */}
                    {!loading && !error && isPending && !pollExhausted && (
                        <div
                            className="rounded-xl border border-gray-200 bg-white p-5"
                            aria-live="polite"
                        >
                            <p className="flex items-center gap-2 text-sm font-medium text-gray-900">
                                <Loader2
                                    className="animate-spin text-brand"
                                    size={16}
                                    aria-hidden="true"
                                />
                                Cubie is writing this guide
                            </p>
                            <p className="mt-1.5 text-sm leading-relaxed text-gray-600">
                                It is comparing each part of the two profiles in turn, so
                                this takes a minute or two. You can leave this page — it
                                carries on without you and will be here when you come back.
                            </p>
                        </div>
                    )}

                    {/* Wedged. Say so rather than spin. */}
                    {!loading && !error && status === 'pending' && pollExhausted && (
                        <div
                            role="alert"
                            className="rounded-xl border border-amber-200 bg-amber-50 p-5"
                        >
                            <p className="text-sm font-medium text-amber-900">
                                This has been writing for a while now.
                            </p>
                            <p className="mt-1 text-sm leading-relaxed text-amber-800">
                                Cubie has stopped checking. Nothing is lost — check again,
                                and if it stays like this, your Cubie admin can look at the job.
                            </p>
                            <div className="mt-3">
                                <SecondaryButton
                                    onClick={() => {
                                        setPollExhausted(false);
                                        pollCountRef.current = 0;
                                        void load();
                                    }}
                                >
                                    <RefreshCw size={14} aria-hidden="true" />
                                    Check again
                                </SecondaryButton>
                            </div>
                        </div>
                    )}

                    {/* Failed. The row's own sentence. */}
                    {!loading && !error && status === 'failed' && (
                        <div
                            role="alert"
                            className="rounded-xl border border-red-200 bg-red-50 p-5"
                        >
                            <p className="text-sm font-medium text-red-900">
                                Cubie could not finish writing this guide.
                            </p>
                            {pair?.guide?.error && (
                                <p className="mt-1.5 text-sm leading-relaxed text-red-800">
                                    {pair.guide.error}
                                </p>
                            )}
                            <p className="mt-1.5 text-xs text-red-700">
                                Nothing partial was kept — a half-written comparison is not
                                shown to anybody.
                            </p>
                            {pair?.can_generate && (
                                <div className="mt-3">
                                    <SecondaryButton
                                        onClick={() => void onGenerate()}
                                        busy={generating}
                                    >
                                        <RefreshCw size={14} aria-hidden="true" />
                                        Try writing it again
                                    </SecondaryButton>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Ready. */}
                    {!loading && !error && status === 'ready' && pair?.guide && (
                        <>
                            <PairGuideDocumentView
                                guide={pair.guide}
                                advisoryNotice={pair.advisory_notice}
                            />
                            {pair.guide.stale && pair.can_generate && (
                                <div className="rounded-xl border border-amber-200 bg-amber-50 p-5">
                                    <p className="text-sm font-medium text-amber-900">
                                        One of you has retaken the interview since this was
                                        written.
                                    </p>
                                    <p className="mt-1 text-sm leading-relaxed text-amber-800">
                                        What you just read still reflects the answers it was
                                        written from. Writing it again reads the current
                                        answers instead; it does not change or delete this
                                        copy.
                                    </p>
                                    <div className="mt-3">
                                        <SecondaryButton
                                            onClick={() => void onGenerate()}
                                            busy={generating}
                                        >
                                            <RefreshCw size={14} aria-hidden="true" />
                                            Write it again from the current answers
                                        </SecondaryButton>
                                    </div>
                                </div>
                            )}
                            <p className="px-1 text-xs leading-relaxed text-gray-500">
                                This guide is not searchable in Cubie Chat and there is no
                                download for it. It stays on this screen on purpose: a saved
                                copy would outlive either of you unpublishing your own
                                working guide.
                            </p>
                        </>
                    )}

                    {/* Rendered in EVERY guide state, including "none": the grid
                        is the answer available before anybody generates prose.
                        The email check is what stops the previous colleague's
                        grid appearing under this one's name mid-switch. */}
                    {heat && heat.about_email === otherEmail.toLowerCase() && (
                        <PairHeatmap data={heat} />
                    )}

                    {advisory && !consentBlocked && status !== 'ready' && (
                        <AdvisoryNotice text={advisory} />
                    )}
                </div>
            )}
        </div>
    );
};

export default SmartTeamsCompare;
