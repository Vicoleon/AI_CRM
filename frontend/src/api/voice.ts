// Voice input API client (/api/voice). One call: a recorded clip in, the words
// out. Mirrors backend/routers/voice.py. The server stores nothing.
import client from './client';

/** Where the words will land. The server uses it only to tell the model what
 *  kind of text it is writing; an unknown value is treated as chat. */
export type VoiceSurface = 'chat';

export interface TranscribeResult {
    text: string;
    /** True when nobody spoke (the server checks the audio before calling the
     *  model), or the model's answer was thrown away as invented. */
    no_speech: boolean;
    seconds: number | null;
}

/** `clip` is the WAV useVoiceRecorder makes (16 kHz, or slower devices at
 *  their own rate); the server refuses other formats. */
export async function transcribeRecording(
    clip: Blob,
    surface: VoiceSurface,
    signal?: AbortSignal,
): Promise<TranscribeResult> {
    const form = new FormData();
    form.append('audio', clip, 'recording.wav');
    form.append('surface', surface);
    const { data } = await client.post<TranscribeResult>('/api/voice/transcribe', form, {
        signal,
        // A two-minute clip transcribes in a few seconds; this only bounds a hang.
        timeout: 60_000,
    });
    return data;
}
