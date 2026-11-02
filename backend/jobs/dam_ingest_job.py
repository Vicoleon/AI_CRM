"""Cloud Run Job entrypoint for one DAM document ingest (#9).

Launched by ``backend/routers/dam.py`` (``POST /dam/upload``) as one execution
of the ``cubie-worker`` job:

    python -m backend.jobs.dam_ingest_job --doc-id D --actor-email E \
        [--replace-doc-id R] [--version N --supersede-source-path gs://...]

The upload endpoint stages the raw bytes at the asset's final GCS path
(``source_path``) before launching, because this Job runs on a different
machine than the request that received the multipart body. Everything else —
AI description generation, corpus routing, registry status flips, the
replace-purge — is the same ``_run_ingest`` the BackgroundTask fallback runs,
so the two executors behave identically.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dam_ingest_job")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dam_ingest_job")
    p.add_argument("--doc-id", required=True)
    p.add_argument("--actor-email", required=True)
    p.add_argument("--replace-doc-id", default=None)
    # New-version ingest: which version row this is, and the previous
    # version's URI to retire from the corpus once the new one is indexed.
    p.add_argument("--version", type=int, default=None)
    p.add_argument("--supersede-source-path", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    import tempfile
    from pathlib import Path

    from google.cloud import storage

    # Deferred imports: pulls the app graph (DB, settings, Vertex).
    from backend.routers.dam import FileProcessor, _run_ingest, dam_registry
    from backend.routers.ingest import get_processor
    from rubik_core.config import settings
    from rubik_core.tenancy.storage import assert_own_uri

    asset = dam_registry.get_asset(args.doc_id)
    if asset is None:
        logger.error(
            "dam_ingest_job: asset %s not found — nothing to ingest", args.doc_id
        )
        return 0

    source = asset.source_path or ""
    try:
        assert_own_uri(source)
        if args.supersede_source_path:
            assert_own_uri(args.supersede_source_path)
    except ValueError:
        logger.error("dam_ingest_job: source is outside the selected workspace")
        dam_registry.update_asset(
            args.doc_id, {"status": "failed", "processing": False}
        )
        return 0
    if not source.startswith("gs://"):
        # The launcher only routes here in GCP mode, where source_path is
        # always the staged GCS URI — anything else means the staging upload
        # never happened; fail the asset visibly instead of hanging in
        # "processing" forever.
        logger.error(
            "dam_ingest_job: asset %s source_path %r is not a GCS URI",
            args.doc_id,
            source,
        )
        dam_registry.update_asset(
            args.doc_id, {"status": "failed", "processing": False}
        )
        return 0

    bucket_name, blob_name = source.removeprefix("gs://").split("/", 1)
    with tempfile.TemporaryDirectory(prefix="dam-ingest-") as td:
        # Keep the staged basename: the GCS blob name (== source_path) derives
        # from it, and the replace-purge matches on that exact URI.
        local_path = Path(td) / Path(blob_name).name
        try:
            storage.Client(project=settings.GCP_PROJECT_ID).bucket(bucket_name).blob(
                blob_name
            ).download_to_filename(str(local_path))
        except Exception:  # noqa: BLE001 — fail the asset, not the task
            logger.exception(
                "dam_ingest_job: could not download staged bytes %s", source
            )
            dam_registry.update_asset(
                args.doc_id, {"status": "failed", "processing": False}
            )
            return 0

        # Rebuild the manual metadata exactly as the upload endpoint does —
        # the asset row (registered before launch) is the source of truth.
        manual_metadata = {
            "owner": asset.owner,
            "approver": "",
            "topic": asset.topic,
            "department": asset.department,
            "description": (asset.description or "").strip(),
            "tags": ",".join(asset.tags or []),
            "status": asset.status or "approved",
            "is_company_wide": asset.is_company_wide,
        }

        # _run_ingest owns registry status flips, audit logging, and the
        # replace-purge; it never raises. cleanup_dir=None — our tempdir
        # context handles cleanup.
        _run_ingest(
            FileProcessor(get_processor()),
            local_path,
            args.doc_id,
            asset.department,
            asset.is_company_wide,
            manual_metadata,
            args.actor_email,
            None,
            args.replace_doc_id,
            args.version,
            args.supersede_source_path,
        )

    # Separate process: flush the AI-Cost ledger (the auto-description is an
    # LLM call) before exit rather than relying on the atexit handler.
    try:
        from rubik_core.cost.usage import flush_usage

        flush_usage()
    except Exception:  # noqa: BLE001 — cost accounting must never fail the job
        logger.debug("dam_ingest_job: cost flush failed", exc_info=True)

    logger.info("dam_ingest_job done doc_id=%s", args.doc_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
