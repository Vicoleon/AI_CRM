// The microphone button: press to talk, press again (or Enter) to stop, and the
// words arrive through `onTranscript`. What happens next is the surface's call:
// Ask Cubie sends them straight away (`sendsOnStop` only changes the wording
// here); a form would just fill the box. Escape throws the clip away, while
// recording or while it is being written down. Reusable on any text box — pass
// the surface the words are for.
import React, { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { Loader2, Mic, Square } from 'lucide-react';

import { getBrandColor } from '../../brand/colors';
import { transcribeRecording } from '../../api/voice';
import type { VoiceSurface } from '../../api/voice';
import { canRecordAudio, useVoiceRecorder } from './useVoiceRecorder';
import type { StopReason } from './useVoiceRecorder';
import { formatClock, voiceKeyAction } from './voiceActivity';

// Matches VOICE_MAX_SECONDS on the server, which refuses anything longer.
const MAX_SECONDS = 120;
const NOTICE_MS = 6000;

export interface DictationInfo {
    /** 'user' = stopped with the button or Enter; 'limit' = hit MAX_SECONDS. */
    reason: StopReason;
}

interface Props {
    surface: VoiceSurface;
    /** The words, and why recording ended. Return a sentence to show by the mic
     *  (say, why the words were left in the box instead of sent), or nothing. */
    onTranscript: (text: string, info: DictationInfo) => string | void;
    /** The surface sends the words when the person stops (Ask Cubie). Only
     *  the button's wording depends on it; the sending is `onTranscript`'s. */
    sendsOnStop?: boolean;
    /** Enter and Escape are claimed only while focus is inside this element,
     *  on the mic, or nowhere — never in another field on the page. */
    keyScope?: React.RefObject<HTMLElement | null>;
    /** The surface's own claim on Enter/Escape comes first (an open @mention
     *  list picks with Enter and closes with Escape). */
    keyClaimed?: () => boolean;
    /** Runs inside the click or key press that stops the mic: a user gesture,
     *  for anything that needs one, and the moment to note what the person
     *  was looking at when they finished speaking. */
    onUserStop?: () => void;
    /** Whether the mic is busy (starting, listening or writing it down). */
    onBusyChange?: (busy: boolean) => void;
    disabled?: boolean;
}

export const VoiceInputButton: React.FC<Props> = ({
    surface,
    onTranscript,
    sendsOnStop = false,
    keyScope,
    keyClaimed,
    onUserStop,
    onBusyChange,
    disabled = false,
}) => {
    const [notice, setNotice] = useState<string | null>(null);
    const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const requestRef = useRef<AbortController | null>(null);
    const wrapperRef = useRef<HTMLDivElement>(null);
    // Set by Escape and read before the upload and again before the words are
    // handed over, so a discard lands whichever moment it comes in.
    const discardRef = useRef(false);
    // The newest props, read when they are needed rather than when a closure
    // was made: transcription takes a few seconds, and a send must use the
    // conversation as it is THEN, not as it was at the click.
    const latest = useRef({ onTranscript, keyScope, keyClaimed, onUserStop, onBusyChange });
    useEffect(() => {
        latest.current = { onTranscript, keyScope, keyClaimed, onUserStop, onBusyChange };
    }, [onTranscript, keyScope, keyClaimed, onUserStop, onBusyChange]);

    const showNotice = (message: string) => {
        if (noticeTimer.current) clearTimeout(noticeTimer.current);
        setNotice(message);
        noticeTimer.current = setTimeout(() => setNotice(null), NOTICE_MS);
    };

    const handleClip = async (clip: Blob, heard: boolean, reason: StopReason) => {
        if (discardRef.current) return;
        if (!heard) {
            showNotice("I didn't hear anything. Check that the right microphone is on and try again.");
            return;
        }
        const controller = new AbortController();
        requestRef.current = controller;
        try {
            const result = await transcribeRecording(clip, surface, controller.signal);
            if (discardRef.current) return;
            if (result.no_speech || !result.text.trim()) {
                showNotice("I couldn't make out any words. Try again a little closer to the microphone.");
            } else {
                const note = latest.current.onTranscript(result.text, { reason });
                if (note) showNotice(note);
            }
        } catch (err) {
            if (axios.isCancel(err)) return;
            const detail = axios.isAxiosError<{ detail?: string }>(err) ? err.response?.data?.detail : undefined;
            showNotice(typeof detail === 'string' && detail
                ? detail
                : "Cubie couldn't turn that recording into text. Please try again.");
        } finally {
            if (requestRef.current === controller) requestRef.current = null;
        }
    };

    const { state, elapsed, level, start, stop, cancel } = useVoiceRecorder({
        maxSeconds: MAX_SECONDS,
        onClip: handleClip,
        onError: showNotice,
    });
    const recording = state === 'recording';
    const busy = state === 'starting' || state === 'transcribing';

    useEffect(() => {
        latest.current.onBusyChange?.(state !== 'idle');
    }, [state]);
    useEffect(() => () => latest.current.onBusyChange?.(false), []);

    // Stopping is a user gesture: the surface hears about it first, inside it.
    const userStop = () => {
        latest.current.onUserStop?.();
        stop();
    };

    // Enter stops (and, in Ask Cubie, sends); Escape throws the clip away.
    // Listened for on window in the CAPTURE phase and stopped there, so the
    // chat box's own Enter-to-send never also fires and sends a half-typed
    // message without the dictated words. Only claimed while focus is in the
    // surface's box, on the mic, or nowhere, and never while the surface has
    // its own use for the key (an open @mention list).
    const phase = recording ? 'recording' : state === 'transcribing' ? 'transcribing' : null;
    useEffect(() => {
        if (!phase) return;
        const inScope = (target: EventTarget | null): boolean => {
            if (!(target instanceof Node)) return true;
            if (target === document.body || target === document.documentElement) return true;
            if (wrapperRef.current?.contains(target)) return true;
            return !!latest.current.keyScope?.current?.contains(target);
        };
        const onKey = (e: KeyboardEvent) => {
            if (!inScope(e.target) || latest.current.keyClaimed?.()) return;
            const action = voiceKeyAction(phase, e);
            if (!action) return;
            e.preventDefault();
            e.stopPropagation();
            if (action === 'stop') {
                latest.current.onUserStop?.();
                stop();
            } else if (action === 'discard') {
                discardRef.current = true;
                cancel();
                requestRef.current?.abort();
            }
        };
        window.addEventListener('keydown', onKey, true);
        return () => window.removeEventListener('keydown', onKey, true);
    }, [phase, stop, cancel]);

    // Leaving the page mid-transcription drops the answer rather than writing
    // it into a box that no longer exists.
    useEffect(() => () => {
        if (requestRef.current) {
            console.debug('[voice] transcription cancelled: the mic left the page');
            requestRef.current.abort();
        }
        if (noticeTimer.current) clearTimeout(noticeTimer.current);
    }, []);

    if (!canRecordAudio()) {
        return (
            <button
                type="button"
                disabled
                className="p-2 rounded-lg text-gray-300 cursor-not-allowed"
                title="Voice input needs a browser that can record audio on a secure (https) page."
                aria-label="Voice input unavailable in this browser"
            >
                <Mic size={18} />
            </button>
        );
    }

    let status: React.ReactNode = null;
    if (recording) {
        status = (
            <span className="flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" aria-hidden />
                Listening… {formatClock(elapsed)}
                <span className="text-gray-400">
                    · Enter to {sendsOnStop ? 'send' : 'stop'} · Esc to cancel
                </span>
            </span>
        );
    } else if (state === 'transcribing') {
        status = (
            <span>
                Writing it down… <span className="text-gray-400">· Esc to cancel</span>
            </span>
        );
    } else if (notice) {
        status = notice;
    }

    return (
        <div data-tour="chat-voice" className="relative" ref={wrapperRef}>
            {status && (
                <div
                    role="status"
                    aria-live="polite"
                    className="absolute bottom-full right-0 mb-3 w-max max-w-[18rem] rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-700 shadow-md"
                >
                    {status}
                </div>
            )}
            <button
                type="button"
                onClick={recording
                    ? userStop
                    : () => { discardRef.current = false; setNotice(null); void start(); }}
                disabled={disabled || busy}
                aria-pressed={recording}
                aria-label={recording
                    ? (sendsOnStop ? 'Stop recording and send' : 'Stop recording and transcribe')
                    : 'Dictate with your voice'}
                title={recording
                    ? (sendsOnStop ? 'Stop and send what you said' : 'Stop and turn what you said into text')
                    : (sendsOnStop
                        ? 'Speak your question; it sends when you stop (Enter or this button). Esc cancels.'
                        : 'Dictate: speak, then stop to fill in the box')}
                style={recording ? { boxShadow: `0 0 0 ${2 + level * 10}px ${getBrandColor('brand', 0.18)}` } : undefined}
                className={`p-2 rounded-lg transition-colors disabled:cursor-not-allowed ${recording
                    ? 'bg-brand text-white hover:bg-brand-hover'
                    : 'text-gray-500 hover:text-brand hover:bg-brand/10 disabled:opacity-60'}`}
            >
                {state === 'transcribing' || state === 'starting'
                    ? <Loader2 size={18} className="animate-spin" />
                    : recording
                        ? <Square size={18} fill="currentColor" />
                        : <Mic size={18} />}
            </button>
        </div>
    );
};
