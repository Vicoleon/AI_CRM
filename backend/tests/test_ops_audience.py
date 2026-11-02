"""Operator powers go by the admin role, and the ops alerts to the admins.

No department is special in code. A team that happens to be called "Systems"
(departments sync from each company's own directory) gets no operator power:
running the scheduled pipelines, the company-wide transcript sync and the
Ingestion Council reports are the admins' (or Cloud Scheduler's), and the
council's verdict and the morning delivery-failure alert go to the admins
unless a setting names other recipients or a department (matched by slug).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agents import ingestion_council
from backend.auth import get_current_user
from backend.main import app
from backend.routers import agents as agents_router
from backend.routers import jobs as jobs_router
from rubik_core.auth.session_tokens import mint_session_token
from rubik_core.company.operations import OperationsConfig, OperationsConfigUnavailable
from rubik_core.config import settings
from rubik_core.database.models import Base, Role, User
from rubik_core.database.schemas import UserProfile

client = TestClient(app)
CREDENTIALS = SimpleNamespace(scheme="Bearer", credentials="token")


def _profile(email: str, department: str, roles: list[str]) -> UserProfile:
    now = "2026-10-29T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles,
        department=department,
        created_at=now,
        updated_at=now,
    )


SYSTEMS_MEMBER = _profile("sys@acme.example", "Systems", ["user"])
ADMIN = _profile("admin@acme.example", "Marketing", ["admin", "user"])


# ── who may run the operator endpoints ─────────────────────────────────────


@pytest.mark.parametrize(
    "module, check",
    [
        (agents_router, "_is_authorized_run_caller"),
        (jobs_router, "_is_authorized_scheduler_caller"),
    ],
)
@pytest.mark.parametrize(
    "who, allowed", [(SYSTEMS_MEMBER, False), (ADMIN, True)], ids=["systems", "admin"]
)
def test_operator_endpoints_go_by_the_admin_role(
    module, check, who, allowed, monkeypatch
):
    monkeypatch.setattr(module, "get_current_user", lambda request, creds: who)
    verdict = getattr(module, check)(None, None, CREDENTIALS)
    granted = verdict[0] if isinstance(verdict, tuple) else verdict
    assert granted is allowed


@pytest.fixture
def launched(monkeypatch):
    """The background launcher, recorded instead of run."""
    from unittest.mock import MagicMock

    from backend.routers import ingest as ingest_router

    submit = MagicMock()
    monkeypatch.setattr(ingest_router, "submit_background_work", submit)
    yield submit
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "who, status", [(SYSTEMS_MEMBER, 403), (ADMIN, 200)], ids=["systems", "admin"]
)
def test_the_company_wide_transcript_sync_is_admins_only(launched, who, status):
    app.dependency_overrides[get_current_user] = lambda: who

    response = client.post("/api/ingest/meet-transcripts")

    assert response.status_code == status, response.text
    assert launched.called is (status == 200)


@pytest.mark.parametrize(
    "who, status", [(SYSTEMS_MEMBER, 403), (ADMIN, 200)], ids=["systems", "admin"]
)
def test_the_scheduler_sync_goes_by_the_admin_role(launched, monkeypatch, who, status):
    from backend.routers import ingest as ingest_router

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "the-key")
    app.dependency_overrides[get_current_user] = lambda: who  # the copilot gate
    monkeypatch.setattr(ingest_router, "get_current_user", lambda request, creds: who)

    token, _, _ = mint_session_token(who.email, "test")
    response = client.post(
        "/api/ingest/scheduler/sync", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == status, response.text
    assert launched.called is (status == 200)


# ── who hears about it ──────────────────────────────────────────────────────


@pytest.fixture
def people():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    admin, member = Role(name="admin"), Role(name="user")
    db.add_all(
        [
            User(email="admin@acme.example", id="a", name="A", department="Marketing"),
            User(email="sys@acme.example", id="s", name="S", department="Systems"),
            User(
                email="ops@acme.example", id="o", name="O", department=" Platform Ops"
            ),
        ]
    )
    db.flush()
    for user in db.query(User).all():
        user.roles = [admin, member] if user.email.startswith("admin") else [member]
    db.commit()
    yield db
    db.close()
    engine.dispose()


def _policy(monkeypatch, **changes):
    monkeypatch.setattr(
        ingestion_council,
        "get_operations_config",
        lambda: OperationsConfig(**changes),
    )


def test_the_council_reports_to_the_admins_by_default(people, monkeypatch):
    _policy(monkeypatch)

    assert ingestion_council._recipient_emails(people, None) == ["admin@acme.example"]


def test_a_setting_may_name_the_ops_department_matched_by_slug(people, monkeypatch):
    _policy(monkeypatch, ingestion_council_department="platform-ops")

    assert ingestion_council._recipient_emails(people, None) == ["ops@acme.example"]


def test_a_department_that_matches_nobody_falls_back_to_the_admins(
    people, monkeypatch, caplog
):
    """A misspelt or emptied department must not silence the ops alerts."""
    _policy(monkeypatch, ingestion_council_department="Platfrom Ops")

    with caplog.at_level("WARNING", logger=ingestion_council.logger.name):
        recipients = ingestion_council._recipient_emails(people, None)

    assert recipients == ["admin@acme.example"]
    warning = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning) == 1
    assert "Platfrom Ops" in warning[0].getMessage()
    assert "admins" in warning[0].getMessage()


def test_named_recipients_win_over_the_department(people, monkeypatch):
    _policy(
        monkeypatch,
        ingestion_council_recipients=("sys@acme.example",),
        ingestion_council_department="Platform Ops",
    )

    assert ingestion_council._recipient_emails(people, None) == ["sys@acme.example"]


def test_foreign_or_unregistered_recipient_cannot_override_workspace_admin(
    people, monkeypatch
):
    _policy(
        monkeypatch,
        ingestion_council_recipients=("lead@bravo.example",),
    )
    assert ingestion_council._recipient_emails(people, None) == ["admin@acme.example"]
    assert (
        ingestion_council._recipient_emails(
            people, ["lead@bravo.example", "sys@acme.example"]
        )
        == []
    )


def test_override_can_only_narrow_the_configured_report_audience(people, monkeypatch):
    _policy(
        monkeypatch,
        ingestion_council_recipients=("sys@acme.example", "ops@acme.example"),
    )

    assert ingestion_council._recipient_emails(
        people, ["sys@acme.example", "admin@acme.example"]
    ) == ["sys@acme.example"]


def test_unavailable_policy_refuses_recipients_even_with_override(people, monkeypatch):
    def unavailable():
        raise OperationsConfigUnavailable("workspace settings unavailable")

    monkeypatch.setattr(ingestion_council, "get_operations_config", unavailable)
    with pytest.raises(OperationsConfigUnavailable):
        ingestion_council._recipient_emails(people, ["sys@acme.example"])


def test_the_delivery_alert_goes_to_the_same_audience(people, monkeypatch):
    from backend.agents import slack_delivery

    _policy(monkeypatch)

    assert slack_delivery._ops_recipients(people) == ["admin@acme.example"]
