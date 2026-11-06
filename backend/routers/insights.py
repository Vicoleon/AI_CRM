"""Insights — Cubie's BI dashboards (contract: ``docs/insights/README.md`` §4).

    GET  /api/insights/meta                        departments, dashboards, metric catalog
    GET  /api/insights/dashboards/{slug}           one dashboard's payload
    GET  /api/insights/people/{email}              one person's profile (audited)
    POST /api/insights/dashboards/{slug}/summary   the AI summary of a view

Mounted on the pilot-gated ``core`` parent in ``backend/main.py`` (which adds
``/api``). Gates, in order (house convention, as ``loose_ends.py``):

* **WHETHER**: ``INSIGHTS_ENABLED`` off answers 404 on every route — a
  feature that is switched off is not a permission problem and must not
  advertise itself.
* **WHO**: Admin → Feature Access. ``insights`` opens the dashboards (403
  otherwise); ``insights_people`` additionally opens the person page (403)
  and adds the per-person tables to dashboards (silently omitted without it).
* **HOW MUCH**: the viewer's department scope (``rubik_core/insights/scope``):
  a ``dept`` filter outside it is a 403, a person outside it a 404 (so the
  profile cannot be used to learn who exists elsewhere).

Reading a person's page writes an ``INSIGHTS_PERSON_VIEW`` audit row naming
the real reader (the admin, under impersonation) BEFORE the profile is
computed; if that row cannot be written the answer is 503 and nothing about
the person is returned.

Connection discipline (docs/ENGINEERING_NOTES.md): no handler holds ``Depends(get_db)``. The
work runs in the threadpool and opens its own short ``SessionLocal()`` blocks
in ``rubik_core/insights/service.py``; the audit write opens one more.

Errors the reader sees are plain sentences; an unexpected failure is logged
with its traceback and answered with a fixed message.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/insights", tags=["insights"])

_FAILED = "Insights could not load right now. Try again in a minute."
_NO_PEOPLE = "The people view of Insights is not switched on for you."


def require_insights(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """404 when switched off, 403 when the caller is not on the roster."""
    if not settings.INSIGHTS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    if not feature_allows("insights", user):
        raise HTTPException(
            status_code=403, detail="Insights is not switched on for you yet."
        )
    return user


def _can_view_people(user: UserProfile) -> bool:
    return bool(feature_allows("insights_people", user))


_AUDIT_FAILED = (
    "This person's page could not be opened because the read could not be "
    "recorded. Try again in a minute."
)


def _audit_actor(request: Request, user: UserProfile) -> tuple[str, dict]:
    """``(actor_email, extra details)``: the person who REALLY made the
    request. ``get_current_user`` stashes it on ``request.state.actor_email``
    before any impersonation (person or department) masks the identity, so
    an admin looking as someone is recorded as the admin."""
    real = (getattr(request.state, "actor_email", None) or "").strip().lower()
    shown = (user.email or "").strip().lower()
    if real and real != shown:
        return real, {"impersonating": shown}
    return real or shown, {}


def _record_person_view(actor: str, target: str, details: dict) -> None:
    """Write the ``INSIGHTS_PERSON_VIEW`` row and RAISE if it cannot be.

    ``record_audit`` logs and swallows a failed write, which is right for an
    action that already happened; here the write gates the read, so a person
    page is never served without its audit row (fail closed)."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import AuditLog

    with SessionLocal() as db:
        try:
            db.add(
                AuditLog(
                    actor_email=actor,
                    action="INSIGHTS_PERSON_VIEW",
                    status="success",
                    doc_id=target,
                    details=details,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


async def _run(fn, *args, **kwargs) -> Any:
    """Run a service call in the threadpool and map its typed errors."""
    from rubik_core.insights.filters import FilterError
    from rubik_core.insights.person import PersonNotFound
    from rubik_core.insights.scope import ScopeError
    from rubik_core.insights.service import DashboardNotFound

    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except FilterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except DashboardNotFound:
        raise HTTPException(status_code=404, detail="No such dashboard.") from None
    except PersonNotFound:
        raise HTTPException(
            status_code=404, detail="That person is not in your view."
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("insights request failed")
        raise HTTPException(status_code=500, detail=_FAILED) from None


@router.get("/meta")
async def meta(user: UserProfile = Depends(require_insights)) -> dict:
    from rubik_core.insights import service

    return await _run(service.meta, user, can_view_people=_can_view_people(user))


@router.get("/dashboards/{slug}")
async def dashboard(
    slug: str,
    dept: str | None = Query(None),
    range: str | None = Query(None),  # noqa: A002 — the contract's name
    start: str | None = Query(None),
    end: str | None = Query(None),
    refresh: str | None = Query(None),
    user: UserProfile = Depends(require_insights),
) -> dict:
    from rubik_core.insights import service

    return await _run(
        service.dashboard,
        user,
        slug,
        dept=dept,
        range_key=range,
        start=start,
        end=end,
        refresh=refresh in ("1", "true", "yes"),
        can_view_people=_can_view_people(user),
    )


@router.get("/people/{email}")
async def person(
    email: str,
    request: Request,
    range: str | None = Query(None),  # noqa: A002 — the contract's name
    start: str | None = Query(None),
    end: str | None = Query(None),
    user: UserProfile = Depends(require_insights),
) -> dict:
    from rubik_core.insights import service

    if not _can_view_people(user):
        raise HTTPException(status_code=403, detail=_NO_PEOPLE)
    # 1. Is this a valid request for someone in view? (400 / 404, unaudited:
    #    nothing about a person is read.)
    target = await _run(
        service.person_target, user, email, range_key=range, start=start, end=end
    )
    # 2. Record the read BEFORE computing it; no record, no data.
    actor, extra = _audit_actor(request, user)
    details = {"range": range or "30d", **extra}
    try:
        await run_in_threadpool(_record_person_view, actor, target, details)
    except Exception:
        logger.exception("insights person-view audit write failed")
        raise HTTPException(status_code=503, detail=_AUDIT_FAILED) from None
    # 3. Only now, the profile.
    return await _run(
        service.person, user, target, range_key=range, start=start, end=end
    )


@router.post("/dashboards/{slug}/summary")
async def summary(
    slug: str,
    dept: str | None = Query(None),
    range: str | None = Query(None),  # noqa: A002 — the contract's name
    start: str | None = Query(None),
    end: str | None = Query(None),
    refresh: str | None = Query(None),
    user: UserProfile = Depends(require_insights),
) -> dict:
    from rubik_core.insights import service

    return await _run(
        service.summary,
        user,
        slug,
        dept=dept,
        range_key=range,
        start=start,
        end=end,
        refresh=refresh in ("1", "true", "yes"),
    )
