"""Voice input: the /api/voice/transcribe gate, its refusals, and the transcriber.

What this file pins:

1. **WHETHER, then WHO.** ``VOICE_INPUT_ENABLED`` off answers 404; a copilot
   user outside the ``voice_input`` roster gets a plain 403. Systems and admins
   are on the shipped roster; the Exec team is not (onboarded by name later).
2. **Refusals are plain sentences**: a format other than the browser's WAV is
   415, an oversized or over-long clip 413, an unreadable one 422, and a Vertex
   failure 502 without internals.
3. **Silence never reaches the model.** Gemini turns silence into plausible company
   questions (2026-10-05 probe), so the server measures the WAV itself and
   answers ``no_speech`` with no model call. Behind that, a transcript with more
   words than the clip can hold is thrown away, and its words are never logged.
4. **One quiet Flash call, attributed.** The call goes to the FLASH tier with
   thinking turned down (not the chat coordinator's "medium"), carries the
   company vocabulary and everyone's names, and lands in its own AI Cost row
   stamped with the speaker.

Outside calls are faked by STRING target (sibling suites ``importlib.reload()``
modules mid-run). Nothing here touches the network.
"""

from __future__ import annotations

import io
import json
import logging
import wave
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from google.genai import types as genai_types

from backend.auth import get_current_user
from backend.main import app
from backend.tests._legacy_brand import LEGACY_PATTERN
from backend.tests._rosters import pin_rosters
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy, clear_cache
from rubik_core.voice import COST_FEATURE, signal
from rubik_core.voice import transcribe as voice

URL = "/api/voice/transcribe"
KILL_SWITCH = "backend.routers.voice.settings.VOICE_INPUT_ENABLED"
MAX_BYTES = "backend.routers.voice.settings.VOICE_MAX_UPLOAD_BYTES"
TRANSCRIBE = "backend.routers.voice.transcribe_clip"
GENAI_CLIENT = "rubik_core.voice.transcribe.get_genai_client"


def _wav(
    seconds: float,
    *,
    speech_at: tuple[float, float] | None = None,
    noise: float = 0.0,
    rate: int = 16_000,
    channels: int = 1,
    width: int = 2,
) -> bytes:
    """A real WAV file: silence (plus optional steady noise) with an optional
    voice-loud tone between ``speech_at`` seconds."""
    n = int(seconds * rate)
    rng = np.random.default_rng(7)
    pcm = rng.normal(0.0, noise, n) if noise else np.zeros(n)
    if speech_at:
        a, b = (int(t * rate) for t in speech_at)
        t = np.arange(b - a) / rate
        pcm[a:b] += 0.2 * np.sin(2 * np.pi * 220 * t)
    ints = (np.clip(pcm, -1, 1) * 32767).astype("<i2")
    if channels > 1:
        ints = np.repeat(ints, channels)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(
            ints.tobytes() if width == 2 else (ints // 256 + 128).astype("u1").tobytes()
        )
    return buf.getvalue()


SPOKEN = _wav(3.0, speech_at=(1.0, 2.0))
WAV = ("recording.wav", SPOKEN, "audio/wav")


# ── users ────────────────────────────────────────────────────────────────────


def _profile(
    email: str,
    *,
    department: str = "general",
    roles: list[str] | None = None,
    is_exec: bool = False,
) -> UserProfile:
    now = "2026-10-05T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=[],
        created_at=now,
        updated_at=now,
    )


SYSTEMS = _profile("sys.user@acme.example", department="Systems")
MARKETING = _profile("mk@acme.example", department="Marketing")
SALES_EXEC = _profile("vp@acme.example", department="Sales", is_exec=True)
ADMIN = _profile("admin@acme.example", department="Sales", roles=["admin", "user"])


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """The app's ``SessionLocal`` on a throwaway sqlite file, two named people
    in ``users``, and the feature-policy cache empty before and after."""
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database import eos  # noqa: F401 — register EOS tables
    from rubik_core.database.models import Base, User

    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'voice_input.db'}"
    )
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = dbmod.SessionLocal()
    db.add_all(
        [
            User(email="cd@acme.example", id="cd", name="Indigo Foster"),
            User(email="ms@acme.example", id="ms", name="Emery Brooks"),
        ]
    )
    db.commit()
    db.close()
    clear_cache()
    try:
        yield dbmod.SessionLocal
    finally:
        clear_cache()
        app.dependency_overrides.clear()
        engine.dispose()
        dbmod._engine = None


@pytest.fixture
def as_user():
    def _apply(user: UserProfile) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app)

    yield _apply
    app.dependency_overrides.clear()


def _post(client: TestClient, file=WAV, surface: str | None = "chat"):
    data = {"surface": surface} if surface is not None else {}
    return client.post(URL, files={"audio": file}, data=data)


def _ok(text="What did Indigo say about the Q3 budget?", seconds=4.0):
    async def fake(audio, mime_type, **kwargs):
        fake.calls.append({"audio": audio, "mime_type": mime_type, **kwargs})
        return voice.Transcript(
            text=text, no_speech=not text, audio_seconds=seconds, model="m"
        )

    fake.calls = []
    return fake


# ── 1. Gates ─────────────────────────────────────────────────────────────────


@pytest.fixture
def systems_only(monkeypatch):
    """``voice_input`` ships open to everyone. A case about someone OFF the roster
    installs the narrowed roster an admin would set, as Feature Access data."""
    pin_rosters(
        monkeypatch,
        {
            "voice_input": FeaturePolicy(
                departments=frozenset({"systems"}),
                exec_allowed=False,
                admin_allowed=True,
            )
        },
    )


def test_kill_switch_off_answers_404(as_user):
    with patch(KILL_SWITCH, False):
        res = _post(as_user(SYSTEMS))
    assert res.status_code == 404


def test_copilot_user_outside_the_roster_gets_a_plain_403(as_user, systems_only):
    # Marketing has the copilot (the outer door) but not the narrowed roster.
    res = _post(as_user(MARKETING))
    assert res.status_code == 403
    assert "Voice input" in res.json()["detail"]


def test_exec_flag_does_not_open_a_narrowed_roster(as_user, systems_only):
    assert _post(as_user(SALES_EXEC)).status_code == 403


@pytest.mark.parametrize(
    "user", [SYSTEMS, MARKETING, ADMIN], ids=["systems", "marketing", "admin"]
)
def test_the_shipped_default_is_open_to_everyone(as_user, user):
    fake = _ok()
    with patch(TRANSCRIBE, fake):
        res = _post(as_user(user))
    assert res.status_code == 200, res.text
    # `seconds` is the WAV's own length, not the model's token-based guess.
    assert res.json() == {
        "text": "What did Indigo say about the Q3 budget?",
        "no_speech": False,
        "seconds": 3.0,
    }


# ── 2. The endpoint ──────────────────────────────────────────────────────────


def test_clip_is_normalized_and_sent_with_everyones_names(as_user):
    fake = _ok()
    with patch(TRANSCRIBE, fake):
        _post(as_user(SYSTEMS))
    (call,) = fake.calls
    # Gemini gets the measured samples re-encoded, not the upload's bytes.
    sent = signal.read_wav(call["audio"])
    assert sent.rate == 16_000 and sent.seconds == pytest.approx(3.0)
    assert signal.heard_speech(sent)
    assert call["mime_type"] == "audio/wav"
    assert set(call["people"]) == {"Indigo Foster", "Emery Brooks"}
    assert call["surface"] == "chat"
    assert call["user_email"] == SYSTEMS.email
    assert call["clip_seconds"] == pytest.approx(3.0)


def test_unknown_surface_is_treated_as_chat(as_user):
    fake = _ok()
    with patch(TRANSCRIBE, fake):
        _post(as_user(SYSTEMS), surface="<script>")
    assert fake.calls[0]["surface"] == "chat"


def test_no_speech_comes_back_as_no_speech_not_an_error(as_user):
    with patch(TRANSCRIBE, _ok(text="")):
        res = _post(as_user(SYSTEMS))
    assert res.status_code == 200
    assert res.json()["no_speech"] is True
    assert res.json()["text"] == ""


@pytest.mark.parametrize(
    "file",
    [
        ("notes.txt", b"hello", "text/plain"),
        # The browser's own compressed formats: the server cannot measure them.
        ("recording.webm", b"\x1aE\xdf\xa3opus", "audio/webm;codecs=opus"),
    ],
    ids=["text", "webm"],
)
def test_anything_but_wav_is_415(as_user, file):
    fake = _ok()
    with patch(TRANSCRIBE, fake):
        res = _post(as_user(SYSTEMS), file=file)
    assert res.status_code == 415
    assert fake.calls == []


@pytest.mark.parametrize("label", ["silence", "steady room noise"])
def test_a_clip_nobody_spoke_in_never_reaches_the_model(as_user, label):
    clip = _wav(4.0) if label == "silence" else _wav(4.0, noise=0.03)
    fake = _ok(text="What is the policy for verifying employment?")
    with patch(TRANSCRIBE, fake):
        res = _post(as_user(SYSTEMS), file=("recording.wav", clip, "audio/wav"))
    assert res.status_code == 200
    assert res.json() == {"text": "", "no_speech": True, "seconds": 4.0}
    assert fake.calls == []


def test_oversized_upload_is_413_and_never_reaches_the_model(as_user):
    fake = _ok()
    with patch(MAX_BYTES, 1000), patch(TRANSCRIBE, fake):
        res = _post(as_user(SYSTEMS))
    assert res.status_code == 413
    assert "too long" in res.json()["detail"]
    assert fake.calls == []


def test_a_clip_past_the_time_limit_is_413_even_under_the_byte_cap(as_user):
    fake = _ok()
    # 8 kHz keeps 125 s under the byte cap, so only the duration check can stop it.
    long_clip = _wav(125.0, speech_at=(1.0, 3.0), rate=8_000)
    with patch(TRANSCRIBE, fake):
        res = _post(as_user(SYSTEMS), file=("recording.wav", long_clip, "audio/wav"))
    assert res.status_code == 413
    assert fake.calls == []


@pytest.mark.parametrize(
    "payload", [b"", b"RIFF-not-really-a-wav"], ids=["empty", "garbage"]
)
def test_an_unreadable_clip_is_422(as_user, payload):
    with patch(TRANSCRIBE, _ok()):
        res = _post(as_user(SYSTEMS), file=("recording.wav", payload, "audio/wav"))
    assert res.status_code == 422


def test_vertex_failure_is_a_plain_502(as_user):
    async def boom(*_a, **_k):
        raise voice.TranscriptionFailed("429 RESOURCE_EXHAUSTED project data-warehouse")

    with patch(TRANSCRIBE, boom):
        res = _post(as_user(SYSTEMS))
    assert res.status_code == 502
    assert "RESOURCE_EXHAUSTED" not in res.text
    assert "try again" in res.json()["detail"]


# ── 3. Formats and the invented-speech backstop ──────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("audio/wav", "audio/wav"),
        ("AUDIO/X-WAV", "audio/wav"),
        ("audio/wave; charset=binary", "audio/wav"),
        ("audio/webm;codecs=opus", None),
        ("audio/mp4", None),
        ("text/plain", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_mime_type(raw, expected):
    assert voice.normalize_mime_type(raw) == expected


def test_signal_hears_a_short_voice_and_not_silence_or_noise():
    assert signal.heard_speech(signal.read_wav(_wav(3.0, speech_at=(1.0, 1.4))))
    assert not signal.heard_speech(signal.read_wav(_wav(3.0)))
    assert not signal.heard_speech(signal.read_wav(_wav(3.0, noise=0.05)))
    # A 100 ms click is not speech.
    assert not signal.heard_speech(signal.read_wav(_wav(3.0, speech_at=(1.0, 1.1))))
    # A voice over steady noise still is.
    assert signal.heard_speech(
        signal.read_wav(_wav(3.0, speech_at=(1.0, 2.0), noise=0.01))
    )


def test_signal_reads_rate_and_length_and_mixes_stereo_down():
    clip = signal.read_wav(_wav(2.0, speech_at=(0.5, 1.5), rate=8_000, channels=2))
    assert clip.rate == 8_000
    assert clip.seconds == pytest.approx(2.0)
    assert clip.samples.ndim == 1
    assert signal.heard_speech(clip)


def _overrunning_chunk_wav() -> bytes:
    """A WAV with a LIST chunk that claims more bytes than the file has.
    Python 3.10's chunk reader raises a bare RuntimeError on it, not wave.Error."""
    import struct

    good = _wav(1.0, speech_at=(0.2, 0.8))
    bad = good[:36] + b"LIST" + struct.pack("<I", 10_000_000) + b"abcd" + good[36:]
    return bad[:4] + struct.pack("<I", len(bad) - 8) + bad[8:]


def test_signal_turns_every_malformed_wav_into_unreadable_audio():
    with pytest.raises(signal.UnreadableAudio):
        signal.read_wav(_overrunning_chunk_wav())


def test_signal_drops_a_half_sample_left_by_a_cut_off_upload():
    clip = signal.read_wav(SPOKEN[:-1])
    assert clip.seconds == pytest.approx(3.0, abs=1e-3)
    assert signal.heard_speech(clip)


def test_a_malformed_wav_is_a_422_not_a_500(as_user):
    with patch(TRANSCRIBE, _ok()):
        res = _post(
            as_user(SYSTEMS),
            file=("recording.wav", _overrunning_chunk_wav(), "audio/wav"),
        )
    assert res.status_code == 422


def test_a_header_that_understates_the_length_cannot_smuggle_audio_to_the_model(
    as_user,
):
    import struct

    # 60 s of audio, but a RIFF size that covers only the first 2 s (which
    # hold speech). The checks see 2 s; the model must get those 2 s, not 60.
    full = _wav(60.0, speech_at=(0.5, 1.5), rate=8_000)
    riff = 36 + 2 * 8_000 * 2  # "WAVE" + fmt chunk + data header, then 2 s of data
    lying = full[:4] + struct.pack("<I", riff) + full[8:]
    fake = _ok()
    with patch(TRANSCRIBE, fake):
        res = _post(as_user(SYSTEMS), file=("recording.wav", lying, "audio/wav"))
    assert res.status_code == 200
    (call,) = fake.calls
    assert len(lying) > 900_000
    assert len(call["audio"]) < 40_000
    assert signal.read_wav(call["audio"]).seconds == pytest.approx(2.0)


def test_to_wav_round_trips_rate_length_and_speech():
    for rate in (8_000, 16_000):
        clip = signal.read_wav(_wav(2.0, speech_at=(0.5, 1.5), rate=rate, channels=2))
        again = signal.read_wav(signal.to_wav(clip))
        assert again.rate == rate and again.seconds == pytest.approx(2.0)
        assert np.allclose(again.samples, clip.samples, atol=2 / 32768)
        assert signal.heard_speech(again)


def test_the_kill_switch_hides_the_mic_in_users_me(as_user):
    client = as_user(SYSTEMS)
    assert client.get("/api/users/me").json()["features"]["voice_input"] is True
    with patch(KILL_SWITCH, False):
        assert client.get("/api/users/me").json()["features"]["voice_input"] is False


def test_signal_refuses_anything_but_16_bit_pcm():
    with pytest.raises(signal.UnreadableAudio):
        signal.read_wav(_wav(1.0, width=1))
    with pytest.raises(signal.UnreadableAudio):
        signal.read_wav(b"not a wav")


def _response(
    payload, *, audio_tokens: int | None = 96
) -> genai_types.GenerateContentResponse:
    """A real google-genai response object, as Vertex returns it."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    details = (
        [
            genai_types.ModalityTokenCount(
                modality=genai_types.MediaModality.AUDIO, token_count=audio_tokens
            ),
            genai_types.ModalityTokenCount(
                modality=genai_types.MediaModality.TEXT, token_count=400
            ),
        ]
        if audio_tokens is not None
        else None
    )
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(
                    role="model", parts=[genai_types.Part(text=text)]
                )
            )
        ],
        usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=(audio_tokens or 0) + 400,
            candidates_token_count=20,
            prompt_tokens_details=details,
        ),
    )


def test_audio_seconds_come_from_vertexs_audio_token_count():
    assert voice.audio_seconds(_response({"text": "hi"}, audio_tokens=96)) == 3.0
    assert voice.audio_seconds(_response({"text": "hi"}, audio_tokens=None)) is None


def test_a_real_transcript_is_kept_and_whitespace_tidied():
    out = voice.read_response(
        _response({"no_speech": False, "text": "  Pull up the\nGlobex deal.  "}), "m"
    )
    assert out == voice.Transcript(
        text="Pull up the Globex deal.", no_speech=False, audio_seconds=3.0, model="m"
    )


def test_the_model_saying_no_speech_wins_over_any_text():
    out = voice.read_response(_response({"no_speech": True, "text": "uh"}), "m")
    assert out.no_speech is True and out.text == ""


def test_an_invented_sentence_over_silence_is_discarded_unlogged(caplog):
    # The real 2026-10-05 probe: 3 s of digital silence came back as 35 words.
    invented = (
        "Hey so what's up with your whole deal lately? I feel like you've been a "
        "bit distant and, you know, we haven't hung out in a while. I was just "
        "wondering if everything's good with you."
    )
    with caplog.at_level(logging.WARNING, logger="rubik_core.voice.transcribe"):
        out = voice.read_response(
            _response({"no_speech": False, "text": invented}, audio_tokens=96), "m"
        )
    assert out.no_speech is True and out.text == ""
    assert "invented transcript" in caplog.text
    assert "distant" not in caplog.text  # counts only, never the words


def test_the_clips_true_length_beats_the_token_count():
    # 96 audio tokens read as 3 s; the WAV says 20 s, so 40 words is plausible.
    forty = " ".join(["word"] * 40)
    kept = voice.read_response(_response({"text": forty}), "m", clip_seconds=20.0)
    assert kept.no_speech is False and kept.audio_seconds == 20.0
    dropped = voice.read_response(_response({"text": forty}), "m")
    assert dropped.no_speech is True


def test_the_backstop_leaves_fast_but_real_speech_alone():
    # 3 s holds up to 5 * 3 + 4 = 19 words.
    nineteen = " ".join(["word"] * 19)
    assert voice._implausible(nineteen, 3.0) is False
    assert voice._implausible(nineteen + " more", 3.0) is True
    # With no audio count there is nothing to judge by.
    assert voice._implausible(nineteen * 10, None) is False


@pytest.mark.parametrize("raw", ["not json at all", ""])
def test_an_unreadable_answer_is_a_failure_not_a_transcript(raw):
    with pytest.raises(voice.TranscriptionFailed):
        voice.read_response(_response(raw), "m")


# ── 4. The call itself ───────────────────────────────────────────────────────


def test_instructions_add_the_company_profile_to_the_vocabulary(monkeypatch):
    from rubik_core.company.profile import CompanyProfile

    monkeypatch.setattr(
        "rubik_core.company.profile.get_company_profile",
        lambda *a, **k: CompanyProfile(name="Acme Corp", products=("SparkleMax",)),
    )
    text = voice.build_instructions(people=[], surface="chat")
    assert "works at Acme Corp" in text
    assert "SparkleMax" in text


def test_instructions_carry_vocabulary_and_deduplicated_names():
    text = voice.build_instructions(
        people=["Emery Brooks", "Indigo  Foster", "Emery Brooks", "", None],
        surface="chat",
    )
    assert "HubSpot" in text and "L10" in text
    assert "mortgage" not in text.lower()
    assert "Emery Brooks, Indigo Foster" in text  # sorted, deduplicated
    assert "Ask Cubie chat box" in text
    assert "Never translate" in text
    assert not LEGACY_PATTERN.search(text)
    assert "(none listed)" in voice.build_instructions(people=[], surface="unknown")


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gemini-3.8-flash", {"thinking_level": "LOW"}),
        ("gemini-3.5-flash-lite", {"thinking_level": "LOW"}),
        ("gemini-2.5-flash", {"thinking_budget": 0}),
        ("gemini-2.5-pro", None),
    ],
)
def test_thinking_is_turned_down_for_every_family(model, expected):
    config = voice._quiet_thinking(model)
    if expected is None:
        assert config is None
        return
    dumped = config.model_dump(exclude_none=True)
    for key, value in expected.items():
        assert (
            str(getattr(dumped[key], "value", dumped[key])).upper()
            == str(value).upper()
        )


class _FakeAioModels:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []
        self.seen_feature = None
        self.seen_user = None

    async def generate_content(self, **kwargs):
        from rubik_core.cost.usage import current_feature, current_user_email

        self.calls.append(kwargs)
        self.seen_feature = current_feature.get()
        self.seen_user = current_user_email.get()
        if self.error:
            raise self.error
        return self.response


class _FakeClient:
    def __init__(self, models: _FakeAioModels):
        self.aio = type("Aio", (), {"models": models})()


def test_transcribe_clip_makes_one_quiet_attributed_flash_call():
    import asyncio

    from rubik_core.config import settings

    models = _FakeAioModels(
        response=_response({"no_speech": False, "text": "Hello Cubie."})
    )
    with (
        patch(GENAI_CLIENT, lambda: _FakeClient(models)),
        patch.object(settings, "VOICE_TRANSCRIBE_MODEL", ""),
    ):
        out = asyncio.run(
            voice.transcribe_clip(
                SPOKEN,
                "audio/wav",
                people=["Emery Brooks"],
                user_email="sys.user@acme.example",
                clip_seconds=3.0,
            )
        )

    assert out.text == "Hello Cubie."
    (call,) = models.calls
    assert call["model"] == settings.VERTEX_LLM_MODEL_FLASH
    audio_part, instructions = call["contents"]
    assert audio_part.inline_data.mime_type == "audio/wav"
    assert audio_part.inline_data.data == SPOKEN
    assert "Emery Brooks" in instructions
    config = call["config"]
    assert config.temperature == 0.0
    assert config.response_mime_type == "application/json"
    # Not the chat coordinator's "medium": generation_config_for must not win.
    assert str(config.thinking_config.thinking_level.value).upper() == "LOW"
    assert models.seen_feature == COST_FEATURE
    assert models.seen_user == "sys.user@acme.example"


def test_transcribe_clip_wraps_an_upstream_error():
    import asyncio

    models = _FakeAioModels(error=RuntimeError("503 UNAVAILABLE"))
    with patch(GENAI_CLIENT, lambda: _FakeClient(models)):
        with pytest.raises(voice.TranscriptionFailed):
            asyncio.run(voice.transcribe_clip(SPOKEN, "audio/wav"))


# ── 5. Registration ──────────────────────────────────────────────────────────


def test_feature_is_registered_gated_and_costed():
    from backend.routers.admin import _FEATURE_LABELS
    from rubik_core.features import feature_by_key

    spec = feature_by_key("voice_input")
    assert spec.kill_switch == "VOICE_INPUT_ENABLED"
    assert spec.group == "Cubie copilot"
    assert not LEGACY_PATTERN.search(spec.description + spec.notes)
    assert spec.default().everyone and not spec.default().departments
    assert _FEATURE_LABELS[COST_FEATURE] == "Voice input (dictation)"
