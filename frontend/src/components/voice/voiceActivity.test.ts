// Regression tests for the "did anybody speak?" check that runs before a
// voice clip is uploaded.
//
//   cd <repo>/frontend && node --test "src/components/voice/voiceActivity.test.ts"
//
// What is pinned here:
//
//   * Silence and steady room noise are NOT speech. Gemini writes whole
//     sentences for both (2026-10-05 probe), so this is the line that keeps an
//     invented sentence out of someone's chat box.
//   * A short real answer IS speech, including in a noisy room.
//   * Dictation appended to a half-typed message never glues onto its last word.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    formatClock,
    heardSpeech,
    joinDictation,
    levelsFromPcm,
    MIN_SPEECH_MS,
    rms,
    voiceKeyAction,
    voicedMs,
} from './voiceActivity.ts';
import type { LevelSample } from './voiceActivity.ts';
import { concat, downsample, encodeWav, TARGET_RATE, toUploadRate } from './wav.ts';

const frames = (level: number, count: number, ms = 50): LevelSample[] =>
    Array.from({ length: count }, () => ({ rms: level, ms }));

describe('heardSpeech', () => {
    it('rejects digital silence', () => {
        assert.equal(heardSpeech(frames(0, 60)), false);
    });

    it('rejects a quiet room', () => {
        assert.equal(heardSpeech(frames(0.004, 80)), false);
    });

    it('rejects a loud but steady room (a fan, an air conditioner)', () => {
        // Above the absolute floor the whole time, but never above the room.
        assert.equal(heardSpeech(frames(0.03, 80)), false);
    });

    it('rejects a single click or desk bump', () => {
        assert.equal(heardSpeech([...frames(0.002, 40), ...frames(0.4, 2), ...frames(0.002, 40)]), false);
    });

    it('accepts a short spoken answer in a quiet room', () => {
        const clip = [...frames(0.003, 20), ...frames(0.08, 8), ...frames(0.003, 20)];
        assert.ok(voicedMs(clip) >= MIN_SPEECH_MS);
        assert.equal(heardSpeech(clip), true);
    });

    it('accepts speech over a steady noisy room', () => {
        const clip = [...frames(0.03, 40), ...frames(0.15, 30), ...frames(0.03, 40)];
        assert.equal(heardSpeech(clip), true);
    });

    it('counts real elapsed time, not frames (a throttled background tab)', () => {
        // Four 1-second readings of speech: 4000 ms, not 4 frames' worth.
        const clip = [...frames(0.002, 10), ...frames(0.1, 4, 1000)];
        assert.equal(voicedMs(clip), 4000);
    });

    it('treats an empty recording as no speech', () => {
        assert.equal(heardSpeech([]), false);
    });
});

describe('rms', () => {
    it('is zero for silence and the amplitude for a square wave', () => {
        assert.equal(rms([0, 0, 0]), 0);
        assert.equal(rms([0.5, -0.5, 0.5, -0.5]), 0.5);
        assert.equal(rms([]), 0);
    });
});

describe('levelsFromPcm', () => {
    it('cuts 50 ms frames and drops a trailing partial one, as the server does', () => {
        const pcm = new Float32Array(16_000 + 300).fill(0.1); // 1 s plus a sliver
        const levels = levelsFromPcm(pcm, 16_000);
        assert.equal(levels.length, 20);
        assert.ok(levels.every(l => l.ms === 50 && Math.abs(l.rms - 0.1) < 1e-6));
    });

    it('feeds heardSpeech end to end: a tone burst in silence is speech, silence is not', () => {
        const pcm = new Float32Array(16_000 * 2);
        for (let i = 8_000; i < 16_000; i++) pcm[i] = 0.2 * Math.sin(i / 5); // 0.5 s
        assert.equal(heardSpeech(levelsFromPcm(pcm, 16_000)), true);
        assert.equal(heardSpeech(levelsFromPcm(new Float32Array(32_000), 16_000)), false);
    });
});

describe('downsample', () => {
    it('48 kHz to 16 kHz keeps the duration and averages each span', () => {
        const input = new Float32Array(48_000);
        for (let i = 0; i < input.length; i++) input[i] = i % 3 === 0 ? 0.3 : 0;
        const out = downsample(input, 48_000, TARGET_RATE);
        assert.equal(out.length, 16_000);
        assert.ok(Math.abs(out[0] - 0.1) < 1e-6);
    });

    it('handles the 44.1 kHz ratio without running past the input', () => {
        const out = downsample(new Float32Array(44_100).fill(0.5), 44_100, TARGET_RATE);
        assert.equal(out.length, 16_000);
        assert.ok(out.every(v => Math.abs(v - 0.5) < 1e-6));
    });

    it('never upsamples', () => {
        const input = new Float32Array([0.1, 0.2]);
        assert.deepEqual(Array.from(downsample(input, 8_000, TARGET_RATE)), Array.from(input));
    });
});

describe('toUploadRate', () => {
    it('brings a 48 kHz recording down to 16 kHz', () => {
        const out = toUploadRate(new Float32Array(48_000), 48_000);
        assert.equal(out.rate, 16_000);
        assert.equal(out.samples.length, 16_000);
    });

    it('keeps an 8 kHz headset recording at 8 kHz, so it is not labelled double speed', () => {
        const out = toUploadRate(new Float32Array(80_000), 8_000); // 10 s
        assert.equal(out.rate, 8_000);
        assert.equal(out.samples.length / out.rate, 10);
    });
});

describe('concat', () => {
    it('joins chunks in order', () => {
        const out = concat([new Float32Array([1, 2]), new Float32Array([]), new Float32Array([3])]);
        assert.deepEqual(Array.from(out), [1, 2, 3]);
    });
});

describe('encodeWav', () => {
    it('writes a 16 kHz mono 16-bit PCM header the server can read', () => {
        const buf = encodeWav(new Float32Array([0, 1, -1, 2]), TARGET_RATE);
        const view = new DataView(buf);
        const ascii = (o: number, n: number) =>
            String.fromCharCode(...new Uint8Array(buf, o, n));
        assert.equal(buf.byteLength, 44 + 8);
        assert.equal(ascii(0, 4), 'RIFF');
        assert.equal(view.getUint32(4, true), 36 + 8);
        assert.equal(ascii(8, 4), 'WAVE');
        assert.equal(ascii(12, 4), 'fmt ');
        assert.equal(view.getUint16(20, true), 1); // PCM
        assert.equal(view.getUint16(22, true), 1); // mono
        assert.equal(view.getUint32(24, true), 16_000);
        assert.equal(view.getUint16(34, true), 16); // bits
        assert.equal(ascii(36, 4), 'data');
        assert.equal(view.getUint32(40, true), 8);
        // Full scale both ways, and out-of-range input is clipped, not wrapped.
        assert.deepEqual(
            [0, 1, 2, 3].map(i => view.getInt16(44 + i * 2, true)),
            [0, 32767, -32768, 32767],
        );
    });
});

describe('voiceKeyAction', () => {
    it('Enter stops and sends while listening; Escape throws the clip away', () => {
        assert.equal(voiceKeyAction('recording', { key: 'Enter' }), 'stop');
        assert.equal(voiceKeyAction('recording', { key: 'Escape' }), 'discard');
    });

    it('while the words are being written down, Enter is held back and Escape still discards', () => {
        assert.equal(voiceKeyAction('transcribing', { key: 'Enter' }), 'swallow');
        assert.equal(voiceKeyAction('transcribing', { key: 'Escape' }), 'discard');
    });

    it('leaves a modified Enter alone (Shift+Enter is a new line)', () => {
        for (const mod of ['shiftKey', 'altKey', 'ctrlKey', 'metaKey'] as const) {
            assert.equal(voiceKeyAction('recording', { key: 'Enter', [mod]: true }), null, mod);
            assert.equal(voiceKeyAction('transcribing', { key: 'Enter', [mod]: true }), null, mod);
        }
    });

    it('leaves IME composition alone, in both the Chrome and the Safari shape', () => {
        assert.equal(voiceKeyAction('recording', { key: 'Enter', isComposing: true }), null);
        assert.equal(voiceKeyAction('recording', { key: 'Enter', isComposing: false, keyCode: 229 }), null);
        assert.equal(voiceKeyAction('recording', { key: 'Escape', keyCode: 229 }), null);
    });

    it('leaves other keys alone', () => {
        assert.equal(voiceKeyAction('recording', { key: 'a' }), null);
        assert.equal(voiceKeyAction('recording', { key: ' ' }), null);
        assert.equal(voiceKeyAction('transcribing', { key: 'Tab' }), null);
    });
});

describe('joinDictation', () => {
    it('adds a space after a half-typed word, never doubles one', () => {
        assert.equal(joinDictation('Ask about', 'the Globex deal'), ' the Globex deal');
        assert.equal(joinDictation('Ask about ', 'the Globex deal'), 'the Globex deal');
        // A mention chip is followed by a non-breaking space.
        assert.equal(joinDictation('@Indigo Foster ', 'what did she say?'), 'what did she say?');
        assert.equal(joinDictation('', '  hello  '), 'hello');
        assert.equal(joinDictation('anything', '   '), '');
    });
});

describe('formatClock', () => {
    it('reads as m:ss', () => {
        assert.equal(formatClock(0), '0:00');
        assert.equal(formatClock(7.9), '0:07');
        assert.equal(formatClock(83), '1:23');
        assert.equal(formatClock(-3), '0:00');
    });
});
