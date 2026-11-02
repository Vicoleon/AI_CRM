"""EOS alignment context for chat and AI Priorities.

Builds a compact text block of company direction (VTO, Rocks, Scorecard,
Issues) and per-user context (their Rocks + To-Dos due this week) that
gets prepended to the ADK supervisor's system prompt on every chat turn
AND fed to the UC-17 Gemini judge as system-level instructions, so the
overnight AI Priorities pipeline ranks signals tied to company direction
above unaligned noise.

Design rules baked in:

- **Never break chat.** Any error inside this module is logged and we
  return an empty string. The supervisor falls back to its baseline
  prompt automatically.
- **Token budget.** The text block is capped at ~2,800 characters
  (~700 tokens for English text). If we go over, the lowest-impact
  sections are trimmed first (user todos → user rocks → company issues
  → scorecard → company rocks → VTO).
- **5-minute per-user cache.** Spec calls for it; reduces DB load when
  the same user fires several turns in quick succession. Module-level
  dict, single-process — fine because chat is per-process anyway.
- **Dev disable.** `settings.CUBIE_DISABLE_ALIGNMENT=True` short-circuits
  to an empty block for A/B comparisons.
- **Single-tenant.** Pulls "the only Organization" from `eos_organizations`.
  The short-lived result cache is keyed by workspace and user.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypedDict

from sqlalchemy import func as sa_func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from rubik_core.config import settings
from rubik_core.database.eos import (
    Issue,
    IssuePriority,
    IssueStatus,
    Organization,
    Person,
    Quarter,
    Rock,
    RockStatus,
    ScorecardMetric,
    Todo,
    TodoStatus,
    VTOSection,
    VTOSectionType,
    VTOVersion,
)
from rubik_core.ingestion.eos.vto_xlsx import bullets_from_content
from rubik_core.tenancy.context import current_workspace

logger = logging.getLogger(__name__)

# ── Cache (5 min, per-user) ────────────────────────────────────────────

_CACHE_TTL_SECONDS = 300
_cache: dict[tuple[str, str], tuple[float, str]] = {}


def _key(user_email: str) -> tuple[str, str]:
    return current_workspace(), user_email.strip().lower()


def _cache_get(user_email: str) -> str | None:
    entry = _cache.get(_key(user_email))
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        return None
    return value


def _cache_set(user_email: str, value: str) -> None:
    _cache[_key(user_email)] = (time.monotonic(), value)


def invalidate_cache(user_email: str | None = None) -> None:
    """Drop a user (or everyone) from the cache. Call after mutations
    that should reflect immediately (e.g. a rock status change)."""
    slug = current_workspace()
    if user_email is None:
        for key in [key for key in _cache if key[0] == slug]:
            _cache.pop(key, None)
    else:
        _cache.pop(_key(user_email), None)


# ── Structured payload (consumed by `_render` below) ──────────────────


@dataclass
class RockOut:
    description: str
    status: str
    owner_name: str | None


@dataclass
class MetricOut:
    name: str
    target: str | None
    owner_name: str | None


@dataclass
class IssueOut:
    description: str
    priority: str


@dataclass
class TodoOut:
    description: str
    due_date: str | None  # ISO date or None


class VTOOut(TypedDict):
    core_focus: str | None
    ten_year_target: str | None
    one_year_plan: str | None


@dataclass
class AlignmentPayload:
    vto: VTOOut
    quarter_name: str | None
    company_rocks: list[RockOut]
    user_rocks: list[RockOut]
    off_track_metrics: list[MetricOut]
    top_issues: list[IssueOut]
    user_todos_this_week: list[TodoOut]
    last_refreshed: datetime


# ── Public entry points ────────────────────────────────────────────────


def _eos_enabled() -> bool:
    from rubik_core.control.repository import module_enabled

    try:
        return module_enabled("eos")
    except Exception:  # noqa: BLE001 — failed control reads cannot expose EOS
        logger.warning("alignment_context: EOS module lookup failed", exc_info=True)
        return False


def build_alignment_payload(user_email: str, db: Session) -> AlignmentPayload | None:
    """Compose the structured payload (no formatting). Returns `None` if
    there's no EOS data to align against (e.g. fresh dev DB with no
    Organization). Callers should treat None as "no banner / no system
    context to inject".
    """
    if not _eos_enabled():
        return None
    org = (
        db.query(Organization)
        .filter(Organization.is_deleted == False)  # noqa: E712
        .order_by(Organization.created_at.asc())
        .first()
    )
    if org is None:
        return None

    department, is_exec, org_wide = _user_team_scope(user_email, db)

    return AlignmentPayload(
        vto=_vto_for(org.id, db),
        quarter_name=_current_quarter_name(org.id, db),
        company_rocks=_company_rocks(org.id, db, limit=5, include_exec=is_exec),
        user_rocks=_user_rocks(user_email, db, limit=5, include_exec=is_exec),
        off_track_metrics=_off_track_metrics(db, limit=5, include_exec=is_exec),
        top_issues=_top_open_issues(
            db,
            limit=3,
            department=department,
            is_exec=is_exec,
            org_wide=org_wide,
        ),
        user_todos_this_week=_user_todos_this_week(
            user_email, db, limit=5, include_exec=is_exec
        ),
        last_refreshed=datetime.utcnow(),
    )


def _user_team_scope(user_email: str, db: Session) -> tuple[str | None, bool, bool]:
    """Resolve ``(department, is_exec, org_wide)`` for issue scoping from the
    ``users`` table. Used to keep the alignment block's open-issues list on the
    caller's own team instead of dumping every department's issues into every
    chat prompt (the "Migration Plan" cross-topic hallucination). An unknown
    user resolves to ``(None, False, False)`` → no issues surfaced. Org-wide
    execs (``managed_departments`` contains ``"*"``, e.g. the CEO) get
    ``org_wide=True`` and still see all teams' issues."""
    from rubik_core.database.models import User

    u = db.query(User).filter(User.email == user_email).first()
    if u is None:
        return None, False, False
    managed = u.managed_departments or []
    return (u.department or None), bool(u.is_exec), ("*" in managed)


def active_rock_descriptions_for(user_email: str, db: Session) -> list[str]:
    """Return the bag of active rock descriptions a calendar
    item should be matched against when scoring priorities.

    Combines company-wide rocks + the user's own rocks (which already
    include any rock the user owns, regardless of department). Returns
    de-duplicated, in original order (company-first). On any failure,
    returns ``[]`` so the priorities pipeline doesn't break.
    """
    try:
        payload = build_alignment_payload(user_email, db)
        if payload is None:
            return []
    except Exception:
        logger.exception("active_rock_descriptions_for failed for %s", user_email)
        return []

    seen: set[str] = set()
    out: list[str] = []
    for rock in (*payload.company_rocks, *payload.user_rocks):
        desc = (rock.description or "").strip()
        if not desc:
            continue
        key = desc.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(desc)
    return out


def build_alignment_context(user_email: str, db: Session) -> str:
    """Render the alignment block as a plain-text string ready to prepend
    to the supervisor's system prompt. Always safe to call — returns
    "" on any failure so the chat path never 500s because of EOS.
    """
    if settings.CUBIE_DISABLE_ALIGNMENT or not _eos_enabled():
        return ""

    cached = _cache_get(user_email)
    if cached is not None:
        return cached

    try:
        payload = build_alignment_payload(user_email, db)
        if payload is None:
            _cache_set(user_email, "")
            return ""
        rendered = _render(payload)
        _cache_set(user_email, rendered)
        return rendered
    except Exception:
        logger.exception("alignment_context build failed for %s", user_email)
        # Cache the failure for a short window so we don't hammer the DB
        # while it's misbehaving. 30s is short enough not to mask recovery.
        _cache[_key(user_email)] = (
            time.monotonic() - (_CACHE_TTL_SECONDS - 30),
            "",
        )
        return ""


# ── Composition helpers ────────────────────────────────────────────────


def _vto_for(org_id, db: Session) -> VTOOut:
    """Active VTO version's content for the three sections most useful
    in a system prompt: Core Focus, 10-Year Target, 1-Year Plan."""
    version = (
        db.query(VTOVersion)
        .filter(
            VTOVersion.organization_id == org_id,
            VTOVersion.is_deleted == False,  # noqa: E712
        )
        .order_by(VTOVersion.is_active.desc(), VTOVersion.created_at.desc())
        .first()
    )
    if version is None:
        return {"core_focus": None, "ten_year_target": None, "one_year_plan": None}

    wanted = {
        VTOSectionType.FOCUS.value: None,
        VTOSectionType.TEN_YEAR_TARGET.value: None,
        VTOSectionType.ONE_YEAR_PLAN.value: None,
    }
    sections = (
        db.query(VTOSection)
        .filter(
            VTOSection.vto_version_id == version.id,
            VTOSection.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    for s in sections:
        key = (
            s.section_type.value
            if hasattr(s.section_type, "value")
            else str(s.section_type)
        )
        if key in wanted:
            wanted[key] = _flatten_section_content(s.content)

    return {
        "core_focus": wanted[VTOSectionType.FOCUS.value],
        "ten_year_target": wanted[VTOSectionType.TEN_YEAR_TARGET.value],
        "one_year_plan": wanted[VTOSectionType.ONE_YEAR_PLAN.value],
    }


def _flatten_section_content(content) -> str | None:
    """VTO sections store bullets shaped ``{"text": str, "confidential":
    bool}`` (``vto_xlsx.bullets_from_content`` also tolerates the legacy
    plain-``list[str]`` shape). Renders only the OPEN bullets, semicolon
    separated: this block is injected into EVERY chat user's system prompt
    with no exec check (see module docstring / `build_alignment_context`),
    so a confidential bullet must never reach it."""
    bullets = [b for b in bullets_from_content(content) if not b.confidential]
    items = [b.text for b in bullets if b.text]
    return "; ".join(items) if items else None


def _current_quarter_name(org_id, db: Session) -> str | None:
    q = (
        db.query(Quarter)
        .filter(
            Quarter.organization_id == org_id,
            Quarter.is_current == True,  # noqa: E712
            Quarter.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    return q.name if q else None


def _person_name(person: Person | None) -> str | None:
    if person is None:
        return None
    name = f"{person.first_name or ''} {person.last_name or ''}".strip()
    return name or person.email


def _company_rocks(
    org_id, db: Session, limit: int, *, include_exec: bool = False
) -> list[RockOut]:
    """Rocks on the current quarter for this org. Off-track first, then
    on-track, then done. Limit applied after sort.

    This block is prepended to EVERY chat user's system prompt, so Exec-team
    ("ET") Rocks are excluded unless the caller is on the Exec team
    (``include_exec``). Admin status does NOT grant it — the flag comes from
    ``UserProfile.is_exec`` only."""
    q = (
        db.query(Quarter)
        .filter(
            Quarter.organization_id == org_id,
            Quarter.is_current == True,  # noqa: E712
            Quarter.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if q is None:
        return []
    rock_q = db.query(Rock).filter(
        Rock.quarter_id == q.id,
        Rock.is_deleted == False,  # noqa: E712
    )
    if not include_exec:
        rock_q = rock_q.filter(Rock.is_exec == False)  # noqa: E712
    rocks = rock_q.all()
    order = {RockStatus.OFF_TRACK: 0, RockStatus.ON_TRACK: 1, RockStatus.DONE: 2}
    rocks.sort(key=lambda r: order.get(r.status, 3))
    rocks = rocks[:limit]

    people = {p.id: p for p in db.query(Person).all()}
    return [
        RockOut(
            description=r.description,
            status=r.status.value if hasattr(r.status, "value") else str(r.status),
            owner_name=_person_name(people.get(r.owner_id)),
        )
        for r in rocks
    ]


def _user_rocks(
    user_email: str, db: Session, limit: int, *, include_exec: bool = False
) -> list[RockOut]:
    """Rocks owned by the calling user on the current quarter. Off-track first.

    Even a Rock the caller *owns* is withheld when it's Exec-tagged and the
    caller is not on the Exec team — an ET Rock mis-assigned to a non-exec owner
    must not reach their chat context (the exact leak this fence closes)."""
    person = db.query(Person).filter(Person.email == user_email).first()
    if person is None:
        return []
    rock_q = db.query(Rock).filter(
        Rock.owner_id == person.id,
        Rock.is_deleted == False,  # noqa: E712
    )
    if not include_exec:
        rock_q = rock_q.filter(Rock.is_exec == False)  # noqa: E712
    rocks = rock_q.all()
    order = {RockStatus.OFF_TRACK: 0, RockStatus.ON_TRACK: 1, RockStatus.DONE: 2}
    rocks.sort(key=lambda r: order.get(r.status, 3))
    rocks = rocks[:limit]
    return [
        RockOut(
            description=r.description,
            status=r.status.value if hasattr(r.status, "value") else str(r.status),
            owner_name=_person_name(person),
        )
        for r in rocks
    ]


def _off_track_metrics(
    db: Session, limit: int, *, include_exec: bool = False
) -> list[MetricOut]:
    """Scorecard metrics flagged on_track=False. We also surface metrics
    whose latest entry is missing or below target — but the source of
    truth for "off-track" today is the `on_track` boolean column maintained
    in the UI.

    Exec-team ("ET") measurables — the confidential Leadership Scorecard — are
    excluded unless the caller is on the Exec team (``include_exec``). Admin
    status does not grant it."""
    metric_q = db.query(ScorecardMetric).filter(
        ScorecardMetric.is_deleted == False,  # noqa: E712
        ScorecardMetric.on_track == False,  # noqa: E712
    )
    if not include_exec:
        metric_q = metric_q.filter(ScorecardMetric.is_exec == False)  # noqa: E712
    metrics = metric_q.limit(limit).all()
    people = {p.id: p for p in db.query(Person).all()}
    return [
        MetricOut(
            name=m.name,
            target=m.target_value,
            owner_name=_person_name(people.get(m.owner_id)),
        )
        for m in metrics
    ]


def _top_open_issues(
    db: Session,
    limit: int,
    *,
    department: str | None = None,
    is_exec: bool = False,
    org_wide: bool = False,
) -> list[IssueOut]:
    """Open issues, highest priority first — scoped to the caller's TEAM.

    Previously this returned org-wide open issues, which meant every user's
    chat prompt carried every department's issues verbatim on every turn. That
    was a root cause of the "Migration Plan" cross-topic hallucination: a
    Systems issue ("migrate privately owned apps to systems") sat in a
    non-Systems user's prompt and got fused into an unrelated action. We now
    surface only the user's own department team (+ the Exec team if they're on
    it). Org-wide execs (``org_wide=True``) keep the all-teams view.
    """
    q = db.query(Issue).filter(
        Issue.status == IssueStatus.OPEN,
        Issue.is_deleted == False,  # noqa: E712
    )
    if not org_wide:
        clauses = []
        if department:
            clauses.append(sa_func.lower(Issue.department_name) == department.lower())
        if is_exec:
            clauses.append(Issue.is_exec == True)  # noqa: E712
        if not clauses:
            # No resolvable team → surface nothing rather than everything.
            return []
        q = q.filter(or_(*clauses))
    open_issues = q.all()
    priority_order = {
        IssuePriority.HIGH: 0,
        IssuePriority.MEDIUM: 1,
        IssuePriority.LOW: 2,
    }
    open_issues.sort(key=lambda i: priority_order.get(i.priority, 3))
    open_issues = open_issues[:limit]
    return [
        IssueOut(
            description=i.description,
            priority=i.priority.value
            if hasattr(i.priority, "value")
            else str(i.priority),
        )
        for i in open_issues
    ]


def _user_todos_this_week(
    user_email: str, db: Session, limit: int, *, include_exec: bool = False
) -> list[TodoOut]:
    """Pending todos for the user with a due_date in the next 7 days
    (or already overdue + still pending).

    Exec-tagged To-Dos are withheld from a non-exec caller (``include_exec``),
    matching the Rock/Scorecard fence above."""
    person = db.query(Person).filter(Person.email == user_email).first()
    if person is None:
        return []
    horizon = datetime.utcnow() + timedelta(days=7)
    todo_q = db.query(Todo).filter(
        Todo.owner_id == person.id,
        Todo.status == TodoStatus.PENDING,
        Todo.is_deleted == False,  # noqa: E712
    )
    if not include_exec:
        todo_q = todo_q.filter(Todo.is_exec == False)  # noqa: E712
    todos = todo_q.all()
    relevant = [t for t in todos if t.due_date is None or t.due_date <= horizon]
    relevant.sort(key=lambda t: t.due_date or datetime.max)
    relevant = relevant[:limit]
    return [
        TodoOut(
            description=t.description,
            due_date=t.due_date.date().isoformat() if t.due_date else None,
        )
        for t in relevant
    ]


# ── Rendering ──────────────────────────────────────────────────────────


_MAX_CHARS = 2800  # ~700 tokens for English text
_ISSUE_DESC_MAX = 80  # per-issue description cap in the alignment block


def _truncate(text: str, limit: int = _ISSUE_DESC_MAX) -> str:
    """Trim a description to ``limit`` chars with an ellipsis. Keeps the passive
    alignment block terse — a long issue title shouldn't read like a task the
    model was asked to work on."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _render_base(p: AlignmentPayload) -> str:
    """Pure formatter — no length checks, no recursion. Section order is
    fixed; the LLM should learn to scan it from top down."""
    sections: list[str] = []

    sections.append("## COMPANY DIRECTION (live from EOS)")

    vto = p.vto
    if vto["core_focus"]:
        sections.append(f"- Core Focus: {vto['core_focus']}")
    if vto["ten_year_target"]:
        sections.append(f"- 10-Year Target: {vto['ten_year_target']}")
    if vto["one_year_plan"]:
        sections.append(f"- 1-Year Plan: {vto['one_year_plan']}")

    if p.company_rocks:
        sections.append(
            f"- {p.quarter_name or 'Current quarter'} Company Rocks (status):"
        )
        for r in p.company_rocks:
            owner = f" · owner: {r.owner_name}" if r.owner_name else ""
            sections.append(f'   · "{r.description}" — {r.status}{owner}')

    if p.off_track_metrics:
        names = ", ".join(
            f"{m.name}" + (f" (target {m.target})" if m.target else "")
            for m in p.off_track_metrics
        )
        sections.append(f"- Off-track scorecard metrics: {names}")

    if p.top_issues:
        sections.append("- Top open issues (your team; background only):")
        for i in p.top_issues:
            sections.append(f"   · [{i.priority}] {_truncate(i.description)}")

    user_block: list[str] = []
    if p.user_rocks:
        user_block.append("- My Rocks:")
        for r in p.user_rocks:
            user_block.append(f'   · "{r.description}" — {r.status}')
    if p.user_todos_this_week:
        user_block.append("- My To-Dos due this week:")
        for t in p.user_todos_this_week:
            due = f" (due {t.due_date})" if t.due_date else ""
            user_block.append(f"   · {t.description}{due}")
    if user_block:
        sections.append("## USER CONTEXT")
        sections.extend(user_block)

    return "\n".join(sections)


def _render(p: AlignmentPayload) -> str:
    """Render with a token budget. If the base render is too long, progressively
    drop low-priority sections (user todos → user rocks → top issues →
    scorecard → trim company rocks to top 3) until we fit. Last resort:
    truncate at the byte budget. Surrounding newlines simplify
    concatenation with the SYSTEM_INSTRUCTION."""
    rendered = _render_base(p)
    if len(rendered) <= _MAX_CHARS:
        return f"\n{rendered}\n"

    # Trim ladder: clear fields one at a time, in least→most important order.
    trim_order: list[str] = [
        "user_todos_this_week",
        "user_rocks",
        "top_issues",
        "off_track_metrics",
    ]
    candidate = AlignmentPayload(
        vto=p.vto,
        quarter_name=p.quarter_name,
        company_rocks=p.company_rocks,
        user_rocks=p.user_rocks,
        off_track_metrics=p.off_track_metrics,
        top_issues=p.top_issues,
        user_todos_this_week=p.user_todos_this_week,
        last_refreshed=p.last_refreshed,
    )
    for field in trim_order:
        setattr(candidate, field, [])
        rendered = _render_base(candidate)
        if len(rendered) <= _MAX_CHARS:
            return f"\n{rendered}\n"

    # Last resort: trim company rocks too, then byte-cut.
    candidate.company_rocks = candidate.company_rocks[:3]
    rendered = _render_base(candidate)
    if len(rendered) > _MAX_CHARS:
        rendered = rendered[:_MAX_CHARS]
    return f"\n{rendered}\n"
