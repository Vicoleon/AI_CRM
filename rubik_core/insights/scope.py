"""Which departments an Insights viewer may see.

Contract: ``docs/insights/README.md`` §1. This is a STRUCTURAL rule, not a
roster: the roster of who may open Insights at all lives in Admin → Feature
Access (``insights`` / ``insights_people``); this module decides how much of
the company a person who is already in can see.

* Admins and org-wide execs (``UserProfile.sees_all_departments``: the Exec
  team with the ``"*"`` wildcard in ``managed_departments``) see every
  department.
* Anyone else sees the departments they manage plus their own primary
  department.

Departments are compared by RAG slug (``slugify_department``), never by raw
label (docs/ENGINEERING_NOTES.md, "A department's identity is its RAG slug"): the live
``departments`` table carries case-variant duplicates (``Sales``/``sales``),
and a raw compare would split one team in two or lock a manager out of it.
``general`` is the unassigned default of ``users.department``, never a team,
so it is never in anyone's scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.rag.config import canonical_department_labels, slugify_department

#: Slugs that are never a team: the unassigned default, and the org-wide
#: wildcard (which slugifies to "" anyway).
NOT_A_TEAM = frozenset({"", "general"})


class ScopeError(PermissionError):
    """A department filter outside the viewer's scope (HTTP 403)."""


@dataclass(frozen=True)
class Scope:
    """What one request covers.

    ``allowed`` is every department the viewer may see; ``selected`` is what
    this request covers (the ``dept`` filter, or all of ``allowed``).
    """

    viewer_email: str
    all_departments: bool
    allowed: tuple[str, ...]
    selected: tuple[str, ...]
    filtered: bool
    labels: dict[str, str] = field(default_factory=dict)

    def label(self, slug: str) -> str:
        return self.labels.get(slug) or slug.replace("_", " ").title()

    @property
    def company_wide(self) -> bool:
        """True when the view is the whole company: an all-departments viewer
        with no department filter. Only then can spend that is not tied to a
        person (most of it, today) be counted."""
        return self.all_departments and not self.filtered

    def to_dict(self, people: int) -> dict:
        return {
            "departments": list(self.selected),
            "labels": [self.label(s) for s in self.selected],
            "all": self.company_wide,
            "people": people,
        }

    def cache_key(self) -> tuple:
        return (self.all_departments, self.allowed, self.selected, self.filtered)


def sees_all(user: Any) -> bool:
    """Admins and org-wide execs see every department."""
    if bool(getattr(user, "is_admin", False)):
        return True
    return bool(getattr(user, "sees_all_departments", False))


def known_department_labels(db: Session) -> dict[str, str]:
    """slug → the one spelling to show, over the ``departments`` table and
    every ``users.department`` value, with ``general`` dropped.

    Both sources, because a person can sit in a department that has no row in
    ``departments`` (rows are free text, created by hand).
    """
    from rubik_core.database.models import Department, User

    raw: list[str] = [name for (name,) in db.query(Department.name).all() if name]
    raw += [
        dept
        for (dept,) in db.query(User.department)
        .filter(User.is_platform_support.is_(False))
        .distinct()
        .all()
        if dept
    ]
    labels = canonical_department_labels(raw)
    return {slug: label for slug, label in labels.items() if slug not in NOT_A_TEAM}


def own_departments(user: Any) -> set[str]:
    """The slugs a non-all-departments viewer may see: managed + primary."""
    candidates = [getattr(user, "department", None)]
    candidates += list(getattr(user, "managed_departments", None) or [])
    slugs = {slugify_department(c) for c in candidates if isinstance(c, str)}
    return slugs - NOT_A_TEAM


def parse_dept_param(value: str | None) -> tuple[str, ...]:
    """``dept=sales,Marketing`` → ``("marketing", "sales")``; empty → ()."""
    if not value:
        return ()
    slugs = {slugify_department(part) for part in value.split(",")}
    return tuple(sorted(slugs - {""}))


def resolve_scope(db: Session, user: Any, dept_param: str | None = None) -> Scope:
    """The viewer's scope with the ``dept`` filter applied. Raises ScopeError."""
    labels = known_department_labels(db)
    every = sees_all(user)
    if every:
        allowed = set(labels)
    else:
        allowed = own_departments(user)
        for slug in allowed:
            labels.setdefault(slug, slug.replace("_", " ").title())
    requested = parse_dept_param(dept_param)
    if requested and not every:
        outside = [s for s in requested if s not in allowed]
        if outside:
            raise ScopeError(
                "You can only see the departments you manage. "
                f"Not in your view: {', '.join(outside)}."
            )
    selected = requested or tuple(sorted(allowed))
    if every:
        # An all-departments viewer may name a department with no row and no
        # people (it simply shows nothing); give it a readable label.
        for slug in selected:
            labels.setdefault(slug, slug.replace("_", " ").title())
    return Scope(
        viewer_email=(getattr(user, "email", "") or "").strip().lower(),
        all_departments=every,
        allowed=tuple(sorted(allowed)),
        selected=tuple(selected),
        filtered=bool(requested),
        labels=labels,
    )
