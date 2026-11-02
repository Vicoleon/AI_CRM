"""Explicit platform operator commands; no ambient workspace is selected."""

from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import select

from rubik_core.control.db import control_session
from rubik_core.control.models import PlatformAdmin, Workspace, WorkspaceDomain
from rubik_core.platform.executor import LocalExecutor, default_executor
from rubik_core.platform.migrate import migrate_workspace
from rubik_core.platform.provision import CreateSpec, Provisioner, _provision_lock
from rubik_core.tenancy.context import _canonical_slug


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rubik_core.platform.cli")
    groups = parser.add_subparsers(dest="group", required=True)
    workspaces = groups.add_parser("workspace")
    workspace_actions = workspaces.add_subparsers(dest="action", required=True)
    create = workspace_actions.add_parser("create")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--domain", action="append", required=True)
    create.add_argument("--first-admin", action="append", required=True)
    create.add_argument("--dwd-subject", required=True)
    create.add_argument("--time-zone", required=True)
    create.add_argument("--logo")
    create.add_argument("--accent")
    create.add_argument("--module", action="append", default=[])
    create.add_argument("--freeze-window", action="append", default=[])
    create.add_argument("--dry-run", action="store_true")
    create.add_argument("--actor-email", required=True)
    workspace_list = workspace_actions.add_parser("list")
    workspace_list.add_argument("--actor-email", required=True)
    migrate = workspace_actions.add_parser("migrate")
    migrate.add_argument("--slug", required=True)
    migrate.add_argument("--actor-email", required=True)
    delete = workspace_actions.add_parser("delete")
    delete.add_argument("--slug", required=True)
    delete.add_argument("--confirm-slug", required=True)
    delete.add_argument("--dry-run", action="store_true")
    delete.add_argument("--actor-email", required=True)

    admins = groups.add_parser("platform-admin")
    admin_actions = admins.add_subparsers(dest="action", required=True)
    add = admin_actions.add_parser("add")
    add.add_argument("--email", required=True)
    add.add_argument("--home-workspace", required=True)
    add.add_argument("--actor-email", required=True)
    admin_list = admin_actions.add_parser("list")
    admin_list.add_argument("--actor-email", required=True)
    remove = admin_actions.add_parser("remove")
    remove.add_argument("--email", required=True)
    remove.add_argument("--confirm-email", required=True)
    remove.add_argument("--actor-email", required=True)
    return parser


def _authorize_operator(args: argparse.Namespace) -> str:
    actor = args.actor_email.strip().lower()
    if not actor or "@" not in actor:
        raise PermissionError("An explicit platform operator email is required")
    with control_session() as session:
        admin = session.get(PlatformAdmin, actor)
        if admin is not None:
            home = session.get(Workspace, admin.home_workspace_slug)
            if home is not None and home.status == "active":
                return actor
        any_admin = session.scalar(select(PlatformAdmin.email)) is not None
    bootstrap = (
        os.environ.get("CUBIE_PROVISION_BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    )
    if any_admin or not bootstrap or actor != bootstrap:
        raise PermissionError("A platform admin operator is required")
    if args.group == "workspace" and args.action == "create":
        if actor not in {email.strip().lower() for email in args.first_admin}:
            raise PermissionError("Bootstrap operator must be a first admin")
    elif args.group == "platform-admin" and args.action == "add":
        if args.email.strip().lower() != actor:
            raise PermissionError("First platform admin must be the bootstrap operator")
    else:
        raise PermissionError("Bootstrap operator may only create its home workspace")
    return actor


def _workspace_list() -> list[dict[str, object]]:
    with control_session() as session:
        rows = session.scalars(select(Workspace).order_by(Workspace.slug)).all()
        return [
            {
                "slug": row.slug,
                "name": row.name,
                "status": row.status,
                "time_zone": row.time_zone,
                "domains": [
                    domain.domain
                    for domain in session.scalars(
                        select(WorkspaceDomain)
                        .where(WorkspaceDomain.workspace_slug == row.slug)
                        .order_by(WorkspaceDomain.domain)
                    )
                ],
            }
            for row in rows
        ]


def _platform_admin(args: argparse.Namespace) -> object:
    if args.action == "list":
        with control_session() as session:
            return [
                {"email": row.email, "home_workspace": row.home_workspace_slug}
                for row in session.scalars(
                    select(PlatformAdmin).order_by(PlatformAdmin.email)
                )
            ]
    email = args.email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("An exact admin email is required")
    # Serialize admin mutations so concurrent removals cannot each observe
    # the other as the remaining active operator.
    with _provision_lock("_platform_admin"), control_session() as session:
        if args.action == "add":
            slug = _canonical_slug(args.home_workspace)
            workspace = session.get(Workspace, slug)
            if workspace is None or workspace.status != "active":
                raise ValueError("An active home workspace is required")
            row = session.get(PlatformAdmin, email)
            if row is None:
                session.add(PlatformAdmin(email=email, home_workspace_slug=slug))
            elif row.home_workspace_slug != slug:
                raise ValueError("Platform admin already has another home workspace")
        elif args.action == "remove":
            if args.confirm_email != email:
                raise ValueError("Type the exact admin email to remove it")
            row = session.get(PlatformAdmin, email)
            if row is None:
                raise ValueError("Platform admin not found")
            home = session.get(Workspace, row.home_workspace_slug)
            if home is not None and home.status == "active":
                other_active = session.scalar(
                    select(PlatformAdmin.email)
                    .join(
                        Workspace,
                        PlatformAdmin.home_workspace_slug == Workspace.slug,
                    )
                    .where(
                        PlatformAdmin.email != email,
                        Workspace.status == "active",
                    )
                    .limit(1)
                )
                if other_active is None:
                    raise ValueError("Cannot remove the last active platform admin")
            session.delete(row)
        session.commit()
    return {"email": email, "status": args.action}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _authorize_operator(args)
        if args.group == "platform-admin":
            result = _platform_admin(args)
        elif args.action == "list":
            result = _workspace_list()
        elif args.action == "migrate":
            slug = _canonical_slug(args.slug)
            result = {"slug": slug, "schema_revision": migrate_workspace(slug)}
        elif args.action == "delete":
            slug = _canonical_slug(args.slug)
            state = Provisioner(
                executor=LocalExecutor() if args.dry_run else default_executor()
            ).delete(slug, typed_slug=args.confirm_slug, dry_run=args.dry_run)
            result = {
                "slug": slug,
                "status": state,
                "data_and_cloud_resources": "retained",
            }
        else:
            freeze_windows = tuple(json.loads(value) for value in args.freeze_window)
            spec = CreateSpec(
                slug=args.slug,
                name=args.name,
                domains=tuple(args.domain),
                first_admins=tuple(args.first_admin),
                dwd_subject=args.dwd_subject,
                time_zone=args.time_zone,
                logo=args.logo,
                accent=args.accent,
                modules=tuple(args.module),
                freeze_windows=freeze_windows,
            )
            outcome = Provisioner(
                executor=LocalExecutor() if args.dry_run else default_executor()
            ).create(spec, dry_run=args.dry_run)
            result = {
                "slug": outcome.slug,
                "status": outcome.status,
                "completed_steps": outcome.completed_steps,
                "checklist": outcome.checklist,
                "dry_run_details": outcome.dry_run_details,
            }
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, PermissionError, RuntimeError) as exc:
        # These messages contain identifiers and diagnostics, never credential
        # values. Cloud API failures are sanitized by the executor boundary.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
