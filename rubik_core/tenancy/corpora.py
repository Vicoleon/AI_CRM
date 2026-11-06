"""Workspace-bound Vertex RAG corpus identities.

The control database is the authority for resource IDs. A display name is
only used when creating a corpus; it is never used to discover an existing
corpus across the shared Vertex project.
"""

from __future__ import annotations

import re
import threading

from sqlalchemy import select

from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceCorpus
from rubik_core.tenancy.context import _canonical_slug, current_workspace

_NAME = re.compile(r"[a-z][a-z0-9_]{0,99}\Z")
_lock = threading.RLock()


def _key(kind: str, audience: str | None) -> tuple[str, str | None]:
    if not isinstance(kind, str) or (
        audience is not None and not isinstance(audience, str)
    ):
        raise ValueError("Invalid corpus kind or audience")
    if kind.startswith("audience:") and audience is None:
        kind, audience = "rag", kind.partition(":")[2]
    if not _NAME.fullmatch(kind) or (
        audience is not None and not _NAME.fullmatch(audience)
    ):
        raise ValueError("Invalid corpus kind or audience")
    if kind == "rag" and audience is None:
        raise ValueError("A RAG audience is required")
    if kind != "rag" and audience is not None:
        raise ValueError("Only audience RAG corpora accept an audience")
    return kind, audience


def _display_name(slug: str, kind: str, audience: str | None) -> str:
    name = f"{audience}_rag" if audience else kind
    return f"{slug}__{name}"


def provision_corpus_resource(
    slug: str, kind: str, *, audience: str | None = None
) -> str:
    """Create physical corpus during privileged provisioning, before registration.

    The returned resource ID must be journaled and bound into the Workspace
    control row at registration. Runtime access still requires that binding.
    """
    existing = existing_corpus_resource(slug, kind, audience=audience)
    if existing:
        return existing
    return _create_corpus_resource(slug, kind, audience=audience)


def existing_corpus_resource(
    slug: str, kind: str, *, audience: str | None = None
) -> str | None:
    """Recover a provisioning corpus by its exact deterministic display name.

    This project-wide enumeration is for privileged provisioning only. Runtime
    requests always resolve a resource ID from the control binding instead.
    """
    slug = _canonical_slug(slug)
    kind, audience = _key(kind, audience)
    import vertexai
    from vertexai.preview import rag

    from rubik_core.config import settings

    if not settings.GCP_PROJECT_ID:
        raise RuntimeError("GCP_PROJECT_ID is required to provision RAG corpora")
    vertexai.init(
        project=settings.GCP_PROJECT_ID, location=settings.VERTEX_RAG_LOCATION
    )

    wanted = _display_name(slug, kind, audience)
    matches = [c.name for c in rag.list_corpora() if c.display_name == wanted]
    if len(matches) > 1:
        raise RuntimeError(f"Duplicate physical corpora for {wanted}")
    return matches[0] if matches else None


def _create_corpus_resource(
    slug: str, kind: str, *, audience: str | None = None
) -> str:
    import vertexai
    from vertexai.preview import rag

    from rubik_core.config import settings

    if not settings.GCP_PROJECT_ID:
        raise RuntimeError("GCP_PROJECT_ID is required to provision RAG corpora")
    vertexai.init(
        project=settings.GCP_PROJECT_ID, location=settings.VERTEX_RAG_LOCATION
    )

    created = rag.create_corpus(display_name=_display_name(slug, kind, audience))
    resource = getattr(created, "name", None)
    if not isinstance(resource, str) or not resource:
        raise RuntimeError("Vertex did not return a corpus resource ID")
    return resource


def corpus_id(
    kind: str, *, create: bool = False, audience: str | None = None
) -> str | None:
    """Return this workspace's exact bound resource ID, optionally creating it.

    Fixed kinds live in ``workspaces.corpus_ids``. Dynamic audiences live in
    ``workspace_corpora`` so adding a department never changes another
    workspace's mapping. A missing mapping is absent, never a prompt to scan
    all project corpora.
    """
    kind, audience = _key(kind, audience)
    slug = current_workspace()
    with _lock, control_session() as session:
        if create and session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == slug).with_for_update()
        )
        if workspace is None or workspace.status != "active":
            raise PermissionError("Active workspace required for corpus access")
        row = (
            session.get(WorkspaceCorpus, (slug, kind, audience))
            if audience is not None
            else None
        )
        found = (
            row.corpus_id
            if row is not None
            else (workspace.corpus_ids or {}).get(kind)
            if audience is None
            else None
        )
        if found or not create:
            return found

        resource = _create_corpus_resource(slug, kind, audience=audience)
        if audience is None:
            workspace.corpus_ids = {**(workspace.corpus_ids or {}), kind: resource}
        else:
            session.add(
                WorkspaceCorpus(
                    workspace_slug=slug,
                    kind=kind,
                    audience=audience,
                    corpus_id=resource,
                )
            )
        session.commit()
        return resource


def require_bound_corpus_id(resource: str) -> str:
    """Refuse a caller-supplied corpus ID absent from this workspace's map."""
    if not isinstance(resource, str) or not resource:
        raise ValueError("A corpus resource ID is required")
    slug = current_workspace()
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            raise PermissionError("Active workspace required for corpus access")
        if resource in (workspace.corpus_ids or {}).values():
            return resource
        match = session.scalar(
            select(WorkspaceCorpus).where(
                WorkspaceCorpus.workspace_slug == slug,
                WorkspaceCorpus.corpus_id == resource,
            )
        )
        if match is not None:
            return resource
    raise ValueError("Corpus is not bound to this workspace")


def assert_own_rag_file(name: str, corpus: str) -> str:
    """Validate a Vertex ragFile name before a direct delete operation."""
    require_bound_corpus_id(corpus)
    prefix = corpus.rstrip("/") + "/ragFiles/"
    if not isinstance(name, str) or not name.startswith(prefix):
        raise ValueError("RAG file is outside this workspace's corpus")
    file_id = name[len(prefix) :]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
        raise ValueError("Invalid RAG file resource ID")
    return name
