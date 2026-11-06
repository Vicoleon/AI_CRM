"""Phase 0 of the customer relationship graph (docs/customer_relationship_graph.md):
Meeting Prep learns when the reader ACTUALLY last met the people on the invite.

What this asserts about the seam:

* `list_shared_meetings` is ACL-safe by construction — a transcript only
  qualifies when the READER has a meeting_participants row, the same gate
  `search_past_meetings` enforces. A meeting only the counterparts attended
  never surfaces, however relevant it looks.
* The attendance block reaches both prompt personas together with its grounding
  rule, and an empty result leaves the prompt without the section AND without
  the rule — the UC-47 discipline: pointing a model at a section that isn't in
  its prompt is an invitation to invent the section's contents.
* The lookup is fail-soft (a broken table is a partial error, never a failed
  briefing) and the kill switch means the repository is never even called.
* The all-hands guard: a huge invite falls back to external attendees only, and
  skips entirely rather than dump half the company's meeting history into a
  prompt.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.routers.home import (
    _format_prior_meetings_block,
    _persist_meeting_prep_run,
    collect_meeting_prep_context,
    generate_meeting_prep_analysis,
)
from rubik_core.database.repository import SharedMeeting

USER = "bellison@acme.example"
COLLEAGUE = "ifoster@acme.example"
EXTERNAL = "riley.fontaine@northwindholding.alt"
EXTERNAL_2 = "maria.ops@northwindholding.alt"


def _wire_sqlite(tmp_path, monkeypatch):
    """Schema'd throwaway sqlite, rebound wherever SessionLocal was captured.
    Same helper pattern as test_meet_transcripts.py."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database import db as db_module
    from rubik_core.database import repository as repo_module
    from rubik_core.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/acl.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(repo_module, "SessionLocal", TestSession)
    return TestSession


def _participants_repo():
    """Resolve the repository class at CALL time, never at import time.

    test_assets_rbac.py and test_dam_processor.py importlib.reload() the
    repository module mid-suite, so a class object bound when this module was
    collected goes stale in full-suite runs — methods patched on it are never
    hit by code that re-imports the module at call time.
    """
    from rubik_core.database import repository

    return repository.SQLMeetingParticipantsRepository()


def _seed_meeting(
    TestSession,
    doc_id: str,
    title: str,
    created_at: datetime,
    participants: list[str],
    status: str = "approved",
) -> None:
    from rubik_core.database.models import Asset

    with TestSession() as s:
        s.add(
            Asset(
                doc_id=doc_id,
                owner_email=USER,
                status=status,
                topic="Meeting Transcripts",
                original_filename=title,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        s.commit()
    _participants_repo().upsert_participants(
        doc_id, meeting_id=f"m-{doc_id}", emails=participants
    )


# ── the repository method ────────────────────────────────────────────────────


def test_shared_meetings_requires_reader_attendance(tmp_path, monkeypatch):
    """A meeting only the counterpart attended must never surface — the reader's
    own attendance row is the access boundary, exactly as in search_past_meetings."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_meeting(
        TestSession,
        "ours",
        "Acme / Northwind QBR",
        datetime(2026, 8, 4),
        [USER, EXTERNAL],
    )
    _seed_meeting(
        TestSession,
        "theirs",
        "Northwind internal sync",
        datetime(2026, 8, 10),
        [EXTERNAL, EXTERNAL_2],
    )

    shared = _participants_repo().list_shared_meetings(USER, [EXTERNAL])

    assert [m.doc_id for m in shared] == ["ours"]
    assert shared[0].title == "Acme / Northwind QBR"
    assert shared[0].overlap == [EXTERNAL]


def test_shared_meetings_orders_newest_first_and_limits(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_meeting(
        TestSession, "june", "June sync", datetime(2026, 6, 27), [USER, EXTERNAL]
    )
    _seed_meeting(
        TestSession, "aug", "August QBR", datetime(2026, 8, 4), [USER, EXTERNAL]
    )
    _seed_meeting(
        TestSession, "may", "May kickoff", datetime(2026, 5, 1), [USER, EXTERNAL]
    )

    shared = _participants_repo().list_shared_meetings(USER, [EXTERNAL], limit=2)

    assert [m.doc_id for m in shared] == ["aug", "june"]


def test_shared_meetings_excludes_non_live_assets(tmp_path, monkeypatch):
    """Soft-deleted and failed transcripts are not history the briefing may cite."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_meeting(
        TestSession, "live", "Live sync", datetime(2026, 8, 1), [USER, EXTERNAL]
    )
    _seed_meeting(
        TestSession,
        "gone",
        "Deleted sync",
        datetime(2026, 8, 5),
        [USER, EXTERNAL],
        status="deleted",
    )
    _seed_meeting(
        TestSession,
        "broken",
        "Failed ingest",
        datetime(2026, 8, 6),
        [USER, EXTERNAL],
        status="failed",
    )

    shared = _participants_repo().list_shared_meetings(USER, [EXTERNAL])

    assert [m.doc_id for m in shared] == ["live"]


def test_shared_meetings_reports_which_counterparts_overlap(tmp_path, monkeypatch):
    """The overlap names who of the COUNTERPARTS was in the room — not everyone
    who attended, and never the reader (even if passed as a counterpart)."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _seed_meeting(
        TestSession,
        "d1",
        "Big review",
        datetime(2026, 8, 4),
        [USER, COLLEAGUE, EXTERNAL, EXTERNAL_2],
    )

    shared = _participants_repo().list_shared_meetings(
        USER, [EXTERNAL, COLLEAGUE, USER]
    )

    assert len(shared) == 1
    assert shared[0].overlap == sorted([COLLEAGUE, EXTERNAL])
    assert EXTERNAL_2 not in shared[0].overlap
    assert USER not in shared[0].overlap


# ── the collect step ─────────────────────────────────────────────────────────


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


def _collect(event: dict, *, shared=None, shared_side_effect=None, flag: bool = True):
    """Run collect_meeting_prep_context with every external fetch stubbed and the
    attendance lookup replaced by a spy. Returns (ctx, spy)."""
    tools = MagicMock()

    async def _no_meetings(**kwargs):
        return {"status": "error"}

    tools.search_past_meetings = _no_meetings

    async def _no_research(attendees, **kwargs):
        return SimpleNamespace(
            intel=[], skipped_attendees=[], errors=[], as_dicts=lambda: []
        )

    spy = MagicMock(return_value=shared or [], side_effect=shared_side_effect)

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
        patch("rubik_core.people.research_attendees", _no_research),
        # String targets, resolved at test time: the repository module gets
        # importlib.reload()ed by earlier suites, so identity-based patching
        # of an import-time class binding silently misses in full-suite runs.
        patch(
            "rubik_core.database.repository"
            ".SQLMeetingParticipantsRepository.list_shared_meetings",
            spy,
        ),
        patch("rubik_core.config.settings.CUBIE_PREP_ATTENDANCE_LOOKUP", flag),
    ):
        ctx = asyncio.run(
            collect_meeting_prep_context(
                user_email=USER,
                user_department="Systems",
                user_roles=[],
                event=event,
            )
        )
    return ctx, spy


_SHARED = [
    SharedMeeting(
        doc_id="d1",
        title="Acme / Northwind QBR",
        when=datetime(2026, 8, 4),
        overlap=[EXTERNAL],
    ),
    SharedMeeting(
        doc_id="d2",
        title="Northwind GCP migration sync",
        when=datetime(2026, 6, 27),
        overlap=[EXTERNAL],
    ),
]


def test_prep_context_and_bundle_include_attendance_block():
    ctx, spy = _collect(_event(USER, EXTERNAL), shared=_SHARED)

    # The spy was consulted with the counterparties (never the reader).
    assert spy.call_args[0][0] == USER
    assert spy.call_args[0][1] == [EXTERNAL]

    block = ctx["prior_meetings_block"]
    assert block.startswith("## PRIOR MEETINGS WITH THESE ATTENDEES")
    assert "You last met someone on this invite on Tuesday, August 04, 2026." in block
    assert '"Acme / Northwind QBR"' in block
    assert "Saturday, June 27, 2026" in block

    # And the persisted run snapshots the exact block the prompt used.
    db = MagicMock()
    resp = SimpleNamespace(
        title="T", partial_errors=[], model_dump=lambda: {"title": "T"}
    )
    run_id = _persist_meeting_prep_run(db, USER, ctx, resp)
    assert run_id is not None
    persisted = db.add.call_args[0][0]
    assert persisted.context_bundle["prior_meetings_block"] == block


def test_no_shared_meetings_means_no_block():
    ctx, spy = _collect(_event(USER, EXTERNAL), shared=[])

    spy.assert_called_once()
    assert ctx["prior_meetings_block"] == ""
    assert _format_prior_meetings_block([]) == ""


def test_flag_off_never_touches_the_repository():
    ctx, spy = _collect(_event(USER, EXTERNAL), shared=_SHARED, flag=False)

    spy.assert_not_called()
    assert ctx["prior_meetings_block"] == ""


def test_attendance_lookup_failure_is_partial_error():
    ctx, _ = _collect(
        _event(USER, EXTERNAL), shared_side_effect=RuntimeError("table on fire")
    )

    assert any("Attendance lookup failure" in e for e in ctx["errors"])
    assert ctx["prior_meetings"] == []
    assert ctx["prior_meetings_block"] == ""


def test_all_hands_guard_restricts_to_external_attendees():
    colleagues = [f"person{i}@acme.example" for i in range(16)]
    ctx, spy = _collect(_event(USER, *colleagues, EXTERNAL, EXTERNAL_2), shared=[])

    # 18 counterparties > 15 → the lookup falls back to the external attendees.
    assert sorted(spy.call_args[0][1]) == sorted([EXTERNAL, EXTERNAL_2])


def test_all_hands_guard_skips_when_even_externals_are_too_many():
    externals = [f"guest{i}@northwindholding.alt" for i in range(16)]
    ctx, spy = _collect(_event(USER, COLLEAGUE, *externals), shared=[])

    spy.assert_not_called()
    assert ctx["prior_meetings_block"] == ""


# ── how the brief uses it ────────────────────────────────────────────────────


def _fake_client(captured: dict):
    """A genai client that records the prompt and returns a minimal brief.
    Async, matching how generate_meeting_prep_analysis awaits it."""

    async def _generate_content(*, model, contents, config):
        captured["prompt"] = contents
        captured["system"] = config.system_instruction
        return SimpleNamespace(
            text=json.dumps({"executive_summary": "A review of GCP spend."})
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
        "attendee_intel": [],
        "prior_meetings_block": "",
        "is_sales_user": True,
        "is_internal": False,
        "focus_on": None,
        "event_id": "evt-1",
        "errors": [],
    }
    ctx.update(overrides)
    return ctx


_BLOCK = _format_prior_meetings_block(_SHARED)


def test_block_and_rule_reach_the_sales_prompt():
    _, captured = _analyze(_base_ctx(prior_meetings_block=_BLOCK))

    assert "## PRIOR MEETINGS WITH THESE ATTENDEES" in captured["prompt"]
    assert "Acme / Northwind QBR" in captured["prompt"]
    # The grounding rule rides in the system instruction, next to the others.
    assert "comes from attendance records" in captured["system"]


def test_block_and_rule_reach_the_internal_prompt():
    _, captured = _analyze(
        _base_ctx(
            prior_meetings_block=_BLOCK,
            is_sales_user=False,
            is_internal=True,
            attendees=[{"email": USER}, {"email": COLLEAGUE}],
        )
    )

    assert "## PRIOR MEETINGS WITH THESE ATTENDEES" in captured["prompt"]
    assert "comes from attendance records" in captured["system"]


def test_empty_block_leaves_prompt_and_instructions_clean():
    """No block → no section and no rule pointing at it, in either persona.
    (Pointing a model at a section that isn't there invites it to invent one.)"""
    for overrides in (
        {},
        {"is_sales_user": False, "is_internal": True},
    ):
        _, captured = _analyze(_base_ctx(**overrides))
        assert "PRIOR MEETINGS WITH THESE ATTENDEES" not in captured["prompt"]
        assert "attendance records" not in captured["system"]
