"""Version-history helpers for DAM assets.

What lives here is the part of versioning that is not HTTP: which documents
may be versioned at all, turning a stored file back into text, the AI-written
"what changed" note for a revision, and the storage sweep when a versioned
asset is deleted. The endpoints are in ``backend/routers/dam.py``.
"""

from __future__ import annotations

import difflib
import logging
import tempfile
from pathlib import Path
from typing import Any

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import AppMode, settings
from rubik_core.cost.usage import attribute_to
from rubik_core.gemini_tuning import generation_config_for
from rubik_core.models import get_genai_client
from rubik_core.tenancy.storage import assert_own_uri

logger = logging.getLogger(__name__)

# Topic that marks auto-synced meeting transcripts. They live in their own
# corpus and are re-synced from the recording, so they are not versionable.
TRANSCRIPT_TOPIC = "Meeting Transcripts"

# Cap per side for the change summary: two documents' worth of text is the
# largest prompt the DAM sends, and a few thousand words per side is plenty to
# describe what moved.
_SUMMARY_MAX_CHARS = 12_000


def version_block_reason(asset: Any) -> str | None:
    """Why a new version can't be uploaded for ``asset`` — or None if it can.

    Archived docs are out of the RAG on purpose (re-adding them is a deliberate
    re-upload); Cubie-generated briefs are never ingested and are regenerated,
    not edited; transcripts are synced from the recording.
    """
    from backend.services.generated_docs import is_generated_asset

    if getattr(asset, "status", None) == "archived":
        return "This document is archived. Re-upload it to bring it back before adding a version."
    if is_generated_asset(asset):
        return f"{PRODUCT_NAME}-generated briefs can't take new versions — generate a fresh brief instead."
    if (getattr(asset, "topic", None) or "") == TRANSCRIPT_TOPIC:
        return (
            "Meeting transcripts are synced from the recording and can't be versioned."
        )
    return None


def versioned_storage_name(filename: str, content_hash: str) -> str:
    """``report.pdf`` + hash → ``report__1a2b3c4d.pdf``.

    Every revision gets its own storage object: the filename-keyed GCS path
    would otherwise be shared, and Vertex keys ragFiles by URI — re-importing
    the same URI leaves the old ragFile's chunks alongside the new ones, and
    purging by that URI would take the new bytes with it.
    """
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    return f"{stem}__{content_hash[:8]}{suffix}"


def materialize(source_path: str) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    """A local Path for ``source_path`` (a gs:// URI is downloaded to a tempdir).

    The caller must ``cleanup()`` the returned tempdir when it is not None.
    """
    checked = assert_own_uri(source_path)
    if not source_path.startswith("gs://"):
        return Path(checked), None
    from backend.services.generated_docs import load_doc_bytes

    td = tempfile.TemporaryDirectory(prefix="dam-version-")
    local = Path(td.name) / Path(source_path).name
    local.write_bytes(load_doc_bytes(source_path))
    return local, td


def text_of(source_path: str | None) -> str:
    """Extracted text of a stored file, or "" when it can't be read."""
    if not source_path:
        return ""
    td = None
    try:
        local, td = materialize(source_path)
        from rubik_core.ingestion.loader import DocumentLoader

        loaded = DocumentLoader().load_file(local)
        return (loaded.text if loaded else "") or ""
    except Exception:  # noqa: BLE001 — a summary is nice-to-have
        logger.warning(
            "versions: could not read text of %s", source_path, exc_info=True
        )
        return ""
    finally:
        if td is not None:
            try:
                td.cleanup()
            except Exception:
                pass


def _heuristic_change_summary(previous_text: str, new_text: str) -> str | None:
    old_words = previous_text.split()
    new_words = new_text.split()
    if not old_words and not new_words:
        return None
    if not old_words:
        return "First version with readable text."
    if not new_words:
        return "No readable text could be extracted from this version."
    ratio = difflib.SequenceMatcher(
        None, old_words[:5000], new_words[:5000], autojunk=False
    ).ratio()
    changed_pct = max(0, min(100, round((1 - ratio) * 100)))
    delta = len(new_words) - len(old_words)
    if delta > 0:
        length = f"about {delta:,} words longer"
    elif delta < 0:
        length = f"about {-delta:,} words shorter"
    else:
        length = "the same length"
    if changed_pct == 0:
        return "The text is unchanged from the previous version."
    return f"About {changed_pct}% of the text changed; this version is {length}."


@attribute_to("dam")
def summarize_changes(
    previous_text: str,
    new_text: str,
    *,
    previous_name: str | None = None,
    new_name: str | None = None,
) -> str | None:
    """2-3 plain sentences on what the new version changed.

    Gemini Lite when Vertex is configured; a word-level diff heuristic
    otherwise or on any error, so a version row always gets *some* note.
    Returns None only when neither version has readable text.
    """
    prev = " ".join((previous_text or "").split())[:_SUMMARY_MAX_CHARS]
    new = " ".join((new_text or "").split())[:_SUMMARY_MAX_CHARS]
    if not prev and not new:
        return None

    from rubik_core.ai_providers.registry import model_routes

    try:
        selected = model_routes().get("dam_version_summary") or {}
    except Exception:
        selected = {"provider": "unavailable"}
    client_selected = selected.get("provider") not in (None, "vertex_managed")

    if (
        (settings.APP_MODE == AppMode.GCP or settings.GCP_PROJECT_ID or client_selected)
        and prev
        and new
    ):
        try:
            from rubik_core.inference_workloads import run_text

            names = ""
            if previous_name or new_name:
                names = f"Previous file: {previous_name or 'unknown'}. New file: {new_name or 'unknown'}.\n"
            prompt = (
                "You are comparing two versions of the same company document for a "
                "version-history entry. In 2-3 plain sentences, state what the NEW "
                "version changed compared with the PREVIOUS one: sections added or "
                "removed, and any numbers, dates, names, prices or policies that "
                "differ. Be specific and factual. If nothing meaningful changed, say "
                "so in one sentence. Plain text, no preamble, no markdown.\n\n"
                f"{names}"
                f"=== PREVIOUS VERSION ===\n{prev}\n\n"
                f"=== NEW VERSION ===\n{new}"
            )

            def managed(route):
                resp = get_genai_client().models.generate_content(
                    model=route.model,
                    contents=prompt,
                    config=generation_config_for(route.model),
                )
                return getattr(resp, "text", "") or ""

            out = run_text(
                "dam_version_summary",
                prompt,
                default_model=settings.VERTEX_LLM_MODEL_LITE,
                managed=managed,
            )
            out = out.strip()
            if out:
                return out
            if client_selected:
                return "AI change summary unavailable; " + (
                    _heuristic_change_summary(prev, new) or "No visible text change."
                )
        except Exception:  # noqa: BLE001 - retain a visible heuristic
            logger.warning("Change summary generation failed")
            if client_selected:
                return "AI change summary unavailable; " + (
                    _heuristic_change_summary(prev, new) or "No visible text change."
                )

    return _heuristic_change_summary(prev, new)


def purge_version_bytes(versions: list[Any], *, keep_source_path: str | None) -> None:
    """Best-effort delete of every superseded version's stored file.

    Called when an asset is deleted outright. The CURRENT version's object is
    removed by the vector-store delete (``also_delete_gcs=True``), so it is
    passed as ``keep_source_path`` and skipped here.
    """
    from backend.services.generated_docs import delete_doc_bytes

    seen: set[str] = set()
    for v in versions:
        sp = getattr(v, "source_path", None)
        if not sp or sp == keep_source_path or sp in seen:
            continue
        seen.add(sp)
        delete_doc_bytes(sp)
