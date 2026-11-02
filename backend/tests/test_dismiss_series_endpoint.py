"""The dismiss endpoint's occurrence-vs-series contract.

Widening the ✗ button to silence every future occurrence of a recurring meeting
would make one click far more destructive than it looks, so ``scope`` is opt-in
and defaults to the long-standing occurrence behavior. These tests pin that
default, because losing it is a silent data-loss bug — every dismissal would
start suppressing a whole series.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app

mock_user = MagicMock()
mock_user.email = "ebrooks@acme.example"
mock_user.roles = ["user"]

client = TestClient(app)

SERIES = "34r4q9v6gjr8frotn9ntmbvhli"
OCCURRENCE = f"{SERIES}_20260803T160000Z"


@pytest.fixture(autouse=True)
def _override_auth():
    app.dependency_overrides[get_current_user] = lambda: mock_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _stored_priority(*, series: str | None) -> dict:
    return {
        "id": "p-1",
        "title": "Prepare for all-day Vistage meeting",
        "entity_kind": "event",
        "entity_id": OCCURRENCE,
        "priority_kind": "upcoming_prep",
        "series_entity_id": series,
    }


def _dismiss(body: dict | None, *, series: str | None = SERIES):
    """POST the dismiss endpoint with the card lookup stubbed; return the
    (response, dismiss_mock) pair so callers can assert on the memory write."""
    row = MagicMock(state="dismissed", snoozed_until=None)
    with (
        patch(
            "backend.routers.home._find_priority_in_today",
            return_value=_stored_priority(series=series),
        ),
        patch("rubik_core.priorities.memory.dismiss", return_value=row) as dismiss_mock,
    ):
        resp = client.post(
            "/api/homefeed/priorities/p-1/dismiss", json=body if body else {}
        )
    return resp, dismiss_mock


def test_default_scope_dismisses_only_this_occurrence():
    """No body → the historical behavior. The memory row must be keyed on the
    OCCURRENCE id, never the series."""
    resp, dismiss_mock = _dismiss(None)
    assert resp.status_code == 200
    assert dismiss_mock.call_args.kwargs["entity_id"] == OCCURRENCE
    assert resp.json()["entity_id"] == OCCURRENCE


def test_explicit_occurrence_scope_matches_the_default():
    resp, dismiss_mock = _dismiss({"scope": "occurrence"})
    assert resp.status_code == 200
    assert dismiss_mock.call_args.kwargs["entity_id"] == OCCURRENCE


def test_series_scope_keys_the_row_on_the_series_id():
    """The fix: "no preparation is needed for this meeting in the future"."""
    resp, dismiss_mock = _dismiss({"scope": "series"})
    assert resp.status_code == 200
    assert dismiss_mock.call_args.kwargs["entity_id"] == SERIES
    # The response reports what was actually suppressed, not what was clicked.
    assert resp.json()["entity_id"] == SERIES


def test_series_scope_on_a_one_off_event_is_rejected():
    """A non-recurring event has no series — 400 rather than silently falling
    back to an occurrence dismissal the user didn't ask for."""
    resp, dismiss_mock = _dismiss({"scope": "series"}, series=None)
    assert resp.status_code == 400
    assert "recurring" in resp.json()["detail"]
    dismiss_mock.assert_not_called()


def test_unknown_scope_is_rejected():
    resp, dismiss_mock = _dismiss({"scope": "everything"})
    assert resp.status_code == 422
    dismiss_mock.assert_not_called()


def test_missing_priority_still_404s():
    with (
        patch("backend.routers.home._find_priority_in_today", return_value=None),
        patch("rubik_core.priorities.memory.dismiss") as dismiss_mock,
    ):
        resp = client.post(
            "/api/homefeed/priorities/p-1/dismiss", json={"scope": "series"}
        )
    assert resp.status_code == 404
    dismiss_mock.assert_not_called()


def test_untagged_priority_still_400s():
    """Pre-UC-24 rows have no entity tags and cannot be keyed at all."""
    with (
        patch(
            "backend.routers.home._find_priority_in_today",
            return_value={"id": "p-1", "title": "legacy"},
        ),
        patch("rubik_core.priorities.memory.dismiss") as dismiss_mock,
    ):
        resp = client.post("/api/homefeed/priorities/p-1/dismiss", json={})
    assert resp.status_code == 400
    dismiss_mock.assert_not_called()
