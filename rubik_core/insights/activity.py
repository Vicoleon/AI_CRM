"""Usage telemetry — who used Cubie, for what, on which day (``activity_daily``).

The HTTP middleware in ``backend/main.py`` calls :func:`record_request` after
every response. A request counts when an authenticated person made it
(``get_current_user`` stashed them on ``request.state``), it was not made under
impersonation, it succeeded (2xx or 3xx), and its route maps to a product area
(:func:`area_for_route`). Counted requests are folded in memory by (person,
local day, area) and upserted in batches, never one write per request.

Design constraints, the same ones ``rubik_core/cost/usage.py`` lives by:

* **Never breaks or slows a request.** Every entry point swallows its own
  exceptions; the database write runs on a short-lived daemon thread, never on
  the event loop.
* **Bounded DB connections.** One short ``SessionLocal()`` per flush, flushed
  at 25 counted requests or 30 seconds, whichever comes first. The Cloud SQL
  pool is small and has been exhausted before.
* **Instantly disableable.** ``INSIGHTS_ENABLED=false`` records nothing.
* **Counts only.** No path parameters, query strings, bodies or titles are
  kept: a row is a person, a day, an area and a number.

Why the route TEMPLATE and not the URL. The template (``/api/xray/{company_id}``)
exists only when the request matched a real route, so a 404 probe or the SPA
catch-all never counts, and it never carries an id. On FastAPI 0.13x an included
router's ``scope["route"].path`` no longer carries the include prefix
(``/home/...`` rather than ``/api/homefeed/...``); the full template lives on
the effective route context FastAPI keeps in ``scope["fastapi"]``, which
:func:`route_template` reads first.

Department is a snapshot of ``slugify_department(user.department)`` at write
time, so a later department change does not rewrite history.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import logging
import threading
import time
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from rubik_core.tenancy.context import (
    NoWorkspaceContext,
    current_workspace,
    use_workspace,
)
from rubik_core.tenancy.threads import context_thread

logger = logging.getLogger(__name__)

# ── Vocabulary ───────────────────────────────────────────────────────────────
# The product areas a request can count toward. Stored as plain strings in
# ``activity_daily.area`` (deliberately not CHECK-constrained, see
# ``rubik_core/database/insights.py``); the recorder rejects anything else.
AREAS: frozenset[str] = frozenset(
    {
        "chat",
        "priorities",
        "briefing",
        "meeting_prep",
        "proposals",
        "crm_sync",
        "call_capture",
        "cockpit",
        "loose_ends",
        "todos",
        "eos",
        "assets",
        "xray",
        "voice",
        "scheduled_tasks",
        "search",
        "help",
        "insights",
        "settings",
        "admin",
        "other",
    }
)

# Route-template prefixes that are NOT usage. Matched on whole path segments.
#
# * Health checks, session mint and ``/users/me``: plumbing every page load
#   makes, so they would mark someone active for opening a tab.
# * The notification bell (polled every 8–60 s) and the sidebar's Recent list
#   (``GET`` on every page, ``/touch`` on every navigation): chrome, not a
#   product.
# * ``/departments``: reference data every picker loads.
# * The login-time automatic transcript ingests, which the browser fires on
#   its own, not the person.
# * Scheduler, webhook and Slack callback endpoints: they carry no user, so the
#   middleware would skip them anyway; listed so the intent is explicit.
# * Smart Teams: advisory only and never performance data, and Insights must
#   not show any Smart Teams data, usage included.
# * What's New (``/api/whats-new``): the drawer's feed loads with the app shell
#   on every page, like the bell; its own click-throughs are counted, as people,
#   by the feature itself. Admin → What's New sits under ``/api/admin``.
_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/api/health",
    "/api/auth",
    "/api/users/me",
    "/api/notifications",
    "/api/recent",
    "/api/departments",
    "/api/ingest/meet-transcripts",
    "/api/ingest/scheduler",
    "/api/fathom/ingest",
    "/api/fathom/webhook",
    "/api/jobs",
    "/api/agents/jobs",
    "/api/slack/commands",
    "/api/slack/interactivity",
    "/api/slack/callback",
    "/api/smart-teams",
    "/api/whats-new",
)

# Pure status polls: a page asking "is it done yet?" every few seconds while a
# run is in flight. One click would otherwise read as dozens of requests. Exact
# templates, so the action that started the run still counts. (The L10 live
# session's 2-second poll alone can trip a 429 rate limit.)
_POLLING_TEMPLATES: frozenset[str] = frozenset(
    {
        "/api/eos/l10/sessions/{session_id}/state",
        "/api/homefeed/priorities/runs/{run_id}",
        "/api/homefeed/prep-runs/{run_id}/status",
        "/api/homefeed/deep-prep/{run_id}/status",
        "/api/dam/{doc_id:path}/status",
        "/api/crm-sync/runs/{run_id}",
        "/api/xray/runs/{snapshot_id}",
    }
)

# Prefix → area. Longest prefix wins (sorted below), matched on whole path
# segments. Anything else under /api counts as "other".
_AREA_PREFIXES: dict[str, str] = {
    # Ask Cubie
    "/api/chat": "chat",
    # Home: AI Priorities, Meeting Prep, and the briefing around them
    "/api/homefeed/priorities": "priorities",
    "/api/agents/recommendations": "priorities",
    "/api/homefeed/meeting-prep": "meeting_prep",
    "/api/homefeed/prep-runs": "meeting_prep",
    "/api/homefeed/prep-docs": "meeting_prep",
    "/api/homefeed/deep-prep": "meeting_prep",
    "/api/homefeed": "briefing",
    "/api/email": "briefing",
    "/api/calendar": "briefing",
    "/api/slack/activity": "briefing",
    "/api/slack/channels": "briefing",
    "/api/drive/recent-activity": "briefing",
    "/api/drive/folders": "briefing",
    # Sales
    "/api/proposals": "proposals",
    "/api/fathom": "proposals",
    "/api/crm-sync": "crm_sync",
    "/api/calls": "call_capture",
    "/api/scripts": "call_capture",
    "/api/meet-addon": "call_capture",
    "/api/sales/loose-ends": "loose_ends",
    "/api/sales": "cockpit",
    "/api/commitments": "cockpit",
    "/api/stage-lag": "cockpit",
    "/api/xray": "xray",
    # Work
    "/api/todos": "todos",
    "/api/eos": "eos",
    "/api/voice": "voice",
    "/api/scheduled-tasks": "scheduled_tasks",
    "/api/directory": "search",
    "/api/insights": "insights",
    "/api/help": "help",
    # Knowledge
    "/api/assets": "assets",
    "/api/dam": "assets",
    "/api/drive": "assets",
    "/api/saved": "assets",
    "/api/topics": "assets",
    "/api/labels": "assets",
    "/api/department-folders": "assets",
    "/api/transcripts": "assets",
    "/api/ingest": "assets",
    # Settings
    "/api/preferences": "settings",
    "/api/drive/preferences": "settings",
    "/api/mcp": "settings",
    "/api/slack/oauth-start": "settings",
    "/api/slack/status": "settings",
    "/api/slack/disconnect": "settings",
    # Admin surfaces (each carries its own admin/manager gate)
    "/api/admin": "admin",
    "/api/feedback/admin": "admin",
    "/api/users": "admin",
    "/api/groups": "admin",
    "/api/policies": "admin",
    "/api/offboarding": "admin",
    "/api/rag": "admin",
}

_AREA_TABLE: tuple[tuple[str, str], ...] = tuple(
    sorted(_AREA_PREFIXES.items(), key=lambda kv: len(kv[0]), reverse=True)
)


def _under(template: str, prefix: str) -> bool:
    return template == prefix or template.startswith(prefix + "/")


def area_for_route(path_template: str | None) -> str | None:
    """The product area a route template counts toward, or ``None`` if it is
    not usage at all (health, session plumbing, polls, anything not under
    ``/api``). Unknown ``/api`` routes count as ``"other"`` so a new router is
    never silently invisible."""
    if not path_template:
        return None
    template = path_template.split("?", 1)[0]
    if len(template) > 1:
        template = template.rstrip("/")
    if not _under(template, "/api"):
        return None
    if template in _POLLING_TEMPLATES:
        return None
    if any(_under(template, prefix) for prefix in _EXCLUDED_PREFIXES):
        return None
    for prefix, area in _AREA_TABLE:
        if _under(template, prefix):
            return area
    return "other"


def route_template(scope: dict) -> str | None:
    """The matched route's full path template, or ``None`` when nothing matched.

    FastAPI 0.13x keeps routers it includes lazily and records the effective
    (prefixed) route on ``scope["fastapi"]["effective_route_context"]``;
    ``scope["route"].path`` there is the router-local path, without the include
    prefix. Older FastAPI flattens routes, so ``scope["route"].path`` is already
    the full template. Read the effective context first, fall back to the route.
    A template that somehow lost its ``/api`` while the URL has one falls back
    to the URL, which maps the same way because the table is prefix-based.
    """
    try:
        # ``path`` is the declared template ("{doc_id:path}"); ``path_format``
        # drops the converter. The tables above are written in declared form.
        template = None
        fastapi_scope = scope.get("fastapi")
        if isinstance(fastapi_scope, dict):
            ctx = fastapi_scope.get("effective_route_context")
            template = getattr(ctx, "path", None) or getattr(ctx, "path_format", None)
        route = scope.get("route")
        if not template and route is not None:
            template = getattr(route, "path", None) or getattr(
                route, "path_format", None
            )
        if not template:
            return None
        url_path = scope.get("path") or ""
        if not _under(template, "/api") and _under(url_path, "/api"):
            return url_path
        return str(template)
    except Exception:  # noqa: BLE001 — telemetry never breaks a request
        return None


# ── Time and identity helpers ────────────────────────────────────────────────


def _settings():
    from rubik_core.config import settings

    return settings


def insights_enabled() -> bool:
    """The ``INSIGHTS_ENABLED`` kill switch (on when the setting is absent)."""
    try:
        return bool(getattr(_settings(), "INSIGHTS_ENABLED", True))
    except Exception:  # noqa: BLE001
        return False


def business_tz() -> ZoneInfo:
    from rubik_core.insights.filters import business_tz as resolved_business_tz

    return resolved_business_tz()


def local_day(at: datetime) -> date:
    """The calendar day, in the company's business time zone, that a naive-UTC
    instant falls on."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(business_tz()).date()


def department_slug(department: str | None) -> str | None:
    if not department:
        return None
    from rubik_core.rag.config import slugify_department

    return slugify_department(department) or None


# ── Buffered recorder ────────────────────────────────────────────────────────
# (workspace, email, day, area) → folded count. The workspace is recorded at
# enqueue because the background and atexit flushes have no reliable scope.
_BUFFER: dict[tuple[str, str, date, str], dict[str, Any]] = {}
_LOCK = threading.Lock()
_pending = 0  # counted requests since the last flush, NOT distinct keys
_last_flush_monotonic = time.monotonic()
_flush_scheduled = False

_FLUSH_THRESHOLD = 25
_FLUSH_INTERVAL_S = 30.0


def record_activity(
    user_email: str | None,
    area: str | None,
    *,
    department: str | None = None,
    at: datetime | None = None,
) -> None:
    """Count one request by ``user_email`` in ``area``. Never raises.

    ``department`` is the person's raw department label; it is slugified here.
    ``at`` is naive UTC (defaults to now).
    """
    global _pending, _flush_scheduled
    if not insights_enabled():
        return
    try:
        email = (user_email or "").strip().lower()
        if not email or area not in AREAS:
            return
        try:
            workspace = current_workspace()
        except NoWorkspaceContext:
            logger.warning("Insights activity discarded: no workspace context")
            return
        at = at or datetime.utcnow()
        key = (workspace, email, local_day(at), area)
        dept = department_slug(department)
        should_flush = False
        with _LOCK:
            agg = _BUFFER.get(key)
            if agg is None:
                _BUFFER[key] = {
                    "requests": 1,
                    "first_at": at,
                    "last_at": at,
                    "department_slug": dept,
                }
            else:
                agg["requests"] += 1
                agg["first_at"] = min(agg["first_at"], at)
                agg["last_at"] = max(agg["last_at"], at)
                if dept:
                    agg["department_slug"] = dept
            _pending += 1
            due = (
                _pending >= _FLUSH_THRESHOLD
                or (time.monotonic() - _last_flush_monotonic) >= _FLUSH_INTERVAL_S
            )
            if due and not _flush_scheduled:
                _flush_scheduled = True
                should_flush = True
        if should_flush:
            _start_background_flush()
    except Exception:  # noqa: BLE001 — recording must never break the caller
        logger.debug("record_activity failed", exc_info=True)


def _start_background_flush() -> None:
    """Flush off the caller's thread: the middleware runs on the event loop,
    and an inline flush would block it on a database round trip."""
    context_thread(flush_activity, name="insights-activity-flush", daemon=True).start()


def record_request(request: Any, status_code: int) -> None:
    """Middleware hook: count ``request`` if it is a person's real usage.

    Skips when no authenticated person was stashed on ``request.state``, when
    the request ran under impersonation (an admin looking as someone must not
    inflate that person's usage, nor their own), when the response was not
    2xx/3xx, and when the route is not usage. Never raises.
    """
    try:
        if not (200 <= int(status_code) < 400):
            return
        state = getattr(request, "state", None)
        email = getattr(state, "actor_email", None)
        if not email:
            return
        if getattr(state, "impersonation", None) or getattr(
            state, "actor_impersonating", False
        ):
            return
        scope = getattr(request, "scope", {}) or {}
        path = scope.get("path", "")
        if path == "/api/email/accounts" or path.startswith("/api/email/accounts/"):
            return
        if getattr(state, "private_gmail_activity", False):
            return
        if path.startswith("/api/email/") and parse_qs(
            scope.get("query_string", b"").decode("utf-8", errors="replace")
        ).get("account_id"):
            return
        area = area_for_route(route_template(request.scope))
        if area is None:
            return
        record_activity(
            email, area, department=getattr(state, "actor_department", None)
        )
    except Exception:  # noqa: BLE001 — telemetry never breaks a request
        logger.debug("record_request failed", exc_info=True)


def _upsert(db, rows: list[dict[str, Any]]) -> None:
    """One INSERT … ON CONFLICT (user_email, day, area) DO UPDATE for the batch.

    Adds the batch's count to the stored one and widens the first/last window.
    The batch has one row per key (it is a dict), which Postgres requires: a
    single ``ON CONFLICT DO UPDATE`` may not touch the same row twice.
    """
    from sqlalchemy import case, func

    from rubik_core.database.insights import ActivityDaily

    table = ActivityDaily.__table__
    dialect = db.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        stmt = insert(table).values(rows)
        new = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.user_email, table.c.day, table.c.area],
            set_={
                "requests": table.c.requests + new.requests,
                # CASE rather than LEAST/GREATEST: portable across both dialects.
                "first_at": case(
                    (new.first_at < table.c.first_at, new.first_at),
                    else_=table.c.first_at,
                ),
                "last_at": case(
                    (new.last_at > table.c.last_at, new.last_at),
                    else_=table.c.last_at,
                ),
                "department_slug": func.coalesce(
                    new.department_slug, table.c.department_slug
                ),
            },
        )
        db.execute(stmt)
        return

    # Any other dialect: read-modify-write. Only reachable from an ad-hoc
    # engine; production is Postgres and the tests are SQLite.
    for row in rows:
        existing = (
            db.query(ActivityDaily)
            .filter(
                ActivityDaily.user_email == row["user_email"],
                ActivityDaily.day == row["day"],
                ActivityDaily.area == row["area"],
            )
            .first()
        )
        if existing is None:
            db.add(ActivityDaily(**row))
        else:
            existing.requests = (existing.requests or 0) + row["requests"]
            existing.first_at = min(existing.first_at, row["first_at"])
            existing.last_at = max(existing.last_at, row["last_at"])
            if row["department_slug"]:
                existing.department_slug = row["department_slug"]


def flush_activity() -> int:
    """Persist the buffered counts. Returns the number of rows upserted.

    Never raises. A failed batch is dropped rather than retried: these are
    counts, and an ever-growing buffer behind a database outage would be worse
    than a gap. Tests and the Insights read path call this to drain the buffer.
    """
    global _pending, _last_flush_monotonic, _flush_scheduled
    with _LOCK:
        batch = dict(_BUFFER)
        _BUFFER.clear()
        _pending = 0
        _last_flush_monotonic = time.monotonic()
    if not batch:
        with _LOCK:
            _flush_scheduled = False
        return 0
    groups: dict[str, list[dict[str, Any]]] = {}
    # Sorted by workspace and unique key so concurrent instances take row
    # locks in the same order within each tenant database.
    for (workspace, email, day, area), agg in sorted(
        batch.items(), key=lambda kv: (kv[0][0], kv[0][1], str(kv[0][2]), kv[0][3])
    ):
        groups.setdefault(workspace, []).append(
            {
                "user_email": email,
                "day": day,
                "area": area,
                "department_slug": agg["department_slug"],
                "requests": agg["requests"],
                "first_at": agg["first_at"],
                "last_at": agg["last_at"],
            }
        )

    written = 0
    try:
        for workspace, rows in groups.items():

            def persist(workspace: str = workspace, rows: list = rows) -> int:
                # Drain process-owned counters without inheriting a support
                # request's narrower workspace authorization context.
                with use_workspace(workspace):
                    db = None
                    try:
                        from rubik_core.database.db import SessionLocal

                        db = SessionLocal()
                        _upsert(db, rows)
                        db.commit()
                        return len(rows)
                    except Exception:  # noqa: BLE001 — isolate one tenant's failure
                        logger.warning(
                            "insights activity flush failed for workspace %s; dropping %d row(s)",
                            workspace,
                            len(rows),
                            exc_info=True,
                        )
                        if db is not None:
                            with contextlib.suppress(Exception):
                                db.rollback()
                        return 0
                    finally:
                        if db is not None:
                            with contextlib.suppress(Exception):
                                db.close()

            try:
                written += contextvars.Context().run(persist)
            except Exception:  # noqa: BLE001 — an invalid scope cannot stop others
                logger.warning(
                    "insights activity flush could not select workspace %s; dropping %d row(s)",
                    workspace,
                    len(rows),
                    exc_info=True,
                )
        return written
    finally:
        with _LOCK:
            _flush_scheduled = False


def reset_buffer() -> None:
    """Drop anything buffered without writing it (tests)."""
    global _pending, _flush_scheduled
    with _LOCK:
        _BUFFER.clear()
        _pending = 0
        _flush_scheduled = False


# Best-effort flush on interpreter shutdown so the tail of the buffer lands.
atexit.register(lambda: flush_activity())
