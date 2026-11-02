// Microphone → one WAV clip (16 kHz, or the device's own rate if slower),
// with a live loudness reading.
//
// Raw samples are captured with an AudioWorklet (not MediaRecorder) so the clip
// can be sent as plain PCM WAV, which the server measures without a decoder
// (see wav.ts). When the person stops, the clip is judged by `heardSpeech`
// before anything is uploaded. The microphone is released the moment recording
// stops, is cancelled, or the component unmounts — the browser's "in use" dot
// must never outlive the button.
import { useCallback, useEffect, useRef, useState } from 'react';

import { heardSpeech, levelsFromPcm, rms } from './voiceActivity';
import { concat, encodeWav, toUploadRate } from './wav';

export type RecorderState = 'idle' | 'starting' | 'recording' | 'transcribing';

/** Why recording ended: the person stopped it, or it reached `maxSeconds`.
 *  A surface that sends on stop must only send on 'user' — a mic left on by
 *  mistake would otherwise send whatever the room said two minutes later. */
export type StopReason = 'user' | 'limit';

interface Options {
    /** Recording stops by itself at this mark and is transcribed. */
    maxSeconds: number;
    /** The finished WAV clip, whether a voice was heard in it, and why it ended. */
    onClip: (clip: Blob, heardSpeech: boolean, reason: StopReason) => Promise<void> | void;
    onError: (message: string) => void;
}

interface Session {
    stream: MediaStream;
    audio: AudioContext;
    source: MediaStreamAudioSourceNode;
    node: AudioWorkletNode;
    chunks: Float32Array[];
    frames: number;
    ending: boolean;
    flushed: Promise<void>;
    markFlushed: () => void;
}

const PROCESSOR = 'cubie-pcm-capture';
// How long the audio engine gets to start. iOS Safari can leave resume()
// pending forever once the click that asked for it has expired; without a
// limit the button would spin with the microphone on and nothing to stop it.
const RESUME_TIMEOUT_MS = 3000;

function within<T>(promise: Promise<T>, ms: number): Promise<T> {
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new DOMException('audio did not start', 'TimeoutError')), ms);
        promise.then(
            value => { clearTimeout(timer); resolve(value); },
            err => { clearTimeout(timer); reject(err); },
        );
    });
}
// Batches of 2048 samples (~43 ms at 48 kHz) keep the message rate near 23 a
// second instead of one per 128-sample render quantum. 'flush' sends the
// partial batch and answers {done}, so the last words are never cut off.
const PROCESSOR_SOURCE = `
class CubiePcmCapture extends AudioWorkletProcessor {
    constructor() {
        super();
        this.buf = new Float32Array(2048);
        this.n = 0;
        this.port.onmessage = (e) => {
            if (e.data === 'flush') {
                if (this.n > 0) this.port.postMessage(this.buf.slice(0, this.n));
                this.n = 0;
                this.port.postMessage({ done: true });
            }
        };
    }
    process(inputs) {
        const channel = inputs[0] && inputs[0][0];
        if (channel) {
            for (let i = 0; i < channel.length; i++) {
                this.buf[this.n++] = channel[i];
                if (this.n === this.buf.length) {
                    this.port.postMessage(this.buf.slice(0));
                    this.n = 0;
                }
            }
        }
        return true;
    }
}
registerProcessor('${PROCESSOR}', CubiePcmCapture);
`;

// One microphone at a time across the page: the Ask Cubie mic and the Voice
// command bar both listen for Enter and Esc, so two live recordings would both
// act on one key press. Whoever holds this owns the microphone.
let micOwner: symbol | null = null;

let processorUrl: string | null = null;
function processorModuleUrl(): string {
    processorUrl ??= URL.createObjectURL(new Blob([PROCESSOR_SOURCE], { type: 'application/javascript' }));
    return processorUrl;
}

export function canRecordAudio(): boolean {
    return typeof window !== 'undefined'
        && window.isSecureContext
        && typeof AudioWorkletNode !== 'undefined'
        && !!navigator.mediaDevices?.getUserMedia;
}

function microphoneError(err: unknown): string {
    const name = err instanceof DOMException ? err.name : '';
    if (name === 'NotAllowedError' || name === 'SecurityError') {
        return 'Microphone access is blocked. Allow it for this site in your browser and try again.';
    }
    if (name === 'NotFoundError' || name === 'OverconstrainedError') {
        return 'No microphone was found. Plug one in or pick one in your system settings.';
    }
    if (name === 'NotReadableError') {
        return 'Another app is using the microphone. Close it and try again.';
    }
    if (name === 'TimeoutError') {
        return "The browser didn't start the microphone. Click the mic again to retry.";
    }
    return "Cubie couldn't start the microphone. Please try again.";
}

function release(session: Session): void {
    session.node.port.onmessage = null;
    session.source.disconnect();
    session.node.disconnect();
    session.stream.getTracks().forEach(t => t.stop());
    void session.audio.close().catch(() => { /* already closed */ });
}

export function useVoiceRecorder({ maxSeconds, onClip, onError }: Options) {
    const [state, setState] = useState<RecorderState>('idle');
    const [elapsed, setElapsed] = useState(0);
    // 0..1, for the pulsing ring while recording.
    const [level, setLevel] = useState(0);
    const sessionRef = useRef<Session | null>(null);
    const [ownerId] = useState(() => Symbol('recorder'));
    // The microphone and context while start() is still awaiting, so an
    // unmount can release them: closing the context also rejects a resume()
    // that would otherwise never settle (iOS can leave it pending).
    const pendingRef = useRef<{ stream: MediaStream | null; audio: AudioContext } | null>(null);
    const startingRef = useRef(false);
    const mountedRef = useRef(true);
    // The latest callbacks, so a clip finishing after a re-render reaches the
    // current handlers rather than the ones captured when recording began.
    const handlersRef = useRef({ onClip, onError });
    useEffect(() => {
        handlersRef.current = { onClip, onError };
    }, [onClip, onError]);

    // Unmounting mid-recording drops the clip and frees the microphone.
    useEffect(() => {
        mountedRef.current = true;
        return () => {
            mountedRef.current = false;
            if (micOwner === ownerId) micOwner = null;
            const pending = pendingRef.current;
            pendingRef.current = null;
            if (pending) {
                pending.stream?.getTracks().forEach(t => t.stop());
                void pending.audio.close().catch(() => { /* already closed */ });
            }
            const session = sessionRef.current;
            sessionRef.current = null;
            if (session) {
                session.ending = true;
                release(session);
            }
        };
    }, [ownerId]);

    /** End the session; `keep` transcribes it, otherwise it is thrown away. */
    const finish = useCallback(async (session: Session, keep: boolean, reason: StopReason = 'user') => {
        if (session.ending) return;
        session.ending = true;
        if (sessionRef.current === session) sessionRef.current = null;
        if (keep) {
            // Show "writing it down" at once, not after the flush and the
            // encode: until then the button would still read as recording,
            // and an Esc pressed in that gap would reach a stop that has
            // already happened instead of the discard it means.
            setState('transcribing');
            session.node.port.postMessage('flush');
            await Promise.race([session.flushed, new Promise(r => setTimeout(r, 500))]);
        }
        const rate = session.audio.sampleRate;
        release(session);
        if (micOwner === ownerId) micOwner = null;
        setLevel(0);
        setElapsed(0);
        // Unmounted during the flush: nothing is left to show the words in,
        // so the clip is dropped rather than sent to be transcribed.
        if (!keep || !mountedRef.current) {
            setState('idle');
            return;
        }
        const upload = toUploadRate(concat(session.chunks), rate);
        const heard = heardSpeech(levelsFromPcm(upload.samples, upload.rate));
        const clip = new Blob([encodeWav(upload.samples, upload.rate)], { type: 'audio/wav' });
        setState('transcribing');
        try {
            await handlersRef.current.onClip(clip, heard, reason);
        } finally {
            setState('idle');
        }
    }, [ownerId]);

    const start = async () => {
        if (startingRef.current || sessionRef.current) return;
        if (!canRecordAudio()) {
            handlersRef.current.onError('This browser cannot record audio here.');
            return;
        }
        if (micOwner && micOwner !== ownerId) {
            handlersRef.current.onError('Another recording is already on. Finish or cancel it first.');
            return;
        }
        micOwner = ownerId;
        startingRef.current = true;
        setState('starting');
        let audio: AudioContext | null = null;
        let stream: MediaStream | null = null;
        try {
            // Created, and asked to run, inside the click and before any await:
            // Safari only starts audio from a user gesture.
            const ctx = new AudioContext();
            audio = ctx;
            const resumed = ctx.state === 'suspended' ? ctx.resume() : Promise.resolve();
            resumed.catch(() => { /* awaited below; never an unhandled rejection */ });
            const pending = { stream: null as MediaStream | null, audio: ctx };
            pendingRef.current = pending;
            // After every await: a page left (or remounted) meanwhile owns no
            // part of this attempt, and must not get a live microphone nobody
            // can stop. Ownership, not just "mounted": a remount resets that
            // flag, but its cleanup has already closed this context.
            const stillOwned = () => {
                if (!mountedRef.current || pendingRef.current !== pending) {
                    throw new DOMException('unmounted', 'AbortError');
                }
            };
            stream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
            });
            pending.stream = stream;
            stillOwned();
            await ctx.audioWorklet.addModule(processorModuleUrl());
            stillOwned();

            const source = ctx.createMediaStreamSource(stream);
            const node = new AudioWorkletNode(ctx, PROCESSOR, { numberOfOutputs: 1, outputChannelCount: [1] });
            let markFlushed = () => {};
            const flushed = new Promise<void>(resolve => { markFlushed = resolve; });
            const session: Session = {
                stream, audio: ctx, source, node, chunks: [], frames: 0, ending: false, flushed, markFlushed,
            };
            node.port.onmessage = (e: MessageEvent) => {
                const data: unknown = e.data;
                if (data instanceof Float32Array) {
                    session.chunks.push(data);
                    session.frames += data.length;
                    setLevel(Math.min(1, rms(data) * 8));
                    const seconds = session.frames / ctx.sampleRate;
                    setElapsed(seconds);
                    if (seconds >= maxSeconds) void finish(session, true, 'limit');
                } else if (data && typeof data === 'object' && 'done' in data) {
                    session.markFlushed();
                }
            };
            // The processor writes silence; connecting it to the output is what
            // makes the browser run it.
            source.connect(node);
            node.connect(ctx.destination);
            await within(resumed, RESUME_TIMEOUT_MS);
            stillOwned();
            pendingRef.current = null;
            sessionRef.current = session;
            setState('recording');
        } catch (err) {
            stream?.getTracks().forEach(t => t.stop());
            if (micOwner === ownerId) micOwner = null;
            if (audio) void audio.close().catch(() => { /* already closed */ });
            if (pendingRef.current?.audio === audio) pendingRef.current = null;
            if (mountedRef.current) {
                setState('idle');
                handlersRef.current.onError(microphoneError(err));
            }
        } finally {
            startingRef.current = false;
        }
    };

    /** Stop and transcribe. */
    const stop = useCallback(() => {
        const session = sessionRef.current;
        if (session) void finish(session, true, 'user');
    }, [finish]);

    /** Stop and throw the clip away. */
    const cancel = useCallback(() => {
        const session = sessionRef.current;
        if (session) void finish(session, false);
    }, [finish]);

    return { state, elapsed, level, start, stop, cancel };
}
