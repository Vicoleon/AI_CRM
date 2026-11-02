// ST-3 — the Cubie Work Style Interview.
//
// WHERE STATE LIVES
// -----------------
// On the server. `st_interviews` carries the section cursor, the answers blob
// and the conversation, so a refresh is a re-GET and a resume lands on the exact
// item somebody stopped at. Nothing about progress is derived from React state
// that a reload would take with it.
//
// THE ROUTES THIS PAGE USES, AND WHY THAT SENTENCE IS HERE
// -------------------------------------------------------
// Answers are saved with `PUT /interview/sections/{key}`. The previous cut of
// this page sent every save to `PATCH /interview/answers` — a path that exists
// only for GET — so a person could have answered 155 items over half an hour
// with every single save failing while the page looked fine. There is no
// `POST .../complete` on a section either: finishing one is `complete: true` on
// the same PUT, an optional one is `skip: true`, and the three conversational
// sections are finished by the RUNTIME inside `POST .../turn`. Every path this
// file names is asserted against the built app by
// `backend/tests/test_smart_teams_api_contract.py`.
//
// Answers are written as they are given: a change goes into a pending map, a
// debounced PUT sends only what changed, and the server echoes back the keys it
// actually stored so the client retires exactly those. A save that fails is
// retried twice and then SAID OUT LOUD — the one failure mode this page must
// never have is a person answering thirty items into a void.
//
// SURVIVING A SESSION EXPIRY
// --------------------------
// The shared axios interceptor hard-navigates to /login the moment any request
// 401s, which destroys component state without asking; the session JWT is good
// for five hours and this interview takes half an hour, so that can land at item
// 96 of the Big Five bank. Every answer is therefore ALSO written to a small
// localStorage outbox before the request goes out, and retired from it only when
// the server confirms the key. The next mount of the same interview replays what
// is left before rendering anything. See `interviewFlow.ts` — it is a crash log,
// not a cache, and it is never treated as the truth about what has been answered.
//
// NO SSE, NO WEBSOCKET
// --------------------
// Conversational turns are ordinary POSTs and scoring is polled from the landing
// page — no long-lived stream holds a database connection here either.
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ArrowLeft, ArrowRight, CheckCircle2, ClipboardList, Info, Send } from 'lucide-react';

import { BackButton } from '../components/BackButton';
import { useAuth } from '../context/AuthContext';
import { activeToken } from '../brand/storage';
import { confirmDialog } from '../components/eos/ConfirmDialog';
import { toast } from '../components/eos/Toast';
import { ErrorState, LoadingState } from './eos/_shared';
import { Conversation } from '../components/smartTeams/Conversation';
import { ConfirmationStep } from '../components/smartTeams/ConfirmationStep';
import { ScaleBank } from '../components/smartTeams/ScaleBank';
import { SkillsDeclaration } from '../components/smartTeams/SkillsDeclaration';
import {
    AdvisoryNotice,
    PrimaryButton,
    PrivacyNotice,
    ProgressHeader,
    SaveIndicator,
    SecondaryButton,
    UnsavedBanner,
} from '../components/smartTeams/shared';
import type { Outbox, OutboxScope, SaveState } from '../components/smartTeams/interviewFlow';
import {
    SAVE_DEBOUNCE_MS,
    SAVE_MAX_AUTO_RETRIES,
    clampPage,
    mergeOutbox,
    outboxAppliesTo,
    outboxStorageKey,
    readOutbox,
    resumePage,
    retireFromOutbox,
    writeOutbox,
} from '../components/smartTeams/interviewFlow';
import { createInterviewIdentityGate } from '../components/smartTeams/interviewIdentity';
import type {
    AnswerValue,
    CompletedView,
    EnneagramConfirmationAnswer,
    InterviewProgress,
    Notices,
    ResumeState,
    SectionKey,
    SectionView,
    SkillLevel,
} from '../api/smartTeams';
import {
    PREFIX_META,
    PREFIX_SKILL,
    completeInterview,
    getSection,
    saveSection,
    smartTeamsError,
    startInterview,
    takeTurn,
} from '../api/smartTeams';

/** Section keys are stored ids, so a `?section=` from the URL is checked against
 *  the catalog the server sent rather than cast. */
const isKnownSection = (value: string | null, keys: SectionKey[]): value is SectionKey =>
    !!value && (keys as string[]).includes(value);

export const SmartTeamsInterview: React.FC = () => {
    const { user, token } = useAuth();
    const workspaceSlug = user?.workspace?.slug?.trim().toLowerCase() ?? '';
    const userEmail = user?.email?.trim().toLowerCase() ?? '';
    const outboxScope = useMemo<OutboxScope | null>(() => {
        return workspaceSlug && userEmail ? { workspaceSlug, userEmail } : null;
    }, [workspaceSlug, userEmail]);
    const scopeKey = outboxStorageKey(outboxScope);
    // A new bearer is a new session even when the person signs back into the
    // same workspace. The outbox key remains person/workspace scoped.
    const sessionKey = scopeKey && token ? `${scopeKey}:${token}` : null;
    const currentIdentityRef = useRef(sessionKey);
    useLayoutEffect(() => {
        currentIdentityRef.current = sessionKey;
    }, [sessionKey]);
    const identityIsCurrent = useCallback(
        () => currentIdentityRef.current === sessionKey && activeToken() === token,
        [sessionKey, token],
    );

    // Remount all interview state and refs for each identity. The live identity
    // check also closes the gap before the previous instance's cleanup runs.
    if (!sessionKey || !outboxScope) {
        return <LoadingState label="Opening your interview…" />;
    }
    return (
        <InterviewForIdentity
            key={sessionKey}
            outboxScope={outboxScope}
            identityIsCurrent={identityIsCurrent}
        />
    );
};

const InterviewForIdentity: React.FC<{
    outboxScope: OutboxScope;
    identityIsCurrent: () => boolean;
}> = ({ outboxScope, identityIsCurrent }) => {
    const navigate = useNavigate();
    const [params, setParams] = useSearchParams();

    // The resume state and the two notices are tracked apart: a section save
    // returns a full resume state but NO notices (only the three interview entry
    // points carry them), so folding one into the other would blank the advisory
    // notice the first time somebody answered a question.
    const [resume, setResume] = useState<ResumeState | null>(null);
    const [notices, setNotices] = useState<Notices | null>(null);
    const [alreadyDone, setAlreadyDone] = useState<CompletedView | null>(null);
    const [bootError, setBootError] = useState<string | null>(null);
    const [booting, setBooting] = useState(true);

    const [view, setView] = useState<SectionView | null>(null);
    const [sectionError, setSectionError] = useState<string | null>(null);
    const [loadingSection, setLoadingSection] = useState(false);

    const [answers, setAnswers] = useState<Record<string, AnswerValue>>({});
    const [progress, setProgress] = useState<InterviewProgress | null>(null);

    const [pending, setPending] = useState<Record<string, AnswerValue>>({});
    const [stranded, setStranded] = useState<Outbox | null>(null);
    const [saveState, setSaveState] = useState<SaveState>('idle');
    const [saveError, setSaveError] = useState<string | null>(null);
    const [retrying, setRetrying] = useState(false);

    const [completing, setCompleting] = useState(false);
    const [sending, setSending] = useState(false);
    const [turnError, setTurnError] = useState<string | null>(null);
    const [submitting, setSubmitting] = useState(false);
    const [reloadNonce, setReloadNonce] = useState(0);

    // Refs mirror what the debounced flush needs: it runs outside the render
    // that scheduled it, so reading these off state would send a stale batch to
    // the wrong section.
    const pendingRef = useRef<Record<string, AnswerValue>>({});
    const interviewIdRef = useRef<string | null>(null);
    const activeKeyRef = useRef<SectionKey | null>(null);
    const timerRef = useRef<number | null>(null);
    const attemptRef = useRef(0);
    const inFlightRef = useRef(false);
    const gateRef = useRef<ReturnType<typeof createInterviewIdentityGate> | null>(null);
    if (!gateRef.current) {
        gateRef.current = createInterviewIdentityGate(identityIsCurrent, window);
    }
    const gate = gateRef.current;
    // The answers as they stood when the section loaded. The bank's opening page
    // is derived from THIS, not from live `answers`: derive it from live answers
    // and finishing the tenth item on page 1 would silently flip the view to
    // page 2 mid-run, which is the same jump as clicking Next by accident.
    const loadedAnswersRef = useRef<Record<string, AnswerValue>>({});

    const sectionKeys = useMemo(
        () => (resume?.sections ?? []).map(section => section.key),
        [resume?.sections],
    );

    /** Which section is on screen. The URL wins when it names a section that is
     *  real and not already finished — that is what makes a page refresh, a Back
     *  button and a shared link all land in the same place. Otherwise the
     *  server's RESOLVED resume target wins (`resume_section_key`, not the raw
     *  `section_cursor`): the cursor-points-at-a-finished-section race is settled
     *  in `interview.resume_target` and the client does not re-implement it. */
    const activeKey = useMemo<SectionKey | null>(() => {
        if (!resume) return null;
        const requested = params.get('section');
        if (
            isKnownSection(requested, sectionKeys) &&
            !resume.sections_completed.includes(requested)
        ) {
            return requested;
        }
        return resume.resume_section_key;
    }, [params, sectionKeys, resume]);

    useEffect(() => {
        activeKeyRef.current = activeKey;
    }, [activeKey]);

    // ── Saving ───────────────────────────────────────────────────────────────

    /** Send whatever is pending. Returns false when it did not land, so callers
     *  that must not proceed on unsaved answers (finishing a section, leaving the
     *  page) can stop. */
    const flush = useCallback(async (): Promise<boolean> => {
        const epoch = gate.capture();
        if (!gate.isCurrent(epoch)) return false;
        const sectionKey = activeKeyRef.current;
        const batch = { ...pendingRef.current };
        const keys = Object.keys(batch);
        if (!sectionKey || keys.length === 0) return true;
        if (inFlightRef.current) return false;
        inFlightRef.current = true;
        setSaveState('saving');
        try {
            const result = await saveSection(sectionKey, { answers: batch });
            if (!gate.isCurrent(epoch)) return false;
            // Retire exactly what the server says it stored, not the batch we
            // hoped it stored. A partial accept leaves the rest pending.
            const saved = result.saved_keys.length ? result.saved_keys : keys;
            const savedSet = new Set(saved);
            const remaining: Record<string, AnswerValue> = {};
            for (const [key, value] of Object.entries(pendingRef.current)) {
                if (!savedSet.has(key)) remaining[key] = value;
            }
            pendingRef.current = remaining;
            setPending(remaining);
            writeOutbox(retireFromOutbox(readOutbox(outboxScope), saved), outboxScope);
            setResume(result);
            setProgress(result.progress);
            setSaveError(null);
            attemptRef.current = 0;
            setSaveState(Object.keys(remaining).length ? 'idle' : 'saved');
            // Disclosures (a clipped answer, say) are shown rather than dropped.
            for (const caveat of result.caveats) toast.info(caveat);
            return true;
        } catch (e) {
            if (!gate.isCurrent(epoch)) return false;
            const err = smartTeamsError(e, 'Cubie could not save that answer.');
            setSaveError(err.message);
            setSaveState('failed');
            // Two silent retries: the failures worth swallowing are transient
            // blips. Anything that survives them belongs on screen.
            if (attemptRef.current < SAVE_MAX_AUTO_RETRIES) {
                attemptRef.current += 1;
                const delay = 1500 * attemptRef.current;
                gate.schedule(epoch, () => {
                    void flush();
                }, delay);
            }
            return false;
        } finally {
            if (gate.isCurrent(epoch)) inFlightRef.current = false;
        }
    }, [gate, outboxScope]);

    /** Replay the crash log from a session that died mid-bank. */
    const replay = useCallback(async (outbox: Outbox): Promise<boolean> => {
        const epoch = gate.capture();
        if (!gate.isCurrent(epoch)) return false;
        try {
            const result = await saveSection(outbox.section_cursor, {
                answers: outbox.answers,
            });
            if (!gate.isCurrent(epoch)) return false;
            const saved = result.saved_keys.length
                ? result.saved_keys
                : Object.keys(outbox.answers);
            const left = retireFromOutbox(outbox, saved);
            writeOutbox(left, outboxScope);
            setStranded(left);
            setProgress(result.progress);
            return !left;
        } catch (e) {
            if (!gate.isCurrent(epoch)) return false;
            const err = smartTeamsError(e, 'Cubie could not save your earlier answers.');
            setSaveError(err.message);
            setSaveState('failed');
            setStranded(outbox);
            return false;
        }
    }, [gate, outboxScope]);

    const retryAll = useCallback(async () => {
        const epoch = gate.capture();
        if (!gate.isCurrent(epoch)) return;
        setRetrying(true);
        attemptRef.current = 0;
        try {
            if (stranded) {
                const ok = await replay(stranded);
                if (!ok || !gate.isCurrent(epoch)) return;
            }
            const ok = await flush();
            if (ok && gate.isCurrent(epoch)) toast.success('Saved.');
        } finally {
            if (gate.isCurrent(epoch)) setRetrying(false);
        }
    }, [flush, gate, replay, stranded]);

    const recordAnswer = useCallback(
        (key: string, value: AnswerValue) => {
            const epoch = gate.capture();
            if (!gate.isCurrent(epoch)) return;
            setAnswers(prev => ({ ...prev, [key]: value }));
            pendingRef.current = { ...pendingRef.current, [key]: value };
            setPending(pendingRef.current);
            // Outbox BEFORE the request, not after: the whole point is to have a
            // record of the answer if the request never completes.
            const id = interviewIdRef.current;
            const cursor = activeKeyRef.current;
            if (id && cursor) {
                writeOutbox(
                    mergeOutbox(readOutbox(outboxScope), id, cursor, { [key]: value }),
                    outboxScope,
                );
            }
            setSaveState('idle');
            gate.cancel(timerRef.current);
            timerRef.current = gate.schedule(epoch, () => {
                timerRef.current = null;
                void flush();
            }, SAVE_DEBOUNCE_MS);
        },
        [flush, gate, outboxScope],
    );

    // ── Bootstrap ────────────────────────────────────────────────────────────

    useEffect(() => {
        let cancelled = false;
        const epoch = gate.capture();
        (async () => {
            setBooting(true);
            setBootError(null);
            try {
                // Idempotent by contract: this runs on every mount, including a
                // hard refresh mid-interview, and hands back the row that
                // already exists rather than opening a second one. A COMPLETED
                // interview is never reopened — the response says so, and the
                // page offers a retake instead of silently starting over.
                const opened = await startInterview();
                if (cancelled || !gate.isCurrent(epoch)) return;
                setNotices({
                    advisory_notice: opened.advisory_notice,
                    privacy_notice: opened.privacy_notice,
                });
                if (opened.status === 'completed') {
                    setAlreadyDone(opened);
                    return;
                }
                interviewIdRef.current = opened.interview_id;
                setResume(opened);
                setProgress(opened.progress);
                const outbox = readOutbox(outboxScope);
                if (outbox && outboxAppliesTo(outbox, opened.interview_id)) {
                    const restored = await replay(outbox);
                    if (cancelled || !gate.isCurrent(epoch)) return;
                    if (restored) {
                        toast.success('Picked up the answers from your last session.');
                    }
                }
            } catch (e) {
                if (cancelled || !gate.isCurrent(epoch)) return;
                const err = smartTeamsError(e, 'Cubie could not open your interview.');
                setBootError(
                    err.status === 403
                        ? 'Smart Teams is not switched on for your account.'
                        : err.message,
                );
            } finally {
                if (!cancelled && gate.isCurrent(epoch)) setBooting(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [gate, replay, outboxScope]);

    // Keep the URL naming the section on screen, so a refresh returns here.
    // `replace` so a 155-item interview does not bury the browser Back button.
    useEffect(() => {
        if (!activeKey) return;
        if (params.get('section') === activeKey) return;
        const next = new URLSearchParams(params);
        next.set('section', activeKey);
        next.delete('page');
        setParams(next, { replace: true });
    }, [activeKey, params, setParams]);

    // ── Section load ─────────────────────────────────────────────────────────

    useEffect(() => {
        if (!activeKey) {
            setView(null);
            return;
        }
        let cancelled = false;
        const epoch = gate.capture();
        (async () => {
            setLoadingSection(true);
            setSectionError(null);
            try {
                const next = await getSection(activeKey);
                if (cancelled || !gate.isCurrent(epoch)) return;
                setView(next);
                // Seed the form from what is already stored, then re-apply
                // anything still pending so a failed save is not visually
                // reverted by the reload that follows it.
                loadedAnswersRef.current = { ...next.answers, ...pendingRef.current };
                setAnswers(loadedAnswersRef.current);
                setProgress(next.progress);
                setTurnError(null);
            } catch (e) {
                if (cancelled || !gate.isCurrent(epoch)) return;
                setSectionError(
                    smartTeamsError(e, 'Cubie could not load this section.').message,
                );
            } finally {
                if (!cancelled && gate.isCurrent(epoch)) setLoadingSection(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [activeKey, gate, reloadNonce]);

    // ── Leaving ──────────────────────────────────────────────────────────────

    // `visibilitychange` fires on a mobile background and a tab switch. On
    // unmount, pending answers stay in the scoped outbox for the next mount;
    // starting a save during an auth switch could use the replacement token.
    useEffect(() => {
        gate.activate();
        const onHidden = () => {
            if (document.visibilityState === 'hidden') void flush();
        };
        document.addEventListener('visibilitychange', onHidden);
        return () => {
            document.removeEventListener('visibilitychange', onHidden);
            gate.cancel(timerRef.current);
            timerRef.current = null;
            gate.invalidate();
        };
    }, [flush, gate]);

    // The only beforeunload handler in the app, and it earns its keep: half an
    // hour of answers is a genuinely expensive thing to close by accident. It
    // registers ONLY while something is unsaved, so a normal page never carries
    // a confirm dialog.
    const unsavedCount = Object.keys(pending).length + Object.keys(stranded?.answers ?? {}).length;
    useEffect(() => {
        if (!unsavedCount) return;
        const warn = (event: BeforeUnloadEvent) => {
            event.preventDefault();
            event.returnValue = '';
        };
        window.addEventListener('beforeunload', warn);
        return () => window.removeEventListener('beforeunload', warn);
    }, [unsavedCount]);

    // ── Actions ──────────────────────────────────────────────────────────────

    const goToSection = useCallback(
        (key: SectionKey) => {
            const next = new URLSearchParams(params);
            next.set('section', key);
            next.delete('page');
            setParams(next, { replace: true });
        },
        [params, setParams],
    );

    const setPage = useCallback(
        (page: number) => {
            const next = new URLSearchParams(params);
            next.set('page', String(page));
            setParams(next, { replace: true });
        },
        [params, setParams],
    );

    /** Finish (or skip) the section on screen.
     *
     *  One PUT, with `complete` / `skip` as a REQUEST: the server completes the
     *  section only if the answers on the row support it, so a client that
     *  mis-tracks its own progress cannot move the cursor past a half-answered
     *  bank. */
    const finishSection = useCallback(
        async (options: { skip?: boolean } = {}) => {
            const epoch = gate.capture();
            if (!gate.isCurrent(epoch)) return;
            const key = activeKeyRef.current;
            if (!key) return;
            setCompleting(true);
            try {
                // Never mark a section done over unsaved answers — the server
                // would score a shorter interview than the person gave.
                const saved = await flush();
                if (!gate.isCurrent(epoch)) return;
                if (!saved) {
                    toast.error(
                        'Your last answers have not saved yet — retry before moving on.',
                    );
                    return;
                }
                const result = await saveSection(key, {
                    answers: {},
                    complete: !options.skip,
                    skip: !!options.skip,
                });
                if (!gate.isCurrent(epoch)) return;
                setResume(result);
                setProgress(result.progress);
                for (const caveat of result.caveats) toast.info(caveat);
                if (!result.section_completed && !result.skipped) {
                    toast.error(
                        'That section still has answers outstanding, so Cubie kept you on it.',
                    );
                    return;
                }
                if (result.resume_section_key) goToSection(result.resume_section_key);
            } catch (e) {
                if (gate.isCurrent(epoch)) {
                    toast.error(smartTeamsError(e, 'Cubie could not finish that section.').message);
                }
            } finally {
                if (gate.isCurrent(epoch)) setCompleting(false);
            }
        },
        [flush, gate, goToSection],
    );

    /** One conversational turn.
     *
     *  NOT retried on failure — a blind retry is a second answer to the next
     *  question — so a failure re-reads the section instead, which is what the
     *  router's own docstring asks clients to do. */
    const sendTurn = useCallback(async (text: string): Promise<boolean> => {
        const epoch = gate.capture();
        if (!gate.isCurrent(epoch)) return false;
        const key = activeKeyRef.current;
        if (!key) return false;
        setSending(true);
        setTurnError(null);
        try {
            const result = await takeTurn(key, { answer: text });
            if (!gate.isCurrent(epoch)) return false;
            setView(result);
            setProgress(result.progress);
            setResume(prev =>
                prev
                    ? {
                          ...prev,
                          sections_completed: result.sections_completed,
                          resume_section_key: result.current_section?.key ?? null,
                          progress: result.progress,
                      }
                    : prev,
            );
            for (const caveat of result.caveats) toast.info(caveat);
            if (!result.accepted) {
                setTurnError('That conversation had already finished, so nothing was added.');
            }
            return result.accepted;
        } catch (e) {
            if (!gate.isCurrent(epoch)) return false;
            setTurnError(smartTeamsError(e, 'Cubie could not record that answer.').message);
            // Re-read rather than re-post: the turn may have landed.
            setReloadNonce(n => n + 1);
            return false;
        } finally {
            if (gate.isCurrent(epoch)) setSending(false);
        }
    }, [gate]);

    /** The Enneagram confirmation. Same turn endpoint, no model call: the option
     *  string goes back exactly as it arrived and the server maps it to a type by
     *  position. */
    const submitConfirmation = useCallback(
        async (option: string, typeNumber: number | null) => {
            const epoch = gate.capture();
            if (!gate.isCurrent(epoch)) return;
            const key = activeKeyRef.current;
            if (!key) return;
            setCompleting(true);
            setTurnError(null);
            try {
                const result = await takeTurn(key, { option, type_number: typeNumber });
                if (!gate.isCurrent(epoch)) return;
                setView(result);
                setProgress(result.progress);
                setResume(prev =>
                    prev
                        ? {
                              ...prev,
                              sections_completed: result.sections_completed,
                              resume_section_key: result.current_section?.key ?? null,
                              progress: result.progress,
                          }
                        : prev,
                );
                if (result.current_section) goToSection(result.current_section.key);
            } catch (e) {
                if (!gate.isCurrent(epoch)) return;
                setTurnError(
                    smartTeamsError(e, 'Cubie could not record that answer.').message,
                );
                setReloadNonce(n => n + 1);
            } finally {
                if (gate.isCurrent(epoch)) setCompleting(false);
            }
        },
        [gate, goToSection],
    );

    const declareSkill = useCallback(
        (skillKey: string, level: SkillLevel | null) => {
            recordAnswer(`${PREFIX_SKILL}${skillKey}`, level);
        },
        [recordAnswer],
    );

    const saveAndExit = useCallback(async () => {
        const epoch = gate.capture();
        if (!gate.isCurrent(epoch)) return;
        const ok = await flush();
        if (!gate.isCurrent(epoch)) return;
        if (!ok) {
            toast.error('Some answers have not saved — retry before you leave.');
            return;
        }
        toast.success('Saved. Pick up where you left off whenever you like.');
        navigate('/smart-teams');
    }, [flush, gate, navigate]);

    const submitForScoring = useCallback(async () => {
        const epoch = gate.capture();
        if (!gate.isCurrent(epoch)) return;
        const confirmed = await confirmDialog({
            title: 'Send your answers for scoring?',
            message:
                'Cubie will work through your answers in the background. You can retake the interview later — a retake creates a new version rather than overwriting this one.',
        });
        if (!confirmed || !gate.isCurrent(epoch)) return;
        setSubmitting(true);
        try {
            const result = await completeInterview();
            if (!gate.isCurrent(epoch)) return;
            writeOutbox(null, outboxScope);
            setStranded(null);
            if (result.auto_skipped_sections.length) {
                toast.info(
                    `Cubie skipped ${result.auto_skipped_sections.length} optional section(s) that did not apply to you.`,
                );
            }
            navigate('/smart-teams');
        } catch (e) {
            if (gate.isCurrent(epoch)) {
                toast.error(smartTeamsError(e, 'Cubie could not accept your answers.').message);
            }
        } finally {
            if (gate.isCurrent(epoch)) setSubmitting(false);
        }
    }, [gate, navigate, outboxScope]);

    // ── Render ───────────────────────────────────────────────────────────────

    if (booting) {
        return (
            <div className="mx-auto max-w-3xl p-8">
                <LoadingState label="Opening your interview…" />
            </div>
        );
    }

    if (bootError) {
        return (
            <div className="mx-auto max-w-3xl p-8">
                <BackButton fallback="/smart-teams" />
                <div className="mt-4">
                    <ErrorState message={bootError} />
                </div>
            </div>
        );
    }

    // Already finished, and not reopened: the answers a scored profile cites are
    // never edited underneath it. A retake is a new interview, started from the
    // Smart Teams page.
    if (alreadyDone) {
        return (
            <div className="mx-auto max-w-3xl p-8">
                <BackButton fallback="/smart-teams" label="Smart Teams" />
                <div className="mt-4 rounded-lg border border-gray-200 bg-white p-6 text-center">
                    <CheckCircle2 className="mx-auto text-green-600" size={32} />
                    <h1 className="mt-3 text-xl font-semibold text-gray-900">
                        You have already finished this interview
                    </h1>
                    <p className="mx-auto mt-2 max-w-xl text-sm leading-relaxed text-gray-600">
                        Your answers are in and they are not editable — the profile scored from
                        them cites them, so changing them underneath it would make an approved
                        guide unverifiable. Taking it again starts a fresh version and leaves
                        this one exactly as it is.
                    </p>
                    <div className="mt-5">
                        <PrimaryButton onClick={() => navigate('/smart-teams')}>
                            Back to Smart Teams
                            <ArrowRight size={15} />
                        </PrimaryButton>
                    </div>
                </div>
                <PrivacyNotice text={notices?.privacy_notice} className="mt-4" />
                <AdvisoryNotice text={notices?.advisory_notice} className="mt-3" />
            </div>
        );
    }

    if (!resume) {
        return (
            <div className="mx-auto max-w-3xl p-8">
                <BackButton fallback="/smart-teams" />
                <div className="mt-4">
                    <ErrorState message="No interview to show." />
                </div>
            </div>
        );
    }

    const banner =
        unsavedCount > 0 && saveState === 'failed' ? (
            <UnsavedBanner
                unsavedCount={unsavedCount}
                message={saveError ?? 'The last save did not go through.'}
                onRetry={() => void retryAll()}
                retrying={retrying}
            />
        ) : null;

    // Every section done: the interview is answered but nothing is scored yet,
    // and the page says exactly that rather than implying a profile exists.
    if (!activeKey) {
        return (
            <div className="mx-auto max-w-3xl p-8">
                <BackButton fallback="/smart-teams" />
                <div className="mt-4">
                    {banner}
                    <div className="rounded-lg border border-gray-200 bg-white p-6 text-center">
                        <CheckCircle2 className="mx-auto text-green-600" size={32} />
                        <h1 className="mt-3 text-xl font-semibold text-gray-900">
                            That is every section answered
                        </h1>
                        <p className="mx-auto mt-2 max-w-xl text-sm leading-relaxed text-gray-600">
                            Nothing has been scored yet. When you send these answers, Cubie
                            works through them in the background — a few minutes — and then
                            builds the first draft of your profile for you to review.
                        </p>
                        <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
                            <PrimaryButton onClick={() => void submitForScoring()} busy={submitting}>
                                <Send size={15} />
                                Send my answers for scoring
                            </PrimaryButton>
                            <SecondaryButton onClick={() => navigate('/smart-teams')}>
                                Not yet
                            </SecondaryButton>
                        </div>
                    </div>
                    <AdvisoryNotice text={notices?.advisory_notice} className="mt-4" />
                </div>
            </div>
        );
    }

    const sectionMeta = resume.sections.find(section => section.key === activeKey);
    const stepNumber = resume.sections.findIndex(section => section.key === activeKey) + 1;

    return (
        <div className="mx-auto max-w-3xl p-8">
            <div className="mb-4 flex items-center justify-between gap-3">
                <BackButton fallback="/smart-teams" label="Smart Teams" />
                <div data-tour="smart-teams-interview-save" className="flex items-center gap-3">
                    <SaveIndicator state={saveState} unsavedCount={unsavedCount} />
                    <SecondaryButton onClick={() => void saveAndExit()}>
                        <ArrowLeft size={14} />
                        Save &amp; exit
                    </SecondaryButton>
                </div>
            </div>

            {banner}

            {progress && (
                <ProgressHeader
                    percent={progress.percent}
                    stepsDone={progress.steps_done}
                    stepsTotal={progress.steps_total}
                    minutesRemaining={progress.minutes_remaining}
                />
            )}

            <header data-tour="smart-teams-interview-section" className="mb-5">
                <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-brand">
                    <ClipboardList size={13} />
                    Section {stepNumber} of {resume.sections.length}
                </p>
                <h1 className="mt-1 text-2xl font-semibold text-gray-900">
                    {sectionMeta?.title ?? 'This section'}
                </h1>
                {sectionMeta?.blurb && (
                    <p className="mt-1 text-sm text-gray-500">{sectionMeta.blurb}</p>
                )}
            </header>

            {loadingSection ? (
                <LoadingState label="Loading this section…" />
            ) : sectionError ? (
                <div className="space-y-3">
                    <ErrorState message={sectionError} />
                    <SecondaryButton onClick={() => setReloadNonce(n => n + 1)}>
                        Try again
                    </SecondaryButton>
                </div>
            ) : !view ? (
                <ErrorState message="This section came back empty. Try again in a moment." />
            ) : view.payload.answer_kind === 'likert' || view.payload.answer_kind === 'choice_ab' ? (
                <ScaleBank
                    section={view.section}
                    payload={view.payload}
                    answers={answers}
                    onAnswer={recordAnswer}
                    page={
                        // `?page=` wins when it is there, so a refresh returns to
                        // the same page; otherwise open on the page holding the
                        // first item they had not answered when this loaded.
                        params.get('page')
                            ? clampPage(params.get('page'), view.payload.items.length)
                            : resumePage(
                                  (
                                      view.payload.items as ReadonlyArray<{ item_id: string }>
                                  ).map(item => item.item_id),
                                  loadedAnswersRef.current,
                              )
                    }
                    onPageChange={setPage}
                    onComplete={() => void finishSection()}
                    completing={completing}
                />
            ) : view.payload.answer_kind === 'transcript' ? (
                // A segment that cannot run yet says so in the server's own
                // words. `available: false` on the motivators probe means the
                // questions are chosen from the scored Work values bank and that
                // bank is not finished — so the honest render is the reason plus
                // the two ways forward, NOT an empty question box.
                !view.payload.available || !view.payload.conversation ? (
                    <div className="rounded-lg border border-amber-200 bg-amber-50 p-5">
                        <p className="flex items-start gap-2 text-sm leading-relaxed text-amber-900">
                            <Info className="mt-0.5 flex-shrink-0" size={16} />
                            {view.payload.unavailable_reason ??
                                'This part of the interview is not ready yet.'}
                        </p>
                        <div className="mt-4 flex flex-wrap gap-2">
                            {view.section.optional && (
                                <SecondaryButton
                                    onClick={() => void finishSection({ skip: true })}
                                    busy={completing}
                                >
                                    Skip this section
                                </SecondaryButton>
                            )}
                            <SecondaryButton onClick={() => navigate('/smart-teams')}>
                                Back to Smart Teams
                            </SecondaryButton>
                        </div>
                    </div>
                ) : (
                    <Conversation
                        section={view.section}
                        conversation={view.payload.conversation}
                        onSend={sendTurn}
                        sending={sending}
                        error={turnError}
                        onContinue={() =>
                            resume.resume_section_key
                                ? goToSection(resume.resume_section_key)
                                : navigate('/smart-teams/interview')
                        }
                        continuing={completing}
                    />
                )
            ) : view.payload.answer_kind === 'structured' ? (
                !view.payload.available || !view.payload.confirmation ? (
                    <div className="rounded-lg border border-amber-200 bg-amber-50 p-5">
                        <p className="flex items-start gap-2 text-sm leading-relaxed text-amber-900">
                            <Info className="mt-0.5 flex-shrink-0" size={16} />
                            {view.payload.unavailable_reason ??
                                'This part of the interview is not ready yet.'}
                        </p>
                    </div>
                ) : !view.payload.confirmation.required ? (
                    // Nothing to confirm. Unreachable from this path (the bank is
                    // scored fresh on every request), and refused rather than
                    // guessed at if it ever happens: writing down a type nobody
                    // was asked about is the one outcome this section exists to
                    // prevent.
                    <div className="rounded-lg border border-gray-200 bg-white p-5">
                        <p className="text-sm leading-relaxed text-gray-700">
                            There is nothing to confirm in this section. Your Cubie admin can
                            pick this up — nothing you have answered is lost.
                        </p>
                    </div>
                ) : (
                    <>
                        <ConfirmationStep
                            confirmation={view.payload.confirmation}
                            types={view.payload.types}
                            value={
                                (answers[`${PREFIX_META}${activeKey}`] as
                                    | EnneagramConfirmationAnswer
                                    | undefined) ?? null
                            }
                            onSubmit={(option, typeNumber) =>
                                void submitConfirmation(option, typeNumber)
                            }
                            submitting={completing}
                        />
                        {turnError && (
                            <p role="alert" className="mt-3 text-sm text-red-700">
                                {turnError}
                            </p>
                        )}
                    </>
                )
            ) : (
                <SkillsDeclaration
                    payload={view.payload}
                    answers={answers as Record<string, SkillLevel | null>}
                    onDeclare={declareSkill}
                    onComplete={() => void finishSection()}
                    completing={completing}
                />
            )}

            <AdvisoryNotice text={notices?.advisory_notice} className="mt-6" />
        </div>
    );
};

export default SmartTeamsInterview;
