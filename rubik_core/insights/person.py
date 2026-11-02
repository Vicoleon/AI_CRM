"""The person profile: what Cubie looks like for one person (contract §4).

Built by narrowing the view to a population of one and asking each dashboard
for its ``person_kpis``, so a person's numbers are computed by exactly the
same code as their department's and can never disagree with them.

What a profile never shows, by rule (README §1): Call Capture use (Google
Meet's terms forbid tracking individuals), and with it the HubSpot writes a
call wrap-up made, the call scripts attached to a call and the Sales Cockpit
commitments; Smart Teams results; or any message content. Those features
are simply absent from ``features``.

The router gates this on ``insights_people``, checks the person is inside the
viewer's scope (404 otherwise, so a profile cannot be used to discover who
exists outside it) and writes an ``INSIGHTS_PERSON_VIEW`` audit row.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from rubik_core.insights.context import InsightsContext
from rubik_core.insights.dashboards import (
    adoption,
    ai_spend,
    ask_cubie,
    crm,
    follow_through,
    knowledge,
    priorities,
    proposals,
)
from rubik_core.insights.dashboards import get as get_dashboard
from rubik_core.insights.filters import Window
from rubik_core.insights.population import describe, fold, resolve_features_for

CALENDAR_DAYS = 90

#: Feature Access key → the parts of Cubie whose use counts as using it. Only
#: features listed here appear on a profile; Call Capture, the Meet add-on and
#: Smart Teams are deliberately absent.
FEATURE_SURFACES: dict[str, frozenset[str]] = {
    "copilot": frozenset({"chat", "priorities", "briefing", "meeting_prep", "assets"}),
    "customer_xray": frozenset({"xray"}),
    "crm_sync": frozenset({"crm_sync"}),
    "sales_cockpit": frozenset({"cockpit"}),
    "proposal_generator": frozenset({"proposals"}),
    "loop_closure": frozenset({"loose_ends"}),
    "todos": frozenset({"todos"}),
    "voice_input": frozenset({"voice"}),
    "voice_commands": frozenset({"voice"}),
    "scheduled_tasks": frozenset({"scheduled_tasks"}),
    "insights": frozenset({"insights"}),
}


class PersonNotFound(LookupError):
    """The person is not in the viewer's scope (HTTP 404)."""


def _card(slug: str, kpis: list[dict]) -> dict:
    d = get_dashboard(slug)
    return {"dashboard": slug, "title": d.title if d else slug, "kpis": kpis}


def build_person(ctx: InsightsContext, email: str) -> dict:
    key = fold(email)
    person = ctx.population.get(key)
    if person is None:
        raise PersonNotFound(key)
    one = InsightsContext(
        ctx.db,
        viewer=ctx.viewer,
        scope=ctx.scope,
        period=ctx.period,
        population=ctx.population.restricted_to(key),
        can_view_people=True,
        now=ctx.now,
    )
    end = ctx.period.current.end
    cal = Window(end - timedelta(days=CALENDAR_DAYS - 1), end)
    # Person-level signals only: a Call Capture use never lands on a calendar.
    per_day = Counter(s.day for s in one.person_signals() if cal.contains(s.day))
    calendar = [
        {"date": d.isoformat(), "count": per_day.get(d, 0)} for d in cal.dates()
    ]

    adoption_kpis = adoption.person_kpis(one)
    ask_kpis = ask_cubie.person_kpis(one)
    prio_kpis = priorities.person_kpis(one)
    # Phase 2. Each one leaves out what is department-level only (Call
    # Capture write-backs, call scripts, Sales Cockpit commitments).
    follow_kpis = follow_through.person_kpis(one)
    crm_kpis = crm.person_kpis(one)
    proposal_kpis = proposals.person_kpis(one)
    by_id = {k["id"]: k for k in adoption_kpis + ask_kpis + prio_kpis}
    kpis = [
        by_id[i]
        for i in ("active_days", "surfaces_used", "questions", "priorities_acted")
        if i in by_id
    ]

    fl = one.first_last().get(key)
    last = fl[1] if fl else None

    used = one.surfaces_used(ctx.period.current, person_level=True).get(key, set())
    verdicts = {} if person.departed else resolve_features_for(person, ctx.db)
    from rubik_core.features.policy import kill_switch_on
    from rubik_core.features.registry import FEATURES

    features = [
        {
            "key": spec.key,
            "label": spec.label,
            "has_access": bool(verdicts.get(spec.key, False)),
            "used": bool(used & FEATURE_SURFACES[spec.key]),
        }
        for spec in FEATURES
        # A feature switched off in this environment is nobody's to have.
        if spec.key in FEATURE_SURFACES and kill_switch_on(spec) is not False
    ]

    notes = [adoption.coverage_note(one), priorities.coverage_note(one)]
    if person.departed:
        notes.insert(
            0,
            f"{person.name} has left the company. Their history stays; they no longer "
            "have access to any feature.",
        )
    return {
        "person": {
            **describe(person),
            "last_active": last.isoformat() if last else None,
        },
        "period": ctx.period.to_dict(),
        "calendar": calendar,
        "kpis": kpis,
        "cards": [
            _card("adoption", adoption_kpis),
            _card("ask-cubie", ask_kpis),
            _card("priorities", prio_kpis),
            _card("follow-through", follow_kpis),
            _card("crm", crm_kpis),
            _card("proposals", proposal_kpis),
            _card("ai-spend", ai_spend.person_kpis(one)),
            _card("knowledge", knowledge.person_kpis(one)),
        ],
        "features": features,
        "notes": notes,
    }
