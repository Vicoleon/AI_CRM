"""Business-day readers follow the active workspace's control-row clock."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rubik_core.cockpit import business_hours, sow_sent_detector
from rubik_core.control.models import Workspace
from rubik_core.insights import activity, filters
from rubik_core.loop_closure import hubspot_index
from rubik_core.priorities import temporal
from rubik_core.tenancy import workspace_time
from rubik_core.tenancy.context import use_workspace


@pytest.fixture
def control_store(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'time-readers.db'}",
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
                    time_zone="Asia/Tokyo",
                ),
                Workspace(
                    slug="suspended",
                    name="Suspended",
                    status="suspended",
                    time_zone="Europe/London",
                ),
            ]
        )
        db.commit()
    monkeypatch.setattr(workspace_time, "control_session", maker)
    yield maker
    engine.dispose()


def test_all_readers_use_each_workspace_control_zone(control_store, monkeypatch):
    # A process-level legacy setting cannot move either workspace's day.
    monkeypatch.setenv("COCKPIT_BUSINESS_TZ", "Pacific/Auckland")
    monkeypatch.setenv("PRIORITIES_DEFAULT_TZ", "Pacific/Auckland")
    instant = datetime(2026, 10, 9, 2, 30, tzinfo=timezone.utc)
    with use_workspace("acme"):
        assert business_hours._business_tz().key == "America/Costa_Rica"
        assert hubspot_index._business_tz().key == "America/Costa_Rica"
        assert filters.tz_name() == "America/Costa_Rica"
        assert filters.local_day(instant) == date(2026, 10, 8)
        assert activity.local_day(instant) == date(2026, 10, 8)
        assert temporal.resolve_tz()[1] == "America/Costa_Rica"
        assert temporal.resolve_user_now(now_utc=instant).today == date(2026, 10, 8)
        assert "8 Oct" in sow_sent_detector._suggestion_text(
            instant.replace(tzinfo=None)
        )
    with use_workspace("bravo"):
        assert business_hours._business_tz().key == "Asia/Tokyo"
        assert hubspot_index._business_tz().key == "Asia/Tokyo"
        assert filters.tz_name() == "Asia/Tokyo"
        assert filters.local_day(instant) == date(2026, 10, 9)
        assert activity.local_day(instant) == date(2026, 10, 9)
        assert temporal.resolve_tz()[1] == "Asia/Tokyo"
        assert temporal.resolve_user_now(now_utc=instant).today == date(2026, 10, 9)
        assert "9 Oct" in sow_sent_detector._suggestion_text(
            instant.replace(tzinfo=None)
        )


def test_insights_pins_one_zone_per_calculation_and_reloads_after_edit(
    control_store, monkeypatch
):
    reads = 0

    def counted_session():
        nonlocal reads
        reads += 1
        return control_store()

    monkeypatch.setattr(workspace_time, "control_session", counted_session)
    instant = datetime(2026, 10, 9, 2, 30, tzinfo=timezone.utc)
    with use_workspace("acme"):
        with filters.pinned_business_tz():
            for _ in range(100):
                assert filters.local_day(instant) == date(2026, 10, 8)
                assert activity.local_day(instant) == date(2026, 10, 8)
            assert reads == 1
            with use_workspace("bravo"):
                with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
                    filters.local_day(instant)
        workspace_time.set_workspace_time_zone("Asia/Tokyo")
        with filters.pinned_business_tz():
            assert filters.local_day(instant) == date(2026, 10, 9)
            assert activity.local_day(instant) == date(2026, 10, 9)
        assert reads == 3  # first scope, admin edit, new scope


@pytest.mark.parametrize("slug", ["missing", "suspended", "acme"])
def test_missing_suspended_and_malformed_control_zone_refuse_readers(
    control_store, slug
):
    if slug == "acme":
        with control_store() as db:
            db.get(Workspace, "acme").time_zone = "Mars/Colony"
            db.commit()
    with use_workspace(slug):
        for reader in (
            business_hours._business_tz,
            hubspot_index._business_tz,
            filters.tz_name,
            activity.business_tz,
            temporal.resolve_tz,
            lambda: sow_sent_detector._suggestion_text(datetime(2026, 10, 9, 2)),
        ):
            with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
                reader()


def test_invalid_user_zone_falls_back_to_workspace_but_valid_user_zone_wins(
    control_store,
):
    with patch.object(temporal.logger, "warning") as warning:
        with use_workspace("acme"):
            assert temporal.resolve_tz("America/New_York")[1] == "America/New_York"
            assert temporal.resolve_tz("Mars/Colony")[1] == "America/Costa_Rica"
    warning.assert_called_once()
    assert "using workspace zone" in warning.call_args.args[0]


def test_multi_day_sla_pins_one_workspace_zone_for_the_calculation(monkeypatch):
    calls = 0

    def zone_once():
        nonlocal calls
        calls += 1
        if calls != 1:
            raise AssertionError("Business clock changed mid-calculation")
        return ZoneInfo("America/Costa_Rica")

    monkeypatch.setattr(business_hours, "workspace_time_zone", zone_once)
    friday = datetime(2026, 10, 9, 15, 0)  # Friday 09:00 Costa Rica
    monday = datetime(2026, 10, 12, 23, 0)  # Monday 17:00 Costa Rica
    assert business_hours.business_hours_between(friday, monday) == 16
    assert calls == 1

    calls = 0
    assert business_hours.add_business_hours(friday, 16) == monday
    assert calls == 1


def test_business_hours_batch_reads_one_zone_and_never_reuses_another_workspace(
    control_store, monkeypatch
):
    reads = 0

    def counted_session():
        nonlocal reads
        reads += 1
        return control_store()

    monkeypatch.setattr(workspace_time, "control_session", counted_session)
    opened = datetime(2026, 10, 9, 15)  # Friday 09:00 in Costa Rica
    checked = datetime(2026, 10, 9, 20)  # Friday 14:00; Saturday in Tokyo

    @business_hours.pin_business_time_zone
    def ages():
        return [
            business_hours.business_hours_between(opened, checked) for _ in range(75)
        ]

    with use_workspace("acme"):
        assert ages() == [5.0] * 75
        assert reads == 1
        with business_hours.pinned_business_time_zone():
            assert business_hours.business_hours_between(opened, checked) == 5.0
            with use_workspace("bravo"):
                with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
                    business_hours.business_hours_between(opened, checked)
    with use_workspace("bravo"):
        assert ages() == [0.0] * 75
    assert reads == 3  # Acme batch, nested Acme scope, Bravo batch

    with control_store() as db:
        db.get(Workspace, "acme").time_zone = "Asia/Tokyo"
        db.commit()
    with use_workspace("acme"):
        assert ages() == [0.0] * 75
    assert reads == 4  # The next scope observes the admin's zone change.


def test_escaped_child_task_cannot_reuse_closed_business_clock_after_admin_edit(
    control_store, monkeypatch
):
    reads = 0

    def counted_session():
        nonlocal reads
        reads += 1
        return control_store()

    monkeypatch.setattr(workspace_time, "control_session", counted_session)
    opened = datetime(2026, 10, 9, 15)
    checked = datetime(2026, 10, 9, 20)

    async def age_when_released(release: asyncio.Event) -> float:
        await release.wait()
        return business_hours.business_hours_between(opened, checked)

    async def scenario() -> None:
        release = asyncio.Event()
        with use_workspace("acme"):
            with business_hours.pinned_business_time_zone():
                assert business_hours.business_hours_between(opened, checked) == 5.0
                child = asyncio.create_task(age_when_released(release))

            with control_store() as db:
                db.get(Workspace, "acme").time_zone = "Asia/Tokyo"
                db.commit()

            release.set()
            with pytest.raises(
                workspace_time.WorkspaceTimeUnavailable, match="scope has expired"
            ):
                await child
            # The child never observes the stale pin or reads control.
            assert reads == 1

            with business_hours.pinned_business_time_zone():
                assert business_hours.business_hours_between(opened, checked) == 0.0
            assert reads == 2  # A new scope observes the admin's edit.

    asyncio.run(scenario())


def test_control_outage_drops_activity_instead_of_bucketting_in_utc(
    control_store, monkeypatch
):
    def down():
        raise RuntimeError("control unavailable")

    monkeypatch.setattr(workspace_time, "control_session", down)
    activity.reset_buffer()
    try:
        with use_workspace("acme"):
            with pytest.raises(workspace_time.WorkspaceTimeUnavailable):
                filters.local_day(datetime(2026, 10, 9, 2, 30))
            activity.record_activity(
                "rep@acme.example",
                "chat",
                at=datetime(2026, 10, 9, 2, 30),
            )
            assert activity._BUFFER == {}
    finally:
        activity.reset_buffer()
