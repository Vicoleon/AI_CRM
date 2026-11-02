// Voice commands: the Voice button in the sidebar, its keyboard shortcut, and
// the panel that shows what Cubie heard and what it will do.
//
// One command at a time: press the button (or tap the shortcut — Option+Command
// on a Mac, Ctrl+Alt on a PC, changeable in Settings), say it, and press again
// (or Enter) to run it; or hold the shortcut while talking and let go. Esc
// throws it away. Then, by what Cubie read (backend/routers/voice_commands.py):
//
//   * a page          → it opens, from this person's own sidebar only;
//   * a question      → it goes to Ask Cubie, as if typed there;
//   * an EOS change   → a card says exactly what will change, and nothing is
//                       sent until the person confirms: Enter, the button, or
//                       the shortcut again and "yes". The card's request is the
//                       one the page's own button sends, made as the person.
//
// The panel is portalled to <body>: the sidebar clips its content, and is
// hidden altogether during a live L10, where the shortcut must still work.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useLocation, useNavigate } from 'react-router-dom';
import axios from 'axios';
import { Check, CheckCircle2, Loader2, Mic, MessageSquare, Square, X } from 'lucide-react';

import client from '../../api/client';
import { announceVoiceChange, placeAllowed, runVoiceCommand, sendVoicePlan } from '../../api/voiceCommands';
import type { VoiceCommandResult, VoiceDestination, VoicePlace, VoicePlan } from '../../api/voiceCommands';
import { canRecordAudio, useVoiceRecorder } from './useVoiceRecorder';
import type { StopReason } from './useVoiceRecorder';
import {
    ChordTracker,
    VOICE_SHORTCUT_EVENT,
    chordLabel,
    chordWords,
    detectPlatform,
    effectiveChord,
    isChordKey,
    shortcutSuspended,
} from './shortcut';
import type { Chord, VoiceShortcutPref } from './shortcut';
import { formatClock } from './voiceActivity';

// Matches VOICE_COMMAND_MAX_SECONDS on the server.
const MAX_SECONDS = 30;
// A modifier chord only starts listening after this long held, or when it is
// let go sooner (a tap): long enough that AltGr+Q (typing "@") is over first.
const ARM_DELAY_MS = 250;
// Held at least this long, letting go of the shortcut runs the command
// (hold-to-talk); a quicker press leaves the microphone on (tap-to-talk).
const HOLD_MS = 700;
// An Enter this soon after a card appears was typed for something else (the
// field the person was in), never a yes to a card they have not read.
const CARD_SETTLE_MS = 400;
const NOTE_MS = 7000;
const QUICK_NOTE_MS = 2500;

type View =
    // `at`: the page the card was shown on. Leaving that page puts it away, so
    // a card never sits waiting over somewhere else.
    | { kind: 'card'; plan: VoicePlan; heard: string; hint?: string; at: string }
    | { kind: 'choose'; options: VoicePlan[]; prompt: string; heard: string; at: string }
    // A named record: one to confirm (an L10), or several to pick from.
    | { kind: 'places'; places: VoicePlace[]; prompt: string; heard: string; at: string }
    | { kind: 'sending'; plan: VoicePlan }
    | { kind: 'done'; plan: VoicePlan }
    | { kind: 'note'; text: string; heard?: string; ask?: string | null; quick?: boolean };

/** Keys are ours only when nobody else has a use for them: focus in the panel,
 *  or on nothing in particular. A dialog, a menu or a field keeps its keys. */
function ours(target: EventTarget | null, panel: HTMLElement | null): boolean {
    if (!(target instanceof Node)) return true;
    if (target === document.body || target === document.documentElement) return true;
    return !!panel?.contains(target);
}

function errorText(err: unknown, fallback: string): string {
    const detail = axios.isAxiosError<{ detail?: unknown }>(err) ? err.response?.data?.detail : undefined;
    return typeof detail === 'string' && detail ? detail : fallback;
}

/** The card in one sentence, for the "did they say yes?" question. */
function cardQuestion(plan: VoicePlan): string {
    return `${plan.title}: ${plan.fields.map(f => `${f.label} ${f.value}`).join('; ')}`;
}

const NOTHING_HEARD = "I didn't hear anything. Check that the right microphone is on and try again.";

export const VoiceCommandBar: React.FC<{
    /** The nav panel is open, so the button has room for its label. */
    expanded: boolean;
    /** Where this person can go: their own sidebar, flattened. */
    destinations: VoiceDestination[];
}> = ({ expanded, destinations }) => {
    const navigate = useNavigate();
    const location = useLocation();
    const [platform] = useState(detectPlatform);
    const [pref, setPref] = useState<VoiceShortcutPref | null>(null);
    const chord: Chord | null = useMemo(() => effectiveChord(pref, platform), [pref, platform]);

    const [stored, setView] = useState<View | null>(null);
    // A card or a "which one?" belongs to the page it was shown on: moving to
    // another page puts it away for good (React's "adjust state when a prop
    // changes" pattern, not an effect), and it is hidden in the meantime.
    const [seenPath, setSeenPath] = useState(location.pathname);
    if (seenPath !== location.pathname) {
        setSeenPath(location.pathname);
        if (stored && (stored.kind === 'card' || stored.kind === 'choose' || stored.kind === 'places') && stored.at !== location.pathname) {
            setView(null);
        }
    }
    const view = stored && (stored.kind === 'card' || stored.kind === 'choose' || stored.kind === 'places') && stored.at !== location.pathname
        ? null
        : stored;
    const [working, setWorking] = useState(false);
    // 'confirm' while listening for the yes/no to a card on screen.
    const [mode, setMode] = useState<'command' | 'confirm'>('command');
    const requestRef = useRef<AbortController | null>(null);
    const noteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const panelRef = useRef<HTMLDivElement>(null);
    const confirmRef = useRef<HTMLButtonElement>(null);
    const firstOptionRef = useRef<HTMLButtonElement>(null);
    // Where focus was before a card took it, given back when the panel closes.
    const restoreRef = useRef<HTMLElement | null>(null);
    const cardShownAt = useRef(0);
    // Esc (or another shortcut) asked to throw this attempt away. Checked when
    // the microphone comes up, when the clip arrives and after every await, so
    // a cancel lands whichever moment it comes in.
    const discardRef = useRef(false);
    // A stop asked for while the microphone was still starting.
    const stopWhenReady = useRef(false);
    // Read when needed rather than when a closure was made: a command takes a
    // few seconds, and must use the panel, page and menu as they are THEN.
    const latest = useRef({ view, mode, destinations, path: location.pathname });
    useEffect(() => {
        latest.current = { view, mode, destinations, path: location.pathname };
    });

    // The saved shortcut, and any change Settings makes to it.
    useEffect(() => {
        let alive = true;
        client.get('/api/preferences')
            .then(res => { if (alive) setPref(res.data?.voice_shortcut ?? null); })
            .catch(() => { /* the default shortcut stands */ });
        const onChange = (e: Event) => setPref((e as CustomEvent<VoiceShortcutPref | null>).detail ?? null);
        window.addEventListener(VOICE_SHORTCUT_EVENT, onChange);
        return () => {
            alive = false;
            window.removeEventListener(VOICE_SHORTCUT_EVENT, onChange);
        };
    }, []);

    const clearNoteTimer = () => {
        if (noteTimer.current) clearTimeout(noteTimer.current);
        noteTimer.current = null;
    };

    const show = useCallback((next: View | null) => {
        clearNoteTimer();
        setView(next);
        // Notes and "done" go by themselves, except a note offering a next
        // step (Ask Cubie instead), which waits for the person.
        if (next && ((next.kind === 'note' && !next.ask) || next.kind === 'done')) {
            const quick = next.kind === 'note' && next.quick;
            noteTimer.current = setTimeout(() => setView(null), quick ? QUICK_NOTE_MS : NOTE_MS);
        }
    }, []);

    const askCubie = useCallback((text: string) => {
        navigate('/chat', {
            state: { cubieAsk: { text, id: Date.now() } },
            replace: latest.current.path === '/chat',
        });
        show({ kind: 'note', text: 'Asking Cubie…', heard: text, quick: true });
    }, [navigate, show]);

    const openPlace = useCallback((place: VoicePlace, heard: string) => {
        navigate(place.path, place.state ? { state: place.state } : undefined);
        show({ kind: 'note', text: `Opening ${place.label}`, heard, quick: true });
    }, [navigate, show]);

    const handleResult = useCallback((result: VoiceCommandResult) => {
        const { heard } = result;
        const here = latest.current.path;
        if (result.kind === 'navigate' && result.destination) {
            // Only a page from this person's own menu, whatever the answer says.
            const path = result.destination.path;
            const known = latest.current.destinations.find(d => d.path === path);
            if (known) {
                navigate(path);
                show({ kind: 'note', text: `Opening ${known.label}`, heard, quick: true });
                return;
            }
        } else if (result.kind === 'open' && result.places?.length) {
            // Only the record pages Cubie opens, whatever the answer says.
            const places = result.places.filter(p => placeAllowed(p.path));
            if (places.length === 1 && !result.ask_first) {
                openPlace(places[0], heard);
                return;
            }
            if (places.length) {
                show({ kind: 'places', places, prompt: result.prompt || 'Which one?', heard, at: here });
                return;
            }
        } else if (result.kind === 'ask' && result.ask) {
            askCubie(result.ask);
            return;
        } else if (result.kind === 'action' && result.plan) {
            show({ kind: 'card', plan: result.plan, heard, at: here });
            return;
        } else if (result.kind === 'action' && result.options?.length) {
            show({ kind: 'choose', options: result.options, prompt: result.prompt || 'Which one?', heard, at: here });
            return;
        } else if (result.kind === 'message') {
            show({ kind: 'note', text: result.message || "Cubie can't do that yet.", heard, ask: result.ask });
            return;
        } else if (result.kind === 'none') {
            show({ kind: 'note', text: NOTHING_HEARD });
            return;
        }
        show({ kind: 'note', text: "Cubie couldn't work out that command. Please try again.", heard });
    }, [askCubie, navigate, openPlace, show]);

    // Every card is sent at most once. Marked before React re-renders, so a
    // second Enter (or a held one) that lands before the screen catches up can
    // never send the same change twice.
    const sentRef = useRef(new WeakSet<VoicePlan>());
    const confirm = useCallback(async (plan: VoicePlan) => {
        if (sentRef.current.has(plan)) return;
        sentRef.current.add(plan);
        show({ kind: 'sending', plan });
        try {
            await sendVoicePlan(plan);
            announceVoiceChange(plan.open_path);
            show({ kind: 'done', plan });
        } catch (err) {
            show({ kind: 'note', text: errorText(err, "That didn't go through. Nothing was changed.") });
        }
    }, [show]);

    const handleClip = async (clip: Blob, heard: boolean, reason: StopReason) => {
        if (discardRef.current) return;
        const { mode, view: current } = latest.current;
        const card = mode === 'confirm' && current?.kind === 'card' ? current : null;
        if (reason === 'limit') {
            // A microphone left on is never acted on; a card waiting for its
            // yes stays up, so the answer can still be given.
            show(card
                ? { ...card, hint: 'Stopped listening. Press Enter to go ahead, or Esc to cancel.' }
                : { kind: 'note', text: `Stopped after ${MAX_SECONDS} seconds, and nothing was done. Try a shorter command.` });
            return;
        }
        if (!heard) {
            show(card
                ? { ...card, hint: "I didn't hear anything. Say “yes” or “no”, or press Enter or Esc." }
                : { kind: 'note', text: NOTHING_HEARD });
            return;
        }
        const controller = new AbortController();
        requestRef.current = controller;
        setWorking(true);
        try {
            if (card) {
                const result = await runVoiceCommand({
                    clip, expect: 'confirm', question: cardQuestion(card.plan), signal: controller.signal,
                });
                if (discardRef.current) return;
                if (result.answer === 'yes') void confirm(card.plan);
                else if (result.answer === 'no') show({ kind: 'note', text: 'Cancelled. Nothing was changed.', quick: true });
                else show({ ...card, hint: 'Say “yes” or “no”, or press Enter or Esc.' });
                return;
            }
            const result = await runVoiceCommand({
                clip,
                destinations: latest.current.destinations,
                page: latest.current.path,
                signal: controller.signal,
            });
            if (discardRef.current) return;
            handleResult(result);
        } catch (err) {
            if (axios.isCancel(err) || discardRef.current) return;
            const text = errorText(err, "Cubie couldn't work out that command. Please try again.");
            show(card ? { ...card, hint: `${text} Press Enter to go ahead, or Esc to cancel.` } : { kind: 'note', text });
        } finally {
            if (requestRef.current === controller) requestRef.current = null;
            setWorking(false);
        }
    };

    const { state, elapsed, level, start, stop, cancel } = useVoiceRecorder({
        maxSeconds: MAX_SECONDS,
        onClip: handleClip,
        onError: text => show({ kind: 'note', text }),
    });
    const listening = state === 'starting' || state === 'recording';
    const busy = working || state === 'transcribing' || view?.kind === 'sending';

    // A cancel or a stop asked for while the microphone was still starting
    // (the permission prompt, a slow device) is carried out once it is up.
    useEffect(() => {
        if (state !== 'recording') return;
        if (discardRef.current) cancel();
        else if (stopWhenReady.current) {
            stopWhenReady.current = false;
            stop();
        }
    }, [state, cancel, stop]);

    const begin = () => {
        if (busy || listening) return;
        discardRef.current = false;
        stopWhenReady.current = false;
        // With a card on screen, what is said next is the answer to it.
        const confirming = latest.current.view?.kind === 'card';
        latest.current.mode = confirming ? 'confirm' : 'command';
        setMode(latest.current.mode);
        if (confirming) clearNoteTimer();
        else show(null);
        void start();
    };

    /** Stop listening and run what was said. */
    const finish = () => {
        if (state === 'starting') stopWhenReady.current = true;
        else stop();
    };

    /** Throw away what is being said or worked out. */
    const discard = () => {
        discardRef.current = true;
        stopWhenReady.current = false;
        cancel();
        requestRef.current?.abort();
        setWorking(false);
    };

    // The newest handlers, for the long-lived key listeners below.
    const actions = useRef({ begin, finish, discard, confirm, listening, busy });
    useEffect(() => {
        actions.current = { begin, finish, discard, confirm, listening, busy };
    });

    // ── The shortcut ──
    useEffect(() => {
        if (!chord) return;
        const tracker = new ChordTracker(chord);
        let armTimer: ReturnType<typeof setTimeout> | null = null;
        let armedAt = 0;
        let wasListening = false;
        let startedByPress = false;
        const clearArm = () => {
            if (armTimer) clearTimeout(armTimer);
            armTimer = null;
        };
        const onKey = (e: KeyboardEvent) => {
            if (shortcutSuspended()) return;
            const type = e.type === 'keydown' ? 'keydown' : 'keyup';
            const signal = tracker.handle(type, e);
            if (isChordKey(chord, e)) e.preventDefault();
            const a = actions.current;
            if (signal === 'arm') {
                armedAt = performance.now();
                wasListening = a.listening;
                startedByPress = false;
                if (!wasListening && !a.busy) {
                    armTimer = setTimeout(() => {
                        armTimer = null;
                        startedByPress = true;
                        actions.current.begin();
                    }, ARM_DELAY_MS);
                }
            } else if (signal === 'cancel') {
                clearArm();
                // It was the start of another shortcut (or AltGr), not ours.
                if (startedByPress) actions.current.discard();
                startedByPress = false;
            } else if (signal === 'release') {
                e.preventDefault();
                const held = performance.now() - armedAt;
                if (armTimer) {
                    clearArm();
                    a.begin();
                } else if (startedByPress) {
                    if (held >= HOLD_MS) a.finish();
                } else if (wasListening) {
                    a.finish();
                }
                startedByPress = false;
            }
        };
        const onBlur = () => {
            tracker.reset();
            clearArm();
        };
        window.addEventListener('keydown', onKey, true);
        window.addEventListener('keyup', onKey, true);
        window.addEventListener('blur', onBlur);
        return () => {
            clearArm();
            window.removeEventListener('keydown', onKey, true);
            window.removeEventListener('keyup', onKey, true);
            window.removeEventListener('blur', onBlur);
        };
    }, [chord]);

    // ── Enter and Escape ──
    // While listening or working, both are the panel's wherever focus is: the
    // person is talking to Cubie, and an Enter that reached the Chat box would
    // send whatever was typed there. A card or a list claims them only when
    // focus is in the panel or on nothing in particular, so a dialog, a menu
    // or a field opened meanwhile keeps its own Enter and Escape. Notes and
    // "done" claim nothing.
    const active = listening || busy;
    const holdsKeys = active || view?.kind === 'card' || view?.kind === 'choose' || view?.kind === 'places';
    useEffect(() => {
        if (!holdsKeys) return;
        const onKey = (e: KeyboardEvent) => {
            if (e.defaultPrevented || e.isComposing || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
            if (e.key !== 'Escape' && e.key !== 'Enter') return;
            const a = actions.current;
            const current = latest.current.view;
            const claim = () => {
                e.preventDefault();
                e.stopPropagation();
            };
            if (a.listening || a.busy) {
                if (e.key === 'Escape') {
                    claim();
                    a.discard();
                    if (current?.kind !== 'sending') show(null);
                } else if (a.listening && !e.repeat) {
                    claim();
                    a.finish();
                }
                return;
            }
            if (!ours(e.target, panelRef.current)) return;
            if (e.key === 'Escape') {
                claim();
                show(null);
                return;
            }
            if (e.repeat || current?.kind !== 'card') return;
            if (performance.now() - cardShownAt.current < CARD_SETTLE_MS) {
                // Typed for the field the card took focus from.
                claim();
                return;
            }
            // A focused button in the panel answers its own Enter.
            if (e.target instanceof HTMLButtonElement) return;
            claim();
            void a.confirm(current.plan);
        };
        window.addEventListener('keydown', onKey, true);
        return () => window.removeEventListener('keydown', onKey, true);
    }, [holdsKeys, show]);

    // A card or a list takes focus, so Enter answers it and Tab reaches its
    // buttons; focus goes back where it was when the panel is put away.
    const viewKind = view?.kind ?? null;
    useEffect(() => {
        if (viewKind === 'card' || viewKind === 'choose' || viewKind === 'places') {
            const before = document.activeElement;
            if (before instanceof HTMLElement && !panelRef.current?.contains(before) && before !== document.body) {
                restoreRef.current = before;
            }
            if (viewKind === 'card') {
                cardShownAt.current = performance.now();
                confirmRef.current?.focus({ preventScroll: true });
            } else {
                firstOptionRef.current?.focus({ preventScroll: true });
            }
        } else if (viewKind !== 'sending') {
            const back = restoreRef.current;
            restoreRef.current = null;
            const focusIsLost = !document.activeElement || document.activeElement === document.body
                || !!panelRef.current?.contains(document.activeElement);
            if (back?.isConnected && focusIsLost) back.focus({ preventScroll: true });
        }
    }, [viewKind, view]);

    useEffect(() => () => {
        clearNoteTimer();
        requestRef.current?.abort();
    }, []);

    if (!canRecordAudio()) return null;

    const shortcutText = chord ? chordLabel(chord, platform) : null;
    const shortcutWords = chord ? chordWords(chord, platform) : null;
    const runKeys = shortcutText ? `${shortcutText} or Enter` : 'Enter';

    const panelOpen = active || view !== null;
    let body: React.ReactNode = null;
    const card = view?.kind === 'card' ? view : null;
    if (listening || working || state === 'transcribing') {
        const confirming = mode === 'confirm' && card;
        body = (
            <>
                {confirming && <PlanCard plan={card.plan} heard={card.heard} />}
                <div className="flex items-center gap-3 px-4 py-3">
                    {listening ? (
                        <>
                            <span
                                className="h-2.5 w-2.5 rounded-full bg-red-500 animate-pulse"
                                style={{ boxShadow: `0 0 0 ${2 + level * 8}px rgba(239, 68, 68, 0.2)` }}
                                aria-hidden
                            />
                            <span className="text-sm font-semibold text-gray-900">
                                {confirming ? 'Listening for yes or no…' : 'Listening…'}
                            </span>
                            <span className="text-xs tabular-nums text-gray-500" aria-hidden>{formatClock(elapsed)}</span>
                            <span className="ml-auto text-xs text-gray-400">{runKeys} · Esc to cancel</span>
                        </>
                    ) : (
                        <>
                            <Loader2 size={16} className="animate-spin text-brand" aria-hidden />
                            <span className="text-sm font-semibold text-gray-900">Working on it…</span>
                            <span className="ml-auto text-xs text-gray-400">Esc to cancel</span>
                        </>
                    )}
                </div>
            </>
        );
    } else if (card) {
        body = (
            <>
                <PlanCard plan={card.plan} heard={card.heard} />
                {card.hint && <p className="px-4 pt-2 text-xs text-amber-700">{card.hint}</p>}
                <div className="flex items-center gap-2 px-4 py-3">
                    <button
                        ref={confirmRef}
                        type="button"
                        onClick={() => void confirm(card.plan)}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-1"
                    >
                        <Check size={15} aria-hidden /> {card.plan.confirm}
                    </button>
                    <button
                        type="button"
                        onClick={() => show(null)}
                        className="rounded-lg px-3 py-1.5 text-sm font-semibold text-gray-600 hover:bg-gray-100"
                    >
                        Cancel
                    </button>
                    <span className="ml-auto text-xs text-gray-400">
                        Enter{shortcutText ? `, or ${shortcutText} and say “yes”` : ''} · Esc to cancel
                    </span>
                </div>
            </>
        );
    } else if (view?.kind === 'choose') {
        body = (
            <div className="px-4 py-3">
                {view.heard && <Heard text={view.heard} />}
                <p className="text-sm font-semibold text-gray-900">{view.prompt}</p>
                <ul className="mt-2 space-y-1">
                    {view.options.map((option, i) => (
                        <li key={`${option.request.path}-${i}`}>
                            <button
                                ref={i === 0 ? firstOptionRef : undefined}
                                type="button"
                                onClick={() => show({ kind: 'card', plan: option, heard: view.heard, at: view.at })}
                                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-left text-sm text-gray-800 hover:border-brand/40 hover:bg-brand/5"
                            >
                                {option.option || option.title}
                            </button>
                        </li>
                    ))}
                </ul>
                <p className="mt-2 text-xs text-gray-400">Pick one · Esc to cancel</p>
            </div>
        );
    } else if (view?.kind === 'places') {
        body = (
            <div className="px-4 py-3">
                {view.heard && <Heard text={view.heard} />}
                <p className="text-sm font-semibold text-gray-900">{view.prompt}</p>
                <ul className="mt-2 space-y-1">
                    {view.places.map((place, i) => (
                        <li key={place.path}>
                            <button
                                ref={i === 0 ? firstOptionRef : undefined}
                                type="button"
                                onClick={() => openPlace(place, view.heard)}
                                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-left text-sm text-gray-800 hover:border-brand/40 hover:bg-brand/5"
                            >
                                <span className="block font-medium">{place.label}</span>
                                {place.detail && <span className="block text-xs text-gray-500">{place.detail}</span>}
                            </button>
                        </li>
                    ))}
                </ul>
                <p className="mt-2 text-xs text-gray-400">
                    {view.places.length === 1 ? 'Enter to open' : 'Pick one'} · Esc to cancel
                </p>
            </div>
        );
    } else if (view?.kind === 'sending') {
        body = (
            <>
                <PlanCard plan={view.plan} heard="" />
                <div className="flex items-center gap-2 px-4 py-3 text-sm text-gray-700">
                    <Loader2 size={16} className="animate-spin text-brand" aria-hidden /> Saving…
                </div>
            </>
        );
    } else if (view?.kind === 'done') {
        const here = view.plan.open_path && location.pathname === view.plan.open_path;
        body = (
            <div className="flex items-center gap-2 px-4 py-3">
                <CheckCircle2 size={18} className="text-green-600" aria-hidden />
                <span className="text-sm font-semibold text-gray-900">{view.plan.done}</span>
                {view.plan.open_path && !here && (
                    <button
                        type="button"
                        onClick={() => { navigate(view.plan.open_path!); show(null); }}
                        className="ml-auto rounded-lg px-2.5 py-1 text-sm font-semibold text-brand hover:bg-brand/10"
                    >
                        Open {view.plan.open_label}
                    </button>
                )}
            </div>
        );
    } else if (view?.kind === 'note') {
        body = (
            <div className="px-4 py-3">
                {view.heard && <Heard text={view.heard} />}
                <div className="flex items-start gap-2">
                    <p className="text-sm text-gray-800">{view.text}</p>
                    {view.ask && (
                        <button
                            type="button"
                            onClick={() => askCubie(view.ask!)}
                            className="ml-auto inline-flex flex-shrink-0 items-center gap-1 rounded-lg px-2.5 py-1 text-sm font-semibold text-brand hover:bg-brand/10"
                        >
                            <MessageSquare size={14} aria-hidden /> Ask Cubie instead
                        </button>
                    )}
                </div>
            </div>
        );
    }

    const panel = panelOpen && body ? createPortal(
        <div
            ref={panelRef}
            role="dialog"
            aria-label="Voice command"
            aria-live="polite"
            className="fixed left-1/2 top-4 z-[130] w-[min(36rem,calc(100vw-2rem))] -translate-x-1/2 overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-2xl shadow-gray-900/20"
        >
            <div className="flex items-center gap-2 border-b border-gray-100 px-4 py-2">
                <Mic size={14} className="text-brand" aria-hidden />
                <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">Voice command</span>
                <button
                    type="button"
                    onClick={() => { if (listening || busy) discard(); show(null); }}
                    className="ml-auto rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                    aria-label="Close"
                >
                    <X size={14} />
                </button>
            </div>
            {body}
        </div>,
        document.body,
    ) : null;

    const title = listening
        ? 'Stop and run the command'
        : `Voice command${shortcutWords ? ` (${shortcutWords})` : ''}: open a page, ask Cubie, or change an EOS item`;

    return (
        <>
            <button
                data-tour="voice-command-button"
                type="button"
                onClick={listening ? finish : begin}
                disabled={busy}
                aria-label={listening ? 'Stop and run the voice command' : 'Voice command'}
                title={title}
                className={`w-full flex items-center transition-colors hover:bg-white/5 focus:outline-none focus-visible:bg-white/10 disabled:cursor-wait ${
                    listening ? 'bg-white/10 text-white' : 'text-gray-200 hover:text-white'
                } ${expanded ? 'gap-2.5 px-4 py-2.5' : 'justify-center py-2.5'}`}
            >
                <span className="relative flex-shrink-0">
                    {listening
                        ? <Square size={expanded ? 15 : 17} fill="currentColor" aria-hidden="true" />
                        : busy
                            ? <Loader2 size={expanded ? 17 : 19} className="animate-spin" aria-hidden="true" />
                            : <Mic size={expanded ? 17 : 19} aria-hidden="true" />}
                </span>
                {expanded && (
                    <>
                        <span className="text-sm truncate">{listening ? 'Listening…' : 'Voice'}</span>
                        {shortcutText && !listening && (
                            <kbd className="ml-auto rounded bg-white/10 px-1.5 py-0.5 font-sans text-[11px] text-white/80">
                                {shortcutText}
                            </kbd>
                        )}
                    </>
                )}
            </button>
            {panel}
        </>
    );
};

const Heard: React.FC<{ text: string }> = ({ text }) => (
    <p className="mb-1.5 text-xs italic text-gray-500">“{text}”</p>
);

const PlanCard: React.FC<{ plan: VoicePlan; heard: string }> = ({ plan, heard }) => (
    <div className="px-4 pt-3">
        {heard && <Heard text={heard} />}
        <p className="text-sm font-bold text-gray-900">{plan.title}</p>
        <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
            {plan.fields.map(field => (
                <React.Fragment key={field.label}>
                    <dt className="text-gray-500">{field.label}</dt>
                    <dd className="text-gray-900 break-words">{field.value}</dd>
                </React.Fragment>
            ))}
        </dl>
    </div>
);

export default VoiceCommandBar;
