// Page tours ("Walk me"): decides when a tour starts, runs it, and remembers
// how it ended.
//
// Mounted once around the app shell (App.tsx), so the account menu can offer
// "Replay this page's tour" and the overlay can sit above every page. What a
// tour SAYS lives in content/; which tour a page gets is logic.ts; this file
// is only timing and memory:
//
//   * Nothing starts until the person's memory of tours has loaded from the
//     server. If it cannot load, the last copy this browser kept is used; with
//     neither, nothing starts (a tour repeating on every visit because the
//     server is down is worse than no tour).
//   * A tour waits for the page to settle: at least a moment after arriving,
//     no dialog open, and its first card's target on screen. A page that never
//     shows that target on this visit (Meeting Prep still building its first
//     version, say) gets its tour on a later visit instead of a tour of a
//     loading screen. Later cards whose target is missing sit in the middle.
//     A tour that starts in a background tab simply waits there.
//   * Nor while the person is typing, and only one tour per visit to a page.
//   * Leaving the page mid-tour closes it without recording anything; it
//     comes back on a later visit (not again in this session). A page that
//     rewrites its own URL (a tab, a tidied link) keeps its tour.
//   * Finishing or skipping is saved at once, on screen first and then on the
//     server, so a slow save never shows the same tour twice.
//   * Never while an admin views Cubie as someone else: the tour would be the
//     admin's, and the server refuses to record it anyway.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import {
    fetchTourState,
    forgetToursSeen,
    markToursSeen,
    setToursEnabled,
    type TourState,
} from '../api/tours';
import { toast } from '../components/eos/Toast';
import { ToursContext, type ToursContextValue } from './context';
import {
    pickAutoRun,
    replayRun,
    settle,
    toursForPage,
    tourAudience,
    visibleTo,
    withFirstTourHint,
    type SeenStatus,
    type TourRun,
} from './logic.ts';
import { REPLAY_HINT } from './content/account.ts';
import { ALL_TOURS } from './registry.ts';
import { findTarget } from './dom.ts';
import { TourOverlay } from './TourOverlay';
import { readTourProgress, tourProgressSubject, writeTourProgress } from '../brand/storage.ts';

/** Least time on a page before a tour starts by itself. */
const SETTLE_MS = 900;
/** Give up starting a tour on this visit: a dialog stayed open, or the
 *  page never showed the first card's target (still building, say). */
const GIVE_UP_MS = 20000;
/** A tour waits until the person has not pressed a key for this long. */
const QUIET_MS = 1500;
const POLL_MS = 250;

interface ActiveRun {
    run: TourRun;
    index: number;
    mode: 'auto' | 'replay';
    /** The page the run belongs to. A page that rewrites its own URL (a tab,
     *  a `?session=` link tidied away) keeps the run; leaving the page ends it. */
    pathname: string;
    /** What had focus when the run began; it gets focus back at the end. */
    returnFocus: HTMLElement | null;
}

function focusedElement(): HTMLElement | null {
    const el = document.activeElement;
    return el instanceof HTMLElement && el !== document.body ? el : null;
}

export const TourProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const { user, token } = useAuth();
    const location = useLocation();
    const navigate = useNavigate();

    const email = (user?.email ?? '').toLowerCase();
    const inSupport = Boolean(user?.support);
    const progressSubject = tourProgressSubject(email, user?.workspace?.slug, inSupport);
    // The saved fallback is scoped by workspace. The in-memory state also
    // identifies a support session by token so a late save from a prior visit
    // to the same workspace cannot replace the current session's state.
    const stateIdentity = inSupport
        ? `support:${progressSubject ?? 'unknown'}:${token ?? ''}`
        : progressSubject;
    const features = user?.features;
    const available = !!user && features?.page_tours === true && !user.impersonation;

    const [loadedState, setLoadedState] = useState<{ identity: string; state: TourState | null } | null>(null);
    const [active, setActive] = useState<ActiveRun | null>(null);
    // Leaving the page ends the run there and then, so going Back or Forward
    // to it later never brings a half-finished tour back.
    const [runPath, setRunPath] = useState(location.pathname);
    if (runPath !== location.pathname) {
        setRunPath(location.pathname);
        if (active) setActive(null);
    }
    // Tours already started this session, so one closed by leaving the page
    // does not chase the person around.
    const startedRef = useRef<Set<string>>(new Set());
    // The navigation on which the last tour ended: one tour per visit, so
    // skipping a page's tour never sets off the next one (a tab's own tour)
    // on the same visit.
    const endedOnRef = useRef<string | null>(null);
    const lastKeyAtRef = useRef(-Infinity);
    const skipHintShownRef = useRef(false);

    useEffect(() => {
        if (!available || !stateIdentity) return;
        let cancelled = false;
        fetchTourState()
            .then(state => {
                if (cancelled) return;
                if (progressSubject) writeTourProgress(progressSubject, state);
                setLoadedState({ identity: stateIdentity, state });
            })
            .catch(() => {
                if (!cancelled) setLoadedState({
                    identity: stateIdentity,
                    state: progressSubject ? readTourProgress(progressSubject) : null,
                });
            });
        return () => {
            cancelled = true;
        };
    }, [available, stateIdentity, progressSubject]);

    // When the person last pressed a key: a tour never starts while someone is
    // typing, or their next Space or Enter would press its buttons.
    useEffect(() => {
        if (!available) return;
        const onKey = () => {
            lastKeyAtRef.current = performance.now();
        };
        window.addEventListener('keydown', onKey, true);
        return () => window.removeEventListener('keydown', onKey, true);
    }, [available]);

    // The state for THIS person; a previous person's (after a sign-out) never counts.
    const state = available && loadedState?.identity === stateIdentity ? loadedState.state : null;

    const roles = user?.roles;
    const isPlatformSupport = inSupport;
    const has = useMemo(() => tourAudience(features, roles, isPlatformSupport), [features, roles, isPlatformSupport]);
    // Every tour this person can see anywhere: what an intro may settle.
    const visibleTours = useMemo(
        () => (available ? ALL_TOURS.filter(t => visibleTo(t, has)) : []),
        [available, has],
    );
    const pageTours = useMemo(
        () => (available ? toursForPage(ALL_TOURS, location.pathname, location.search, has) : []),
        [available, location.pathname, location.search, has],
    );

    const current = active && active.pathname === location.pathname ? active : null;

    // Start a tour by itself when the page calls for one.
    useEffect(() => {
        if (!state || !state.enabled || current || endedOnRef.current === location.key) return;
        const run = withFirstTourHint(pickAutoRun(pageTours, state.seen, visibleTours), state.seen, REPLAY_HINT);
        if (!run || startedRef.current.has(run.key)) return;
        const arrivedAt = performance.now();
        const path = location.pathname;
        let timer = 0;
        const tick = () => {
            const now = performance.now();
            const waited = now - arrivedAt;
            const quiet = now - lastKeyAtRef.current >= QUIET_MS;
            const dialogOpen = document.querySelector('[aria-modal="true"]') !== null;
            const first = run.steps[0]?.target;
            const targetReady = !first || findTarget(first) !== null;
            if (waited >= SETTLE_MS && quiet && !dialogOpen && targetReady) {
                startedRef.current.add(run.key);
                setActive({ run, index: 0, mode: 'auto', pathname: path, returnFocus: focusedElement() });
                return;
            }
            if (waited < GIVE_UP_MS) timer = window.setTimeout(tick, POLL_MS);
        };
        timer = window.setTimeout(tick, POLL_MS);
        return () => window.clearTimeout(timer);
    }, [state, current, pageTours, visibleTours, location.key, location.pathname]);

    const end = useCallback(
        (status: SeenStatus) => {
            if (!current) return;
            endedOnRef.current = location.key;
            setActive(null);
            const ids = current.run.settles;
            if (state) {
                const optimistic: TourState = { ...state, seen: settle(state.seen, ids, status) };
                setLoadedState({ identity: stateIdentity!, state: optimistic });
                if (progressSubject) writeTourProgress(progressSubject, optimistic);
            }
            markToursSeen(ids, status)
                .then(saved => {
                    if (progressSubject) writeTourProgress(progressSubject, saved);
                    setLoadedState(currentState => currentState?.identity === stateIdentity
                        ? { identity: stateIdentity, state: saved }
                        : currentState);
                })
                .catch(() => {
                    // Kept on screen and in this browser; the tour may come back
                    // once on another device. Not worth interrupting anyone over.
                });
            if (status === 'skipped' && current.mode === 'auto' && !skipHintShownRef.current) {
                skipHintShownRef.current = true;
                toast.info('Tour skipped. You can replay it any time from the account menu, under your name at the bottom left.');
            }
        },
        [current, state, stateIdentity, progressSubject, location.key],
    );

    const replay = useCallback(() => {
        const run = replayRun(pageTours, visibleTours);
        if (run) {
            setActive({ run, index: 0, mode: 'replay', pathname: location.pathname, returnFocus: focusedElement() });
        }
    }, [pageTours, visibleTours, location.pathname]);

    const setEnabled = useCallback(
        async (on: boolean) => {
            const saved = await setToursEnabled(on);
            if (progressSubject) writeTourProgress(progressSubject, saved);
            setLoadedState(currentState => currentState?.identity === stateIdentity
                ? { identity: stateIdentity, state: saved }
                : currentState);
        },
        [progressSubject, stateIdentity],
    );

    const resetAll = useCallback(async () => {
        const saved = await forgetToursSeen();
        if (progressSubject) writeTourProgress(progressSubject, saved);
        // Every tour comes back on the NEXT visit to its page. The page the
        // person is on (Settings, where the button is) would otherwise start
        // its tour the instant they click.
        const here = pickAutoRun(pageTours, {}, visibleTours);
        startedRef.current = new Set(here ? [here.key] : []);
        setLoadedState(currentState => currentState?.identity === stateIdentity
            ? { identity: stateIdentity, state: saved }
            : currentState);
    }, [progressSubject, stateIdentity, pageTours, visibleTours]);

    const value = useMemo<ToursContextValue>(
        () => ({
            available,
            loaded: state !== null,
            enabled: state?.enabled ?? true,
            pageHasTour: replayRun(pageTours) !== null,
            replay,
            setEnabled,
            resetAll,
        }),
        [available, state, pageTours, replay, setEnabled, resetAll],
    );

    const openHelp = useCallback(
        (topic: string) => {
            end('completed');
            navigate(`/help/${topic}`);
        },
        [end, navigate],
    );

    return (
        <ToursContext.Provider value={value}>
            {children}
            {current && (
                <TourOverlay
                    key={current.run.key + current.mode}
                    run={current.run}
                    index={current.index}
                    onIndex={i => setActive(a => (a ? { ...a, index: i } : a))}
                    onFinish={() => end('completed')}
                    onSkip={() => end('skipped')}
                    onOpenHelp={openHelp}
                    returnFocus={current.returnFocus}
                />
            )}
        </ToursContext.Provider>
    );
};
