"""Deep Prep council — plan → write → verify composition, and deep PDF render.

The three Gemini calls are faked via the module's `_call_json` seam, so no
model is contacted.
"""

import asyncio

import rubik_core.deep_prep.council as council
from rubik_core.deep_prep.models import DeepPrepResult, DeepPrepSection
from rubik_core.deep_prep.pdf import render_deep_prep_pdf

_WRITTEN = {
    "headline": "Preparing the Initech rollout SOW.",
    "sections": [
        {
            "heading": "Objectives",
            "body": "Align on scope, hours, pricing.",
            "sources": ["Transcript - Monday, July 7, 2025"],
        }
    ],
    "data_gaps": ["Prior-year fee not in the records"],
    "open_questions": ["When is the renewal decision?"],
}


def _fake_call_json_factory(record):
    async def fake(client, model_id, system, prompt):
        if "AGREE what the prep should contain" in system:  # PLAN
            record.append("plan")
            return {
                "sections": [
                    {"key": "objectives", "heading": "Objectives", "why": "core"}
                ],
                "rationale": "keep it tight",
            }
        if "writing a deep" in system:  # WRITE
            record.append("write")
            return dict(_WRITTEN)
        # VERIFY — strips an unsupported outcome claim.
        record.append("verify")
        out = dict(_WRITTEN)
        out["removed_claims"] = ["'previous unsuccessful bid' — unsupported"]
        out["verdict"] = "revised"
        return out

    return fake


def test_council_runs_all_three_stages_and_composes(monkeypatch):
    monkeypatch.setattr("rubik_core.models.get_genai_client", lambda: object())
    stages: list[str] = []
    monkeypatch.setattr(council, "_call_json", _fake_call_json_factory(stages))

    ctx = {
        "title": "Initech Rollout SOW — Working Session",
        "attendees": [],
        "past_meetings_report": "**Initech sync** — Monday, July 7, 2025\n...",
        "recent_emails": [],
    }
    result = asyncio.run(
        council.generate_deep_prep(
            ctx,
            user_email="pjensen@acme.example",
            is_sales_user=False,
            is_internal=True,
            department="Operations",
        )
    )
    assert stages == ["plan", "write", "verify"]  # full council ran, in order
    assert isinstance(result, DeepPrepResult)
    assert result.headline.startswith("Preparing the Initech")
    assert result.sections[0].heading == "Objectives"
    assert result.data_gaps == ["Prior-year fee not in the records"]
    assert result.verdict["verdict"] == "revised"
    assert result.verdict["removed_claims"]  # the fabricated claim was stripped
    assert result.all_sources == ["Transcript - Monday, July 7, 2025"]
    assert result.is_internal is True


def test_council_degrades_when_plan_fails(monkeypatch):
    monkeypatch.setattr("rubik_core.models.get_genai_client", lambda: object())

    async def fake(client, model_id, system, prompt):
        if "AGREE what the prep should contain" in system:
            raise RuntimeError("plan model 500")
        if "writing a deep" in system:
            return dict(_WRITTEN)
        return {**_WRITTEN, "verdict": "pass", "removed_claims": []}

    monkeypatch.setattr(council, "_call_json", fake)
    result = asyncio.run(
        council.generate_deep_prep(
            {"title": "X", "recent_emails": []},
            user_email="j@acme.example",
            is_sales_user=True,
            is_internal=False,
        )
    )
    # Plan failed → default plan used, but write/verify still produced a brief.
    assert result.sections and result.headline
    assert result.plan.get("rationale", "").startswith("default")


def test_deep_pdf_renders():
    result = DeepPrepResult(
        title="Initech Rollout SOW — Working Session",
        headline="Preparing the SOW — no prior-bid outcome is stated in the records.",
        sections=[
            DeepPrepSection(
                "Objectives", "Align on scope, hours, pricing.", ["Email — Parker"]
            ),
            DeepPrepSection("Relationship", "Active client per the records.", []),
        ],
        data_gaps=["Prior-year fee unknown"],
        open_questions=["Renewal timing?"],
        all_sources=["Email — Parker"],
        is_internal=True,
    )
    pdf = render_deep_prep_pdf(
        result, prepared_for="pjensen@acme.example", meeting_time="Wed 10 AM"
    )
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


def test_council_calls_ask_for_json_mode():
    """The prompts ask for raw JSON, but only JSON mode enforces it. Without it,
    gemini-4-argon left quoted names unescaped in 3 of 14 replayed drafts, so
    the brief failed to parse and the job fell back to the one-call brief."""
    from types import SimpleNamespace

    seen = {}

    async def generate_content(*, model, contents, config):
        seen["config"] = config
        return SimpleNamespace(text='```json\n{"sections": []}\n```')

    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    out = asyncio.run(council._call_json(client, "gemini-4-argon", "system", "prompt"))

    assert out == {"sections": []}
    assert seen["config"].response_mime_type == "application/json"
    assert seen["config"].temperature == 0.2


def test_council_splits_a_pinned_thinking_level_off_the_model_choice():
    """Admin → Sales Models offers Deep Prep "gemini-4-argon@high": the id goes
    to Vertex bare, the level into the config. A plain id sends no level, as
    before, so 2.5 Pro keeps its own default thinking."""
    from types import SimpleNamespace

    from google.genai.types import ThinkingLevel

    seen = []

    async def generate_content(*, model, contents, config):
        seen.append((model, config))
        return SimpleNamespace(text="{}")

    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    asyncio.run(council._call_json(client, "gemini-4-argon@high", "s", "p"))
    asyncio.run(council._call_json(client, "gemini-2.5-pro", "s", "p"))

    (argon, argon_cfg), (pro, pro_cfg) = seen
    assert argon == "gemini-4-argon"
    assert argon_cfg.thinking_config.thinking_level == ThinkingLevel.HIGH
    assert pro == "gemini-2.5-pro"
    assert pro_cfg.thinking_config is None


def test_council_runs_on_the_admins_deep_prep_choice(monkeypatch):
    """Production passes no model: the council reads the admin's Deep Prep
    choice at run time, so a change applies to the next brief, no deploy."""
    monkeypatch.setattr("rubik_core.models.get_genai_client", lambda: object())
    monkeypatch.setattr(
        "rubik_core.sales.model_config.model_for",
        lambda role: {"deep_prep": "gemini-4-argon@high"}[role],
    )
    used = []

    async def fake(client, model_id, system, prompt):
        used.append(model_id)
        return dict(_WRITTEN)

    monkeypatch.setattr(council, "_call_json", fake)
    asyncio.run(
        council.generate_deep_prep(
            {"title": "t"}, user_email="a@b.c", is_sales_user=False, is_internal=True
        )
    )
    assert used and set(used) == {"gemini-4-argon@high"}
