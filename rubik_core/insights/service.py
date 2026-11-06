"""The Insights entry points the router calls (contract §4).

Each function opens ONE short ``SessionLocal()`` block for its reads and is
meant to run in a worker thread (``run_in_threadpool``): a dashboard is a
dozen narrow queries and some Python, not something to run on the event loop.
Errors are typed so the router can map them to 400 / 403 / 404 without
looking inside: :class:`~rubik_core.insights.filters.FilterError`,
:class:`~rubik_core.insights.scope.ScopeError`,
:class:`DashboardNotFound` and :class:`~rubik_core.insights.person.PersonNotFound`.
"""

from __future__ import annotations

from typing import Any

from rubik_core.insights import dashboards
from rubik_core.insights.cache import cached
from rubik_core.insights.catalog import catalog_dict
from rubik_core.insights.context import InsightsContext
from rubik_core.insights.filters import RANGES, parse_period, pin_business_tz, tz_name
from rubik_core.insights.population import fold, load_population
from rubik_core.insights.scope import resolve_scope
from rubik_core.insights.signals import drain_activity_buffer

#: How long a person profile stays warm. Shorter than a dashboard's: a person
#: page is usually opened to check something that just happened.
PERSON_CACHE_TTL_SECONDS = 300


class DashboardNotFound(LookupError):
    """Unknown slug, or a dashboard that has not shipped (HTTP 404)."""


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def meta(viewer: Any, *, can_view_people: bool) -> dict:
    with _session() as db:
        scope = resolve_scope(db, viewer)
        population = load_population(db, scope, selected_only=False)
    counts: dict[str, int] = {}
    for p in population.people.values():
        counts[p.dept] = counts.get(p.dept, 0) + 1
    departments = sorted(
        (
            {"slug": slug, "label": scope.label(slug), "people": counts.get(slug, 0)}
            for slug in scope.allowed
        ),
        key=lambda d: d["label"].lower(),
    )
    return {
        "departments": departments,
        "all_departments": scope.all_departments,
        "can_view_people": can_view_people,
        "dashboards": [d.to_dict() for d in dashboards.DASHBOARDS],
        "metrics": catalog_dict(),
        "tz": tz_name(),
        "ranges": list(RANGES),
    }


def _context(
    db, viewer, *, dept, range_key, start, end, can_view_people
) -> InsightsContext:
    period = parse_period(range_key, start, end)
    scope = resolve_scope(db, viewer, dept)
    population = load_population(db, scope)
    return InsightsContext(
        db,
        viewer=viewer,
        scope=scope,
        period=period,
        population=population,
        can_view_people=can_view_people,
    )


@pin_business_tz
def dashboard(
    viewer: Any,
    slug: str,
    *,
    dept: str | None = None,
    range_key: str | None = None,
    start: str | None = None,
    end: str | None = None,
    refresh: bool = False,
    can_view_people: bool = False,
) -> dict:
    """A dashboard payload, served from the 10-minute cache when it can be."""
    module = dashboards.module_for(slug)
    if module is None:
        raise DashboardNotFound(slug)
    d = dashboards.get(slug)
    # Validate the filters before touching the cache, so a bad request is a
    # 400/403 every time rather than a cache hit for someone else's key.
    period = parse_period(range_key, start, end)
    with _session() as db:
        scope = resolve_scope(db, viewer, dept)
    key = (
        "dashboard",
        scope.viewer_email,
        d.slug,
        bool(can_view_people),
        scope.cache_key(),
        period.cache_key(),
        tz_name(),
    )

    def compute() -> dict:
        # Flush the activity buffer FIRST, with no session held: the flush
        # takes its own connection, and taking it while the build holds one
        # is a nested checkout that a burst of builds can deadlock-wait on.
        drain_activity_buffer()
        with _session() as db:
            ctx = _context(
                db,
                viewer,
                dept=dept,
                range_key=range_key,
                start=start,
                end=end,
                can_view_people=can_view_people,
            )
            return module.build(ctx)

    payload, was_cached = cached(key, compute, refresh=refresh)
    return {**payload, "cached": was_cached}


def person_target(
    viewer: Any,
    email: str,
    *,
    range_key: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> str:
    """Validate a person request WITHOUT computing it: the filters (400) and
    that the person is in the viewer's scope (404). Returns the folded email.

    The router calls this, then writes the audit row, and only then asks for
    the profile — so a read that could not be recorded is never served.
    """
    from rubik_core.insights.person import PersonNotFound

    parse_period(range_key, start, end)
    with _session() as db:
        scope = resolve_scope(db, viewer)
        population = load_population(db, scope)
    key = fold(email)
    if population.get(key) is None:
        raise PersonNotFound(key)
    return key


@pin_business_tz
def person(
    viewer: Any,
    email: str,
    *,
    range_key: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """The person profile, warm for five minutes per viewer. Raises
    PersonNotFound outside the viewer's scope."""
    from rubik_core.insights.person import build_person

    period = parse_period(range_key, start, end)
    with _session() as db:
        scope = resolve_scope(db, viewer)
    target = fold(email)
    key = (
        "person",
        scope.viewer_email,
        True,
        scope.cache_key(),
        period.cache_key(),
        tz_name(),
        target,
    )

    def compute() -> dict:
        drain_activity_buffer()  # before the build session, as in dashboard()
        with _session() as db:
            ctx = _context(
                db,
                viewer,
                dept=None,
                range_key=range_key,
                start=start,
                end=end,
                can_view_people=True,
            )
            return build_person(ctx, target)

    body, _was_cached = cached(key, compute, ttl=PERSON_CACHE_TTL_SECONDS)
    return body


def summary(
    viewer: Any,
    slug: str,
    *,
    dept: str | None = None,
    range_key: str | None = None,
    start: str | None = None,
    end: str | None = None,
    refresh: bool = False,
) -> dict:
    """The AI summary of a dashboard view. The payload is built (or read from
    the cache) WITHOUT people tables, so nothing person-level is even in hand
    when the model input is assembled."""
    from rubik_core.insights.summarize import summarize_payload

    payload = dashboard(
        viewer,
        slug,
        dept=dept,
        range_key=range_key,
        start=start,
        end=end,
        refresh=False,
        can_view_people=False,
    )
    return summarize_payload(payload, refresh=refresh)
