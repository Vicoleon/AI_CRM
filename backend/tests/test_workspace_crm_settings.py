"""HubSpot connector configuration belongs to the selected control workspace."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from sqlalchemy import delete

from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.tenancy.context import NoWorkspaceContext, use_workspace


@pytest.fixture
def config_workspaces():
    with control_session() as session:
        session.add_all(
            [
                Workspace(slug="acme-config", name="Acme", status="active"),
                Workspace(slug="beta-config", name="Beta", status="active"),
            ]
        )
        session.commit()
    try:
        yield
    finally:
        with control_session() as session:
            session.execute(
                delete(WorkspaceConnector).where(
                    WorkspaceConnector.workspace_slug.in_(
                        ("acme-config", "beta-config")
                    )
                )
            )
            for slug in ("acme-config", "beta-config"):
                row = session.get(Workspace, slug)
                if row is not None:
                    session.delete(row)
            session.commit()


def test_crm_settings_are_workspace_owned_and_preserve_secret_refs(config_workspaces):
    from rubik_core.tenancy.hubspot_config import (
        HubSpotConnectorConfig,
        get_hubspot_config,
        save_hubspot_config,
    )

    with control_session() as session:
        session.add(
            WorkspaceConnector(
                workspace_slug="acme-config",
                connector="hubspot",
                config={"future_key": "keep"},
                secret_refs={"read_token": "ws-acme-config-hubspot-read_token"},
            )
        )
        session.commit()

    with use_workspace("acme-config"):
        acme = HubSpotConnectorConfig(
            pipeline_ids=("default", "acme_sales"),
            closed_won_stage_ids=("closedwon", "5077"),
            stage_lag_stages="draft,sow=sow_sent,legal=moved_to_legal_review",
            portal_id="12345",
            deal_product_property="product_line",
            xray_auto_renew_properties=("auto_renew",),
            ccs_vendor_fields_default=("payroll_vendor",),
            sales_rhythm_late_stages=("Proposal Sent",),
            scorecard_sync_department="Sales",
            scorecard_sync_metrics={"Revenue": "company_won_amount"},
        )
        assert save_hubspot_config(acme) == acme
        assert get_hubspot_config() == acme

    with use_workspace("beta-config"):
        default = get_hubspot_config()
        assert default.pipeline_ids == ()
        assert default.closed_won_stage_ids == ()
        assert default.portal_id is None
        assert default.ccs_vendor_fields_default == ()
        assert default.scorecard_sync_metrics == {}
        beta = HubSpotConnectorConfig(pipeline_ids=("beta",), portal_id="98765")
        save_hubspot_config(beta)
        assert get_hubspot_config() == beta

    with use_workspace("acme-config"):
        assert get_hubspot_config() == acme
    with control_session() as session:
        row = session.get(WorkspaceConnector, ("acme-config", "hubspot"))
        assert row.secret_refs == {"read_token": "ws-acme-config-hubspot-read_token"}
        assert row.config["future_key"] == "keep"


def test_partial_patch_merges_against_live_connector_row(config_workspaces):
    from rubik_core.tenancy.hubspot_config import (
        get_hubspot_config,
        patch_hubspot_config,
    )

    with use_workspace("acme-config"):
        patch_hubspot_config({"pipeline_ids": ["default"]})
        patch_hubspot_config({"portal_id": "12345"})
        config = get_hubspot_config()
        assert config.pipeline_ids == ("default",)
        assert config.portal_id == "12345"


def test_concurrent_partial_patches_keep_both_fields(config_workspaces, monkeypatch):
    from threading import Event, Thread, current_thread

    from rubik_core.tenancy import hubspot_config

    first_read = Event()
    release_first = Event()
    second_attempt = Event()
    second_done = Event()
    errors = []
    original_current = hubspot_config._current_config
    original_active = hubspot_config._active_workspace

    def paused_current(row):
        if current_thread().name == "config-first":
            first_read.set()
            if not release_first.wait(5):
                raise TimeoutError("First config patch timed out")
        return original_current(row)

    def traced_active(session, slug, *, lock):
        if current_thread().name == "config-second":
            second_attempt.set()
        return original_active(session, slug, lock=lock)

    monkeypatch.setattr(hubspot_config, "_current_config", paused_current)
    monkeypatch.setattr(hubspot_config, "_active_workspace", traced_active)

    def patch(changes):
        try:
            with use_workspace("acme-config"):
                hubspot_config.patch_hubspot_config(changes)
        except Exception as exc:
            errors.append(exc)
        finally:
            if current_thread().name == "config-second":
                second_done.set()

    first = Thread(
        name="config-first",
        target=patch,
        args=({"pipeline_ids": ["default"]},),
        daemon=True,
    )
    second = Thread(
        name="config-second", target=patch, args=({"portal_id": "12345"},), daemon=True
    )
    first.start()
    try:
        assert first_read.wait(5)
        second.start()
        assert second_attempt.wait(5)
        assert not second_done.wait(0.1)
    finally:
        release_first.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    with use_workspace("acme-config"):
        config = hubspot_config.get_hubspot_config()
        assert config.pipeline_ids == ("default",)
        assert config.portal_id == "12345"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"pipeline_ids": ["same", "same"]}, "duplicate"),
        ({"pipeline_ids": ["../other"]}, "pipeline"),
        ({"closed_won_stage_ids": ["bad stage"]}, "stage"),
        ({"stage_lag_stages": "draft=unknown"}, "signal"),
        ({"stage_lag_stages": "draft,draft"}, "duplicate"),
        ({"deal_product_property": "bad-property"}, "property"),
        ({"xray_extra_deal_properties": ["bad property"]}, "property"),
        ({"portal_id": "not-a-number"}, "portal"),
        ({"ccs_vendor_fields_default": ["domain"]}, "reserved"),
        ({"ccs_vendor_fields_default": ["DOMAIN"]}, "reserved"),
        ({"scorecard_sync_metrics": {"Revenue": "unknown"}}, "recipe"),
        ({"not_a_setting": "value"}, "unknown"),
    ],
)
def test_invalid_connector_patch_is_rejected_before_storage(
    config_workspaces, changes, message
):
    from rubik_core.tenancy.hubspot_config import (
        HubSpotConfigInvalid,
        patch_hubspot_config,
    )

    with use_workspace("acme-config"):
        with pytest.raises(HubSpotConfigInvalid, match=message):
            patch_hubspot_config(changes)
    with control_session() as session:
        assert session.get(WorkspaceConnector, ("acme-config", "hubspot")) is None


def test_malformed_stored_connector_config_fails_closed(config_workspaces):
    from rubik_core.tenancy.hubspot_config import (
        HubSpotConfigUnavailable,
        get_hubspot_config,
        patch_hubspot_config,
    )

    with control_session() as session:
        session.add(
            WorkspaceConnector(
                workspace_slug="acme-config",
                connector="hubspot",
                config={"pipeline_ids": "shared-pipeline"},
            )
        )
        session.commit()
    with use_workspace("acme-config"):
        with pytest.raises(HubSpotConfigUnavailable):
            get_hubspot_config()
        with pytest.raises(HubSpotConfigUnavailable):
            patch_hubspot_config({"portal_id": "123"})


def test_missing_or_suspended_workspace_refuses_config(config_workspaces):
    from rubik_core.tenancy.hubspot_config import (
        HubSpotConfigUnavailable,
        get_hubspot_config,
        patch_hubspot_config,
    )

    with use_workspace("not-registered"):
        with pytest.raises(HubSpotConfigUnavailable):
            get_hubspot_config()
    with control_session() as session:
        row = session.get(Workspace, "beta-config")
        row.status = "suspended"
        session.commit()
    with use_workspace("beta-config"):
        with pytest.raises(HubSpotConfigUnavailable):
            get_hubspot_config()
        with pytest.raises(HubSpotConfigUnavailable):
            patch_hubspot_config({"pipeline_ids": ["beta"]})


def test_no_workspace_context_never_reads_control(config_workspaces, monkeypatch):
    from rubik_core.tenancy import context, hubspot_config

    monkeypatch.setattr(context, "_process_workspace", None)
    control = Mock(side_effect=AssertionError("control read"))
    monkeypatch.setattr(hubspot_config, "control_session", control)
    with pytest.raises(NoWorkspaceContext):
        hubspot_config.get_hubspot_config()
    control.assert_not_called()


def test_control_outage_fails_closed(config_workspaces, monkeypatch):
    from rubik_core.tenancy import hubspot_config

    @contextmanager
    def offline_control():
        raise OSError("control offline")
        yield

    monkeypatch.setattr(hubspot_config, "control_session", offline_control)
    with use_workspace("acme-config"):
        with pytest.raises(hubspot_config.HubSpotConfigUnavailable):
            hubspot_config.get_hubspot_config()
        with pytest.raises(hubspot_config.HubSpotConfigUnavailable):
            hubspot_config.patch_hubspot_config({"pipeline_ids": ["default"]})
