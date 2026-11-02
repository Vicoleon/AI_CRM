"""Personal Gmail context does not enter shared priority debug or telemetry."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import admin_required
from backend.main import app
from rubik_core.database.db import get_db
from rubik_core.database.insights import PriorityEvent
from rubik_core.database.models import AuditLog, Base, HomeCard, PriorityMemory, User
from rubik_core.insights.filters import Window
from rubik_core.insights.signals import load_first_last, load_signals
from rubik_core.priorities.privacy import (
    is_private_gmail_bundle,
    public_priority_ids,
)

OWNER = "owner@acme.example"


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(id="owner", email=OWNER, name="Owner", department="Sales"))
    session.commit()
    monkeypatch.setattr(
        "rubik_core.tenancy.workspace_time.workspace_time_zone_name", lambda: "UTC"
    )
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def _card(db, card_id: str, priority_id: str, *, private: bool):
    metadata = {
        "priorities": [
            {
                "id": priority_id,
                "title": "Personal source" if private else "Public work",
                "attrs": {"private_gmail_context": True} if private else {},
            }
        ]
    }
    db.add(
        HomeCard(
            id=card_id,
            user_email=OWNER,
            date="2026-10-30",
            category="daily_briefing",
            content_md="stored text",
            metadata_json=metadata,
        )
    )
    db.commit()


def test_public_ids_exclude_entire_mixed_private_run_and_duplicate(db):
    _card(db, "public", "work", private=False)
    _card(db, "private", "personal", private=True)
    _card(db, "duplicate", "work", private=True)
    _card(db, "public-maybe", "maybe", private=False)
    db.add(
        HomeCard(
            id="malformed-duplicate",
            user_email=OWNER,
            date="2026-10-30",
            category="next_week_report",
            content_md="stored text",
            metadata_json={"priorities": [{"id": "maybe"}, None]},
        )
    )
    db.commit()
    assert is_private_gmail_bundle(
        {"priorities": [{"attrs": {"private_gmail_context": True}}]}
    )
    assert is_private_gmail_bundle(None)
    assert is_private_gmail_bundle({"priorities": "not a card list"})
    assert is_private_gmail_bundle({"priorities": [{"attrs": None}]})
    assert not is_private_gmail_bundle({"priorities": [{"id": "legacy"}]})
    assert public_priority_ids(db, OWNER) == set()


def test_admin_priority_memory_hides_private_and_unknown_rows(db, monkeypatch):
    from rubik_core.priorities import memory

    _card(db, "public", "work", private=False)
    _card(db, "private", "personal", private=True)

    def memory_row(priority_id: str | None):
        return SimpleNamespace(
            last_priority_id=priority_id,
            to_dict=lambda: {"last_priority_id": priority_id, "entity_id": "secret"},
        )

    monkeypatch.setattr(
        memory,
        "load_all",
        lambda _email: [
            memory_row("work"),
            memory_row("personal"),
            memory_row("unknown"),
            memory_row(None),
        ],
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[admin_required] = lambda: SimpleNamespace(
        email="admin@acme.example"
    )
    response = TestClient(app).get(
        "/api/admin/priorities/memory", params={"user_email": OWNER}
    )
    assert response.status_code == 200, response.text
    assert [row["last_priority_id"] for row in response.json()] == ["work"]


def test_shared_usage_omits_private_priority_actions(db):
    _card(db, "public", "work", private=False)
    _card(db, "private", "personal", private=True)
    at = datetime(2026, 10, 30, 12)
    for index, priority_id in enumerate(("work", "personal", "unknown")):
        db.add(
            PriorityMemory(
                id=f"memory-{index}",
                user_email=OWNER,
                entity_kind="thread",
                entity_id=f"thread-{index}",
                priority_kind="follow_up_debt",
                state="dismissed",
                last_priority_id=priority_id,
                last_state_change_by=OWNER,
                last_state_change_at=at,
            )
        )
        db.add(
            PriorityEvent(
                user_email=OWNER,
                priority_id=priority_id,
                kind="dismissed",
                actor=OWNER,
                at=at,
            )
        )
    db.commit()

    signals = load_signals(db, (OWNER,), Window(date(2026, 10, 30), date(2026, 10, 30)))
    assert [signal.source for signal in signals].count("priority_memory") == 1
    assert [signal.source for signal in signals].count("priority_events") == 1

    db.query(HomeCard).filter(HomeCard.id == "public").delete()
    db.query(PriorityMemory).filter(PriorityMemory.last_priority_id == "work").delete()
    db.query(PriorityEvent).filter(PriorityEvent.priority_id == "work").delete()
    db.commit()
    assert OWNER not in load_first_last(db, (OWNER,))


def test_shared_usage_omits_personal_gmail_audit_actions(db):
    at = datetime(2026, 10, 30, 12)
    for action, details in (
        ("GMAIL_ACCOUNT_CONNECTED", {"mailbox": "owner@gmail.example"}),
        ("VIEW_EMAIL_INBOX", {"account_id": "personal-account"}),
        ("CREATE_EMAIL_DRAFT", {"account_id": "personal-account"}),
        ("VIEW_EMAIL_INBOX", {}),
    ):
        db.add(
            AuditLog(
                actor_email=OWNER,
                timestamp=at,
                action=action,
                status="success",
                details=details,
            )
        )
    db.commit()
    signals = load_signals(db, (OWNER,), Window(date(2026, 10, 30), date(2026, 10, 30)))
    assert [signal.source for signal in signals].count("audit") == 1


def test_personal_gmail_audits_do_not_make_owner_active_in_shared_insights(db):
    at = datetime(2026, 10, 30, 12)
    for action, details in (
        ("GMAIL_ACCOUNT_CONNECTED", {"mailbox": "owner@gmail.example"}),
        ("VIEW_EMAIL_INBOX", {"account_id": "personal-account"}),
        ("CREATE_EMAIL_DRAFT", {"account_id": "personal-account"}),
    ):
        db.add(
            AuditLog(
                actor_email=OWNER,
                timestamp=at,
                action=action,
                status="success",
                details=details,
            )
        )
    db.commit()
    assert (
        load_signals(db, (OWNER,), Window(date(2026, 10, 30), date(2026, 10, 30))) == []
    )
    assert OWNER not in load_first_last(db, (OWNER,))


def test_admin_audit_redacts_historical_personal_mailbox_details(db):
    from backend.routers.admin import list_audit_logs

    db.add_all(
        [
            AuditLog(
                actor_email=OWNER,
                timestamp=datetime(2026, 10, 30, 12),
                action="GMAIL_ACCOUNT_CONNECTED",
                status="success",
                details={
                    "account_id": "personal-account",
                    "mailbox": "owner@gmail.example",
                },
            ),
            AuditLog(
                actor_email=OWNER,
                timestamp=datetime(2026, 10, 30, 13),
                action="VIEW_EMAIL_INBOX",
                status="success",
                details={"account_id": "personal-account"},
            ),
            AuditLog(
                actor_email=OWNER,
                timestamp=datetime(2026, 10, 30, 14),
                action="USER_UPDATE",
                status="success",
                details={"field": "name"},
            ),
            AuditLog(
                actor_email=OWNER,
                timestamp=datetime(2026, 10, 30, 15),
                action="gmail_account_connected",
                status="success",
                details={"mailbox": "older@gmail.example"},
            ),
        ]
    )
    db.commit()
    rows = asyncio.run(
        list_audit_logs(
            limit=20,
            offset=0,
            actor=None,
            action=None,
            user=SimpleNamespace(is_admin=True),
            db=db,
        )
    )
    assert [row.details for row in rows] == [
        None,
        {"field": "name"},
        None,
        None,
    ]
