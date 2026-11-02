"""Offboard handoff — store semantics and the exit sweep.

The sweep's Google last pass and the LLM synthesis are stubbed (this suite
runs with no service account and no Vertex); what's under test is everything
deterministic: record snapshots, the cached-history boundary, item
persistence + dedup, the degraded-mode dossier note, and status roll-up.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.database.models import Base, HomeCard, OffboardItem, User
from rubik_core.offboarding import store, sweep

DEPARTED = "leaver@acme.example"
SUCCESSOR = "manager@acme.example"
ADMIN = "admin@acme.example"


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # Modules under test open their own sessions via deferred imports of
    # SessionLocal — patch by string target so those late imports see the
    # test engine.
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_users(db):
    for email, name, dept in (
        (DEPARTED, "John Doe", "Sales"),
        (SUCCESSOR, "Jane Doe", "Sales"),
    ):
        db.add(
            User(
                email=email,
                id=email.split("@")[0],
                name=name,
                department=dept,
            )
        )
    db.commit()


def _seed_history_card(db, *, kinds=("commitment_debt",)):
    priorities = [
        {
            "title": f"Send pricing options ({k})",
            "why_it_matters": "Committed to send pricing early next week.",
            "urgency": "high",
            "priority_kind": k,
            "entity_kind": "thread",
            "entity_id": f"thread-{k}",
            "source_links": ["https://mail.google.com/x"],
        }
        for k in kinds
    ] + [
        {
            "title": "Prep for a meeting (should NOT be inherited)",
            "priority_kind": "upcoming_prep",
            "entity_kind": "event",
            "entity_id": "ev-1",
        }
    ]
    db.add(
        HomeCard(
            id=str(uuid.uuid4()),
            user_email=DEPARTED,
            date=(date.today() - timedelta(days=2)).isoformat(),
            category="daily_briefing",
            content_md="x",
            metadata_json={"priorities": priorities},
        )
    )
    db.commit()


# ── Store ─────────────────────────────────────────────────────────────────────


def test_create_record_snapshots_the_departing_user(db):
    _seed_users(db)
    rec = store.create_record(
        db, user_email=DEPARTED, successor_email=SUCCESSOR, created_by=ADMIN
    )
    db.commit()
    assert rec.user_name == "John Doe"
    assert rec.department == "Sales"
    assert rec.successor_email == SUCCESSOR
    assert rec.sweep_status == "pending"


def test_eos_off_hides_inherited_eos_items_and_chart_successor(db, monkeypatch):
    from backend.routers.offboarding import _record_out
    from rubik_core.control import repository

    _seed_users(db)
    record = store.create_record(
        db, user_email=DEPARTED, successor_email=SUCCESSOR, created_by=ADMIN
    )
    store.add_items(
        db,
        record,
        [
            {"kind": "todo", "label": "EOS team task", "entity_kind": "eos_todo"},
            {
                "kind": "google_signal_commitment",
                "label": "Send the proposal",
                "entity_kind": "thread",
                "entity_id": "google-thread-1",
            },
        ],
    )
    db.commit()
    monkeypatch.setattr(repository, "module_enabled", lambda *_a, **_k: False)
    assert store.suggest_successor(db, DEPARTED) is None
    assert sweep._inventory_eos(db, DEPARTED, []) == []
    assert [
        item.label for item, _ in store.open_items_for_successor(db, SUCCESSOR)
    ] == ["Send the proposal"]
    visible = _record_out(db, record, with_items=True)
    assert visible["open_items"] == visible["total_items"] == 1
    assert [item["label"] for item in visible["items"]] == ["Send the proposal"]
    assert "EOS team task" not in visible["dossier_md"]


def test_create_record_rejects_bad_inputs(db):
    _seed_users(db)
    with pytest.raises(ValueError):
        store.create_record(
            db, user_email=DEPARTED, successor_email=DEPARTED, created_by=ADMIN
        )
    with pytest.raises(ValueError):
        store.create_record(
            db,
            user_email="ghost@acme.example",
            successor_email=SUCCESSOR,
            created_by=ADMIN,
        )
    with pytest.raises(ValueError):
        store.create_record(
            db,
            user_email=DEPARTED,
            successor_email="ghost@acme.example",
            created_by=ADMIN,
        )


def test_item_status_rolls_the_record_up(db):
    _seed_users(db)
    rec = store.create_record(
        db, user_email=DEPARTED, successor_email=SUCCESSOR, created_by=ADMIN
    )
    items = store.add_items(
        db,
        rec,
        [
            {"kind": "todo", "label": "a"},
            {"kind": "todo", "label": "b"},
        ],
    )
    db.commit()
    store.set_item_status(db, items[0].id, "done", actor=SUCCESSOR)
    assert rec.status == "active"  # one still open
    store.set_item_status(db, items[1].id, "dismissed", actor=SUCCESSOR)
    assert rec.status == "closed"
    store.set_item_status(db, items[1].id, "open", actor=SUCCESSOR)
    assert rec.status == "active"


def test_writeback_only_matches_the_records_successor(db):
    _seed_users(db)
    rec = store.create_record(
        db, user_email=DEPARTED, successor_email=SUCCESSOR, created_by=ADMIN
    )
    items = store.add_items(db, rec, [{"kind": "todo", "label": "a"}])
    db.commit()
    # A stranger resolving the same entity ids changes nothing.
    changed = store.resolve_items_by_entity(
        db,
        successor_email="stranger@acme.example",
        entity_ids=[items[0].id],
        status="done",
        actor="stranger@acme.example",
    )
    assert changed == 0
    assert db.get(OffboardItem, items[0].id).status == "open"
    changed = store.resolve_items_by_entity(
        db,
        successor_email=SUCCESSOR,
        entity_ids=[items[0].id],
        status="done",
        actor=SUCCESSOR,
    )
    assert changed == 1
    assert db.get(OffboardItem, items[0].id).status == "done"


# ── Sweep ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def stubbed_sweep(monkeypatch):
    """No Google, no Vertex: last pass reports 'account not readable'."""
    monkeypatch.setattr(sweep, "_google_last_pass", lambda departed, audit: ([], False))
    monkeypatch.setattr(
        "rubik_core.priorities.agents.base.LLMJsonClient.invoke_json",
        lambda self, prompt, fallback=None, attempts=1: (_ for _ in ()).throw(
            RuntimeError("no LLM in tests")
        ),
    )


def test_sweep_skips_untrusted_history_and_marks_degraded(db, stubbed_sweep):
    _seed_users(db)
    _seed_history_card(db, kinds=("commitment_debt", "pre_touchpoint_followup"))
    rec = store.create_record(
        db, user_email=DEPARTED, successor_email=SUCCESSOR, created_by=ADMIN
    )
    db.commit()
    rid = rec.id

    sweep.run_offboard_sweep(rid)

    db.expire_all()
    rec = store.get_record(db, rid)
    assert rec.sweep_status == "degraded"
    assert rec.items == []
    assert any("origin unprovable" in line for line in rec.sweep_detail)
    assert "was not readable" in rec.dossier_md
    assert rec.status == "closed"


def test_forged_gmail_tagged_crm_history_never_becomes_inherited_work(
    db, stubbed_sweep, monkeypatch
):
    from backend.routers import agents

    _seed_users(db)
    record = store.create_record(
        db, user_email=DEPARTED, successor_email=SUCCESSOR, created_by=ADMIN
    )
    secret = "Private foreign deal terms"
    priority = {
        "title": secret,
        "why_it_matters": "Quoted pipeline value",
        "priority_kind": "commitment_debt",
        "entity_kind": "thread",
        "entity_id": "gmail-thread-1",
        "source_channels": ["gmail"],
        "source_links": ["https://mail.google.com/mail/u/0/#inbox/gmail-thread-1"],
    }
    db.add(
        HomeCard(
            id=str(uuid.uuid4()),
            user_email=DEPARTED,
            date=date.today().isoformat(),
            category="daily_briefing",
            content_md=secret,
            metadata_json={"priorities": [priority]},
        )
    )
    legacy = store.add_items(
        db,
        record,
        [
            {
                "kind": "commitment",
                "label": priority["title"],
                "detail": priority["why_it_matters"],
                "entity_kind": priority["entity_kind"],
                "entity_id": priority["entity_id"],
                "source_links": priority["source_links"],
            }
        ],
    )[0]
    db.commit()
    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda ids: set(ids))
    monkeypatch.setattr(sweep, "_inventory_eos", lambda *_: [])
    monkeypatch.setattr(sweep, "_inventory_hubspot", lambda *_: [])
    assert legacy not in store.visible_items_for_record(db, record)
    assert store.open_items_for_successor(db, SUCCESSOR) == []
    assert (
        store.resolve_items_by_entity(
            db,
            successor_email=SUCCESSOR,
            entity_ids=[legacy.id],
            status="done",
            actor=SUCCESSOR,
        )
        == 0
    )
    sweep.run_offboard_sweep(record.id)
    db.expire_all()
    assert [item.id for item in store.get_record(db, record.id).items] == [legacy.id]


def test_google_commitments_are_bound_to_google_signal_not_crm_inventory(monkeypatch):
    from rubik_core.priorities.agents import base

    prompts = []
    secret = "FOREIGN_CRM_PIPELINE_SECRET"

    class Client:
        def __init__(self, **_kwargs):
            pass

        def invoke_json(self, prompt, **_kwargs):
            prompts.append(prompt)
            if len(prompts) == 1:
                return {
                    "narrative_md": "A handoff is needed.",
                    "extra_items": [{"label": secret}],
                }
            return {
                "selected": [
                    {
                        "signal_index": 0,
                        "label": secret,
                        "detail": secret,
                        "source_links": ["https://app.hubspot.com/secret"],
                        "urgency": "high",
                    },
                    {"signal_index": 99},
                ]
            }

    monkeypatch.setattr(base, "LLMJsonClient", Client)
    monkeypatch.setattr(
        "rubik_core.company.profile.company_possessive", lambda *_: "their company"
    )
    monkeypatch.setattr("rubik_core.company.profile.get_company_profile", lambda: None)
    record = SimpleNamespace(
        user_name="John Doe",
        user_email=DEPARTED,
        department="Sales",
        successor_email=SUCCESSOR,
    )
    signal = {
        "channel": "gmail",
        "title": "Reply to customer email",
        "snippet": "They need a response this week.",
        "link": "https://mail.google.com/mail/u/0/#inbox/thread-1",
        "entity_kind": "thread",
        "entity_id": "thread-1",
    }
    _, extra = sweep._synthesize(
        record,
        [{"kind": "deal", "label": secret}],
        [signal, {**signal, "link": "https://app.hubspot.com/secret"}],
        True,
        [],
    )
    assert len(prompts) == 2
    assert secret in prompts[0]
    assert secret not in prompts[1]
    assert "app.hubspot.com" not in prompts[1]
    assert extra == [
        {
            "kind": "google_signal_commitment",
            "label": signal["title"],
            "detail": signal["snippet"],
            "urgency": "high",
            "due": None,
            "entity_kind": "thread",
            "entity_id": "thread-1",
            "source_links": [signal["link"]],
        }
    ]


def test_legacy_google_commitment_is_hidden_and_cannot_be_resolved(db):
    _seed_users(db)
    record = store.create_record(
        db, user_email=DEPARTED, successor_email=SUCCESSOR, created_by=ADMIN
    )
    old, new = store.add_items(
        db,
        record,
        [
            {
                "kind": "google_commitment",
                "label": "Maybe copied from CRM inventory",
            },
            {
                "kind": "google_signal_commitment",
                "label": "Google thread",
                "entity_kind": "thread",
                "entity_id": "thread-1",
            },
        ],
    )
    db.commit()
    assert store.visible_items_for_record(db, record) == [new]
    assert (
        store.resolve_items_by_entity(
            db,
            successor_email=SUCCESSOR,
            entity_ids=[old.id, new.id],
            status="done",
            actor=SUCCESSOR,
        )
        == 1
    )
    assert old.status == "open"
    assert new.status == "done"


def test_sweep_reruns_do_not_duplicate_items(db, stubbed_sweep, monkeypatch):
    _seed_users(db)
    monkeypatch.setattr(
        sweep,
        "_inventory_eos",
        lambda *_: [{"kind": "todo", "label": "Owned EOS task"}],
    )
    rec = store.create_record(
        db, user_email=DEPARTED, successor_email=SUCCESSOR, created_by=ADMIN
    )
    db.commit()
    rid = rec.id

    sweep.run_offboard_sweep(rid)
    db.expire_all()
    first = len(store.get_record(db, rid).items)
    assert first > 0

    sweep.run_offboard_sweep(rid)
    db.expire_all()
    assert len(store.get_record(db, rid).items) == first


def test_sweep_failure_is_recorded_not_silent(db, stubbed_sweep, monkeypatch):
    _seed_users(db)
    rec = store.create_record(
        db, user_email=DEPARTED, successor_email=SUCCESSOR, created_by=ADMIN
    )
    db.commit()
    rid = rec.id
    monkeypatch.setattr(
        sweep,
        "_inventory_eos",
        lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    sweep.run_offboard_sweep(rid)
    db.expire_all()
    assert store.get_record(db, rid).sweep_status == "failed"
