"""Regression tests for the crm_sync nightly sweep entrypoint."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.jobs import crm_sync_job
from backend.tests._rosters import pin_rosters
from rubik_core.features import FeaturePolicy, clear_cache


@pytest.fixture
def user_db(monkeypatch):
    """A real (in-memory) User table the sweep's roster query runs against."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from rubik_core.config import settings
    from rubik_core.database.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", factory)
    # The roster test exercises the feature after its process kill switch has
    # been enabled; otherwise the live gate correctly admits nobody.
    monkeypatch.setattr(settings, "CUBIE_CRM_SYNC_ENABLED", True)
    clear_cache()
    yield factory
    clear_cache()


def _add_people(factory, *people: tuple[str, str]) -> None:
    from rubik_core.database.models import User

    with factory() as db:
        for email, department in people:
            db.add(
                User(
                    email=email,
                    id=email.split("@")[0],
                    name=email,
                    department=department,
                )
            )
        db.commit()


def test_the_roster_query_matches_the_real_user_model(user_db):
    """`User.is_active` does not exist — referencing it killed the sweep.

    Runs the real query against a real (empty) User table, so an invented
    column fails here instead of at 3am in Cloud Run.
    """
    assert crm_sync_job._eligible_emails() == []


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_the_sweep_covers_everyone_crm_sync_admits_by_default(user_db):
    """Open to everyone by default: no department name decides who is scanned,
    and someone with no department is scanned too. Fixtures never are."""
    _add_people(
        user_db,
        ("ae@acme.example", "Account Executives"),
        ("rep@acme.example", "Sales"),
        ("new@acme.example", ""),
        ("boss@example.com", "Sales"),
    )

    assert sorted(crm_sync_job._eligible_emails()) == [
        "ae@acme.example",
        "new@acme.example",
        "rep@acme.example",
    ]


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_the_sweep_follows_a_narrowed_crm_sync_list(user_db, monkeypatch):
    _add_people(
        user_db,
        ("ae@acme.example", "Account Executives"),
        ("rep@acme.example", "Sales"),
    )
    pin_rosters(
        monkeypatch,
        {"crm_sync": FeaturePolicy(departments=frozenset({"account_executives"}))},
    )

    assert crm_sync_job._eligible_emails() == ["ae@acme.example"]


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_the_sweep_skips_someone_narrowed_out_of_the_copilot(user_db, monkeypatch):
    _add_people(user_db, ("ae@acme.example", "Sales"), ("ops@acme.example", "Ops"))
    pin_rosters(
        monkeypatch, {"copilot": FeaturePolicy(departments=frozenset({"sales"}))}
    )

    assert crm_sync_job._eligible_emails() == ["ae@acme.example"]


def test_the_sweep_exits_zero_even_when_the_roster_lookup_explodes():
    """Cloud Run retries a non-zero exit; re-running a spent scan helps nobody."""
    with patch.object(crm_sync_job, "_run_all_users", side_effect=RuntimeError("boom")):
        assert crm_sync_job.main(["--all-users"]) == 0


def test_a_scan_failure_still_exits_zero():
    with patch(
        "rubik_core.crm_sync.service.run_scan", side_effect=RuntimeError("boom")
    ):
        assert crm_sync_job.main(["--run-id", "r1", "--user-email", "a@b.com"]) == 0
