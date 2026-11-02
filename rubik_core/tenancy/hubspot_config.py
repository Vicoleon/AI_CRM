"""Typed, workspace-owned non-secret HubSpot connector configuration.

The control connector row owns these values; the tenant database never stores
them. A missing connector is an unconfigured workspace, while a missing,
inactive or malformed workspace fails closed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.tenancy.context import current_workspace

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PROPERTY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}\Z")
_PORTAL = re.compile(r"[0-9]{1,20}\Z")


class HubSpotConfigInvalid(ValueError):
    """An admin-supplied connector value is invalid."""


class HubSpotConfigUnavailable(RuntimeError):
    """The selected workspace's control configuration cannot be trusted."""


def _ids(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise HubSpotConfigInvalid(f"{label} must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not _ID.fullmatch(item.strip()):
            raise HubSpotConfigInvalid(f"Invalid {label} ID")
        normalized = item.strip()
        if normalized.casefold() in seen:
            raise HubSpotConfigInvalid(f"{label} has a duplicate ID")
        seen.add(normalized.casefold())
        out.append(normalized)
    return tuple(out)


def _property(value: object, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HubSpotConfigInvalid(f"{label} must be a property name")
    normalized = value.strip()
    if normalized and not _PROPERTY.fullmatch(normalized):
        raise HubSpotConfigInvalid(f"Invalid {label} property name")
    return normalized


def _properties(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise HubSpotConfigInvalid(f"{label} must be a property list")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = _property(item, label)
        if not name:
            raise HubSpotConfigInvalid(f"{label} contains a blank property")
        if name.casefold() in seen:
            raise HubSpotConfigInvalid(f"{label} has a duplicate property")
        seen.add(name.casefold())
        out.append(name)
    return tuple(out)


def _labels(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise HubSpotConfigInvalid(f"{label} must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise HubSpotConfigInvalid(f"{label} must contain text")
        normalized = item.strip()
        if (
            not normalized
            or len(normalized) > 128
            or "," in normalized
            or any(ord(char) < 32 for char in normalized)
        ):
            raise HubSpotConfigInvalid(f"Invalid {label} stage")
        if normalized.casefold() in seen:
            raise HubSpotConfigInvalid(f"{label} has a duplicate stage")
        seen.add(normalized.casefold())
        out.append(normalized)
    return tuple(out)


def _stage_lag(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise HubSpotConfigInvalid("stage_lag_stages must be text")
    if not value.strip():
        return ""
    # Import lazily: stage_lag reads this config after its module has loaded.
    from rubik_core.cockpit.stage_lag import STAGE_SIGNALS

    stages: set[str] = set()
    signals: set[str] = set()
    normalized: list[str] = []
    for part in value.split(","):
        stage, separator, signal = part.partition("=")
        stage, signal = stage.strip(), signal.strip()
        if not _ID.fullmatch(stage) or (separator and not signal):
            raise HubSpotConfigInvalid("Invalid stage_lag_stages stage ID")
        if stage.casefold() in stages:
            raise HubSpotConfigInvalid("stage_lag_stages has a duplicate stage")
        if separator and ("=" in signal or signal not in STAGE_SIGNALS):
            raise HubSpotConfigInvalid("Unknown stage_lag_stages signal")
        if signal in signals:
            raise HubSpotConfigInvalid("stage_lag_stages has a duplicate signal")
        stages.add(stage.casefold())
        if signal:
            signals.add(signal)
        normalized.append(f"{stage}={signal}" if signal else stage)
    return ",".join(normalized)


def _metric_mapping(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise HubSpotConfigInvalid("scorecard_sync_metrics must be an object")
    if not value:
        return MappingProxyType({})
    # One recipe registry remains the source of truth for sync and admin input.
    from rubik_core.scorecard_sync.hubspot_green import RECIPES

    normalized: dict[str, str] = {}
    for name, recipe in value.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > 200
            or any(ord(char) < 32 for char in name)
        ):
            raise HubSpotConfigInvalid("Invalid scorecard measurable name")
        if not isinstance(recipe, str) or recipe.strip() not in RECIPES:
            raise HubSpotConfigInvalid("Unknown scorecard recipe")
        metric = name.strip()
        if metric in normalized:
            raise HubSpotConfigInvalid("Duplicate scorecard measurable")
        normalized[metric] = recipe.strip()
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class HubSpotConnectorConfig:
    pipeline_ids: tuple[str, ...] = ()
    closed_won_stage_ids: tuple[str, ...] = ()
    stage_lag_stages: str = ""
    portal_id: str | None = None
    deal_product_property: str = ""
    deal_turnaround_property: str = ""
    xray_renewal_date_property: str = ""
    xray_auto_renew_properties: tuple[str, ...] = ()
    xray_account_status_properties: tuple[str, ...] = ()
    xray_csm_owner_property: str = ""
    xray_extra_deal_properties: tuple[str, ...] = ()
    xray_extra_company_properties: tuple[str, ...] = ()
    ccs_vendor_fields_default: tuple[str, ...] = ()
    ccs_vendor_fields_extended: tuple[str, ...] = ()
    sales_rhythm_late_stages: tuple[str, ...] = ()
    scorecard_sync_department: str = ""
    scorecard_sync_metrics: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("pipeline_ids", "closed_won_stage_ids"):
            object.__setattr__(self, name, _ids(getattr(self, name), name))
        object.__setattr__(self, "stage_lag_stages", _stage_lag(self.stage_lag_stages))
        portal = self.portal_id
        if portal is not None:
            if not isinstance(portal, str):
                raise HubSpotConfigInvalid("Invalid portal ID")
            portal = portal.strip() or None
            if portal is not None and not _PORTAL.fullmatch(portal):
                raise HubSpotConfigInvalid("Invalid portal ID")
        object.__setattr__(self, "portal_id", portal)
        for name in (
            "deal_product_property",
            "deal_turnaround_property",
            "xray_renewal_date_property",
            "xray_csm_owner_property",
        ):
            object.__setattr__(self, name, _property(getattr(self, name), name))
        for name in (
            "xray_auto_renew_properties",
            "xray_account_status_properties",
            "xray_extra_deal_properties",
            "xray_extra_company_properties",
            "ccs_vendor_fields_default",
            "ccs_vendor_fields_extended",
        ):
            object.__setattr__(self, name, _properties(getattr(self, name), name))
        if not isinstance(self.scorecard_sync_department, str):
            raise HubSpotConfigInvalid("Invalid scorecard department")
        department = self.scorecard_sync_department.strip()
        if len(department) > 200 or any(ord(char) < 32 for char in department):
            raise HubSpotConfigInvalid("Invalid scorecard department")
        object.__setattr__(self, "scorecard_sync_department", department)
        object.__setattr__(
            self,
            "sales_rhythm_late_stages",
            _labels(self.sales_rhythm_late_stages, "sales_rhythm_late_stages"),
        )
        object.__setattr__(
            self, "scorecard_sync_metrics", _metric_mapping(self.scorecard_sync_metrics)
        )
        if self.ccs_vendor_fields_default or self.ccs_vendor_fields_extended:
            from rubik_core.retrieval.hubspot_write import (
                COMPANY_NEVER_WRITABLE,
                RESEARCH_LINK_PROPERTY,
            )

            reserved = {
                name.casefold()
                for name in COMPANY_NEVER_WRITABLE | {RESEARCH_LINK_PROPERTY}
            }
            if any(
                name.casefold() in reserved
                for name in (
                    *self.ccs_vendor_fields_default,
                    *self.ccs_vendor_fields_extended,
                )
            ):
                raise HubSpotConfigInvalid(
                    "A reserved company property is not writable"
                )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> HubSpotConnectorConfig:
        if not isinstance(raw, Mapping):
            raise HubSpotConfigInvalid("HubSpot connector config must be an object")
        known = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in raw.items() if key in known})

    def to_dict(self) -> dict[str, Any]:
        return {
            field.name: (
                dict(value)
                if isinstance(value, Mapping)
                else list(value)
                if isinstance(value, tuple)
                else value
            )
            for field in fields(self)
            for value in (getattr(self, field.name),)
        }


_CONFIG_FIELDS = frozenset(field.name for field in fields(HubSpotConnectorConfig))


def _active_workspace(session: Session, slug: str, *, lock: bool) -> Workspace:
    if lock and session.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE; acquire its writer lock before any read.
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    query = select(Workspace).where(Workspace.slug == slug)
    if lock:
        query = query.with_for_update()
    workspace = session.scalar(query)
    if workspace is None or workspace.status != "active":
        raise HubSpotConfigUnavailable("Active workspace control row required")
    return workspace


def _current_config(row: WorkspaceConnector | None) -> HubSpotConnectorConfig:
    raw = {} if row is None else row.config
    try:
        return HubSpotConnectorConfig.from_mapping(raw)
    except HubSpotConfigInvalid as exc:
        raise HubSpotConfigUnavailable("HubSpot connector config is malformed") from exc


def get_hubspot_config() -> HubSpotConnectorConfig:
    """Read the exact active workspace's HubSpot config, with safe empty defaults."""
    slug = current_workspace()
    try:
        with control_session() as session:
            _active_workspace(session, slug, lock=False)
            return _current_config(session.get(WorkspaceConnector, (slug, "hubspot")))
    except HubSpotConfigUnavailable:
        raise
    except Exception as exc:
        raise HubSpotConfigUnavailable("HubSpot control config is unavailable") from exc


def patch_hubspot_config(changes: Mapping[str, object]) -> HubSpotConnectorConfig:
    """Atomically merge a partial admin edit against the live control row."""
    slug = current_workspace()
    if not isinstance(changes, Mapping):
        raise HubSpotConfigInvalid("HubSpot config patch must be an object")
    unknown = changes.keys() - _CONFIG_FIELDS
    if unknown:
        raise HubSpotConfigInvalid("HubSpot config patch has unknown fields")
    try:
        with control_session() as session:
            _active_workspace(session, slug, lock=True)
            row = session.scalar(
                select(WorkspaceConnector)
                .where(
                    WorkspaceConnector.workspace_slug == slug,
                    WorkspaceConnector.connector == "hubspot",
                )
                .with_for_update()
            )
            current = _current_config(row)
            updated = HubSpotConnectorConfig.from_mapping(
                {**current.to_dict(), **changes}
            )
            raw = dict(row.config) if row is not None else {}
            raw.update(updated.to_dict())
            if row is None:
                row = WorkspaceConnector(
                    workspace_slug=slug, connector="hubspot", config=raw
                )
                session.add(row)
            else:
                row.config = raw
            session.commit()
            return updated
    except (HubSpotConfigInvalid, HubSpotConfigUnavailable):
        raise
    except Exception as exc:
        raise HubSpotConfigUnavailable("HubSpot control config is unavailable") from exc


def save_hubspot_config(value: HubSpotConnectorConfig) -> HubSpotConnectorConfig:
    """Persist a complete typed value; API partial edits should use patch."""
    if not isinstance(value, HubSpotConnectorConfig):
        raise TypeError("A validated HubSpotConnectorConfig is required")
    return patch_hubspot_config(value.to_dict())
