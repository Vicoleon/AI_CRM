"""Build one workspace's BYOK vector candidate in the durable worker Job."""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cloud_embedding_rebuild_job")
    parser.add_argument("--run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    from rubik_core.ai_providers.cloud_embedding_workflow import (
        run_cloud_embedding_rebuild,
    )

    try:
        completed = run_cloud_embedding_rebuild(args.run_id)
    except Exception:
        logger.error("Cloud embedding rebuild could not start")
        return 1
    finally:
        try:
            from rubik_core.cost.usage import flush_usage

            flush_usage()
        except Exception:
            logger.warning("Cloud embedding cost accounting could not be flushed")
    if not completed:
        logger.error("Cloud embedding rebuild did not complete")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
