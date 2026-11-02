"""Whether a brief or a chat gets its sales flavour.

Meeting Prep, Deep Prep and the chat's Sales Email Agent each have a sales
version: a battle brief with product matching, an agent that drafts outbound
mail. Which one a person gets follows their department or role, compared by
RAG slug (``slugify_department``) like every department check, so "Sales",
" sales" and "Sales." are one team.

This picks the FLAVOUR of something a person already has. It is not an access
gate: who may reach a feature is the registry's business (``rubik_core.features``).
"""

from __future__ import annotations

from collections.abc import Iterable

from rubik_core.rag.config import slugify_department

#: The department slug (or role name) that gets the sales flavour.
SALES_SLUG = "sales"


def is_sales_flavoured(department: str | None, roles: Iterable[str] = ()) -> bool:
    """True when ``department`` or any of ``roles`` slugifies to ``sales``."""
    if slugify_department(department or "") == SALES_SLUG:
        return True
    return any(slugify_department(role or "") == SALES_SLUG for role in roles or ())
