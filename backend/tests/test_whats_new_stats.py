"""What's New opens: people, never who (``rubik_core/whats_new/stats.py``).

Pinned here:

* a person counts once per release note per source, by an ``ON CONFLICT DO
  NOTHING`` insert (safe on pg8000, where a duplicate is no ``IntegrityError``);
* no email is ever stored: an open is a keyed hash under a key this feature
  generates once and keeps, and the same person is not linkable across notes;
* the impersonation placeholder and addresses off the Workspace domain are
  never counted;
* the module speaks of opens: the click-era names left with the approval flow.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.database.models import WorkspaceSetting
from rubik_core.database.whats_new import WhatsNewOpen
from rubik_core.whats_new import stats

ANA = "ana@acme.example"


@pytest.fixture()
def db(monkeypatch, set_test_workspace_domains):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    set_test_workspace_domains("acme.example")
    from rubik_core.company.operations import OperationsConfig

    monkeypatch.setattr(
        "rubik_core.insights.population.get_operations_config", OperationsConfig
    )
    stats.reset_caches()
    session = Session()
    yield session
    session.close()
    stats.reset_caches()


def _open(db, entry_id, email, department="Sales", source="try_it"):
    return stats.record_open(
        db, entry_id, email=email, department=department, source=source
    )


def test_the_same_person_counts_once_per_note_per_source(db):
    assert _open(db, "rel-1", ANA) is True
    assert _open(db, "rel-1", "ANA@acme.example ") is False
    assert _open(db, "rel-1", ANA, source="slack") is True
    assert _open(db, "rel-2", ANA) is True

    assert db.query(WhatsNewOpen).count() == 3


def test_no_email_is_ever_stored(db):
    _open(db, "rel-1", ANA, department="Sales Team")

    (row,) = db.query(WhatsNewOpen).all()
    stored = " ".join(
        str(v) for v in (row.entry_id, row.viewer_key, row.department, row.source)
    )
    assert "ana" not in stored and "@" not in stored
    assert len(row.viewer_key) == 64
    assert row.department == "sales_team"
    assert row.entry_id == "rel-1"


def test_the_key_is_per_note_and_stable_across_restarts(db):
    a1 = stats.viewer_key(db, "rel-1", ANA)
    a2 = stats.viewer_key(db, "rel-2", ANA)

    stats.reset_caches()  # a new process reads the stored key back
    again = stats.viewer_key(db, "rel-1", "Ana@Acme.example")

    assert a1 != a2, "the same person is not linkable across notes"
    assert a1 == again
    row = db.get(WorkspaceSetting, stats.OPEN_KEY_ROW)
    assert isinstance(row.value["secret"], str) and len(row.value["secret"]) >= 32


def test_two_instances_minting_the_key_together_both_use_the_winner(db, monkeypatch):
    """Another instance commits its key between our read and our write: ours
    must never overwrite it (an overwrite would leave the two processes on
    different keys, and one person counting twice). The open still counts,
    under the key the other instance stored."""
    from rubik_core.database import db as database

    theirs = "f" * 64
    real_get = db.get
    raced = []

    def lose_the_race(model, key, *args, **kwargs):
        if key == stats.OPEN_KEY_ROW and not raced:
            raced.append(True)
            other = database.SessionLocal()
            other.add(
                WorkspaceSetting(key=stats.OPEN_KEY_ROW, value={"secret": theirs})
            )
            other.commit()
            other.close()
            return None  # what our read saw, just before theirs landed
        return real_get(model, key, *args, **kwargs)

    monkeypatch.setattr(db, "get", lose_the_race)

    assert _open(db, "rel-1", ANA) is True
    assert raced, "the race was not exercised"
    assert stats._secret(db) == theirs.encode()
    check = database.SessionLocal()
    try:
        assert check.get(WorkspaceSetting, stats.OPEN_KEY_ROW).value == {
            "secret": theirs
        }
    finally:
        check.close()


@pytest.mark.parametrize(
    "email",
    ["", "impersonated_user@systems.local", "someone@outside.example", "  "],
)
def test_placeholders_and_outside_addresses_are_never_counted(db, email):
    assert _open(db, "rel-1", email) is False
    assert db.query(WhatsNewOpen).count() == 0


def test_an_unknown_source_is_refused(db):
    with pytest.raises(ValueError):
        _open(db, "rel-1", ANA, source="email")


def test_the_module_speaks_of_opens_not_clicks():
    """A release note is opened, from Slack or from "Try it". The click-era
    names (``record_click``, ``CLICK_KEY_ROW``) went with the approval flow."""
    leftovers = sorted(name for name in vars(stats) if "click" in name.lower())
    assert leftovers == []
    assert callable(stats.record_open)
    assert isinstance(stats.OPEN_KEY_ROW, str) and stats.OPEN_KEY_ROW
