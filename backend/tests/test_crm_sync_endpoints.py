"""UC-46 — the review/approve endpoints.

The security-relevant contract: an AE can only ever see and act on their own
suggestions, one approval means exactly one write, and a deal that moved since
the scan gets re-confirmed rather than silently overwritten.

Uses an in-memory SQLite DB so nothing touches deployed Cloud SQL (the known
test_pollution_bug — a module-level SessionLocal would otherwise read .env's
DATABASE_URL).

Covers:
  - IDOR: another user's suggestion 404s on read, approve and dismiss
  - CAS: a second approve of the same row 409s; one write happened
  - a stuck `applying` row is reaped to `failed` so it can be retried
  - approve/dismiss each write an audit row with before/after
  - edits are whitelist-checked (422) and the edited value is what's written
  - dealstage drift → 409 and the card is re-anchored on live state
  - the feature flag and the department gate both 403
  - scan lifecycle: 202, 409 while running, lazy timeout, cross-user poll 404
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user, require_crm_sync
from backend.main import app
from backend.tests._rosters import narrow_copilot, pin_rosters
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.models import AuditLog, Base, CrmSyncRun, CrmSyncSuggestion
from rubik_core.features import FeaturePolicy

AE = "ae@acme.example"
OTHER = "other@acme.example"

client = TestClient(app)


def _profile(
    email: str,
    department: str = "sales",
    is_admin: bool = False,
    is_exec: bool = False,
    managed_departments: list[str] | None = None,
):
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name="Test User",
        department=department,
        roles=["admin"] if is_admin else ["user"],  # is_admin is derived from roles
        is_exec=is_exec,
        managed_departments=managed_departments or [],
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


@pytest.fixture
def db_session(monkeypatch):
    from backend.routers import crm_sync
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    class OwnedHubSpot:
        def batch_read(self, _object_type, ids, _properties):
            return [{"id": item, "properties": {"pipeline": "default"}} for item in ids]

        def fully_owned_deal_associations(self, _object_type, ids):
            return {item: ["owned-deal"] for item in ids}

    monkeypatch.setattr(
        crm_sync,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )
    monkeypatch.setattr(crm_sync, "HubSpotAPIClient", OwnedHubSpot)
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()

    monkeypatch.setattr(settings, "CUBIE_CRM_SYNC_ENABLED", True)
    monkeypatch.setattr("rubik_core.crm_sync.run_state.SessionLocal", Session)
    monkeypatch.setattr("rubik_core.crm_sync.service.SessionLocal", Session)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _profile(AE)
    try:
        yield db
    finally:
        app.dependency_overrides.clear()
        db.close()


def _suggestion(db, **kw) -> CrmSyncSuggestion:
    defaults = dict(
        id="sug-1",
        user_email=AE,
        run_id="run-1",
        doc_id="doc-1",
        meeting_title="Acme call",
        meeting_date=datetime(2026, 8, 1),
        change_type="update_contact",
        hubspot_object_type="contact",
        hubspot_object_id="c1",
        target_key="update_contact:contact:c1",
        dedupe_key="update_contact:contact:c1:abc",
        payload={"properties": {"phone": {"before": "555-0100", "after": "555-0143"}}},
        evidence={"quote": "My direct line is 555-0143"},
        confidence=0.9,
        status="pending",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    defaults.update(kw)
    row = CrmSyncSuggestion(**defaults)
    db.add(row)
    db.commit()
    return row


def _writer(**kw) -> MagicMock:
    writer = MagicMock()
    writer.update_contact.return_value = {"id": "c1", "_mode": "updated"}
    writer.update_deal.return_value = {"id": "d1", "_mode": "updated"}
    writer.create_contact.return_value = {"id": "c9", "_mode": "created"}
    for key, value in kw.items():
        setattr(writer, key, value)
    return writer


def _patch_writer(writer):
    return patch(
        "rubik_core.crm_sync.apply.HubSpotWriteClient", MagicMock(return_value=writer)
    )


# ─── IDOR ────────────────────────────────────────────────────────────────────


def test_the_list_never_includes_another_users_suggestions(db_session):
    _suggestion(db_session, id="mine")
    _suggestion(db_session, id="theirs", user_email=OTHER, dedupe_key="other:key")

    body = client.get("/api/crm-sync/suggestions").json()

    assert [s["id"] for s in body["suggestions"]] == ["mine"]


@pytest.mark.parametrize("retired", ["log_note", "a_type_retired_later"])
def test_a_stored_card_of_a_type_the_scan_no_longer_produces_is_not_listed(
    db_session, retired
):
    """A card queued before its change type was retired stays out of the list,
    rather than showing as a card the page has no rendering for."""
    _suggestion(db_session, id="contact")
    _suggestion(
        db_session,
        id="retired",
        change_type=retired,
        target_key=f"{retired}:contact:c1|field",
        dedupe_key=f"{retired}:contact:c1|field:abc",
    )

    body = client.get("/api/crm-sync/suggestions?status=all").json()

    assert [s["id"] for s in body["suggestions"]] == ["contact"]


def test_approving_another_users_suggestion_404s_and_changes_nothing(db_session):
    row = _suggestion(db_session, id="theirs", user_email=OTHER, dedupe_key="k")
    writer = _writer()

    with _patch_writer(writer):
        resp = client.post("/api/crm-sync/suggestions/theirs/approve")

    assert resp.status_code == 404
    db_session.refresh(row)
    assert row.status == "pending"
    writer.update_contact.assert_not_called()


def test_dismissing_another_users_suggestion_changes_nothing(db_session):
    row = _suggestion(db_session, id="theirs", user_email=OTHER, dedupe_key="k")

    resp = client.post("/api/crm-sync/suggestions/theirs/dismiss")

    assert resp.status_code == 404
    db_session.refresh(row)
    assert row.status == "pending"


# ─── Approve: exactly once ───────────────────────────────────────────────────


def test_approve_writes_once_and_records_an_audit_row(db_session):
    _suggestion(db_session)
    writer = _writer()

    with _patch_writer(writer):
        resp = client.post("/api/crm-sync/suggestions/sug-1/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    writer.update_contact.assert_called_once_with("c1", {"phone": "555-0143"})

    audit = db_session.query(AuditLog).filter(AuditLog.action == "CRM_SYNC_APPLY").one()
    assert audit.actor_email == AE
    assert audit.status == "success"
    assert audit.details["before"] == {"phone": "555-0100"}
    assert audit.details["after"] == {"phone": "555-0143"}


def test_a_second_approve_conflicts_and_does_not_write_twice(db_session):
    _suggestion(db_session)
    writer = _writer()

    with _patch_writer(writer):
        first = client.post("/api/crm-sync/suggestions/sug-1/approve")
        second = client.post("/api/crm-sync/suggestions/sug-1/approve")

    assert first.status_code == 200
    assert second.status_code == 409
    assert writer.update_contact.call_count == 1


def test_a_hubspot_refusal_leaves_a_retryable_failure_with_readable_text(db_session):
    from rubik_core.retrieval.hubspot_write import HubSpotWriteRefused

    _suggestion(db_session)
    writer = _writer(
        update_contact=MagicMock(side_effect=HubSpotWriteRefused("nope, gate said no"))
    )

    with _patch_writer(writer):
        resp = client.post("/api/crm-sync/suggestions/sug-1/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert resp.json()["error"] == "nope, gate said no"
    audit = db_session.query(AuditLog).filter(AuditLog.action == "CRM_SYNC_APPLY").one()
    assert audit.status == "failure"


def test_a_failed_row_can_be_retried(db_session):
    _suggestion(db_session, status="failed", error="HubSpot was rate-limiting")
    writer = _writer()

    with _patch_writer(writer):
        resp = client.post("/api/crm-sync/suggestions/sug-1/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"


def test_a_stuck_applying_row_is_reaped_so_it_can_be_retried(db_session):
    row = _suggestion(
        db_session,
        status="applying",
        updated_at=datetime.utcnow() - timedelta(minutes=10),
    )

    body = client.get("/api/crm-sync/suggestions?status=all").json()

    db_session.refresh(row)
    assert row.status == "failed"
    assert "Check HubSpot before retrying" in row.error
    assert body["suggestions"][0]["status"] == "failed"


# ─── Edits ───────────────────────────────────────────────────────────────────


def test_an_edited_value_is_what_gets_written(db_session):
    _suggestion(db_session)
    writer = _writer()

    with _patch_writer(writer):
        resp = client.post(
            "/api/crm-sync/suggestions/sug-1/approve",
            json={"edited_after": {"phone": "555-9999"}},
        )

    assert resp.status_code == 200
    writer.update_contact.assert_called_once_with("c1", {"phone": "555-9999"})


def test_editing_a_field_outside_the_whitelist_is_rejected(db_session):
    row = _suggestion(db_session)
    writer = _writer()

    with _patch_writer(writer):
        resp = client.post(
            "/api/crm-sync/suggestions/sug-1/approve",
            json={"edited_after": {"hs_all_owner_ids": "1"}},
        )

    assert resp.status_code == 422
    db_session.refresh(row)
    assert row.status == "pending"  # still actionable
    writer.update_contact.assert_not_called()


# ─── Drift ───────────────────────────────────────────────────────────────────


def _deal_suggestion(db):
    return _suggestion(
        db,
        change_type="update_deal",
        hubspot_object_type="deal",
        hubspot_object_id="d1",
        target_key="update_deal:deal:d1",
        payload={
            "properties": {
                "dealstage": {
                    "before": "appointmentscheduled",
                    "after": "presentationscheduled",
                }
            }
        },
    )


def test_a_stage_that_moved_since_the_scan_blocks_and_re_anchors_the_card(db_session):
    row = _deal_suggestion(db_session)
    writer = _writer()
    writer.get_object.return_value = {"properties": {"dealstage": "contractsent"}}

    with _patch_writer(writer):
        resp = client.post("/api/crm-sync/suggestions/sug-1/approve")

    assert resp.status_code == 409
    assert "changed in HubSpot" in resp.json()["detail"]
    db_session.refresh(row)
    assert row.status == "pending"
    # Re-anchored on reality so the AE decides against what's actually there.
    assert row.payload["properties"]["dealstage"]["before"] == "contractsent"
    writer.update_deal.assert_not_called()


def test_an_unchanged_stage_applies_normally(db_session):
    _deal_suggestion(db_session)
    writer = _writer()
    writer.get_object.return_value = {
        "properties": {"dealstage": "appointmentscheduled"}
    }

    with _patch_writer(writer):
        resp = client.post("/api/crm-sync/suggestions/sug-1/approve")

    assert resp.status_code == 200
    writer.update_deal.assert_called_once()


def test_non_stage_drift_does_not_block_an_approval(db_session):
    """The AE approved a phone number; a concurrent edit isn't a veto."""
    _suggestion(db_session)
    writer = _writer()

    with _patch_writer(writer):
        resp = client.post("/api/crm-sync/suggestions/sug-1/approve")

    assert resp.status_code == 200
    writer.get_object.assert_not_called()


# ─── Dismiss ─────────────────────────────────────────────────────────────────


def test_dismiss_marks_the_row_and_audits_it(db_session):
    _suggestion(db_session)

    resp = client.post(
        "/api/crm-sync/suggestions/sug-1/dismiss", json={"reason": "already handled"}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"
    audit = (
        db_session.query(AuditLog).filter(AuditLog.action == "CRM_SYNC_DISMISS").one()
    )
    assert audit.details["reason"] == "already handled"


# ─── Access gates ────────────────────────────────────────────────────────────


def test_every_endpoint_403s_when_the_feature_flag_is_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "CUBIE_CRM_SYNC_ENABLED", False)

    assert client.get("/api/crm-sync/suggestions").status_code == 403
    assert client.post("/api/crm-sync/scan").status_code == 403
    assert client.post("/api/crm-sync/suggestions/sug-1/approve").status_code == 403


NAMED_REVIEWER = "named.reviewer@acme.example"


@pytest.fixture
def narrowed_rosters(monkeypatch):
    """CRM Sync ships open to everyone. These cases install the roster an admin
    would narrow it to — Sales + Systems, one reviewer by name, the Exec flag
    off — and a copilot narrowed to four teams, as Feature Access data."""
    pin_rosters(
        monkeypatch,
        {
            "crm_sync": FeaturePolicy(
                departments=frozenset({"sales", "systems"}),
                extra_emails=frozenset({NAMED_REVIEWER}),
                exec_allowed=False,
                admin_allowed=True,
            ),
        },
    )
    narrow_copilot(monkeypatch, "Sales", "Marketing", "Systems", "Support")


def test_a_copilot_user_outside_the_roster_is_denied_by_the_crm_sync_gate(
    db_session, narrowed_rosters
):
    """Marketing clears the outer copilot gate, so this exercises OUR gate."""
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "someone@acme.example", department="marketing"
    )

    resp = client.get("/api/crm-sync/suggestions")

    assert resp.status_code == 403
    assert "CRM Sync is not turned on for you" in resp.json()["detail"]


def test_a_user_outside_the_copilot_entirely_is_denied_by_the_outer_gate(
    db_session, narrowed_rosters
):
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "someone@acme.example", department="operations"
    )

    assert client.get("/api/crm-sync/suggestions").status_code == 403


def test_an_admin_outside_sales_is_allowed(db_session, narrowed_rosters):
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "admin@acme.example", department="systems", is_admin=True
    )

    assert client.get("/api/crm-sync/suggestions").status_code == 200


# ─── Scan lifecycle ──────────────────────────────────────────────────────────


@pytest.fixture
def _no_job_launch(monkeypatch):
    """Keep the launcher from touching GCP; record what it was asked to run."""
    calls: list[dict] = []

    def fake_submit(background_tasks, *, module, args, fallback, label, **kw):
        calls.append({"module": module, "args": args, "label": label})
        return "job", "executions/fake"

    monkeypatch.setattr("backend.jobs.launcher.submit_background_work", fake_submit)
    return calls


def test_scan_returns_202_and_records_the_run(db_session, _no_job_launch):
    resp = client.post("/api/crm-sync/scan")

    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    row = db_session.query(CrmSyncRun).filter(CrmSyncRun.run_id == run_id).one()
    assert row.user_email == AE
    assert row.status == "pending"
    # The module must be re-supplied as `-m <module>` by the launcher itself;
    # here we only assert the args carry the identifying run.
    assert _no_job_launch[0]["module"] == "backend.jobs.crm_sync_job"
    assert run_id in _no_job_launch[0]["args"]


def test_a_second_scan_while_one_is_running_conflicts(db_session, _no_job_launch):
    first = client.post("/api/crm-sync/scan")
    second = client.post("/api/crm-sync/scan")

    assert first.status_code == 202
    assert second.status_code == 409
    assert len(_no_job_launch) == 1


def test_a_zombie_run_stops_blocking_and_reports_failed(db_session, _no_job_launch):
    resp = client.post("/api/crm-sync/scan")
    run_id = resp.json()["run_id"]
    row = db_session.query(CrmSyncRun).filter(CrmSyncRun.run_id == run_id).one()
    row.queued_at = datetime.utcnow() - timedelta(seconds=1000)
    db_session.commit()

    polled = client.get(f"/api/crm-sync/runs/{run_id}").json()

    assert polled["status"] == "failed"
    assert "timed out" in polled["error"]
    assert client.post("/api/crm-sync/scan").status_code == 202


def test_polling_another_users_run_404s(db_session, _no_job_launch):
    run_id = client.post("/api/crm-sync/scan").json()["run_id"]
    app.dependency_overrides[get_current_user] = lambda: _profile(OTHER)

    assert client.get(f"/api/crm-sync/runs/{run_id}").status_code == 404


def test_the_portal_id_is_passed_through_for_deep_links(db_session, monkeypatch):
    from backend.routers import crm_sync
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    monkeypatch.setattr(
        crm_sync,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(portal_id="12345"),
    )
    assert (
        client.get("/api/crm-sync/suggestions").json()["hubspot_portal_id"] == "12345"
    )

    monkeypatch.setattr(crm_sync, "get_hubspot_config", HubSpotConnectorConfig)
    assert client.get("/api/crm-sync/suggestions").json()["hubspot_portal_id"] is None


def test_unconfigured_workspace_hides_stored_crm_suggestions_without_hubspot_calls(
    db_session, monkeypatch
):
    from backend.routers import crm_sync
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    _suggestion(db_session, id="old-contact")
    monkeypatch.setattr(crm_sync, "get_hubspot_config", HubSpotConnectorConfig)
    monkeypatch.setattr(
        crm_sync,
        "HubSpotAPIClient",
        lambda: pytest.fail("unconfigured workspace must not call HubSpot"),
    )

    response = client.get("/api/crm-sync/suggestions")

    assert response.status_code == 200
    assert response.json()["suggestions"] == []


def test_narrowed_workspace_hides_old_foreign_deal_and_contact_suggestions(
    db_session, monkeypatch
):
    from backend.routers import crm_sync

    _suggestion(
        db_session,
        id="old-deal",
        change_type="update_deal",
        hubspot_object_type="deal",
        hubspot_object_id="foreign-deal",
    )
    _suggestion(
        db_session,
        id="old-contact",
        hubspot_object_id="foreign-contact",
        target_key="update_contact:contact:foreign-contact",
        dedupe_key="update_contact:contact:foreign-contact:abc",
    )

    class ForeignHubSpot:
        def batch_read(self, _object_type, _ids, _properties):
            return []

        def fully_owned_deal_associations(self, _object_type, ids):
            return {item: [] for item in ids}

    monkeypatch.setattr(crm_sync, "HubSpotAPIClient", ForeignHubSpot)
    response = client.get("/api/crm-sync/suggestions")

    assert response.status_code == 200
    assert response.json()["suggestions"] == []


@pytest.mark.parametrize("action", ["approve", "dismiss"])
@pytest.mark.parametrize("object_type", ["contact", "deal"])
def test_retained_foreign_suggestion_id_cannot_act_or_reveal_stored_values(
    db_session, monkeypatch, action, object_type
):
    from backend.routers import crm_sync

    if object_type == "deal":
        row = _deal_suggestion(db_session)
    else:
        row = _suggestion(db_session)

    class ForeignHubSpot:
        def batch_read(self, _object_type, ids, _properties):
            return [
                {"id": item, "properties": {"pipeline": "another-workspace"}}
                for item in ids
            ]

        def fully_owned_deal_associations(self, _object_type, _ids):
            return {}

    monkeypatch.setattr(crm_sync, "HubSpotAPIClient", ForeignHubSpot)
    writer = _writer()
    with _patch_writer(writer):
        response = client.post(f"/api/crm-sync/suggestions/{row.id}/{action}")

    assert response.status_code == 404
    assert "555-0100" not in response.text
    assert "My direct line" not in response.text
    db_session.refresh(row)
    assert row.status == "pending"
    writer.update_contact.assert_not_called()
    writer.update_deal.assert_not_called()


@pytest.mark.parametrize("action", ["approve", "dismiss"])
def test_unconfigured_workspace_refuses_actions_without_provider_calls(
    db_session, monkeypatch, action
):
    from backend.routers import crm_sync
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    _suggestion(db_session)
    monkeypatch.setattr(crm_sync, "get_hubspot_config", HubSpotConnectorConfig)
    monkeypatch.setattr(
        crm_sync,
        "HubSpotAPIClient",
        lambda: pytest.fail("unconfigured workspace must not call HubSpot"),
    )

    response = client.post(f"/api/crm-sync/suggestions/sug-1/{action}")

    assert response.status_code == 404


def test_failed_live_proof_returns_unavailable_without_stored_values(
    db_session, monkeypatch
):
    from backend.routers import crm_sync

    _suggestion(db_session)

    class UnavailableHubSpot:
        def fully_owned_deal_associations(self, _object_type, _ids):
            raise RuntimeError("HubSpot unavailable")

    monkeypatch.setattr(crm_sync, "HubSpotAPIClient", UnavailableHubSpot)

    response = client.post("/api/crm-sync/suggestions/sug-1/dismiss")

    assert response.status_code == 503
    assert "555-0100" not in response.text
    assert "My direct line" not in response.text


def test_action_result_is_hidden_if_contact_ownership_changes_during_dismiss(
    db_session, monkeypatch
):
    from backend.routers import crm_sync

    row = _suggestion(db_session)
    checks = 0

    class MovingHubSpot:
        def fully_owned_deal_associations(self, _object_type, ids):
            nonlocal checks
            checks += 1
            return {item: ["owned-deal"] for item in ids} if checks == 1 else {}

    monkeypatch.setattr(crm_sync, "HubSpotAPIClient", MovingHubSpot)

    response = client.post("/api/crm-sync/suggestions/sug-1/dismiss")

    assert checks == 2
    assert response.status_code == 404
    assert "555-0100" not in response.text
    db_session.refresh(row)
    assert row.status == "dismissed"


# Keep the module-level import used by the gate override honest.
assert require_crm_sync is not None


# ─── Edited values get the same normalisation as proposed ones ───────────────


def _deal_amount_suggestion(db):
    return _suggestion(
        db,
        change_type="update_deal",
        hubspot_object_type="deal",
        hubspot_object_id="d1",
        target_key="update_deal:deal:d1|amount",
        payload={"properties": {"amount": {"before": "30000", "after": "45000"}}},
    )


def test_a_hand_typed_currency_amount_is_normalised_before_it_reaches_hubspot(
    db_session,
):
    _deal_amount_suggestion(db_session)
    writer = _writer()

    with _patch_writer(writer):
        resp = client.post(
            "/api/crm-sync/suggestions/sug-1/approve",
            json={"edited_after": {"amount": "$52,500"}},
        )

    assert resp.status_code == 200
    writer.update_deal.assert_called_once_with(
        "d1", {"amount": "52500"}, acting_owner_email=AE
    )


def test_an_implausible_hand_typed_close_date_is_rejected(db_session):
    _suggestion(
        db_session,
        change_type="update_deal",
        hubspot_object_type="deal",
        hubspot_object_id="d1",
        target_key="update_deal:deal:d1|closedate",
        payload={"properties": {"closedate": {"before": None, "after": "2026-09-30"}}},
    )
    writer = _writer()

    with _patch_writer(writer):
        resp = client.post(
            "/api/crm-sync/suggestions/sug-1/approve",
            json={"edited_after": {"closedate": "2099-01-01"}},
        )

    assert resp.status_code == 422
    writer.update_deal.assert_not_called()


# ─── Who can see CRM Sync ────────────────────────────────────────────────────


def test_systems_can_use_crm_sync(db_session):
    """Systems supports the feature, so they're inside the gate."""
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "sysperson@acme.example", department="systems"
    )

    assert client.get("/api/crm-sync/suggestions").status_code == 200


def test_a_named_reviewer_can_use_crm_sync_without_a_department(
    db_session, narrowed_rosters
):
    """How one exec reviews it without opening the feature to every exec."""
    app.dependency_overrides[get_current_user] = lambda: _profile(
        NAMED_REVIEWER.replace("named", "Named"), department="executive", is_exec=True
    )

    assert client.get("/api/crm-sync/suggestions").status_code == 200


def test_being_an_exec_alone_does_not_grant_crm_sync(db_session, narrowed_rosters):
    """Access follows the work, not seniority — an exec off the roster is out."""
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "otherexec@acme.example", department="operations", is_exec=True
    )

    assert client.get("/api/crm-sync/suggestions").status_code == 403


def test_a_sales_team_manager_sitting_elsewhere_still_qualifies(
    db_session, narrowed_rosters
):
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "manager@acme.example",
        department="operations",
        managed_departments=["Sales"],
    )

    assert client.get("/api/crm-sync/suggestions").status_code == 200
