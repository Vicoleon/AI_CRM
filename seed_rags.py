from __future__ import annotations

"""
seed_rags.py — first-run ingestion for the audience-shaped Sales-Brain RAGs.

What this does
--------------
1. Bootstraps any missing Vertex AI RAG corpora for every slug in the live
   registry (companywide + every row in the `departments` table).
2. Walks a knowledge content directory (default: `./rubik_core_content/`)
   whose subdirectories MUST be valid RAG slugs:

       rubik_core_content/
           companywide/    *.md
           sales/          *.md
           hr/             *.md
           finance/        *.md
           ...

   Files placed directly in `rubik_core_content/` (no subdirectory) are
   reported and skipped — never silently routed.

Idempotency
-----------
Re-running is safe. Files whose `gs://` URI already appears in the slug's
corpus are skipped. To force a re-import for a single file, use
PUT /api/rag/<slug>/documents which deletes the stale ragFile cleanly.

Usage
-----
    APP_MODE=gcp \\
    GCP_PROJECT_ID=... GCS_BUCKET_NAME=... VERTEX_RAG_LOCATION=us-south1 \\
    python seed_rags.py [--content-dir ./rubik_core_content] [--dry-run]

Always seed Dev first; confirm `GET /api/rag/<slug>/documents` returns the
expected files before running against Prod.
"""

import argparse
import logging
import sys
from pathlib import Path

from rubik_core.config import AppMode, settings
from rubik_core.logging_config import setup_logging
from rubik_core.rag import RagManager, build_registry
from rubik_core.rag.manager import RagOperationError

logger = logging.getLogger("seed_rags")

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONTENT_DIR = REPO_ROOT / "rubik_core_content"


def _existing_gcs_uris(manager: RagManager, slug: str) -> set[str]:
    """Return the set of GCS URIs already imported into a slug's corpus."""
    docs = manager.list_documents(slug)
    return {d.gcs_uri for d in docs if d.gcs_uri}


def _seed_file(
    manager: RagManager,
    slug: str,
    local_path: Path,
    *,
    already_indexed: set[str],
    dry_run: bool,
) -> str:
    """Import one file unless already indexed. Returns a status string."""
    spec = manager.registry[slug]
    candidate_uri = (
        f"gs://{manager.bucket_name}/{manager._gcs_blob_name(spec, local_path.name)}"
    )
    if candidate_uri in already_indexed:
        logger.info("[skip] %s — already indexed in rag=%s", local_path.name, slug)
        return "skipped"
    if dry_run:
        logger.info(
            "[dry-run] would add %s → rag=%s (%s)", local_path, slug, candidate_uri
        )
        return "dry-run"
    try:
        result = manager.add_document(slug, local_path)
    except RagOperationError as e:
        logger.error("[fail] %s → rag=%s: %s", local_path, slug, e)
        return "failed"
    logger.info("[ok] %s → rag=%s | gcs=%s", local_path.name, slug, result["gcs_uri"])
    return "added"


def seed(
    content_dir: Path,
    *,
    dry_run: bool,
    departments: list[str] | None = None,
) -> dict[str, dict[str, int]]:
    """Run the full seed flow. Returns per-RAG counters.

    If `departments` is provided, the registry is built from that list directly
    (bypassing the SQL `departments` table) — useful for first-run seeding
    against environments where the SQL table is empty or out of sync. The
    Vertex corpora that get created are independent of any SQL state and will
    be reused once the SQL `departments` table is populated to match.
    """
    if settings.APP_MODE != AppMode.GCP:
        logger.error(
            "seed_rags.py requires APP_MODE=gcp (current=%s). Aborting to avoid "
            "writing prod-shaped data into a local Chroma store.",
            settings.APP_MODE,
        )
        sys.exit(2)

    if departments:
        registry = build_registry(departments)
        logger.info("Using explicit department list: %s", departments)
        manager = RagManager(registry=registry)
    else:
        manager = RagManager()
    known = manager.known_slugs()
    logger.info("Live RAG registry: %s", known)

    # 1. Bootstrap corpora for every slug in the live registry.
    if dry_run:
        logger.info("[dry-run] would ensure corpora exist for: %s", known)
    else:
        try:
            corpora = manager.ensure_all_corpora()
            logger.info("Corpora ready: %s", corpora)
        except RagOperationError as e:
            logger.error("Bootstrap failed: %s", e)
            sys.exit(3)

    counters: dict[str, dict[str, int]] = {
        slug: {"added": 0, "skipped": 0, "failed": 0, "dry-run": 0} for slug in known
    }

    # 2. Walk the content directory by subdirectory → slug.
    if not content_dir.is_dir():
        logger.warning(
            "Content dir not found: %s — no .md files seeded. "
            "Create subdirectories named after RAG slugs (%s) under that path.",
            content_dir,
            known,
        )
        return counters

    for subdir in sorted(p for p in content_dir.iterdir() if p.is_dir()):
        slug = subdir.name
        if slug not in known:
            logger.warning(
                "Unknown subdirectory %s/ in content dir — slug %r is not in the "
                "live registry %s. Files inside will NOT be ingested.",
                subdir,
                slug,
                known,
            )
            continue
        already = set() if dry_run else _existing_gcs_uris(manager, slug)
        for md_file in sorted(subdir.glob("*.md")):
            status = _seed_file(
                manager, slug, md_file, already_indexed=already, dry_run=dry_run
            )
            counters[slug][status] += 1

    # Warn about loose files at the top of content_dir (not silently dropped).
    for stray in content_dir.glob("*.md"):
        logger.warning(
            "Stray %s lives directly in %s — move it under one of %s to ingest.",
            stray.name,
            content_dir,
            known,
        )

    return counters


def main() -> int:
    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()

    setup_logging()
    parser = argparse.ArgumentParser(description="Seed Sales-Brain RAG corpora.")
    parser.add_argument(
        "--content-dir",
        type=Path,
        default=DEFAULT_CONTENT_DIR,
        help=f"Knowledge content root (default: {DEFAULT_CONTENT_DIR})",
    )
    parser.add_argument(
        "--departments",
        type=str,
        default=None,
        help=(
            "Comma-separated department labels to use as the registry "
            "(bypasses the SQL departments table). Example: "
            "--departments 'Sales,HR,Finance,Marketing,Operations,Engineering'"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would happen without uploading or importing.",
    )
    args = parser.parse_args()

    departments_list: list[str] | None = None
    if args.departments:
        departments_list = [d.strip() for d in args.departments.split(",") if d.strip()]

    counters = seed(
        args.content_dir.resolve(),
        dry_run=args.dry_run,
        departments=departments_list,
    )
    logger.info("Seed summary: %s", counters)
    failed = sum(c["failed"] for c in counters.values())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
