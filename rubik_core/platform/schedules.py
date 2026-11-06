"""Expand and reconcile the same schedule manifest used by deployment."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from rubik_core.automation import automations_enabled
from rubik_core.tenancy.scheduler import expand_schedule_entries

DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "deploy" / "schedules.yaml"
_SCHEDULE_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = yaml.safe_load(path.read_text())
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("schedules"), list
    ):
        raise ValueError("Invalid schedule manifest")
    return manifest


def reconcile_schedules(
    manifest: dict[str, Any],
    active_workspaces: Iterable[Any],
    executor: Any,
    *,
    dry_run: bool = False,
    prune: bool = True,
) -> list[dict[str, Any]]:
    """Reconcile only manifest-owned schedules, respecting a global hold.

    Provisioning calls this with ``prune=False`` for one newly registered row;
    deployment passes the complete active roster and leaves pruning enabled.
    A hold never creates a Scheduler job: its initial state cannot be set to
    PAUSED atomically. Existing managed schedules are paused, never deleted.
    """
    expanded = expand_schedule_entries(manifest, active_workspaces)
    expected = {entry["name"] for entry in expanded}
    for entry in expanded:
        body = entry.get("body")
        if body is not None:
            if not isinstance(body, dict):
                raise ValueError("Schedule body must be a JSON object")
            entry["body"] = json.dumps(body, sort_keys=True, separators=(",", ":"))
            entry["headers"] = {
                **entry.get("headers", {}),
                "Content-Type": "application/json",
            }
    prefixes = tuple(
        f"{entry['name']}--"
        for entry in manifest["schedules"]
        if entry.get("scope") == "per_workspace_timezone"
    )
    platform_names = {
        entry["name"]
        for entry in manifest["schedules"]
        if entry.get("scope") == "platform_fanout"
    }

    def managed(name: str) -> bool:
        return name in platform_names or any(
            name.startswith(prefix) and _SCHEDULE_SLUG.fullmatch(name[len(prefix) :])
            for prefix in prefixes
        )

    if not automations_enabled():
        if not dry_run:
            for name in executor.list_schedule_names():
                if managed(name):
                    executor.pause_schedule(name)
        return expanded

    if not dry_run:
        for entry in expanded:
            executor.upsert_schedule(entry)
    if prune and not dry_run:
        for name in executor.list_schedule_names():
            if name in expected:
                continue
            if any(
                name.startswith(prefix)
                and _SCHEDULE_SLUG.fullmatch(name[len(prefix) :])
                for prefix in prefixes
            ):
                executor.delete_schedule(name)
    return expanded
