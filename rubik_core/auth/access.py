# rubik_core/auth/access.py
import logging
from collections.abc import Sequence

from rubik_core.auth.registry import UserRegistry
from rubik_core.database.schemas import AccessPolicy, AssetMetadata, UserProfile

logger = logging.getLogger(__name__)


def _dept_key(value: str | None) -> str:
    """Canonical department identity for case-insensitive ACL matching.

    A department's identity in this system is its RAG slug (see
    ``rags_for_user_email`` / ``slugify_department``), so "Sales", "sales", and
    " Sales " are the same team. Matching on the slug here keeps this legacy
    exact-match ACL path in agreement with corpus-based retrieval — a user
    assigned a differently-cased department row still sees their own files.

    Lazy import keeps this leaf auth module free of the rag package's heavier
    imports at module-load time.
    """
    from rubik_core.rag.config import slugify_department

    return slugify_department(value or "")


def load_document_access_policies(user: UserProfile) -> list[AccessPolicy]:
    """Load the caller's policy grants once for a multi-asset read."""
    if user.is_admin or getattr(user, "sees_all_departments", False):
        return []
    return UserRegistry().meta_repo.get_user_access_policies(user.email)


def has_document_access(
    user: UserProfile,
    asset: AssetMetadata,
    *,
    policies: Sequence[AccessPolicy] | None = None,
) -> bool:
    """
    Determines if a user has read access to a specific RAG documentation/asset.
    Rules:
    1. Admins have access to everything.
    2. owners have access to their own documents.
    3. Company-wide documents are accessible to everyone.
    4. Explicitly allowed emails have access.
    5. Access Policies (User or Group) grant access for specific Dept/Topic combinations.
    6. Default fallback: Users in the same department have access (Legacy behavior).
    """
    # 1. Admin bypass
    if user.is_admin:
        return True

    # 1b. Org-wide Exec bypass (CEO etc., managed_departments = "All") —
    # cross-department READ across every asset, including Meeting Transcripts.
    if getattr(user, "sees_all_departments", False):
        return True

    # 1c. Scoped Exec — read access to the departments they own on the EOS
    # Accountability Chart (``managed_departments``). Case-insensitive match.
    if getattr(user, "is_exec", False) and asset.department:
        managed = getattr(user, "managed_departments", None) or []
        asset_dept = asset.department.strip().lower()
        if any(md and md.strip().lower() == asset_dept for md in managed):
            return True

    # 2. Owner bypass
    if asset.owner == user.email:
        return True

    # 3. Company-wide
    if getattr(asset, "is_company_wide", False):
        return True

    # 4. Explicit email list
    if user.email in getattr(asset, "allowed_emails", []):
        return True

    # 5. Access Policies
    if policies is None:
        policies = load_document_access_policies(user)

    for p in policies:
        # Match department (allow wildcard)
        dept_match = (
            p.department is None
            or p.department == "*"
            or _dept_key(p.department) == _dept_key(asset.department)
        )
        # Match topic (allow wildcard)
        topic_match = p.topic is None or p.topic == "*" or p.topic == asset.topic

        if dept_match and topic_match and p.can_read:
            return True

    # 6. Legacy fallback
    # Strictly isolate Meeting Transcripts from department matching
    if asset.topic != "Meeting Transcripts" and _dept_key(
        asset.department
    ) == _dept_key(user.department):
        return True

    return False
