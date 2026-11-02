"""Debounce/coalesce state machine for event-driven EOS re-ingestion.

The EOS edit hooks (``backend/jobs/eos_ingest_hooks.py``) fire on every
Issue / VTO section / Process mutation, and each run re-ingests EVERY doc of
that kind. Routing them naively through the ``cubie-worker`` Cloud Run Job
(#9 pattern) would spawn one full-kind re-ingest execution per edit — and
edits arrive in bursts during a live L10. Keeping them as FastAPI
BackgroundTasks is worse: Cloud Run throttles the Service's CPU after the
response, silently starving the ingest (the #9 bug class), and the weekly
full-sweep backstop cron is NOT currently scheduled, so these hooks are the
primary freshness path for EOS content in RAG.

One ``eos_ingest_state`` row per kind implements the coalescing:

- every edit bumps ``requested_at`` (the request watermark);
- the first edit to find no live claim flips ``status`` idle→launching via a
  single-statement compare-and-swap and dispatches ONE worker execution;
- the worker ingests, then releases the claim only if no NEWER request
  arrived while it ran (CAS on ``requested_at``) — otherwise it loops. A
  burst of N edits therefore coalesces into one execution and a handful of
  ingest passes, and the pass that releases always STARTED after the last
  edit, so the newest data always lands.

Crash safety: a claim older than ``EOS_REINGEST_CLAIM_STALE_SECONDS`` is
treated as dead (worker crashed / launch lost) and is reclaimable by the next
edit. Overlapping workers in the pathological case are wasteful but safe —
re-ingest is delete-then-import, so repeated runs replace rather than
duplicate.

Layering: this module lives in ``rubik_core`` and never imports ``backend``;
the Cloud Run Jobs launcher glue lives in ``backend/jobs/eos_ingest_hooks.py``
and the executor entrypoint in ``backend/jobs/eos_reingest_job.py``.

Every helper opens its own short-lived session (run_state.py idiom): callers
are request hooks and background executors whose request-scoped sessions, if
any, are long gone.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rubik_core.config import settings
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import EosIngestState

logger = logging.getLogger(__name__)

KIND_VTO = "vto"
KIND_ISSUES = "issues"
KIND_PROCESSES = "processes"
# Kinds whose hooks re-ingest the FULL kind on every edit and therefore
# coalesce through this state machine. L10 session ingest is per-session
# (one shot at meeting end) and dispatches directly — no row here.
COALESCED_KINDS = (KIND_VTO, KIND_ISSUES, KIND_PROCESSES)

STATUS_IDLE = "idle"
STATUS_LAUNCHING = "launching"
STATUS_RUNNING = "running"

EXECUTOR_JOB = "job"
EXECUTOR_INLINE = "inline"

_PIPELINE_FN_BY_KIND = {
    KIND_VTO: "ingest_vto",
    KIND_ISSUES: "ingest_issues",
    KIND_PROCESSES: "ingest_processes",
}

# Back-to-back ingest passes allowed inside ONE worker execution before it
# chains a fresh execution (job mode) or gives up (inline mode). Bounds a
# single execution's runtime well under cubie-worker's 3600s task timeout
# even when a long meeting keeps edits flowing.
MAX_PASSES_PER_EXECUTION = 5

# How often the quiet-wait re-polls the watermark. The sleep runs on the
# CPU-allocated Job task (never on the throttled Service).
QUIET_POLL_SECONDS = 10


def _utcnow() -> datetime:
    return datetime.utcnow()


def _ensure_row(db: Session, kind: str) -> None:
    """Get-or-create the kind's singleton row, tolerating a concurrent creator."""
    if db.get(EosIngestState, kind) is not None:
        return
    try:
        db.add(EosIngestState(kind=kind, status=STATUS_IDLE))
        db.commit()
    except IntegrityError:
        db.rollback()  # another request/worker created it first — fine


def request_reingest(kind: str) -> bool:
    """Record an edit-driven re-ingest request; return True when the caller
    won the dispatch claim (and must launch a worker).

    False means a live claim exists — a launched/running worker whose
    end-of-run watermark check will pick this request up, so the edit is
    coalesced and nothing needs launching. A claim older than
    ``EOS_REINGEST_CLAIM_STALE_SECONDS`` is treated as dead and reclaimed.
    """
    if kind not in COALESCED_KINDS:
        raise ValueError(f"not a coalesced ingest kind: {kind!r}")
    now = _utcnow()
    stale_cutoff = now - timedelta(seconds=settings.EOS_REINGEST_CLAIM_STALE_SECONDS)
    with SessionLocal() as db:
        _ensure_row(db, kind)
        # Bump the watermark unconditionally: whoever ends up running next
        # must ingest data at least as new as this edit.
        db.query(EosIngestState).filter(EosIngestState.kind == kind).update(
            {"requested_at": now}, synchronize_session=False
        )
        db.commit()
        # Single-statement CAS: only one concurrent edit can move the row
        # out of idle (or off a stale claim) — everyone else coalesces.
        claimed = (
            db.query(EosIngestState)
            .filter(EosIngestState.kind == kind)
            .filter(
                or_(
                    EosIngestState.status == STATUS_IDLE,
                    EosIngestState.claimed_at.is_(None),
                    EosIngestState.claimed_at < stale_cutoff,
                )
            )
            .update(
                {
                    "status": STATUS_LAUNCHING,
                    "claimed_at": now,
                    "executor": None,
                    "execution_name": None,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return claimed == 1


def record_dispatch(kind: str, executor: str, execution_name: str | None) -> None:
    """Record which executor picked the claim up (debugging aid). Never raises."""
    try:
        with SessionLocal() as db:
            db.query(EosIngestState).filter(EosIngestState.kind == kind).update(
                {"executor": executor, "execution_name": execution_name},
                synchronize_session=False,
            )
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[eos-ingest][debounce:%s] record_dispatch failed", kind)


def force_idle(kind: str, note: str) -> None:
    """Release the claim unconditionally (dispatch failed / passes exhausted
    with no relaunch). The pending ``requested_at``, if any, stays put — the
    NEXT edit re-claims and re-dispatches. Never raises."""
    try:
        with SessionLocal() as db:
            db.query(EosIngestState).filter(EosIngestState.kind == kind).update(
                {
                    "status": STATUS_IDLE,
                    "last_finished_at": _utcnow(),
                    "last_result": note,
                },
                synchronize_session=False,
            )
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[eos-ingest][debounce:%s] force_idle failed", kind)


# ─── Executor side (cubie-worker job / inline fallback) ─────────────────


def acquire_for_run(kind: str) -> None:
    """Executor takeover: flip the row to ``running`` and re-stamp the claim.

    Unconditional — the executor is authoritative once it starts. This also
    covers a Cloud Run task retry (``--max-retries=1``) re-entering after a
    crashed first attempt, and a manual ``gcloud run jobs execute`` with
    ``--kind`` overrides (the row is created on the fly)."""
    values = {
        "status": STATUS_RUNNING,
        "claimed_at": _utcnow(),
        "last_started_at": _utcnow(),
    }
    execution = os.environ.get("CLOUD_RUN_EXECUTION")
    if execution:
        values["executor"] = EXECUTOR_JOB
        values["execution_name"] = execution
    with SessionLocal() as db:
        _ensure_row(db, kind)
        db.query(EosIngestState).filter(EosIngestState.kind == kind).update(
            values, synchronize_session=False
        )
        db.commit()


def read_watermark(kind: str) -> datetime | None:
    with SessionLocal() as db:
        row = db.get(EosIngestState, kind)
        return row.requested_at if row else None


def wait_until_quiet(
    kind: str,
    *,
    quiet_seconds: int,
    max_wait_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Block until the newest request is at least ``quiet_seconds`` old.

    Edits arrive in bursts (a live L10 creates issues every couple of
    minutes), and every pass re-ingests the FULL kind — waiting for a lull
    turns a burst into one pass instead of one per edit. ``max_wait_seconds``
    caps the wait so constant editing can never starve ingestion entirely.
    Runs on the CPU-allocated Job task, so sleeping here is safe and cheap.
    """
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        requested_at = read_watermark(kind)
        if requested_at is None:
            return  # manual run with nothing pending — go straight to work
        age = (_utcnow() - requested_at).total_seconds()
        if age >= quiet_seconds:
            return
        remaining_quiet = quiet_seconds - age
        sleep(max(1.0, min(QUIET_POLL_SECONDS, remaining_quiet)))


def release_if_no_newer_request(
    kind: str, watermark: datetime | None, *, result_summary: str
) -> bool:
    """CAS release: back to ``idle`` iff no request arrived after ``watermark``
    (the value read just before this pass extracted). False → a newer edit
    landed mid-pass and the caller must run another pass."""
    if watermark is None:
        newer_guard = EosIngestState.requested_at.is_(None)
    else:
        newer_guard = or_(
            EosIngestState.requested_at.is_(None),
            EosIngestState.requested_at <= watermark,
        )
    with SessionLocal() as db:
        released = (
            db.query(EosIngestState)
            .filter(EosIngestState.kind == kind)
            .filter(newer_guard)
            .update(
                {
                    "status": STATUS_IDLE,
                    "last_finished_at": _utcnow(),
                    "last_result": result_summary,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return released == 1


def mark_relaunching(kind: str) -> None:
    """Keep the claim across a chained execution (passes exhausted, requests
    still arriving): back to ``launching`` with a fresh claim stamp."""
    with SessionLocal() as db:
        db.query(EosIngestState).filter(EosIngestState.kind == kind).update(
            {
                "status": STATUS_LAUNCHING,
                "claimed_at": _utcnow(),
                "execution_name": None,
            },
            synchronize_session=False,
        )
        db.commit()


def _run_ingest_pass(kind: str) -> str:
    """One full-kind ingest on a fresh session; returns a short summary.

    Never raises: a hard failure (extract/DB/Vertex-init) is logged loudly
    and reported in the summary — same log-and-swallow contract the old
    BackgroundTask hooks had. Per-doc failures are already captured inside
    ``IngestResult`` without raising.
    """
    from rubik_core.ingestion.eos import pipeline

    session = SessionLocal()
    try:
        fn = getattr(pipeline, _PIPELINE_FN_BY_KIND[kind])
        result = fn(session)
        summary = result.summary()
        logger.info("[eos-ingest][worker:%s] %s", kind, summary)
        return summary
    except Exception as e:  # noqa: BLE001 — report, don't crash the loop
        logger.exception("[eos-ingest][worker:%s] ingest pass failed", kind)
        return f"failed: {type(e).__name__}: {e}"
    finally:
        session.close()


def run_debounced_reingest(
    kind: str,
    *,
    wait_for_quiet: bool = True,
    relaunch: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Executor body for one coalesced kind; returns a short outcome string.

    Loop: (optionally) wait for the edit burst to go quiet, snapshot the
    watermark, ingest the full kind, then CAS-release — a release that fails
    means a newer edit arrived mid-pass, so loop again. After
    ``MAX_PASSES_PER_EXECUTION`` passes with requests still arriving, chain a
    fresh execution via ``relaunch`` (job mode) or release with the request
    left pending (inline mode — the next edit re-claims).

    ``wait_for_quiet=False`` is for the inline BackgroundTask fallback, which
    must not sleep for minutes on the throttled service.

    A failed pass still releases: the failure is logged + recorded on the row,
    and freshness self-heals on the next edit (delete-then-import re-ingest is
    idempotent). This matches the old hooks' best-effort contract instead of
    retry-looping against a broken Vertex.
    """
    if kind not in COALESCED_KINDS:
        raise ValueError(f"not a coalesced ingest kind: {kind!r}")

    acquire_for_run(kind)
    summary = "no pass ran"
    for _ in range(MAX_PASSES_PER_EXECUTION):
        if wait_for_quiet:
            wait_until_quiet(
                kind,
                quiet_seconds=settings.EOS_REINGEST_DEBOUNCE_SECONDS,
                max_wait_seconds=settings.EOS_REINGEST_DEBOUNCE_MAX_WAIT_SECONDS,
                sleep=sleep,
            )
        watermark = read_watermark(kind)
        summary = _run_ingest_pass(kind)
        if release_if_no_newer_request(kind, watermark, result_summary=summary):
            return summary
        logger.info(
            "[eos-ingest][worker:%s] new request arrived mid-pass — running again",
            kind,
        )

    if relaunch is not None:
        try:
            mark_relaunching(kind)
            relaunch()
            logger.info(
                "[eos-ingest][worker:%s] passes exhausted with requests still "
                "arriving — chained a fresh execution",
                kind,
            )
            return "relaunched"
        except Exception:  # noqa: BLE001 — release so the next edit recovers
            logger.exception(
                "[eos-ingest][worker:%s] chain relaunch failed — releasing; "
                "the next edit will re-dispatch",
                kind,
            )
            force_idle(kind, "chain relaunch failed — next edit re-dispatches")
            return "relaunch-failed"

    force_idle(kind, "passes exhausted (inline executor) — next edit re-dispatches")
    return "passes-exhausted"
