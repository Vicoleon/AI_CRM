"""A platform support identity is usable for FKs, but is never company staff."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sql_text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

pytestmark = pytest.mark.usefixtures("roster_admits_acme_example")


@pytest.fixture
def db(set_test_workspace_domains):
    from rubik_core.database.models import Base, User

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    set_test_workspace_domains("acme.example")
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add_all(
            [
                User(
                    email="rep@acme.example",
                    id="rep",
                    name="Real Rep",
                    department="Sales",
                    last_login=datetime(2026, 10, 1),
                ),
                User(
                    email="support@acme.example",
                    id="support",
                    name="Platform Support",
                    department="Sales",
                    last_login=datetime(2026, 10, 1),
                    is_platform_support=True,
                ),
                User(
                    email="support-only@acme.example",
                    id="support-only",
                    name="Second Support",
                    department="Platform",
                    last_login=datetime(2026, 10, 1),
                    is_platform_support=True,
                ),
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def test_support_user_stays_out_of_staff_and_priorities_rosters(db, monkeypatch):
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    from backend.agents.slack_delivery import _department_members
    from rubik_core.database.repository import SQLUserRegistry
    from rubik_core.loop_closure.roster import roster_emails
    from rubik_core.priorities.batch import priorities_roster

    assert [user.email for user in SQLUserRegistry(db).list_users()] == [
        "rep@acme.example"
    ]
    assert priorities_roster(db) == ["rep@acme.example"]
    assert roster_emails(db, deactivated_lookup=lambda _: set()) == ["rep@acme.example"]
    assert _department_members(db, "Sales") == ["rep@acme.example"]
    support = SQLUserRegistry(db).get_user("support@acme.example")
    assert support is not None and support.is_platform_support
    assert not support.is_admin


def test_support_row_cannot_be_converted_to_normal_user_by_registry(db):
    from rubik_core.database.models import User
    from rubik_core.database.repository import SQLUserRegistry

    registry = SQLUserRegistry(db)
    with pytest.raises(ValueError, match="platform support"):
        registry.register_user(
            "support@acme.example", "New Name", "new-google-id", roles=["admin"]
        )
    with pytest.raises(ValueError, match="platform support"):
        registry.update_user("support@acme.example", {"roles": ["admin"]})
    db.expire_all()
    row = db.get(User, "support@acme.example")
    assert row.name == "Platform Support"
    assert row.id == "support"
    assert not row.roles
    assert row.is_platform_support


def test_support_user_stays_out_of_eos_people_and_insights(db):
    from backend.routers.eos.people import _sync_people_from_users
    from rubik_core.database.eos import Person
    from rubik_core.insights.population import load_population
    from rubik_core.insights.scope import Scope, known_department_labels

    _sync_people_from_users(db)
    assert {row.email for row in db.query(Person).all()} == {"rep@acme.example"}
    scope = Scope(
        viewer_email="rep@acme.example",
        all_departments=True,
        allowed=("sales",),
        selected=("sales",),
        filtered=False,
        labels={"sales": "Sales"},
    )
    assert set(load_population(db, scope).people) == {"rep@acme.example"}
    assert known_department_labels(db) == {"sales": "Sales"}


def test_background_and_webhook_rosters_skip_support_user(db, monkeypatch):
    import backend.routers.fathom as fathom
    import rubik_core.database.db as db_mod
    import rubik_core.features as features
    from backend.agents import ingestion_council
    from backend.agents.ingestion_council import _recipient_emails
    from backend.jobs.crm_sync_job import _eligible_emails
    from rubik_core.company.operations import OperationsConfig

    monkeypatch.setattr(
        ingestion_council,
        "get_operations_config",
        lambda: OperationsConfig(ingestion_council_department="Sales"),
    )
    assert _recipient_emails(db, None) == ["rep@acme.example"]
    assert _recipient_emails(db, ["support@acme.example", "rep@acme.example"]) == [
        "rep@acme.example"
    ]

    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    monkeypatch.setattr(features, "feature_allows", lambda *_args: True)
    monkeypatch.setattr(fathom, "feature_allows", lambda *_args: True)
    assert _eligible_emails() == ["rep@acme.example"]
    assert fathom._host_may_auto_ingest("rep@acme.example")
    assert not fathom._host_may_auto_ingest("support@acme.example")


def test_l10_attendee_picker_and_digest_exclude_support(db, monkeypatch):
    import backend.routers.eos._authz as authz
    from backend.routers.eos.l10 import _attendee_pool_emails
    from backend.services.l10_summary_email import resolve_digest_recipients

    meeting = SimpleNamespace(
        roster_emails=["rep@acme.example", "support@acme.example"],
        is_exec_meeting=False,
        department_name="Sales",
        name="Sales L10",
    )
    assert _attendee_pool_emails(db, meeting) == ["rep@acme.example"]
    monkeypatch.setattr(authz, "can_access_meeting", lambda *_args: True)
    recipients, excluded = resolve_digest_recipients(
        db, meeting, SimpleNamespace(id=uuid4()), SimpleNamespace()
    )
    assert recipients == ["rep@acme.example"]
    assert excluded["support@acme.example"] == (
        "platform support is not a meeting recipient"
    )


def test_migration_backfills_existing_users_as_non_support(tmp_path):
    root = Path(__file__).resolve().parents[2]
    path = tmp_path / "test.db"
    env = {
        **os.environ,
        "APP_MODE": "local",
        "DATABASE_URL": f"sqlite:///{path}",
        "WORKSPACE_DATABASE_DIR": str(tmp_path),
        "CUBIE_WORKSPACE": "test",
        "CLOUDSQL_CONNECTION_NAME": "",
        "GCP_PROJECT_ID": "",
        "PYTHONPATH": str(root),
    }
    for revision in ("0001_cubie_baseline", "head"):
        if revision == "head":
            with create_engine(f"sqlite:///{path}").begin() as conn:
                conn.execute(
                    sql_text(
                        "INSERT INTO users (email, id, name, is_exec) "
                        "VALUES ('rep@acme.example', 'rep', 'Real Rep', 0)"
                    )
                )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-x",
                "workspace=test",
                "upgrade",
                revision,
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
    with create_engine(f"sqlite:///{path}").connect() as conn:
        assert (
            conn.scalar(
                sql_text(
                    "SELECT is_platform_support FROM users WHERE email='rep@acme.example'"
                )
            )
            == 0
        )


def test_smart_teams_inference_and_l10_manager_identity_skip_support(db):
    from backend.routers.eos._manager_access import _has_active_account
    from rubik_core.smart_teams.skill_inference import roster

    assert roster(db) == ["rep@acme.example"]
    assert _has_active_account(db, "rep@acme.example")
    assert not _has_active_account(db, "support@acme.example")


def test_promise_team_report_never_counts_support_as_an_owner(db, monkeypatch):
    from datetime import timezone

    from backend.services.promise_report import build_report, resolve_owners

    executive = SimpleNamespace(email="leader@acme.example", sees_all_departments=True)
    owners, refusal = resolve_owners(db, executive, whose="team", team="")
    assert refusal is None
    assert owners == ["rep@acme.example"]

    report = build_report(
        db, executive, whose="team", now=datetime(2026, 10, 29, tzinfo=timezone.utc)
    )
    assert report["status"] == "success"
    assert report["people"] == 1

    monkeypatch.setattr(
        "backend.routers.eos._authz.email_scope",
        lambda *_args: SimpleNamespace(
            owner_emails=frozenset(
                {
                    "rep@acme.example",
                    "SUPPORT@ACME.EXAMPLE",
                    "support-only@acme.example",
                }
            )
        ),
    )
    monkeypatch.setattr("backend.routers.eos._authz.team_scope_problem", lambda _: None)
    manager = SimpleNamespace(email="manager@acme.example", sees_all_departments=False)
    owners, refusal = resolve_owners(db, manager, whose="team", team="")
    assert refusal is None
    assert owners == ["rep@acme.example"]


def test_voice_spelling_hints_skip_support_identity(db):
    from backend.routers.voice import people_names

    assert people_names(db) == ["Real Rep"]


def test_voice_guide_finder_skips_support_identity(db, monkeypatch):
    from backend.services import voice_records

    monkeypatch.setattr(
        "rubik_core.smart_teams.guide.published_guide",
        lambda *_args: object(),
    )
    ctx = SimpleNamespace(db=db)
    real = voice_records._guide(ctx, {"name": "Real Rep"})
    assert [place.path for place in real.places] == [
        "/smart-teams/guide/rep@acme.example"
    ]
    with pytest.raises(voice_records._Refusal, match="couldn't find a published"):
        voice_records._guide(ctx, {"name": "Platform Support"})
