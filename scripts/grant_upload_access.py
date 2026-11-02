"""Grant a user the ``editor`` role — the role that carries ``assets:create``,
i.e. permission to add documents to the knowledge base.

Why this exists: role assignment is entirely manual, and moving someone into a
new department (a promotion) doesn't change their roles. A person moved into
Sales with ``roles=['user']`` keeps the base ``user`` role, which grants
``assets: read`` only, so their first upload attempt 403s while their Sales
peers — who hold ``editor`` — are unaffected.

Adds ``editor`` and keeps every role the user already has. Idempotent: a user
who already holds it is left alone.

    # dry run — prints what it WOULD do, writes nothing
    cd <repo> && \
      python -m scripts.grant_upload_access --dry-run ana@acme.example

    # apply
    cd <repo> && \
      python -m scripts.grant_upload_access ana@acme.example
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

UPLOAD_ROLE = "editor"


def main() -> int:
    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("emails", nargs="+", help="user email(s) to grant")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the intended change without writing",
    )
    args = parser.parse_args()

    from rubik_core.auth.permissions import has_permission
    from rubik_core.auth.registry import UserRegistry

    reg = UserRegistry()

    if UPLOAD_ROLE not in reg.get_role_names():
        print(
            f"ERROR: role {UPLOAD_ROLE!r} does not exist in this database. "
            "update_user() silently drops unknown role names, so refusing to "
            "write a change that would strip the user's roles instead.",
            file=sys.stderr,
        )
        return 1

    exit_code = 0
    for email in args.emails:
        user = reg.get_user(email)
        if not user:
            print(f"SKIP {email}: no such user", file=sys.stderr)
            exit_code = 1
            continue

        if has_permission(user, "assets", "create"):
            print(f"OK   {email}: already has upload access (roles={user.roles})")
            continue

        new_roles = sorted(set(user.roles) | {UPLOAD_ROLE})
        if args.dry_run:
            print(f"DRY  {email}: roles {user.roles} -> {new_roles}")
            continue

        reg.update_user(email, {"roles": new_roles})
        after = reg.get_user(email)
        granted = bool(after and has_permission(after, "assets", "create"))
        print(
            f"{'DONE' if granted else 'FAIL'} {email}: "
            f"roles={after.roles if after else None} upload_access={granted}"
        )
        if not granted:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
