"""Short-lived control lookups; routing refuses inactive workspaces."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from rubik_core.config import settings
from rubik_core.control.db import control_session
from rubik_core.control.models import (
    PlatformAdmin,
    SlackInstallation,
    SupportSession,
    Workspace,
    WorkspaceConnector,
    WorkspaceDomain,
    WorkspaceModule,
    normalize,
)
from rubik_core.tenancy.support_context import SupportScope

_connector_mutation_lock = threading.RLock()


def _locked_workspace_for_secret_mutation(
    session: Session, slug: str
) -> Workspace | None:
    """Take a process and database level lock before reading connector state."""
    if session.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE. Acquire its database-wide write lock now,
        # before the first read, so another local process cannot interleave.
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    return session.scalar(
        select(Workspace).where(Workspace.slug == slug).with_for_update()
    )


def get_workspace(slug: str) -> Workspace | None:
    """Administrative lookup, including suspended rows for explicit rejection."""
    with control_session() as session:
        return session.get(Workspace, normalize(slug))


def bind_connector_secret_ref(slug: str, connector: str, field: str, ref: str) -> str:
    """Persist a typed, immutable connector reference in control data."""
    if connector == "db":
        raise PermissionError("Database credentials use privileged accessors")
    with _connector_mutation_lock, control_session() as session:
        workspace = _locked_workspace_for_secret_mutation(session, slug)
        if workspace is None or workspace.status != "active":
            raise PermissionError("Active workspace required for secret binding")
        row = session.scalar(
            select(WorkspaceConnector)
            .where(
                WorkspaceConnector.workspace_slug == slug,
                WorkspaceConnector.connector == connector,
            )
            .with_for_update()
        )
        created = row is None
        if row is None:
            row = WorkspaceConnector(workspace_slug=slug, connector=connector)
            session.add(row)
        refs = dict(row.secret_refs or {})
        existing = refs.get(field)
        if existing is not None and existing != ref:
            raise PermissionError("Connector secret binding cannot be replaced")
        refs[field] = ref
        row.secret_refs = refs
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if created:
                raise PermissionError("Concurrent connector binding changed") from exc
            raise
    return ref


def operate_connector_secret_ref(
    slug: str,
    connector: str,
    field: str,
    ref: str,
    operation: Callable[[], None],
    *,
    create: bool = False,
    clear: bool = False,
    missing_ok: bool = False,
) -> None:
    """Serialize one secret backend mutation and its control binding.

    Locking the workspace row also serializes first writes, where no connector
    row exists yet. The process lock supplies the same behavior on SQLite,
    whose SELECT FOR UPDATE is a no-op; PostgreSQL row locks cover instances.
    """
    if connector == "db" or (create and clear):
        raise PermissionError("Invalid connector secret operation")
    with _connector_mutation_lock, control_session() as session:
        workspace = _locked_workspace_for_secret_mutation(session, slug)
        if workspace is None or workspace.status != "active":
            raise PermissionError("Active workspace required for secret mutation")
        row = session.scalar(
            select(WorkspaceConnector)
            .where(
                WorkspaceConnector.workspace_slug == slug,
                WorkspaceConnector.connector == connector,
            )
            .with_for_update()
        )
        refs = dict(row.secret_refs or {}) if row is not None else {}
        existing = refs.get(field)
        if existing is None and missing_ok and not create:
            return
        if (existing is None and not create) or (
            existing is not None and existing != ref
        ):
            raise PermissionError("Connector secret binding changed")
        operation()
        if create:
            if row is None:
                row = WorkspaceConnector(workspace_slug=slug, connector=connector)
                session.add(row)
            refs[field] = ref
            row.secret_refs = refs
        elif clear:
            del refs[field]
            row.secret_refs = refs
        session.commit()


def operate_bound_secret_ref(
    slug: str, ref: str, operation: Callable[[], None]
) -> None:
    """Mutate an already bound ref under the same lock as clear and upsert."""
    with _connector_mutation_lock, control_session() as session:
        workspace = _locked_workspace_for_secret_mutation(session, slug)
        if workspace is None or workspace.status != "active":
            raise PermissionError("Active workspace required for secret mutation")
        rows = session.scalars(
            select(WorkspaceConnector)
            .where(WorkspaceConnector.workspace_slug == slug)
            .with_for_update()
        ).all()
        matches = [
            (row, field)
            for row in rows
            if row.connector != "db" and isinstance(row.secret_refs, dict)
            for field, bound in row.secret_refs.items()
            if isinstance(field, str)
            and bound == ref
            and ref == f"ws-{slug}-{row.connector}-{field}"
        ]
        if len(matches) != 1:
            raise PermissionError("Workspace secret reference is not bound")
        operation()
        session.commit()


def connector_secret_ref(slug: str, connector: str, field: str) -> str | None:
    """Return only the exact typed binding in an active workspace."""
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            return None
        row = session.get(WorkspaceConnector, (slug, connector))
        refs = row.secret_refs if row is not None else None
        return refs.get(field) if isinstance(refs, dict) else None


def unbind_connector_secret_ref(
    slug: str, connector: str, field: str, ref: str
) -> None:
    """Remove one binding only if it still names the value just cleared."""
    operate_connector_secret_ref(slug, connector, field, ref, lambda: None, clear=True)


def workspace_secret_ref_is_bound(slug: str, ref: str) -> bool:
    """Authorize a complete reference against live control rows, not a prefix."""
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            return False
        for row in session.scalars(
            select(WorkspaceConnector).where(WorkspaceConnector.workspace_slug == slug)
        ):
            if row.connector == "db" or not isinstance(row.secret_refs, dict):
                continue
            for field, bound_ref in row.secret_refs.items():
                if (
                    isinstance(field, str)
                    and isinstance(bound_ref, str)
                    and bound_ref == ref
                    and ref == f"ws-{slug}-{row.connector}-{field}"
                ):
                    return True
    return False


def workspace_for_domain(hd: str) -> Workspace | None:
    with control_session() as session:
        return session.scalar(
            select(Workspace)
            .join(WorkspaceDomain)
            .where(
                WorkspaceDomain.domain == normalize(hd), Workspace.status == "active"
            )
        )


def workspace_for_domain_any(hd: str) -> Workspace | None:
    """Look up a hosted domain including inactive rows for clear sign-in errors."""
    with control_session() as session:
        return session.scalar(
            select(Workspace)
            .join(WorkspaceDomain)
            .where(WorkspaceDomain.domain == normalize(hd))
        )


def domain_belongs_to_workspace(slug: str, domain: str) -> bool:
    """Recheck a normal session against the current control-domain binding."""
    with control_session() as session:
        return (
            session.scalar(
                select(WorkspaceDomain.domain)
                .join(Workspace)
                .where(
                    WorkspaceDomain.workspace_slug == normalize(slug),
                    WorkspaceDomain.domain == normalize(domain),
                    Workspace.status == "active",
                )
            )
            is not None
        )


def domains_for_workspace(slug: str) -> tuple[tuple[str, bool], ...]:
    """Active workspace domains, primary first, from control data only."""
    with control_session() as session:
        return tuple(
            (row.domain, row.is_primary)
            for row in session.scalars(
                select(WorkspaceDomain)
                .join(Workspace)
                .where(
                    WorkspaceDomain.workspace_slug == normalize(slug),
                    Workspace.status == "active",
                )
                .order_by(WorkspaceDomain.is_primary.desc(), WorkspaceDomain.domain)
            )
        )


_MODULE_DEFAULTS = {"core": True, "eos": False, "sales_coach": False}
MODULE_CACHE_TTL_SECONDS = 15.0
MODULE_CACHE_MAX_ENTRIES = 256
_MODULE_CACHE: dict[tuple[str, str], tuple[float, dict[str, bool]]] = {}
_MODULE_LOCK = threading.Lock()
_MODULE_GLOBAL_EPOCH = 0
_MODULE_SLUG_EPOCH: dict[str, int] = {}


def clear_module_cache(slug: str | None = None) -> None:
    """Invalidate effective switches after a control module or workspace write."""
    global _MODULE_GLOBAL_EPOCH
    with _MODULE_LOCK:
        if slug is None:
            _MODULE_GLOBAL_EPOCH += 1
            _MODULE_CACHE.clear()
            _MODULE_SLUG_EPOCH.clear()
        else:
            normalized = normalize(slug)
            _MODULE_SLUG_EPOCH[normalized] = _MODULE_SLUG_EPOCH.get(normalized, 0) + 1
            for key in [key for key in _MODULE_CACHE if key[1] == normalize(slug)]:
                _MODULE_CACHE.pop(key, None)


def _read_module_flags(slug: str) -> dict[str, bool]:
    """Read the source of truth without caching errors or inactive workspaces."""
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            return {name: False for name in _MODULE_DEFAULTS}
        flags = dict(_MODULE_DEFAULTS)
        for row in session.scalars(
            select(WorkspaceModule).where(
                WorkspaceModule.workspace_slug == workspace.slug
            )
        ):
            if row.module in flags:
                flags[row.module] = bool(row.enabled)
        return flags


def module_flags(slug: str) -> dict[str, bool]:
    """Effective switches, cached briefly by control DB and workspace."""
    key = (settings.CONTROL_DATABASE_URL, normalize(slug))
    for _ in range(3):
        now = time.monotonic()
        with _MODULE_LOCK:
            hit = _MODULE_CACHE.get(key)
            if hit is not None and hit[0] > now:
                return dict(hit[1])
            version = (_MODULE_GLOBAL_EPOCH, _MODULE_SLUG_EPOCH.get(key[1], 0))
        flags = _read_module_flags(key[1])
        with _MODULE_LOCK:
            if version == (_MODULE_GLOBAL_EPOCH, _MODULE_SLUG_EPOCH.get(key[1], 0)):
                if len(_MODULE_CACHE) >= MODULE_CACHE_MAX_ENTRIES:
                    _MODULE_CACHE.pop(next(iter(_MODULE_CACHE)))
                _MODULE_CACHE[key] = (
                    time.monotonic() + MODULE_CACHE_TTL_SECONDS,
                    flags,
                )
                return dict(flags)
    raise RuntimeError("Workspace modules changed during lookup")


def module_enabled(module: str, slug: str | None = None) -> bool:
    """Fail closed for unknown modules and inactive/unknown workspaces."""
    from rubik_core.tenancy.context import current_workspace

    name = normalize(module)
    if name not in _MODULE_DEFAULTS:
        return False
    return module_flags(slug if slug is not None else current_workspace())[name]


def active_workspaces(module: str | None = None) -> list[Workspace]:
    statement = select(Workspace).where(Workspace.status == "active")
    if module is not None:
        name = normalize(module)
        if name not in _MODULE_DEFAULTS:
            return []
        statement = statement.outerjoin(
            WorkspaceModule,
            and_(
                WorkspaceModule.workspace_slug == Workspace.slug,
                WorkspaceModule.module == name,
            ),
        ).where(
            func.coalesce(WorkspaceModule.enabled, _MODULE_DEFAULTS[name]).is_(True)
        )
    with control_session() as session:
        return list(session.scalars(statement.order_by(Workspace.slug)))


def workspace_for_slack(app_id: str, team_id: str) -> Workspace | None:
    with control_session() as session:
        return session.scalar(
            select(Workspace)
            .join(SlackInstallation)
            .where(
                SlackInstallation.app_id == app_id.strip(),
                SlackInstallation.team_id == team_id.strip(),
                Workspace.status == "active",
            )
        )


@dataclass(frozen=True)
class SlackRoute:
    """The active control installation's workspace and signing-secret binding."""

    workspace_slug: str
    signing_secret_ref: str


def slack_route_for(app_id: str, team_id: str) -> SlackRoute | None:
    """Resolve an untrusted app/team pair without touching a tenant database."""
    with control_session() as session:
        row = session.execute(
            select(
                SlackInstallation.workspace_slug, SlackInstallation.signing_secret_ref
            )
            .join(Workspace, SlackInstallation.workspace_slug == Workspace.slug)
            .where(
                SlackInstallation.app_id == app_id,
                SlackInstallation.team_id == team_id,
                Workspace.status == "active",
            )
        ).one_or_none()
        if row is None:
            return None
        return SlackRoute(workspace_slug=row[0], signing_secret_ref=row[1])


def is_platform_admin(email: str) -> bool:
    with control_session() as session:
        return session.get(PlatformAdmin, normalize(email)) is not None


def support_enter_target(actor: str, home_slug: str, target_slug: str) -> Workspace:
    """Authorize a normal home session to enter an active target workspace."""
    from rubik_core.tenancy.context import _canonical_slug

    home_slug = _canonical_slug(home_slug)
    target_slug = _canonical_slug(target_slug)
    with control_session() as session:
        admin = session.get(PlatformAdmin, normalize(actor))
        if admin is None or admin.home_workspace_slug != home_slug:
            raise PermissionError("Platform support access required")
        home = session.get(Workspace, home_slug)
        target = session.get(Workspace, target_slug)
        if home is None or home.status != "active":
            raise PermissionError("Platform support home is unavailable")
        if target is None or target.status != "active":
            raise ValueError("Target workspace is unavailable")
        return target


def create_support_session(
    actor: str, home_slug: str, target_slug: str
) -> SupportScope:
    """Mint one live control sid, rechecking the admin and target at commit."""
    target = support_enter_target(actor, home_slug, target_slug)
    now = datetime.now(timezone.utc)
    sid = token_urlsafe(32)
    with control_session() as session:
        session.add(
            SupportSession(
                sid=sid,
                actor=normalize(actor),
                target_workspace_slug=target.slug,
                expires_at=now + timedelta(minutes=15),
            )
        )
        # Another control write can revoke the admin or suspend the target
        # between initial authorization and this insert. The FK protects row
        # existence; recheck status and home binding in the same transaction.
        admin = session.get(PlatformAdmin, normalize(actor))
        current_home = session.get(Workspace, home_slug)
        current_target = session.get(Workspace, target.slug)
        if (
            admin is None
            or admin.home_workspace_slug != home_slug
            or current_home is None
            or current_home.status != "active"
            or current_target is None
            or current_target.status != "active"
        ):
            raise PermissionError("Platform support access changed")
        session.commit()
        return SupportScope(
            actor=normalize(actor),
            sid=sid,
            workspace_slug=target.slug,
            workspace_name=current_target.name,
        )


def validate_support_session(
    sid: str, actor: str, target_slug: str
) -> SupportScope | None:
    """Every request checks live sid, platform admin, and active workspaces."""
    from rubik_core.tenancy.context import _canonical_slug

    try:
        target_slug = _canonical_slug(target_slug)
    except ValueError:
        return None
    if not isinstance(sid, str) or not sid or len(sid) > 128:
        return None
    now = datetime.now(timezone.utc)
    home = aliased(Workspace)
    with control_session() as session:
        row = session.execute(
            select(SupportSession, Workspace)
            .join(PlatformAdmin, SupportSession.actor == PlatformAdmin.email)
            .join(Workspace, SupportSession.target_workspace_slug == Workspace.slug)
            .join(home, PlatformAdmin.home_workspace_slug == home.slug)
            .where(
                SupportSession.sid == sid,
                SupportSession.actor == normalize(actor),
                SupportSession.target_workspace_slug == target_slug,
                SupportSession.revoked_at.is_(None),
                Workspace.status == "active",
                home.status == "active",
            )
        ).first()
        if row is None:
            return None
        support, target = row
        expires = support.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            return None
        return SupportScope(
            actor=support.actor,
            sid=support.sid,
            workspace_slug=target.slug,
            workspace_name=target.name,
        )


def revoke_support_session(sid: str, actor: str, target_slug: str) -> bool:
    """Revoke exactly the actor's target sid; repeated Exit is harmless."""
    with control_session() as session:
        row = session.get(SupportSession, sid)
        if (
            row is None
            or row.actor != normalize(actor)
            or row.target_workspace_slug != normalize(target_slug)
        ):
            return False
        if row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)
            session.commit()
        return True
