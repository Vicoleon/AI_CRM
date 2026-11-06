"""Meeting Prep builds in the background, and the reader is told when it's ready.

Building a brief takes 60-120 seconds (calendar, email, Drive, CRM and the
other signals, then one Pro-model call). Opening a meeting with no brief yet,
or pressing Regenerate, used to hold the page for all of that. Now the request
answers 202 at once, the brief is built elsewhere, and the reader gets an
in-app notification linking back to the meeting's prep page when it is ready
(or could not be built). The reader can stay and watch, or leave.

**The pending row IS the future version.** Starting a build inserts one
``meeting_prep_runs`` row: ``mode="sync"``, ``version`` NULL,
``change_kind="pending"``, ``delivery_status="queued"``, no brief, and
``context_bundle={"request": {...}}`` (what was asked: ``kind`` first |
regenerate, ``focus_on``, ``keep_edits``, ``requested_at``). The worker marks it
``running``, runs the same pipeline the request used to run, and FINALIZES
THAT SAME ROW as the next version (``versions.append_version(existing_row=…)``
— same numbering, retry, must-build-on-latest and keep-edits rules; its
``created_at`` becomes the moment it became a version; ``delivery_status``
``ready``). A failure marks it ``change_kind="failed"`` /
``delivery_status="failed"`` with ``version`` still NULL. Pending and failed
rows are invisible to the chain, exactly like the failed rows before them.

**One build per reader per meeting.** Asking again while a build is queued or
running returns that build (two tabs no longer make v1 and v2). A reader may
have at most ``CUBIE_PREP_MAX_INFLIGHT_PER_USER`` builds in flight across
meetings; one more is refused (:class:`TooManyBuilds`, HTTP 429).

**A build that never finishes is reported, not left spinning.** One still
queued/running after ``CUBIE_PREP_BACKGROUND_TTL_MINUTES`` — or still QUEUED
after ``CUBIE_PREP_QUEUED_TTL_MINUTES``: its worker never started — is marked
failed the next time anything looks at it: that meeting's page, its status, or
the reader's notifications feed, which every signed-in tab polls
(:func:`expire_stale_for_user`), so a reader who left is told too. If the
worker does finish after that, it still finalizes the version and notifies —
the worker wins over the clock — unless the reader has moved on (a newer
version, or another build of that meeting in flight) or it is a Cloud Run retry
of the build; then the row stays failed and nobody is told twice.

**Where it runs** (:func:`runner_mode`): on GCP an execution of the existing
``cubie-deep-prep`` Cloud Run Job running ``backend.jobs.meeting_prep_job`` — the
Service throttles CPU once the 202 is sent, which is what starved Deep Prep's
first version — and otherwise a task on the app's own event loop (never a
second loop: the process-wide genai client's async session is bound to the
loop that first used it). A Job launch that fails falls back to in-process,
loudly (the X-Ray Deep Research pattern).

Notifications are a courtesy, never a contract: inserting one never raises.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import urllib.parse
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.automation import (
    AutomationsPausedError,
    automations_enabled,
    require_automations_enabled,
)
from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import MeetingPrepRun, Notification
from rubik_core.meeting_prep import versions
from rubik_core.tenancy.threads import ContextThreadPoolExecutor

logger = logging.getLogger(__name__)

KIND_FIRST = "first"
KIND_REGENERATE = "regenerate"
BUILD_KINDS = (KIND_FIRST, KIND_REGENERATE)

#: ``delivery_status`` values of a build still in flight.
IN_FLIGHT = ("queued", "running")

# Reader-safe failure details: they reach the page and the notification, so
# plain words only — the raw error stays in the logs and ``partial_errors``.
TTL_DETAIL = f"{PRODUCT_NAME} took too long to build this briefing — try again."
GENERIC_FAILED_DETAIL = f"{PRODUCT_NAME} couldn't build this briefing — try again."
NOT_STARTED_DETAIL = (
    f"{PRODUCT_NAME} couldn't start building this briefing — try again."
)
UNSAVED_DETAIL = f"{PRODUCT_NAME} built this briefing but couldn't save it — try again."
NO_ACCOUNT_DETAIL = (
    f"{PRODUCT_NAME} couldn't load your account to build this briefing — try again."
)
EVENT_NOT_FOUND_DETAIL = (
    f"{PRODUCT_NAME} couldn't find this meeting on your calendar — it may have been "
    "moved or cancelled."
)
TOO_MANY_DETAIL = (
    "You have several briefings building already — wait for one to finish."
)

#: ``failed_by`` of a build the clock failed (still running past the TTL, or
#: still queued past the queued TTL) — a worker may yet finish it.
CLOCK_FAILURES = ("ttl", "queued_ttl")

NOTIFY_READY_KIND = "meeting_prep_ready"
NOTIFY_FAILED_KIND = "meeting_prep_failed"

# The keys of ``context_bundle["request"]`` that describe what was ASKED (the
# rest — ``detail``, ``failed_by``, ``finished_at`` — describe how it ended).
_REQUEST_KEYS = (
    "kind",
    "focus_on",
    "keep_edits",
    "requested_at",
    "product_to_emphasize",
)

# In-process builds run as tasks on the app's event loop (:func:`dispatch`);
# the loop holds only weak references to tasks, so these strong ones keep a
# build from being garbage-collected mid-flight. Discarded when it finishes.
_TASKS: set[asyncio.Task] = set()

# Only for a caller with no event loop at all (a script, a sync test): a few
# builds at once is plenty; more would only queue behind the model's limits.
_MAX_WORKERS = 4
_POOL = ContextThreadPoolExecutor(
    max_workers=_MAX_WORKERS, thread_name_prefix="meeting-prep"
)


class BuildNotStarted(Exception):
    """The build could not even be recorded or dispatched (the route answers
    502 with ``str(exc)``, which is reader-safe)."""


class TooManyBuilds(Exception):
    """The reader already has ``CUBIE_PREP_MAX_INFLIGHT_PER_USER`` builds in
    flight (the route answers 429 with ``str(exc)``, which is reader-safe)."""


# ─── Settings ─────────────────────────────────────────────────────────────────


def _int_setting(name: str, default: int) -> int:
    from rubik_core.config import settings

    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def ttl_minutes() -> int:
    """``CUBIE_PREP_BACKGROUND_TTL_MINUTES`` (default 20, never below 1)."""
    return _int_setting("CUBIE_PREP_BACKGROUND_TTL_MINUTES", 20)


def queued_ttl_minutes() -> int:
    """``CUBIE_PREP_QUEUED_TTL_MINUTES`` (default 5, never below 1)."""
    return _int_setting("CUBIE_PREP_QUEUED_TTL_MINUTES", 5)


def max_inflight_per_user() -> int:
    """``CUBIE_PREP_MAX_INFLIGHT_PER_USER`` (default 5, never below 1)."""
    return _int_setting("CUBIE_PREP_MAX_INFLIGHT_PER_USER", 5)


def runner_mode() -> str:
    """``"job"`` or ``"inprocess"`` — mirrors ``xray.config.deep_runner_mode``.

    ``CUBIE_PREP_RUNNER`` forces one; ``"auto"`` (default) picks the Job on
    GCP and the thread pool everywhere else, so local runs need no GCP."""
    from rubik_core.config import AppMode, settings

    mode = str(getattr(settings, "CUBIE_PREP_RUNNER", "auto") or "auto").lower()
    if mode in ("job", "inprocess"):
        return mode
    try:
        return "job" if settings.APP_MODE == AppMode.GCP else "inprocess"
    except Exception:  # noqa: BLE001 — config selection must never crash a build
        return "inprocess"


# ─── The request stored on the row ────────────────────────────────────────────


def request_of(row: MeetingPrepRun) -> dict[str, Any]:
    """A copy of ``row.context_bundle["request"]`` ({} for rows built in a
    request, before background builds existed)."""
    bundle = row.context_bundle if isinstance(row.context_bundle, dict) else {}
    req = bundle.get("request")
    return copy.deepcopy(req) if isinstance(req, dict) else {}


def _bundle_with_request(row: MeetingPrepRun, **updates: Any) -> dict[str, Any]:
    """``row.context_bundle`` with its request updated, as a NEW dict (the
    JSON column only notices reassignment)."""
    bundle = (
        copy.deepcopy(row.context_bundle)
        if isinstance(row.context_bundle, dict)
        else {}
    )
    req = bundle.get("request") if isinstance(bundle.get("request"), dict) else {}
    req.update(updates)
    bundle["request"] = req
    return bundle


def asked(row: MeetingPrepRun) -> dict[str, Any]:
    """Only what was asked for (kind, focus, keep_edits, when), for the
    ``request`` key of the finalized version's bundle."""
    req = request_of(row)
    return {k: req[k] for k in _REQUEST_KEYS if k in req}


# ─── State ────────────────────────────────────────────────────────────────────


def build_status(row: MeetingPrepRun) -> str:
    """queued | running | ready | failed, read off the row."""
    if row.version is not None:
        return "ready"
    if (
        row.change_kind == versions.PENDING_CHANGE_KIND
        and row.delivery_status in IN_FLIGHT
    ):
        return str(row.delivery_status)
    return "failed"


def _expiry(
    row: MeetingPrepRun, *, now: datetime | None = None
) -> tuple[str, str] | None:
    """``(detail, failed_by)`` when this in-flight build has outlived its clock,
    else ``None``: past the TTL whatever its state, or still QUEUED past the
    (shorter) queued TTL — a worker marks it running within seconds, so one
    still queued that long never started."""
    status = build_status(row)
    if status not in IN_FLIGHT:
        return None
    if row.created_at is None:
        return TTL_DETAIL, "ttl"
    now = now or versions.utcnow_naive()
    if row.created_at < now - timedelta(minutes=ttl_minutes()):
        return TTL_DETAIL, "ttl"
    if status == "queued" and row.created_at < now - timedelta(
        minutes=queued_ttl_minutes()
    ):
        return NOT_STARTED_DETAIL, "queued_ttl"
    return None


def is_expired(row: MeetingPrepRun, *, now: datetime | None = None) -> bool:
    """An in-flight build that outlived its clock (:func:`_expiry`)."""
    return _expiry(row, now=now) is not None


def _in_flight_query(db: Session, user_email: str):
    return db.query(MeetingPrepRun).filter(
        MeetingPrepRun.user_email == user_email,
        MeetingPrepRun.mode == "sync",
        MeetingPrepRun.version.is_(None),
        MeetingPrepRun.change_kind == versions.PENDING_CHANGE_KIND,
        MeetingPrepRun.delivery_status.in_(IN_FLIGHT),
    )


def _in_flight_rows(db: Session, user_email: str, event_id: str):
    return (
        _in_flight_query(db, user_email)
        .filter(MeetingPrepRun.event_id == event_id)
        .order_by(MeetingPrepRun.created_at.desc())
        .all()
    )


def find_in_flight(
    db: Session, user_email: str, event_id: str | None
) -> MeetingPrepRun | None:
    """The reader's live build for this meeting, or ``None``. Builds that
    outlived their clock are marked failed on the way (and are not returned)."""
    if not event_id:
        return None
    now = versions.utcnow_naive()
    live: MeetingPrepRun | None = None
    for row in _in_flight_rows(db, user_email, event_id):
        expiry = _expiry(row, now=now)
        if expiry is not None:
            mark_failed(db, row, detail=expiry[0], failed_by=expiry[1])
            continue
        if live is None:
            live = row
    return live


def expire_if_stale(db: Session, row: MeetingPrepRun) -> bool:
    """Lazily mark ``row`` failed if it is an in-flight build past its clock.
    True when this call made that change."""
    expiry = _expiry(row)
    if expiry is None:
        return False
    return mark_failed(db, row, detail=expiry[0], failed_by=expiry[1])


def expire_stale_for_user(db: Session, user_email: str | None) -> int:
    """Apply both clocks to every build this reader has in flight, on ANY
    meeting, and mark the ones that outlived them failed — each with its one
    "couldn't be built" notification (:func:`mark_failed` is a conditional
    UPDATE, so a racing caller never notifies twice).

    Called from ``GET /api/notifications/``, which every signed-in tab polls,
    so a build whose worker never started or died is reported wherever the
    reader is in Cubie, not only when they reopen that meeting. Returns how
    many it failed. Never raises: the feed must not break over this."""
    if not user_email:
        return 0
    now = versions.utcnow_naive()
    oldest_live = now - timedelta(minutes=min(ttl_minutes(), queued_ttl_minutes()))
    try:
        rows = (
            _in_flight_query(db, user_email)
            .filter(MeetingPrepRun.created_at < oldest_live)
            .all()
        )
    except Exception:  # noqa: BLE001 — a sweep must never break its caller
        logger.warning(
            "meeting_prep: in-flight build sweep failed for %s",
            user_email,
            exc_info=True,
        )
        db.rollback()
        return 0
    failed = 0
    for row in rows:
        expiry = _expiry(row, now=now)
        if expiry is not None and mark_failed(
            db, row, detail=expiry[0], failed_by=expiry[1]
        ):
            failed += 1
    return failed


def _in_flight_count(db: Session, user_email: str) -> int:
    """How many builds the reader has in flight, across meetings (the stale
    ones are failed first, so they never count)."""
    expire_stale_for_user(db, user_email)
    return _in_flight_query(db, user_email).count()


def mark_failed(
    db: Session,
    row: MeetingPrepRun,
    *,
    detail: str,
    failed_by: str,
    notify: bool = True,
    title: str | None = None,
) -> bool:
    """Move an IN-FLIGHT build to failed (``version`` stays NULL) and tell the
    reader. Never raises.

    The move is one conditional UPDATE (only from queued/running), so of two
    callers racing past the TTL exactly one wins and only the winner sends the
    notification. Returns True when this call made the move. ``title`` fills
    the row's title when it had none (a first build learns it from the
    calendar)."""
    try:
        now = versions.utcnow_naive()
        values: dict[Any, Any] = {
            MeetingPrepRun.delivery_status: "failed",
            MeetingPrepRun.change_kind: "failed",
            MeetingPrepRun.context_bundle: _bundle_with_request(
                row,
                detail=detail,
                failed_by=failed_by,
                finished_at=versions.iso(now),
            ),
            MeetingPrepRun.partial_errors: [*list(row.partial_errors or []), detail],
        }
        if title and not row.title:
            values[MeetingPrepRun.title] = title
        moved = (
            db.query(MeetingPrepRun)
            .filter(
                MeetingPrepRun.id == row.id,
                MeetingPrepRun.version.is_(None),
                MeetingPrepRun.delivery_status.in_(IN_FLIGHT),
            )
            .update(values, synchronize_session=False)
        )
        db.commit()
    except Exception:  # noqa: BLE001 — a failure report must not become a second failure
        logger.exception("meeting_prep build %s: could not record the failure", row.id)
        db.rollback()
        return False
    try:
        db.refresh(row)
    except Exception:  # noqa: BLE001
        logger.debug("meeting_prep build %s: refresh failed", row.id, exc_info=True)
    if moved and notify:
        notify_failed(
            row.user_email,
            event_id=row.event_id,
            meeting_title=row.title or title,
            detail=detail,
        )
    return bool(moved)


def _settled(db: Session, row: MeetingPrepRun) -> bool:
    """True when ``row``, read fresh, is no longer in flight (ready or failed)
    — False when it is still queued/running or could not be read."""
    try:
        db.refresh(row)
    except Exception:  # noqa: BLE001
        logger.warning(
            "meeting_prep build %s: could not re-read the run", row.id, exc_info=True
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False
    return build_status(row) not in IN_FLIGHT


def _mark_failed_by_id(
    run_id: str, *, detail: str, failed_by: str, title: str | None = None
) -> bool:
    """:func:`mark_failed` on a fresh session — for when the worker's own
    session is the thing that broke. True when the row is SETTLED afterwards
    (this call failed it, or it had already finished); False when the
    database could not be reached or the row is gone — the build is then
    unrecorded and the Job exits non-zero so Cloud Run retries it."""
    db = SessionLocal()
    try:
        row = db.get(MeetingPrepRun, run_id)
        if row is None:
            return False
        mark_failed(db, row, detail=detail, failed_by=failed_by, title=title)
        return _settled(db, row)
    except Exception:  # noqa: BLE001
        logger.exception("meeting_prep build %s: could not record the failure", run_id)
        return False
    finally:
        db.close()


# ─── API payloads (shapes in backend/routers/home.py) ─────────────────────────


def _started_at(row: MeetingPrepRun) -> str | None:
    return request_of(row).get("requested_at") or versions.iso(row.created_at)


def status_payload(row: MeetingPrepRun) -> dict[str, Any]:
    """``GET /prep-runs/{run_id}/status``."""
    req = request_of(row)
    status = build_status(row)
    if status == "ready":
        finished_at: str | None = versions.iso(row.created_at)
    elif status == "failed":
        finished_at = req.get("finished_at")
    else:
        finished_at = None
    return {
        "run_id": row.id,
        "event_id": row.event_id,
        "status": status,
        "kind": req.get("kind"),
        "version": row.version,
        "detail": (req.get("detail") or GENERIC_FAILED_DETAIL)
        if status == "failed"
        else None,
        "started_at": _started_at(row),
        "finished_at": finished_at,
    }


def pending_payload(row: MeetingPrepRun) -> dict[str, Any]:
    """The ``pending_run`` object on the page and the version list."""
    return {
        "run_id": row.id,
        "status": build_status(row),
        "kind": request_of(row).get("kind"),
        "started_at": _started_at(row),
    }


def accepted_payload(row: MeetingPrepRun) -> dict[str, Any]:
    """The 202 body when a build was started (or joined)."""
    return {
        "status": build_status(row),
        "run_id": row.id,
        "event_id": row.event_id,
        "kind": request_of(row).get("kind"),
        "started_at": _started_at(row),
        "title": row.title,
    }


def pending_run(
    db: Session, user_email: str, event_id: str | None
) -> dict[str, Any] | None:
    """The reader's live build for this meeting as ``pending_run``, or None."""
    row = find_in_flight(db, user_email, event_id)
    return pending_payload(row) if row is not None else None


# ─── Starting a build ─────────────────────────────────────────────────────────


def request_build(
    db: Session,
    *,
    user_email: str,
    event_id: str,
    kind: str,
    focus_on: str | None = None,
    keep_edits: bool = True,
    title: str | None = None,
    product_to_emphasize: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> tuple[MeetingPrepRun, bool]:
    """Start the reader's background build of this meeting's brief, or join
    the one already in flight. Returns ``(row, started)``; ``started`` False
    means the live build was returned and nothing new was dispatched.

    Blocking (database writes, and on GCP the Cloud Run Admin call), so the
    route runs it on a worker thread and passes its event ``loop``: an
    in-process build is scheduled there (:func:`dispatch`).

    Raises :class:`TooManyBuilds` when the reader already has
    ``CUBIE_PREP_MAX_INFLIGHT_PER_USER`` builds in flight on other meetings
    (nothing is written), and :class:`BuildNotStarted` when the row could not
    be written or the build could not be dispatched (the row is then marked
    failed, without a notification: the reader is looking at the error)."""
    require_automations_enabled()
    if kind not in BUILD_KINDS:
        raise ValueError(f"Not a build kind: {kind!r}")
    try:
        live = find_in_flight(db, user_email, event_id)
        if live is not None:
            return live, False
        if _in_flight_count(db, user_email) >= max_inflight_per_user():
            raise TooManyBuilds(TOO_MANY_DETAIL)
        now = versions.utcnow_naive()
        req: dict[str, Any] = {
            "kind": kind,
            "focus_on": focus_on or None,
            "keep_edits": bool(keep_edits),
            "requested_at": versions.iso(now),
        }
        if product_to_emphasize:
            req["product_to_emphasize"] = product_to_emphasize
        row = MeetingPrepRun(
            id=uuid.uuid4().hex,
            created_at=now,
            user_email=user_email,
            event_id=event_id,
            title=title,
            mode="sync",
            version=None,
            change_kind=versions.PENDING_CHANGE_KIND,
            delivery_status="queued",
            response=None,
            partial_errors=[],
            context_bundle={"request": req},
        )
        db.add(row)
        db.commit()
    except TooManyBuilds:
        raise
    except Exception as exc:
        db.rollback()
        logger.exception(
            "meeting_prep build for %s/%s: could not record it", user_email, event_id
        )
        raise BuildNotStarted(NOT_STARTED_DETAIL) from exc

    try:
        how = dispatch(
            run_id=row.id,
            user_email=user_email,
            event_id=event_id,
            focus_on=req["focus_on"],
            keep_edits=req["keep_edits"],
            product_to_emphasize=product_to_emphasize,
            loop=loop,
        )
    except Exception as exc:
        logger.exception("meeting_prep build %s: could not be dispatched", row.id)
        mark_failed(
            db, row, detail=NOT_STARTED_DETAIL, failed_by="dispatch", notify=False
        )
        raise BuildNotStarted(NOT_STARTED_DETAIL) from exc
    logger.info(
        "meeting_prep build %s (%s) for %s/%s dispatched: %s",
        row.id,
        kind,
        user_email,
        event_id,
        how,
    )
    return row, True


def dispatch(
    *,
    run_id: str,
    user_email: str,
    event_id: str,
    focus_on: str | None,
    keep_edits: bool,
    product_to_emphasize: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> str:
    """Run the build where :func:`runner_mode` says. Returns ``"job"`` or
    ``"inprocess"``. A Job launch that fails falls back to in-process — on
    Cloud Run that can be starved, but a slow brief beats none; the TTL
    reports one that never lands.

    In-process means a task on the APP'S event loop: ``loop`` (the route's,
    when this runs on a worker thread) or the loop running this call. Never
    ``asyncio.run`` on another thread while the app's loop is alive: the
    process-wide genai client (``rubik_core.models.get_genai_client``) keeps
    one aiohttp session, bound to the loop that first used it, so a build on
    a second loop fails its model call ("attached to a different loop") and
    breaks the app loop's own calls while it holds the session. The thread
    pool is only for a caller with no loop at all (a script, a sync test):
    there nothing else drives the client, so the build's own loop is safe."""
    require_automations_enabled()
    if runner_mode() == "job":
        try:
            from backend.routers.home import _launch_meeting_prep_job_execution

            _launch_meeting_prep_job_execution(
                run_id=run_id,
                user_email=user_email,
                event_id=event_id,
                focus_on=focus_on,
                keep_edits=keep_edits,
                product_to_emphasize=product_to_emphasize,
            )
            return "job"
        except AutomationsPausedError:
            raise
        except Exception:  # noqa: BLE001 — degrade to in-process, never fail the request
            logger.exception(
                "meeting_prep build %s: Cloud Run Job launch failed — falling back "
                "to in-process (the 'cubie-deep-prep' Cloud Run Job runs these builds; "
                "check it exists and the Service may run it)",
                run_id,
            )
    build = {
        "run_id": run_id,
        "user_email": user_email,
        "event_id": event_id,
        "focus_on": focus_on,
        "keep_edits": keep_edits,
        "product_to_emphasize": product_to_emphasize,
    }
    app_loop = loop if loop is not None else _running_loop()
    if app_loop is not None and app_loop.is_running():
        _schedule_on_loop(app_loop, build)
        return "inprocess"
    _POOL.submit(
        _run_in_thread,
        run_id,
        user_email,
        event_id,
        focus_on,
        keep_edits,
        product_to_emphasize,
    )
    return "inprocess"


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _schedule_on_loop(loop: asyncio.AbstractEventLoop, build: dict[str, Any]) -> None:
    """Start :func:`run_build` as a task on ``loop``, holding a strong
    reference until it is done. Thread-safe: from a worker thread the task is
    created by the loop itself (``call_soon_threadsafe``)."""

    def spawn() -> None:
        task = loop.create_task(
            run_build(**build), name=f"meeting-prep-{build['run_id']}"
        )
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)

    if _running_loop() is loop:
        spawn()
    else:
        loop.call_soon_threadsafe(spawn)


def _run_in_thread(
    run_id: str,
    user_email: str,
    event_id: str,
    focus_on: str | None,
    keep_edits: bool,
    product_to_emphasize: str | None,
) -> None:
    """Thread-pool entry — never lets an exception escape the worker."""
    run_build_sync(
        run_id=run_id,
        user_email=user_email,
        event_id=event_id,
        focus_on=focus_on,
        keep_edits=keep_edits,
        product_to_emphasize=product_to_emphasize,
    )


# ─── The worker ───────────────────────────────────────────────────────────────


def run_build_sync(**kwargs: Any) -> str:
    """Sync entry for the Cloud Run Job (a process of its own) and the thread
    pool (only used with no app loop, see :func:`dispatch`) — each runs the
    build on a fresh loop, which is safe only because nothing else in that
    process drives the shared genai client. Never raises."""
    try:
        return asyncio.run(run_build(**kwargs))
    except Exception:  # noqa: BLE001 — run_build already recorded the failure
        logger.exception("meeting_prep build %s crashed", kwargs.get("run_id"))
        return "failed"


def _task_attempt() -> int:
    """Cloud Run's 0-based retry index of this Job task (0 outside a Job)."""
    try:
        return int(os.environ.get("CLOUD_RUN_TASK_ATTEMPT") or 0)
    except ValueError:
        return 0


def _failed_by_clock(row: MeetingPrepRun) -> bool:
    return (
        row.change_kind == "failed"
        and request_of(row).get("failed_by") in CLOCK_FAILURES
    )


def _superseded(db: Session, row: MeetingPrepRun) -> bool:
    """The reader moved on after this build was asked for: a version of this
    meeting landed since (``created_at`` of a pending row is when it was
    asked), or another build of it is in flight."""
    newer = db.query(MeetingPrepRun.id).filter(
        MeetingPrepRun.user_email == row.user_email,
        MeetingPrepRun.event_id == row.event_id,
        MeetingPrepRun.version.isnot(None),
        MeetingPrepRun.id != row.id,
    )
    if row.created_at is not None:
        newer = newer.filter(MeetingPrepRun.created_at > row.created_at)
    if newer.first() is not None:
        return True
    return any(
        other.id != row.id and not is_expired(other)
        for other in _in_flight_rows(db, row.user_email, row.event_id)
    )


def _may_finish_late(db: Session, row: MeetingPrepRun) -> bool:
    """Whether a build already reported failed (``row`` read fresh) may still
    become the next version: never on a Cloud Run retry of a build the clock
    failed (only the original attempt may finish late), and never once the
    reader has moved on — a surprise version on top of the one they asked
    for next would replace it as the latest."""
    if _failed_by_clock(row) and _task_attempt() > 0:
        return False
    return not _superseded(db, row)


def _fail(db: Session, row: MeetingPrepRun, *, detail: str) -> str:
    """Mark the build failed by the worker: ``failed`` once the row is settled,
    ``unrecorded`` when it could not be (the Job then exits non-zero)."""
    mark_failed(db, row, detail=detail, failed_by="worker")
    return "failed" if _settled(db, row) else "unrecorded"


async def run_build(
    *,
    run_id: str,
    user_email: str,
    event_id: str,
    focus_on: str | None = None,
    keep_edits: bool = True,
    product_to_emphasize: str | None = None,
) -> str:
    """Build the brief for one pending row and finalize it. Returns the
    outcome: ``ready`` | ``failed`` | ``missing`` | ``skipped`` |
    ``unrecorded`` (the row could not be settled — the database failed).

    Idempotent: a row that is already a version (or already failed by an
    earlier attempt) is left alone; a ``running`` row from an attempt that
    crashed is built again (the Job retries once). Every way out of here
    leaves the row ready or failed — never running — and tells the reader,
    except ``unrecorded``/``missing``, where the Job exits non-zero."""
    if not automations_enabled():
        return "skipped"
    db = SessionLocal()
    try:
        return await _run_build(
            db,
            run_id=run_id,
            user_email=user_email,
            event_id=event_id,
            focus_on=focus_on,
            keep_edits=keep_edits,
            product_to_emphasize=product_to_emphasize,
        )
    except BaseException as exc:
        logger.exception("meeting_prep build %s failed", run_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        settled = _mark_failed_by_id(
            run_id, detail=GENERIC_FAILED_DETAIL, failed_by="worker"
        )
        if not isinstance(exc, Exception):
            raise  # cancellation / exit: recorded, then let it through
        return "failed" if settled else "unrecorded"
    finally:
        db.close()


async def _run_build(
    db: Session,
    *,
    run_id: str,
    user_email: str,
    event_id: str,
    focus_on: str | None,
    keep_edits: bool,
    product_to_emphasize: str | None,
) -> str:
    from fastapi import HTTPException

    from backend.routers import home
    from rubik_core.database.repository import SQLUserRegistry

    row = db.get(MeetingPrepRun, run_id)
    if row is None:
        logger.error("meeting_prep build %s: no such run", run_id)
        return "missing"
    owner = row.user_email or ""
    if (
        row.mode != "sync"
        or owner.strip().lower() != (user_email or "").strip().lower()
        or row.event_id != event_id
    ):
        logger.error(
            "meeting_prep build %s: the run does not match its arguments "
            "(user %s, event %s); not building",
            run_id,
            user_email,
            event_id,
        )
        return "skipped"
    if row.version is not None or row.delivery_status == "ready":
        logger.info("meeting_prep build %s: already v%s", run_id, row.version)
        return "ready"
    if row.change_kind == versions.PENDING_CHANGE_KIND:
        # Conditional, like mark_failed: a clock that failed the row a moment
        # ago must not be flipped back to running (a second failure would
        # then notify the reader twice).
        db.query(MeetingPrepRun).filter(
            MeetingPrepRun.id == row.id,
            MeetingPrepRun.version.is_(None),
            MeetingPrepRun.change_kind == versions.PENDING_CHANGE_KIND,
            MeetingPrepRun.delivery_status.in_(IN_FLIGHT),
        ).update({MeetingPrepRun.delivery_status: "running"}, synchronize_session=False)
        db.commit()
        db.refresh(row)
    if row.change_kind != versions.PENDING_CHANGE_KIND:
        # A build the clock already reported failed still runs (the worker
        # wins if it finishes) — but not a Cloud Run retry of it, and not once
        # the reader has moved on. Anything else finished (failed) by an
        # earlier attempt, or is not a background build.
        if not _failed_by_clock(row) or not _may_finish_late(db, row):
            logger.info(
                "meeting_prep build %s: already %s; nothing to do",
                run_id,
                row.delivery_status or row.change_kind,
            )
            return "failed" if row.change_kind == "failed" else "skipped"

    reader = SQLUserRegistry(db).get_user(owner)
    if reader is None:
        logger.error("meeting_prep build %s: unknown user %s", run_id, owner)
        return _fail(db, row, detail=NO_ACCOUNT_DETAIL)

    try:
        ctx = await home.collect_meeting_prep_context(
            user_email=owner,
            user_department=reader.department,
            user_roles=reader.roles,
            event_id=event_id,
            focus_on=focus_on,
            product_to_emphasize=product_to_emphasize,
            reader=reader,
        )
    except HTTPException as exc:
        logger.warning(
            "meeting_prep build %s: gathering sources failed (%s)",
            run_id,
            exc.status_code,
        )
        detail = (
            EVENT_NOT_FOUND_DETAIL if exc.status_code == 404 else GENERIC_FAILED_DETAIL
        )
        return _fail(db, row, detail=detail)
    meeting_title = ctx.get("title") or row.title

    try:
        personalization = home.build_user_context_block(reader, db)
    except Exception:  # noqa: BLE001 — personalization is a nicety, not a source
        logger.warning(
            "meeting_prep build %s: no personalization", run_id, exc_info=True
        )
        db.rollback()
        personalization = ""
    try:
        from rubik_core.feedback.wgll import build_wgll_context

        wgll = build_wgll_context(db, surface="meeting_prep", user_email=owner)
    except Exception:  # noqa: BLE001 — WGLL is best-effort
        logger.warning("meeting_prep build %s: no WGLL context", run_id, exc_info=True)
        db.rollback()
        wgll = ""

    from rubik_core.meeting_prep.provenance import context_visible

    if not await asyncio.to_thread(context_visible, ctx):
        return _fail(db, row, detail=GENERIC_FAILED_DETAIL)
    resp = await home.generate_meeting_prep_analysis(ctx, owner, personalization, wgll)

    request = asked(row)
    request["finished_at"] = versions.iso(versions.utcnow_naive())

    if resp.generation_failed:
        has_current = versions.latest_version_row(db, owner, event_id) is not None
        detail = home._prep_failure_detail(resp, has_current_version=has_current)
        resp.partial_errors = [*(resp.partial_errors or []), detail]
        request.update(detail=detail, failed_by="worker")
        db.refresh(row)
        if row.version is not None or row.change_kind not in (
            versions.PENDING_CHANGE_KIND,
            "failed",
        ):
            # Another attempt (a duplicate launch) finished it meanwhile:
            # never overwrite a version with this failure.
            logger.info(
                "meeting_prep build %s: generation failed, but the run is "
                "already %s; leaving it",
                run_id,
                f"v{row.version}" if row.version is not None else row.change_kind,
            )
            return "ready" if row.version is not None else "skipped"
        was_in_flight = (
            row.change_kind == versions.PENDING_CHANGE_KIND
            and row.delivery_status in IN_FLIGHT
        )
        saved = home._persist_meeting_prep_run(
            db,
            owner,
            ctx,
            resp,
            event_id=event_id,
            keep_edits=keep_edits,
            pending_row=row,
            request=request,
        )
        if saved is None:
            # Could not even store the failure with its evidence: record the
            # bare failure so the row is never left running.
            if not _mark_failed_by_id(
                run_id, detail=detail, failed_by="worker", title=meeting_title
            ):
                return "unrecorded"
        elif was_in_flight:
            notify_failed(
                owner,
                event_id=event_id,
                meeting_title=saved.title or meeting_title,
                detail=detail,
            )
        logger.info("meeting_prep build %s: generation failed", run_id)
        return "failed"

    db.refresh(row)
    if row.version is not None:
        logger.info(
            "meeting_prep build %s: another attempt already made it v%s",
            run_id,
            row.version,
        )
        return "ready"
    if row.change_kind != versions.PENDING_CHANGE_KIND and not _may_finish_late(
        db, row
    ):
        # Reported failed while it built (the clock, or a duplicate attempt),
        # and the reader has moved on or this is a retry: it stays failed and
        # the reader, already told, is not told again.
        logger.info(
            "meeting_prep build %s: finished after it was reported failed; "
            "superseded, left failed",
            run_id,
        )
        return "failed"

    saved = home._persist_meeting_prep_run(
        db,
        owner,
        ctx,
        resp,
        event_id=event_id,
        keep_edits=keep_edits,
        pending_row=row,
        request=request,
    )
    if saved is None:
        settled = _mark_failed_by_id(
            run_id, detail=UNSAVED_DETAIL, failed_by="worker", title=meeting_title
        )
        return "failed" if settled else "unrecorded"
    notify_ready(
        owner,
        event_id=event_id,
        meeting_title=saved.title or meeting_title,
        version=saved.version,
    )
    logger.info(
        "meeting_prep build %s: ready as v%s for %s/%s",
        run_id,
        saved.version,
        owner,
        event_id,
    )
    return "ready"


# ─── Notifications ────────────────────────────────────────────────────────────


def prep_link(event_id: str | None) -> str:
    """The prep page's in-app path (``encodeURIComponent`` of the event id,
    like every link into the page)."""
    return f"/meeting-prep/{urllib.parse.quote(event_id or '', safe='')}"


def _meeting_label(meeting_title: str | None) -> str:
    text = (meeting_title or "").strip()
    return text or "Your meeting"


def notify(
    recipient: str,
    *,
    kind: str,
    title: str,
    body: str,
    severity: str,
    link: str | None,
) -> bool:
    """Insert one in-app notification on its own session. Never raises."""
    if not recipient:
        return False
    db = SessionLocal()
    try:
        db.add(
            Notification(
                id=str(uuid.uuid4()),
                actor_email=recipient,
                kind=kind,
                title=title,
                body=body,
                severity=severity,
                link=link,
                created_at=datetime.utcnow(),
            )
        )
        db.commit()
        return True
    except Exception:  # noqa: BLE001 — a notification is a courtesy, not a contract
        db.rollback()
        logger.exception("meeting_prep: notification insert failed for %s", recipient)
        return False
    finally:
        db.close()


def notify_ready(
    recipient: str,
    *,
    event_id: str | None,
    meeting_title: str | None,
    version: int | None,
) -> bool:
    return notify(
        recipient,
        kind=NOTIFY_READY_KIND,
        title="Your meeting prep is ready",
        body=f"{_meeting_label(meeting_title)} — v{version} is ready to review.",
        severity="info",
        link=prep_link(event_id),
    )


def notify_failed(
    recipient: str,
    *,
    event_id: str | None,
    meeting_title: str | None,
    detail: str,
) -> bool:
    return notify(
        recipient,
        kind=NOTIFY_FAILED_KIND,
        title="Meeting prep couldn't be built",
        body=f"{_meeting_label(meeting_title)}: {detail}",
        severity="warn",
        link=prep_link(event_id),
    )
