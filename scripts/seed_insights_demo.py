"""Seed a LOCAL SQLite database with synthetic data for the Insights dashboards.

For visual QA of ``/insights`` without production data: ~25 made-up people in
four departments (Sales, Marketing, Engineering, Operations) on the configured
Workspace domain, and 60 days of plausible rows in every table Phase 1 reads
(sign-ins, Ask Cubie questions and conversations, ratings, daily briefings
with priorities, priority states and events, page activity, AI usage,
priorities runs, scheduled task runs, Meeting Prep runs and proposals), plus
the Phase-2 tables (CRM Sync, Call Capture writes and scripts, Sales Cockpit
commitments, nudges and stage corrections, Loose Ends,
proposal versions, Meeting Prep versions and deep preps, Customer X-Ray,
My To-Dos promises) and the Phase-3 tables (EOS teams with
rocks, to-dos, issues, L10s and a scorecard; AI spend tied to people;
documents, transcripts and versions; knowledge entries proposed from email;
answers with citations; more ratings). Everyone and everything here is
invented.

REFUSES to run unless the database is SQLite: it inserts made-up people and
activity, and a run against Cloud SQL would put them in front of real users
(the test-pollution incident the root conftest.py describes).

Usage (from the repo root, with an active local workspace selected):

    CUBIE_WORKSPACE=<active-local-workspace> \\
      DATABASE_URL=sqlite:////tmp/insights_demo.db \\
      python scripts/seed_insights_demo.py --reset

``--reset`` drops and recreates every table in that SQLite file first, so a
re-run does not double the data. The tests import :func:`seed` directly
(``backend/tests/test_insights_router.py``).
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import uuid
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Written into every free-text column (questions, answers, titles, card text)
#: so a test can prove none of it ever reaches an Insights response.
DEFAULT_SENTINEL = "DEMO-CONTENT-NEVER-SHOWN"

DEPARTMENTS = ("Sales", "Marketing", "Engineering", "Operations")

# (first name, last name, department, persona). Persona drives how often the
# person shows up: power ≈ most weekdays, regular ≈ half, light ≈ now and then,
# dormant = stopped three weeks ago, never = no activity at all.
PEOPLE: tuple[tuple[str, str, str, str], ...] = (
    ("Avery", "Stone", "Sales", "power"),
    ("Blake", "Rivera", "Sales", "power"),
    ("Casey", "Nguyen", "Sales", "regular"),
    ("Drew", "Brooks", "Sales", "regular"),
    ("Elliot", "Price", "Sales", "light"),
    ("Finley", "Ward", "Sales", "dormant"),
    ("Gray", "Holt", "Sales", "never"),
    ("Harper", "Lane", "Sales", "regular"),
    ("Indy", "Moss", "Marketing", "power"),
    ("Jules", "Park", "Marketing", "regular"),
    ("Kai", "Reed", "Marketing", "light"),
    ("Logan", "Shaw", "Marketing", "light"),
    ("Morgan", "Tate", "Marketing", "dormant"),
    ("Noel", "Vance", "Engineering", "power"),
    ("Oakley", "West", "Engineering", "power"),
    ("Parker", "Young", "Engineering", "regular"),
    ("Quinn", "Zane", "Engineering", "regular"),
    ("Riley", "Adams", "Engineering", "light"),
    ("Sage", "Baker", "Engineering", "regular"),
    ("Taylor", "Cole", "Operations", "regular"),
    ("Umi", "Dean", "Operations", "light"),
    ("Val", "Ellis", "Operations", "light"),
    ("Wren", "Ford", "Operations", "never"),
    ("Xen", "Gale", "Operations", "regular"),
    ("Yael", "Hart", "Operations", "power"),
)

_P_ACTIVE = {"power": 0.85, "regular": 0.55, "light": 0.2, "dormant": 0.5, "never": 0.0}
_INTENTS = ("search_documents", "hubspot", "email", "calendar", "eos", "general")
_BACKENDS = ("rag", "hubspot", "gmail", "calendar", "eos", "llm")
_CHANNELS = (
    "gmail",
    "calendar",
    "hubspot",
    "slack",
    "eos",
    "meet",
    "promises",
)
_KINDS = (
    "follow_up_debt",
    "commitment_debt",
    "prep_debt",
    "upcoming_prep",
    "approaching_deadline",
    "neglect_debt",
    "close_date_at_risk",
)
_AREAS = ("chat", "priorities", "briefing", "eos", "meeting_prep", "assets", "insights")
_SALES_AREAS = ("proposals", "crm_sync", "loose_ends", "cockpit", "xray")


def _email(first: str, last: str, domain: str) -> str:
    return f"{first}.{last}@{domain}".lower()


def _utc(d: date, hour: float) -> datetime:
    from rubik_core.insights.filters import local_midnight_utc

    return local_midnight_utc(d) + timedelta(hours=hour)


def seed(
    db,
    *,
    domain: str,
    today: date | None = None,
    days: int = 60,
    sentinel: str = DEFAULT_SENTINEL,
    rng_seed: int = 7,
) -> dict:
    """Insert the demo rows through ``db`` and commit. Returns a summary."""
    from rubik_core.database.insights import ActivityDaily, PriorityEvent
    from rubik_core.database.models import (
        AssistantFeedback,
        ChatSession,
        Department,
        HomeCard,
        InteractionLog,
        LLMUsageEvent,
        LoginEvent,
        MeetingPrepRun,
        OffboardRecord,
        PrioritiesRun,
        PriorityMemory,
        Role,
        ScheduledTask,
        ScheduledTaskRun,
        User,
    )
    from rubik_core.database.proposals import Proposal
    from rubik_core.insights.filters import today_local
    from rubik_core.tenancy.workspace_time import workspace_time_zone_name

    rng = random.Random(rng_seed)
    today = today or today_local()
    start = today - timedelta(days=days - 1)
    # Card-by-card history and page activity start three weeks ago, as they
    # would on a real deploy of Insights.
    tracking_start = today - timedelta(days=20)
    dormant_after = today - timedelta(days=21)

    def uid() -> str:
        return uuid.uuid4().hex

    # ── roles, departments, people ──────────────────────────────────────────
    roles = {}
    for name in ("admin", "user"):
        role = db.get(Role, name)
        if role is None:
            role = Role(name=name, description=name)
            db.add(role)
        roles[name] = role
    for dept in DEPARTMENTS:
        if db.get(Department, dept) is None:
            db.add(Department(name=dept))
    db.flush()

    people: list[tuple[str, str, str]] = []  # (email, department, persona)
    for i, (first, last, dept, persona) in enumerate(PEOPLE):
        email = _email(first, last, domain)
        if db.get(User, email) is not None:
            people.append((email, dept, persona))
            continue
        user = User(
            email=email,
            id=f"demo-{i}",
            name=f"{first} {last}",
            department=dept,
            is_exec=first in ("Yael", "Avery"),
            managed_departments=(
                ["*"] if first == "Yael" else ["Sales"] if first == "Avery" else []
            ),
        )
        user.roles = (
            [roles["admin"], roles["user"]] if first == "Noel" else [roles["user"]]
        )
        db.add(user)
        people.append((email, dept, persona))
    departed_email = _email("Rowan", "Ex", domain)
    db.add(
        OffboardRecord(
            id=uid(),
            user_email=departed_email,
            user_name="Rowan Ex",
            department="Sales",
            successor_email=_email("Avery", "Stone", domain),
            created_by=_email("Noel", "Vance", domain),
            offboarded_at=_utc(today - timedelta(days=15), 10),
            sweep_status="ok",
        )
    )
    people.append((departed_email, "Sales", "departed"))
    db.flush()

    counts: dict[str, int] = {}

    def bump(name: str, n: int = 1) -> None:
        counts[name] = counts.get(name, 0) + n

    # ── daily activity ──────────────────────────────────────────────────────
    d = start
    while d <= today:
        weekday = d.weekday() < 5
        for email, dept, persona in people:
            if persona == "departed" and d >= today - timedelta(days=15):
                continue
            if persona == "dormant" and d >= dormant_after:
                continue
            p = _P_ACTIVE.get(persona, 0.6 if persona == "departed" else 0.0)
            if not weekday:
                p *= 0.15
            if rng.random() >= p:
                continue
            # Sign-in.
            db.add(
                LoginEvent(user_email=email, timestamp=_utc(d, rng.uniform(7.5, 10)))
            )
            bump("login_events")
            # Ask Cubie.
            n_q = (
                rng.choice((0, 1, 2, 3, 4, 6))
                if persona != "light"
                else rng.choice((0, 1))
            )
            sessions: list[str] = []
            for _ in range(n_q):
                if not sessions or rng.random() < 0.35:
                    sid = str(uuid.uuid4())
                    sessions.append(sid)
                    db.add(
                        ChatSession(
                            id=sid,
                            user_email=email,
                            title=f"{sentinel} conversation",
                            created_at=_utc(d, rng.uniform(8, 17)),
                        )
                    )
                    bump("chat_sessions")
                at = _utc(d, rng.uniform(8, 18))
                meta = (
                    f"Intent: {rng.choice(_INTENTS)} | Backend: {rng.choice(_BACKENDS)} | ReqID: r{rng.randint(1, 9999)}"
                    if rng.random() < 0.7
                    else None
                )
                ok = rng.random() < 0.93
                db.add(
                    InteractionLog(
                        id=str(uuid.uuid4()),
                        session_id=sessions[-1],
                        user_email=email,
                        timestamp=at,
                        user_input=f"{sentinel} question",
                        system_prompt=meta,
                        llm_output=f"{sentinel} answer",
                        duration_ms=rng.randint(1800, 28000) if ok else 0,
                        successful=ok,
                        mode=rng.choice(
                            ("ask", "ask", "ask", "draft", "investigate", None)
                        ),
                    )
                )
                bump("interaction_logs")
                db.add(
                    LLMUsageEvent(
                        id=str(uuid.uuid4()),
                        timestamp=at,
                        feature="chat",
                        model="gemini-flash",
                        total_tokens=rng.randint(2000, 12000),
                        cost_usd=round(rng.uniform(0.002, 0.03), 5),
                        user_email=email,
                    )
                )
                bump("llm_usage_events")
                if dept == "Engineering" and rng.random() < 0.3:
                    db.add(
                        LLMUsageEvent(
                            id=str(uuid.uuid4()),
                            timestamp=at - timedelta(seconds=20),
                            feature="voice_input",
                            model="gemini-flash",
                            total_tokens=900,
                            cost_usd=0.0008,
                            user_email=email,
                        )
                    )
                    bump("llm_usage_events")
                if rng.random() < 0.12:
                    db.add(
                        AssistantFeedback(
                            id=uid(),
                            surface="chat",
                            proposal_id=uid(),
                            proposal_text=f"{sentinel} rated answer",
                            rating=rng.choice(
                                ("useful", "useful", "useful", "meh", "not_useful")
                            ),
                            user_email=email,
                            created_at=at,
                            updated_at=at,
                        )
                    )
                    bump("assistant_feedback")
            # Meeting Prep and proposals.
            if rng.random() < 0.15:
                db.add(
                    MeetingPrepRun(
                        id=uid(),
                        created_at=_utc(d, rng.uniform(8, 16)),
                        user_email=email,
                        title=f"{sentinel} meeting",
                        mode="sync",
                    )
                )
                bump("meeting_prep_runs")
            if dept == "Sales" and rng.random() < 0.06:
                db.add(
                    Proposal(
                        id=uid(),
                        owner_email=email,
                        title=f"{sentinel} proposal",
                        status=rng.choice(("brief", "drafted")),
                        created_at=_utc(d, rng.uniform(9, 16)),
                    )
                )
                bump("proposals")
            # Page activity, only once Insights was "deployed".
            if d >= tracking_start and persona != "departed":
                areas = list(_AREAS) + (list(_SALES_AREAS) if dept == "Sales" else [])
                for area in rng.sample(areas, k=min(len(areas), rng.randint(1, 4))):
                    db.add(
                        ActivityDaily(
                            user_email=email,
                            day=d,
                            area=area,
                            department_slug=dept.lower(),
                            requests=rng.randint(1, 40),
                            first_at=_utc(d, 8),
                            last_at=_utc(d, 17),
                        )
                    )
                    bump("activity_daily")
        # Nightly AI Priorities spend, tied to nobody (as in production today).
        db.add(
            LLMUsageEvent(
                id=str(uuid.uuid4()),
                timestamp=_utc(d, 1),
                feature="ai_priorities",
                model="gemini-pro",
                total_tokens=400000,
                cost_usd=round(rng.uniform(3.0, 6.0), 2),
                user_email=None,
            )
        )
        bump("llm_usage_events")
        d += timedelta(days=1)

    # ── AI Priorities: briefings, memory, events, runs ──────────────────────
    copilot_people = [
        (e, dept, persona)
        for e, dept, persona in people
        if dept in ("Sales", "Marketing", "Engineering") or persona == "departed"
    ]
    for email, _dept, persona in copilot_people:
        carry: list[dict] = []
        d = start
        while d <= today:
            if d.weekday() >= 5 or (
                persona == "departed" and d >= today - timedelta(days=15)
            ):
                d += timedelta(days=1)
                continue
            fresh = []
            for _ in range(rng.randint(2, 5)):
                kind = rng.choice(_KINDS)
                fresh.append(
                    {
                        "id": f"demo-{uid()[:12]}",
                        "title": f"{sentinel} card title",
                        "why_it_matters": f"{sentinel} reason",
                        "urgency": rng.choice(("high", "medium", "medium", "low")),
                        "source_channels": rng.sample(
                            _CHANNELS, k=rng.choice((1, 1, 2))
                        ),
                        "suggested_action": f"{sentinel} action",
                        "priority_kind": kind,
                        "entity_kind": "thread",
                        "entity_id": f"t-{uid()[:10]}",
                    }
                )
            bundle = fresh + carry[:2]
            created = _utc(d, 6.5)
            db.add(
                HomeCard(
                    id=str(uuid.uuid4()),
                    user_email=email,
                    date=d.isoformat(),
                    category="daily_briefing",
                    content_md=f"{sentinel} briefing",
                    metadata_json={
                        "schema_version": 1,
                        "headline": f"{sentinel} headline",
                        "last_critique_score": round(rng.uniform(6.0, 9.5), 1),
                        "audit_trail": [f"{sentinel} audit"],
                        "priorities": bundle,
                    },
                    priority=100,
                )
            )
            bump("home_cards")
            # What happened to each fresh card.
            # Someone who never opens Cubie (or stopped) never touches a card;
            # Cubie may still close one when it sees the work done.
            idle = persona == "never" or (persona == "dormant" and d >= dormant_after)
            for p in fresh:
                roll = rng.random()
                if idle:
                    state, by = (
                        ("resolved", "system") if roll < 0.2 else ("active", None)
                    )
                    if by is None:
                        carry.append(p)
                elif persona in ("power", "regular") and roll < 0.38:
                    state, by = "resolved", email
                elif roll < 0.5:
                    state, by = "resolved", "system"
                elif roll < 0.62:
                    state, by = "dismissed", email
                elif roll < 0.68:
                    state, by = "snoozed", email
                else:
                    state, by = "active", None
                    carry.append(p)
                changed = created + timedelta(hours=rng.uniform(1, 30))
                if changed.date() > today:
                    changed = created + timedelta(hours=2)
                # priority_memory has a users FK, so a departed person (no
                # users row) has no memory rows, as after a real offboarding.
                if persona != "departed":
                    db.add(
                        PriorityMemory(
                            id=uid(),
                            user_email=email,
                            entity_kind=p["entity_kind"],
                            entity_id=p["entity_id"],
                            priority_kind=p["priority_kind"],
                            state=state,
                            last_priority_id=p["id"],
                            first_seen_at=created,
                            last_seen_at=created,
                            last_state_change_at=changed if by else created,
                            last_state_change_by=by,
                        )
                    )
                    bump("priority_memory")
                if d >= tracking_start and by is not None and persona != "departed":
                    kind = {
                        ("resolved", True): "resolved",
                        ("resolved", False): "auto_resolved",
                        ("dismissed", True): "dismissed",
                        ("snoozed", True): "snoozed",
                    }[(state, by != "system")]
                    if kind == "resolved" and rng.random() < 0.3:
                        kind = "slack_followup"
                    db.add(
                        PriorityEvent(
                            user_email=email,
                            kind=kind,
                            priority_id=p["id"],
                            entity_kind=p["entity_kind"],
                            entity_id=p["entity_id"],
                            priority_kind=p["priority_kind"],
                            actor=by,
                            department_slug=None,
                            at=changed,
                        )
                    )
                    bump("priority_events")
            if d >= tracking_start and persona != "departed":
                db.add(
                    PriorityEvent(
                        user_email=email,
                        kind="delivered_slack",
                        actor="system",
                        at=created + timedelta(minutes=30),
                    )
                )
                bump("priority_events")
            carry = carry[-4:]
            if rng.random() < 0.08:
                status = "failed" if rng.random() < 0.15 else "ready"
                db.add(
                    PrioritiesRun(
                        run_id=str(uuid.uuid4()),
                        user_email=email,
                        status=status,
                        queued_at=_utc(d, 11),
                        finished_at=_utc(d, 11.1),
                    )
                )
                bump("priorities_runs")
            if rng.random() < 0.1:
                db.add(
                    AssistantFeedback(
                        id=uid(),
                        surface="priorities",
                        proposal_id=uid(),
                        proposal_text=f"{sentinel} rated card",
                        rating=rng.choice(("useful", "useful", "meh", "not_useful")),
                        user_email=email,
                        created_at=_utc(d, 9),
                        updated_at=_utc(d, 9),
                    )
                )
                bump("assistant_feedback")
            d += timedelta(days=1)

    seed_time_zone = workspace_time_zone_name()

    # ── scheduled tasks (one team only: a feature that has not spread yet) ──
    for email, dept, persona in people:
        if dept != "Engineering" or persona not in ("power", "regular"):
            continue
        task_id = uid()
        db.add(
            ScheduledTask(
                id=task_id,
                user_email=email,
                title=f"{sentinel} task",
                prompt=f"{sentinel} prompt",
                schedule_kind="weekdays",
                time_local="08:00",
                timezone=seed_time_zone,
                delivery_channels=[],
                status="active",
                created_at=_utc(start, 9),
            )
        )
        d = start
        while d <= today:
            if d.weekday() < 5:
                sid = str(uuid.uuid4())
                db.add(
                    ChatSession(
                        id=sid,
                        user_email=email,
                        title=f"{sentinel} scheduled",
                        created_at=_utc(d, 8.05),
                    )
                )
                db.add(
                    ScheduledTaskRun(
                        id=uid(),
                        task_id=task_id,
                        user_email=email,
                        status="failed" if rng.random() < 0.07 else "succeeded",
                        chat_session_id=sid,
                        created_at=_utc(d, 8),
                    )
                )
                db.add(
                    InteractionLog(
                        id=str(uuid.uuid4()),
                        session_id=sid,
                        user_email=email,
                        timestamp=_utc(d, 8.05),
                        user_input=f"{sentinel} scheduled prompt",
                        system_prompt=f"Scheduled task {task_id} | run x",
                        llm_output=f"{sentinel} scheduled answer",
                        duration_ms=40000,
                        successful=True,
                        mode="scheduled",
                    )
                )
                bump("scheduled_task_runs")
            d += timedelta(days=1)

    # ── Phase 2: CRM & HubSpot, Proposals & Prep, Follow-through ───────────
    for name, n in _seed_phase2(
        db,
        people=people,
        today=today,
        start=start,
        dormant_after=dormant_after,
        sentinel=sentinel,
        rng_seed=rng_seed,
    ).items():
        bump(name, n)

    # ── Phase 3: AI Spend, Knowledge & Content, Quality & Trust (+ EOS rows) ──
    _seed_phase3(
        db,
        people=people,
        today=today,
        start=start,
        tracking_start=tracking_start,
        dormant_after=dormant_after,
        sentinel=sentinel,
        bump=bump,
        rng_seed=rng_seed,
    )

    db.commit()
    return {
        "people": len(PEOPLE),
        "departed": 1,
        "days": days,
        "start": start.isoformat(),
        "end": today.isoformat(),
        "rows": counts,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2 — the rows the CRM & HubSpot, Proposals & Prep and Follow-through
# dashboards read. Kept in its own function, with its own random stream, so
# the Phase-1 rows above (and every number the Phase-1 tests pin) stay the
# same, and a Phase-3 seed can be appended after it without touching it.
#
# Only people who use Cubie ("power", "regular", "light") ever DO anything
# here: a "never" person never approves, edits or keeps anything, and a
# "dormant" person does nothing at all, so the Phase-1 activity numbers hold.
# The morning Loose Ends scan still flags their loose ends, as it would.
# ═════════════════════════════════════════════════════════════════════════════


def _seed_phase2(
    db,
    *,
    people: list[tuple[str, str, str]],
    today: date,
    start: date,
    dormant_after: date,
    sentinel: str,
    rng_seed: int,
) -> dict[str, int]:
    """Insert the Phase-2 demo rows through ``db`` (no commit). Returns row
    counts by table."""
    from rubik_core.database.call_capture import (
        CallExtraction,
        CallExtractionItem,
        CallHubspotWrite,
        CallSession,
    )
    from rubik_core.database.call_scripts import CallScript, CallSessionScript
    from rubik_core.database.commitments import (
        Commitment,
        CommitmentNudge,
        CommitmentStageLag,
    )
    from rubik_core.database.loop_closure import LoopItem
    from rubik_core.database.models import (
        CrmSyncRun,
        CrmSyncSuggestion,
        MeetingPrepRun,
        XraySnapshot,
    )
    from rubik_core.database.promises import Promise, PromiseEvent
    from rubik_core.database.proposals import Proposal, ProposalVersion
    from rubik_core.tenancy.workspace_time import workspace_time_zone_name

    # The app's sessions do not autoflush: the Phase-1 proposals and Meeting
    # Prep rows this function updates must be in the database first.
    db.flush()
    rng = random.Random(rng_seed + 202)
    seed_time_zone = workspace_time_zone_name()
    counts: dict[str, int] = {}
    latest = _utc(today, 9)  # nothing below is stamped later than this

    def bump(name: str, n: int = 1) -> None:
        counts[name] = counts.get(name, 0) + n

    def uid() -> str:
        return uuid.uuid4().hex

    def cap(dt: datetime) -> datetime:
        return min(dt, latest)

    def weekdays(first: date, last: date):
        d = first
        while d <= last:
            if d.weekday() < 5:
                yield d
            d += timedelta(days=1)

    departed_cutoff = today - timedelta(days=15)

    def acts(persona: str) -> bool:
        return persona in ("power", "regular", "light")

    def last_day(persona: str) -> date:
        return departed_cutoff - timedelta(days=1) if persona == "departed" else today

    sales = [(e, p) for e, d, p in people if d == "Sales"]
    aes = [(e, p) for e, p in sales if p in ("power", "regular", "departed")]
    manager = next(e for e, p in sales if p == "power")  # the Sales exec

    # ── CRM Sync (UC-46) ────────────────────────────────────────────────────
    change_types = (
        "update_deal",
        "update_deal",
        "update_contact",
        "create_contact",
        "create_deal",
    )
    for email, persona in [(e, p) for e, p in sales if acts(p) or p == "departed"]:
        for d in weekdays(start, last_day(persona)):
            if d.weekday() != 0 and rng.random() > 0.15:
                continue
            run_id = uid()
            at = _utc(d, rng.uniform(6, 9))
            db.add(
                CrmSyncRun(
                    run_id=run_id,
                    user_email=email,
                    status="ready",
                    trigger="nightly",
                    stats={"suggestions_created": 0},
                    queued_at=at,
                    finished_at=at + timedelta(minutes=3),
                )
            )
            bump("crm_sync_runs")
            for _ in range(rng.randint(1, 4)):
                roll = rng.random()
                status = (
                    "applied"
                    if roll < 0.5
                    else "dismissed"
                    if roll < 0.68
                    else "expired"
                    if roll < 0.76
                    else "superseded"
                    if roll < 0.83
                    else "failed"
                    if roll < 0.88
                    else "pending"
                )
                decided = cap(at + timedelta(hours=rng.uniform(0.5, 60)))
                if persona == "departed" and decided.date() >= departed_cutoff:
                    status = "expired"
                db.add(
                    CrmSyncSuggestion(
                        id=uid(),
                        user_email=email,
                        run_id=run_id,
                        meeting_title=f"{sentinel} meeting",
                        change_type=rng.choice(change_types),
                        hubspot_object_type="deal",
                        target_key=f"t-{uid()}",
                        dedupe_key=f"k-{uid()}",
                        payload={"properties": {"amount": {"after": sentinel}}},
                        evidence={"quote": f"{sentinel} quote"},
                        confidence=round(rng.uniform(0.6, 0.98), 2),
                        status=status,
                        error=f"{sentinel} error" if status == "failed" else None,
                        applied_at=decided if status == "applied" else None,
                        applied_by=email if status == "applied" else None,
                        hubspot_response={"id": sentinel}
                        if status == "applied"
                        else None,
                        created_at=at,
                        updated_at=decided if status != "pending" else at,
                    )
                )
                bump("crm_sync_suggestions")

    # ── Call Capture: sessions, wrap-up items, HubSpot writes, scripts ──────
    # Five Sales AEs record calls (shown per department); one marketer records
    # two (a group of one: hidden for everyone).
    scripts = []
    for key in ("implementation", "support_plan"):
        script = CallScript(
            id=uid(), product_key=key, name=f"{key} checklist", source="manual"
        )
        db.add(script)
        scripts.append(script)
    marketer = next(e for e, d, p in people if d == "Marketing" and p == "regular")
    callers = [(e, p) for e, p in sales if p in ("power", "regular")]
    callers.append((marketer, "regular"))
    item_kinds = ("vendor_field", "task", "sow_request", "stage_signal", "lead")
    for email, _persona in callers:
        n_calls = 2 if email == marketer else 0
        days = list(weekdays(start, today))
        picks = (
            rng.sample(days, n_calls)
            if n_calls
            else [d for d in days if rng.random() < 0.3]
        )
        for d in picks:
            sid = uid()
            at = _utc(d, rng.uniform(9, 15))
            db.add(
                CallSession(
                    id=sid,
                    ae_email=email,
                    status="wrapped",
                    origin="calendar",
                    attrs={"company_name": f"{sentinel} client"},
                    created_at=at,
                )
            )
            ext = uid()
            db.add(
                CallExtraction(
                    id=ext,
                    call_session_id=sid,
                    model="gemini-flash",
                    prompt_version="v1",
                    schema_version="1",
                    raw_output={"text": f"{sentinel} transcript"},
                    created_at=at + timedelta(minutes=20),
                )
            )
            bump("call_sessions")
            for script in rng.sample(scripts, k=rng.choice((0, 1, 1, 2))):
                db.add(
                    CallSessionScript(
                        id=uid(),
                        call_session_id=sid,
                        script_id=script.id,
                        product_key=script.product_key,
                        attached_at=at - timedelta(minutes=5),
                    )
                )
                bump("call_session_scripts")
            for _ in range(rng.randint(1, 4)):
                roll = rng.random()
                status = (
                    "written"
                    if roll < 0.55
                    else "rejected"
                    if roll < 0.72
                    else "write_failed"
                    if roll < 0.78
                    else "superseded"
                    if roll < 0.85
                    else "proposed"
                )
                item = uid()
                decided = cap(at + timedelta(hours=rng.uniform(1, 30)))
                db.add(
                    CallExtractionItem(
                        id=item,
                        extraction_id=ext,
                        call_session_id=sid,
                        kind=rng.choice(item_kinds),
                        proposed_value=f"{sentinel} value",
                        evidence_text=f"{sentinel} evidence",
                        status=status,
                        decided_by=email if status != "proposed" else None,
                        decided_at=decided if status != "proposed" else None,
                        created_at=at + timedelta(minutes=21),
                    )
                )
                bump("call_extraction_items")
                if status in ("written", "write_failed"):
                    ok = status == "written"
                    db.add(
                        CallHubspotWrite(
                            id=uid(),
                            extraction_item_id=item,
                            call_session_id=sid,
                            idempotency_key=uid(),
                            method="PATCH",
                            path="/crm/v3/objects/companies/1",
                            request_body={"properties": {"note": sentinel}},
                            response_status=200 if ok else 400,
                            response_body={"message": sentinel},
                            attempts=1 if ok else 2,
                            succeeded_at=decided if ok else None,
                            created_at=decided,
                            updated_at=decided,
                        )
                    )
                    bump("call_hubspot_writes")

    # ── Sales Cockpit: commitments, nudges, stage corrections ───────────────
    kinds = ("sow", "wrapup", "lead_touch")
    for email, persona in aes:
        for d in weekdays(start, last_day(persona)):
            if rng.random() > 0.18:
                continue
            opened = _utc(d, rng.uniform(9, 16))
            sla = rng.choice((8, 16, 24, 40))
            breach_at = opened + timedelta(hours=sla * 3)
            breached = breach_at <= latest and rng.random() < 0.35
            closed = None
            if not breached and rng.random() < 0.7:
                closed = cap(opened + timedelta(hours=rng.uniform(2, sla * 2.5)))
            elif breached and rng.random() < 0.5:
                closed = cap(breach_at + timedelta(hours=rng.uniform(4, 60)))
            state = (
                "closed" if closed is not None else "breached" if breached else "open"
            )
            row = Commitment(
                id=uid(),
                kind=rng.choice(kinds),
                hubspot_deal_id=f"deal-{uid()[:10]}",
                owner_email=email,
                opened_at=opened,
                opened_source=rng.choice(("call", "manual")),
                opened_evidence_text=f"{sentinel} evidence",
                sla_hours=sla,
                breach_at=breach_at,
                breached_at=breach_at if breached else None,
                closed_at=closed,
                closed_source="manual" if closed is not None else None,
                state=state,
                created_at=opened,
                updated_at=closed or opened,
            )
            row.refresh_live_scope_flags()
            db.add(row)
            bump("commitments")
            if breached and persona != "departed" and rng.random() < 0.8:
                db.add(
                    CommitmentNudge(
                        id=uid(),
                        commitment_id=row.id,
                        from_email=manager,
                        to_email=email,
                        channel="both",
                        hubspot_task_id=f"task-{uid()[:8]}",
                        sent_at=cap(breach_at + timedelta(hours=rng.uniform(2, 20))),
                        created_at=cap(breach_at + timedelta(hours=2)),
                    )
                )
                bump("commitment_nudges")
    for d in weekdays(start, today):
        if rng.random() > 0.25:
            continue
        email, _p = rng.choice([(e, p) for e, p in sales if p in ("power", "regular")])
        status = rng.choice(("applied", "applied", "rejected", "open"))
        at = _utc(d, rng.uniform(9, 15))
        db.add(
            CommitmentStageLag(
                id=uid(),
                hubspot_deal_id=f"deal-{uid()[:10]}",
                hubspot_stage_id="drafting",
                evidence_stage_id="sow_sent",
                evidence_text=f"{sentinel} stage evidence",
                confidence=0.8,
                status=status,
                resolved_by=email if status != "open" else None,
                resolved_at=cap(at + timedelta(hours=5)) if status != "open" else None,
                created_at=at,
                updated_at=at,
            )
        )
        bump("commitment_stage_lags")

    # ── Loose Ends: started 24 days ago, the morning scan of every AE ───────
    from rubik_core.identifiers import LOOP_CLOSE_LOGGED_FROM_ASSISTANT
    from rubik_core.loop_closure.constants import (
        DISMISS_REASONS,
        FIXED_CLOSE_REASONS,
    )

    fixed = [r for r in FIXED_CLOSE_REASONS if r != LOOP_CLOSE_LOGGED_FROM_ASSISTANT]
    loose_start = today - timedelta(days=24)
    sources = {
        "unlogged": ("gmail", "slack"),
        "open_loop": ("gmail",),
        "todo": ("promise", "hubspot_task"),
    }
    for email, persona in sales:
        if persona == "departed":
            continue
        for d in weekdays(loose_start, today):
            for _ in range(rng.choice((0, 1, 1, 2, 3))):
                kind = rng.choice(tuple(sources))
                flagged = _utc(d, 12.2)  # midday, after the morning scan
                opened = flagged - timedelta(days=rng.uniform(0.5, 6))
                roll = rng.random()
                status, closed, reason, dismissed, why = "open", None, None, None, None
                if acts(persona) and roll < 0.13:
                    status = "dismissed"
                    dismissed = flagged + timedelta(hours=rng.uniform(1, 30))
                    why = rng.choice(DISMISS_REASONS)
                elif roll < 0.65:
                    status = "closed"
                    closed = flagged + timedelta(days=rng.uniform(0.1, 6))
                    if acts(persona) and kind == "unlogged" and rng.random() < 0.35:
                        reason = LOOP_CLOSE_LOGGED_FROM_ASSISTANT
                    elif rng.random() < 0.08:
                        reason = rng.choice(("rescheduled", "aged_out"))
                    else:
                        reason = rng.choice(fixed)
                if (closed and closed > latest) or (dismissed and dismissed > latest):
                    status, closed, reason, dismissed, why = (
                        "open",
                        None,
                        None,
                        None,
                        None,
                    )
                db.add(
                    LoopItem(
                        owner_email=email,
                        kind=kind,
                        source=rng.choice(sources[kind]),
                        source_key=f"demo:{uid()}",
                        status=status,
                        title=f"{sentinel} subject",
                        headline=f"{sentinel} headline",
                        counterparty_email="client@example.com",
                        counterparty_label=f"{sentinel} client",
                        company_label=f"{sentinel} company",
                        opened_at=opened,
                        first_flagged_at=flagged,
                        last_checked_at=latest,
                        closed_at=closed,
                        close_reason=reason,
                        close_evidence={"type": "notes", "id": sentinel},
                        dismissed_at=dismissed,
                        dismiss_reason=why,
                        dismissed_by=email if dismissed else None,
                        detail={"note": sentinel},
                        created_at=flagged,
                        updated_at=closed or dismissed or flagged,
                    )
                )
                bump("loop_items")

    # ── Proposals: more of them, then confirm, version and link them all ────
    for email, persona in sales:
        if persona not in ("power", "regular"):
            continue
        for d in weekdays(start, today):
            if rng.random() > 0.1:
                continue
            db.add(
                Proposal(
                    id=uid(),
                    owner_email=email,
                    title=f"{sentinel} proposal",
                    status=rng.choice(
                        ("drafted",) * 5 + ("brief",) * 3 + ("archived",)
                    ),
                    created_at=_utc(d, rng.uniform(10, 16)),
                )
            )
            bump("proposals")
    db.flush()
    for prop in db.query(Proposal).all():
        prop.brief = {"service_title": f"{sentinel} service"}
        prop.brief_source = {"origin": f"{sentinel} call"}
        if rng.random() < 0.6:
            prop.hubspot_deal_id = f"deal-{uid()[:10]}"
        if prop.status == "drafted" or rng.random() < 0.35:
            prop.brief_confirmed_at = cap(
                prop.created_at + timedelta(hours=rng.uniform(1, 20))
            )
        if prop.status != "drafted":
            continue
        at = prop.brief_confirmed_at
        for n in range(1, rng.choice((1, 1, 2, 2, 3, 4)) + 1):
            at = cap(at + timedelta(hours=rng.uniform(0.1, 30)))
            db.add(
                ProposalVersion(
                    id=uid(),
                    proposal_id=prop.id,
                    version_no=n,
                    brief={"service_title": f"{sentinel} service"},
                    content={"summary": f"{sentinel} prose"},
                    instruction=f"{sentinel} instruction" if n > 1 else None,
                    change_summary=f"{sentinel} change" if n > 1 else None,
                    created_by=prop.owner_email,
                    created_at=at,
                )
            )
            bump("proposal_versions")

    # ── Meeting Prep versions (tracked from 9 days ago) and deep preps ──────
    versions_from = today - timedelta(days=9)
    for run in db.query(MeetingPrepRun).filter(
        MeetingPrepRun.created_at >= _utc(versions_from, 0)
    ):
        run.change_kind, run.version, run.event_id = "generated", 1, f"evt-{uid()}"
    for email, dept, persona in people:
        if persona not in ("power", "regular"):
            continue
        for d in weekdays(versions_from, today):
            if rng.random() > 0.4:
                continue
            event = f"evt-{uid()}"
            at = _utc(d, rng.uniform(7, 9))
            if at > latest:
                continue
            if rng.random() < 0.06:
                db.add(
                    MeetingPrepRun(
                        id=uid(),
                        created_at=at,
                        user_email=email,
                        event_id=event,
                        title=f"{sentinel} meeting",
                        mode="sync",
                        change_kind="failed",
                    )
                )
                bump("meeting_prep_runs")
                continue
            version = 1
            for kind in ["generated"] + [
                k
                for k, p in (
                    ("section_edited", 0.3),
                    ("section_rewritten", 0.25),
                    ("regenerated", 0.1),
                    ("restored", 0.05),
                )
                if rng.random() < p
            ]:
                db.add(
                    MeetingPrepRun(
                        id=uid(),
                        created_at=cap(at + timedelta(minutes=10 * (version - 1))),
                        user_email=email,
                        event_id=event,
                        title=f"{sentinel} meeting",
                        response={"summary": f"{sentinel} brief"},
                        mode="sync",
                        version=version,
                        change_kind=kind,
                        change_note=f"{sentinel} instruction"
                        if kind == "section_rewritten"
                        else None,
                    )
                )
                bump("meeting_prep_runs")
                version += 1
        if dept in ("Sales", "Engineering"):
            for d in weekdays(start, today):
                if rng.random() > 0.1:
                    continue
                db.add(
                    MeetingPrepRun(
                        id=uid(),
                        created_at=_utc(d, 5),
                        user_email=email,
                        title=f"{sentinel} meeting",
                        mode="deep",
                        delivery_status="failed" if rng.random() < 0.1 else "delivered",
                    )
                )
                bump("meeting_prep_runs")

    # ── Customer X-Ray snapshots ────────────────────────────────────────────
    for email, dept, persona in people:
        if dept not in ("Sales", "Engineering") or not acts(persona):
            continue
        for d in weekdays(start, today):
            if rng.random() > 0.08:
                continue
            db.add(
                XraySnapshot(
                    id=uid(),
                    company_id=f"co-{uid()[:8]}",
                    company_name=f"{sentinel} company",
                    kind=rng.choice(("fast", "fast", "fast", "deep")),
                    status="failed" if rng.random() < 0.08 else "ready",
                    sections={"executive_summary": f"{sentinel} summary"},
                    created_by=email,
                    created_at=_utc(d, rng.uniform(9, 16)),
                )
            )
            bump("xray_snapshots")

    # ── My To-Dos: promises and their ledger ────────────────────────────────
    promise_sources = (
        "heard_transcript",
        "heard_email",
        "page",
        "chat",
        "slack_cmd",
        "priority_card",
    )
    for email, dept, persona in people:
        if dept not in ("Sales", "Engineering") or not acts(persona):
            continue
        for d in weekdays(start, today):
            if rng.random() > (0.25 if persona != "light" else 0.08):
                continue
            created = _utc(d, rng.uniform(9, 16))
            roll = rng.random()
            status = (
                "kept"
                if roll < 0.42
                else "dropped"
                if roll < 0.52
                else "slipping"
                if roll < 0.64
                else "evidence_found"
                if roll < 0.69
                else "suggested"
                if roll < 0.77
                else "open"
            )
            done = created + timedelta(days=rng.uniform(0.3, 9))
            if status in ("kept", "dropped") and done > latest:
                status = "open"
            due = (created + timedelta(days=rng.randint(1, 7))).date()
            if status == "slipping" and due >= today:
                due = today - timedelta(days=1)
            prom = Promise(
                id=uid(),
                owner_email=email,
                title=f"{sentinel} promise",
                detail=f"{sentinel} detail",
                status=status,
                promised_for=due,
                due_local=due,
                timezone=seed_time_zone,
                source=rng.choice(promise_sources),
                source_ref={"quote": f"{sentinel} quote"},
                slipping_since=_utc(due + timedelta(days=1), 6)
                if status == "slipping"
                else None,
                created_at=created,
                updated_at=created,
                kept_at=done if status == "kept" else None,
                dropped_at=done if status == "dropped" else None,
            )
            db.add(prom)
            bump("promises")
            events = [("created", email, created)]
            if status == "kept":
                events.append(("kept_confirmed", email, done))
            elif status == "dropped":
                events.append(("dropped", email, done))
            elif status == "slipping":
                events.append(("slipping", "system", prom.slipping_since))
            if status in ("kept", "open", "slipping") and rng.random() < 0.15:
                prom.eos_todo_id = f"eos-{uid()[:8]}"
                events.append(
                    ("promoted_eos", email, cap(created + timedelta(hours=3)))
                )
            for kind, actor, at in events:
                db.add(
                    PromiseEvent(
                        id=uid(),
                        promise_id=prom.id,
                        at=at,
                        actor=actor,
                        kind=kind,
                        payload={"note": sentinel},
                    )
                )
                bump("promise_events")
    db.flush()
    return counts


# ── Phase 3: AI Spend, Knowledge & Content, Quality & Trust (+ EOS rows) ──────

_TOPICS = (
    "Pricing",
    "Policies",
    "Sales Playbooks",
    "Compliance",
    "Product",
    "Onboarding",
)


def _seed_phase3(
    db,
    *,
    people: list[tuple[str, str, str]],
    today: date,
    start: date,
    tracking_start: date,
    dormant_after: date,
    sentinel: str,
    bump,
    rng_seed: int,
) -> None:
    """Rows for every table the Phase 3 dashboards read, plus EOS (quarters,
    seats, rocks and their weekly updates, to-dos, issues, L10s, the scorecard;
    no dashboard reads these since EOS Health was removed, but EOS activity
    still counts in Adoption), AI
    spend that is tied to a person (the nightly run from the Insights release
    on, Loose Ends, knowledge capture, proposals, plus whole-company-only
    features), documents and transcripts with versions, knowledge entries
    proposed from email, answers with and without citations, and ratings on
    Meeting Prep, CRM Sync and Customer X-Ray. Proposals, Meeting Prep
    versions, CRM Sync suggestions and Loose Ends are Phase 2's rows; this
    only adds the AI spend and ratings that go with them. Every free-text
    column carries the sentinel.

    Personas are respected: a never-active person does nothing, a dormant one
    stops on ``dormant_after``, and only days a person is active carry any of
    the things Insights counts as "using Cubie" (a proposal, a brief, a CRM
    Sync approval, a proposal-generator call)."""
    from rubik_core.database.eos import (
        Issue,
        IssueStatus,
        L10Meeting,
        L10Session,
        Organization,
        Person,
        Quarter,
        Rock,
        RockStatus,
        RockWeeklyUpdate,
        ScorecardEntry,
        ScorecardMetric,
        Seat,
        Todo,
        TodoStatus,
        Visibility,
    )
    from rubik_core.database.models import (
        Asset,
        AssetVersion,
        AssistantFeedback,
        ChatMessage,
        CrmSyncSuggestion,
        InteractionLog,
        LLMUsageEvent,
        MeetingPrepRun,
    )
    from rubik_core.database.proposals import Proposal

    rng = random.Random(rng_seed + 3)

    def uid() -> str:
        return uuid.uuid4().hex

    def active(persona: str, d: date) -> bool:
        if persona == "never" or persona == "departed":
            return False
        if persona == "dormant" and d >= dormant_after:
            return False
        return d.weekday() < 5 and rng.random() < _P_ACTIVE.get(persona, 0.0)

    def usage(feature, at, cost, email=None, model="gemini-flash"):
        db.add(
            LLMUsageEvent(
                id=str(uuid.uuid4()),
                timestamp=at,
                feature=feature,
                model=model,
                total_tokens=int(cost * 200000),
                cost_usd=round(cost, 5),
                user_email=email,
            )
        )
        bump("llm_usage_events")

    current = [(e, dept, p) for e, dept, p in people if p != "departed"]
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]

    # ── EOS ──────────────────────────────────────────────────────────────────
    org = Organization(name="Demo organization", created_by="seed")
    db.add(org)
    db.flush()

    def quarter_bounds(d: date) -> tuple[date, date, str]:
        q = (d.month - 1) // 3
        first = date(d.year, 3 * q + 1, 1)
        nxt = date(d.year + 1, 1, 1) if q == 3 else date(d.year, 3 * q + 4, 1)
        return first, nxt - timedelta(days=1), f"Q{q + 1} {d.year}"

    quarters = []
    anchor = today
    for _ in range(3):
        first, last, name = quarter_bounds(anchor)
        quarters.append((first, last, name))
        anchor = first - timedelta(days=1)
    quarters.reverse()
    q_rows = []
    for first, last, name in quarters:
        q = Quarter(
            organization_id=org.id,
            name=name,
            start_date=datetime(first.year, first.month, first.day),
            end_date=datetime(last.year, last.month, last.day, 23, 59, 59),
            is_current=first <= today <= last,
            created_by="seed",
        )
        db.add(q)
        q_rows.append((q, first, last))
    db.flush()

    persons: dict[str, Person] = {}
    team_people: dict[str, list[Person]] = {}
    for email, dept, _persona in current:
        seat = Seat(department_name=dept, title=f"{sentinel} seat", created_by="seed")
        db.add(seat)
        db.flush()
        first_name, last_name = email.split("@")[0].split(".")
        p = Person(
            first_name=first_name.title(),
            last_name=last_name.title(),
            email=email,
            seat_id=seat.id,
            created_by="seed",
        )
        db.add(p)
        persons[email] = p
        team_people.setdefault(dept, []).append(p)
    db.flush()
    leader = persons.get(next((e for e in persons if e.startswith("yael.")), ""), None)

    teams: list[tuple[str | None, list[Person], bool]] = [
        (dept, members, False) for dept, members in team_people.items()
    ]
    if leader is not None:
        teams.append((None, [leader] + team_people.get("Sales", [])[:1], True))

    for dept, members, is_exec in teams:
        # Rocks, with a weekly update each week the quarter was live.
        for q, first, last in q_rows[-2:]:
            is_now = first <= today <= last
            for _ in range(2 if is_exec else 5):
                roll = rng.random()
                if is_now:
                    status = (
                        "on_track"
                        if roll < 0.5
                        else "off_track"
                        if roll < 0.8
                        else "done"
                    )
                else:
                    status = (
                        "done"
                        if roll < 0.6
                        else "off_track"
                        if roll < 0.85
                        else "on_track"
                    )
                rock = Rock(
                    quarter_id=q.id,
                    owner_id=rng.choice(members).id,
                    description=f"{sentinel} rock",
                    status=RockStatus(status),
                    visibility=Visibility.DEPARTMENT,
                    is_exec=is_exec,
                    department_name=dept,
                    created_at=datetime(first.year, first.month, first.day, 15),
                    created_by="seed",
                )
                db.add(rock)
                db.flush()
                bump("eos_rocks")
                monday = first - timedelta(days=first.weekday())
                while monday <= min(last, today):
                    if monday >= first and rng.random() < 0.8:
                        db.add(
                            RockWeeklyUpdate(
                                rock_id=rock.id,
                                status=RockStatus(
                                    status
                                    if rng.random() < 0.7
                                    else rng.choice(("on_track", "off_track"))
                                ),
                                percent_complete=rng.randint(0, 100),
                                comment=f"{sentinel} update",
                                update_date=monday,
                                created_by="seed",
                            )
                        )
                        bump("eos_rock_updates")
                    monday += timedelta(days=7)
        # L10: one weekly meeting, held most Tuesdays.
        meeting = L10Meeting(
            department_name=dept,
            name=f"{dept or 'Leadership'} L10",
            day_of_week=1,
            start_time="10:00",
            is_exec_meeting=is_exec,
            created_by="seed",
        )
        db.add(meeting)
        db.flush()
        for d in days:
            if d.weekday() == 1 and rng.random() < 0.85:
                at = _utc(d, 10)
                db.add(
                    L10Session(
                        meeting_id=meeting.id,
                        actual_start=at,
                        actual_end=at + timedelta(minutes=90),
                        average_rating=rng.choice((6, 7, 8, 8, 9, 9, 10))
                        if rng.random() < 0.85
                        else None,
                        notes=f"{sentinel} notes",
                        recap=f"{sentinel} recap",
                        created_at=at,
                        created_by="seed",
                    )
                )
                bump("eos_l10_sessions")
            # To-dos most weekdays, due a week later.
            if d.weekday() < 5 and rng.random() < (0.3 if is_exec else 0.6):
                due = d + timedelta(days=7)
                done = due < today and rng.random() < 0.8 or rng.random() < 0.2
                db.add(
                    Todo(
                        owner_id=rng.choice(members).id,
                        description=f"{sentinel} to-do",
                        department_name=dept,
                        is_exec=is_exec,
                        due_date=_utc(due, 12),
                        status=TodoStatus.DONE if done else TodoStatus.PENDING,
                        created_at=_utc(d, 11),
                        created_by="seed",
                    )
                )
                bump("eos_todos")
            # Issues raised now and then; most get solved within a few weeks.
            if d.weekday() < 5 and rng.random() < 0.25:
                solved_at = _utc(d, 11) + timedelta(days=rng.randint(1, 25))
                solved = rng.random() < 0.6 and solved_at.date() <= today
                db.add(
                    Issue(
                        owner_id=rng.choice(members).id,
                        department_name=dept,
                        is_exec=is_exec,
                        title=f"{sentinel} issue",
                        description=f"{sentinel} issue detail",
                        status=IssueStatus.SOLVED if solved else IssueStatus.OPEN,
                        solved_at=solved_at if solved else None,
                        created_at=_utc(d, 11),
                        created_by="seed",
                    )
                )
                bump("eos_issues")
        # Scorecard: four weekly measurables and a monthly one, Sunday-anchored.
        metrics = []
        for i in range(5):
            m = ScorecardMetric(
                owner_id=rng.choice(members).id,
                department_name=dept,
                is_exec=is_exec,
                name=f"{sentinel} measurable {i}",
                frequency="monthly" if i == 4 else "weekly",
                created_at=_utc(start - timedelta(days=30), 9),
                created_by="seed",
            )
            db.add(m)
            metrics.append(m)
        db.flush()
        sunday = start - timedelta(days=(start.weekday() + 1) % 7)
        while sunday + timedelta(days=6) < today:
            for m in metrics[:4]:
                if rng.random() < 0.8:
                    db.add(
                        ScorecardEntry(
                            metric_id=m.id,
                            week_start_date=datetime(
                                sunday.year, sunday.month, sunday.day
                            ),
                            value=str(rng.randint(1, 50)),
                            note=f"{sentinel} note",
                            created_by="seed",
                        )
                    )
                    bump("eos_scorecard_entries")
            sunday += timedelta(days=7)

    # ── AI spend tied to people, and whole-company-only features ───────────
    for d in days:
        for email, dept, persona in current:
            if (
                d.weekday() < 5
                and d >= tracking_start
                and dept in ("Sales", "Marketing", "Engineering")
            ):
                # The nightly run, tied to its person from the Insights release on.
                usage(
                    "ai_priorities",
                    _utc(d, 1.5),
                    rng.uniform(0.05, 0.15),
                    email,
                    "gemini-pro",
                )
            if d.weekday() < 5 and dept == "Sales" and persona != "never":
                usage("loop_closure", _utc(d, 7.2), rng.uniform(0.01, 0.05), email)
        usage(
            "ingestion", _utc(d, 3), rng.uniform(0.2, 0.6), None, "text-embedding-005"
        )
        if d.weekday() < 5:
            # Spend reported for the whole company only, tied and untied.
            who = rng.choice([e for e, _d, p in current if p in ("power", "regular")])
            usage("smart_teams", _utc(d, 14), rng.uniform(0.2, 0.5), who, "gemini-pro")
            usage(
                "smart_teams_guide",
                _utc(d, 15),
                rng.uniform(0.1, 0.3),
                None,
                "gemini-pro",
            )

    # ── Proposal Generator spend, on the day each proposal was started ─────
    # (Proposals and their drafts are Phase 2's rows; a proposal's start is
    # already a day its owner used Cubie, so this adds no active day.)
    for owner, created in db.query(Proposal.owner_email, Proposal.created_at).all():
        if created is not None:
            usage("proposal_generator", created, rng.uniform(0.1, 0.3), owner)

    # ── Knowledge base: documents, versions, transcripts ────────────────────
    for email, dept, persona in current:
        for d in days:
            if not active(persona, d):
                continue
            if rng.random() < 0.08:
                doc_id = uid()
                at = _utc(d, rng.uniform(9, 16))
                db.add(
                    Asset(
                        doc_id=doc_id,
                        owner_email=email,
                        status="approved",
                        topic=rng.choice(_TOPICS),
                        department=dept,
                        description=f"{sentinel} description",
                        tags=[f"{sentinel} tag"],
                        original_filename=f"{sentinel}.pdf",
                        created_at=at,
                    )
                )
                db.add(
                    AssetVersion(
                        id=uid(),
                        doc_id=doc_id,
                        version_number=1,
                        original_filename=f"{sentinel}.pdf",
                        uploaded_by=email,
                        created_at=at,
                    )
                )
                bump("assets")
                later = d + timedelta(days=rng.randint(1, 12))
                if rng.random() < 0.35 and later <= today:
                    db.add(
                        AssetVersion(
                            id=uid(),
                            doc_id=doc_id,
                            version_number=2,
                            original_filename=f"{sentinel} v2.pdf",
                            uploaded_by=email,
                            note=f"{sentinel} version note",
                            change_summary=f"{sentinel} change summary",
                            created_at=_utc(later, 11),
                        )
                    )
                    bump("asset_versions")
            if dept in ("Sales", "Marketing") and rng.random() < 0.3:
                db.add(
                    Asset(
                        doc_id=uid(),
                        owner_email=email,
                        status="approved",
                        topic="Meeting Transcripts",
                        department=None,
                        description=f"{sentinel} transcript",
                        original_filename=f"{sentinel} transcript.txt",
                        created_at=_utc(d, rng.uniform(10, 17)),
                    )
                )
                bump("transcripts")

    # ── Answers, with and without citations, for every question asked ───────
    asked = (
        db.query(InteractionLog.session_id, InteractionLog.timestamp)
        .filter(InteractionLog.session_id.isnot(None))
        .all()
    )
    for session_id, at in asked:
        db.add(
            ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="user",
                content=f"{sentinel} question",
                created_at=at,
            )
        )
        roll = rng.random()
        meta = (
            {"citations": [{"doc_id": uid(), "title": f"{sentinel} cited"}]}
            if roll < 0.45
            else {"citations": []}
            if roll < 0.85
            else {"citations": None}
            if roll < 0.92
            else None
        )
        db.add(
            ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="assistant",
                content=f"{sentinel} answer",
                metadata_json=meta,
                created_at=at + timedelta(seconds=8),
            )
        )
        bump("chat_messages", 2)

    # ── Ratings on Meeting Prep briefs, CRM Sync and Customer X-Ray ─────────
    # (The briefs, suggestions and loose ends themselves are Phase 2's rows.)
    briefs = (
        db.query(
            MeetingPrepRun.user_email,
            MeetingPrepRun.event_id,
            MeetingPrepRun.created_at,
        )
        .filter(MeetingPrepRun.version == 1, MeetingPrepRun.event_id.isnot(None))
        .all()
    )
    for owner, event, at in briefs:
        if rng.random() < 0.4:
            db.add(
                AssistantFeedback(
                    id=uid(),
                    surface="meeting_prep",
                    proposal_id=event,
                    proposal_text=f"{sentinel} rated brief",
                    rating=rng.choice(("useful", "useful", "meh", "not_useful")),
                    user_email=owner,
                    created_at=at,
                    updated_at=at,
                )
            )
            bump("assistant_feedback")
    applied = (
        db.query(
            CrmSyncSuggestion.id,
            CrmSyncSuggestion.user_email,
            CrmSyncSuggestion.applied_at,
        )
        .filter(CrmSyncSuggestion.applied_at.isnot(None))
        .all()
    )
    for sid, owner, at in applied:
        if rng.random() < 0.3:
            db.add(
                AssistantFeedback(
                    id=uid(),
                    surface=rng.choice(("crm_sync", "xray")),
                    proposal_id=sid,
                    proposal_text=f"{sentinel} rated suggestion",
                    rating=rng.choice(("useful", "useful", "meh", "not_useful")),
                    user_email=owner,
                    created_at=at,
                    updated_at=at,
                )
            )
            bump("assistant_feedback")


def _refuse_unless_sqlite() -> None:
    from rubik_core.config import settings

    url = (settings.DATABASE_URL or "").strip().lower()
    if settings.CLOUDSQL_CONNECTION_NAME or not url.startswith("sqlite"):
        sys.exit(
            "Refusing to seed: this script only writes to a local SQLite database. "
            "Set DATABASE_URL=sqlite:///… and unset CLOUDSQL_CONNECTION_NAME."
        )


def main() -> None:
    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the SQLite file's tables and recreate them before seeding",
    )
    args = parser.parse_args()
    _refuse_unless_sqlite()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.company.domain import workspace_domain
    from rubik_core.config import settings
    from rubik_core.database.models import Base

    # This is a disposable demo file, selected explicitly by DATABASE_URL.
    # The normal workspace engine ignores that variable and must not be reset.
    engine = create_engine(settings.DATABASE_URL)
    try:
        if args.reset:
            Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        with sessionmaker(bind=engine)() as db:
            summary = seed(db, domain=workspace_domain(), days=args.days)
    finally:
        engine.dispose()
    print(f"Seeded Insights demo data into {settings.DATABASE_URL}:")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
