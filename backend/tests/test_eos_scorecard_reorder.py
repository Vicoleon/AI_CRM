"""Tests for drag-and-drop reordering of scorecard measurables.

Covers ``POST /scorecard/metrics/reorder``:
- writes each id's array index to ``sort_order`` and echoes the requested order;
- ``GET /metrics`` then reflects the stored ``sort_order``;
- duplicate ids are rejected (400) and unknown ids are rejected whole (404),
  so a stale client can't silently drop a measurable's position;
- the literal ``/metrics/reorder`` path wins over ``/metrics/{metric_id}``.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


def _profile(email: str, roles: list[str] | None = None) -> UserProfile:
    now = "2026-09-22T00:00:00"
    stored = UserRegistry().get_user(email)
    return UserProfile(
        id=stored.id if stored else email.split("@")[0],
        email=email,
        name=stored.name if stored else email.split("@")[0].title(),
        roles=roles or (stored.roles if stored else ["user"]),
        department=stored.department if stored else "general",
        created_at=now,
        updated_at=now,
        is_exec=bool(stored.is_exec) if stored else False,
    )


def _as_user(email: str, roles: list[str] | None = None):
    app.dependency_overrides[get_current_user] = lambda: _profile(email, roles)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_reorder.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    reg = UserRegistry()
    if not reg.get_user("admin@example.com"):
        reg.register_user(
            email="admin@example.com",
            name="Admin",
            id="admin@example.com",
            roles=["admin"],
        )
    yield
    app.dependency_overrides.clear()


def _seed_three_metrics() -> list[str]:
    """One owner, three measurables (sort_order starts NULL). Returns ids in
    creation order."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person, ScorecardMetric

    db = SessionLocal()
    try:
        owner = Person(
            first_name="Marlo",
            last_name="Factoran",
            email=f"owner-{datetime.utcnow().strftime('%H%M%S%f')}@example.com",
            created_by="admin@example.com",
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)

        ids = []
        for name in ("RR in Pipeline", "Pending QC", "Business Days Remaining"):
            m = ScorecardMetric(
                owner_id=owner.id,
                name=name,
                frequency="weekly",
                on_track=True,
                created_by="admin@example.com",
            )
            db.add(m)
            db.commit()
            db.refresh(m)
            ids.append(str(m.id))
        return ids
    finally:
        db.close()


def test_reorder_sets_sort_order_and_echoes_requested_order():
    a, b, c = _seed_three_metrics()
    _as_user("admin@example.com", roles=["admin"])

    new_order = [c, a, b]
    r = client.post(
        "/api/eos/scorecard/metrics/reorder", json={"ordered_ids": new_order}
    )
    assert r.status_code == 200
    body = r.json()
    # Echo is in the requested order, with sort_order = array index.
    assert [m["id"] for m in body] == new_order
    assert [m["sort_order"] for m in body] == [0, 1, 2]

    # GET /metrics reflects the stored positions.
    got = {
        m["id"]: m["sort_order"]
        for m in client.get("/api/eos/scorecard/metrics").json()
    }
    assert got[c] == 0 and got[a] == 1 and got[b] == 2


def test_reorder_rejects_duplicate_ids():
    a, b, _ = _seed_three_metrics()
    _as_user("admin@example.com", roles=["admin"])
    r = client.post(
        "/api/eos/scorecard/metrics/reorder", json={"ordered_ids": [a, b, a]}
    )
    assert r.status_code == 400


def test_reorder_rejects_unknown_id_whole():
    a, b, _ = _seed_three_metrics()
    _as_user("admin@example.com", roles=["admin"])
    bogus = "00000000-0000-0000-0000-000000000000"
    r = client.post(
        "/api/eos/scorecard/metrics/reorder", json={"ordered_ids": [a, bogus, b]}
    )
    assert r.status_code == 404
    # Nothing landed: a and b keep their NULL sort_order.
    got = {
        m["id"]: m["sort_order"]
        for m in client.get("/api/eos/scorecard/metrics").json()
    }
    assert got[a] is None and got[b] is None


def test_reorder_path_is_not_shadowed_by_metric_id():
    # A POST to the literal /metrics/reorder must not be parsed as
    # /metrics/{metric_id}; an empty list is a valid no-op, not a 422.
    _as_user("admin@example.com", roles=["admin"])
    r = client.post("/api/eos/scorecard/metrics/reorder", json={"ordered_ids": []})
    assert r.status_code == 200
    assert r.json() == []
