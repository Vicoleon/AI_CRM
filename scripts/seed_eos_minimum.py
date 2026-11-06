"""Seed the EOS subdomain with the minimum rows needed for the UI to render.

What this creates:
- One `eos_organizations` row ("Your company", rename it in EOS) — idempotent.
- One draft `eos_vto_versions` row attached to that organization — idempotent.
- One `eos_quarters` row covering the current calendar quarter, with
  `is_current=True` so Rock creation works out of the box — idempotent
  on (organization, name). If any quarter on this org already has
  `is_current=True`, no new quarter is created.

Cubie's `departments` table is the source of truth for departments now
(EOS no longer has its own departments table). This script does NOT touch
that table; use the Admin UI to populate departments first.

Usage:
    python scripts/seed_eos_minimum.py            # idempotent re-runs safe
    python scripts/seed_eos_minimum.py --force    # delete + recreate (DEV ONLY)

Confirm the target DB (DATABASE_URL / CloudSQL connection) before running
against anything other than local dev.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

# Add project root to path so `rubik_core` and `backend` import cleanly when
# the script is invoked from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rubik_core.database.db import SessionLocal  # noqa: E402
from rubik_core.database.eos import (  # noqa: E402
    Organization,
    Quarter,
    VTOVersion,
)

ORG_NAME = "Your company"
ORG_DESCRIPTION = "The company's EOS organization."
INITIAL_VTO_VERSION_NAME = "v1 (draft)"


def _current_quarter_window(
    today: datetime | None = None,
) -> tuple[str, datetime, datetime]:
    """Return ("Qn YYYY", start_of_quarter, end_of_quarter) for the calendar
    quarter that contains `today` (UTC). End is the last day of the quarter
    at 00:00 — fine for our purposes since the field is a free-form DateTime.
    """
    today = today or datetime.utcnow()
    quarter_num = (today.month - 1) // 3 + 1
    start_month = (quarter_num - 1) * 3 + 1
    start = datetime(today.year, start_month, 1)
    if quarter_num == 4:
        end = datetime(today.year, 12, 31)
    else:
        next_q_start = datetime(today.year, start_month + 3, 1)
        end = next_q_start - timedelta(days=1)
    return f"Q{quarter_num} {today.year}", start, end


def seed(force: bool = False) -> None:
    session = SessionLocal()
    try:
        existing_org = (
            session.query(Organization).filter(Organization.name == ORG_NAME).first()
        )
        if existing_org and force:
            print(
                f"[--force] Deleting existing organization {ORG_NAME} (id={existing_org.id})"
            )
            session.delete(existing_org)
            session.flush()
            existing_org = None

        if existing_org:
            org = existing_org
            print(
                f"Organization '{ORG_NAME}' already exists (id={org.id}); skipping insert."
            )
        else:
            org = Organization(name=ORG_NAME, description=ORG_DESCRIPTION)
            session.add(org)
            session.flush()
            print(f"Created organization '{ORG_NAME}' (id={org.id})")

        existing_vto = (
            session.query(VTOVersion)
            .filter(
                VTOVersion.organization_id == org.id,
                VTOVersion.version_name == INITIAL_VTO_VERSION_NAME,
            )
            .first()
        )
        if existing_vto:
            print(
                f"Draft VTOVersion '{INITIAL_VTO_VERSION_NAME}' already exists "
                f"(id={existing_vto.id}); skipping."
            )
        else:
            vto = VTOVersion(
                organization_id=org.id,
                version_name=INITIAL_VTO_VERSION_NAME,
                is_active=False,
                status="draft",
            )
            session.add(vto)
            session.flush()
            print(
                f"Created draft VTOVersion '{INITIAL_VTO_VERSION_NAME}' (id={vto.id})"
            )

        # Ensure there's a current Quarter so Rock create works out of the
        # box. Idempotent: if *any* quarter on this org already has
        # is_current=True, we leave the world alone. Otherwise we create one
        # for the calendar quarter that contains today.
        any_current = (
            session.query(Quarter)
            .filter(
                Quarter.organization_id == org.id,
                Quarter.is_current == True,  # noqa: E712
                Quarter.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if any_current:
            print(
                f"Current quarter '{any_current.name}' already set "
                f"(id={any_current.id}); skipping."
            )
        else:
            qname, qstart, qend = _current_quarter_window()
            existing_named = (
                session.query(Quarter)
                .filter(
                    Quarter.organization_id == org.id,
                    Quarter.name == qname,
                )
                .first()
            )
            if existing_named:
                existing_named.is_current = True
                print(
                    f"Flipped existing Quarter '{qname}' (id={existing_named.id}) to is_current=True"
                )
            else:
                q = Quarter(
                    organization_id=org.id,
                    name=qname,
                    start_date=qstart,
                    end_date=qend,
                    is_current=True,
                )
                session.add(q)
                session.flush()
                print(f"Created Quarter '{qname}' (id={q.id}, is_current=True)")

        session.commit()
        print("Seed complete.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main() -> None:
    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete and recreate the organization row (DEV ONLY).",
    )
    args = parser.parse_args()
    seed(force=args.force)


if __name__ == "__main__":
    main()
