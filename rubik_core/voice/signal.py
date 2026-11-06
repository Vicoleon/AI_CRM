"""Read the browser's WAV clip and decide, from the samples, whether anyone spoke.

This exists because Gemini cannot be asked. Probed live on 2026-10-05, silence
and steady noise came back as plausible work questions ("What is the
requirement for verifying a new vendor?") in 14 of 15 tries, and the
model said there was speech when asked to decide first. The answer has to come from
the audio itself, before the model is called — which is also why the browser
sends uncompressed WAV: the server has no audio decoder, and 16-bit PCM needs
nothing but ``wave`` and numpy.

The decision mirrors ``frontend/src/components/voice/voiceActivity.ts`` exactly
(same frame size, same thresholds), so the browser and the server agree, and a
silent clip that slips past a buggy client still never reaches Vertex.
"""

from __future__ import annotations

import io
import struct
import wave
from dataclasses import dataclass

import numpy as np

# Keep in step with voiceActivity.ts.
FRAME_MS = 50
SPEECH_RMS_FLOOR = 0.015
SPEECH_OVER_ROOM = 3.0
MIN_SPEECH_MS = 250
ROOM_PERCENTILE = 0.1


class UnreadableAudio(ValueError):
    """Not a 16-bit PCM WAV file."""


@dataclass(frozen=True)
class Clip:
    samples: np.ndarray  # float32, mono, in [-1, 1]
    rate: int

    @property
    def seconds(self) -> float:
        return len(self.samples) / self.rate if self.rate else 0.0


def read_wav(data: bytes) -> Clip:
    """16-bit PCM WAV bytes → mono float samples. Raises :class:`UnreadableAudio`."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != 2 or w.getcomptype() != "NONE":
                raise UnreadableAudio("expected 16-bit PCM")
            rate = w.getframerate()
            channels = w.getnchannels()
            frames = w.readframes(w.getnframes())
    # Python 3.10's chunk reader raises a bare RuntimeError (and struct.error)
    # when a chunk's size runs past the file, not wave.Error; all of them are a
    # malformed upload, which is a 422, never a 500.
    except (wave.Error, EOFError, RuntimeError, struct.error) as exc:
        raise UnreadableAudio(str(exc)) from None
    if rate <= 0 or channels <= 0:
        raise UnreadableAudio("no sample rate or channels")
    # A data chunk cut off mid-sample leaves an odd byte that np.frombuffer
    # would refuse with a ValueError; drop the partial sample instead.
    frames = frames[: len(frames) - len(frames) % 2]
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        usable = len(pcm) - len(pcm) % channels
        pcm = pcm[:usable].reshape(-1, channels).mean(axis=1)
    return Clip(samples=pcm, rate=rate)


def to_wav(clip: Clip) -> bytes:
    """The parsed clip as a fresh mono 16-bit WAV.

    This is what goes to the model, never the uploaded bytes: a hand-made file
    whose header understates its length would otherwise show the checks above
    two seconds and hand Gemini everything after them. Re-encoding means the
    model hears exactly the samples that were measured.
    """
    pcm = (np.clip(clip.samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(clip.rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def frame_levels(clip: Clip) -> np.ndarray:
    """RMS loudness of each ``FRAME_MS`` frame (a trailing partial frame is dropped)."""
    size = max(1, int(clip.rate * FRAME_MS / 1000))
    count = len(clip.samples) // size
    if count == 0:
        return np.zeros(0, dtype=np.float32)
    frames = clip.samples[: count * size].reshape(count, size)
    return np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))


def voiced_ms(clip: Clip) -> float:
    """Milliseconds loud enough, and above the room enough, to be a voice."""
    levels = frame_levels(clip)
    if levels.size == 0:
        return 0.0
    room = float(np.sort(levels)[int((levels.size - 1) * ROOM_PERCENTILE)])
    threshold = max(SPEECH_RMS_FLOOR, room * SPEECH_OVER_ROOM)
    return float(np.count_nonzero(levels >= threshold) * FRAME_MS)


def heard_speech(clip: Clip) -> bool:
    return voiced_ms(clip) >= MIN_SPEECH_MS
