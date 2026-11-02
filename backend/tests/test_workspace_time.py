"""The control row is the only source of a workspace's business time zone."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rubik_core.control.models import Workspace
from rubik_core.tenancy import workspace_time
from rubik_core.tenancy.context import use_workspace


@pytest.fixture
def control_store(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}",
        connect_args={"check_same_thread": False},
    )
    Workspace.__table__.create(engine)
    maker = sessionmaker(bind=engine)
    with maker() as db:
        db.add_all(
            [
                Workspace(
                    slug="acme",
                    name="Acme",
                    status="active",
                    time_zone="America/Costa_Rica",
                ),
                Workspace(
                    slug="bravo",
                    name="Bravo",
                    status="active",
                    time_zone="Europe/London",
                ),
                Workspace(
                    slug="suspended",
                    name="Suspended",
                    status="suspended",
                    time_zone="Asia/Tokyo",
                ),
            ]
        )
        db.commit()
    monkeypatch.setattr(workspace_time, "control_session", maker)
    yield maker
    engine.dispose()


def test_each_workspace_uses_its_own_control_zone(control_store):
    with use_workspace("acme"):
        assert workspace_time.workspace_time_zone_name() == "America/Costa_Rica"
        assert workspace_time.workspace_time_zone().key == "America/Costa_Rica"
    with use_workspace("bravo"):
        assert workspace_time.workspace_time_zone_name() == "Europe/London"
        assert workspace_time.workspace_time_zone().key == "Europe/London"


def test_setter_updates_only_the_selected_active_workspace(control_store):
    with use_workspace("acme"):
        assert workspace_time.set_workspace_time_zone("America/New_York") == (
            "America/New_York"
        )
        assert workspace_time.workspace_time_zone().key == "America/New_York"
    with use_workspace("bravo"):
        assert workspace_time.workspace_time_zone_name() == "Europe/London"


@pytest.mark.parametrize(
    "bad_zone", ["", "Mars/Colony", "../etc/passwd", "US\\Eastern", "Etc/GMT+99"]
)
def test_invalid_admin_zone_is_rejected_without_changing_the_row(
    control_store, bad_zone
):
    with use_workspace("acme"):
        with pytest.raises(workspace_time.WorkspaceTimeInvalid):
            workspace_time.set_workspace_time_zone(bad_zone)
        assert workspace_time.workspace_time_zone_name() == "America/Costa_Rica"


def test_missing_suspended_and_malformed_control_rows_fail_closed(control_store):
    with use_workspace("missing"):
        with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
            workspace_time.workspace_time_zone_name()
        with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
            workspace_time.set_workspace_time_zone("UTC")
    with use_workspace("suspended"):
        with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
            workspace_time.workspace_time_zone()
        with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
            workspace_time.set_workspace_time_zone("UTC")
    with control_store() as db:
        row = db.get(Workspace, "acme")
        row.time_zone = "Mars/Colony"
        db.commit()
    with use_workspace("acme"):
        with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
            workspace_time.workspace_time_zone_name()
        with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
            workspace_time.set_workspace_time_zone("UTC")


def test_control_database_failure_never_uses_a_process_time_zone(
    control_store, monkeypatch
):
    def unavailable():
        raise RuntimeError("control DB down")

    monkeypatch.setattr(workspace_time, "control_session", unavailable)
    with use_workspace("acme"):
        with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
            workspace_time.workspace_time_zone()
        with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
            workspace_time.set_workspace_time_zone("UTC")
