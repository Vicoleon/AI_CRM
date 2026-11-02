"""A card's facts have to survive the whole way to the browser.

The My To-Dos "You said you would…" card is not honest without its facts: the
words the reader said, when and where they said them, and what Cubie checked
before saying it has not happened. Those live in the card's ``attrs``.

Four hops carry them, and each can fail silently on its own:

  1. the promises agent stamps them on the signal;
  2. ``_reattach_promise_entities`` copies the allow-listed subset onto the card;
  3. ``bundle_to_metadata`` persists it into ``HomeCard.metadata_json``;
  4. **the read endpoint projects it** — ``_priority_dict_from_metadata`` must
     carry an ``attrs`` key, and ``PriorityItem`` must DECLARE an ``attrs``
     field, or the response model drops the projected value on the way out.

Hop 4 is what these tests pin, end to end through the real endpoint, because
both halves of it fail silently and independently: drop the projection and the
response carries ``attrs: null``; drop the model field and the projection is
thrown away on the way out. Each half has its own test below.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings

USER = "pat@acme.example"
PROMISE = "3f2b1c44-1111-4a0e-9c11-aaaaaaaaaaaa"
LABEL = "Send the Q3 deck"

# The words the whole card exists to quote back.
QUOTE = "I'll send you the Q3 deck by Friday."

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """A throwaway SQLite file, and a hard guard against the dev Cloud SQL.

    ``db.get_engine()`` reads CLOUDSQL_CONNECTION_NAME *before* DATABASE_URL, so
    a developer .env alone is enough to point a suite that writes rows at the
    live dev database. Blank it, then prove where we landed.
    """
    settings.CLOUDSQL_CONNECTION_NAME = ""
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'card_attrs.db'}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    assert engine.dialect.name == "sqlite", "tests must never touch Cloud SQL"
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database.models import Base, User

    Base.metadata.create_all(bind=engine)

    from rubik_core.database.db import SessionLocal

    with SessionLocal() as db:
        db.add(
            User(
                email=USER,
                id="sub-pat",
                name="Pat Example",
                department="Systems",
                is_exec=False,
            )
        )
        db.commit()
    yield


mock_user = MagicMock()
mock_user.email = USER
mock_user.name = "Pat Example"
mock_user.is_exec = False


@pytest.fixture(autouse=True)
def _override_auth():
    app.dependency_overrides[get_current_user] = lambda: mock_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _store_card(attrs: dict | None) -> None:
    """Persist today's daily_briefing HomeCard holding one promise priority."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import HomeCard
    from rubik_core.priorities import DAILY_BRIEFING_CATEGORY
    from rubik_core.priorities.temporal import user_today_str
    from rubik_core.tenancy.context import current_workspace

    priority: dict = {
        "id": "p-promise-1",
        "title": f"You told Tatum you'd {LABEL.lower()}",
        "why_it_matters": "No sign of it in your sent mail since Monday.",
        "urgency": "high",
        "source_channels": ["promises"],
        "source_links": ["/todos"],
        "suggested_action": "Send it today, or tell Tatum the new date.",
        "priority_kind": "promise_unkept",
        "entity_kind": "promise",
        "entity_id": PROMISE,
        "entity_label": LABEL,
    }
    if attrs is not None:
        priority["attrs"] = attrs

    with SessionLocal() as db:
        db.add(
            HomeCard(
                id="card-1",
                user_email=USER,
                date=user_today_str(USER),
                category=DAILY_BRIEFING_CATEGORY,
                content_md="",
                metadata_json={
                    "schema_version": 2,
                    "crm_provenance": {
                        "workspace": current_workspace(),
                        "deal_ids": [],
                        "unprovable": False,
                    },
                    "generated_at": "2026-08-13T08:00:00+00:00",
                    "headline": "One promise needs you",
                    "priorities": [priority],
                },
            )
        )
        db.commit()


def _read_priorities() -> dict:
    """The cached-bundle read path, through the real endpoint and its model."""
    res = client.post("/api/homefeed/priorities", json={"force_refresh": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["priorities"], "the seeded card did not survive the read path"
    return body["priorities"][0]


# ── the chain, end to end ────────────────────────────────────────────────────


def test_the_promise_facts_reach_the_browser():
    """The whole point. The stored words, when and where come back on the card.

    Asserted through the endpoint rather than the projection helper because the
    response model sits between them and used to swallow the key.
    """
    _store_card(
        {
            "promise_quote": QUOTE,
            "promise_said_on": "2026-08-10",
            "promise_where": "the Monday sales call",
            "promise_checked": "your sent mail",
        }
    )

    item = _read_priorities()

    assert item["attrs"] is not None, (
        "the promise card reached the browser with no facts — it cannot quote "
        "what the reader said or say what Cubie checked"
    )
    assert item["attrs"]["promise_quote"] == QUOTE
    assert item["attrs"]["promise_said_on"] == "2026-08-10"
    assert item["attrs"]["promise_checked"] == "your sent mail"


def test_the_response_model_declares_attrs_so_the_projection_is_not_dropped():
    """Pinned separately from the projection: two independent silent failures.

    ``PrioritiesResponse(**cached)`` re-validates the projected dict, so a
    `PriorityItem` without an ``attrs`` field discards it no matter how
    correctly the projection ran.
    """
    from backend.routers.home import PriorityItem

    assert "attrs" in PriorityItem.model_fields, (
        "PriorityItem must DECLARE attrs — an undeclared key is dropped by the "
        "response model on the way out"
    )

    item = PriorityItem(
        id="p-1",
        title="t",
        why_it_matters="w",
        urgency="high",
        source_channels=["promises"],
        attrs={"promise_quote": QUOTE, "promise_status": "open"},
    )
    assert item.model_dump()["attrs"]["promise_quote"] == QUOTE


def test_the_allowlist_holds_at_the_read_boundary():
    """The projection re-filters; it does not pass the stored blob through.

    A row written by an older build, hand-edited, or produced by some future
    path that sets ``PriorityCandidate.attrs`` directly must still not put an
    un-allow-listed key in front of a reader — including the keys a card of a
    removed channel once carried.
    """
    _store_card(
        {
            "promise_quote": QUOTE,
            "risk_basis_note": "a key from a removed channel",
            "owner_email": "someone@acme.example",
            "promise_where": {"nested": "object"},
        }
    )

    attrs = _read_priorities()["attrs"]

    assert attrs["promise_quote"] == QUOTE
    assert "risk_basis_note" not in attrs, "a removed channel's key is not allow-listed"
    assert "owner_email" not in attrs, "an address is not a card fact"
    assert "promise_where" not in attrs, "a nested object has no renderer"


def test_a_card_with_no_facts_carries_null_rather_than_an_empty_dict():
    """Absent and empty read identically to the panel; send the smaller one.

    Nothing is inferred from an absence: a card with no facts says nothing.
    """
    _store_card(None)

    assert _read_priorities()["attrs"] is None


def test_a_card_from_a_channel_without_facts_is_unaffected():
    """Every other card kind still projects a bare ``attrs: null``."""
    from backend.routers.home import _priority_dict_from_metadata

    item = _priority_dict_from_metadata(
        {
            "id": "p-eos",
            "title": "Your Rock is off track",
            "entity_kind": "rock",
            "entity_id": "rock-1",
            "priority_kind": "eos_rock_off_track",
        }
    )

    assert item["attrs"] is None
