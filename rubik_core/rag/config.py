from __future__ import annotations

"""
Schema for the RAG corpora — now audience-shaped.

Each RAG corpus corresponds to one of:
    - the literal "companywide" RAG (everyone sees it), or
    - one row in the `departments` table (HR, Finance, Sales, etc.).

The registry is built dynamically from the live department list at the time
`current_registry()` is called; there is no hardcoded list. This means:
    - Adding a new department creates a new RAG slot for it.
    - Removing a department drops it from the registry (the underlying Vertex
      corpus is NOT auto-deleted — that is a manual admin action).

Naming convention
-----------------
    department label          slug                  display_name           gcs_subprefix
    "HR"                      hr                    hr_rag                 rags/hr
    "Compliance/Audit"        compliance_audit      compliance_audit_rag   rags/compliance_audit
    (special: companywide)    companywide           companywide_rag        rags/companywide
"""

import logging
import re
from collections.abc import Iterable

from pydantic import BaseModel

logger = logging.getLogger(__name__)


# Reserved slug for the "everyone sees this" RAG. Not derived from any
# department; literal constant. Treated like a department for all CRUD
# purposes but not stored in the `departments` table.
COMPANYWIDE_SLUG = "companywide"

# Reserved slug for the confidential, Exec-only VTO corpus. Like
# `companywide` it's a literal constant rather than a department, but unlike
# companywide it is audience-restricted: only Exec-team members / admins may
# retrieve it (see `rags_for_user_email`), and it is hidden from non-exec
# RAG-management surfaces. The VTO is ingested here instead of the shared
# corpus so chat can never surface it to a non-exec employee.
VTO_SLUG = "vto"
# SOC 2 H-14: Exec-team-tagged EOS content (is_exec rocks/issues, is_exec_meeting
# L10 notes) is ingested into this dedicated corpus so ONLY Exec-team members
# can retrieve it. Vertex RAG has no per-chunk metadata filter, so isolation is
# corpus-level — exactly how the confidential VTO is handled.
EOS_EXEC_SLUG = "eos_exec"

# Slugs that live in the registry but are restricted to Exec/admin audiences.
# Every place that *enumerates* RAGs for a user (chat fan-out, the RAG admin
# list/CRUD) must filter these out for everyone else.
EXEC_ONLY_SLUGS = frozenset({VTO_SLUG, EOS_EXEC_SLUG})


def is_exec_only_slug(slug: str) -> bool:
    """True if `slug` is an Exec/admin-restricted RAG (e.g. the VTO)."""
    return slug in EXEC_ONLY_SLUGS


class RagSpec(BaseModel):
    """Describes one RAG corpus."""

    slug: str  # canonical identifier — URL path, asset.rag column
    display_name: str  # Vertex corpus display_name (matches `<slug>_rag`)
    description: str
    gcs_subprefix: str  # path under workspaces/<workspace-slug>/


_VALID_SLUG = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def slugify_department(label: str) -> str:
    """
    Convert a department label into a canonical RAG slug.

    Rules: lowercase; non-alphanumerics collapse to a single underscore;
    leading/trailing underscores stripped. Empty input → empty string.

    Examples:
        "HR"                 -> "hr"
        "Compliance/Audit"   -> "compliance_audit"
        " Sales "            -> "sales"
        "Eng & IT"           -> "eng_it"
        ""                   -> ""
    """
    if not label:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower())
    return slug.strip("_")


def canonical_department_labels(labels: Iterable[str]) -> dict[str, str]:
    """Map each department slug to the ONE spelling that should represent it.

    `departments.name` is the raw, case-sensitive PK, so a table can carry
    case-variant rows — `sales`/`Sales`, `engineering`/`Engineering`. A local
    test run writing to a shared database mints the lowercase ones (the root
    `conftest.py` now closes that hole). Rows are cheap to create and
    awkward to delete, so anything that *lists* or *groups* departments has to
    collapse them itself — and every such surface has to agree on which spelling
    wins, or a cleanup consolidates onto one label while a dropdown shows
    another.

    The winner is the spelling carrying capitals ("Sales" over "sales") — the
    human-canonical label, and on the live table the variant the majority of
    users sit on. Ties fall back to lexicographic order, so the result never
    depends on the database's collation or on row order.

    Blank labels are skipped. Callers decide policy on non-team rows: the EOS
    team pickers additionally drop `general` (the *unassigned* default of
    `users.department`) — see ``_canonical_departments`` in
    ``backend/routers/eos/me.py``.
    """
    best: dict[str, str] = {}
    for raw in labels:
        label = (raw or "").strip()
        slug = slugify_department(label)
        if not slug:
            continue
        incumbent = best.get(slug)
        if incumbent is None or _prefers(label, incumbent):
            best[slug] = label
    return best


def _prefers(candidate: str, incumbent: str) -> bool:
    """Should `candidate` replace `incumbent` as the canonical spelling?"""
    if incumbent.islower() != candidate.islower():
        return incumbent.islower()  # a spelling with capitals wins
    return candidate < incumbent  # deterministic tie-break


def make_rag_spec(slug: str, *, description: str | None = None) -> RagSpec:
    """Build the canonical `RagSpec` for a slug. Raises on malformed slugs."""
    if not slug or not _VALID_SLUG.match(slug):
        raise ValueError(
            f"Invalid RAG slug {slug!r}: must be lowercase a-z0-9 with single underscores."
        )
    if slug == COMPANYWIDE_SLUG:
        desc = description or "Documents visible to everyone in the company."
    elif slug == VTO_SLUG:
        desc = description or (
            "The company VTO (Vision/Traction Organizer) — confidential, Exec-only."
        )
    else:
        desc = description or f"Documents owned by the {slug} department."
    return RagSpec(
        slug=slug,
        display_name=f"{slug}_rag",
        description=desc,
        gcs_subprefix=f"rags/{slug}",
    )


def build_registry(department_labels: Iterable[str]) -> dict[str, RagSpec]:
    """
    Build the live registry: companywide + one entry per department label.

    Slug collisions (two departments slugifying to the same string) are
    resolved by first-wins; the duplicate is logged at WARNING. Empty or
    whitespace-only labels are silently ignored.
    """
    out: dict[str, RagSpec] = {
        COMPANYWIDE_SLUG: make_rag_spec(COMPANYWIDE_SLUG),
        VTO_SLUG: make_rag_spec(VTO_SLUG),
        EOS_EXEC_SLUG: make_rag_spec(EOS_EXEC_SLUG),
    }
    for label in department_labels:
        slug = slugify_department(label)
        if not slug:
            continue
        if slug in out:
            logger.warning(
                "RAG registry: slug %r already present; ignoring duplicate label %r",
                slug,
                label,
            )
            continue
        out[slug] = make_rag_spec(
            slug, description=f"Documents owned by the {label} department."
        )
    return out


def current_registry() -> dict[str, RagSpec]:
    """
    Live registry: reads the current `departments` table and returns the
    matching RagSpec dict. Cheap (one SQL query) — call per request rather
    than caching, so admins adding a new department see the change instantly.
    """
    try:
        from rubik_core.database.repository import SQLMetadataRegistry

        labels = SQLMetadataRegistry().get_departments()
    except Exception as e:
        logger.warning(
            "current_registry: failed to read departments table (%s); returning companywide-only",
            e,
        )
        labels = []
    return build_registry(labels)


def get_rag_spec(slug: str, registry: dict[str, RagSpec] | None = None) -> RagSpec:
    """Look up a slug in the supplied registry (defaults to current_registry())."""
    reg = registry if registry is not None else current_registry()
    if slug not in reg:
        valid = ", ".join(sorted(reg))
        raise ValueError(f"Unknown RAG slug {slug!r}. Known: {valid}")
    return reg[slug]
