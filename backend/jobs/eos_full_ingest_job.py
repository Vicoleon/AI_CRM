"""Run the weekly EOS RAG sweep in a workspace-scoped Cloud Run Job."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def main() -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    from rubik_core.database.db import SessionLocal
    from rubik_core.ingestion.eos import ingest_all_sources

    with SessionLocal() as session:
        result = ingest_all_sources(session)
    logger.info(
        "EOS full ingestion: succeeded=%d failed=%d",
        len(result.succeeded),
        len(result.failed),
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
