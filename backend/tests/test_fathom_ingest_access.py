"""``POST /api/fathom/ingest`` is gated on the ``fathom_auto_ingest`` roster.

The app calls the endpoint when someone opens it, and it imports the company
Fathom account's recent recordings into the shared meeting library — so only
the people an admin puts on the roster (Admin → Feature Access; it ships off)
may start it. These tests go through the real router and the real
``feature_allows`` verdict, with only the signed-in user, the ingestion
processor and the background launcher stood in.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.routers import fathom as fathom_router
from backend.routers.ingest import get_processor
from backend.tests._rosters import pin_rosters
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy, clear_cache

client = TestClient(app)


def _person(email: str, *, department: str = "", roles=None) -> UserProfile:
    now = "2026-10-28T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def launched(monkeypatch):
    """The background launcher, recorded instead of run."""
    submit = MagicMock()
    monkeypatch.setattr(fathom_router, "submit_background_work", submit)
    app.dependency_overrides[get_processor] = lambda: MagicMock()
    clear_cache()
    yield submit
    clear_cache()
    app.dependency_overrides.clear()


def _sign_in(person: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: person


@pytest.fixture
def recording_team(monkeypatch):
    """An admin put the team that records client calls on the roster."""
    pin_rosters(
        monkeypatch,
        {"fathom_auto_ingest": FeaturePolicy(departments=frozenset({"sales"}))},
    )


def test_someone_on_the_roster_starts_the_import(launched, recording_team):
    _sign_in(_person("rep@acme.example", department="Sales"))

    response = client.post("/api/fathom/ingest")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    launched.assert_called_once()
    assert launched.call_args.kwargs["args"] == ["--user-email", "rep@acme.example"]


def test_someone_off_the_roster_is_refused_and_nothing_starts(launched, recording_team):
    _sign_in(_person("ops@acme.example", department="Operations"))

    response = client.post("/api/fathom/ingest")

    assert response.status_code == 403
    assert "Fathom import is not turned on for you" in response.json()["detail"]
    launched.assert_not_called()


def test_with_the_shipped_default_nobody_starts_it(launched, monkeypatch):
    """It ships off: not even an admin starts an import before someone is chosen."""
    from rubik_core.features import policy

    real = policy.stored_policy
    monkeypatch.setattr(  # no Feature Access row, whatever the shared DB holds
        policy,
        "stored_policy",
        lambda key, db=None: None if key == "fathom_auto_ingest" else real(key, db),
    )
    for person in (
        _person("rep@acme.example", department="Sales"),
        _person("admin@acme.example", roles=["admin", "user"]),
    ):
        _sign_in(person)
        assert client.post("/api/fathom/ingest").status_code == 403, person.email
    launched.assert_not_called()
