"""UC-47 wired into meeting prep: research the room, don't let the model rewrite it.

Four things this asserts about the seam between the research step and the brief:

* An all-internal meeting researches nobody (no call, no cost, no colleague
  profiling) and its prompt stays byte-identical to before UC-47.
* An external meeting researches only the external people, whatever the reader's
  department — a Systems lead meeting a vendor's architect needs this as much as a
  rep does.
* The attendee cards on the response come from the research step VERBATIM. They
  carry citations, and letting the briefing model re-emit them is how a citation
  gets detached from the claim it supports.
* Research failure is a partial error, never a failed briefing.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.routers.home import (
    collect_meeting_prep_context,
    generate_meeting_prep_analysis,
)
from rubik_core.people.schemas import AttendeeIntel

USER = "bellison@acme.example"
COLLEAGUE = "ifoster@acme.example"
EXTERNAL = "riley.fontaine@northwind.example"


def _event(*attendees: str) -> dict:
    return {
        "id": "evt-1",
        "summary": "Acme / Northwind — GCP review",
        "start": {"dateTime": "2026-08-20T15:00:00Z"},
        "end": {"dateTime": "2026-08-20T16:00:00Z"},
        "attendees": [{"email": e} for e in attendees],
        "description": "",
        "attachments": [],
    }


def _intel_dict(email: str = EXTERNAL) -> dict:
    return AttendeeIntel.from_dict(
        {
            "email": email,
            "name": "Riley Fontaine",
            "profile": {
                "name": "Riley Fontaine",
                "linkedin_url": "https://linkedin.example/in/riley-fontaine",
                "current_title": "Account Manager",
                "current_company": "Northwind",
                "headline": "Account Manager, Cloud Infrastructure",
            },
            "activity": [
                {
                    "kind": "post",
                    "posted_at": "2026-07-02",
                    "summary": "Argued most GCP overspend is idle non-prod.",
                    "theme": "cloud cost",
                    "url": "https://www.linkedin.com/posts/rs-1",
                }
            ],
            "persona": {
                "communication_style": "Numbers-first.",
                "posts_about": ["cloud cost"],
                "confidence": "high",
            },
            "sources": ["https://linkedin.example/in/riley-fontaine"],
            "provider": "web_search",
        }
    ).as_dict()


def _collect(event: dict, *, research=None, department: str = "Systems"):
    """Run collect_meeting_prep_context with every external fetch stubbed."""
    tools = MagicMock()

    async def _no_meetings(**kwargs):
        return {"status": "error"}

    tools.search_past_meetings = _no_meetings

    async def _default_research(attendees, *, user_email, **kwargs):
        return SimpleNamespace(
            intel=[], skipped_attendees=[], errors=[], as_dicts=lambda: []
        )

    research_fn = research or _default_research
    spy = MagicMock(side_effect=research_fn)

    async def _research(attendees, **kwargs):
        return await spy(attendees, **kwargs)

    with (
        patch("rubik_core.adk_agent.tools.AgentTools", return_value=tools),
        patch("backend.routers.home._resolve_attendee_names", lambda a: None),
        patch("backend.routers.home._fetch_emails_with_people", return_value=[]),
        patch("backend.routers.home._fetch_emails_for_domains", return_value=[]),
        patch(
            "rubik_core.retrieval.product_catalog.get_product_catalog",
            return_value=MagicMock(search_products=MagicMock(return_value=[])),
        ),
        patch("rubik_core.tools.drive.GoogleDriveClient", side_effect=RuntimeError),
        patch("rubik_core.people.research_attendees", _research),
    ):
        ctx = asyncio.run(
            collect_meeting_prep_context(
                user_email=USER,
                user_department=department,
                user_roles=[],
                event=event,
            )
        )
    return ctx, spy


# ── who the context researches ───────────────────────────────────────────────


def test_internal_meeting_researches_nobody():
    """No external attendees → no research call at all. We don't profile
    colleagues, and there is nothing to look up."""
    ctx, spy = _collect(_event(USER, COLLEAGUE))

    spy.assert_not_called()
    assert ctx["attendee_intel"] == []
    assert ctx["is_internal"] is True


def test_external_meeting_researches_only_the_external_people():
    ctx, spy = _collect(_event(USER, COLLEAGUE, EXTERNAL))

    spy.assert_called_once()
    researched = [a["email"] for a in spy.call_args[0][0]]
    assert researched == [EXTERNAL]
    assert spy.call_args.kwargs["user_email"] == USER


def test_research_runs_for_non_sales_readers_too():
    """A Systems lead meeting a vendor's architect needs the same read."""
    _, spy = _collect(_event(USER, EXTERNAL), department="Systems")
    spy.assert_called_once()

    _, sales_spy = _collect(_event(USER, EXTERNAL), department="Sales")
    sales_spy.assert_called_once()


def test_research_results_land_on_the_context():
    async def _found(attendees, *, user_email, **kwargs):
        return SimpleNamespace(
            intel=[], skipped_attendees=[], errors=[], as_dicts=lambda: [_intel_dict()]
        )

    ctx, _ = _collect(_event(USER, EXTERNAL), research=_found)

    assert [a["email"] for a in ctx["attendee_intel"]] == [EXTERNAL]


def test_research_errors_become_partial_errors_not_a_failure():
    async def _boom(attendees, *, user_email, **kwargs):
        raise RuntimeError("grounding 503")

    ctx, _ = _collect(_event(USER, EXTERNAL), research=_boom)

    assert ctx["attendee_intel"] == []
    assert any("grounding 503" in e for e in ctx["errors"])


def test_capped_research_reports_the_skip_to_the_reader():
    async def _capped(attendees, *, user_email, **kwargs):
        return SimpleNamespace(
            intel=[],
            skipped_attendees=["extra@northwind.example"],
            errors=[
                "Attendee research capped at 6 people; skipped extra@northwind.example"
            ],
            as_dicts=lambda: [],
        )

    ctx, _ = _collect(_event(USER, EXTERNAL), research=_capped)

    assert any("capped at 6" in e for e in ctx["errors"])


# ── how the brief uses it ────────────────────────────────────────────────────


def _fake_client(captured: dict):
    """A genai client that records the prompt and returns a minimal brief.

    Async, because `generate_meeting_prep_analysis` awaits
    `client.aio.models.generate_content` — a sync stub would raise, land in the
    error branch, and quietly pass assertions that were never exercised.
    """

    async def _generate_content(*, model, contents, config):
        captured["prompt"] = contents
        captured["system"] = config.system_instruction
        return SimpleNamespace(
            text=json.dumps(
                {
                    "executive_summary": "A review of GCP spend.",
                    "stakeholder_map": [
                        {
                            "name": "Riley Fontaine",
                            "email": EXTERNAL,
                            "role": "Account Manager",
                            "context": "Owns the account.",
                        }
                    ],
                    # The model volunteering its own attendee_intel must be
                    # ignored — this is the detached-citation failure mode.
                    "attendee_intel": [{"email": "fabricated@nowhere.com"}],
                }
            )
        )

    aio = SimpleNamespace(models=SimpleNamespace(generate_content=_generate_content))
    return SimpleNamespace(aio=aio)


def _analyze(ctx: dict) -> tuple[object, dict]:
    captured: dict = {}
    with (
        patch(
            "rubik_core.models.get_genai_client", return_value=_fake_client(captured)
        ),
        patch("backend.routers.home._resolve_prep_debt", lambda *a, **k: None),
    ):
        resp = asyncio.run(generate_meeting_prep_analysis(ctx, USER))
    return resp, captured


def _base_ctx(**overrides) -> dict:
    ctx = {
        "title": "Acme / Northwind — GCP review",
        "description": "",
        "location": "",
        "start": "2026-08-20T15:00:00Z",
        "start_local": "Thu, Aug 20 at 10:00 AM",
        "attendees": [{"email": USER}, {"email": EXTERNAL}],
        "recent_emails": [],
        "past_meetings_report": "",
        "drive_context": "",
        "matched_products": [],
        "attendee_intel": [_intel_dict()],
        "is_sales_user": True,
        "is_internal": False,
        "focus_on": None,
        "event_id": "evt-1",
        "errors": [],
    }
    ctx.update(overrides)
    return ctx


def test_the_research_reaches_the_prompt():
    _, captured = _analyze(_base_ctx())

    assert "ATTENDEE INTELLIGENCE" in captured["prompt"]
    assert "Account Manager @ Northwind" in captured["prompt"]
    assert "idle non-prod" in captured["prompt"]
    # And the system instruction tells the model how much to trust the read.
    assert "confidence" in captured["system"]


def test_the_response_carries_the_researched_cards_verbatim():
    """Not what the model said — what the research found."""
    resp, _ = _analyze(_base_ctx())

    assert [a["email"] for a in resp.attendee_intel] == [EXTERNAL]
    assert resp.attendee_intel[0]["profile"]["current_company"] == "Northwind"
    assert resp.attendee_intel[0]["sources"] == [
        "https://linkedin.example/in/riley-fontaine"
    ]


def test_an_internal_brief_has_no_intelligence_block():
    """Internal prompts must be unchanged by this feature — including the
    instructions. Pointing a model at a section that isn't in its prompt is an
    invitation to invent the section's contents."""
    resp, captured = _analyze(
        _base_ctx(attendee_intel=[], is_internal=True, is_sales_user=False)
    )

    assert "ATTENDEE INTELLIGENCE" not in captured["prompt"]
    assert "ATTENDEE INTELLIGENCE" not in captured["system"]
    assert resp.attendee_intel == []


def test_the_sales_brief_keeps_its_original_search_mandate_without_research():
    """Turning the feature off must not leave the sales brief worse off than it
    was before the feature existed: search goes back to researching people."""
    _, captured = _analyze(_base_ctx(attendee_intel=[]))

    assert "ATTENDEE INTELLIGENCE" not in captured["prompt"]
    assert (
        "you MUST use the google_search tool to research the individual"
        in (captured["prompt"])
    )


def test_the_sales_brief_redirects_search_to_the_company_when_research_exists():
    _, captured = _analyze(_base_ctx())

    assert "already been researched for you" in captured["prompt"]
    assert (
        "you MUST use the google_search tool to research the individual"
        not in (captured["prompt"])
    )


def test_a_failed_synthesis_still_returns_the_research():
    """Research is already paid for; a synthesis failure shouldn't also cost the
    reader the attendee cards."""
    broken = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=MagicMock(side_effect=RuntimeError("model down"))
            )
        )
    )
    with patch("rubik_core.models.get_genai_client", return_value=broken):
        resp = asyncio.run(generate_meeting_prep_analysis(_base_ctx(), USER))

    assert "Error generating analysis" in resp.executive_summary
    assert [a["email"] for a in resp.attendee_intel] == [EXTERNAL]


def test_deep_prep_council_sees_the_same_block():
    """The emailed PDF and the on-screen brief must not form different
    impressions of the same person from the same evidence."""
    from rubik_core.deep_prep.council import _context_block

    block = _context_block(_base_ctx())

    assert "ATTENDEE INTELLIGENCE" in block
    assert "idle non-prod" in block
