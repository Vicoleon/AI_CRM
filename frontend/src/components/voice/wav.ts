// Raw microphone samples → the mono 16-bit WAV the server reads: 16 kHz, or
// the device's own rate when it is slower (see toUploadRate).
//
// WAV rather than the browser's own webm/mp4 because the server has to measure
// the clip itself before calling the model (rubik_core/voice/signal.py): it has
// no audio decoder, and plain PCM needs none. It also means every browser sends
// the same format. 16 kHz is all Gemini uses for speech; it is 32 KB a second.
//
// A leaf module with no imports, so Node's test runner can load it directly.

export const TARGET_RATE = 16_000;

/** Lower the sample rate by averaging each output sample's span of input. */
export function downsample(input: Float32Array, fromRate: number, toRate: number): Float32Array {
    if (toRate >= fromRate) return input.slice();
    const ratio = fromRate / toRate;
    const length = Math.floor(input.length / ratio);
    const out = new Float32Array(length);
    for (let i = 0; i < length; i++) {
        const start = Math.floor(i * ratio);
        const end = Math.min(input.length, Math.floor((i + 1) * ratio));
        let sum = 0;
        for (let j = start; j < end; j++) sum += input[j];
        out[i] = end > start ? sum / (end - start) : 0;
    }
    return out;
}

/**
 * The samples to upload and the rate to label them with. Faster devices are
 * brought down to TARGET_RATE; a slower one (a Bluetooth headset in hands-free
 * mode runs at 8 kHz) keeps its own rate, because labelling 8 kHz samples as
 * 16 kHz would play them at double speed and half length. The server reads
 * the rate from the header, so any rate up to TARGET_RATE is fine.
 */
export function toUploadRate(samples: Float32Array, fromRate: number): { samples: Float32Array; rate: number } {
    const rate = Math.min(Math.round(fromRate), TARGET_RATE);
    return { samples: downsample(samples, fromRate, rate), rate };
}

/** Join the recorder's chunks into one buffer. */
export function concat(chunks: readonly Float32Array[]): Float32Array {
    const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
    let offset = 0;
    for (const c of chunks) {
        out.set(c, offset);
        offset += c.length;
    }
    return out;
}

/** Mono 16-bit PCM WAV, little-endian, 44-byte header. */
export function encodeWav(samples: Float32Array, sampleRate: number): ArrayBuffer {
    const dataBytes = samples.length * 2;
    const buffer = new ArrayBuffer(44 + dataBytes);
    const view = new DataView(buffer);
    const ascii = (offset: number, text: string) => {
        for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
    };
    ascii(0, 'RIFF');
    view.setUint32(4, 36 + dataBytes, true);
    ascii(8, 'WAVE');
    ascii(12, 'fmt ');
    view.setUint32(16, 16, true); // fmt chunk size
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true); // byte rate
    view.setUint16(32, 2, true); // block align
    view.setUint16(34, 16, true); // bits per sample
    ascii(36, 'data');
    view.setUint32(40, dataBytes, true);
    for (let i = 0; i < samples.length; i++) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buffer;
}
