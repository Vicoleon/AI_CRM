"""Run one user's nightly briefing with a validated workspace Job context."""

from __future__ import annotations

import argparse
import logging

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    parser = argparse.ArgumentParser(description="Run one user's nightly briefing")
    parser.add_argument("--target-email", required=True)
    args = parser.parse_args(argv)
    email = args.target_email.strip().lower()
    if not email or "@" not in email:
        return 2

    from sqlalchemy import func

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import User

    with SessionLocal() as db:
        if (
            db.query(User.id)
            .filter(
                func.lower(User.email) == email, User.is_platform_support.is_(False)
            )
            .first()
            is None
        ):
            logger.warning("Nightly briefing target is outside the workspace")
            return 2

    from backend.routers.jobs import _run_pipeline_sync

    _run_pipeline_sync(email)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
