"""
RAG-corpus subsystem (audience-shaped).

Each Vertex AI RAG corpus corresponds either to the literal `companywide`
audience or to one row in the `departments` table. The registry is built
dynamically from the live department list at the time it's read.

Public API:
    from rubik_core.rag import (
        COMPANYWIDE_SLUG,
        RagManager,
        RagSpec,
        RagDocument,
        RagQueryHit,
        RagOperationError,
        NoUC1CorporaError,
        build_registry,
        current_registry,
        slugify_department,
        rags_for_user_email,
    )
"""

from rubik_core.rag.access import rags_for_user_email
from rubik_core.rag.config import (
    COMPANYWIDE_SLUG,
    EXEC_ONLY_SLUGS,
    VTO_SLUG,
    RagSpec,
    build_registry,
    current_registry,
    get_rag_spec,
    is_exec_only_slug,
    make_rag_spec,
    slugify_department,
)
from rubik_core.rag.manager import (
    NoUC1CorporaError,
    RagDocument,
    RagManager,
    RagOperationError,
    RagQueryHit,
)

__all__ = [
    "COMPANYWIDE_SLUG",
    "EXEC_ONLY_SLUGS",
    "VTO_SLUG",
    "is_exec_only_slug",
    "RagManager",
    "RagSpec",
    "RagDocument",
    "RagQueryHit",
    "RagOperationError",
    "NoUC1CorporaError",
    "build_registry",
    "current_registry",
    "get_rag_spec",
    "make_rag_spec",
    "slugify_department",
    "rags_for_user_email",
]
