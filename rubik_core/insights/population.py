"""The people an Insights view counts, and what each of them can reach.

Contract: ``docs/insights/README.md`` §1 "People in scope".

* **Who counts.** ``users`` rows whose department slug is in the view, with an
  email on the Workspace domain (``rubik_core.company.domain``). Left
  out: the ``general`` department (the unassigned default, never a team), the
  impersonation placeholder, and the workspace's Insights exclusions (shared
  mailboxes, demo accounts). Emails are folded to lower case and de-duplicated,
  because several writers store whatever casing the sign-in handed them.
* **Departed people.** Offboarding deletes the ``users`` row but leaves their
  history (chats, cards, events) behind. They resolve through the
  ``offboard_records.department`` snapshot, so last quarter's numbers still
  count the work they did. They are kept apart from ``people``: they count in
  volumes (questions asked, priorities surfaced) but never in a rate's
  denominator, because "people who can use Cubie" no longer includes them.
* **Eligibility.** Whether a person can reach a feature comes from Admin →
  Feature Access, through the same ``user_allowed`` verdict the gates use, so
  "used X of the people who can" never drifts from who actually can. The
  policy for each key is read once per request and judged per person in
  memory (``resolve_all`` would re-read every key for every person).
  Eligibility is today's roster, not the roster on the day of the activity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rubik_core.company.operations import get_operations_config
from rubik_core.insights.scope import NOT_A_TEAM, Scope
from rubik_core.rag.config import slugify_department

#: The placeholder an admin's impersonated session signs requests with.
IMPERSONATION_PLACEHOLDER = "impersonated_user@systems.local"

#: Eligibility sentinel for a surface every signed-in person has (EOS).
EVERYONE = "*everyone*"
#: The feature that opens the AI copilot. A surface with no feature key of its
#: own (Ask Cubie, AI Priorities, Meeting Prep, Assets) is reachable by anyone
#: the copilot admits.
COPILOT = "copilot"


def fold(email: str | None) -> str:
    return (email or "").strip().lower()


def excluded_emails() -> frozenset[str]:
    """Workspace-owned exclusions plus the impersonation placeholder.

    A policy read failure must interrupt the view: treating it as an empty
    exclusion list could surface shared mailboxes in a company report.
    """
    configured = get_operations_config().insights_excluded_emails
    return frozenset(configured) | {IMPERSONATION_PLACEHOLDER}


def on_workspace_domain(email: str) -> bool:
    from rubik_core.company.domain import workspace_domains

    domains = workspace_domains()
    return bool(domains) and fold(email).rsplit("@", 1)[-1] in domains


@dataclass(frozen=True)
class Person:
    email: str
    name: str
    dept: str
    dept_label: str
    departed: bool = False
    is_admin: bool = False
    is_exec: bool = False
    department_raw: str | None = None
    managed: tuple[str, ...] = ()

    def subject(self) -> SimpleNamespace:
        """The shape ``features.policy.user_allowed`` judges."""
        return SimpleNamespace(
            email=self.email,
            department=self.department_raw,
            managed_departments=list(self.managed),
            is_exec=self.is_exec,
            is_admin=self.is_admin,
            roles=["admin"] if self.is_admin else ["user"],
        )


@dataclass
class Population:
    """The people one view covers, plus a per-request eligibility cache."""

    people: dict[str, Person]
    departed: dict[str, Person] = field(default_factory=dict)
    #: The build's own session, handed to every Feature Access read so a cold
    #: policy cache never checks a SECOND connection out of the pool while the
    #: build holds one (a burst of builds would otherwise wait on each other).
    db: Session | None = field(default=None, repr=False, compare=False)
    _eligible: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)

    def get(self, email: str) -> Person | None:
        key = fold(email)
        return self.people.get(key) or self.departed.get(key)

    def in_scope(self, email: str | None) -> bool:
        key = fold(email)
        return key in self.people or key in self.departed

    def dept_of(self, email: str | None) -> str | None:
        p = self.get(email or "")
        return p.dept if p else None

    def emails(self) -> frozenset[str]:
        return frozenset(self.people)

    def all_emails(self) -> frozenset[str]:
        return frozenset(self.people) | frozenset(self.departed)

    def by_dept(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for email, p in self.people.items():
            out.setdefault(p.dept, []).append(email)
        return out

    def eligible(self, key: str | None) -> frozenset[str]:
        """Current people who may reach feature ``key``.

        ``None`` means the copilot (a surface with no key of its own);
        ``EVERYONE`` means every current person. A feature whose env kill
        switch is off is reachable by nobody, whatever its roster says.
        """
        if key == EVERYONE:
            return self.emails()
        key = key or COPILOT
        hit = self._eligible.get(key)
        if hit is not None:
            return hit
        from rubik_core.features.policy import (
            effective_policy,
            kill_switch_on,
            user_allowed,
        )
        from rubik_core.features.registry import feature_by_key

        spec = feature_by_key(key)
        if kill_switch_on(spec) is False:
            allowed: frozenset[str] = frozenset()
        else:
            policy, _ = effective_policy(key, self.db)
            allowed = frozenset(
                email
                for email, p in self.people.items()
                if user_allowed(spec, policy, p.subject())
            )
        self._eligible[key] = allowed
        return allowed

    def restricted_to(self, email: str) -> Population:
        """A population of one person (the person profile), sharing nothing
        mutable with this one."""
        key = fold(email)
        people = {key: self.people[key]} if key in self.people else {}
        departed = {key: self.departed[key]} if key in self.departed else {}
        return Population(people=people, departed=departed, db=self.db)


def _role_map(db: Session) -> dict[str, set[str]]:
    from rubik_core.database.models import user_roles

    roles: dict[str, set[str]] = {}
    for email, role in db.execute(
        select(user_roles.c.user_email, user_roles.c.role_name)
    ).all():
        roles.setdefault(fold(email), set()).add(str(role))
    return roles


def load_population(
    db: Session, scope: Scope, *, selected_only: bool = True
) -> Population:
    """Everyone the view counts. ``selected_only=False`` covers every
    department the viewer may see (``/meta`` counts people per department)."""
    from rubik_core.database.models import OffboardRecord, User

    wanted = set(scope.selected if selected_only else scope.allowed)
    excluded = excluded_emails()
    roles = _role_map(db)

    people: dict[str, Person] = {}
    rows = (
        db.query(
            User.email,
            User.name,
            User.department,
            User.is_exec,
            User.managed_departments,
        )
        .filter(User.is_platform_support.is_(False))
        .all()
    )
    for email, name, department, is_exec, managed in rows:
        key = fold(email)
        if not key or key in excluded or not on_workspace_domain(key):
            continue
        slug = slugify_department(department or "")
        if slug in NOT_A_TEAM or slug not in wanted:
            continue
        if key in people:  # case-variant duplicate row: first one wins
            continue
        people[key] = Person(
            email=key,
            name=(name or "").strip() or key.split("@")[0],
            dept=slug,
            dept_label=scope.label(slug),
            is_admin="admin" in roles.get(key, set()),
            is_exec=bool(is_exec),
            department_raw=department,
            managed=tuple(m for m in (managed or []) if isinstance(m, str)),
        )

    departed: dict[str, Person] = {}
    offboarded = (
        db.query(
            OffboardRecord.user_email,
            OffboardRecord.user_name,
            OffboardRecord.department,
            OffboardRecord.offboarded_at,
        )
        .order_by(OffboardRecord.offboarded_at.desc())
        .all()
    )
    for email, name, department, _at in offboarded:
        key = fold(email)
        if not key or key in people or key in departed or key in excluded:
            continue
        if not on_workspace_domain(key):
            continue
        slug = slugify_department(department or "")
        if slug in NOT_A_TEAM or slug not in wanted:
            continue
        departed[key] = Person(
            email=key,
            name=(name or "").strip() or key.split("@")[0],
            dept=slug,
            dept_label=scope.label(slug),
            departed=True,
            department_raw=department,
        )
    return Population(people=people, departed=departed, db=db)


def resolve_features_for(person: Person, db: Session | None = None) -> dict[str, bool]:
    """Every feature verdict for one person (the person profile): the roster
    verdict, and False for a feature whose env kill switch is off."""
    from rubik_core.features.policy import kill_switch_on, resolve_all
    from rubik_core.features.registry import FEATURES

    verdicts = resolve_all(person.subject(), db)
    for spec in FEATURES:
        if kill_switch_on(spec) is False:
            verdicts[spec.key] = False
    return verdicts


def describe(person: Person) -> dict[str, Any]:
    return {
        "email": person.email,
        "name": person.name,
        "department": person.dept,
        "department_label": person.dept_label,
    }
