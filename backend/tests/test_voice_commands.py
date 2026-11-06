"""Voice commands: the gate, the reading, and the EOS cards.

What this file pins:

1. **WHETHER, then WHO.** ``VOICE_COMMANDS_ENABLED`` off answers 404 and hides
   the button through ``/api/users/me``; outside the ``voice_commands`` roster
   is a plain 403. Systems ships first.
2. **Silence never reaches the model**, exactly as for dictation.
3. **The model only picks.** A page is opened only if it was in the person's
   own list; the call is forced to choose a function, carries ``heard``, goes
   to the FLASH tier quietly, and lands in its own AI Cost row.
4. **A card is the page's own request.** Every EOS card is sent, as the person,
   to the real endpoint it names, and the change it describes is what lands —
   the Rock in Q4 2026 on the owner's team, the To-Do due a week out, the
   percent stored as a fraction on the week's Sunday. A record the person may
   not change is never offered; words that fit several records offer each;
   words that fit none say so.
5. **The shortcut is a saved preference** that a bad value cannot break.

Model calls are faked by STRING target (sibling suites reload modules). Nothing
here touches the network.
"""

from __future__ import annotations

import asyncio
import io
import wave
from datetime import date, datetime, timedelta
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from google.genai import types as genai_types

from backend.auth import get_current_user
from backend.main import app
from backend.services import voice_actions as va
from backend.tests._legacy_brand import LEGACY_PATTERN
from backend.tests._rosters import pin_rosters
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy, clear_cache
from rubik_core.voice import COMMANDS_COST_FEATURE
from rubik_core.voice import commands as cmd

URL = "/api/voice/commands"
KILL_SWITCH = "backend.routers.voice_commands.settings.VOICE_COMMANDS_ENABLED"
READ_COMMAND = "rubik_core.voice.commands.read_command"
READ_CONFIRMATION = "rubik_core.voice.commands.read_confirmation"
TODAY = "backend.routers.voice_commands.local_today"
GENAI_CLIENT = "rubik_core.voice.commands.get_genai_client"

# A Tuesday in Q4; its scorecard week starts Sunday 2026-10-04.
DAY = date(2026, 10, 6)

DESTINATIONS = [
    {"path": "/productivity", "label": "Productivity", "group": "My Work"},
    {"path": "/eos/rocks", "label": "Rocks", "group": "EOS"},
    {"path": "/sales/proposals", "label": "Proposals", "group": "My Work › Sales"},
]


def _wav(seconds: float, *, speech_at: tuple[float, float] | None = None) -> bytes:
    rate = 16_000
    pcm = np.zeros(int(seconds * rate))
    if speech_at:
        a, b = (int(t * rate) for t in speech_at)
        pcm[a:b] += 0.2 * np.sin(2 * np.pi * 220 * np.arange(b - a) / rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


SPOKEN = _wav(3.0, speech_at=(1.0, 2.0))
SILENT = _wav(3.0)


def _profile(
    email: str, *, department: str, roles=None, name: str | None = None, is_exec=False
):
    now = "2026-10-06T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=name or email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=[],
        created_at=now,
        updated_at=now,
    )


ME = _profile("jo.ellison@acme.example", department="Systems", name="Jo Ellison")


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_eos_off_refuses_voice_action_before_context(monkeypatch, lookup_fails):
    from rubik_core.control import repository

    if lookup_fails:
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("control down")),
        )
    else:
        monkeypatch.setattr(repository, "module_enabled", lambda *_a, **_k: False)
    monkeypatch.setattr(
        va,
        "_context",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("EOS table queried")),
    )
    proposal = va.propose(cmd.ADD_TODO, {"title": "Follow up"}, ME, None, today=DAY)
    assert proposal.plan is None
    assert proposal.options == []
    assert proposal.message


SALES = _profile("sally@acme.example", department="Sales", name="Sally Seller")


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database import eos  # noqa: F401 — register EOS tables
    from rubik_core.database.models import Base

    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'voice_commands.db'}"
    )
    dbmod._engine = None
    engine = dbmod.get_engine()
    assert engine.dialect.name == "sqlite", "tests must never touch Cloud SQL"
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    clear_cache()
    try:
        yield dbmod.SessionLocal
    finally:
        clear_cache()
        app.dependency_overrides.clear()
        engine.dispose()
        dbmod._engine = None


@pytest.fixture
def eos(fresh_db) -> dict:
    """Q4 2026, a Systems team (me, two Pauls) and a Sales seller, each in
    ``users`` and seated in EOS, a Rock / To-Do / two measurables of mine and a
    Sales Rock I may not touch."""
    from rubik_core.database.eos import (
        Organization,
        Person,
        Quarter,
        Rock,
        ScorecardMetric,
        Seat,
        Todo,
    )
    from rubik_core.database.models import Department, User

    db = fresh_db()
    org = Organization(name="Acme", created_by="seed")
    db.add_all([org, Department(name="Systems"), Department(name="Sales")])
    db.commit()
    q4 = Quarter(
        organization_id=org.id,
        name="Q4 2026",
        start_date=datetime(2026, 10, 1),
        end_date=datetime(2026, 12, 31),
        is_current=True,
        created_by="seed",
    )
    systems = Seat(department_name="Systems", title="Systems", created_by="seed")
    sales = Seat(department_name="Sales", title="Sales", created_by="seed")
    db.add_all([q4, systems, sales])
    db.commit()
    rows = [
        ("jo.ellison@acme.example", "Jo", "Ellison", "Systems", systems),
        ("parker.g@acme.example", "Parker", "Jensen", "Systems", systems),
        ("parker.k@acme.example", "Parker", "Kent", "Systems", systems),
        ("sally@acme.example", "Sally", "Seller", "Sales", sales),
    ]
    people = {}
    for email, first, last, dept, seat in rows:
        db.add(
            User(
                email=email,
                id=email.split("@")[0],
                name=f"{first} {last}",
                department=dept,
            )
        )
        person = Person(
            first_name=first,
            last_name=last,
            email=email,
            seat_id=seat.id,
            created_by="seed",
        )
        db.add(person)
        people[first if first != "Parker" else f"Parker {last}"] = person
    db.commit()
    me, sally = people["Jo"], people["Sally"]
    rock = Rock(
        quarter_id=q4.id,
        owner_id=me.id,
        description="Implement Cubie voice across Cubie",
        department_name="Systems",
        created_by=ME.email,
    )
    sales_rock = Rock(
        quarter_id=q4.id,
        owner_id=sally.id,
        description="Voice of the customer survey",
        department_name="Sales",
        created_by=SALES.email,
    )
    todo = Todo(
        owner_id=me.id,
        description="Send Parker the onboarding deck",
        department_name="Systems",
        created_by=ME.email,
    )
    calls = ScorecardMetric(
        owner_id=me.id,
        department_name="Systems",
        name="Calls Made",
        value_type="number",
    )
    utilization = ScorecardMetric(
        owner_id=me.id,
        department_name="Systems",
        name="Utilization Rate",
        value_type="percent",
    )
    db.add_all([rock, sales_rock, todo, calls, utilization])
    db.commit()
    ids = {
        "q4": q4.id,
        "me": me.id,
        "rock": rock.id,
        "sales_rock": sales_rock.id,
        "todo": todo.id,
        "calls": calls.id,
        "utilization": utilization.id,
        **{k: p.id for k, p in people.items()},
    }
    db.close()
    return ids


def _client(user: UserProfile) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _said(name: str, heard: str = "", **args):
    """Fake the model: it chose ``name`` with ``args``."""

    async def fake(context, **kwargs):
        fake.contexts.append(context)
        return cmd.Command(name=name, args=args, heard=heard or name)

    fake.contexts = []
    return fake


def _run(client: TestClient, *, text="do it", **form):
    data = {
        "text": text,
        "destinations": form.pop("destinations", None)
        or __import__("json").dumps(DESTINATIONS),
        "page": "/",
        **form,
    }
    with patch(TODAY, lambda tz: DAY):
        return client.post(URL, data=data)


def _send(client: TestClient, plan: dict):
    """What the browser does once the person says yes."""
    req = plan["request"]
    return client.request(req["method"], req["path"], json=req["body"])


def _fields(plan: dict) -> dict[str, str]:
    return {f["label"]: f["value"] for f in plan["fields"]}


# ── 1. Whether, then who ─────────────────────────────────────────────────────


@pytest.fixture
def systems_only(monkeypatch):
    """``voice_commands`` ships open to everyone. A case about someone OFF the roster
    installs the narrowed roster an admin would set, as Feature Access data."""
    pin_rosters(
        monkeypatch,
        {
            "voice_commands": FeaturePolicy(
                departments=frozenset({"systems"}),
                exec_allowed=False,
                admin_allowed=True,
            )
        },
    )


def test_switched_off_is_404_and_hides_the_button():
    client = _client(ME)
    with patch(KILL_SWITCH, False):
        assert client.post(URL, data={"text": "open rocks"}).status_code == 404
        assert client.get("/api/users/me").json()["features"]["voice_commands"] is False
    assert client.get("/api/users/me").json()["features"]["voice_commands"] is True


def test_outside_the_roster_is_a_plain_403(systems_only):
    res = _client(SALES).post(URL, data={"text": "open rocks"})
    assert res.status_code == 403
    assert "not turned on for you" in res.json()["detail"]


def test_feature_is_registered_gated_and_costed():
    from backend.routers.admin import _FEATURE_LABELS
    from rubik_core.features import feature_by_key

    spec = feature_by_key("voice_commands")
    assert spec.kill_switch == "VOICE_COMMANDS_ENABLED"
    assert spec.group == "Cubie copilot"
    assert not LEGACY_PATTERN.search(spec.description + spec.notes)
    assert spec.default().everyone and not spec.default().departments
    assert _FEATURE_LABELS[COMMANDS_COST_FEATURE] == "Voice commands"


def test_every_eos_function_has_a_planner():
    assert set(cmd.EOS_ACTIONS) == set(va.PLANNERS)
    declared = {f["name"] for f in cmd.build_functions([])}
    assert set(cmd.EOS_ACTIONS) <= declared


# ── 2. Silence ───────────────────────────────────────────────────────────────


def test_a_recording_nobody_spoke_in_never_reaches_the_model(eos):
    fake = _said(cmd.OPEN_PAGE, destination="/eos/rocks")
    with patch(READ_COMMAND, fake):
        res = _client(ME).post(
            URL,
            files={"audio": ("command.wav", SILENT, "audio/wav")},
            data={"destinations": "[]"},
        )
    assert res.status_code == 200
    assert res.json()["kind"] == "none"
    assert fake.contexts == []


def test_a_spoken_command_reaches_the_model_re_encoded(eos):
    seen = {}

    async def fake(context, **kwargs):
        seen.update(kwargs)
        return cmd.Command(
            name=cmd.OPEN_PAGE, args={"destination": "/eos/rocks"}, heard="open rocks"
        )

    with patch(READ_COMMAND, fake), patch(TODAY, lambda tz: DAY):
        res = _client(ME).post(
            URL,
            files={"audio": ("command.wav", SPOKEN, "audio/wav")},
            data={"destinations": __import__("json").dumps(DESTINATIONS)},
        )
    assert res.json()["kind"] == "navigate"
    assert seen["audio"][:4] == b"RIFF"
    assert seen["clip_seconds"] == pytest.approx(3.0, abs=0.05)


def test_an_over_long_recording_is_refused_in_words(eos):
    res = _client(ME).post(
        URL,
        files={"audio": ("command.wav", _wav(40.0, speech_at=(1, 2)), "audio/wav")},
    )
    assert res.status_code == 413
    assert "shorter command" in res.json()["detail"]


# ── 3. The model only picks ──────────────────────────────────────────────────


def test_opens_only_a_page_from_the_persons_own_list(eos):
    client = _client(ME)
    with patch(
        READ_COMMAND, _said(cmd.OPEN_PAGE, "open my work", destination="/productivity")
    ):
        res = _run(client).json()
    assert res["kind"] == "navigate"
    assert res["destination"] == DESTINATIONS[0]
    assert res["heard"] == "open my work"

    with patch(READ_COMMAND, _said(cmd.OPEN_PAGE, destination="/admin?tab=access")):
        res = _run(client).json()
    assert res["kind"] == "message"


def test_a_destination_list_with_an_outside_link_is_refused(eos):
    bad = __import__("json").dumps([{"path": "//evil.example/x", "label": "X"}])
    res = _run(_client(ME), destinations=bad)
    assert res.status_code == 422


def test_the_model_is_told_who_is_speaking_and_what_they_can_open(eos):
    fake = _said(cmd.NOT_UNDERSTOOD)
    with patch(READ_COMMAND, fake):
        res = _run(_client(ME)).json()
    assert res["kind"] == "message"
    (context,) = fake.contexts
    assert context.speaker == "Jo Ellison"
    assert context.today == DAY
    assert context.current_quarter == "Q4 2026"
    assert "Systems" in context.teams
    assert [d.path for d in context.destinations] == [d["path"] for d in DESTINATIONS]
    assert "Parker Jensen" in context.people


def test_a_question_goes_to_the_assistant(eos):
    with patch(
        READ_COMMAND,
        _said(
            cmd.ASK_CUBIE,
            "what did Parker promise Globex",
            question="What did Parker promise Globex?",
        ),
    ):
        res = _run(_client(ME)).json()
    assert res == {**res, "kind": "ask", "ask": "What did Parker promise Globex?"}


def test_a_confirmation_is_read_as_yes_no_or_unclear(eos):
    async def fake(question, **kwargs):
        fake.question = question
        return cmd.Command(name=cmd.ANSWER, args={"answer": "yes"}, heard="yeah do it")

    with patch(READ_CONFIRMATION, fake):
        res = (
            _client(ME)
            .post(
                URL,
                files={"audio": ("reply.wav", SPOKEN, "audio/wav")},
                data={"expect": "confirm", "question": "Add a Rock: Ship it"},
            )
            .json()
        )
    assert res["kind"] == "answer" and res["answer"] == "yes"
    assert fake.question == "Add a Rock: Ship it"


class _FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    async def generate_content(self, **kwargs):
        from rubik_core.cost.usage import current_feature, current_user_email

        self.calls.append(kwargs)
        self.feature = current_feature.get()
        self.user = current_user_email.get()
        return self.response


def _call_response(name: str, args: dict) -> genai_types.GenerateContentResponse:
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(
                    role="model",
                    parts=[
                        genai_types.Part(
                            function_call=genai_types.FunctionCall(name=name, args=args)
                        )
                    ],
                )
            )
        ]
    )


def test_one_quiet_forced_choice_flash_call_attributed_to_its_own_row():
    from rubik_core.config import settings

    models = _FakeModels(
        _call_response(
            cmd.ADD_ROCK, {"heard": "Add a Rock called Ship it", "title": "Ship it"}
        )
    )
    context = cmd.CommandContext(
        speaker="Jo Ellison",
        today=DAY,
        current_quarter="Q4 2026",
        destinations=[cmd.Destination("/eos/rocks", "Rocks", "EOS")],
        people=["Emery Brooks"],
    )
    client = type("C", (), {"aio": type("A", (), {"models": models})()})()
    with (
        patch(GENAI_CLIENT, lambda: client),
        patch.object(settings, "VOICE_COMMAND_MODEL", ""),
    ):
        out = asyncio.run(
            cmd.read_command(
                context, audio=SPOKEN, user_email=ME.email, clip_seconds=3.0
            )
        )
    assert (out.name, out.args, out.heard) == (
        cmd.ADD_ROCK,
        {"title": "Ship it"},
        "Add a Rock called Ship it",
    )
    (call,) = models.calls
    assert call["model"] == settings.VERTEX_LLM_MODEL_FLASH
    audio_part, instructions = call["contents"]
    assert audio_part.inline_data.mime_type == "audio/wav"
    assert "/eos/rocks — Rocks (EOS)" in instructions and "Emery Brooks" in instructions
    config = call["config"]
    assert config.temperature == 0.0
    assert config.tool_config.function_calling_config.mode.value == "ANY"
    assert str(config.thinking_config.thinking_level.value).upper() == "LOW"
    names = [d.name for d in config.tools[0].function_declarations]
    assert names[0] == cmd.OPEN_PAGE and cmd.ASK_CUBIE in names
    for declaration in config.tools[0].function_declarations:
        assert declaration.parameters_json_schema["required"][0] == "heard"
    assert models.feature == COMMANDS_COST_FEATURE
    assert models.user == ME.email


def test_words_a_clip_that_short_cannot_hold_are_thrown_away():
    response = _call_response(
        cmd.ASK_CUBIE,
        {"heard": " ".join(["word"] * 40), "question": "x"},
    )
    out = cmd.read_response(response, allowed={cmd.ASK_CUBIE}, clip_seconds=1.0)
    assert out.no_speech is True and out.heard == ""


def test_an_unoffered_function_or_no_call_is_a_failure():
    with pytest.raises(cmd.CommandFailed):
        cmd.read_response(
            _call_response("delete_everything", {}), allowed={cmd.ASK_CUBIE}
        )
    empty = genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(
                    role="model", parts=[genai_types.Part(text="hi")]
                )
            )
        ]
    )
    with pytest.raises(cmd.CommandFailed):
        cmd.read_response(empty, allowed={cmd.ASK_CUBIE})


def test_a_model_failure_is_a_502_without_internals(eos):
    async def boom(context, **kwargs):
        raise cmd.CommandFailed("503 UNAVAILABLE quota project xyz")

    with patch(READ_COMMAND, boom):
        res = _run(_client(ME))
    assert res.status_code == 502
    assert "xyz" not in res.text


# ── 4. A card is the page's own request ──────────────────────────────────────


def test_add_rock_for_q4_lands_in_q4_on_the_owners_team(eos, fresh_db):
    from rubik_core.database.eos import Quarter, Rock

    client = _client(ME)
    with patch(
        READ_COMMAND,
        _said(cmd.ADD_ROCK, title="implement Cubie voice across Cubie.", quarter=4),
    ):
        res = _run(client).json()
    assert res["kind"] == "action"
    plan = res["plan"]
    assert _fields(plan) == {
        "Rock": "Implement Cubie voice across Cubie",
        "Quarter": "Q4 2026",
        "Owner": "You (Jo Ellison)",
        "Team": "Systems",
    }
    assert _send(client, plan).status_code == 201
    db = fresh_db()
    rock = (
        db.query(Rock)
        .filter(
            Rock.description == "Implement Cubie voice across Cubie",
            Rock.id != eos["rock"],
        )
        .one()
    )
    assert rock.owner_id == eos["me"]
    assert rock.department_name == "Systems"
    assert db.get(Quarter, rock.quarter_id).name == "Q4 2026"
    db.close()


def test_a_quarter_already_over_this_year_means_next_year(eos, fresh_db):
    from rubik_core.database.eos import Quarter, Rock

    client = _client(ME)
    with patch(
        READ_COMMAND, _said(cmd.ADD_ROCK, title="Plan the 2027 kickoff", quarter=1)
    ):
        plan = _run(client).json()["plan"]
    assert _fields(plan)["Quarter"] == "Q1 2027"
    assert _send(client, plan).status_code == 201
    db = fresh_db()
    rock = db.query(Rock).filter(Rock.description == "Plan the 2027 kickoff").one()
    assert db.get(Quarter, rock.quarter_id).name == "Q1 2027"
    db.close()


def test_a_shared_first_name_offers_each_person(eos):
    with patch(
        READ_COMMAND, _said(cmd.ADD_ROCK, title="Close the audit", owner="Parker")
    ):
        res = _run(_client(ME)).json()
    assert res["kind"] == "action" and res["plan"] is None
    assert res["prompt"] == "Who is this Rock for?"
    assert sorted(o["option"] for o in res["options"]) == [
        "Parker Jensen",
        "Parker Kent",
    ]


def test_a_full_name_picks_the_one_person(eos):
    with patch(
        READ_COMMAND, _said(cmd.ADD_ROCK, title="Close the audit", owner="Parker Kent")
    ):
        plan = _run(_client(ME)).json()["plan"]
    assert plan["request"]["body"]["owner_id"] == str(eos["Parker Kent"])


def test_someone_outside_the_owner_list_is_refused(eos):
    with patch(
        READ_COMMAND, _said(cmd.ADD_ROCK, title="Close the audit", owner="Sally Seller")
    ):
        res = _run(_client(ME)).json()
    assert res["kind"] == "message"
    assert "Sally Seller" in res["message"]


def test_a_team_that_is_not_yours_is_refused(eos):
    with patch(
        READ_COMMAND, _said(cmd.ADD_ISSUE, title="Pricing is off", team="Sales")
    ):
        res = _run(_client(ME)).json()
    assert res["kind"] == "message"
    assert "Your teams: Systems" in res["message"]


def test_update_rock_status_offers_only_rocks_you_may_update(eos, fresh_db):
    from rubik_core.database.eos import Rock

    client = _client(ME)
    # "voice" is in my Rock AND in Sales' Rock; only mine may be offered.
    with patch(
        READ_COMMAND,
        _said(cmd.UPDATE_ROCK_STATUS, rock="the voice rock", status="done"),
    ):
        res = _run(client).json()
    plan = res["plan"]
    assert _fields(plan)["Rock"] == "Implement Cubie voice across Cubie"
    assert _fields(plan)["Status"] == "On track → Done"
    assert _send(client, plan).status_code == 200
    db = fresh_db()
    assert str(db.get(Rock, eos["rock"]).status.value) == "done"
    assert str(db.get(Rock, eos["sales_rock"]).status.value) == "on_track"
    db.close()


def test_words_that_fit_no_rock_say_so(eos):
    with patch(
        READ_COMMAND,
        _said(cmd.UPDATE_ROCK_STATUS, rock="the pricing overhaul", status="done"),
    ):
        res = _run(_client(ME)).json()
    assert res["kind"] == "message"
    assert "pricing overhaul" in res["message"]


def test_add_todo_is_due_a_week_out_on_my_team(eos, fresh_db):
    from rubik_core.database.eos import Todo

    client = _client(ME)
    with patch(
        READ_COMMAND, _said(cmd.ADD_TODO, title="send the deck", owner="Parker Jensen")
    ):
        plan = _run(client).json()["plan"]
    assert _fields(plan) == {
        "To-Do": "Send the deck",
        "Owner": "Parker Jensen",
        "Due": "Tue, Oct 13",
        "Team": "Systems",
    }
    assert _send(client, plan).status_code == 201
    db = fresh_db()
    todo = db.query(Todo).filter(Todo.description == "Send the deck").one()
    assert todo.owner_id == eos["Parker Jensen"]
    assert todo.due_date.date() == date(2026, 10, 13)
    assert todo.department_name == "Systems"
    db.close()


def test_a_said_due_date_is_kept(eos):
    with patch(
        READ_COMMAND, _said(cmd.ADD_TODO, title="Send the deck", due="2026-10-09")
    ):
        plan = _run(_client(ME)).json()["plan"]
    assert _fields(plan)["Due"] == "Fri, Oct 9"
    assert plan["request"]["body"]["due_date"] == "2026-10-09T00:00:00"


def test_complete_todo_marks_the_matching_one_done(eos, fresh_db):
    from rubik_core.database.eos import Todo

    client = _client(ME)
    with patch(READ_COMMAND, _said(cmd.COMPLETE_TODO, todo="the onboarding deck")):
        plan = _run(client).json()["plan"]
    assert _fields(plan)["To-Do"] == "Send Parker the onboarding deck"
    assert _send(client, plan).status_code == 200
    db = fresh_db()
    assert str(db.get(Todo, eos["todo"]).status.value) == "done"
    db.close()


def test_add_issue_files_it_under_my_team(eos, fresh_db):
    from rubik_core.database.eos import Issue

    client = _client(ME)
    with patch(
        READ_COMMAND,
        _said(cmd.ADD_ISSUE, title="Client portal keeps timing out", priority="high"),
    ):
        plan = _run(client).json()["plan"]
    assert _fields(plan)["Team"] == "Systems"
    assert _fields(plan)["Priority"] == "High"
    with patch("backend.routers.eos.issues.enqueue_issue_ingest", lambda *a, **k: None):
        assert _send(client, plan).status_code == 201
    db = fresh_db()
    issue = db.query(Issue).one()
    assert (issue.title, issue.department_name) == (
        "Client portal keeps timing out",
        "Systems",
    )
    db.close()


def test_a_percent_is_stored_as_a_fraction_on_the_weeks_sunday(eos, fresh_db):
    from rubik_core.database.eos import ScorecardEntry

    client = _client(ME)
    with patch(
        READ_COMMAND,
        _said(cmd.RECORD_SCORECARD_VALUE, measurable="utilization", value="92%"),
    ):
        plan = _run(client).json()["plan"]
    assert _fields(plan)["Value"] == "92%"
    assert _fields(plan)["Week"] == "This week (from Sun, Oct 4)"
    assert _send(client, plan).status_code == 200
    db = fresh_db()
    entry = db.query(ScorecardEntry).filter_by(metric_id=eos["utilization"]).one()
    assert entry.value == "0.92"
    assert entry.week_start_date == datetime(2026, 10, 4)
    db.close()


def test_a_number_already_there_is_shown_before_it_is_replaced(eos, fresh_db):
    from rubik_core.database.eos import ScorecardEntry

    db = fresh_db()
    db.add(
        ScorecardEntry(
            metric_id=eos["calls"],
            week_start_date=datetime(2026, 9, 27, 5),
            value="30",
            created_by=ME.email,
        )
    )
    db.commit()
    db.close()
    client = _client(ME)
    with patch(
        READ_COMMAND,
        _said(
            cmd.RECORD_SCORECARD_VALUE,
            measurable="calls made",
            value="1,250",
            week="last_week",
        ),
    ):
        plan = _run(client).json()["plan"]
    fields = _fields(plan)
    assert fields["Value"] == "1,250"
    assert fields["Replaces"] == "30"
    assert fields["Week"] == "Last week (from Sun, Sep 27)"
    assert _send(client, plan).status_code == 200
    db = fresh_db()
    (entry,) = db.query(ScorecardEntry).filter_by(metric_id=eos["calls"]).all()
    assert entry.value == "1250"
    db.close()


def test_a_value_that_is_not_a_number_is_refused(eos):
    with patch(
        READ_COMMAND,
        _said(cmd.RECORD_SCORECARD_VALUE, measurable="calls made", value="a lot"),
    ):
        res = _run(_client(ME)).json()
    assert res["kind"] == "message" and "a lot" in res["message"]


def test_someone_not_in_eos_cannot_make_a_record_theirs(eos):
    stranger = _profile("new.hire@acme.example", department="Systems")
    with patch(READ_COMMAND, _said(cmd.ADD_TODO, title="Read the handbook")):
        res = _run(_client(stranger)).json()
    assert res["kind"] == "message"
    assert "not set up in EOS" in res["message"]


def test_a_user_who_never_opened_eos_is_still_themselves(eos, fresh_db):
    """In `users` but with no eos_people row yet: reading the owner list (as the
    page does) creates it, so the command works first time."""
    from rubik_core.database.models import User

    db = fresh_db()
    db.add(
        User(
            email="fresh@acme.example",
            id="fresh",
            name="Fresh Face",
            department="Systems",
        )
    )
    db.commit()
    db.close()
    fresh = _profile("fresh@acme.example", department="Systems", name="Fresh Face")
    with patch(READ_COMMAND, _said(cmd.ADD_TODO, title="Read the handbook")):
        res = _run(_client(fresh)).json()
    assert res["kind"] == "action", res
    assert _fields(res["plan"])["Owner"] == "You (Fresh Face)"


def test_an_exec_files_where_the_page_would_the_exec_team(eos):
    boss = _profile(ME.email, department="Systems", name="Jo Ellison", is_exec=True)
    with patch(READ_COMMAND, _said(cmd.ADD_ISSUE, title="Plan the offsite")):
        plan = _run(_client(boss)).json()["plan"]
    assert plan["request"]["body"]["is_exec"] is True
    assert "department_name" not in plan["request"]["body"]
    assert _fields(plan)["Team"] == "Executive View"
    with patch(
        READ_COMMAND, _said(cmd.ADD_ISSUE, title="Plan the offsite", team="Systems")
    ):
        plan = _run(_client(boss)).json()["plan"]
    assert plan["request"]["body"]["department_name"] == "Systems"


def test_a_teammates_todo_you_may_read_but_not_change_is_not_offered(eos, fresh_db):
    from rubik_core.database.eos import Todo

    db = fresh_db()
    db.add(
        Todo(
            owner_id=eos["Parker Jensen"],
            description="Finish the audit checklist",
            department_name="Systems",
            created_by="parker.g@acme.example",
        )
    )
    db.commit()
    db.close()
    with patch(READ_COMMAND, _said(cmd.COMPLETE_TODO, todo="the audit checklist")):
        res = _run(_client(ME)).json()
    assert res["kind"] == "message"


def test_a_company_wide_rock_you_may_read_but_not_change_is_not_offered(eos, fresh_db):
    from rubik_core.database.eos import Rock

    db = fresh_db()
    db.add(
        Rock(
            quarter_id=eos["q4"],
            owner_id=eos["Sally"],
            description="Launch the referral program",
            visibility="company",
            created_by=SALES.email,
        )
    )
    db.commit()
    db.close()
    client = _client(ME)
    assert any(
        r["description"] == "Launch the referral program"
        for r in client.get("/api/eos/rocks/").json()
    ), "the Rock must be readable for this test to mean anything"
    with patch(
        READ_COMMAND,
        _said(cmd.UPDATE_ROCK_STATUS, rock="referral program", status="done"),
    ):
        res = _run(client).json()
    assert res["kind"] == "message"


def test_a_measurable_not_kept_weekly_is_left_to_the_scorecard_page(eos, fresh_db):
    from rubik_core.database.eos import ScorecardMetric

    db = fresh_db()
    db.add(
        ScorecardMetric(
            owner_id=eos["me"],
            department_name="Systems",
            name="Client Audits Closed",
            frequency="biweekly",
        )
    )
    db.commit()
    db.close()
    with patch(
        READ_COMMAND,
        _said(cmd.RECORD_SCORECARD_VALUE, measurable="client audits closed", value="3"),
    ):
        res = _run(_client(ME)).json()
    assert res["kind"] == "message"
    assert "Scorecard page" in res["message"]


@pytest.mark.parametrize("args", [{"owner": 5}, {"team": 7}, {"quarter": "soon"}])
def test_odd_argument_types_never_500(eos, args):
    with patch(READ_COMMAND, _said(cmd.ADD_ROCK, title="Ship it", **args)):
        res = _run(_client(ME))
    assert res.status_code == 200
    assert res.json()["kind"] in {"action", "message"}


def test_a_zero_quarter_means_none_was_said(eos):
    with patch(READ_COMMAND, _said(cmd.ADD_ROCK, title="Ship it", quarter=0, year=0)):
        plan = _run(_client(ME)).json()["plan"]
    assert _fields(plan)["Quarter"] == "Q4 2026"
    assert "quarter_number" not in plan["request"]["body"]


def test_a_seatless_owners_rock_goes_to_the_speakers_one_team(eos, fresh_db):
    from rubik_core.database.eos import Person, Rock
    from rubik_core.database.models import User

    db = fresh_db()
    db.add(
        User(
            email="seatless@acme.example",
            id="seatless",
            name="No Seat",
            department="Systems",
        )
    )
    db.add(
        Person(
            first_name="No",
            last_name="Seat",
            email="seatless@acme.example",
            created_by="seed",
        )
    )
    db.commit()
    db.close()
    seatless = _profile("seatless@acme.example", department="Systems", name="No Seat")
    client = _client(seatless)
    with patch(READ_COMMAND, _said(cmd.ADD_ROCK, title="Tidy the runbooks")):
        plan = _run(client).json()["plan"]
    assert _fields(plan)["Team"] == "Systems"
    assert _send(client, plan).status_code == 201
    db = fresh_db()
    assert (
        db.query(Rock).filter_by(description="Tidy the runbooks").one().department_name
        == "Systems"
    )
    db.close()


def test_a_long_todo_shows_what_will_be_saved(eos):
    long = "Send " + "the full quarterly compliance package " * 6
    with patch(READ_COMMAND, _said(cmd.ADD_TODO, title=long)):
        plan = _run(_client(ME)).json()["plan"]
    assert len(_fields(plan)["To-Do"]) <= 120
    assert plan["request"]["body"]["description"] == _fields(plan)["To-Do"]


# ── Matching, quarters, numbers ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("said", "title", "matches"),
    [
        ("the voice rock", "Implement Cubie voice across Cubie", True),
        ("onboarding deck", "Send Parker the onboarding deck", True),
        ("calls", "Calls Made", True),
        ("pricing overhaul", "Implement Cubie voice across Cubie", False),
        ("rock", "Implement Cubie voice across Cubie", False),
    ],
)
def test_similarity(said, title, matches):
    assert (va.similarity(said, title) >= va.MATCH_FLOOR) is matches


def test_an_initial_names_the_person():
    class P:
        def __init__(self, first, last):
            self.first_name, self.last_name = first, last

    jensen, kent = P("Parker", "Jensen"), P("Parker", "Kent")
    assert va.match_people("Parker J", [jensen, kent]) == [jensen]
    assert va.match_people("Parker", [jensen, kent]) == [jensen, kent]


def test_best_matches_prefers_mine_only_to_break_a_tie():
    items = [("Weekly report", "them"), ("Weekly report", "me"), ("Budget", "me")]
    picked = va.best_matches(
        "weekly report", items, lambda i: i[0], prefer=lambda i: i[1] == "me"
    )
    assert picked == [("Weekly report", "me")]


@pytest.mark.parametrize(
    ("number", "year", "expected"),
    [
        (4, None, (4, 2026)),
        (1, None, (1, 2027)),
        (1, 2026, (1, 2026)),
        (3, None, (3, 2027)),
    ],
)
def test_quarter_for(number, year, expected):
    assert va.quarter_for(number, year, DAY) == expected


def test_week_sunday():
    assert va.week_sunday(date(2026, 10, 4)) == date(2026, 10, 4)
    assert va.week_sunday(date(2026, 10, 10)) == date(2026, 10, 4)
    assert va.week_sunday(DAY - timedelta(days=2)) == date(2026, 10, 4)


@pytest.mark.parametrize(
    ("said", "kind", "stored", "shown"),
    [
        ("92%", "percent", "0.92", "92%"),
        ("9.25", "percent", "0.0925", "9.25%"),
        ("$12,500", "currency", "12500", "$12,500"),
        ("42", "number", "42", "42"),
        ("4.50", "number", "4.5", "4.5"),
    ],
)
def test_scorecard_values(said, kind, stored, shown):
    value = va.scorecard_storage_value(va.parse_number(said), kind)
    assert value == stored
    assert va.scorecard_display(value, kind) == shown


# ── 5. The shortcut preference ───────────────────────────────────────────────


def test_the_shortcut_round_trips_and_leaves_other_settings_alone(eos):
    client = _client(ME)
    assert (
        client.put(
            "/api/preferences", json={"timezone": "America/New_York"}
        ).status_code
        == 200
    )
    shortcut = {"mac": {"mods": ["ctrl", "alt"], "code": "Space"}, "pc": "off"}
    res = client.put("/api/preferences", json={"voice_shortcut": shortcut})
    assert res.status_code == 200
    body = client.get("/api/preferences").json()
    assert body["voice_shortcut"] == {
        "mac": {"mods": ["ctrl", "alt"], "code": "Space"},
        "pc": "off",
    }
    assert body["timezone"] == "America/New_York"
    assert (
        client.put("/api/preferences", json={"voice_shortcut": None}).status_code == 200
    )
    assert client.get("/api/preferences").json()["voice_shortcut"] is None


def test_the_shortcut_lives_in_its_own_table_not_user_preferences(eos, fresh_db):
    """Adding a column to user_preferences at boot can take the app down, so
    the shortcut must never be stored there."""
    from rubik_core.database.models import UserPreference, UserVoiceShortcut

    assert "voice_shortcut" not in UserPreference.__table__.columns
    client = _client(ME)
    shortcut = {"mac": {"mods": ["ctrl"], "code": "Space"}}
    assert (
        client.put("/api/preferences", json={"voice_shortcut": shortcut}).status_code
        == 200
    )
    db = fresh_db()
    assert db.get(UserVoiceShortcut, ME.email.lower()).shortcut == shortcut
    db.close()
    # Back to the default removes the row.
    assert (
        client.put("/api/preferences", json={"voice_shortcut": {}}).status_code == 200
    )
    db = fresh_db()
    assert db.get(UserVoiceShortcut, ME.email.lower()) is None
    db.close()


@pytest.mark.parametrize(
    "bad",
    [
        {"mac": {"mods": [], "code": "KeyK"}},
        {"mac": {"mods": ["hyper"], "code": None}},
        {"pc": {"mods": ["ctrl"], "code": "Key K"}},
    ],
)
def test_a_shortcut_without_a_modifier_or_with_junk_is_refused(eos, bad):
    assert (
        _client(ME).put("/api/preferences", json={"voice_shortcut": bad}).status_code
        == 422
    )


def test_an_unreadable_stored_shortcut_reads_as_the_default(eos, fresh_db):
    from rubik_core.database.models import UserVoiceShortcut

    db = fresh_db()
    db.add(
        UserVoiceShortcut(
            user_email=ME.email,
            shortcut={"mac": "sideways"},
            updated_at=datetime(2026, 10, 6),
        )
    )
    db.commit()
    db.close()
    res = _client(ME).get("/api/preferences")
    assert res.status_code == 200
    assert res.json()["voice_shortcut"] is None
