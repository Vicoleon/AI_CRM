"""``/api/agents/priorities/deliver-slack`` must honour a ``report`` in its body.

The Friday afternoon Cloud Scheduler job posts ``{"report": "next_week"}``; that
field is the ONLY thing separating the look-ahead DM from a second copy of the
morning briefing (see ``test_slack_next_week_delivery.py`` for the duplicate
this fixes). The weekday morning job posts no body at all, so the
default has to stay ``daily``, and a typo has to fail loudly here rather than
quietly delivering the wrong report to the whole company.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import app
from rubik_core.config import settings

client = TestClient(app)

_SCHED_KEY = "sched-secret-test"
_PATH = "/api/agents/priorities/deliver-slack"


def _headers():
    return {"X-Scheduler-Key": _SCHED_KEY, "X-Cubie-Workspace": "test"}


def _auth(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", _SCHED_KEY)
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)


def test_no_body_delivers_the_daily_briefing(monkeypatch):
    """The weekday morning scheduler sends no body — that must stay daily."""
    _auth(monkeypatch)
    from backend.agents.slack_delivery import SlackDeliveryResult

    with patch(
        "backend.agents.slack_delivery.deliver_slack_priorities",
        return_value=SlackDeliveryResult(),
    ) as deliver:
        r = client.post(_PATH, headers=_headers())

    assert r.status_code == 200, r.text
    assert r.json()["report"] == "daily"
    assert deliver.call_args.kwargs["report"] == "daily"


def test_next_week_body_is_forwarded(monkeypatch):
    _auth(monkeypatch)
    from backend.agents.slack_delivery import SlackDeliveryResult

    with patch(
        "backend.agents.slack_delivery.deliver_slack_priorities",
        return_value=SlackDeliveryResult(report="next_week"),
    ) as deliver:
        r = client.post(_PATH, headers=_headers(), json={"report": "next_week"})

    assert r.status_code == 200, r.text
    assert r.json()["report"] == "next_week"
    assert deliver.call_args.kwargs["report"] == "next_week"


def test_targeted_next_week_resend_forwards_both(monkeypatch):
    """The ops-alert recovery recipe: a report AND an explicit recipient list."""
    _auth(monkeypatch)
    from backend.agents.slack_delivery import SlackDeliveryResult

    with patch(
        "backend.agents.slack_delivery.deliver_slack_priorities",
        return_value=SlackDeliveryResult(report="next_week"),
    ) as deliver:
        r = client.post(
            _PATH,
            headers=_headers(),
            json={"report": "next_week", "emails": ["bellison@acme.example"]},
        )

    assert r.status_code == 200, r.text
    assert r.json()["targeted"] is True
    assert deliver.call_args.kwargs["report"] == "next_week"
    assert deliver.call_args.kwargs["emails"] == ["bellison@acme.example"]


def test_unknown_report_is_rejected(monkeypatch):
    """A misconfigured scheduler must 400, not silently send the wrong report."""
    _auth(monkeypatch)

    with patch("backend.agents.slack_delivery.deliver_slack_priorities") as deliver:
        r = client.post(_PATH, headers=_headers(), json={"report": "nextweek"})

    assert r.status_code == 400, r.text
    assert "nextweek" in r.json()["detail"]
    deliver.assert_not_called()
