"""``POST /api/fathom/w/test/webhook``: signed workspace webhook.

Fathom calls the webhook with no user session, so the route cannot sit behind
the copilot gate (``require_full_app_access`` on the ``core`` parent), which
would answer 401 before the handler ever ran. It is mounted outside that gate,
and Fathom's signature is its only gate. Everything here goes through the REAL
app (``backend.main.app``), signed the way Fathom signs (Standard Webhooks:
``webhook-id``, ``webhook-timestamp``, ``webhook-signature: v1,<base64>``, the
HMAC key being the base64 part of the ``whsec_`` secret). Pinned:

* A correctly signed event with no bearer token reaches the handler.
* A missing, wrong or stale signature is refused with 401 before any work is
  done: no ingestion processor is built and nothing is launched.
* With no webhook secret configured, every event is refused (fail closed).
* A signed event whose ``webhook-id`` this instance already accepted inside
  the replay window gets 200 "duplicate" and no work. Only a correctly signed
  event's id is remembered, and a delivery that failed is forgotten so Fathom's
  retry of it (same id) is imported.
* Every other Fathom route (``/fathom/ingest``, ``/fathom/calls``) is still
  behind the copilot gate.

And the handler itself, once the signature is good: the meeting's host decides
whose import it is, judged by ``fathom_auto_ingest`` (and the copilot), the same
list as ``/fathom/ingest``, never by a department name.

Only the ingestion processor and the background launcher are stood in; the
users live in a throwaway SQLite database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.routers import fathom as fathom_router
from backend.routers.ingest import get_processor
from backend.tests._rosters import pin_rosters
from rubik_core.config import settings
from rubik_core.features import FeaturePolicy, clear_cache
from rubik_core.integrations import fathom as fathom_integration
from rubik_core.integrations.fathom import WebhookReplayGuard
from rubik_core.rag.config import slugify_department
from rubik_core.tenancy.secrets import (
    bind_workspace_secret_ref,
    clear_workspace_secret,
    write_secret,
)

client = TestClient(app)

WEBHOOK = "/api/fathom/w/test/webhook"
_KEY = b"cubie-test-fathom-webhook-key"
SECRET = "whsec_" + base64.b64encode(_KEY).decode()


def test_fathom_client_reads_workspace_api_key(monkeypatch):
    seen = []
    monkeypatch.setenv("FATHOM_API_KEY", "process-key")
    monkeypatch.setattr(
        fathom_integration,
        "workspace_secret",
        lambda connector, field: seen.append((connector, field)) or "workspace-key",
        raising=False,
    )

    client = fathom_integration.FathomClient.from_settings()

    assert client.api_key == "workspace-key"
    assert seen == [("fathom", "api_key")]


def test_fathom_webhook_verifier_reads_workspace_secret(monkeypatch):
    seen = []
    monkeypatch.setenv("FATHOM_WEBHOOK_SECRET", "bad-global-secret")
    monkeypatch.setattr(
        fathom_integration,
        "workspace_secret",
        lambda connector, field: seen.append((connector, field)) or SECRET,
        raising=False,
    )
    body = b"{}"

    assert fathom_integration.verify_webhook_signature(body, _sign(body))
    assert seen == [("fathom", "webhook_secret")]


def _sign(
    body: bytes, *, key: bytes = _KEY, msg_id: str | None = None, ts=None
) -> dict:
    """The headers Fathom sends with ``body``, signed with ``key``: a new event
    (a fresh ``webhook-id``) unless the test names the id."""
    msg_id = msg_id or f"msg_{uuid.uuid4().hex}"
    stamp = str(int(time.time()) if ts is None else ts)
    digest = hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": stamp,
        "webhook-signature": "v1," + base64.b64encode(digest.digest()).decode(),
        "content-type": "application/json",
    }


@pytest.fixture
def users_db(tmp_path, monkeypatch):
    """A fresh database holding three people with provisioned roles only."""
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'fathom_webhook.db'}"
    )
    import rubik_core.database.db as dbmod
    from rubik_core.database.models import Base, Role, User

    monkeypatch.setattr(dbmod, "_engine", None)
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    with dbmod.SessionLocal() as db:
        member = Role(name="user")
        db.add_all(
            [
                User(
                    email="rep@acme.example", id="rep", name="Rep", department="Sales"
                ),
                User(
                    email="cs@acme.example",
                    id="cs",
                    name="Cs",
                    department="Customer Success",
                ),
                User(email="ops@acme.example", id="ops", name="Ops", department=""),
            ]
        )
        db.flush()
        for user in db.query(User).all():
            user.roles = [member]
        db.commit()
    clear_cache()
    yield
    clear_cache()
    monkeypatch.setattr(dbmod, "_engine", None)


@pytest.fixture
def launched(monkeypatch, tmp_path):
    """The real app with Fathom's secret configured and no user session at all:
    real auth (no mock user), the background launcher recorded instead of run,
    and a processor factory that records whether it was ever asked for one."""
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    ref = bind_workspace_secret_ref("fathom", "webhook_secret")
    write_secret(ref, SECRET)
    submit = MagicMock()
    monkeypatch.setattr(fathom_router, "submit_background_work", submit)
    submit.processors_built = []

    def build_processor():  # a plain function: FastAPI reads its signature
        submit.processors_built.append(MagicMock())
        return submit.processors_built[-1]

    app.dependency_overrides.clear()
    app.dependency_overrides[get_processor] = build_processor
    fathom_router.WEBHOOK_REPLAYS.clear()
    yield submit
    clear_workspace_secret("fathom", "webhook_secret")
    fathom_router.WEBHOOK_REPLAYS.clear()
    app.dependency_overrides.clear()


def _completed(host_email: str | None = None) -> dict:
    meeting = {} if host_email is None else {"host_email": host_email}
    return {"type": "meeting.completed", "meeting_id": "m-1", "meeting": meeting}


def _post(payload: dict, *, headers: dict | None = None, **sign):
    body = json.dumps(payload).encode()
    return client.post(
        WEBHOOK,
        content=body,
        headers=_sign(body, **sign) if headers is None else headers,
    )


def _list(monkeypatch, *departments: str, emails: tuple[str, ...] = ()) -> None:
    """An admin put ``departments`` (names, as typed) and ``emails`` on the list."""
    pin_rosters(
        monkeypatch,
        {
            "fathom_auto_ingest": FeaturePolicy(
                departments=frozenset(slugify_department(d) for d in departments),
                extra_emails=frozenset(emails),
                admin_allowed=False,
            )
        },
    )


def _refused_before_any_work(response, launched) -> None:
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Invalid signature"}
    launched.assert_not_called()
    assert launched.processors_built == []


# -- reachable, behind its signature and nothing else ---------------------------


def test_a_signed_event_with_no_bearer_token_reaches_the_handler(users_db, launched):
    response = _post(_completed())

    assert "authorization" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "message": "Ignored, no host."}
    assert len(launched.processors_built) == 1  # the handler ran


def test_a_signed_event_for_a_host_on_the_list_starts_the_import(
    users_db, launched, monkeypatch
):
    _list(monkeypatch, "Customer Success")

    response = _post(_completed("cs@acme.example"))

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}
    launched.assert_called_once()
    assert launched.call_args.kwargs["args"] == ["--user-email", "cs@acme.example"]
    assert launched.call_args.kwargs["env"] == {"CUBIE_WORKSPACE": "test"}


def test_the_older_svix_header_names_are_accepted_too(users_db, launched):
    body = json.dumps(_completed()).encode()
    headers = {
        name.replace("webhook-", "svix-"): value for name, value in _sign(body).items()
    }

    response = client.post(WEBHOOK, content=body, headers=headers)

    assert response.status_code == 200, response.text


def test_a_missing_signature_is_refused_before_any_work(users_db, launched):
    _refused_before_any_work(_post(_completed(), headers={}), launched)


@pytest.mark.parametrize(
    "drop", ["webhook-id", "webhook-timestamp", "webhook-signature"]
)
def test_an_event_missing_any_signature_header_is_refused(users_db, launched, drop):
    body = json.dumps(_completed()).encode()
    headers = {k: v for k, v in _sign(body).items() if k != drop}

    _refused_before_any_work(
        client.post(WEBHOOK, content=body, headers=headers), launched
    )


def test_a_signature_made_with_another_key_is_refused(users_db, launched):
    _refused_before_any_work(_post(_completed(), key=b"someone-elses-key"), launched)


def test_a_body_changed_after_signing_is_refused(users_db, launched, monkeypatch):
    _list(monkeypatch, "Customer Success")
    signed = json.dumps(_completed("rep@acme.example")).encode()
    forged = json.dumps(_completed("cs@acme.example")).encode()

    response = client.post(WEBHOOK, content=forged, headers=_sign(signed))

    _refused_before_any_work(response, launched)


@pytest.mark.parametrize("skew", [-6 * 60, 6 * 60], ids=["replayed", "future"])
def test_an_event_stamped_over_five_minutes_away_is_refused(users_db, launched, skew):
    _refused_before_any_work(_post(_completed(), ts=int(time.time()) + skew), launched)


def test_a_signature_without_its_version_prefix_is_refused(users_db, launched):
    body = json.dumps(_completed()).encode()
    headers = _sign(body)
    headers["webhook-signature"] = headers["webhook-signature"].removeprefix("v1,")

    _refused_before_any_work(
        client.post(WEBHOOK, content=body, headers=headers), launched
    )


def test_with_no_secret_configured_every_event_is_refused(users_db, launched):
    """Fail closed: no secret means nothing can prove an event is Fathom's."""
    clear_workspace_secret("fathom", "webhook_secret")

    _refused_before_any_work(_post(_completed()), launched)


# -- a repeat of an event already accepted --------------------------------------


def test_a_repeated_event_id_gets_duplicate_and_no_work(
    users_db, launched, monkeypatch
):
    _list(monkeypatch, "Customer Success")
    body = json.dumps(_completed("cs@acme.example")).encode()
    headers = _sign(body, msg_id="msg_repeat")

    first = client.post(WEBHOOK, content=body, headers=headers)
    again = client.post(WEBHOOK, content=body, headers=headers)

    assert first.status_code == 200, first.text
    assert first.json() == {"status": "ok"}
    assert again.status_code == 200, again.text
    assert again.json() == {"status": "duplicate"}
    launched.assert_called_once()
    assert len(launched.processors_built) == 1  # the repeat built nothing


def test_a_repeat_under_the_older_svix_names_is_the_same_event(users_db, launched):
    body = json.dumps(_completed()).encode()
    headers = _sign(body, msg_id="msg_svix")
    svix = {name.replace("webhook-", "svix-"): value for name, value in headers.items()}

    assert client.post(WEBHOOK, content=body, headers=headers).status_code == 200
    again = client.post(WEBHOOK, content=body, headers=svix)

    assert again.json() == {"status": "duplicate"}
    assert len(launched.processors_built) == 1


def test_two_events_with_their_own_ids_are_both_handled(
    users_db, launched, monkeypatch
):
    _list(monkeypatch, "Customer Success")

    assert _post(_completed("cs@acme.example")).json() == {"status": "ok"}
    assert _post(_completed("cs@acme.example")).json() == {"status": "ok"}
    assert launched.call_count == 2


def test_only_a_signed_event_marks_its_id_as_seen(users_db, launched, monkeypatch):
    """A forged request cannot burn a real event's id: its id is not kept."""
    _list(monkeypatch, "Customer Success")
    body = json.dumps(_completed("cs@acme.example")).encode()

    forged = client.post(
        WEBHOOK, content=body, headers=_sign(body, key=b"wrong", msg_id="msg_real")
    )
    _refused_before_any_work(forged, launched)

    real = client.post(WEBHOOK, content=body, headers=_sign(body, msg_id="msg_real"))

    assert real.json() == {"status": "ok"}
    launched.assert_called_once()


def test_a_delivery_that_failed_is_imported_when_fathom_retries_it(
    users_db, launched, monkeypatch
):
    """Fathom retries a failed delivery under the same id: that retry is work
    still to do, not a duplicate."""
    _list(monkeypatch, "Customer Success")
    launched.side_effect = [RuntimeError("the worker is unreachable"), None]
    body = json.dumps(_completed("cs@acme.example")).encode()
    headers = _sign(body, msg_id="msg_retry")

    failed = client.post(WEBHOOK, content=body, headers=headers)
    retried = client.post(WEBHOOK, content=body, headers=headers)

    assert failed.status_code == 500
    assert retried.status_code == 200, retried.text
    assert retried.json() == {"status": "ok"}
    assert launched.call_count == 2


def test_a_delivery_that_failed_before_the_handler_is_imported_on_retry(
    users_db, launched, monkeypatch
):
    """A failure while building the handler's own dependencies (the ingestion
    processor) is still a failed delivery: Fathom's retry must not be taken
    for a duplicate."""
    _list(monkeypatch, "Customer Success")
    attempts = []

    def flaky_processor():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("the vector store is unreachable")
        return MagicMock()

    app.dependency_overrides[get_processor] = flaky_processor
    body = json.dumps(_completed("cs@acme.example")).encode()
    headers = _sign(body, msg_id="msg_processor_down")
    lenient = TestClient(app, raise_server_exceptions=False)

    failed = lenient.post(WEBHOOK, content=body, headers=headers)
    retried = lenient.post(WEBHOOK, content=body, headers=headers)

    assert failed.status_code == 500
    assert retried.status_code == 200, retried.text
    assert retried.json() == {"status": "ok"}
    launched.assert_called_once()


def test_the_replay_guard_forgets_an_id_once_the_window_has_passed():
    now = [1_000.0]
    guard = WebhookReplayGuard(window_seconds=600, max_ids=10, clock=lambda: now[0])

    assert guard.first_delivery("msg_a") is True
    now[0] += 599
    assert guard.first_delivery("msg_a") is False
    now[0] += 2  # 601 s after the first delivery
    assert guard.first_delivery("msg_a") is True


def test_the_replay_guard_is_bounded_and_forgets_the_oldest_first():
    guard = WebhookReplayGuard(window_seconds=600, max_ids=2, clock=lambda: 0.0)

    for msg_id in ("msg_1", "msg_2", "msg_3"):
        assert guard.first_delivery(msg_id) is True
    assert len(guard) == 2
    assert guard.first_delivery("msg_3") is False
    assert guard.first_delivery("msg_1") is True  # forgotten to make room


def test_the_replay_window_covers_every_moment_the_timestamp_check_passes():
    """A replay is refused by its stamp after twice the tolerance, so the guard
    must remember an id at least that long."""
    from rubik_core.integrations import fathom as fathom_client

    assert (
        fathom_client.WEBHOOK_REPLAY_WINDOW_SECONDS
        >= 2 * fathom_client.WEBHOOK_TOLERANCE_SECONDS
    )
    assert (
        fathom_router.WEBHOOK_REPLAYS.window_seconds
        == fathom_client.WEBHOOK_REPLAY_WINDOW_SECONDS
    )


@pytest.mark.parametrize(
    ("method", "path"), [("post", "/api/fathom/ingest"), ("get", "/api/fathom/calls")]
)
def test_every_other_fathom_route_is_still_behind_the_copilot_gate(
    users_db, launched, method, path
):
    response = getattr(client, method)(path)

    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Missing authentication credentials"
    launched.assert_not_called()


# -- whose import it is, once the signature is good -----------------------------


def test_a_sales_host_off_the_list_is_not_imported(users_db, launched, monkeypatch):
    """The department name decides nothing: the list does."""
    _list(monkeypatch, "Customer Success")

    response = _post(_completed("rep@acme.example"))

    assert response.status_code == 200, response.text
    assert "not on the Fathom import list" in response.json()["message"]
    launched.assert_not_called()


def test_a_host_named_on_the_list_with_no_department_is_imported(
    users_db, launched, monkeypatch
):
    _list(monkeypatch, emails=("ops@acme.example",))

    assert _post(_completed("OPS@acme.example")).status_code == 200
    launched.assert_called_once()


def test_with_the_shipped_default_nobody_is_imported(users_db, launched):
    """The list ships with nobody on it, so a fresh workspace imports nothing."""
    for host in ("rep@acme.example", "cs@acme.example", "stranger@acme.example"):
        assert _post(_completed(host)).status_code == 200, host
    launched.assert_not_called()


def test_a_host_narrowed_out_of_the_copilot_is_not_imported(
    users_db, launched, monkeypatch
):
    pin_rosters(
        monkeypatch,
        {
            "fathom_auto_ingest": FeaturePolicy(everyone=True, admin_allowed=False),
            "copilot": FeaturePolicy(departments=frozenset({"sales"})),
        },
    )

    _post(_completed("cs@acme.example"))
    launched.assert_not_called()
    _post(_completed("rep@acme.example"))
    launched.assert_called_once()
