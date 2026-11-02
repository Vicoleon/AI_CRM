"""Read and edit the current workspace's IANA time zone in control data."""

from __future__ import annotations

import re
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy import select
from sqlalchemy.orm import Session

from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace
from rubik_core.tenancy.context import current_workspace

_ZONE_NAME = re.compile(r"[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*\Z")


class WorkspaceTimeInvalid(ValueError):
    """An admin supplied a name that is not a valid IANA time zone."""


class WorkspaceTimeUnavailable(RuntimeError):
    """The selected workspace's time zone cannot be safely resolved."""


@lru_cache(maxsize=1)
def _iana_zones() -> frozenset[str]:
    return frozenset(available_timezones())


def _valid_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 100
        or not _ZONE_NAME.fullmatch(value)
        or any(part in (".", "..") for part in value.split("/"))
    ):
        raise WorkspaceTimeInvalid("A valid IANA time zone is required")
    try:
        installed = _iana_zones()
    except Exception as exc:
        raise WorkspaceTimeUnavailable("IANA time-zone data unavailable") from exc
    if value not in installed:
        raise WorkspaceTimeInvalid("A valid IANA time zone is required")
    # Membership in the installed database should imply this, but verify the
    # actual tzinfo is loadable before storing the name.
    try:
        ZoneInfo(value)
    except Exception as exc:
        raise WorkspaceTimeInvalid("IANA time zone data is unavailable") from exc
    return value


def _active_row(session: Session, slug: str, *, lock: bool) -> Workspace:
    if lock and session.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE; serialize the read and write explicitly.
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    query = select(Workspace).where(Workspace.slug == slug)
    if lock:
        query = query.with_for_update()
    row = session.scalar(query)
    if row is None or row.status != "active":
        raise WorkspaceTimeUnavailable("Active workspace control row required")
    try:
        _valid_name(row.time_zone)
    except WorkspaceTimeInvalid as exc:
        raise WorkspaceTimeUnavailable("Stored workspace time zone is invalid") from exc
    return row


def workspace_time_zone_name() -> str:
    """Return this active workspace's validated control time-zone name."""
    slug = current_workspace()
    try:
        with control_session() as session:
            return _active_row(session, slug, lock=False).time_zone
    except WorkspaceTimeUnavailable:
        raise
    except Exception as exc:
        raise WorkspaceTimeUnavailable("Workspace time zone unavailable") from exc


def workspace_time_zone() -> ZoneInfo:
    """Return the tzinfo for the current workspace, with no process fallback."""
    try:
        return ZoneInfo(workspace_time_zone_name())
    except WorkspaceTimeUnavailable:
        raise
    except Exception as exc:
        raise WorkspaceTimeUnavailable("Workspace time zone unavailable") from exc


def set_workspace_time_zone(name: str) -> str:
    """Atomically update this active workspace's time zone after validation.

    Authorization belongs to the calling admin endpoint. The accessor only
    operates on ``current_workspace`` and never accepts a caller-supplied slug.
    """
    valid = _valid_name(name)
    slug = current_workspace()
    try:
        with control_session() as session:
            row = _active_row(session, slug, lock=True)
            row.time_zone = valid
            session.commit()
            return valid
    except WorkspaceTimeUnavailable:
        raise
    except Exception as exc:
        raise WorkspaceTimeUnavailable("Workspace time zone unavailable") from exc
