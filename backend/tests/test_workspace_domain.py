"""Verified workspace domains come only from the control database."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.main import app
from rubik_core.company import domain as company_domain
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceDomain
from rubik_core.database.schemas import UserProfile
from rubik_core.tenancy.context import use_workspace

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "acme.example"
_DIRECT_READ = re.compile(
    r"settings\.GOOGLE_WORKSPACE_DOMAIN"
    r"|getattr\(\s*settings\s*,\s*[\"']GOOGLE_WORKSPACE_DOMAIN[\"']"
    r"|\.get\(\s*[\"']workspace_domain[\"']"
)


def test_no_production_reader_uses_the_old_domain_sources():
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for top in ("rubik_core", "backend", "scripts")
        for path in sorted((REPO_ROOT / top).rglob("*.py"))
        if "tests" not in path.relative_to(REPO_ROOT).parts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _DIRECT_READ.search(line)
    ]
    assert hits == []


def test_primary_and_secondary_domains_are_control_owned(set_test_workspace_domains):
    set_test_workspace_domains(
        " @Acme.Example ", "ALIAS.EXAMPLE", primary="ALIAS.EXAMPLE"
    )
    assert company_domain.workspace_domain() == "alias.example"
    assert company_domain.workspace_domains() == frozenset(
        {"acme.example", "alias.example"}
    )


def test_secondary_domain_is_internal_across_meeting_and_roster_paths(
    set_test_workspace_domains, monkeypatch
):
    from backend.routers.email import _external_transcript_emails
    from backend.routers.home import _fetch_crm_relationships, _outside_attendees
    from rubik_core.call_capture.domain_match import own_domains
    from rubik_core.database.repository import is_placeholder_email
    from rubik_core.insights.population import on_workspace_domain
    from rubik_core.loop_closure.constants import internal_domains as loose_domains
    from rubik_core.meeting_prep.signals import internal_attendee_emails
    from rubik_core.orchestration.confirmation_render import internal_domains
    from rubik_core.people.service import build_queries
    from rubik_core.priorities.batch import is_eligible_priorities_email
    from rubik_core.smart_teams.authz import is_employee

    set_test_workspace_domains(DOMAIN, "subsidiary.alt")
    domains = company_domain.workspace_domains()
    attendees = [
        {"email": "pat@acme.example"},
        {"email": "colleague@subsidiary.alt"},
        {"email": "client@vendor.alt"},
    ]
    assert is_employee("colleague@subsidiary.alt")
    assert on_workspace_domain("colleague@subsidiary.alt")
    assert not is_placeholder_email("colleague@subsidiary.alt")
    assert is_eligible_priorities_email("colleague@subsidiary.alt", domains)
    assert own_domains(None) == domains
    assert set(loose_domains()) == domains
    assert set(internal_domains()) == domains
    assert internal_attendee_emails(attendees, "pat@acme.example") == [
        "colleague@subsidiary.alt"
    ]
    assert _outside_attendees(attendees, domains) == [{"email": "client@vendor.alt"}]
    assert _external_transcript_emails(
        "colleague@subsidiary.alt and client@vendor.alt",
        "pat@acme.example",
        domains,
    ) == ["client@vendor.alt"]
    queries, _ = build_queries(attendees, user_email="pat@acme.example")
    assert [query.email for query in queries] == ["client@vendor.alt"]

    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy import hubspot_config

    captured: list[list[str]] = []

    class FakeHubspot:
        def company_relationships_for_domains(self, values, **kwargs):
            captured.append(values)
            return []

    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", FakeHubspot)
    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: MagicMock(pipeline_ids=("test-pipeline",)),
    )
    monkeypatch.setattr(
        hubspot_api, "format_client_relationship_block", lambda _rels: "ok"
    )
    assert (
        _fetch_crm_relationships("pat@acme.example", {"attendees": attendees}) == "ok"
    )
    assert captured == [["vendor.alt"]]


def test_existing_domain_consumers_read_the_control_primary(
    set_test_workspace_domains, monkeypatch
):
    from backend.services.l10_summary_email import _company_domain
    from rubik_core.call_capture.domain_match import own_domains
    from rubik_core.database.repository import is_placeholder_email
    from rubik_core.graph.hubspot_sweep import _workspace_domain
    from rubik_core.insights.population import on_workspace_domain
    from rubik_core.loop_closure import constants as loop_constants
    from rubik_core.orchestration import confirmation_render
    from rubik_core.people.config import workspace_domain as people_domain
    from rubik_core.smart_teams import authz

    set_test_workspace_domains(DOMAIN)
    assert authz.workspace_domain() == DOMAIN
    assert authz.is_employee(f"pat@{DOMAIN}")
    assert people_domain() == DOMAIN
    assert _workspace_domain() == DOMAIN
    assert _company_domain() == DOMAIN
    assert loop_constants.internal_domains() == (DOMAIN,)
    assert confirmation_render.internal_domains() == (DOMAIN,)
    assert own_domains(None) == frozenset({DOMAIN})
    assert on_workspace_domain(f"pat@{DOMAIN}")
    assert not on_workspace_domain("vendor@outside.example")
    assert not is_placeholder_email(f"pat@{DOMAIN}")


def test_cache_is_scoped_by_workspace_and_explicitly_invalidated(
    set_test_workspace_domains,
):
    from rubik_core.loop_closure.constants import internal_domains as loose_domains
    from rubik_core.orchestration.confirmation_render import internal_domains

    set_test_workspace_domains("first.example")
    assert company_domain.workspace_domain() == "first.example"
    assert internal_domains() == ("first.example",)
    assert loose_domains() == ("first.example",)
    with control_session() as session:
        session.add(Workspace(slug="second", name="Second", status="active"))
        session.add(
            WorkspaceDomain(
                workspace_slug="second", domain="second.example", is_primary=True
            )
        )
        session.commit()
    try:
        with use_workspace("second"):
            assert company_domain.workspace_domain() == "second.example"
            assert company_domain.workspace_domains() == frozenset({"second.example"})
            assert internal_domains() == ("second.example",)
            assert loose_domains() == ("second.example",)
        set_test_workspace_domains("changed.example")
        assert company_domain.workspace_domain() == "changed.example"
        with use_workspace("second"):
            assert company_domain.workspace_domain() == "second.example"
    finally:
        with control_session() as session:
            session.query(WorkspaceDomain).filter_by(workspace_slug="second").delete()
            session.query(Workspace).filter_by(slug="second").delete()
            session.commit()
        company_domain.clear_cache("second")


def test_domain_write_cannot_republish_a_removed_inflight_read(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from rubik_core.control import repository as control_repo

    started = Event()
    proceed = Event()
    current = {"rows": (("old.example", True),)}
    reads: list[tuple[tuple[str, bool], ...]] = []

    def delayed_read(_slug):
        rows = current["rows"]
        reads.append(rows)
        if len(reads) == 1:
            started.set()
            assert proceed.wait(timeout=5)
        return rows

    monkeypatch.setattr(control_repo, "domains_for_workspace", delayed_read)
    company_domain.clear_cache("test")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(company_domain.workspace_domains)
            assert started.wait(timeout=5)
            current["rows"] = (("new.example", True),)
            company_domain.clear_cache("test")
            proceed.set()
            assert future.result(timeout=5) == frozenset({"new.example"})
        assert len(reads) == 2
        assert company_domain.workspace_domains() == frozenset({"new.example"})
        assert len(reads) == 2
    finally:
        proceed.set()
        company_domain.clear_cache("test")


def test_unconfigured_workspace_has_no_domain(set_test_workspace_domains):
    set_test_workspace_domains()
    assert company_domain.workspace_domain() == ""
    assert company_domain.workspace_domains() == frozenset()


def test_control_domain_drives_sign_in(monkeypatch, set_test_workspace_domains):
    from rubik_core.auth.google_id_token import GoogleIdentity

    set_test_workspace_domains(DOMAIN)
    person = UserProfile(
        id="pat",
        email=f"pat@{DOMAIN}",
        name="Pat",
        roles=["user"],
        created_at="2026-10-29T00:00:00",
        updated_at="2026-10-29T00:00:00",
    )
    monkeypatch.setattr(
        "backend.routers.auth_session.verify_google_id_token",
        lambda raw: GoogleIdentity(
            email=person.email, hd=DOMAIN, sub="google-sub", name="Pat", picture=None
        ),
    )
    monkeypatch.setattr(
        "backend.routers.auth_session._upsert_verified_user",
        lambda request, identity: person,
    )
    monkeypatch.setattr(
        "backend.routers.auth_session.mint_session_token",
        lambda email, slug, **kwargs: (
            f"{email}@{slug}",
            datetime.now(timezone.utc),
            1.0,
        ),
    )
    client = TestClient(app)
    accepted = client.post(
        "/api/auth/session", headers={"Authorization": "Bearer signed-google-token"}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["token"] == f"{person.email}@test"
    set_test_workspace_domains("other.example")
    refused = client.post(
        "/api/auth/session", headers={"Authorization": "Bearer signed-google-token"}
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "workspace_unknown"


def test_removing_domain_revokes_bound_session_before_user_lookup(monkeypatch):
    from backend.auth import get_current_user
    from rubik_core.auth.session_tokens import mint_session_token

    with control_session() as session:
        session.add(Workspace(slug="removed", name="Removed", status="active"))
        session.add(
            WorkspaceDomain(
                workspace_slug="removed", domain="removed.example", is_primary=True
            )
        )
        session.commit()
    try:
        with use_workspace("removed"):
            token, _, _ = mint_session_token(
                "pat@removed.example", "removed", hd="removed.example", hours=1
            )
            with control_session() as session:
                session.query(WorkspaceDomain).filter_by(
                    workspace_slug="removed"
                ).delete()
                session.commit()
            with patch("backend.auth.user_registry.get_user") as lookup:
                with pytest.raises(HTTPException) as refused:
                    get_current_user(MagicMock(), MagicMock(credentials=token))
            assert refused.value.status_code == 401
            lookup.assert_not_called()
    finally:
        with control_session() as session:
            session.query(WorkspaceDomain).filter_by(workspace_slug="removed").delete()
            session.query(Workspace).filter_by(slug="removed").delete()
            session.commit()


def test_workspace_admin_cannot_edit_control_domain():
    from backend.auth import admin_required
    from rubik_core.database.repository import SQLUserRegistry

    with pytest.raises(ValueError, match="cannot be edited"):
        SQLUserRegistry().update_workspace_settings(
            {"workspace_domain": "evil.example"}
        )
    stale_db = MagicMock()
    stale_db.get.return_value.value = {
        "workspace_sync_enabled": True,
        "workspace_domain": "stale.example",
        "admin_email": "accidental-admin@example.com",
    }
    assert SQLUserRegistry(stale_db).get_workspace_settings() == {
        "workspace_sync_enabled": True,
        "multi_gmail_enabled": False,
    }

    admin = UserProfile(
        id="admin",
        email="admin@acme.example",
        name="Admin",
        roles=["admin", "user"],
        created_at="2026-10-29T00:00:00",
        updated_at="2026-10-29T00:00:00",
    )
    app.dependency_overrides[admin_required] = lambda: admin
    try:
        response = TestClient(app).patch(
            "/api/users/workspace/settings", json={"workspace_domain": "evil.example"}
        )
        assert response.status_code == 422
        assert company_domain.workspace_domain() == DOMAIN
    finally:
        app.dependency_overrides.clear()
