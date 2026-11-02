"""Resumable, explicitly targeted workspace provisioning.

Only the control database contains the journal. A pending database is migrated
under its persisted identity before its workspace becomes routable.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from rubik_core.company.domain import fold_domain
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.db import get_engine as control_engine
from rubik_core.control.models import (
    PlatformAdmin,
    ProvisioningStep,
    Workspace,
    WorkspaceConnectionSpec,
    WorkspaceDomain,
    WorkspaceModule,
)
from rubik_core.control.repository import _MODULE_DEFAULTS, clear_module_cache
from rubik_core.database.models import Role, User
from rubik_core.database.seed import seed_workspace
from rubik_core.platform.migrate import migrate_workspace, migration_advisory_lock
from rubik_core.platform.schedules import load_manifest, reconcile_schedules
from rubik_core.tenancy.context import _canonical_slug, use_workspace
from rubik_core.tenancy.registry import engine_for
from rubik_core.tenancy.secrets import read_pending_secret, write_pending_secret
from rubik_core.tenancy.workspace_time import _valid_name as valid_time_zone_name

STEPS = (
    "validate",
    "check_headroom",
    "create_database",
    "migrate",
    "create_storage",
    "create_service_account",
    "register",
    "seed_and_schedule",
    "checklist",
)

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")
_ACCENT = re.compile(r"#[0-9a-fA-F]{6}\Z")
_SQL_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


@dataclass(frozen=True)
class CreateSpec:
    slug: str
    name: str
    domains: tuple[str, ...]
    first_admins: tuple[str, ...]
    dwd_subject: str
    time_zone: str
    logo: str | None = None
    accent: str | None = None
    modules: tuple[str, ...] = ()
    freeze_windows: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ConnectionBudget:
    max_connections: int
    service_max_instances: int
    job_max_instances: int
    reserve: int = 10
    control_pool: int = 4
    sync_pool: int = 4
    adk_pool: int = 5

    @classmethod
    def from_environment(cls) -> ConnectionBudget:
        if settings.APP_MODE != AppMode.GCP and not settings.CLOUDSQL_CONNECTION_NAME:
            return cls(10000, 1, 1, reserve=0)
        names = (
            "CUBIE_PROVISION_DB_MAX_CONNECTIONS",
            "CUBIE_PROVISION_SERVICE_MAX_INSTANCES",
            "CUBIE_PROVISION_JOB_MAX_INSTANCES",
        )
        values = []
        for name in names:
            raw = os.environ.get(name)
            if raw is None or not raw.isdecimal() or int(raw) <= 0:
                raise ValueError(f"Set positive {name} before provisioning")
            values.append(int(raw))
        reserve_raw = os.environ.get("CUBIE_PROVISION_RESERVED_CONNECTIONS", "10")
        if not reserve_raw.isdecimal():
            raise ValueError("CUBIE_PROVISION_RESERVED_CONNECTIONS must be nonnegative")
        return cls(*values, reserve=int(reserve_raw))


def check_connection_headroom(candidate_count: int, budget: ConnectionBudget) -> int:
    if candidate_count < 1:
        raise ValueError("At least one workspace is required")
    if (
        min(
            budget.max_connections,
            budget.service_max_instances,
            budget.job_max_instances,
            budget.control_pool,
            budget.sync_pool,
            budget.adk_pool,
        )
        < 1
        or budget.reserve < 0
    ):
        raise ValueError("Invalid connection budget")
    instances = budget.service_max_instances + budget.job_max_instances
    # Every process may hold a bounded sync engine and an ADK async pool for
    # each candidate workspace, plus its separate platform control pool.
    required = instances * (
        budget.control_pool + candidate_count * (budget.sync_pool + budget.adk_pool)
    )
    remaining = budget.max_connections - budget.reserve - required
    if remaining < 0:
        raise ValueError(
            f"Insufficient connection headroom: need {required} plus reserve "
            f"{budget.reserve}, maximum {budget.max_connections}"
        )
    return remaining


def _sql_identifier(value: str) -> str:
    if not _SQL_IDENT.fullmatch(value):
        raise ValueError("Invalid database or role identifier")
    return f'"{value}"'


def database_grants_sql(db_name: str, db_user: str) -> str:
    """Postgres grants for one database, never to PUBLIC or another tenant."""
    database = _sql_identifier(db_name)
    role = _sql_identifier(db_user)
    return (
        f"REVOKE CONNECT ON DATABASE {database} FROM PUBLIC;\n"
        f"GRANT CONNECT ON DATABASE {database} TO {role};"
    )


def _database_spec(slug: str) -> WorkspaceConnectionSpec:
    canonical = _canonical_slug(slug)
    name = canonical.replace("-", "_")
    suffix = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    short = name[:39]
    db_name = f"cubie_{short}_{suffix}"
    db_user = f"{db_name}_user"
    return WorkspaceConnectionSpec(db_name, db_user, f"ws-{canonical}-db-password")


def _normalize_spec(spec: CreateSpec) -> CreateSpec:
    if not isinstance(spec, CreateSpec):
        raise TypeError("CreateSpec required")
    slug = _canonical_slug(spec.slug)
    name = spec.name.strip()
    if not name or len(name) > 255:
        raise ValueError("A workspace name is required")
    domains = tuple(dict.fromkeys(fold_domain(value) for value in spec.domains))
    if not domains or any(
        not value
        or len(value) > 253
        or "." not in value
        or any(not _DOMAIN_LABEL.fullmatch(label) for label in value.split("."))
        for value in domains
    ):
        raise ValueError("At least one valid domain is required")
    admins = tuple(dict.fromkeys(value.strip().lower() for value in spec.first_admins))
    if not admins or any(not _EMAIL.fullmatch(email) for email in admins):
        raise ValueError("At least one valid first admin is required")
    if any(email.rsplit("@", 1)[1] not in domains for email in admins):
        raise ValueError("Every first admin must belong to a workspace domain")
    subject = spec.dwd_subject.strip().lower()
    if not _EMAIL.fullmatch(subject) or subject.rsplit("@", 1)[1] not in domains:
        raise ValueError("The delegation subject must belong to a workspace domain")
    time_zone = valid_time_zone_name(spec.time_zone)
    if spec.accent is not None and not _ACCENT.fullmatch(spec.accent):
        raise ValueError("Accent must be a six-digit hex color")
    modules = tuple(dict.fromkeys(module.strip().lower() for module in spec.modules))
    if any(module not in _MODULE_DEFAULTS for module in modules):
        raise ValueError("Invalid module key")
    freeze_windows = []
    for item in spec.freeze_windows:
        if not isinstance(item, dict) or set(item) != {
            "day",
            "start",
            "end",
            "time_zone",
        }:
            raise ValueError("A freeze window needs day, start, end and time_zone")
        day, start, end, zone = (
            item["day"],
            item["start"],
            item["end"],
            item["time_zone"],
        )
        if day not in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"):
            raise ValueError("Freeze day must be MON through SUN")
        if (
            not all(
                isinstance(value, str)
                and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)
                for value in (start, end)
            )
            or start >= end
        ):
            raise ValueError("Freeze start/end must be same-day HH:MM times")
        zone = valid_time_zone_name(zone)
        freeze_windows.append(
            {"day": day, "start": start, "end": end, "time_zone": zone}
        )
    return CreateSpec(
        slug,
        name,
        domains,
        admins,
        subject,
        time_zone,
        spec.logo,
        spec.accent,
        modules,
        tuple(freeze_windows),
    )


def _spec_fingerprint(spec: CreateSpec) -> str:
    """Freeze validated input across retries without storing credentials."""
    payload = json.dumps(asdict(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


@contextmanager
def _provision_lock(slug: str) -> Iterator[None]:
    """Serialize retries and suspension across processes for this slug."""
    engine = control_engine()
    if engine.dialect.name == "postgresql":
        with (
            engine.connect() as connection,
            migration_advisory_lock(connection, f"provision:{slug}"),
        ):
            yield
        return
    if engine.dialect.name != "sqlite":
        raise RuntimeError("Provisioning lock needs PostgreSQL or SQLite")
    database = engine.url.database
    directory = (
        Path(database).expanduser().resolve().parent
        if database and database != ":memory:"
        else Path(settings.LOCAL_DATA_DIR).expanduser().resolve()
    )
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / f".provision-{slug}.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@dataclass(frozen=True)
class ProvisionResult:
    slug: str
    status: str
    completed_steps: tuple[str, ...]
    checklist: tuple[str, ...] = ()
    dry_run_details: tuple[str, ...] = ()


class ProvisioningExecutor(Protocol):
    def ensure_database(self, spec: WorkspaceConnectionSpec, password: str) -> None: ...
    def ensure_storage(self, spec: CreateSpec) -> None: ...
    def corpus_resources(self, spec: CreateSpec) -> dict[str, str]: ...
    def ensure_service_account(self, spec: CreateSpec) -> None: ...
    def service_account_identity(
        self, spec: CreateSpec
    ) -> tuple[str | None, str | None]: ...
    def upsert_schedule(self, row: dict[str, Any]) -> None: ...
    def checklist(self, spec: CreateSpec, client_id: str | None) -> tuple[str, ...]: ...


def _step(session: Session, slug: str, name: str) -> ProvisioningStep | None:
    return session.get(ProvisioningStep, (slug, name))


def _completed(slug: str, name: str) -> bool:
    with control_session() as session:
        row = _step(session, slug, name)
        return row is not None and row.status == "complete"


def _mark_complete(
    slug: str,
    name: str,
    *,
    spec: WorkspaceConnectionSpec | None = None,
    revision: str | None = None,
) -> None:
    with control_session() as session:
        row = _step(session, slug, name)
        if row is None:
            row = ProvisioningStep(workspace_slug=slug, step=name)
            session.add(row)
        row.status = "complete"
        row.updated_at = datetime.now(timezone.utc)
        if spec is not None:
            row.db_name = spec.db_name
            row.db_user = spec.db_user
            row.db_password_secret_ref = spec.db_password_secret_ref
        if revision is not None:
            row.schema_revision = revision
        session.commit()


def _pending_spec(slug: str) -> WorkspaceConnectionSpec:
    with control_session() as session:
        row = _step(session, slug, "create_database")
        if row is None or row.status != "complete":
            raise RuntimeError("Pending database identity is missing")
        result = WorkspaceConnectionSpec(
            row.db_name, row.db_user, row.db_password_secret_ref
        )
    if result != _database_spec(slug):
        raise PermissionError("Pending database identity changed")
    return result


def _seed_registered_workspace(spec: CreateSpec) -> None:
    with use_workspace(spec.slug):
        session = sessionmaker(bind=engine_for(spec.slug), expire_on_commit=False)()
        try:
            seed_workspace(session)
            admin_role = session.get(Role, "admin")
            if admin_role is None:
                admin_role = Role(name="admin", description="Workspace administrator")
                session.add(admin_role)
            user_role = session.get(Role, "user")
            if user_role is None:
                user_role = Role(name="user", description="Workspace user")
                session.add(user_role)
            for email in spec.first_admins:
                user = session.get(User, email)
                if user is None:
                    user = User(
                        email=email, id=f"provisioned:{email}", name=email.split("@")[0]
                    )
                    session.add(user)
                if admin_role not in user.roles:
                    user.roles.append(admin_role)
                if user_role not in user.roles:
                    user.roles.append(user_role)
            session.commit()
        finally:
            session.close()


class Provisioner:
    def __init__(
        self,
        executor: ProvisioningExecutor,
        budget: ConnectionBudget | None = None,
        *,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        self.executor = executor
        self.budget = budget or ConnectionBudget.from_environment()
        self.manifest = manifest if manifest is not None else load_manifest()

    def _validate_control(self, spec: CreateSpec) -> None:
        with control_session() as session:
            workspace = session.get(Workspace, spec.slug)
            if workspace is not None and workspace.status != "active":
                raise ValueError("Workspace is inactive")
            if workspace is not None and workspace.name != spec.name:
                raise ValueError("Workspace slug is already registered")
            validated = _step(session, spec.slug, "validate")
            if workspace is not None and (
                validated is None or validated.status != "complete"
            ):
                raise ValueError("Workspace was not created by this provisioning run")
            for domain in spec.domains:
                row = session.get(WorkspaceDomain, domain)
                if row is not None and row.workspace_slug != spec.slug:
                    raise ValueError(
                        f"Workspace domain is already registered: {domain}"
                    )
            if workspace is not None:
                existing = {
                    row.domain
                    for row in session.scalars(
                        select(WorkspaceDomain).where(
                            WorkspaceDomain.workspace_slug == spec.slug
                        )
                    )
                }
                if existing != set(spec.domains):
                    raise ValueError(
                        "Registered workspace domains differ from create spec"
                    )
            if validated is not None and (
                validated.status != "complete"
                or validated.schema_revision != _spec_fingerprint(spec)
            ):
                raise ValueError("Provisioning input differs from its recorded spec")

    def _candidate_count(self, slug: str) -> int:
        with control_session() as session:
            existing = {row[0] for row in session.execute(select(Workspace.slug))}
            # A validated but not yet registered workspace has already reserved
            # capacity. Keep that reservation across a failed, resumable run.
            existing.update(
                row[0]
                for row in session.execute(
                    select(ProvisioningStep.workspace_slug).where(
                        ProvisioningStep.step == "validate",
                        ProvisioningStep.status == "complete",
                    )
                )
            )
        return len(existing | {slug})

    def _ensure_database(self, slug: str) -> WorkspaceConnectionSpec:
        expected = _database_spec(slug)
        with control_session() as session:
            registered = session.get(Workspace, slug)
            journal = _step(session, slug, "create_database")
            if registered is not None and (
                journal is None or journal.status != "complete"
            ):
                raise RuntimeError(
                    "Registered workspace has no completed database identity journal"
                )
        if journal is not None and journal.status == "complete":
            stored = _pending_spec(slug)
            if registered is not None:
                if (
                    registered.db_name,
                    registered.db_user,
                    registered.db_password_secret_ref,
                ) != (
                    stored.db_name,
                    stored.db_user,
                    stored.db_password_secret_ref,
                ):
                    raise PermissionError("Registered database identity changed")
                return stored
            if read_pending_secret(slug, stored):
                return stored
        # A crash before the secret write leaves a completed identity but no
        # password. Reapply the idempotent role/database DDL with a fresh value.
        password = secrets.token_urlsafe(48)
        self.executor.ensure_database(expected, password)
        _mark_complete(slug, "create_database", spec=expected)
        write_pending_secret(slug, expected, password)
        return expected

    def _register(self, spec: CreateSpec, db_spec: WorkspaceConnectionSpec) -> None:
        with control_session() as session:
            row = session.get(Workspace, spec.slug)
            if row is None:
                account, _ = self.executor.service_account_identity(spec)
                row = Workspace(
                    slug=spec.slug,
                    name=spec.name,
                    status="active",
                    db_name=db_spec.db_name,
                    db_user=db_spec.db_user,
                    db_password_secret_ref=db_spec.db_password_secret_ref,
                    time_zone=spec.time_zone,
                    storage_prefix=f"workspaces/{spec.slug}/",
                    google_service_account=account,
                    google_subject=spec.dwd_subject,
                    corpus_ids=self.executor.corpus_resources(spec),
                    branding={"logo": spec.logo, "accent": spec.accent},
                    freeze_windows=list(spec.freeze_windows),
                )
                session.add(row)
                session.flush()
                session.add_all(
                    WorkspaceDomain(
                        workspace_slug=spec.slug,
                        domain=domain,
                        is_primary=index == 0,
                    )
                    for index, domain in enumerate(spec.domains)
                )
                session.add_all(
                    WorkspaceModule(
                        workspace_slug=spec.slug, module=module, enabled=True
                    )
                    for module in spec.modules
                )
            elif (
                row.db_name,
                row.db_user,
                row.db_password_secret_ref,
            ) != (
                db_spec.db_name,
                db_spec.db_user,
                db_spec.db_password_secret_ref,
            ):
                raise PermissionError("Registered database identity changed")
            migration = _step(session, spec.slug, "migrate")
            if migration is None or migration.status != "complete":
                raise RuntimeError("Migration must complete before registration")
            row.schema_revision = migration.schema_revision
            session.commit()

    def create(self, spec: CreateSpec, *, dry_run: bool = False) -> ProvisionResult:
        spec = _normalize_spec(spec)
        if dry_run:
            return self._create_normalized(spec, dry_run=True)
        # Capacity is a platform-wide reservation, not a per-slug resource.
        # Hold this lock until registration so cross-slug creates cannot both
        # pass the same N+1 headroom check.
        with _provision_lock("_capacity"), _provision_lock(spec.slug):
            return self._create_normalized(spec, dry_run=False)

    def _create_normalized(self, spec: CreateSpec, *, dry_run: bool) -> ProvisionResult:
        self._validate_control(spec)
        candidate_count = self._candidate_count(spec.slug)
        remaining = check_connection_headroom(candidate_count, self.budget)
        if dry_run:
            return ProvisionResult(
                spec.slug,
                "dry_run",
                (),
                dry_run_details=(
                    f"workspace={spec.slug}",
                    f"database={_database_spec(spec.slug).db_name}",
                    f"connection_headroom={remaining}",
                    database_grants_sql(
                        _database_spec(spec.slug).db_name,
                        _database_spec(spec.slug).db_user,
                    ),
                    *STEPS,
                ),
            )

        if not _completed(spec.slug, "validate"):
            # This step's revision slot stores an input fingerprint; the
            # migrate row stores the actual Alembic schema revision.
            _mark_complete(spec.slug, "validate", revision=_spec_fingerprint(spec))
        if not _completed(spec.slug, "check_headroom"):
            _mark_complete(spec.slug, "check_headroom")
        db_spec = self._ensure_database(spec.slug)
        if not _completed(spec.slug, "migrate"):
            revision = migrate_workspace(spec.slug, connection_spec=db_spec)
            _mark_complete(spec.slug, "migrate", revision=revision)
        if not _completed(spec.slug, "create_storage"):
            self.executor.ensure_storage(spec)
            _mark_complete(spec.slug, "create_storage")
        if not _completed(spec.slug, "create_service_account"):
            self.executor.ensure_service_account(spec)
            _mark_complete(spec.slug, "create_service_account")
        if not _completed(spec.slug, "register"):
            self._register(spec, db_spec)
            _mark_complete(spec.slug, "register")
        if not _completed(spec.slug, "seed_and_schedule"):
            _seed_registered_workspace(spec)
            clear_module_cache(spec.slug)
            with control_session() as session:
                workspace = session.get(Workspace, spec.slug)
                # Detach immutable scalar state before closing the session.
                schedule_target = Workspace(
                    slug=workspace.slug,
                    name=workspace.name,
                    status=workspace.status,
                    time_zone=workspace.time_zone,
                )
            reconcile_schedules(
                self.manifest, [schedule_target], self.executor, prune=False
            )
            _mark_complete(spec.slug, "seed_and_schedule")
        checklist: tuple[str, ...] = ()
        if not _completed(spec.slug, "checklist"):
            _, client_id = self.executor.service_account_identity(spec)
            checklist = self.executor.checklist(spec, client_id)
            _mark_complete(spec.slug, "checklist")
        return ProvisionResult(spec.slug, "complete", STEPS, checklist)

    def delete(self, slug: str, *, typed_slug: str, dry_run: bool = False) -> str:
        slug = _canonical_slug(slug)
        if typed_slug != slug:
            raise ValueError("Type the exact slug to delete a workspace")
        with nullcontext() if dry_run else _provision_lock(slug):
            with control_session() as session:
                row = session.get(Workspace, slug)
                if row is None:
                    raise ValueError("Workspace not found")
                if session.scalar(
                    select(PlatformAdmin.email).where(
                        PlatformAdmin.home_workspace_slug == slug
                    )
                ):
                    raise ValueError(
                        "Move platform admins to another home workspace first"
                    )
                if dry_run:
                    return "would_suspend"
                row.status = "suspended"
                session.commit()
        # Data and cloud resources remain until a separate retention policy
        # authorizes their irreversible removal.
        return "suspended"
