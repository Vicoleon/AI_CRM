"""Who a release note reaches (``rubik_core/whats_new/audience.py``).

Spec §4.6.1: workspaces see the changelog filtered to the features they have
switched on. Pinned here: a note with no feature is for everyone; a note about
a feature reaches only the people Feature Access lets use it, and nobody while
the feature's kill switch is off; a key the registry no longer knows hides the
note and never raises (Review Focus 2); a failing check hides it too.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.whats_new import audience
from rubik_core.whats_new.changelog import ChangelogEntry


@pytest.fixture(autouse=True)
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    monkeypatch.setattr("rubik_core.config.settings.SCHEDULED_TASKS_ENABLED", True)
    from rubik_core.features.policy import clear_cache

    clear_cache()
    yield Session
    clear_cache()


def _entry(feature_key=None) -> ChangelogEntry:
    return ChangelogEntry(
        id="rel-1",
        title="Something new",
        body="It does a new thing.",
        feature_key=feature_key,
        path=None,
        published_at=datetime(2026, 10, 27, 12, 0),
    )


def _person(department):
    return SimpleNamespace(
        email="p@acme.example",
        department=department,
        managed_departments=[],
        is_exec=False,
        is_admin=False,
        roles=["user"],
    )


def _limit_feature_to(session_factory, key, department):
    from rubik_core.database.models import FeatureAccess
    from rubik_core.features.policy import clear_cache

    with session_factory() as db:
        db.add(
            FeatureAccess(
                feature_key=key,
                departments=[department],
                extra_emails=[],
                exec_allowed=False,
                admin_allowed=False,
                everyone=False,
            )
        )
        db.commit()
    clear_cache()


def test_a_note_without_a_feature_is_for_everyone():
    assert audience.can_see(_entry(), _person("HR")) is True
    assert audience.feature_switched_on(None) is True
    assert audience.feature_label(None) is None


def test_a_note_about_a_feature_reaches_only_its_users(db):
    _limit_feature_to(db, "scheduled_tasks", "sales")
    entry = _entry("scheduled_tasks")

    assert audience.can_see(entry, _person("Sales")) is True
    assert audience.can_see(entry, _person("HR")) is False
    assert audience.feature_label("scheduled_tasks") == "Scheduled tasks"


def test_a_feature_switched_off_hides_its_notes(db, monkeypatch):
    _limit_feature_to(db, "scheduled_tasks", "sales")
    monkeypatch.setattr("rubik_core.config.settings.SCHEDULED_TASKS_ENABLED", False)

    assert audience.feature_switched_on("scheduled_tasks") is False
    assert audience.can_see(_entry("scheduled_tasks"), _person("Sales")) is False


@pytest.mark.parametrize("key", ["help_desk", "system_kb", "retired_feature"])
def test_a_feature_that_left_the_registry_hides_its_notes(key):
    assert audience.feature_switched_on(key) is False
    assert audience.feature_label(key) is None
    assert audience.can_see(_entry(key), _person("Systems")) is False


def test_a_failing_check_hides_the_note(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("policy store away")

    monkeypatch.setattr(audience, "feature_allows", boom)
    assert audience.can_see(_entry("scheduled_tasks"), _person("Sales")) is False
