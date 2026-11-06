// Did anybody actually speak? Decided in the browser, BEFORE a clip is sent.
//
// Gemini invents speech from silence: probed on 2026-10-05, silence and steady
// noise came back as plausible work questions in 14 of 15 tries, under every
// prompt wording tried (rubik_core/voice/transcribe.py). So a clip in which the
// microphone never rose above the room is dropped here and never uploaded.
// The server runs the SAME check on the WAV it receives
// (rubik_core/voice/signal.py) — same 50 ms frames, same thresholds — so a
// silent clip that slips past this one still never reaches the model.
//
// A leaf module with no imports, so Node's test runner can load it directly:
//
//   cd <repo>/frontend && node --test "src/components/voice/voiceActivity.test.ts"

/** One reading of the microphone: its loudness and how long it stood for. */
export interface LevelSample {
    rms: number;
    ms: number;
}

// Keep in step with rubik_core/voice/signal.py.
export const FRAME_MS = 50;
// Browsers record with noise suppression and automatic gain on, so a quiet
// room reads well under 0.005 and normal speech 0.03–0.3. A frame counts as
// speech when it is above this absolute floor AND clearly above the room.
export const SPEECH_RMS_FLOOR = 0.015;
export const SPEECH_OVER_ROOM = 3;
// A short "yes" or "send it" is ~300 ms of voiced sound; less than this is a
// click, a cough or a bump of the desk.
export const MIN_SPEECH_MS = 250;

/** Root-mean-square loudness of one buffer of samples in [-1, 1]. */
export function rms(samples: ArrayLike<number>): number {
    if (samples.length === 0) return 0;
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    return Math.sqrt(sum / samples.length);
}

/** The clip cut into FRAME_MS frames, each with its loudness. A trailing
 *  partial frame is dropped, as the server does. */
export function levelsFromPcm(samples: Float32Array, sampleRate: number): LevelSample[] {
    const size = Math.max(1, Math.floor((sampleRate * FRAME_MS) / 1000));
    const out: LevelSample[] = [];
    for (let start = 0; start + size <= samples.length; start += size) {
        out.push({ rms: rms(samples.subarray(start, start + size)), ms: FRAME_MS });
    }
    return out;
}

/** The room's loudness: the quietest tenth of the recording. */
export function roomLevel(samples: readonly LevelSample[]): number {
    if (samples.length === 0) return 0;
    const sorted = samples.map(s => s.rms).sort((a, b) => a - b);
    return sorted[Math.floor((sorted.length - 1) * 0.1)];
}

/** Milliseconds of the recording loud enough, and above the room enough, to be a voice. */
export function voicedMs(samples: readonly LevelSample[]): number {
    const threshold = Math.max(SPEECH_RMS_FLOOR, roomLevel(samples) * SPEECH_OVER_ROOM);
    let total = 0;
    for (const s of samples) if (s.rms >= threshold) total += s.ms;
    return total;
}

export function heardSpeech(samples: readonly LevelSample[]): boolean {
    return voicedMs(samples) >= MIN_SPEECH_MS;
}

/** What a key does while the mic is busy. Enter stops and keeps the clip
 *  (Ask Cubie then sends it), Escape throws it away; while the words are being
 *  written down Enter does nothing, so a second press cannot send a
 *  half-typed message ahead of them. `null` = leave the key alone: a modified
 *  Enter (Shift+Enter is a new line) and anything an input method is still
 *  composing belong to whoever has focus. */
export type VoiceKeyAction = 'stop' | 'discard' | 'swallow' | null;

/** The parts of a KeyboardEvent the decision reads (plain objects in tests). */
export interface KeyLike {
    key: string;
    isComposing?: boolean;
    keyCode?: number;
    shiftKey?: boolean;
    altKey?: boolean;
    ctrlKey?: boolean;
    metaKey?: boolean;
}

export function voiceKeyAction(phase: 'recording' | 'transcribing', e: KeyLike): VoiceKeyAction {
    // Safari fires the Enter that confirms an IME conversion with
    // isComposing=false but keyCode 229, so both shapes count as composing.
    if (e.isComposing || e.keyCode === 229) return null;
    if (e.key === 'Escape') return 'discard';
    if (e.key !== 'Enter') return null;
    if (e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return null;
    return phase === 'recording' ? 'stop' : 'swallow';
}

/** Text to append after `existing` so dictation never glues onto the last word. */
export function joinDictation(existing: string, addition: string): string {
    const words = addition.trim();
    if (!words) return '';
    if (!existing || /\s$/.test(existing)) return words;
    return ` ${words}`;
}

/** m:ss for the recording timer. */
export function formatClock(totalSeconds: number): string {
    const s = Math.max(0, Math.floor(totalSeconds));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
