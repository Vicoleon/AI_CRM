"""Opt-in bootstrap privilege checks against a throwaway local PostgreSQL 16+."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from rubik_core.platform import deployment


@dataclass
class BootstrapCase:
    admin: Engine = field(repr=False)
    operator_url: URL = field(repr=False)
    target_url: URL = field(repr=False)
    operator: str
    target: str


def _runtime_url(url: URL, socket: str) -> URL:
    return URL.create(
        url.drivername,
        username=url.username,
        password=url.password,
        database=url.database,
        query={"unix_sock": socket},
    )


def _create_login(connection, name: str, password: str, *, operator=False) -> None:
    attributes = " CREATEDB CREATEROLE" if operator else ""
    connection.exec_driver_sql(f'CREATE ROLE "{name}" LOGIN{attributes}')
    quoted = connection.scalar(
        text("SELECT quote_literal(:password)"), {"password": password}
    )
    connection.exec_driver_sql(f'ALTER ROLE "{name}" WITH PASSWORD {quoted}')


@pytest.fixture
def pg_bootstrap_case(monkeypatch):
    raw = os.environ.get("CUBIE_TEST_PG_ADMIN_URL", "")
    if not raw:
        pytest.skip(
            "Set CUBIE_TEST_PG_ADMIN_URL for local PostgreSQL bootstrap integration"
        )
    parsed = make_url(raw)
    if parsed.get_backend_name() != "postgresql" or parsed.host not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        pytest.skip(
            "PostgreSQL bootstrap integration requires a local throwaway server"
        )
    admin = create_engine(parsed, isolation_level="AUTOCOMMIT", hide_parameters=True)
    suffix = secrets.token_hex(6)
    operator = f"cubie_bootstrap_operator_{suffix}"
    target = f"cubie_bootstrap_control_{suffix}"
    operator_password, target_password = (
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(32),
    )
    operator_url = parsed.set(
        drivername="postgresql+pg8000", username=operator, password=operator_password
    )
    target_url = operator_url.set(
        username=target, password=target_password, database=target
    )
    case = BootstrapCase(admin, operator_url, target_url, operator, target)
    resources_attempted = False
    try:
        try:
            with admin.connect() as connection:
                version = int(connection.scalar(text("SHOW server_version_num")))
                is_super = connection.scalar(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
                )
                maximum = int(connection.scalar(text("SHOW max_connections")))
                if version < 160000 or not is_super or maximum < 36:
                    pytest.skip(
                        "Requires PostgreSQL 16+ with a local test superuser and 36 connections"
                    )
                resources_attempted = True
                _create_login(connection, operator, operator_password, operator=True)
                connection.exec_driver_sql(
                    f"ALTER ROLE \"{operator}\" SET createrole_self_grant = ''"
                )
        except SQLAlchemyError:
            pytest.fail(
                "Could not prepare the isolated PostgreSQL bootstrap fixture",
                pytrace=False,
            )

        project, region, instance = "test-project", "us-central1", "test-sql"
        socket = f"/cloudsql/{project}:{region}:{instance}/.s.PGSQL.5432"
        for name, value in {
            "CONTROL_DATABASE_URL": target_url.render_as_string(hide_password=False),
            "CUBIE_PROVISION_ADMIN_DATABASE_URL": operator_url.render_as_string(
                hide_password=False
            ),
            "CUBIE_RUNTIME_CONTROL_DATABASE_URL": _runtime_url(
                target_url, socket
            ).render_as_string(hide_password=False),
            "CUBIE_RUNTIME_ADMIN_DATABASE_URL": _runtime_url(
                operator_url, socket
            ).render_as_string(hide_password=False),
            "CUBIE_OPERATOR_SQL_INSTANCE": f"{project}:{region}:{instance}",
            "CUBIE_PROVISION_DB_MAX_CONNECTIONS": str(maximum),
            "CUBIE_PROVISION_SERVICE_MAX_INSTANCES": "1",
            "CUBIE_PROVISION_JOB_MAX_INSTANCES": "1",
            "CUBIE_PROVISION_RESERVED_CONNECTIONS": "10",
        }.items():
            monkeypatch.setenv(name, value)

        def describe(*args):
            assert args[:3] == ("sql", "instances", "describe")
            return json.dumps(
                {
                    "name": instance,
                    "region": region,
                    "connectionName": f"{project}:{region}:{instance}",
                    "state": "RUNNABLE",
                    "databaseVersion": "POSTGRES_16",
                }
            )

        monkeypatch.setattr(deployment, "_gcloud_maybe", describe)
        monkeypatch.setattr(
            deployment,
            "_gcloud",
            lambda *a, **k: pytest.fail("Unexpected cloud mutation"),
        )
        operator_engine = create_engine(operator_url, hide_parameters=True)
        try:
            with operator_engine.connect() as connection:
                assert connection.scalar(text("SHOW createrole_self_grant")) == ""
                assert (
                    connection.scalar(
                        text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
                    )
                    is False
                )
        finally:
            operator_engine.dispose()
        yield case
    finally:
        try:
            if resources_attempted:
                with admin.connect() as connection:
                    connection.exec_driver_sql(
                        f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)'
                    )
                    connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{target}"')
                    connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{operator}"')
        finally:
            admin.dispose()


def _preflight() -> None:
    deployment._control_bootstrap_preflight(
        "test-project", "us-central1", "test-sql", fresh_install=True
    )


def _password_verifier(case: BootstrapCase):
    with case.admin.connect() as connection:
        return connection.scalar(
            text("SELECT rolpassword FROM pg_authid WHERE rolname=:name"),
            {"name": case.target},
        )


def _assert_owner_login(case: BootstrapCase) -> None:
    owner = create_engine(case.target_url, hide_parameters=True)
    try:
        with owner.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == case.target
            assert connection.scalar(text("SELECT current_database()")) == case.target
    finally:
        owner.dispose()


def test_pg16_non_superuser_bootstrap_owns_database_and_preserves_password_on_retry(
    pg_bootstrap_case, monkeypatch
):
    case = pg_bootstrap_case
    _preflight()
    deployment._create_fresh_control_database()
    _assert_owner_login(case)
    with case.admin.connect() as connection:
        owner = connection.scalar(
            text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=:name"),
            {"name": case.target},
        )
        public_connect = connection.scalar(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_database AS db,
                    LATERAL aclexplode(COALESCE(db.datacl, acldefault('d', db.datdba))) AS acl
                    WHERE db.datname=:name AND acl.grantee=0 AND acl.privilege_type='CONNECT'
                )
            """),
            {"name": case.target},
        )
    assert owner == case.target
    assert public_connect is False
    verifier = _password_verifier(case)

    _preflight()
    deployment._create_fresh_control_database()
    unchanged = _password_verifier(case) == verifier
    assert unchanged, "Retry must not reset an existing control-role password"

    wrong = case.target_url.set(password=secrets.token_urlsafe(32))
    monkeypatch.setenv(
        "CONTROL_DATABASE_URL", wrong.render_as_string(hide_password=False)
    )
    socket = "/cloudsql/test-project:us-central1:test-sql/.s.PGSQL.5432"
    monkeypatch.setenv(
        "CUBIE_RUNTIME_CONTROL_DATABASE_URL",
        _runtime_url(wrong, socket).render_as_string(hide_password=False),
    )
    with pytest.raises(deployment.DeployBlocked, match="could not be verified"):
        _preflight()
    unchanged = _password_verifier(case) == verifier
    assert unchanged, "A wrong retry credential must not change the existing role"
    _assert_owner_login(case)


def test_pg16_existing_role_without_owner_membership_is_refused_before_writes(
    pg_bootstrap_case,
):
    case = pg_bootstrap_case
    with case.admin.connect() as connection:
        _create_login(connection, case.target, case.target_url.password)
    verifier = _password_verifier(case)
    with pytest.raises(deployment.DeployBlocked, match="SET and INHERIT"):
        _preflight()
    with case.admin.connect() as connection:
        database_exists = connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": case.target}
        )
        membership_exists = connection.scalar(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_auth_members AS membership
                    JOIN pg_roles AS granted ON granted.oid=membership.roleid
                    JOIN pg_roles AS member ON member.oid=membership.member
                    WHERE granted.rolname=:target AND member.rolname=:operator
                )
            """),
            {"target": case.target, "operator": case.operator},
        )
    assert database_exists is None
    assert membership_exists is False
    unchanged = _password_verifier(case) == verifier
    assert unchanged, "Refused preflight must not reset an existing control role"


def test_pg16_non_superuser_provisions_workspace_database(pg_bootstrap_case):
    from rubik_core.control.models import WorkspaceConnectionSpec
    from rubik_core.platform.executor import CloudExecutor

    case = pg_bootstrap_case
    spec = WorkspaceConnectionSpec(case.target, case.target, "test")
    executor = object.__new__(CloudExecutor)
    executor.admin_database_url = case.operator_url.render_as_string(
        hide_password=False
    )

    executor.ensure_database(spec, case.target_url.password)
    _assert_owner_login(case)
    with case.admin.connect() as connection:
        owner = connection.scalar(
            text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=:name"),
            {"name": case.target},
        )
        public_connect = connection.scalar(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_database AS db,
                    LATERAL aclexplode(COALESCE(db.datacl, acldefault('d', db.datdba))) AS acl
                    WHERE db.datname=:name AND acl.grantee=0 AND acl.privilege_type='CONNECT'
                )
            """),
            {"name": case.target},
        )
    assert owner == case.target
    assert public_connect is False

    executor.ensure_database(spec, case.target_url.password)
    _assert_owner_login(case)
