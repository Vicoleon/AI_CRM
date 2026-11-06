"""UC-46 — how a re-scan behaves against suggestions it has already made.

The nightly window deliberately overlaps the on-demand one, so the same call is
scanned more than once. What must never happen is the AE seeing the same
proposal twice, or a change they already applied coming back.

Covers:
  - already pending / applied / failed → skipped
  - recently dismissed → skipped; long-dismissed → offered again
  - superseded and expired never suppress
  - a new value for the same field supersedes the old card
  - the unique constraint absorbs a concurrent insert
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.config import settings
from rubik_core.crm_sync import service
from rubik_core.crm_sync.validators import ValidatedSuggestion, build_keys
from rubik_core.database.models import Base, CrmSyncSuggestion

AE = "ae@acme.example"


class _Record:
    doc_id = "doc-1"
    meeting_id = "fathom-1"
    original_filename = "Acme call"
    created_at = datetime(2026, 8, 1)


@pytest.fixture
def Session(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(settings, "CRM_SYNC_DISMISS_SUPPRESS_DAYS", 30)
    return factory


def _item(after="555-0143", field="phone") -> ValidatedSuggestion:
    """A suggestion whose keys come from the REAL build_keys().

    Hand-writing a plausible-looking target_key here is what let an earlier
    version of this file pass while the collision it claimed to cover was live.
    """
    properties = {field: {"before": "555-0100", "after": after}}
    target_key, dedupe_key = build_keys(
        "update_contact",
        hubspot_object_id="c1",
        contact_email=None,
        properties=properties,
    )
    return ValidatedSuggestion(
        change_type="update_contact",
        hubspot_object_type="contact",
        hubspot_object_id="c1",
        properties=properties,
        evidence={"quote": "My direct line", "doc_id": "doc-1"},
        confidence=0.9,
        rationale="new number",
        target_key=target_key,
        dedupe_key=dedupe_key,
    )


def _existing(db, *, status, dedupe_key, target=None, age_days=0):
    stamp = datetime.utcnow() - timedelta(days=age_days)
    db.add(
        CrmSyncSuggestion(
            id=f"old-{status}-{dedupe_key}",
            user_email=AE,
            run_id="run-0",
            change_type="update_contact",
            hubspot_object_type="contact",
            hubspot_object_id="c1",
            target_key=target or dedupe_key.rsplit(":", 1)[0],
            dedupe_key=dedupe_key,
            payload={"properties": {}},
            status=status,
            created_at=stamp,
            updated_at=stamp,
        )
    )
    db.commit()


def _persist(item) -> tuple[bool, int]:
    """Returns (queued, superseded_count)."""
    return service._persist("run-1", AE, _Record(), item, False)


@pytest.mark.parametrize("status", ["pending", "applying", "applied", "failed"])
def test_an_identical_suggestion_is_skipped_while_live_or_done(Session, status):
    item = _item()
    with Session() as db:
        _existing(db, status=status, dedupe_key=item.dedupe_key)

    assert _persist(item) == (False, 0)

    with Session() as db:
        assert db.query(CrmSyncSuggestion).count() == 1


def test_a_recently_dismissed_suggestion_is_not_raised_again(Session):
    item = _item()
    with Session() as db:
        _existing(db, status="dismissed", dedupe_key=item.dedupe_key, age_days=5)

    assert _persist(item) == (False, 0)


def test_a_long_dismissed_suggestion_is_offered_again_with_fresh_evidence(Session):
    item = _item()
    with Session() as db:
        _existing(db, status="dismissed", dedupe_key=item.dedupe_key, age_days=31)

    assert _persist(item)[0] is True

    with Session() as db:
        rows = db.query(CrmSyncSuggestion).all()
    # Re-offered on the SAME row — one row per exact change, history intact.
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].run_id == "run-1"
    assert rows[0].evidence["quote"] == "My direct line"


@pytest.mark.parametrize("status", ["superseded", "expired"])
def test_superseded_and_expired_rows_never_suppress(Session, status):
    item = _item()
    with Session() as db:
        _existing(db, status=status, dedupe_key=item.dedupe_key)

    assert _persist(item)[0] is True

    with Session() as db:
        rows = db.query(CrmSyncSuggestion).all()
    assert len(rows) == 1
    assert rows[0].status == "pending"


def test_a_newer_value_for_the_same_field_supersedes_the_old_card(Session):
    first = _item(after="555-0143")
    with Session() as db:
        _existing(db, status="pending", dedupe_key=first.dedupe_key)

    queued, superseded = _persist(_item(after="555-0199"))

    assert (queued, superseded) == (True, 1)
    with Session() as db:
        rows = {r.dedupe_key: r.status for r in db.query(CrmSyncSuggestion).all()}
    # Exactly one live card for this field, showing the newer value.
    assert sorted(rows.values()) == ["pending", "superseded"]
    assert rows[first.dedupe_key] == "superseded"


def test_a_different_field_on_the_same_record_is_not_superseded(Session):
    """Two real changes to one record must both survive for the AE to approve.

    Keys come from build_keys() on both sides, so this actually exercises the
    collision rather than a hand-written string that the real code never emits.
    """
    assert _persist(_item(field="phone"))[0] is True
    queued, superseded = _persist(_item(field="jobtitle", after="VP Compliance"))

    assert (queued, superseded) == (True, 0)
    with Session() as db:
        live = db.query(CrmSyncSuggestion).filter_by(status="pending").count()
    assert live == 2


def test_a_new_value_for_the_same_field_still_supersedes(Session):
    """The other direction: one live card per field, not per record."""
    assert _persist(_item(field="phone", after="555-0143"))[0] is True
    queued, superseded = _persist(_item(field="phone", after="555-0199"))

    assert (queued, superseded) == (True, 1)
    with Session() as db:
        statuses = sorted(r.status for r in db.query(CrmSyncSuggestion).all())
    assert statuses == ["pending", "superseded"]


def test_a_concurrent_insert_is_absorbed_by_the_unique_constraint(Session, monkeypatch):
    """Two overlapping scans racing on the same change must not both land.

    Simulates the race by blinding the pre-check, so the only thing standing
    between the two inserts is the DB constraint itself.
    """
    item = _item()
    assert _persist(item)[0] is True

    # Blind the pre-check, so the only thing between the second insert and a
    # duplicate row is the DB constraint itself.
    monkeypatch.setattr(service, "_lookup_existing", lambda *_a: None)
    assert _persist(item) == (False, 0)  # IntegrityError, not a crash

    with Session() as db:
        assert db.query(CrmSyncSuggestion).count() == 1


def test_expiring_stale_pendings_leaves_recent_ones_alone(Session, monkeypatch):
    monkeypatch.setattr(settings, "CRM_SYNC_PENDING_EXPIRE_DAYS", 14)
    with Session() as db:
        _existing(db, status="pending", dedupe_key="old", age_days=20)
        _existing(db, status="pending", dedupe_key="new", age_days=2)

    assert service._expire_stale_pending(AE) == 1

    with Session() as db:
        rows = {r.dedupe_key: r.status for r in db.query(CrmSyncSuggestion).all()}
    assert rows == {"old": "expired", "new": "pending"}
