from __future__ import annotations

"""
Access policy for the audience-shaped RAGs.

This is the only place where "which RAGs is this user allowed to query?"
gets decided. Routers and the chat tool import `rags_for_user_email`
rather than re-implementing the policy.

Rule (current):
    - Admins (`is_admin == True`) → every slug in the live registry
      (including Exec-only RAGs like the VTO).
    - Org-wide Exec (`sees_all_departments`, i.e. Exec + managed_departments
      "All") → every slug too (the CEO case).
    - Exec team (`is_exec == True`) → their dept + companywide + each
      department in `managed_departments` + every EXEC_ONLY_SLUG that exists
      (the confidential VTO).
    - Everyone else → [<their_dept_slug>, COMPANYWIDE_SLUG], with both
      filtered down to slugs that actually exist in the registry today
      (a renamed/removed department won't surface stale slugs). Never an
      Exec-only slug.
    - Unknown user (no DB record) → [COMPANYWIDE_SLUG] only — fail closed.
"""

import logging

from rubik_core.rag.config import (
    COMPANYWIDE_SLUG,
    EXEC_ONLY_SLUGS,
    current_registry,
    is_exec_only_slug,
    slugify_department,
)

logger = logging.getLogger(__name__)


def rags_for_user_email(email: str) -> list[str]:
    """Return the RAG slugs `email` is allowed to query, ordered with the
    user's own department first (better for log readability)."""
    registry = current_registry()
    available = set(registry)

    user = None
    try:
        from rubik_core.auth.registry import UserRegistry

        user = UserRegistry().get_user(email)
    except Exception as e:
        logger.warning("rags_for_user_email: user lookup failed for %s (%s)", email, e)

    # Org-wide Execs (CEO etc., managed_departments = "All") see every corpus,
    # including the Exec-only VTO / eos_exec. ``sees_all_departments`` implies
    # Exec-team membership, so this is an exec grant, not an admin one.
    if user and getattr(user, "sees_all_departments", False):
        return sorted(available)

    # A Cubie ADMIN is an app super-user, NOT automatically cleared for
    # Exec-team-confidential content. A non-exec admin sees every DEPARTMENT
    # corpus (management/debug reach) but NEVER an Exec-only slug (VTO,
    # eos_exec). An admin who is *also* on the Exec team gets everything.
    # Invariant: nothing tagged Exec team reaches a non-exec user via chat RAG.
    if user and getattr(user, "is_admin", False):
        if getattr(user, "is_exec", False):
            return sorted(available)
        return sorted(s for s in available if not is_exec_only_slug(s))

    out: list[str] = []
    if user and user.department:
        slug = slugify_department(user.department)
        if slug and slug in available:
            out.append(slug)
    if COMPANYWIDE_SLUG in available and COMPANYWIDE_SLUG not in out:
        out.append(COMPANYWIDE_SLUG)
    # Exec team: their primary dept + companywide (above) PLUS each department
    # they own on the EOS Accountability Chart (``managed_departments``), so a
    # scoped exec can query the teams they lead. (Org-wide execs already
    # returned everything.) The "*" wildcard slugifies to "" and is ignored
    # here — it's handled by the sees_all_departments early-return above.
    if user and getattr(user, "is_exec", False):
        for dept in getattr(user, "managed_departments", None) or []:
            slug = slugify_department(dept)
            if slug and slug in available and slug not in out:
                out.append(slug)
    # Exec-only RAGs (the confidential VTO). Admins already returned
    # everything above; here we grant them to non-admin Exec-team members.
    if user and getattr(user, "is_exec", False):
        for slug in sorted(EXEC_ONLY_SLUGS):
            if slug in available and slug not in out:
                out.append(slug)
    return out
