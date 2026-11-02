// ST-6 + ST-5 — "How to Be Successful Working with me": my guide, and the
// optional official report that can feed it.
//
// ONE COMPONENT, TWO AUDIENCES
// ----------------------------
// `/smart-teams/guide` is the subject's own view: generate, read the draft,
// approve (which publishes), unpublish, download. `/smart-teams/guide/:email` is
// a reader's view of somebody's PUBLISHED guide and has none of those controls.
// The presence of the route parameter is the only switch, matching how App.tsx
// registers the two routes against this one component.
//
// THE STATES, KEPT APART
// ----------------------
//   no scored profile      `state: 'none'`, `can_generate: false` → the interview
//                          has not been scored, so there is nothing to write
//                          from. Not an error and not an empty guide.
//   scored, no guide       `state: 'none'`, `can_generate: true` → a Generate
//                          button and what pressing it does.
//   generating             `state: 'pending'`. Generation runs on cubie-worker,
//                          and `DOCUMENT_STATUSES` has no `queued`/`running`, so
//                          a job that died leaves the row `pending` for ever.
//                          The poll is therefore BOUNDED and says so when it
//                          gives up — an unbounded spinner is how that failure
//                          stays invisible.
//   failed                 `state: 'failed'` with the row's own sentence, and a
//                          retry that re-dispatches onto the same row.
//   draft                  `ready`, not published → the document in full plus the
//                          consent panel. Nobody else can read it.
//   published              `ready`, published → the same document plus who can
//                          see it and the way back out.
//   stale                  `guide.stale`, server-decided. Named with both version
//                          numbers, and Regenerate lands on a NEW row so approved
//                          wording is never overwritten.
//
// WHAT THIS PAGE NEVER DOES
// -------------------------
//   * It never renders a raw score, a percentile or a domain number. The guide
//     body cannot contain one (`guide.py` refuses to write one) and no field on
//     `GuideDocument` carries one. The only raw numbers in this feature are on
//     the subject's own upload-review screen, inside `uploads/UploadReview.tsx`,
//     and they never travel further than that component.
//   * It never renders a bare four-letter type. Nothing here parses the body;
//     the derived-layer line arrives inside `content_md` with the qualifier the
//     instrument requires attached.
//   * It never pads a short guide. A refused layer is an omitted section, not an
//     empty card — see `guide/GuideBody.tsx`.
//   * It never claims a guide is published to Cubie Chat on the strength of
//     having asked. `rag_ingested` is reported as it stands.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
    AlertTriangle,
    ArrowRight,
    ClipboardList,
    Download,
    FileText,
    Loader2,
    RefreshCw,
    Search,
    Sparkles,
} from 'lucide-react';

import { useAuth } from '../context/AuthContext';
import { BackButton } from '../components/BackButton';
import { toast } from '../components/eos/Toast';
import { EmptyState, ErrorState, LoadingState } from './eos/_shared';
import { AdvisoryNotice, PrimaryButton, SecondaryButton } from '../components/smartTeams/shared';
import { GuideBody, ShortfallFraming } from '../components/smartTeams/guide/GuideBody';
import { OwnVisibilityBanner, StaleBanner } from '../components/smartTeams/guide/GuideStatus';
import {
    isShortfallGuide,
    ownGuidePhase,
    shortDate,
} from '../components/smartTeams/guide/guideState';
import {
    ApprovalConsent,
    UnpublishPanel,
} from '../components/smartTeams/guide/ApprovalConsent';
import { OwnHeatmap } from '../components/smartTeams/heatmap/Heatmap';
import { OfficialReportSection } from '../components/smartTeams/uploads/OfficialReportSection';
import type {
    ColleagueGuideResponse,
    GuideDocument,
    OwnGuideResponse,
    OwnHeatmapResponse,
} from '../api/smartTeams';
import {
    approveGuide,
    downloadGuideDocx,
    downloadGuidePdf,
    generateOwnGuide,
    getColleagueGuide,
    getOwnGuide,
    getOwnHeatmap,
    guideDocxFilename,
    guidePdfFilename,
    smartTeamsError,
    unpublishGuide,
} from '../api/smartTeams';

/** 5s, the interval every polled surface in this app uses. */
const POLL_MS = 5000;
/** ~7.5 minutes, matching the interview's scoring poll. Long enough for a Gemini
 *  call on a cold worker; short enough that a dead job becomes visible. */
const MAX_POLLS = 90;

const Card: React.FC<{ children: React.ReactNode; className?: string }> = ({
    children,
    className,
}) => (
    <div className={`rounded-lg border border-gray-200 bg-white p-5 ${className ?? ''}`}>
        {children}
    </div>
);

/** Hand a blob to the browser. Four lines rather than an import from a page
 *  module, which would pull a whole other screen into this bundle. */
function saveBlob(data: Blob, filename: string): void {
    const url = URL.createObjectURL(data);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
}

/** The document, with the framing a mostly-hedged guide needs. Shared by both
 *  views so the reader and the subject see the identical rendering — which is
 *  what makes "this is exactly what colleagues will see" a true statement. */
const GuideDocumentCard: React.FC<{ guide: GuideDocument; ownView: boolean }> = ({
    guide,
    ownView,
}) => (
    <Card>
        {isShortfallGuide(guide.content_md) && (
            <ShortfallFraming ownGuide={ownView} displayName={guide.display_name} />
        )}
        <GuideBody markdown={guide.content_md} />
    </Card>
);

// ── The subject's own view ───────────────────────────────────────────────────

const OwnGuideView: React.FC = () => {
    const [data, setData] = useState<OwnGuideResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [denied, setDenied] = useState(false);

    const [generating, setGenerating] = useState(false);
    const [generateError, setGenerateError] = useState<string | null>(null);
    const [approving, setApproving] = useState(false);
    const [approveError, setApproveError] = useState<string | null>(null);
    const [approveNote, setApproveNote] = useState<string | null>(null);
    const [unpublishing, setUnpublishing] = useState(false);
    const [unpublishError, setUnpublishError] = useState<string | null>(null);
    const [downloading, setDownloading] = useState(false);
    const [pollTimedOut, setPollTimedOut] = useState(false);
    // Bumped by any action that starts something new on a worker. The poll
    // effect depends on it, so pressing "Start it again" after the ceiling was
    // hit actually resumes checking instead of leaving a stopped poll behind a
    // hopeful button.
    const [pollCycle, setPollCycle] = useState(0);

    const pollRef = useRef<number | null>(null);
    const pollsRef = useRef(0);

    const restartPolling = useCallback(() => {
        setPollTimedOut(false);
        setPollCycle(cycle => cycle + 1);
    }, []);

    // The grid is a SEPARATE read, and a separate failure. It comes from the
    // profile rather than from the guide document, so it is present for somebody
    // who has been scored but has never generated a guide — and a heatmap that
    // failed to load must not take the guide down with it.
    const [grid, setGrid] = useState<OwnHeatmapResponse | null>(null);

    const load = useCallback(async (): Promise<void> => {
        try {
            const next = await getOwnGuide();
            setData(next);
            setError(null);
            setDenied(false);
        } catch (e) {
            const err = smartTeamsError(e, 'Cubie could not load your guide.');
            // A 403 never reaches the axios interceptor's redirect (only a 401
            // does), so the denial state is this page's to render.
            if (err.status === 403) setDenied(true);
            else setError(err.message);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    const loadGrid = useCallback(async (): Promise<void> => {
        // Swallowed on purpose: this is an enhancement beside the guide, and a
        // 403 or a 500 here should leave the page it decorates working. The
        // absence is the failure state — there is no error box for it.
        try {
            setGrid(await getOwnHeatmap());
        } catch {
            setGrid(null);
        }
    }, []);

    useEffect(() => {
        void loadGrid();
    }, [loadGrid]);

    const stopPolling = useCallback(() => {
        if (pollRef.current) {
            window.clearInterval(pollRef.current);
            pollRef.current = null;
        }
    }, []);

    // Three things can be in flight, each of them a cubie-worker job: the
    // generation itself, the Chat copy after an approve, and the Chat purge
    // after an unpublish. Each ends in a row field changing, so one poll of
    // `GET /guides/me` covers all three.
    const guide = data?.guide ?? null;
    const generatingRow = data?.state === 'pending';
    const corpusInFlight = guide
        ? (guide.published && !guide.rag_ingested) ||
          (!guide.published && guide.rag_ingested && Boolean(guide.unpublished_at))
        : false;
    const shouldPoll = generatingRow || corpusInFlight;

    useEffect(() => {
        if (!shouldPoll) {
            stopPolling();
            return;
        }
        if (pollRef.current) return;
        pollsRef.current = 0;
        pollRef.current = window.setInterval(() => {
            pollsRef.current += 1;
            void load();
            if (pollsRef.current >= MAX_POLLS) {
                stopPolling();
                setPollTimedOut(true);
            }
        }, POLL_MS);
        return stopPolling;
        // `pollCycle` is a dependency on purpose: it is how an action restarts a
        // poll that had already given up.
    }, [load, pollCycle, shouldPoll, stopPolling]);

    useEffect(() => stopPolling, [stopPolling]);

    const generate = useCallback(async (): Promise<void> => {
        setGenerating(true);
        setGenerateError(null);
        setApproveNote(null);
        restartPolling();
        try {
            const response = await generateOwnGuide();
            if (!response.enqueued) {
                toast.info('That guide was already written — here it is.');
            }
            await load();
        } catch (e) {
            // Every refusal here is a 409 with a sentence written for the person:
            // no scored profile yet, or a published guide that would be
            // overwritten. Verbatim, and never swallowed into a toast that
            // disappears.
            setGenerateError(
                smartTeamsError(e, 'Cubie could not start writing your guide.').message,
            );
        } finally {
            setGenerating(false);
        }
    }, [load, restartPolling]);

    const approve = useCallback(async (): Promise<void> => {
        if (!guide) return;
        setApproving(true);
        setApproveError(null);
        try {
            const response = await approveGuide(guide.id);
            setData(current =>
                current
                    ? { ...current, state: response.guide.status, guide: response.guide }
                    : current,
            );
            restartPolling();
            setApproveNote(
                response.superseded.length > 0
                    ? 'Your guide is published. It replaces the one you had published before, which is no longer readable by anybody.'
                    : 'Your guide is published. Colleagues can read it now.',
            );
        } catch (e) {
            setApproveError(smartTeamsError(e, 'Cubie could not publish your guide.').message);
        } finally {
            setApproving(false);
        }
    }, [guide, restartPolling]);

    const unpublish = useCallback(async (): Promise<void> => {
        if (!guide) return;
        setUnpublishing(true);
        setUnpublishError(null);
        setApproveNote(null);
        try {
            const response = await unpublishGuide(guide.id);
            setData(current =>
                current
                    ? { ...current, state: response.guide.status, guide: response.guide }
                    : current,
            );
            restartPolling();
            toast.success(
                response.rag_purge === 'done'
                    ? 'Your guide is out of company view.'
                    : 'Your guide is out of company view. Cubie is removing the Chat copy now.',
            );
        } catch (e) {
            setUnpublishError(
                smartTeamsError(e, 'Cubie could not take your guide out of view.').message,
            );
        } finally {
            setUnpublishing(false);
        }
    }, [guide, restartPolling]);

    // PDF is the copy meant to be sent; Word is for adapting the wording. One
    // busy flag for both, because the two buttons sit together and a second
    // spinner would only say which format somebody picked.
    const download = useCallback(
        async (format: 'pdf' | 'docx'): Promise<void> => {
            if (!guide) return;
            setDownloading(true);
            try {
                const blob =
                    format === 'pdf'
                        ? await downloadGuidePdf(guide.id)
                        : await downloadGuideDocx(guide.id);
                saveBlob(
                    blob,
                    format === 'pdf'
                        ? guidePdfFilename(guide.display_name)
                        : guideDocxFilename(guide.display_name),
                );
            } catch (e) {
                toast.error(
                    smartTeamsError(
                        e,
                        `Cubie could not build the ${format === 'pdf' ? 'PDF' : 'Word'} file.`,
                    ).message,
                );
            } finally {
                setDownloading(false);
            }
        },
        [guide],
    );

    if (loading) {
        return <LoadingState label="Loading your guide…" />;
    }

    if (denied) {
        return (
            <EmptyState
                title="Smart Teams isn't switched on for this account"
                hint="It's open to everyone with a work account at your company. If that's you and you're seeing this, tell your Cubie admin."
            />
        );
    }

    if (error || !data) {
        return (
            <div className="space-y-3">
                <ErrorState message={error ?? 'Nothing came back.'} />
                <SecondaryButton onClick={() => void load()}>
                    <RefreshCw size={14} />
                    Try again
                </SecondaryButton>
            </div>
        );
    }

    const phase = guide ? ownGuidePhase(guide) : null;

    return (
        <div className="space-y-4">
            {/* ── No scored profile yet ─────────────────────────────────────── */}
            {data.state === 'none' && !data.can_generate && (
                <Card>
                    <h2 className="flex items-center gap-2 text-base font-semibold text-gray-900">
                        <ClipboardList className="text-brand" size={18} />
                        Your interview has not been scored yet
                    </h2>
                    <p className="mt-2 text-sm leading-relaxed text-gray-700">
                        A guide is written from your own answers, so there is nothing to write
                        from until the Work Style Interview has been answered and scored. That
                        is the only thing standing between you and this page.
                    </p>
                    <div className="mt-4">
                        <Link
                            to="/smart-teams"
                            className="inline-flex items-center gap-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2"
                        >
                            Go to the interview
                            <ArrowRight size={15} />
                        </Link>
                    </div>
                </Card>
            )}

            {/* ── Scored, nothing written yet ───────────────────────────────── */}
            {data.state === 'none' && data.can_generate && (
                <Card>
                    <h2 className="flex items-center gap-2 text-base font-semibold text-gray-900">
                        <Sparkles className="text-brand" size={18} />
                        Ready to write your guide
                    </h2>
                    <p className="mt-2 text-sm leading-relaxed text-gray-700">
                        Cubie turns your answers into a short guide, written in your voice,
                        about how you prefer to work. What comes back is a draft: nothing
                        about it is company-visible unless you approve it.
                    </p>
                    <p className="mt-2 text-sm text-gray-600">
                        Writing it takes a few minutes and runs in the background, so you can
                        leave this page.
                    </p>
                    {generateError && (
                        <p
                            role="alert"
                            className="mt-3 rounded-md border border-red-200 bg-red-50 p-3 text-sm leading-relaxed text-red-800"
                        >
                            {generateError}
                        </p>
                    )}
                    <div className="mt-4">
                        <PrimaryButton onClick={() => void generate()} busy={generating}>
                            <Sparkles size={15} />
                            Write my guide
                        </PrimaryButton>
                    </div>
                </Card>
            )}

            {/* ── Being written ────────────────────────────────────────────── */}
            {data.state === 'pending' && (
                <Card className="border-brand-tint bg-brand-tint/40">
                    <h2 className="flex items-center gap-2 text-base font-semibold text-brand-hover">
                        <Loader2 className="animate-spin" size={18} />
                        Cubie is writing your guide
                    </h2>
                    <p className="mt-2 text-sm leading-relaxed text-brand-hover">
                        This runs in the background and takes a few minutes. You can leave this
                        page and come back — nothing is lost, and nothing becomes
                        company-visible until you approve it.
                    </p>
                    {pollTimedOut && (
                        <p className="mt-2 text-sm font-medium text-brand-hover">
                            It is taking longer than expected and Cubie has stopped checking
                            automatically. Press Check now, or start it again — that reuses the
                            same draft rather than making a second one.
                        </p>
                    )}
                    {generateError && (
                        <p role="alert" className="mt-2 text-sm font-medium text-red-800">
                            {generateError}
                        </p>
                    )}
                    <div className="mt-4 flex flex-wrap gap-2">
                        <SecondaryButton onClick={() => void load()}>
                            <RefreshCw size={14} />
                            Check now
                        </SecondaryButton>
                        {pollTimedOut && (
                            <SecondaryButton onClick={() => void generate()} busy={generating}>
                                Start it again
                            </SecondaryButton>
                        )}
                    </div>
                </Card>
            )}

            {/* ── The generation failed ────────────────────────────────────── */}
            {data.state === 'failed' && (
                <Card className="border-amber-200 bg-amber-50">
                    <h2 className="flex items-center gap-2 text-base font-semibold text-amber-900">
                        <AlertTriangle size={18} />
                        Cubie could not finish writing your guide
                    </h2>
                    {/* The row's own failure sentence. Shown because a person
                        chasing their admin needs the actual reason, not "something
                        went wrong". */}
                    {guide?.error && (
                        <p className="mt-2 text-sm leading-relaxed text-amber-900">{guide.error}</p>
                    )}
                    <p className="mt-2 text-sm leading-relaxed text-amber-900">
                        Your answers are safe and nothing has been published. Trying again
                        reuses the same draft.
                    </p>
                    {generateError && (
                        <p role="alert" className="mt-2 text-sm font-medium text-red-800">
                            {generateError}
                        </p>
                    )}
                    <div className="mt-4">
                        <PrimaryButton onClick={() => void generate()} busy={generating}>
                            <RefreshCw size={15} />
                            Try again
                        </PrimaryButton>
                    </div>
                </Card>
            )}

            {/* ── Ready: draft, published, or retracted ────────────────────── */}
            {data.state === 'ready' && guide && (
                <>
                    <OwnVisibilityBanner guide={guide} />

                    {guide.stale && (
                        <StaleBanner
                            guide={guide}
                            onRegenerate={() => void generate()}
                            busy={generating}
                        />
                    )}

                    {generateError && (
                        <p
                            role="alert"
                            className="rounded-md border border-red-200 bg-red-50 p-3 text-sm leading-relaxed text-red-800"
                        >
                            {generateError}
                        </p>
                    )}

                    {approveNote && (
                        <p className="rounded-md border border-green-200 bg-green-50 p-3 text-sm leading-relaxed text-green-900">
                            {approveNote}
                        </p>
                    )}

                    <GuideDocumentCard guide={guide} ownView />

                    <div className="flex flex-wrap items-center gap-2">
                        <PrimaryButton onClick={() => void download('pdf')} busy={downloading}>
                            <Download size={15} />
                            Download PDF
                        </PrimaryButton>
                        <SecondaryButton
                            onClick={() => void download('docx')}
                            busy={downloading}
                        >
                            Word
                        </SecondaryButton>
                        <span className="text-xs text-gray-500">
                            The PDF is the one to send. Word is there if you want to adapt the
                            wording — it is editable, so a copy somebody else changes is no
                            longer what you approved. Either file is a copy — it does not stay in
                            step with the version here,
                            and taking your guide out of view cannot recall a file somebody has
                            already saved.
                        </span>
                    </div>

                    {phase === 'published' ? (
                        <UnpublishPanel
                            ragIngested={guide.rag_ingested}
                            busy={unpublishing}
                            error={unpublishError}
                            onUnpublish={() => void unpublish()}
                        />
                    ) : (
                        <ApprovalConsent
                            previouslyPublished={phase === 'retracted'}
                            busy={approving}
                            error={approveError}
                            onApprove={() => void approve()}
                        />
                    )}
                </>
            )}

            {/* Your own shape, drawn. Above the upload section because it is
                about the profile you already have rather than about adding to
                it, and below the guide because the prose is the point. */}
            {grid && <OwnHeatmap data={grid} />}

            {/* ── ST-5, and it stays quiet ─────────────────────────────────── */}
            <OfficialReportSection
                onProfileChanged={() => {
                    void load();
                    // A confirmed merge writes a NEW profile version, so the
                    // grid is stale the moment this fires.
                    void loadGrid();
                }}
            />

            {/* Verbatim from the server — the advisory promise the instrument
                authored. Never paraphrased in the browser. */}
            <AdvisoryNotice text={guide?.advisory_notice ?? data.advisory_notice} />

            <ColleagueLookup />
        </div>
    );
};

/** Open somebody else's published guide.
 *
 *  A plain address field rather than a directory picker: every call this page
 *  makes is one of the 37 Smart Teams routes, and there is no people-search
 *  endpoint among them. An address that is not an employee, or somebody with no
 *  published guide, both come back as the same honest 404 — which is the point,
 *  because telling those apart would let anybody enumerate who has an
 *  unpublished draft. */
const ColleagueLookup: React.FC = () => {
    const navigate = useNavigate();
    const [value, setValue] = useState('');

    return (
        <form
            data-tour="smart-teams-guide-colleague"
            className="rounded-lg border border-gray-200 bg-white p-5"
            onSubmit={event => {
                event.preventDefault();
                const email = value.trim();
                if (!email) return;
                navigate(`/smart-teams/guide/${encodeURIComponent(email)}`);
            }}
        >
            <label
                htmlFor="st-colleague-email"
                className="flex items-center gap-2 text-sm font-medium text-gray-800"
            >
                <Search size={15} className="text-gray-400" />
                Read a colleague&apos;s guide
            </label>
            <p className="mt-1 text-xs leading-relaxed text-gray-500">
                Their work email. You will see their guide if they have published one, and
                nothing at all if they have not — Cubie will not tell you which.
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-2">
                <input
                    id="st-colleague-email"
                    type="email"
                    inputMode="email"
                    autoComplete="off"
                    value={value}
                    onChange={event => setValue(event.target.value)}
                    placeholder="name@company.com"
                    className="min-w-0 flex-1 rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-900 placeholder:text-gray-400 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                />
                <PrimaryButton type="submit" disabled={!value.trim()}>
                    Open
                </PrimaryButton>
            </div>
        </form>
    );
};

// ── A colleague's published guide ────────────────────────────────────────────

const ColleagueGuideView: React.FC<{ email: string }> = ({ email }) => {
    const { user } = useAuth();
    const [data, setData] = useState<ColleagueGuideResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [status, setStatus] = useState<number | null>(null);
    const [message, setMessage] = useState<string | null>(null);
    const [downloading, setDownloading] = useState(false);

    const load = useCallback(async (): Promise<void> => {
        setLoading(true);
        try {
            const next = await getColleagueGuide(email);
            setData(next);
            setStatus(null);
            setMessage(null);
        } catch (e) {
            const err = smartTeamsError(e, 'Cubie could not load that guide.');
            setData(null);
            setStatus(err.status);
            setMessage(err.message);
        } finally {
            setLoading(false);
        }
    }, [email]);

    useEffect(() => {
        void load();
    }, [load]);

    const guide = data?.guide ?? null;
    const isMine =
        guide != null &&
        (user?.email ?? '').trim().toLowerCase() === guide.subject_email.trim().toLowerCase();

    const download = async (format: 'pdf' | 'docx'): Promise<void> => {
        if (!guide) return;
        setDownloading(true);
        try {
            const blob =
                format === 'pdf'
                    ? await downloadGuidePdf(guide.id)
                    : await downloadGuideDocx(guide.id);
            saveBlob(
                blob,
                format === 'pdf'
                    ? guidePdfFilename(guide.display_name)
                    : guideDocxFilename(guide.display_name),
            );
        } catch (e) {
            toast.error(
                smartTeamsError(
                    e,
                    `Cubie could not build the ${format === 'pdf' ? 'PDF' : 'Word'} file.`,
                ).message,
            );
        } finally {
            setDownloading(false);
        }
    };

    if (loading) return <LoadingState label="Loading that guide…" />;

    // 404 covers BOTH "no published guide" and "no guide at all", deliberately.
    // This copy must not speculate about which — that ambiguity is the privacy
    // guarantee, not a gap in the message.
    if (status === 404) {
        return (
            <EmptyState
                title="There is no published guide for that person"
                hint="Either they have not written one or they have not published it — Cubie does not say which, because an unpublished draft is private. Ask them directly if you need one."
            />
        );
    }

    if (status === 403) {
        return <EmptyState title="You cannot read that guide" hint={message ?? undefined} />;
    }

    if (!guide) {
        return (
            <div className="space-y-3">
                <ErrorState message={message ?? 'Nothing came back.'} />
                <SecondaryButton onClick={() => void load()}>
                    <RefreshCw size={14} />
                    Try again
                </SecondaryButton>
            </div>
        );
    }

    const approved = shortDate(guide.approved_at);
    // Two ways to arrive here holding a row its subject never published, and
    // both used to render "<name> published this" over it.
    //
    //   1. The lookup form does not exclude your own address, and the read
    //      returns your NEWEST row whatever its status — so looking yourself up
    //      showed your own unapproved draft as published.
    //   2. authz deliberately grants an unpublished draft to a support admin for
    //      support. That is the right rule; presenting the draft as something
    //      the person published is not.
    //
    // Saying somebody published a document they did not is the one claim this
    // screen must never make: the whole feature rests on approval being consent.
    const unpublished = !guide.published;
    const notReady = guide.status !== 'ready';

    return (
        <div className="space-y-4">
            <div
                data-tour="smart-teams-colleague-guide-status"
                className={
                    unpublished || notReady
                        ? 'rounded-lg border border-amber-300 bg-amber-50 p-4'
                        : 'rounded-lg border border-gray-200 bg-gray-50 p-4'
                }
            >
                {notReady ? (
                    <p className="text-sm text-gray-800">
                        <span className="font-medium">Not finished yet.</span> Cubie is still
                        writing this guide, so there is nothing to read here. You are seeing
                        the row because it is yours or because you support Smart Teams.
                    </p>
                ) : unpublished ? (
                    <p className="text-sm text-gray-800">
                        <span className="font-medium">
                            {isMine ? 'Your own unpublished draft.' : 'An unpublished draft.'}
                        </span>{' '}
                        {isMine
                            ? 'You have not published this, so no colleague can see it.'
                            : `${guide.display_name} has not published this. You can see it because you support Smart Teams — not because they shared it.`}{' '}
                        Please do not treat it as something they told you about themselves.
                    </p>
                ) : (
                    <p className="text-sm text-gray-800">
                        <span className="font-medium">{guide.display_name}</span> published this
                        {approved ? ` on ${approved}` : ''}. It is their own account of how they
                        prefer to work — advisory only, and never a statement about how well
                        they do it.
                    </p>
                )}
                {guide.stale && (
                    <p className="mt-2 text-xs leading-relaxed text-gray-600">
                        They have answered the interview again since this was written, so it
                        reflects their earlier answers. Whether to publish a newer one is theirs
                        to decide.
                    </p>
                )}
                {isMine && (
                    <p className="mt-2 text-xs leading-relaxed text-gray-600">
                        This is your own guide.{' '}
                        <Link to="/smart-teams/guide" className="font-medium text-brand hover:underline">
                            Manage it here
                        </Link>
                        .
                    </p>
                )}
            </div>

            <GuideDocumentCard guide={guide} ownView={false} />

            <div data-tour="smart-teams-colleague-guide-download" className="flex flex-wrap items-center gap-2">
                {notReady ? null : unpublished ? (
                    // No export for a draft nobody published. The caption on the
                    // other branch — treat it as something they told you about
                    // themselves — is exactly backwards for a document its
                    // subject has not shared, and a saved .docx outlives any
                    // later decision they make about it.
                    <span className="text-xs text-gray-700">
                        No download while this is unpublished. A saved copy would outlast the
                        subject&apos;s decision about whether to share it at all.
                    </span>
                ) : (
                    <>
                        <PrimaryButton onClick={() => void download('pdf')} busy={downloading}>
                            <Download size={15} />
                            Download PDF
                        </PrimaryButton>
                        <SecondaryButton
                            onClick={() => void download('docx')}
                            busy={downloading}
                        >
                            Word
                        </SecondaryButton>
                        <span className="text-xs text-gray-500">
                            A saved copy does not disappear if they later take their guide out
                            of view. Treat it the way you would treat anything else somebody
                            told you about themselves.
                        </span>
                    </>
                )}
            </div>

            <AdvisoryNotice text={guide.advisory_notice ?? data?.advisory_notice} />
        </div>
    );
};

// ── The page ─────────────────────────────────────────────────────────────────

export const SmartTeamsGuide: React.FC = () => {
    const { email } = useParams<{ email: string }>();
    const reading = (email ?? '').trim();

    return (
        <div className="mx-auto max-w-4xl p-8">
            <header data-tour="smart-teams-guide-header" className="mb-6">
                {reading && <BackButton fallback="/smart-teams/guide" className="mb-4" />}
                <div className="flex items-start gap-3">
                    <div className="flex items-center justify-center rounded-md bg-brand p-2.5 text-white">
                        <FileText size={20} />
                    </div>
                    <div>
                        <h1 className="text-2xl font-semibold text-gray-900">
                            {reading ? 'Working guide' : 'My working guide'}
                        </h1>
                        <p className="mt-1 text-sm leading-relaxed text-gray-500">
                            How to be successful working with{' '}
                            {reading ? 'a colleague' : 'you'} — written from the Work Style
                            Interview. Advisory only: it is about how somebody prefers to work,
                            never about how well they do it, and it is never used for
                            performance review, promotion, or compensation.
                        </p>
                    </div>
                </div>
            </header>

            {reading ? <ColleagueGuideView email={reading} /> : <OwnGuideView />}
        </div>
    );
};

export default SmartTeamsGuide;
