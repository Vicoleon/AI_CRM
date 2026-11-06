"""UC-45 — the HubSpot research-link writeback (``rubik_core/xray/crm_link.py``).

Every Fast/Deep run stamps the company record in HubSpot with a link back to its
Customer X-Ray page. What is under test is the set of ways that could go wrong
quietly:

  - the link is the STABLE company page, never a versioned snapshot URL. That is
    what lets the write happen in-request on the Service instead of at the end
    of a Deep run inside the ``cubie-xray-deep`` Job, which carries no write token.
  - a process without a real ``FRONTEND_URL`` writes NOTHING rather than the
    ``http://localhost:5173`` default — the ``cubie-worker`` dead-link failure,
    except a dead link in the CRM outlives a dead link in an email.
  - the property is fenced separately from ``company_writable()``, so the Sales
    Call Capture extractor can never propose a value for it.
  - the write is skipped when HubSpot already agrees. This fires on every run
    and the value only ever changes once per company; without the skip it is a
    wasted write per run against the 190-per-10s budget.
  - nothing here can fail a research run: a missing property, a HubSpot outage,
    an unset write token all return None quietly.

No live HubSpot: the write client's ``get_object`` and ``_request`` are stubbed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from rubik_core.config import settings
from rubik_core.retrieval import hubspot_write as hw
from rubik_core.retrieval.hubspot_write import (
    RESEARCH_LINK_PROPERTY,
    RESEARCH_LINK_WRITABLE,
    HubSpotWriteClient,
    HubSpotWriteError,
    HubSpotWriteRefused,
    company_writable,
)
from rubik_core.tenancy.hubspot_config import (
    HubSpotConfigInvalid,
    HubSpotConnectorConfig,
)
from rubik_core.xray import crm_link

COMPANY_ID = "8412334521"
ORIGIN = "https://cubie.acme.example"
EXPECTED = f"{ORIGIN}/customers/{COMPANY_ID}/xray"


@pytest.fixture(autouse=True)
def _workspace_pipeline(monkeypatch):
    monkeypatch.setattr(
        hw,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )


@pytest.fixture
def app_origin(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", ORIGIN)
    monkeypatch.setattr(settings, "XRAY_CRM_LINK_ENABLED", True)


class _Recorder:
    """Stands in for ``HubSpotWriteClient._request``; records what would be sent."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method: str, path: str, payload: dict) -> dict:
        self.calls.append((method, path, payload))
        return {"id": COMPANY_ID}


def _stub_client(monkeypatch, *, existing: str | None = None, read_exc=None):
    """Patch ``crm_link``'s client construction; return (recorder, get_object)."""
    recorder = _Recorder()
    if read_exc is not None:
        get_object = MagicMock(side_effect=read_exc)
    else:
        props = {RESEARCH_LINK_PROPERTY: existing} if existing is not None else {}
        get_object = MagicMock(return_value={"id": COMPANY_ID, "properties": props})

    read_client = MagicMock()
    read_client.fully_owned_deal_associations.return_value = {
        COMPANY_ID: ["owned-deal"]
    }
    client = HubSpotWriteClient(token="write-token", read_client=read_client)
    monkeypatch.setattr(client, "_request", recorder)
    monkeypatch.setattr(client, "get_object", get_object)
    monkeypatch.setattr(crm_link, "HubSpotWriteClient", lambda *a, **k: client)
    return recorder, get_object


# ── The URL itself ───────────────────────────────────────────────────────────
def test_link_is_the_stable_company_page(app_origin):
    """No version, no snapshot id — the page that renders the LATEST research.

    A versioned URL would rot into a pointer at a stale brief AND would have to
    be written when a Deep run finishes, i.e. from the ``cubie-xray-deep`` Job, which
    holds no write token by design.
    """
    assert crm_link.research_link_url(COMPANY_ID) == EXPECTED


def test_url_matches_the_spa_route():
    """Guards the one thing a frontend refactor would silently break.

    `frontend/src/App.tsx` mounts `customers/:companyId/xray`. If that path
    moves, this constant has to move with it or every link in the CRM 404s.
    """
    assert crm_link._XRAY_PATH == "/customers/{company_id}/xray"


@pytest.mark.parametrize(
    "frontend_url",
    [
        "http://localhost:5173",  # the config DEFAULT — the dangerous one
        "http://127.0.0.1:8000",
        "https://localhost",
        "http://mac-mini.local:5173",
        "",
        "cubie.acme.example",  # no scheme
    ],
)
def test_no_link_without_a_real_public_origin(monkeypatch, frontend_url):
    """Write nothing rather than a link that is dead for everyone who clicks it."""
    monkeypatch.setattr(settings, "FRONTEND_URL", frontend_url)
    assert crm_link.research_link_url(COMPANY_ID) is None


def test_trailing_slash_does_not_double_up(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", ORIGIN + "/")
    assert crm_link.research_link_url(COMPANY_ID) == EXPECTED


@pytest.mark.parametrize("bad", ["", "abc", "../../evil", "84123/x", "8412 334"])
def test_non_numeric_company_id_yields_no_link(app_origin, bad):
    """HubSpot ids are numeric; anything else is interpolated into a URL and a
    request path, so it is refused before either is built."""
    assert crm_link.research_link_url(bad) is None


# ── The write fence ──────────────────────────────────────────────────────────
def test_research_link_is_not_in_the_call_capture_fence():
    """The separation that keeps an approval-free write from spreading.

    ``company_writable()`` is what Sales Call Capture's transcript extractor may
    propose. This property has no approval step because it carries no model
    output — it must therefore stay out of reach of anything that does, even
    when somebody lists it as a vendor field.
    """
    with pytest.raises(HubSpotConfigInvalid):
        HubSpotConnectorConfig(ccs_vendor_fields_default=(RESEARCH_LINK_PROPERTY,))
    assert RESEARCH_LINK_PROPERTY not in company_writable()
    assert RESEARCH_LINK_WRITABLE == frozenset({RESEARCH_LINK_PROPERTY})


def test_write_refused_without_a_token():
    writer = HubSpotWriteClient(token=None, read_client=MagicMock())
    with pytest.raises(HubSpotWriteRefused):
        writer.update_company_research_link(COMPANY_ID, EXPECTED)


def test_write_refused_for_non_numeric_company_id(monkeypatch):
    writer = HubSpotWriteClient(token="t", read_client=MagicMock())
    monkeypatch.setattr(writer, "_request", _Recorder())
    with pytest.raises(HubSpotWriteRefused):
        writer.update_company_research_link("../../evil", EXPECTED)


def test_write_refused_for_a_non_http_value(monkeypatch):
    writer = HubSpotWriteClient(token="t", read_client=MagicMock())
    monkeypatch.setattr(writer, "_request", _Recorder())
    with pytest.raises(HubSpotWriteRefused):
        writer.update_company_research_link(COMPANY_ID, "javascript:alert(1)")


def test_write_is_one_patch_on_crm_v3(monkeypatch):
    """One property, one PATCH, v3 path (a mixed version 404s)."""
    read_client = MagicMock()
    read_client.fully_owned_deal_associations.return_value = {
        COMPANY_ID: ["owned-deal"]
    }
    writer = HubSpotWriteClient(token="t", read_client=read_client)
    recorder = _Recorder()
    monkeypatch.setattr(writer, "_request", recorder)
    writer.update_company_research_link(COMPANY_ID, EXPECTED)
    assert recorder.calls == [
        (
            "PATCH",
            f"/crm/v3/objects/companies/{COMPANY_ID}",
            {"properties": {RESEARCH_LINK_PROPERTY: EXPECTED}},
        )
    ]


# ── publish_research_link ────────────────────────────────────────────────────
def test_publishes_when_hubspot_is_empty(app_origin, monkeypatch):
    recorder, get_object = _stub_client(monkeypatch)
    assert crm_link.publish_research_link(COMPANY_ID) == EXPECTED
    get_object.assert_called_once_with(
        "companies", COMPANY_ID, [RESEARCH_LINK_PROPERTY]
    )
    assert len(recorder.calls) == 1


def test_skips_the_write_when_hubspot_already_agrees(app_origin, monkeypatch):
    """The quota guard. Runs are repeated; the value is not."""
    recorder, _ = _stub_client(monkeypatch, existing=EXPECTED)
    assert crm_link.publish_research_link(COMPANY_ID) == EXPECTED
    assert recorder.calls == []


def test_repairs_a_stale_value(app_origin, monkeypatch):
    """Someone edited it, or the origin moved. Ours wins — it is derived, not typed."""
    recorder, _ = _stub_client(monkeypatch, existing="https://old.example.com/x")
    assert crm_link.publish_research_link(COMPANY_ID) == EXPECTED
    assert recorder.calls[0][2]["properties"][RESEARCH_LINK_PROPERTY] == EXPECTED


def test_missing_property_is_survivable_and_self_healing(app_origin, monkeypatch):
    """Rollout is order-independent: the property can be created after the deploy.

    HubSpot 400s the read until someone creates it. We log one actionable line
    and skip — no exception, no write attempt, and the next run just works once
    the property exists.
    """
    exc = HubSpotWriteError(
        f"HubSpot read of companies {COMPANY_ID} failed: HTTP 400: "
        f'{{"message":"Property \\"{RESEARCH_LINK_PROPERTY}\\" does not exist"}}'
    )
    recorder, _ = _stub_client(monkeypatch, read_exc=exc)
    warning = MagicMock()
    monkeypatch.setattr(crm_link.logger, "warning", warning)
    assert crm_link.publish_research_link(COMPANY_ID) is None
    assert recorder.calls == []
    warning.assert_called_once()
    assert (
        RESEARCH_LINK_PROPERTY in warning.call_args.args[0] % warning.call_args.args[1:]
    )


def test_missing_property_is_reported_when_hubspot_rejects_the_WRITE(
    app_origin, monkeypatch
):
    """Same operator-facing message whichever endpoint rejects it.

    HubSpot's object GET may ignore an unknown property name and 200 instead of
    400, in which case the "property does not exist" 400 surfaces on the PATCH.
    Which one it is must not decide whether the log tells you what to do.
    """
    _stub_client(monkeypatch)
    client = crm_link.HubSpotWriteClient()
    monkeypatch.setattr(
        client,
        "update_company_research_link",
        MagicMock(
            side_effect=HubSpotWriteError(
                f"HubSpot PATCH /crm/v3/objects/companies/{COMPANY_ID} failed: "
                f'HTTP 400: {{"message":"Property "{RESEARCH_LINK_PROPERTY}" '
                'does not exist"}'
            )
        ),
    )
    monkeypatch.setattr(crm_link, "HubSpotWriteClient", lambda *a, **k: client)
    warning = MagicMock()
    monkeypatch.setattr(crm_link.logger, "warning", warning)
    assert crm_link.publish_research_link(COMPANY_ID) is None
    warning.assert_called_once()
    assert "does not exist on the portal" in (
        warning.call_args.args[0] % warning.call_args.args[1:]
    )


def test_hubspot_read_outage_never_raises(app_origin, monkeypatch):
    recorder, _ = _stub_client(
        monkeypatch, read_exc=HubSpotWriteError("HubSpot transport error: timeout")
    )
    assert crm_link.publish_research_link(COMPANY_ID) is None
    assert recorder.calls == []


def test_write_failure_never_raises(app_origin, monkeypatch):
    _stub_client(monkeypatch)
    client = crm_link.HubSpotWriteClient()
    monkeypatch.setattr(
        client,
        "update_company_research_link",
        MagicMock(side_effect=HubSpotWriteError("HTTP 500")),
    )
    monkeypatch.setattr(crm_link, "HubSpotWriteClient", lambda *a, **k: client)
    assert crm_link.publish_research_link(COMPANY_ID) is None


def test_unset_write_token_is_inert_not_an_error(app_origin, monkeypatch):
    """A dev environment without the write token must not log exceptions on
    every run — the refusal is expected there, and it is swallowed."""
    monkeypatch.setattr(hw, "workspace_secret", lambda *_: None)
    real = HubSpotWriteClient(token=None, read_client=MagicMock())
    monkeypatch.setattr(
        real,
        "get_object",
        MagicMock(return_value={"id": COMPANY_ID, "properties": {}}),
    )
    monkeypatch.setattr(crm_link, "HubSpotWriteClient", lambda *a, **k: real)
    assert crm_link.publish_research_link(COMPANY_ID) is None


def test_kill_switch_touches_hubspot_not_at_all(app_origin, monkeypatch):
    monkeypatch.setattr(settings, "XRAY_CRM_LINK_ENABLED", False)
    recorder, get_object = _stub_client(monkeypatch)
    assert crm_link.publish_research_link(COMPANY_ID) is None
    get_object.assert_not_called()
    assert recorder.calls == []


def test_no_write_when_the_origin_is_unusable(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "http://localhost:5173")
    monkeypatch.setattr(settings, "XRAY_CRM_LINK_ENABLED", True)
    recorder, get_object = _stub_client(monkeypatch)
    assert crm_link.publish_research_link(COMPANY_ID) is None
    get_object.assert_not_called()
    assert recorder.calls == []


def test_module_is_the_only_caller_of_the_research_link_writer():
    """One caller, and only one.

    A second call site is how an approval-free CRM write quietly acquires a
    second set of rules; if one is ever added, this test is the conversation.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    hits = {
        path.relative_to(root).as_posix()
        for path in list(root.glob("rubik_core/**/*.py"))
        + list(root.glob("backend/**/*.py"))
        if "update_company_research_link" in path.read_text()
        and "tests/" not in path.relative_to(root).as_posix()
    }
    assert hits == {
        "rubik_core/retrieval/hubspot_write.py",
        "rubik_core/xray/crm_link.py",
    }


def test_the_writer_method_is_not_reachable_through_update_company(monkeypatch):
    """`update_company` must keep refusing the research-link property.

    Both methods PATCH the same object; only the fences differ. If the property
    ever leaks into `company_writable()` this is what catches it.
    """
    writer = HubSpotWriteClient(token="t", read_client=MagicMock())
    monkeypatch.setattr(writer, "_request", _Recorder())
    with pytest.raises(HubSpotWriteRefused):
        writer.update_company(COMPANY_ID, {RESEARCH_LINK_PROPERTY: EXPECTED})


def test_hubspot_write_module_exposes_the_fence():
    assert hw.RESEARCH_LINK_PROPERTY == "cubie_research_link"


# ── Router wiring: the stamp happens ON THE SERVICE, in-request ──────────────
# Why these live at the router and not in `service`: the write token is mounted
# on the `cubie` Service only. `run_deep_job` runs on the `cubie-xray-deep` Cloud
# Run Job, which carries the READ token alone by design, so the same call made
# from the service layer would be refused every time in prod and pass in tests.
@pytest.fixture
def as_admin(monkeypatch):
    from fastapi.testclient import TestClient

    import backend.routers.xray as xr
    from backend.auth import get_current_user
    from backend.main import app
    from rubik_core.auth.registry import UserProfile

    monkeypatch.setattr(xr.fetchers, "company_available", lambda cid: cid == COMPANY_ID)

    app.dependency_overrides[get_current_user] = lambda: UserProfile(
        id="u1",
        email="u1@acme.example",
        name="U One",
        department="sales",
        roles=["admin"],
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def spy(monkeypatch):
    """Record what the router asked to stamp, without touching HubSpot."""
    calls: list[str] = []
    import backend.routers.xray as xr

    monkeypatch.setattr(
        xr.crm_link, "publish_research_link", lambda cid: calls.append(cid)
    )
    return calls


def test_deep_run_stamps_the_link(as_admin, spy, monkeypatch):
    import backend.routers.xray as xr
    from rubik_core.xray import service as svc

    monkeypatch.setattr(svc, "create_pending_deep", lambda cid, **kw: "snap-1")
    monkeypatch.setattr(xr._DEEP_POOL, "submit", lambda *a, **k: None)
    r = as_admin.post(f"/api/xray/{COMPANY_ID}/deep", json={})
    assert r.status_code == 202
    assert spy == [COMPANY_ID]


def test_deep_conflict_stamps_nothing(as_admin, spy, monkeypatch):
    """409 means someone else's run owns this company right now — and that run
    already stamped. Nothing to do."""
    from rubik_core.xray import service as svc

    def _raise(cid, **kw):
        raise svc.XrayConflict("already running")

    monkeypatch.setattr(svc, "create_pending_deep", _raise)
    r = as_admin.post(f"/api/xray/{COMPANY_ID}/deep", json={})
    assert r.status_code == 409
    assert spy == []


def test_fast_run_stamps_the_link(as_admin, spy, monkeypatch):
    import backend.routers.xray as xr
    from rubik_core.xray import service as svc

    monkeypatch.setattr(svc, "run_fast", lambda cid, **kw: "snap-1")
    monkeypatch.setattr(
        xr, "_load_snapshot_dict", lambda sid, with_timeline: {"status": "ready"}
    )
    r = as_admin.post(f"/api/xray/{COMPANY_ID}/fast", json={})
    assert r.status_code == 200
    assert spy == [COMPANY_ID]


def test_fast_run_that_found_nothing_stamps_nothing(as_admin, spy, monkeypatch):
    """A company with no CRM data persists a `failed` snapshot. Linking reps to
    an empty page is worse than not linking them at all."""
    import backend.routers.xray as xr
    from rubik_core.xray import service as svc

    monkeypatch.setattr(svc, "run_fast", lambda cid, **kw: "snap-1")
    monkeypatch.setattr(
        xr,
        "_load_snapshot_dict",
        lambda sid, with_timeline: {"status": "failed", "error": "No CRM data"},
    )
    r = as_admin.post(f"/api/xray/{COMPANY_ID}/fast", json={})
    assert r.status_code == 200
    assert spy == []


def test_a_hubspot_failure_does_not_fail_the_run(as_admin, monkeypatch):
    """The brief is the deliverable; the CRM stamp is a courtesy."""
    import backend.routers.xray as xr
    from rubik_core.xray import service as svc

    monkeypatch.setattr(svc, "run_fast", lambda cid, **kw: "snap-1")
    monkeypatch.setattr(
        xr, "_load_snapshot_dict", lambda sid, with_timeline: {"status": "ready"}
    )
    monkeypatch.setattr(settings, "FRONTEND_URL", ORIGIN)
    monkeypatch.setattr(settings, "XRAY_CRM_LINK_ENABLED", True)
    boom = MagicMock(side_effect=HubSpotWriteError("HubSpot is down"))
    client = HubSpotWriteClient(token="t", read_client=MagicMock())
    monkeypatch.setattr(client, "get_object", boom)
    monkeypatch.setattr(crm_link, "HubSpotWriteClient", lambda *a, **k: client)

    r = as_admin.post(f"/api/xray/{COMPANY_ID}/fast", json={})
    assert r.status_code == 200
    boom.assert_called_once()
