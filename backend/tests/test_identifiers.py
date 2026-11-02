"""Stored identifiers are frozen as named constants in one module.

Some strings outlive the code that writes them: ADK session rows keep the app
and agent names, the vector store keeps its collection name, and database rows
keep an origin, a speaker, a detail key, a close reason, an asset tag, an audit
marker or a sync outcome. Renaming one by a text search would orphan every row
that holds the old value, so each lives once in ``rubik_core.identifiers`` and
every writer and reader imports it from there.

These tests pin the values, check that each consumer reads its constant from
that module (rather than repeating a literal), and check that the frontend
mirrors of three of them agree with the backend.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.tests._legacy_brand import LEGACY_PATTERN
from rubik_core import identifiers

REPO_ROOT = Path(__file__).resolve().parents[2]

FROZEN = {
    "ADK_APP_NAME": "cubie",
    "ADK_AGENT_NAME": "cubie_agent",
    "ADK_COORDINATOR_NAME": "cubie_coordinator",
    "NIGHTLY_APP_NAME": "cubie_nightly",
    "IMPERSONATION_APP_NAME": "cubie_impersonation",
    "CHROMA_COLLECTION": "cubie",
    "CALL_ORIGIN_ASSISTANT": "cubie",
    "SMART_TEAMS_ASSISTANT_SPEAKER": "cubie",
    "CRM_LOGGED_DETAIL_KEY": "cubie_logged",
    "LOOP_CLOSE_LOGGED_FROM_ASSISTANT": "logged_from_assistant",
    "ASSET_TAG_GENERATED": "cubie-generated",
    "AUDIT_VIA_ASSISTANT": "cubie",
    "FATHOM_SKIP_HOST_OUTSIDE": "skipped_host_outside_workspace",
}

# (consumer module, the constant it must import from rubik_core.identifiers)
CONSUMERS = [
    ("rubik_core.orchestration.adk_orchestrator", "ADK_APP_NAME"),
    ("rubik_core.adk_agent.agent", "ADK_AGENT_NAME"),
    ("rubik_core.agent_runtime.coordinator", "ADK_COORDINATOR_NAME"),
    ("backend.routers.jobs", "NIGHTLY_APP_NAME"),
    ("rubik_core.agent_runtime.hubspot_only_chat", "IMPERSONATION_APP_NAME"),
    ("rubik_core.adapters.chroma", "CHROMA_COLLECTION"),
    ("rubik_core.database.call_capture", "CALL_ORIGIN_ASSISTANT"),
    ("backend.routers.calls", "CALL_ORIGIN_ASSISTANT"),
    ("rubik_core.smart_teams.instruments.strengths", "SMART_TEAMS_ASSISTANT_SPEAKER"),
    ("rubik_core.loop_closure.crm_log", "CRM_LOGGED_DETAIL_KEY"),
    ("rubik_core.loop_closure.store", "CRM_LOGGED_DETAIL_KEY"),
    ("rubik_core.loop_closure.store", "LOOP_CLOSE_LOGGED_FROM_ASSISTANT"),
    ("rubik_core.database.loop_closure", "LOOP_CLOSE_LOGGED_FROM_ASSISTANT"),
    ("backend.routers.loose_ends", "LOOP_CLOSE_LOGGED_FROM_ASSISTANT"),
    ("rubik_core.insights.dashboards.crm", "LOOP_CLOSE_LOGGED_FROM_ASSISTANT"),
    (
        "rubik_core.insights.dashboards.follow_through",
        "LOOP_CLOSE_LOGGED_FROM_ASSISTANT",
    ),
    ("backend.services.generated_docs", "ASSET_TAG_GENERATED"),
    ("backend.routers.browser_extension", "AUDIT_VIA_ASSISTANT"),
    ("rubik_core.call_capture.fathom_attach", "FATHOM_SKIP_HOST_OUTSIDE"),
]


def test_every_stored_identifier_has_its_frozen_value():
    assert {name: getattr(identifiers, name, None) for name in FROZEN} == FROZEN


def test_the_module_holds_only_the_frozen_identifiers():
    public = {
        name
        for name, value in vars(identifiers).items()
        if name.isupper() and isinstance(value, str)
    }
    assert public == set(FROZEN)


def test_no_frozen_identifier_carries_the_legacy_brand():
    source = Path(inspect.getsourcefile(identifiers)).read_text(encoding="utf-8")
    assert LEGACY_PATTERN.search(source) is None


@pytest.mark.parametrize(("module_name", "constant"), CONSUMERS)
def test_each_consumer_imports_its_constant(module_name, constant):
    module = importlib.import_module(module_name)
    assert getattr(module, constant, None) == getattr(identifiers, constant)
    source = Path(inspect.getsourcefile(module)).read_text(encoding="utf-8")
    assert "rubik_core.identifiers import" in source


def test_call_capture_default_origin_is_the_assistant_origin():
    from rubik_core.database.call_capture import CallSession

    default = CallSession.__table__.c.origin.default.arg
    assert default == identifiers.CALL_ORIGIN_ASSISTANT


def test_adk_orchestrator_names_no_app_but_the_frozen_one():
    from rubik_core.orchestration import adk_orchestrator

    source = inspect.getsource(adk_orchestrator)
    assert adk_orchestrator.ADK_APP_NAME == identifiers.ADK_APP_NAME
    assert 'app_name="' not in source
    assert 'name="' + "rubik" not in source


def test_the_assistant_speaker_is_the_frozen_value():
    from rubik_core.smart_teams.instruments.strengths import Speaker

    speaker = identifiers.SMART_TEAMS_ASSISTANT_SPEAKER
    assert {member.value for member in Speaker} == {"interviewee", speaker}
    assert Speaker(speaker) is not Speaker.INTERVIEWEE


def test_the_chroma_adapter_defaults_to_the_frozen_collection():
    from rubik_core.adapters.chroma import ChromaAdapter

    params = inspect.signature(ChromaAdapter.__init__).parameters
    assert params["collection_name"].default == identifiers.CHROMA_COLLECTION


def test_a_generated_asset_is_recognised_by_the_frozen_tag():
    from backend.services.generated_docs import is_generated_asset

    tagged = SimpleNamespace(tags=[identifiers.ASSET_TAG_GENERATED, "meeting-prep"])
    assert is_generated_asset(tagged)
    assert not is_generated_asset(SimpleNamespace(tags=["meeting-prep"]))


def test_the_frontend_mirrors_the_stored_values():
    smart_teams = (REPO_ROOT / "frontend/src/api/smartTeams.ts").read_text()
    speaker = identifiers.SMART_TEAMS_ASSISTANT_SPEAKER
    assert f"export type Speaker = 'interviewee' | '{speaker}';" in smart_teams

    support = (REPO_ROOT / "frontend/src/pages/SmartTeamsSupport.tsx").read_text()
    assert f"turn.speaker === '{speaker}'" in support

    assets = (REPO_ROOT / "frontend/src/pages/Assets.tsx").read_text()
    assert f"a.tags?.includes('{identifiers.ASSET_TAG_GENERATED}')" in assets

    loose_ends = (REPO_ROOT / "frontend/src/api/looseEnds.ts").read_text()
    reason = identifiers.LOOP_CLOSE_LOGGED_FROM_ASSISTANT
    assert f"    '{reason}',\n" in loose_ends  # CLOSE_REASONS
    assert f"    {reason}: '" in loose_ends  # CLOSE_REASON_LABELS


def test_the_close_reason_check_admits_the_frozen_value():
    from rubik_core.database.loop_closure import LOOP_CLOSE_REASONS

    assert identifiers.LOOP_CLOSE_LOGGED_FROM_ASSISTANT in LOOP_CLOSE_REASONS
