"""The private library: one Vertex RAG corpus, every search fenced to one owner.

Vertex RAG Engine has no per-chunk metadata filter (``rag/config.py``), but a
retrieval can be confined to named files: ``RagResource(rag_corpus=…,
rag_file_ids=[…])``. So every person's files share one corpus, and each search
names exactly the files ``chat_uploads`` says that person owns. The fence is
applied by Vertex BEFORE ranking, not by filtering someone else's hits out
afterwards, so another person's file can neither leak nor crowd out a result.
:func:`search` refuses to run with no file ids at all: an empty list would
search the whole corpus.

Not a ``rag/config.py`` registry slug, deliberately: a registry entry is
reachable by the
ungated ``RagManager.query(rags=None)`` fan-out and by every admin and exec
through ``rags_for_user_email``. This corpus is reachable only through this
module, and only ``service`` calls it, with ids it read under an owner filter.

Files go in with ``rag.upload_file`` (synchronous, returns the new ragFile, so
its id is recorded straight away) rather than the GCS import the shared corpora
use: the import cannot say which ragFile it made. An uploaded file's retrieval
hits carry its display name (``chatfile__<upload id>.md``) as ``source_uri``,
which :func:`upload_id_from_source` maps back for the SQL re-check.

``None``/``[]``/``False`` from here mean "not available": the caller keeps the
file for the chats it is attached to and never falls back to a shared corpus.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import storage

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import AppMode, settings
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.storage import object_name, safe_upload_name

logger = logging.getLogger(__name__)

DISPLAY_PREFIX = "chatfile__"
DISPLAY_SUFFIX = ".md"

# Same chunking as the shared corpora (adapters/vertex_rag.py).
CHUNK_SIZE = 512
CHUNK_OVERLAP = 50

_corpus_lock = threading.Lock()
_corpus_names: dict[str, str] = {}


class PrivateIndexError(Exception):
    """The library could not take the file. The file stays usable in chat."""


@dataclass(frozen=True)
class Hit:
    upload_id: str
    text: str
    score: float


def available() -> bool:
    """Whether files can go into the private library in this environment."""
    return bool(
        settings.APP_MODE == AppMode.GCP
        and settings.CHAT_FILE_PRIVATE_INDEX_ENABLED
        and settings.CHAT_FILE_PRIVATE_CORPUS
    )


def display_name_for(upload_id: str) -> str:
    return f"{DISPLAY_PREFIX}{upload_id}{DISPLAY_SUFFIX}"


def upload_id_from_source(source: str | None) -> str | None:
    name = (source or "").rsplit("/", 1)[-1]
    if name.startswith(DISPLAY_PREFIX) and name.endswith(DISPLAY_SUFFIX):
        return name[len(DISPLAY_PREFIX) : -len(DISPLAY_SUFFIX)] or None
    return None


def file_id_of(rag_file_name: str) -> str:
    """``projects/…/ragCorpora/C/ragFiles/F`` → ``F``."""
    return (rag_file_name or "").rstrip("/").rsplit("/", 1)[-1]


def cloud_source_uri(upload_id: str) -> str:
    """Workspace-private, deterministic source for a filed chat document."""
    if not settings.GCS_BUCKET_NAME:
        raise PrivateIndexError("the private library bucket is unavailable")
    name = object_name(
        "chat_files", "private", safe_upload_name(display_name_for(upload_id))
    )
    return f"gs://{settings.GCS_BUCKET_NAME}/{name}"


def _cloud_blob(uri: str):
    bucket, name = uri[5:].split("/", 1)
    return storage.Client(project=settings.GCP_PROJECT_ID).bucket(bucket).blob(name)


def _store_cloud_document(upload_id: str, document: str) -> str:
    uri = cloud_source_uri(upload_id)
    try:
        blob = _cloud_blob(uri)
        blob.upload_from_string(document.encode("utf-8"), content_type="text/markdown")
    except Exception:
        raise PrivateIndexError(
            "the private library source could not be stored"
        ) from None
    return uri


def _ragfile_upload_id(rag_file) -> str | None:
    return upload_id_from_source(getattr(rag_file, "display_name", None))


def _verified_ragfiles(corpus: str) -> dict[str, str]:
    """Map exact bound ragFile names to deterministic private source URIs."""
    from rubik_core.tenancy.corpora import assert_own_rag_file

    files: dict[str, str] = {}
    for item in _rag().list_files(corpus_name=corpus):
        name = assert_own_rag_file(item.name, corpus)
        upload_id = _ragfile_upload_id(item)
        if not upload_id or name in files:
            raise PrivateIndexError("a private library file cannot be mapped")
        files[name] = cloud_source_uri(upload_id)
    return files


def _source_for_filed_name(rag_file_name: str) -> str:
    from rubik_core.chat_files import INDEX_PRIVATE
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import ChatUpload

    with SessionLocal() as db:
        rows = (
            db.query(ChatUpload)
            .filter(ChatUpload.rag_file_name == rag_file_name)
            .limit(2)
            .all()
        )
        if (
            len(rows) != 1
            or not rows[0].owner_email
            or rows[0].index_status != INDEX_PRIVATE
        ):
            raise PrivateIndexError("a private library record cannot be mapped")
        return cloud_source_uri(rows[0].id)


def ensure_cloud_sources() -> set[str]:
    """Materialize and verify every filed private document before BYOK rebuild."""
    from rubik_core.chat_files import INDEX_PRIVATE
    from rubik_core.chat_files.service import library_document
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import ChatUpload

    corpus = resolve_corpus(create_if_missing=False)
    if not corpus:
        raise PrivateIndexError("the private library corpus is unavailable")
    files = _verified_ragfiles(corpus)
    with SessionLocal() as db:
        rows = (
            db.query(ChatUpload).filter(ChatUpload.index_status == INDEX_PRIVATE).all()
        )
        expected = {}
        for row in rows:
            if (
                not row.owner_email
                or not row.rag_file_name
                or row.rag_file_name not in files
                or files[row.rag_file_name] != cloud_source_uri(row.id)
            ):
                raise PrivateIndexError("a private library record cannot be mapped")
            expected[row.rag_file_name] = row
        if set(expected) != set(files):
            raise PrivateIndexError("a private library file has no owner record")
        uris = set()
        for name, row in expected.items():
            uri = files[name]
            document = library_document(row)
            try:
                blob = _cloud_blob(uri)
                blob.reload()
                existing = blob.download_as_bytes(
                    if_generation_match=int(blob.generation)
                )
            except NotFound:
                existing = None
            except Exception:
                raise PrivateIndexError(
                    "a private library source could not be verified"
                ) from None
            if existing != document.encode("utf-8"):
                _store_cloud_document(row.id, document)
            uris.add(uri)
        return uris


def _rag():
    import asyncio

    from vertexai.preview import rag

    from rubik_core.adapters.vertex_rag import _init_vertexai

    # Called from worker threads; Vertex's gRPC layer wants an event loop in
    # the calling thread (the chat router's _run_with_loop note). Left open on
    # purpose, as there: closing it breaks the SDK's cached channels.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    # VERTEX_RAG_LOCATION, where the corpora live: the default region 400s.
    _init_vertexai()
    return rag


def resolve_corpus(*, create_if_missing: bool = True) -> str | None:
    """The private corpus resource ID from this workspace's control row."""
    slug = current_workspace()
    if not available():
        return None
    with _corpus_lock:
        try:
            from rubik_core.tenancy.corpora import corpus_id

            _rag()  # initialize the SDK before a possible create
            resource = corpus_id("private_uploads", create=create_if_missing)
            if resource:
                _corpus_names[slug] = resource
            else:
                _corpus_names.pop(slug, None)
            return resource
        except Exception:  # noqa: BLE001 — a Vertex blip is "not available"
            _corpus_names.pop(slug, None)
            logger.exception("[chat-files] could not resolve the private corpus")
            return None


def forget_corpus() -> None:
    """Drop the cached name (after a NotFound, or in tests)."""
    _corpus_names.pop(current_workspace(), None)


def add_document(upload_id: str, document: str) -> str:
    """File one reading; return the ragFile resource name. Blocking."""
    corpus = resolve_corpus()
    if not corpus:
        raise PrivateIndexError("the private library is not available")
    rag = _rag()
    from rubik_core.ai_providers.cloud_rag_bridge import (
        begin_cloud_source_update,
        fail_cloud_source_update,
        finish_cloud_source_update,
    )
    from rubik_core.ai_providers.embedding_index import cloud_index_versions

    source_uri = None
    update = None
    if any(cloud_index_versions(corpus)):
        source_uri = _store_cloud_document(upload_id, document)
        update = begin_cloud_source_update(corpus, source_uri, "upsert")
    try:
        with tempfile.TemporaryDirectory(prefix="chat-file-") as td:
            path = Path(td) / display_name_for(upload_id)
            path.write_text(document, encoding="utf-8")
            rag_file = rag.upload_file(
                corpus_name=corpus,
                path=str(path),
                display_name=display_name_for(upload_id),
                description=f"Private Ask {PRODUCT_NAME} upload",
                transformation_config=rag.TransformationConfig(
                    chunking_config=rag.ChunkingConfig(
                        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
                    )
                ),
            )
    except ValueError as exc:
        # upload_file raises ValueError when the corpus is gone.
        fail_cloud_source_update(update)
        forget_corpus()
        raise PrivateIndexError("the private library upload failed") from exc
    except Exception as exc:  # noqa: BLE001
        fail_cloud_source_update(update)
        raise PrivateIndexError("the private library upload failed") from exc
    name = getattr(rag_file, "name", "") or ""
    if not name:
        fail_cloud_source_update(update)
        raise PrivateIndexError("Vertex did not return the new file's name")
    from rubik_core.tenancy.corpora import assert_own_rag_file

    try:
        assert_own_rag_file(name, corpus)
    except ValueError:
        fail_cloud_source_update(update)
        raise PrivateIndexError("the private library returned a foreign file") from None
    if source_uri is not None:
        try:
            finish_cloud_source_update(corpus, source_uri, "upsert", update)
        except Exception:
            raise PrivateIndexError(
                "the private library index is unavailable"
            ) from None
    return name


def search(rag_file_names: list[str], query: str, top_k: int) -> list[Hit]:
    """Semantic search over THESE files only. ``[]`` when there are none."""
    file_ids = [file_id_of(n) for n in rag_file_names if n]
    file_ids = [f for f in file_ids if f]
    if not file_ids or not (query or "").strip():
        # Never query with an empty fence: that would search everyone's files.
        return []
    corpus = resolve_corpus(create_if_missing=False)
    if not corpus:
        return []
    from rubik_core.tenancy.corpora import assert_own_rag_file

    for name in rag_file_names:
        if name:
            assert_own_rag_file(name, corpus)
    from rubik_core.ai_providers.cloud_rag_bridge import query_cloud_resource
    from rubik_core.ai_providers.embedding_index import active_cloud_index

    if active_cloud_index(corpus) is not None:
        mapping = _verified_ragfiles(corpus)
        allowed_uris = []
        for name in rag_file_names:
            if name:
                uri = mapping.get(name)
                if uri is None:
                    raise PrivateIndexError("a private library file is unavailable")
                allowed_uris.append(uri)
        chunks = query_cloud_resource(
            corpus,
            query,
            top_k=top_k,
            distance_threshold=settings.RAG_DISTANCE_THRESHOLD,
            allowed_source_uris=list(dict.fromkeys(allowed_uris)),
        )
        if chunks is None:
            raise PrivateIndexError("the private library index changed")
        allowed = set(allowed_uris)
        hits = []
        for item in chunks:
            source = item.chunk.metadata.get("source_path")
            if source not in allowed:
                raise PrivateIndexError("a private library result crossed its fence")
            upload_id = upload_id_from_source(source)
            if not upload_id:
                raise PrivateIndexError("a private library result cannot be mapped")
            hits.append(Hit(upload_id, item.chunk.text, item.score))
        return hits
    rag = _rag()
    try:
        response = rag.retrieval_query(
            rag_resources=[rag.RagResource(rag_corpus=corpus, rag_file_ids=file_ids)],
            text=query,
            similarity_top_k=top_k,
            vector_distance_threshold=settings.RAG_DISTANCE_THRESHOLD,
        )
    except Exception:  # noqa: BLE001
        logger.warning("[chat-files] private search failed", exc_info=True)
        return []
    hits: list[Hit] = []
    for ctx in response.contexts.contexts:
        source = getattr(ctx, "source_display_name", "") or getattr(
            ctx, "source_uri", ""
        )
        upload_id = upload_id_from_source(source)
        if not upload_id:
            continue
        hits.append(
            Hit(
                upload_id=upload_id,
                text=getattr(ctx, "text", "") or "",
                score=float(getattr(ctx, "score", 0.0) or 0.0),
            )
        )
    return hits


def remove(rag_file_name: str | None) -> bool:
    """Delete one ragFile. True when it is gone (or was never there).

    Not gated on :func:`available`: a file filed before the library was
    switched off must still be deletable by its owner.
    """
    if not rag_file_name:
        return True
    if settings.APP_MODE != AppMode.GCP:
        return False
    corpus = resolve_corpus(create_if_missing=False)
    if not corpus:
        return False
    from rubik_core.tenancy.corpora import assert_own_rag_file

    assert_own_rag_file(rag_file_name, corpus)
    from rubik_core.ai_providers.cloud_rag_bridge import (
        begin_cloud_source_update,
        fail_cloud_source_update,
        finish_cloud_source_update,
    )
    from rubik_core.ai_providers.embedding_index import cloud_index_versions

    source_uri = None
    update = None
    if any(cloud_index_versions(corpus)):
        source_uri = _source_for_filed_name(rag_file_name)
        update = begin_cloud_source_update(corpus, source_uri, "delete")

    try:
        _rag().delete_file(name=rag_file_name)
    except NotFound:
        pass
    except Exception:  # noqa: BLE001
        fail_cloud_source_update(update)
        logger.warning("[chat-files] could not delete a private ragFile", exc_info=True)
        return False
    if source_uri is not None:
        try:
            finish_cloud_source_update(corpus, source_uri, "delete", update)
            _cloud_blob(source_uri).delete()
        except NotFound:
            pass
        except Exception:
            logger.warning("[chat-files] private vector removal failed", exc_info=True)
            return False
    return True
