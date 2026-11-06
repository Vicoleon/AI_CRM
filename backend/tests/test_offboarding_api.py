"""Offboard handoff — endpoint authz and the offboard action itself.

The gate matrix under test: admin runs the offboard; the record's successor
and an org-wide exec (``sees_all_departments``) read it and work its items; a
stranger gets nothing; departed-priorities reads land in the audit log.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import (
    Asset,
    AuditLog,
    Base,
    HomeCard,
    OffboardRecord,
    User,
)
from rubik_core.offboarding import store

DEPARTED = "leaver@acme.example"
SUCCESSOR = "manager@acme.example"
STRANGER = "stranger@acme.example"
ADMIN = "admin@acme.example"
CEO = "ceo@acme.example"

client = TestClient(app)


def _profile(email: str, *, is_admin=False, is_exec=False, managed=None) -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department="Sales",
        roles=["admin"] if is_admin else ["user"],
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at="2026-08-20T00:00:00",
        updated_at="2026-08-20T00:00:00",
    )


def _as(email: str, **kw) -> None:
    app.dependency_overrides[get_current_user] = lambda: _profile(email, **kw)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    app.dependency_overrides[get_db] = lambda: s
    try:
        yield s
    finally:
        app.dependency_overrides.clear()
        s.close()
        engine.dispose()


@pytest.fixture
def seeded(db):
    for email, name in ((DEPARTED, "John Doe"), (SUCCESSOR, "Jane Doe")):
        db.add(User(email=email, id=email.split("@")[0], name=name, department="Sales"))
    db.add(
        Asset(
            doc_id="doc-1",
            owner_email=DEPARTED,
            status="approved",
            department="Sales",
        )
    )
    db.add(
        HomeCard(
            id=str(uuid.uuid4()),
            user_email=DEPARTED,
            date="2026-08-21",
            category="daily_briefing",
            content_md="### AI Priorities — 2026-08-21",
            metadata_json={
                "schema_version": 2,
                "crm_provenance": {
                    "workspace": "test",
                    "unprovable": False,
                    "deal_ids": [],
                },
                "priorities": [],
            },
        )
    )
    db.commit()
    return db


def _offboard(db) -> str:
    """Run the offboard as admin (sweep skipped) and return the record id."""
    _as(ADMIN, is_admin=True)
    res = client.post(
        "/api/offboarding/offboard",
        json={"email": DEPARTED, "successor_email": SUCCESSOR, "run_sweep": False},
    )
    assert res.status_code == 200, res.text
    return res.json()["record"]["id"]


# ── The offboard action ──────────────────────────────────────────────────────


def test_offboard_requires_admin(seeded):
    _as(SUCCESSOR)
    res = client.post(
        "/api/offboarding/offboard",
        json={"email": DEPARTED, "successor_email": SUCCESSOR},
    )
    assert res.status_code == 403


def test_offboard_deletes_roster_row_and_reassigns_assets(seeded):
    rid = _offboard(seeded)
    seeded.expire_all()
    # Roster row gone (that's the org-wide "departed" signal)…
    assert seeded.get(User, DEPARTED) is None
    # …but the priorities history survives, still under the departed email.
    assert seeded.query(HomeCard).filter(HomeCard.user_email == DEPARTED).count() == 1
    # Docs went to the successor.
    assert seeded.get(Asset, "doc-1").owner_email == SUCCESSOR
    # And the action is in the audit log.
    audit = seeded.query(AuditLog).filter(AuditLog.action == "USER_OFFBOARD").all()
    assert len(audit) == 1 and audit[0].doc_id == DEPARTED  # target lands in doc_id
    rec = store.get_record(seeded, rid)
    assert rec.sweep_status == "skipped"


def test_held_sweep_refuses_before_offboard_writes(seeded, monkeypatch):
    monkeypatch.setenv("CUBIE_AUTOMATIONS_ENABLED", "false")
    _as(ADMIN, is_admin=True)
    response = client.post(
        "/api/offboarding/offboard",
        json={"email": DEPARTED, "successor_email": SUCCESSOR},
    )
    assert response.status_code == 503, response.text
    seeded.expire_all()
    assert seeded.get(User, DEPARTED) is not None
    assert seeded.get(Asset, "doc-1").owner_email == DEPARTED
    assert seeded.query(OffboardRecord).count() == 0
    assert (
        seeded.query(AuditLog).filter(AuditLog.action == "USER_OFFBOARD").count() == 0
    )


def test_held_resweep_keeps_existing_record_state(seeded, monkeypatch):
    record_id = _offboard(seeded)
    monkeypatch.setenv("CUBIE_AUTOMATIONS_ENABLED", "false")
    response = client.post(f"/api/offboarding/records/{record_id}/resweep")
    assert response.status_code == 503, response.text
    seeded.expire_all()
    record = seeded.get(OffboardRecord, record_id)
    assert record.sweep_status == "skipped"
    assert record.status == "closed"


def test_offboard_rejects_unknown_successor(seeded):
    _as(ADMIN, is_admin=True)
    res = client.post(
        "/api/offboarding/offboard",
        json={"email": DEPARTED, "successor_email": "ghost@acme.example"},
    )
    assert res.status_code == 400


# ── Reading + working the handoff ────────────────────────────────────────────


def test_records_visibility_matrix(seeded):
    rid = _offboard(seeded)

    _as(SUCCESSOR)
    mine = client.get("/api/offboarding/records").json()["records"]
    assert [r["id"] for r in mine] == [rid]
    assert client.get(f"/api/offboarding/records/{rid}").status_code == 200

    _as(STRANGER)
    assert client.get("/api/offboarding/records").json()["records"] == []
    assert client.get(f"/api/offboarding/records/{rid}").status_code == 403

    # Org-wide exec (the CEO shape: is_exec + managed "*") sees every record.
    _as(CEO, is_exec=True, managed=["*"])
    assert [
        r["id"] for r in client.get("/api/offboarding/records").json()["records"]
    ] == [rid]
    assert client.get(f"/api/offboarding/records/{rid}").status_code == 200

    # A scoped exec without the wildcard does not.
    _as(CEO, is_exec=True, managed=["Sales"])
    assert client.get(f"/api/offboarding/records/{rid}").status_code == 403


def test_item_status_gate_and_rollup(seeded):
    rid = _offboard(seeded)
    rec = store.get_record(seeded, rid)
    items = store.add_items(seeded, rec, [{"kind": "todo", "label": "a"}])
    seeded.commit()

    _as(STRANGER)
    res = client.post(
        f"/api/offboarding/items/{items[0].id}/status", json={"status": "done"}
    )
    assert res.status_code == 403

    _as(SUCCESSOR)
    res = client.post(
        f"/api/offboarding/items/{items[0].id}/status", json={"status": "done"}
    )
    assert res.status_code == 200
    assert res.json()["item"]["status"] == "done"
    assert res.json()["record_status"] == "closed"


def test_departed_priorities_view_is_gated_and_audited(seeded):
    _offboard(seeded)

    _as(STRANGER)
    res = client.get(f"/api/offboarding/departed/{DEPARTED}/priorities")
    assert res.status_code == 403

    _as(SUCCESSOR)
    res = client.get(f"/api/offboarding/departed/{DEPARTED}/priorities")
    assert res.status_code == 200
    cards = res.json()["cards"]
    assert len(cards) == 1 and cards[0]["date"] == "2026-08-21"

    seeded.expire_all()
    views = (
        seeded.query(AuditLog)
        .filter(AuditLog.action == "DEPARTED_PRIORITIES_VIEW")
        .all()
    )
    assert len(views) == 1
    assert views[0].actor_email == SUCCESSOR
    assert views[0].doc_id == DEPARTED  # record_audit stores target in doc_id


def test_departed_history_omits_entire_private_gmail_briefing(seeded):
    """A successor cannot read even the work-looking card from a mixed run."""
    card = seeded.query(HomeCard).filter(HomeCard.user_email == DEPARTED).one()
    card.metadata_json = {
        "schema_version": 2,
        "crm_provenance": {"workspace": "test", "unprovable": False, "deal_ids": []},
        "priorities": [
            {
                "id": "work-looking",
                "title": "Work looking title with private context",
                "entity_kind": "thread",
                "source_channels": ["gmail"],
                "attrs": {"private_gmail_context": True},
            },
            {
                "id": "personal",
                "title": "Private mailbox secret",
                "entity_kind": "thread",
                "source_channels": ["gmail"],
                "attrs": {"personal_gmail": True},
            },
        ],
    }
    seeded.add(
        HomeCard(
            id=str(uuid.uuid4()),
            user_email=DEPARTED,
            date="2026-08-20",
            category="daily_briefing",
            content_md="Old stored text must never be returned",
            metadata_json={
                "schema_version": 2,
                "crm_provenance": {
                    "workspace": "test",
                    "unprovable": False,
                    "deal_ids": [],
                },
                "priorities": [
                    {
                        "id": "public",
                        "title": "Public work priority",
                        "entity_kind": "thread",
                        "source_channels": ["gmail"],
                    }
                ],
            },
        )
    )
    seeded.commit()
    _offboard(seeded)

    for addr, kwargs in (
        (SUCCESSOR, {}),
        (CEO, {"is_exec": True, "managed": ["*"]}),
        (ADMIN, {"is_admin": True}),
    ):
        _as(addr, **kwargs)
        response = client.get(
            f"/api/offboarding/departed/{DEPARTED}/priorities?limit=1"
        )
        assert response.status_code == 200, response.text
        assert [item["date"] for item in response.json()["cards"]] == ["2026-08-20"]
        assert "Public work priority" in response.text
        assert "Private mailbox secret" not in response.text
        assert "Work looking title with private context" not in response.text


def test_departed_priorities_redacts_eos_from_mixed_history_when_module_off(
    seeded, monkeypatch
):
    from rubik_core.control import repository

    _offboard(seeded)
    card = seeded.query(HomeCard).filter(HomeCard.user_email == DEPARTED).one()
    card.content_md = "Secret Rock from EOS and a core follow-up"
    card.metadata_json = {
        "schema_version": 2,
        "crm_provenance": {"workspace": "test", "unprovable": False, "deal_ids": []},
        "priorities": [
            {
                "title": "Secret Rock from EOS",
                "entity_kind": "rock",
                "priority_kind": "eos_rock_attention",
            },
            {
                "title": "Send the proposal",
                "entity_kind": "thread",
                "priority_kind": "follow_up_debt",
            },
            {"title": "Secret Rock from EOS in legacy partial metadata"},
        ],
    }
    seeded.commit()
    monkeypatch.setattr(repository, "module_enabled", lambda *_a, **_k: False)
    _as(SUCCESSOR)
    response = client.get(f"/api/offboarding/departed/{DEPARTED}/priorities")
    assert response.status_code == 200, response.text
    assert [card["content_md"] for card in response.json()["cards"]] == [
        "- Send the proposal"
    ]
    assert "Secret Rock" not in response.text

    card.metadata_json = {
        "schema_version": 2,
        "crm_provenance": {"workspace": "test", "unprovable": False, "deal_ids": []},
        "priorities": [{"title": "Secret Rock from EOS in legacy partial metadata"}],
    }
    seeded.commit()
    response = client.get(f"/api/offboarding/departed/{DEPARTED}/priorities")
    assert response.status_code == 200, response.text
    assert response.json()["cards"] == []
    assert "Secret Rock" not in response.text


def test_preexisting_handoff_deal_is_hidden_after_pipeline_revocation(
    seeded, monkeypatch
):
    from backend.routers import agents, home

    rid = _offboard(seeded)
    rec = store.get_record(seeded, rid)
    item = store.add_items(
        seeded,
        rec,
        [
            {
                "kind": "deal",
                "label": "Private foreign account",
                "detail": "Sensitive deal terms",
                "entity_kind": "deal",
                "entity_id": "deal-1",
            }
        ],
    )[0]
    rec.dossier_md = "Private foreign account has sensitive deal terms"
    seeded.commit()

    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda ids: set(ids))
    _as(SUCCESSOR)
    before = client.get(f"/api/offboarding/records/{rid}")
    assert before.status_code == 200
    assert "Private foreign account" in before.text

    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda _ids: set())
    after = client.get(f"/api/offboarding/records/{rid}")
    assert after.status_code == 200
    assert after.json()["record"]["items"] == []
    assert "Private foreign account" not in after.text
    assert "Sensitive deal terms" not in after.text
    assert store.open_items_for_successor(seeded, SUCCESSOR) == []
    assert (
        client.post(
            f"/api/offboarding/items/{item.id}/status", json={"status": "done"}
        ).status_code
        == 404
    )

    inherited = home._bundle_dict_from_metadata(
        {
            "schema_version": 2,
            "crm_provenance": {
                "workspace": "test",
                "unprovable": False,
                "deal_ids": [],
            },
            "priorities": [
                {
                    "id": "inherited-card",
                    "title": "Private foreign account",
                    "source_channels": ["inherited"],
                    "entity_kind": "offboard_item",
                    "entity_id": item.id,
                }
            ],
        }
    )
    assert inherited["priorities"] == []
    assert inherited["headline"] == ""


def test_bare_delete_409s_for_users_with_history(seeded):
    _as(ADMIN, is_admin=True)
    res = client.delete(f"/api/users/{DEPARTED}")
    assert res.status_code == 409
    assert "offboard" in res.json()["detail"].lower()
    # Nothing changed.
    assert seeded.get(User, DEPARTED) is not None
