"""Control-plane authorization and bounded execution for scheduled work."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request

from rubik_core.config import settings
from rubik_core.logsafe import safe_error
from rubik_core.tenancy.context import _canonical_slug, use_workspace

logger = logging.getLogger(__name__)
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_T = TypeVar("_T")


@dataclass(frozen=True)
class SchedulerScope:
    slugs: tuple[str, ...]
    trigger: str
    fanout: bool


def scheduler_key_valid(raw: str | None) -> bool:
    """A missing configuration never makes an empty header a credential."""
    return bool(
        raw is not None
        and settings.SCHEDULER_API_KEY
        and hmac.compare_digest(raw, settings.SCHEDULER_API_KEY)
    )


def scheduler_scope(
    request: Request,
    module: str | None = None,
    *,
    actor_workspace: str | None = None,
) -> SchedulerScope:
    """Resolve one verified scheduler target or the active fan-out roster.

    This consults only the control database. Routers still authorize human
    roles, while the middleware ensures a human cannot select another tenant.
    """
    from rubik_core.control.repository import (
        active_workspaces,
        get_workspace,
        module_enabled,
    )

    key = request.headers.get("X-Scheduler-Key")
    requested = request.headers.get("X-Cubie-Workspace")
    if key is not None:
        if not scheduler_key_valid(key):
            raise HTTPException(status_code=403, detail="Invalid scheduler key")
        if requested is not None:
            try:
                slug = _canonical_slug(requested)
            except ValueError:
                raise HTTPException(
                    status_code=404, detail="Workspace not found"
                ) from None
            workspace = get_workspace(slug)
            if workspace is None or workspace.status != "active":
                raise HTTPException(status_code=404, detail="Workspace not found")
            if module and not module_enabled(module, slug):
                raise HTTPException(status_code=404, detail="Module not enabled")
            return SchedulerScope((slug,), "scheduler", False)
        return SchedulerScope(
            tuple(row.slug for row in active_workspaces(module)),
            "scheduler",
            True,
        )

    if actor_workspace is None:
        raise HTTPException(status_code=403, detail="Unauthorized")
    if requested is not None and requested != actor_workspace:
        raise HTTPException(status_code=403, detail="Workspace mismatch")
    return SchedulerScope((actor_workspace,), "admin", False)


async def run_for_workspaces(
    slugs: Iterable[str],
    fn: Callable[[str], _T | Awaitable[_T]],
    limit: int = 4,
) -> dict[str, dict[str, Any]]:
    """Return a result for every tenant, even if a neighbor fails."""
    if limit < 1:
        raise ValueError("Fan-out concurrency must be positive")
    ordered = tuple(slugs)
    if len(set(ordered)) != len(ordered):
        raise ValueError("Fan-out workspace slugs must be unique")
    semaphore = asyncio.Semaphore(limit)

    async def one(slug: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                with use_workspace(slug):
                    if inspect.iscoroutinefunction(fn):
                        value = fn(slug)
                    else:
                        value = await asyncio.to_thread(fn, slug)
                    if inspect.isawaitable(value):
                        value = await value
                return slug, {"status": "ok", "result": value}
            except Exception as exc:  # noqa: BLE001 - one tenant never aborts another
                error = safe_error(exc)
                logger.error("Scheduled workspace %s failed: %s", slug, error)
                return slug, {"status": "error", "error": error}

    return dict(await asyncio.gather(*(one(slug) for slug in ordered)))


def local_schedule_slot(time_zone: str, instant: datetime | None = None) -> str:
    """Stable workspace-local day key for one daily scheduled occurrence."""
    now = instant or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("A timezone-aware instant is required")
    return now.astimezone(ZoneInfo(time_zone)).date().isoformat()


def expand_schedule_entries(
    manifest: dict[str, Any], workspaces: Iterable[Any]
) -> list[dict[str, Any]]:
    """Expand declarative schedules into deterministic platform job specs."""
    templates = manifest.get("schedules")
    if not isinstance(templates, list):
        raise ValueError("Schedule manifest needs a schedules list")
    active = sorted(
        (row for row in workspaces if row.status == "active"),
        key=lambda row: row.slug,
    )
    expanded: list[dict[str, Any]] = []
    names: set[str] = set()
    for template in templates:
        if not isinstance(template, dict):
            raise ValueError("Schedule entries must be objects")
        name = template.get("name")
        scope = template.get("scope")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("Invalid schedule name")
        if scope not in ("per_workspace_timezone", "platform_fanout"):
            raise ValueError(f"Invalid scope for schedule {name}")
        if name in names:
            raise ValueError(f"Duplicate schedule name {name}")
        names.add(name)
        if scope == "platform_fanout":
            if template.get("target") == "job":
                raise ValueError("A direct Job schedule needs one workspace")
            entry = dict(template)
            entry.pop("scope")
            entry["time_zone"] = "UTC"
            entry["headers"] = dict(template.get("headers") or {})
            entry["headers"].pop("X-Cubie-Workspace", None)
            if entry.get("target", "http") == "http":
                entry["headers"]["X-Scheduler-Key"] = {"secret": "SCHEDULER_API_KEY"}
            expanded.append(entry)
            continue
        for workspace in active:
            slug = _canonical_slug(workspace.slug)
            module = template.get("module")
            if module:
                from rubik_core.control.repository import module_enabled

                if not module_enabled(module, slug):
                    continue
            entry = dict(template)
            entry.pop("scope")
            entry["name"] = f"{name}--{slug}"
            entry["time_zone"] = ZoneInfo(workspace.time_zone).key
            entry["headers"] = {
                **dict(template.get("headers") or {}),
                "X-Cubie-Workspace": slug,
            }
            if entry.get("target", "http") == "http":
                entry["headers"]["X-Scheduler-Key"] = {"secret": "SCHEDULER_API_KEY"}
            if template.get("target") == "job":
                entry["env"] = {
                    **dict(template.get("env") or {}),
                    "CUBIE_WORKSPACE": slug,
                }
            expanded.append(entry)
    return expanded
