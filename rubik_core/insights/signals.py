"""What counts as "using Cubie on a day", and which part of Cubie was used.

Contract: ``docs/insights/README.md`` §5. Cubie had no usage log before
Insights (``activity_daily`` starts with it), so "active" is the union of every
table that already records a person DOING something, each mapped to the part
of Cubie (the "surface") it belongs to:

====================  ===================================  ================
source                what counts                          surface
====================  ===================================  ================
login_events          a Google sign-in                     (none)
interaction_logs      an Ask Cubie question (not a         chat
                      scheduled run: Cubie ran those)
chat_sessions         a conversation started (not one a    chat
                      scheduled run created)
llm_usage_events      an AI call made on a person's        see below
                      request, for interactive features
audit_logs            an audited action by the person      by action prefix
priority_memory       a card state the person changed      priorities
meeting_prep_runs     a Meeting Prep brief (not the        meeting_prep
                      emailed deep prep, which is queued)
proposals             a proposal started                   proposals
crm_sync_suggestions  a CRM Sync approval                  crm_sync
promise_events        a My To-Dos change by the person     todos
activity_daily        any request in an area (since        the area
                      Insights launched)
priority_events       a card action by the person          priorities
====================  ===================================  ================

Anything a background job does in a person's name — the nightly priorities
run, the Loose Ends morning scan, a scheduled task, Slack delivery — is NOT a
use of Cubie by that person, so those writers are filtered out here. Only
narrow columns are read (an email, a timestamp, a flag); no message, prompt or
answer column is ever selected.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import extract, false, func, or_, select
from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.insights.filters import Window, local_day
from rubik_core.insights.population import EVERYONE, fold


@dataclass(frozen=True)
class Surface:
    """A part of Cubie a person can use, for the adoption matrix.

    ``feature`` is the Feature Access key that decides who CAN use it; None
    means "anyone with the copilot", ``EVERYONE`` means every signed-in person.
    ``person_level`` False keeps the surface out of anything that names a
    person (Call Capture: Google Meet's terms forbid tracking individuals).
    """

    key: str
    label: str
    feature: str | None
    person_level: bool = True


SURFACES: tuple[Surface, ...] = (
    Surface("chat", f"Ask {PRODUCT_NAME}", None),
    Surface("priorities", "AI Priorities", None),
    Surface("briefing", "Daily Briefing", None),
    Surface("meeting_prep", "Meeting Prep", None),
    Surface("assets", "Assets", None),
    Surface("eos", "EOS", EVERYONE),
    Surface("todos", "My To-Dos", "todos"),
    Surface("proposals", "Proposals", "proposal_generator"),
    Surface("crm_sync", "CRM Sync", "crm_sync"),
    Surface("xray", "Customer X-Ray", "customer_xray"),
    Surface("loose_ends", "Loose Ends", "loop_closure"),
    Surface("cockpit", "Sales Cockpit", "sales_cockpit"),
    Surface("call_capture", "Sales Call Capture", "sales_call_capture", False),
    Surface("voice", "Voice", "voice_input"),
    Surface("scheduled_tasks", "Scheduled tasks", "scheduled_tasks"),
)
SURFACE_BY_KEY: dict[str, Surface] = {s.key: s for s in SURFACES}
PERSON_SURFACES = frozenset(s.key for s in SURFACES if s.person_level)

#: AI calls that only happen because a person asked for something right then.
#: Everything else (priorities runs, scans, ingestion, scheduled tasks) runs
#: on its own and says nothing about whether the person used Cubie.
LLM_FEATURE_AREAS: dict[str, str] = {
    "chat": "chat",
    "voice_input": "voice",
    "voice_commands": "voice",
    "meeting_prep": "meeting_prep",
    "meeting_prep_section": "meeting_prep",
    "customer_xray": "xray",
    "proposal_generator": "proposals",
    "todos": "todos",
    "insights_summary": "insights",
}

#: Audit action prefix → surface. First match wins; anything else is "other".
AUDIT_PREFIX_AREAS: tuple[tuple[str, str], ...] = (
    ("PROPOSAL", "proposals"),
    ("CRM_SYNC", "crm_sync"),
    ("STAGE_LAG", "cockpit"),
    ("COMMITMENT", "cockpit"),
    ("COCKPIT", "cockpit"),
    ("LOOSE_ENDS", "loose_ends"),
    ("CALL_", "call_capture"),
    ("INSIGHTS", "insights"),
    ("DEPARTED_PRIORITIES", "priorities"),
    ("EOS", "eos"),
    ("ADD_DRIVE_FILE_TO_ASSETS", "assets"),
    ("ASSET", "assets"),
    ("DAM", "assets"),
    ("USER_", "admin"),
    ("ROLE_", "admin"),
    ("FEATURE_ACCESS", "admin"),
    ("IMPERSONATE", "admin"),
    ("WORKSPACE_SETTINGS", "admin"),
    ("GROUP_", "admin"),
)

#: priority_events kinds a PERSON causes (the rest are Cubie's own doing).
PERSON_PRIORITY_EVENT_KINDS = frozenset(
    {
        "resolved",
        "dismissed",
        "snoozed",
        "reopened",
        "slack_followup",
    }
)


def audit_area(action: str | None) -> str:
    action = (action or "").upper()
    for prefix, area in AUDIT_PREFIX_AREAS:
        if action.startswith(prefix):
            return area
    return "other"


def shared_audit_action(action: str | None, details: object) -> bool:
    """Personal mailbox actions cannot establish shared workspace activity."""
    if (action or "").upper() in {
        "GMAIL_ACCOUNT_CONNECTED",
        "GMAIL_ACCOUNT_DISCONNECTED",
    }:
        return False
    return not (isinstance(details, dict) and details.get("account_id") is not None)


@dataclass(frozen=True)
class Signal:
    """One recorded use of Cubie. ``at`` is the naive-UTC instant when the
    source has one (``activity_daily`` only knows the day)."""

    email: str
    day: date
    area: str | None
    source: str
    at: datetime | None = None


def _scheduled_session_ids():
    from rubik_core.database.models import ScheduledTaskRun

    return select(ScheduledTaskRun.chat_session_id).where(
        ScheduledTaskRun.chat_session_id.isnot(None)
    )


def _not_scheduled(mode_col):
    return or_(mode_col.is_(None), mode_col != "scheduled")


#: Bound on one ``IN (...)`` list; larger rosters are OR-ed in chunks.
_IN_CHUNK = 500

#: Areas that are never attributed to a person (README §1): Google Meet's terms
#: forbid tracking individuals' call use, so Call Capture counts per
#: department only.
DEPARTMENT_ONLY_AREAS = frozenset({"call_capture"})


def email_filter(col, emails: Iterable[str], *, folded: bool = False):
    """``col`` is one of ``emails`` (already lower-case), in SQL.

    ``folded=True`` for columns whose writers lower-fold (``activity_daily``,
    ``priority_events``, ``promise_events.actor``, ``proposals.owner_email``)
    so the plain column's index can serve the lookup; every other column is
    compared through ``lower()``, because sign-in and chat store whatever
    casing the identity provider handed over. An empty roster matches nothing.
    """
    values = sorted(set(emails))
    if not values:
        return false()
    target = col if folded else func.lower(col)
    chunks = [values[i : i + _IN_CHUNK] for i in range(0, len(values), _IN_CHUNK)]
    clauses = [target.in_(chunk) for chunk in chunks]
    return clauses[0] if len(clauses) == 1 else or_(*clauses)


def not_smart_teams(action_col):
    """Smart Teams actions are never Insights data (README §1): an upload or a
    profile view is the person's own advisory record, not a use to count."""
    return ~action_col.like("SMART\\_TEAMS%", escape="\\")


def drain_activity_buffer() -> None:
    """Write out the requests the activity recorder still holds in memory
    (``rubik_core/insights/activity.py`` batches them), so a dashboard counts
    this instance's last few minutes too.

    Called ONCE by ``service`` before it opens the build session — never from
    inside a load: the flush opens its own session, and a second checkout
    while the build holds one is how a burst of dashboard loads exhausts the
    pool. Cheap when the buffer is empty; never raises.
    """
    try:
        from rubik_core.insights.activity import flush_activity

        flush_activity()
    except Exception:  # noqa: BLE001 — a read must not fail on telemetry
        pass


def hourly_counts(db: Session, email_col, ts_col, *conds, extra=()):
    """``(email, *extra, utc_hour, count)`` rows: one per person per UTC hour
    (per ``extra`` value), counted in SQL instead of one row per event.

    Hours, not days, because in a whole-hour time zone a local day boundary is
    a whole UTC hour: the caller converts ``utc_hour`` to its local day exactly.
    (A half-hour zone would put the boundary mid-hour and misplace up to 30
    minutes of activity.) ``date()`` and ``EXTRACT(hour …)`` render on both
    Postgres and SQLite.
    """
    day_col = func.date(ts_col)
    hour_col = extract("hour", ts_col)
    q = (
        db.query(email_col, *extra, day_col, hour_col, func.count())
        .filter(*conds)
        .group_by(email_col, *extra, day_col, hour_col)
    )
    out = []
    for row in q:
        *head, day, hour, n = row
        d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
        out.append((*head, datetime(d.year, d.month, d.day, int(hour or 0)), int(n)))
    return out


def load_signals(db: Session, emails: Iterable[str], window: Window) -> list[Signal]:
    """Every recorded use of Cubie by ``emails`` in ``window``.

    Each query is bounded by the window AND the roster in SQL, so a
    department view never pulls the rest of the company's rows.
    """
    from rubik_core.database.insights import ActivityDaily, PriorityEvent
    from rubik_core.database.models import (
        AuditLog,
        ChatSession,
        CrmSyncSuggestion,
        InteractionLog,
        LLMUsageEvent,
        LoginEvent,
        MeetingPrepRun,
        PriorityMemory,
    )
    from rubik_core.database.promises import PromiseEvent
    from rubik_core.database.proposals import Proposal
    from rubik_core.priorities.privacy import public_priority_ids_for_users

    wanted = frozenset(emails)
    if not wanted:
        return []
    lo, hi = window.utc_bounds()
    out: list[Signal] = []

    def add(email, at, area, source, keep_at=True):
        key = fold(email)
        if key not in wanted or at is None:
            return
        day = local_day(at)
        if window.contains(day):
            exact = at if keep_at and isinstance(at, datetime) else None
            out.append(Signal(key, day, area, source, exact))

    def by(col, folded=False):
        return email_filter(col, wanted, folded=folded)

    for email, at in db.query(LoginEvent.user_email, LoginEvent.timestamp).filter(
        LoginEvent.timestamp >= lo, LoginEvent.timestamp < hi, by(LoginEvent.user_email)
    ):
        add(email, at, None, "login")

    for email, at in db.query(
        InteractionLog.user_email, InteractionLog.timestamp
    ).filter(
        InteractionLog.timestamp >= lo,
        InteractionLog.timestamp < hi,
        _not_scheduled(InteractionLog.mode),
        by(InteractionLog.user_email),
    ):
        add(email, at, "chat", "chat")

    for email, at in db.query(ChatSession.user_email, ChatSession.created_at).filter(
        ChatSession.created_at >= lo,
        ChatSession.created_at < hi,
        ChatSession.id.notin_(_scheduled_session_ids()),
        by(ChatSession.user_email),
    ):
        add(email, at, "chat", "chat_session")

    # AI calls: one row per person, feature and hour, not one per call.
    for email, feature, hour, _n in hourly_counts(
        db,
        LLMUsageEvent.user_email,
        LLMUsageEvent.timestamp,
        LLMUsageEvent.timestamp >= lo,
        LLMUsageEvent.timestamp < hi,
        LLMUsageEvent.feature.in_(tuple(LLM_FEATURE_AREAS)),
        by(LLMUsageEvent.user_email),
        extra=(LLMUsageEvent.feature,),
    ):
        add(email, hour, LLM_FEATURE_AREAS.get(feature), "ai_usage", keep_at=False)

    for email, at, action, details in db.query(
        AuditLog.actor_email,
        AuditLog.timestamp,
        AuditLog.action,
        AuditLog.details,
    ).filter(
        AuditLog.timestamp >= lo,
        AuditLog.timestamp < hi,
        not_smart_teams(AuditLog.action),
        by(AuditLog.actor_email),
    ):
        if not shared_audit_action(action, details):
            # Personal mailbox activity belongs to its owner, not the shared
            # adoption/usage dashboard. The audit row remains available for
            # security review without being counted as team activity.
            continue
        add(email, at, audit_area(action), "audit")

    # A personal-Gmail priority run is owner-only. Card action telemetry needs
    # a proven public card ID before it can enter shared usage reporting.
    public_priority_ids = public_priority_ids_for_users(db, wanted)
    for email, priority_id, changed_by, at in db.query(
        PriorityMemory.user_email,
        PriorityMemory.last_priority_id,
        PriorityMemory.last_state_change_by,
        PriorityMemory.last_state_change_at,
    ).filter(
        PriorityMemory.last_state_change_at >= lo,
        PriorityMemory.last_state_change_at < hi,
        by(PriorityMemory.user_email),
    ):
        if fold(changed_by) == fold(email) and priority_id in public_priority_ids.get(
            fold(email), set()
        ):
            add(email, at, "priorities", "priority_memory")

    for email, at in db.query(
        MeetingPrepRun.user_email, MeetingPrepRun.created_at
    ).filter(
        MeetingPrepRun.created_at >= lo,
        MeetingPrepRun.created_at < hi,
        or_(MeetingPrepRun.mode.is_(None), MeetingPrepRun.mode != "deep"),
        by(MeetingPrepRun.user_email),
    ):
        add(email, at, "meeting_prep", "meeting_prep")

    for email, at in db.query(Proposal.owner_email, Proposal.created_at).filter(
        Proposal.created_at >= lo,
        Proposal.created_at < hi,
        by(Proposal.owner_email, folded=True),
    ):
        add(email, at, "proposals", "proposals")

    for email, at in db.query(
        CrmSyncSuggestion.applied_by, CrmSyncSuggestion.applied_at
    ).filter(
        CrmSyncSuggestion.applied_at.isnot(None),
        CrmSyncSuggestion.applied_at >= lo,
        CrmSyncSuggestion.applied_at < hi,
        by(CrmSyncSuggestion.applied_by),
    ):
        add(email, at, "crm_sync", "crm_sync")

    for actor, at in db.query(PromiseEvent.actor, PromiseEvent.at).filter(
        PromiseEvent.at >= lo, PromiseEvent.at < hi, by(PromiseEvent.actor, folded=True)
    ):
        add(actor, at, "todos", "promises")

    for email, day, area in db.query(
        ActivityDaily.user_email, ActivityDaily.day, ActivityDaily.area
    ).filter(
        ActivityDaily.day >= window.start,
        ActivityDaily.day <= window.end,
        by(ActivityDaily.user_email, folded=True),
    ):
        add(email, day, area or "other", "activity")

    for owner, priority_id, actor, at in db.query(
        PriorityEvent.user_email,
        PriorityEvent.priority_id,
        PriorityEvent.actor,
        PriorityEvent.at,
    ).filter(
        PriorityEvent.at >= lo,
        PriorityEvent.at < hi,
        PriorityEvent.kind.in_(tuple(PERSON_PRIORITY_EVENT_KINDS)),
        by(PriorityEvent.actor, folded=True),
    ):
        if priority_id in public_priority_ids.get(fold(owner), set()):
            add(actor, at, "priorities", "priority_events")

    return out


def load_first_last(db: Session, emails: Iterable[str]) -> dict[str, tuple[date, date]]:
    """``email → (first day, last day)`` of recorded use, over ALL history.

    One grouped MIN/MAX per source, restricted to the roster in SQL, folded by
    email in Python because several sources store mixed case. Used for "newly
    active" and "dormant", which need to know about use from before the
    period. Call Capture actions are left out, as everywhere per person.
    """
    from rubik_core.database.insights import ActivityDaily, PriorityEvent
    from rubik_core.database.models import (
        AuditLog,
        ChatSession,
        CrmSyncSuggestion,
        InteractionLog,
        LLMUsageEvent,
        LoginEvent,
        MeetingPrepRun,
        PriorityMemory,
    )
    from rubik_core.database.promises import PromiseEvent
    from rubik_core.database.proposals import Proposal
    from rubik_core.priorities.privacy import public_priority_ids_for_users

    wanted = frozenset(emails)
    found: dict[str, tuple[date, date]] = {}
    if not wanted:
        return found
    public_priority_ids = public_priority_ids_for_users(db, wanted)

    def fold_in(rows):
        for email, first, last in rows:
            key = fold(email)
            if key not in wanted:
                continue
            d1, d2 = local_day(first), local_day(last)
            if d1 is None or d2 is None:
                continue
            cur = found.get(key)
            found[key] = (min(cur[0], d1), max(cur[1], d2)) if cur else (d1, d2)

    def grouped(email_col, ts_col, *conds, folded=False):
        q = db.query(email_col, func.min(ts_col), func.max(ts_col)).filter(
            email_filter(email_col, wanted, folded=folded), *conds
        )
        return q.group_by(email_col).all()

    fold_in(grouped(LoginEvent.user_email, LoginEvent.timestamp))
    fold_in(
        grouped(
            InteractionLog.user_email,
            InteractionLog.timestamp,
            _not_scheduled(InteractionLog.mode),
        )
    )
    fold_in(
        grouped(
            ChatSession.user_email,
            ChatSession.created_at,
            ChatSession.id.notin_(_scheduled_session_ids()),
        )
    )
    fold_in(
        grouped(
            LLMUsageEvent.user_email,
            LLMUsageEvent.timestamp,
            LLMUsageEvent.feature.in_(tuple(LLM_FEATURE_AREAS)),
        )
    )
    fold_in(
        grouped(
            AuditLog.actor_email,
            AuditLog.timestamp,
            not_smart_teams(AuditLog.action),
            ~AuditLog.action.like("CALL\\_%", escape="\\"),
            ~func.upper(AuditLog.action).in_(
                ("GMAIL_ACCOUNT_CONNECTED", "GMAIL_ACCOUNT_DISCONNECTED")
            ),
            AuditLog.details["account_id"].as_string().is_(None),
        )
    )
    fold_in(
        (
            (email, at, at)
            for email, priority_id, at in db.query(
                PriorityMemory.user_email,
                PriorityMemory.last_priority_id,
                PriorityMemory.last_state_change_at,
            ).filter(
                email_filter(PriorityMemory.user_email, wanted),
                func.lower(PriorityMemory.last_state_change_by)
                == func.lower(PriorityMemory.user_email),
            )
            if priority_id in public_priority_ids.get(fold(email), set())
        )
    )
    fold_in(
        grouped(
            MeetingPrepRun.user_email,
            MeetingPrepRun.created_at,
            or_(MeetingPrepRun.mode.is_(None), MeetingPrepRun.mode != "deep"),
        )
    )
    fold_in(grouped(Proposal.owner_email, Proposal.created_at, folded=True))
    fold_in(
        grouped(
            CrmSyncSuggestion.applied_by,
            CrmSyncSuggestion.applied_at,
            CrmSyncSuggestion.applied_at.isnot(None),
        )
    )
    fold_in(grouped(PromiseEvent.actor, PromiseEvent.at, folded=True))
    fold_in(
        grouped(
            ActivityDaily.user_email,
            ActivityDaily.day,
            ActivityDaily.area.notin_(tuple(DEPARTMENT_ONLY_AREAS)),
            folded=True,
        )
    )
    fold_in(
        (
            (actor, first, last)
            for owner, actor, priority_id, first, last in db.query(
                PriorityEvent.user_email,
                PriorityEvent.actor,
                PriorityEvent.priority_id,
                func.min(PriorityEvent.at),
                func.max(PriorityEvent.at),
            )
            .filter(
                email_filter(PriorityEvent.actor, wanted, folded=True),
                PriorityEvent.kind.in_(tuple(PERSON_PRIORITY_EVENT_KINDS)),
            )
            .group_by(
                PriorityEvent.user_email,
                PriorityEvent.actor,
                PriorityEvent.priority_id,
            )
            if priority_id in public_priority_ids.get(fold(owner), set())
        )
    )
    return found


def earliest(db: Session, model_col) -> date | None:
    """The local day of the oldest value in a column, or None if empty."""
    value = db.query(func.min(model_col)).scalar()
    return local_day(value)
