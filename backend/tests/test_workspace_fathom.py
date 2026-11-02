"""Fathom's public webhook binds one active workspace before tenant work."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.routers import fathom as fathom_router
from backend.routers.ingest import get_processor
from rubik_core.config import settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace
from rubik_core.tenancy import context
from rubik_core.tenancy.context import current_workspace

CLIENT = TestClient(app)
KEYS = {"alpha": b"fathom-alpha-key", "beta": b"fathom-beta-key"}


def _secret(slug: str) -> str:
    return "whsec_" + base64.b64encode(KEYS[slug]).decode()


def _signed(body: bytes, slug: str, event_id: str = "evt-1") -> dict[str, str]:
    stamp = str(int(time.time()))
    signed = f"{event_id}.{stamp}.".encode() + body
    signature = base64.b64encode(hmac.new(KEYS[slug], signed, hashlib.sha256).digest())
    return {
        "webhook-id": event_id,
        "webhook-timestamp": stamp,
        "webhook-signature": "v1," + signature.decode(),
        "content-type": "application/json",
    }


def _post(url_slug: str, key_slug: str, event_id: str = "evt-1"):
    body = json.dumps({"type": "meeting.completed", "meeting_id": "m1"}).encode()
    return CLIENT.post(
        f"/api/fathom/w/{url_slug}/webhook",
        content=body,
        headers=_signed(body, key_slug, event_id),
    )


@pytest.fixture
def workspaces(monkeypatch):
    from rubik_core.integrations import fathom as integration

    monkeypatch.setattr(context, "_process_workspace", None)
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    with control_session() as db:
        for slug in KEYS:
            db.add(Workspace(slug=slug, name=slug, status="active"))
        db.commit()
    read_secrets = []

    def workspace_secret(connector: str, field: str) -> str:
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        slug = current_workspace()
        read_secrets.append((slug, connector, field))
        return _secret(slug)

    monkeypatch.setattr(integration, "workspace_secret", workspace_secret)
    processors = []

    def processor():
        processors.append(current_workspace())
        return MagicMock()

    app.dependency_overrides[get_processor] = processor
    fathom_router.WEBHOOK_REPLAYS.clear()
    try:
        yield read_secrets, processors
    finally:
        fathom_router.WEBHOOK_REPLAYS.clear()
        app.dependency_overrides.pop(get_processor, None)
        with control_session() as db:
            for slug in KEYS:
                row = db.get(Workspace, slug)
                if row is not None:
                    db.delete(row)
            db.commit()


def test_fathom_webhook_uses_url_workspace_secret(workspaces):
    reads, processors = workspaces
    wrong = _post("alpha", "beta")
    assert wrong.status_code == 401
    assert processors == []
    assert reads == [("alpha", "fathom", "webhook_secret")]

    accepted = _post("alpha", "alpha")
    assert accepted.status_code == 200
    assert accepted.json() == {"status": "ok", "message": "Ignored, no host."}
    assert processors == ["alpha"]


def test_fathom_replay_ids_are_scoped_to_workspace(workspaces):
    _, processors = workspaces
    assert _post("alpha", "alpha", "shared-event").status_code == 200
    assert _post("beta", "beta", "shared-event").status_code == 200
    duplicate = _post("alpha", "alpha", "shared-event")
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate"}
    assert processors == ["alpha", "beta"]


def test_unknown_or_suspended_slug_refused_before_tenant_dependency(workspaces):
    _, processors = workspaces
    assert _post("missing", "alpha").status_code == 404
    with control_session() as db:
        db.get(Workspace, "alpha").status = "suspended"
        db.commit()
    assert _post("alpha", "alpha").status_code == 404
    assert processors == []


def test_old_unscoped_fathom_webhook_is_gone(workspaces):
    body = b"{}"
    response = CLIENT.post(
        "/api/fathom/webhook", content=body, headers=_signed(body, "alpha")
    )
    assert response.status_code == 404


def test_fathom_job_requires_validated_process_workspace(monkeypatch):
    from backend.jobs.fathom_sync_job import main

    monkeypatch.setattr(context, "_process_workspace", None)
    monkeypatch.delenv("CUBIE_WORKSPACE", raising=False)
    assert main([]) == 2


def test_chunked_oversized_webhook_rejected_before_secret_read(workspaces):
    reads, processors = workspaces
    chunks = iter([b"a" * (128 * 1024), b"b" * (128 * 1024 + 1)])
    response = CLIENT.post(
        "/api/fathom/w/alpha/webhook",
        content=chunks,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert reads == []
    assert processors == []


def test_malformed_signature_is_rejected_before_secret_lookup(workspaces):
    reads, processors = workspaces
    response = CLIENT.post(
        "/api/fathom/w/alpha/webhook",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401
    assert reads == []
    assert processors == []


def test_fathom_handler_runs_sync_tenant_work_off_event_loop(workspaces, monkeypatch):
    seen = []

    def host_allowed(email: str) -> bool:
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        seen.append(("host", current_workspace(), email))
        return True

    def dispatch(*_args, **kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        seen.append(("dispatch", current_workspace(), kwargs["env"]))
        return "inline", None

    monkeypatch.setattr(fathom_router, "_host_may_auto_ingest", host_allowed)
    monkeypatch.setattr(fathom_router, "submit_background_work", dispatch)
    body = json.dumps(
        {
            "type": "meeting.completed",
            "meeting_id": "m1",
            "meeting": {"host_email": "owner@alpha.example"},
        }
    ).encode()
    response = CLIENT.post(
        "/api/fathom/w/alpha/webhook",
        content=body,
        headers=_signed(body, "alpha", "offloop-event"),
    )
    assert response.status_code == 200
    assert seen == [
        ("host", "alpha", "owner@alpha.example"),
        ("dispatch", "alpha", {"CUBIE_WORKSPACE": "alpha"}),
    ]
