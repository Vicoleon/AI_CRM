#!/usr/bin/env python3
"""
scripts/reindex_to_rag.py

Re-ingests all existing DAM assets into the Vertex AI RAG Corpus.
Reads source_path from the DAM registry, uploads each file to GCS,
and triggers a RAG Corpus import.

Usage:
    python scripts/reindex_to_rag.py [--status approved] [--dry-run]
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()

    parser = argparse.ArgumentParser(
        description="Re-index DAM assets into Vertex AI RAG Corpus"
    )
    parser.add_argument(
        "--status",
        default=None,
        help="Only re-index assets with this status (e.g. 'approved')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without uploading",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logger = logging.getLogger("reindex_to_rag")

    from rubik_core.adapters.vertex_rag import VertexRagAdapter
    from rubik_core.dam.registry import DAMRegistry
    from rubik_core.tenancy.storage import assert_own_uri

    logger.info("Loading DAM registry...")
    registry = DAMRegistry()
    all_assets = registry.list_assets()
    logger.info(f"Found {len(all_assets)} total assets in DAM.")

    # Filter by status if requested
    assets_to_process = {}
    for doc_id, meta in all_assets.items():
        asset_status = meta.get("status", "")
        if args.status and asset_status != args.status:
            logger.info(
                f"Skipping [{asset_status}] {meta.get('original_filename', doc_id)}"
            )
            continue
        assets_to_process[doc_id] = meta

    logger.info(f"Will process {len(assets_to_process)} assets.")

    if args.dry_run:
        logger.info("DRY RUN — no uploads will happen.")
        for _doc_id, meta in assets_to_process.items():
            src = meta.get("source_path", "?")
            fname = meta.get("original_filename", Path(src).name)
            status = meta.get("status", "?")
            logger.info(f"  Would upload [{status}] {fname}  from {src}")
        return

    adapter = VertexRagAdapter()
    success = 0
    failed = 0

    for doc_id, meta in assets_to_process.items():
        source_path = meta.get("source_path", "")
        fname = meta.get("original_filename") or Path(source_path).name
        status = meta.get("status", "?")

        if not source_path:
            logger.warning(f"Asset {doc_id} ({fname}) has no source_path, skipping.")
            failed += 1
            continue

        try:
            checked = assert_own_uri(source_path)
        except ValueError:
            logger.warning(
                "Asset %s source is outside this workspace — skipping", doc_id
            )
            failed += 1
            continue

        file_path = Path(checked) if source_path.startswith("file://") else None
        if file_path is not None and not file_path.exists():
            logger.warning(f"File not found: {file_path} — skipping {fname}")
            failed += 1
            continue

        try:
            logger.info(f"Uploading [{status}] {fname}...")
            gcs_uri = (
                adapter.upload_to_gcs(file_path) if file_path is not None else checked
            )
            adapter.import_gcs_uri_to_corpus(gcs_uri)
            logger.info(f"  ✓ Imported {fname} → {gcs_uri}")
            success += 1
        except Exception as e:
            logger.error(f"  ✗ Failed to process {fname}: {e}")
            failed += 1

    logger.info(f"\nDone. {success} succeeded, {failed} failed.")


if __name__ == "__main__":
    main()
