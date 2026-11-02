"""UC-45 access gate + router contract.

Unit-tests the access predicate directly, then exercises the router's gate
(403 for non-members, 200 for a Sales AE) and the deep-run 202/409 contract
with the service + deep pool monkeypatched (no HubSpot, no LLM, no real thread).

X-Ray ships open to everyone. The narrowed cases install the roster an admin
would set in Admin → Feature Access — Sales + Systems + Exec + admins — and
check the gate honours it (``narrowed_xray``).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user, user_can_access_xray
from backend.main import app
from backend.tests._rosters import pin_rosters
from rubik_core.auth.registry import UserProfile
from rubik_core.features import FeaturePolicy
from rubik_core.xray import service


def _user(**kw) -> UserProfile:
    base = dict(
        id="u1",
        email="u1@acme.example",
        name="U One",
        department="general",
        roles=["user"],
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
    )
    base.update(kw)
    return UserProfile(**base)


@pytest.fixture(autouse=True)
def narrowed_xray(monkeypatch):
    pin_rosters(
        monkeypatch,
        {
            "customer_xray": FeaturePolicy(
                departments=frozenset({"sales", "systems"}),
                exec_allowed=True,
                admin_allowed=True,
            )
        },
    )


@pytest.fixture(autouse=True)
def owned_company_for_router_plumbing(monkeypatch):
    """These route-contract tests use a fake company; ownership is tested separately."""
    from rubik_core.xray import fetchers

    monkeypatch.setattr(fetchers, "company_available", lambda _company_id: True)
    monkeypatch.setattr(
        fetchers, "visible_company_ids", lambda company_ids: set(company_ids)
    )


def test_gate_ships_open_to_everyone():
    from rubik_core.features import feature_by_key

    default = feature_by_key("customer_xray").default()
    assert default.everyone and default.exec_allowed and not default.departments


# ── Gate predicate, narrowed: Sales + Systems + Exec + admins ─────────────────
def test_gate_admin_allowed():
    assert user_can_access_xray(_user(roles=["admin"], department="marketing"))


def test_gate_sales_allowed():
    # Case/whitespace-insensitive: department rows are uncurated free text.
    assert user_can_access_xray(_user(department="Sales"))
    assert user_can_access_xray(_user(department=" sales "))


def test_gate_sales_team_manager_allowed():
    assert user_can_access_xray(
        _user(department="operations", managed_departments=["Sales"])
    )


def test_gate_systems_allowed():
    # Systems stayed in the audience when X-Ray opened to Sales: they support
    # the feature and fielded every X-Ray request during the soft launch.
    assert user_can_access_xray(_user(department="Systems"))


def test_gate_systems_team_manager_allowed():
    assert user_can_access_xray(
        _user(department="operations", managed_departments=["Systems"])
    )


def test_gate_orgwide_exec_allowed():
    assert user_can_access_xray(_user(is_exec=True, managed_departments=["*"]))


def test_gate_any_exec_allowed():
    assert user_can_access_xray(_user(is_exec=True, managed_departments=["finance"]))
    assert user_can_access_xray(_user(is_exec=True))


def test_gate_marketing_denied():
    assert not user_can_access_xray(_user(department="marketing"))


def test_gate_general_denied():
    assert not user_can_access_xray(_user())


# ── Router ─────────────────────────────────────────────────────────────────────
@pytest.fixture
def as_user():
    def _apply(user: UserProfile):
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app)

    yield _apply
    app.dependency_overrides.clear()


def test_router_403_for_marketing(as_user):
    # Marketing is not sales, systems, exec or admin.
    client = as_user(_user(department="marketing"))
    r = client.get("/api/xray/customers?q=acme")
    assert r.status_code == 403
    assert "Customer X-Ray" in r.json()["detail"]


def test_router_customers_ok_for_sales_ae(as_user, monkeypatch):
    # The whole point of ending the soft launch: a plain Sales AE (no admin
    # role, no exec flag, no managed departments) gets through the router gate.
    monkeypatch.setattr(service, "customer_search", lambda q, limit=10: [])
    client = as_user(_user(department="Sales"))
    r = client.get("/api/xray/customers?q=acme")
    assert r.status_code == 200
    assert r.json() == []


# Router plumbing tests use an ADMIN caller, who is allowed whatever the
# department audience is, so they stay green across audience changes.
def test_router_customers_ok_for_admin(as_user, monkeypatch):
    monkeypatch.setattr(service, "customer_search", lambda q, limit=10: [])
    client = as_user(_user(roles=["admin"]))
    r = client.get("/api/xray/customers?q=acme")
    assert r.status_code == 200
    assert r.json() == []


def test_router_deep_returns_202(as_user, monkeypatch):
    monkeypatch.setattr(service, "create_pending_deep", lambda cid, **kw: "snap-123")
    import backend.routers.xray as xr

    monkeypatch.setattr(xr._DEEP_POOL, "submit", lambda *a, **k: None)
    client = as_user(_user(roles=["admin"]))
    r = client.post("/api/xray/999/deep", json={"focus": "renewal"})
    assert r.status_code == 202
    body = r.json()
    assert body["snapshot_id"] == "snap-123"
    assert body["status"] == "pending"


def test_router_deep_409_on_conflict(as_user, monkeypatch):
    def _raise(cid, **kw):
        raise service.XrayConflict("already running")

    monkeypatch.setattr(service, "create_pending_deep", _raise)
    client = as_user(_user(roles=["admin"]))
    r = client.post("/api/xray/999/deep", json={})
    assert r.status_code == 409


def test_router_runs_status(as_user, monkeypatch):
    import backend.routers.xray as xr

    monkeypatch.setattr(
        xr,
        "_load_snapshot_dict",
        lambda sid, with_timeline: {
            "company_id": "999",
            "status": "running",
            "progress": {"hubspot": "done", "web": "running"},
            "error": None,
        },
    )
    client = as_user(_user(roles=["admin"]))
    r = client.get("/api/xray/runs/snap-123")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert r.json()["progress"]["hubspot"] == "done"


def test_router_recent_route_ordering(as_user, monkeypatch):
    # GET /api/xray/recent must resolve to recent_xrays, NOT be captured by the
    # greedy /{company_id} route (which would 404 via latest_ready).
    import backend.routers.xray as xr

    monkeypatch.setattr(xr.persistence, "recent_ready", lambda db, limit=24: [])
    client = as_user(_user(roles=["admin"]))
    r = client.get("/api/xray/recent")
    assert r.status_code == 200
    assert r.json() == []


def _ready_snapshot(version: int):
    from datetime import datetime

    from rubik_core.database.models import XraySnapshot

    return XraySnapshot(
        id=f"snap-v{version}",
        company_id="999",
        company_name="Acme Supply, Inc.",
        version=version,
        kind="fast",
        status="ready",
        sections={"executive_summary": "Steady account."},
        signals={},
        created_at=datetime(2026, 9, 12, 14, 0),
    )


def test_router_docx_filename_carries_the_version(as_user, monkeypatch):
    # The X-Ray page can open any earlier version, and Word exports it by
    # snapshot id. Two versions of one customer must not download (or land on
    # the Assets page) under the same name.
    import backend.routers.xray as xr

    monkeypatch.setattr(xr, "_load_ready_snapshot", lambda sid: _ready_snapshot(3))
    client = as_user(_user(roles=["admin"]))
    r = client.get("/api/xray/snapshots/snap-v3/docx")
    assert r.status_code == 200
    assert (
        'filename="customer-xray-acme-supply-inc-v3.docx"'
        in r.headers["content-disposition"]
    )


def test_router_save_to_assets_names_the_version(as_user, monkeypatch):
    import backend.routers.xray as xr
    import backend.services.generated_docs as generated_docs

    saved: dict = {}

    class _Asset:
        doc_id = "gendoc-1"
        original_filename = "customer-xray-acme-supply-inc-v2.docx"

    def _save(user_email, **kw):
        saved.update(kw)
        return _Asset(), True

    monkeypatch.setattr(xr, "_load_ready_snapshot", lambda sid: _ready_snapshot(2))
    monkeypatch.setattr(generated_docs, "save_generated_doc", _save)
    client = as_user(_user(roles=["admin"]))
    r = client.post("/api/xray/snapshots/snap-v2/save-to-assets")
    assert r.status_code == 200
    assert saved["filename"] == "customer-xray-acme-supply-inc-v2.docx"
    assert saved["title"] == "Customer X-Ray — Acme Supply, Inc. (v2)"
    assert saved["source_company_id"] == "999"
