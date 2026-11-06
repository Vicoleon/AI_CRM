"""Work somebody asked for that never produced a result.

Every async feature here dispatches a Cloud Run Job and returns 202 immediately.
That is correct — Cloud Run throttles the service's CPU to near-zero once a
response returns, so the work has to leave the process. The cost is that the
request can no longer report the outcome, and nothing else did either: a
circular import once killed every Smart Teams job on startup for weeks, people
lost a 30-minute interview result, and the only signal was someone mentioning a
stuck screen.

The Job alerts watch the MECHANISM — a Job execution that failed, a Job that
never launched. This module covers the shape neither can see: a job that
finished, or died, without the row it was supposed to write ever becoming a
result. From the person's side all three are identical, because in every case
the screen says the work is in progress and the request succeeded long ago.

**Each probe reuses the feature's own definition of dead** rather than inventing
a threshold, because a second opinion about when something is broken is how an
alert ends up firing at a different moment from the screen that tells somebody
their result is missing:

* Smart Teams — ``profile.SCORING_STALE_SECONDS``, the same constant the router
  reports ``delayed`` on.
* Deep X-Ray — ``xray.config.DEEP_RUN_TTL_MINUTES``. X-Ray already knew these
  runs exist: its 409 guard treats a pending row older than the TTL as dead so
  it stops blocking fresh runs, and that reaper's comment says a detached run
  "has no other watchdog". This is that watchdog. Until now the system
  recognised the corpse and quietly stepped over it.
* Deep Prep — no feature constant existed, so :data:`DEEP_PREP_STALE_MINUTES`
  is defined below and lives here because this is its only consumer.

**Nothing here mutates.** A stuck row is evidence, and reaping it would destroy
the only record of what happened while making the feature look healthy — which
is the failure this module exists to end. Recovery is a re-dispatch, and each
item carries the exact arguments for it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: A queued/running Deep Prep older than this is dead rather than slow. A deep
#: run is a multi-source gather, one Pro-model call, a PDF render, a Drive
#: upload and an email — minutes, not tens of minutes. Generous enough that a
#: slow-but-live run is never reported; no feature constant existed to reuse,
#: unlike the other two probes.
DEEP_PREP_STALE_MINUTES: int = 30

#: A guide whose RAG write has not landed after this long is stuck rather than
#: in flight. The guide job is a single corpus write — a hand re-run measured
#: ~25s — so 30 minutes is far past plausible, and matches the Deep Prep number
#: for the same reason: no feature constant existed to reuse.
GUIDE_RAG_STALE_MINUTES: int = 30

#: Cap per feature. A bad week must not turn one nightly log line into a flood,
#: and the alert only needs to know the count is above zero.
DEFAULT_LIMIT: int = 50

# ── Age ceilings: when work stops being a pending obligation ────────────────
# A ceiling exists because an alert that can never be cleared is an alert
# everyone learns to ignore. A Deep Prep request weeks old still sitting in
# `queued` is a genuine loss, and permanently unrecoverable, so without a
# ceiling it would re-fire the alert every night for ever and teach the
# recipients to filter it.
#
# **The ceiling is PER PROBE and most probes correctly have none.** The question
# is not "is this old?" but "has the value of this work expired?", and the two
# answers differ completely:
#
#   * time-tied work expires. A Deep Prep is a brief for one meeting and an
#     X-Ray is a point-in-time research snapshot; once the meeting has happened
#     nobody wants the brief, and a two-month-old stalled snapshot would be
#     re-requested fresh rather than resumed. Reporting those for ever is noise.
#   * everything else here stays recoverable, so it stays reported. An unscored
#     interview is still worth scoring months later — the answers do not go
#     stale, which is exactly why nobody had to retake anything in September. A
#     published guide missing from the corpus is still wrong.
#
# And ONE probe must never have a ceiling on principle: a retracted guide still
# in the corpus is a live privacy exposure that gets MORE serious with age, not
# less. A ceiling there would mean the longer somebody's retracted guide stayed
# readable in Chat, the quieter the system became about it.

#: A brief for a meeting that has already happened. Nobody is waiting.
DEEP_PREP_MAX_AGE_DAYS: int | None = 30
#: A point-in-time snapshot; a stalled one this old would be re-requested.
XRAY_MAX_AGE_DAYS: int | None = 30
#: Deliberately None — see above. Interview answers stay valid and scoreable.
SMART_TEAMS_MAX_AGE_DAYS: int | None = None
#: Deliberately None — a live privacy exposure, worse with age, never expires.
GUIDE_PURGE_MAX_AGE_DAYS: int | None = None
#: Deliberately None — a published guide missing from the corpus stays wrong.
GUIDE_INGEST_MAX_AGE_DAYS: int | None = None


def _floor(max_age_days: int | None, now: datetime) -> datetime | None:
    """The oldest timestamp still worth reporting, or ``None`` for no ceiling."""
    if max_age_days is None:
        return None
    return now - timedelta(days=max_age_days)


@dataclass(frozen=True)
class StaleItem:
    """One piece of work somebody is still waiting on."""

    feature: str
    #: Distinctive literal the log-based metric greps for. One per feature, so
    #: each alert can carry a runbook with the right re-run command instead of a
    #: generic "some job broke".
    token: str
    id: str
    owner: str | None
    since: datetime | None
    #: Container args that re-dispatch exactly this item on ``cubie-worker``.
    rerun_args: str

    def describe(self) -> str:
        when = self.since.isoformat() if self.since else "?"
        return f"id={self.id} owner={self.owner or '?'} since={when}"


def smart_teams_stale(db: Session, *, limit: int = DEFAULT_LIMIT) -> list[StaleItem]:
    """Work Style Interviews answered in full that never became a profile."""
    from rubik_core.smart_teams.profile import stale_scoring_interviews

    return [
        StaleItem(
            feature="smart_teams",
            token="SMART_TEAMS_SCORING_STALE",
            id=row.id,
            owner=row.user_email,
            since=row.completed_at,
            rerun_args=(
                f"-m,backend.jobs.smart_teams_score_job,--interview-id,{row.id}"
            ),
        )
        for row in stale_scoring_interviews(db, limit=limit)
    ]


def deep_prep_stale(db: Session, *, limit: int = DEFAULT_LIMIT) -> list[StaleItem]:
    """Deep Preps that never left ``queued``/``running``.

    ``meeting_prep_runs`` has no ``updated_at``, so ``created_at`` is the only
    anchor — which is also the honest one here: the row is written ``queued`` by
    the request and the ONLY thing that ever moves it is the job
    (``_set_deep_prep_status``). A job that dies before its first status write
    leaves the row exactly as the request left it, forever.
    """
    from rubik_core.database.models import MeetingPrepRun

    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=DEEP_PREP_STALE_MINUTES)
    floor = _floor(DEEP_PREP_MAX_AGE_DAYS, now)
    rows = (
        db.query(MeetingPrepRun)
        .filter(
            MeetingPrepRun.mode == "deep",
            MeetingPrepRun.delivery_status.in_(("queued", "running")),
            MeetingPrepRun.created_at < cutoff,
            *([MeetingPrepRun.created_at >= floor] if floor else []),
        )
        .order_by(MeetingPrepRun.created_at.asc())
        .limit(limit)
        .all()
    )
    return [
        StaleItem(
            feature="deep_prep",
            token="DEEP_PREP_DELIVERY_STALE",
            id=row.id,
            owner=row.user_email,
            since=row.created_at,
            rerun_args=(
                "-m,backend.jobs.deep_prep_job,--run-id,"
                f"{row.id},--user-email,{row.user_email},--event-id,"
                f"{row.event_id or '<event id>'}"
            ),
        )
        for row in rows
    ]


def xray_deep_stale(db: Session, *, limit: int = DEFAULT_LIMIT) -> list[StaleItem]:
    """Deep X-Ray runs still pending/running past the feature's own TTL.

    Anchored on ``updated_at``, not ``created_at``: a live deep run writes its
    per-agent ``progress`` as agents finish, so ``updated_at`` advancing is
    proof of life and a long-but-progressing run is correctly left alone. A run
    that dies leaves ``updated_at`` at its last write — and one that dies on
    import never writes at all, so the two timestamps are equal and it is
    caught either way.

    This is stricter than X-Ray's 409 reaper, which asks the same question of
    ``created_at`` only because it needs to know whether to BLOCK a new run, not
    whether anyone was failed.
    """
    from rubik_core.database.models import XraySnapshot
    from rubik_core.xray import config as xray_config

    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=xray_config.DEEP_RUN_TTL_MINUTES)
    floor = _floor(XRAY_MAX_AGE_DAYS, now)
    rows = (
        db.query(XraySnapshot)
        .filter(
            XraySnapshot.kind == "deep",
            XraySnapshot.status.in_(("pending", "running")),
            XraySnapshot.updated_at < cutoff,
            *([XraySnapshot.updated_at >= floor] if floor else []),
        )
        .order_by(XraySnapshot.updated_at.asc())
        .limit(limit)
        .all()
    )
    return [
        StaleItem(
            feature="xray_deep",
            token="XRAY_DEEP_RUN_STALE",
            id=row.id,
            owner=row.created_by,
            since=row.updated_at,
            rerun_args=(
                "-m,backend.jobs.xray_deep_job,--snapshot-id,"
                f"{row.id},--company-id,{row.company_id},--user-email,"
                f"{row.created_by or '<requester>'}"
            ),
        )
        for row in rows
    ]


def _guide_owner(row) -> str | None:
    """Whose guide it is, for the log line. Pair/team guides have several."""
    emails = list(row.subject_emails or [])
    return ", ".join(emails) if emails else row.subject_key


def guide_purge_stale(db: Session, *, limit: int = DEFAULT_LIMIT) -> list[StaleItem]:
    """Retracted guides still sitting in the companywide RAG corpus.

    **Reported before the ingest probe on purpose: this one is a privacy
    defect.** The guide job's own docstring says so — the guide is out of the
    app and still answerable in Chat, so somebody who retracted their "How to
    work with me" guide has been told it is gone while Chat can still quote it.

    The row deliberately keeps ``rag_ingested = true`` after a failed purge
    rather than claiming a retraction that did not happen, which is exactly what
    makes this query possible.

    Note what this covers that the audit trail cannot. When the job RUNS and
    fails it already writes ``SMART_TEAMS_GUIDE_PURGE_FAILED`` to ``audit_logs``.
    A job that never ran — never dispatched, or dead on import — writes
    nothing at all, and the ``--purge`` dispatch
    is fire-once from ``unpublish`` with no retry and no other backstop. So an
    empty audit trail is not evidence that nothing was lost; this is.
    """
    from rubik_core.database.smart_teams import SmartTeamsDocument

    cutoff = datetime.utcnow() - timedelta(minutes=GUIDE_RAG_STALE_MINUTES)
    rows = (
        db.query(SmartTeamsDocument)
        .filter(
            SmartTeamsDocument.published.is_(False),
            SmartTeamsDocument.unpublished_at.isnot(None),
            SmartTeamsDocument.rag_ingested.is_(True),
            SmartTeamsDocument.unpublished_at < cutoff,
        )
        .order_by(SmartTeamsDocument.unpublished_at.asc())
        .limit(limit)
        .all()
    )
    return [
        StaleItem(
            feature="guide_purge",
            token="SMART_TEAMS_GUIDE_PURGE_STALE",
            id=row.id,
            owner=_guide_owner(row),
            since=row.unpublished_at,
            rerun_args=(
                f"-m,backend.jobs.smart_teams_guide_job,--document-id,{row.id},--purge"
            ),
        )
        for row in rows
    ]


def guide_ingest_stale(db: Session, *, limit: int = DEFAULT_LIMIT) -> list[StaleItem]:
    """Published guides that never reached the corpus, so Chat cannot use them.

    Not a privacy problem — the in-app read works — but the feature is half
    shipped, and the person sees "Cubie is still copying it into Chat's
    knowledge base" indefinitely.

    Anchored on ``updated_at``, which the publish itself bumps, so the clock
    starts when the guide went live rather than when it was generated.

    Same audit-trail caveat as the purge probe: a job that ran and failed leaves
    ``SMART_TEAMS_GUIDE_INGEST_FAILED``, a job that never ran leaves nothing.
    Recovery is a plain re-run of ``--ingest`` — ``RagManager.update_document``
    deletes any ragFile at the same path before writing, so a re-ingest also
    replaces a stale earlier copy and no separate purge is needed.
    """
    from rubik_core.database.smart_teams import SmartTeamsDocument

    cutoff = datetime.utcnow() - timedelta(minutes=GUIDE_RAG_STALE_MINUTES)
    rows = (
        db.query(SmartTeamsDocument)
        .filter(
            SmartTeamsDocument.published.is_(True),
            SmartTeamsDocument.approved_at.isnot(None),
            SmartTeamsDocument.rag_ingested.is_(False),
            SmartTeamsDocument.updated_at < cutoff,
        )
        .order_by(SmartTeamsDocument.updated_at.asc())
        .limit(limit)
        .all()
    )
    return [
        StaleItem(
            feature="guide_ingest",
            token="SMART_TEAMS_GUIDE_INGEST_STALE",
            id=row.id,
            owner=_guide_owner(row),
            since=row.updated_at,
            rerun_args=(
                f"-m,backend.jobs.smart_teams_guide_job,--document-id,{row.id},--ingest"
            ),
        )
        for row in rows
    ]


#: Every probe, in the order they are reported. Adding a feature here is the
#: whole job of covering it — but the alert needs its token in
#: ``scripts/setup_monitoring.sh`` too, and
#: ``backend/tests/test_stale_work_sweep.py`` fails if a token here has no
#: policy there.
PROBES: tuple[Callable[..., list[StaleItem]], ...] = (
    # Privacy defect first: a retracted guide still answerable in Chat outranks
    # anything else here when somebody is triaging a morning's findings.
    guide_purge_stale,
    smart_teams_stale,
    deep_prep_stale,
    xray_deep_stale,
    guide_ingest_stale,
)

#: Token per feature, for the drift guard and the monitoring script.
TOKENS: dict[str, str] = {
    "guide_purge": "SMART_TEAMS_GUIDE_PURGE_STALE",
    "smart_teams": "SMART_TEAMS_SCORING_STALE",
    "deep_prep": "DEEP_PREP_DELIVERY_STALE",
    "xray_deep": "XRAY_DEEP_RUN_STALE",
    "guide_ingest": "SMART_TEAMS_GUIDE_INGEST_STALE",
}


def find_stale_work(db: Session, *, limit: int = DEFAULT_LIMIT) -> list[StaleItem]:
    """Run every probe. One probe raising must not hide the others' findings."""
    found: list[StaleItem] = []
    for probe in PROBES:
        try:
            found.extend(probe(db, limit=limit))
        except Exception:  # noqa: BLE001 — a broken probe is not a silent all-clear
            logger.exception(
                "[reliability] stale-work probe %s failed; its feature is NOT "
                "covered by this run",
                getattr(probe, "__name__", probe),
            )
    return found


def report_stale_work(*, limit: int = DEFAULT_LIMIT) -> list[StaleItem] | None:
    """Log what nobody is being told about. Never raises.

    One summary line per affected feature carrying that feature's token (what
    the alert counts), then one line per item carrying the id and the re-run
    args (what the person who opens the alert actually needs). Asking somebody
    to go find those in a database while a colleague's result is missing is the
    wrong way round.

    Returns the items found, or ``None`` if the sweep could not run at all —
    which the caller must NOT treat as an all-clear. An empty list means
    "checked, nothing waiting"; ``None`` means "did not check", and the job
    turns that into a non-zero exit so the Job-failure alert pages. A backstop
    that reports success when it could not look is worse than no backstop.
    """
    try:
        from rubik_core.database.db import SessionLocal

        with SessionLocal() as db:
            items = find_stale_work(db, limit=limit)
    except Exception:  # noqa: BLE001 — a backstop must not become a second outage
        logger.exception("[reliability] stale-work sweep could not run")
        return None

    if not items:
        logger.info(
            "[reliability] no stale background work across %d features", len(PROBES)
        )
        return []

    by_feature: dict[str, list[StaleItem]] = {}
    for item in items:
        by_feature.setdefault(item.feature, []).append(item)

    for feature, group in by_feature.items():
        token = group[0].token
        logger.error(
            "%s: %d %s item(s) somebody is still waiting on. Re-dispatch each "
            "on cubie-worker with the args below.",
            token,
            len(group),
            feature,
        )
        for item in group:
            logger.error("%s: %s rerun=%s", token, item.describe(), item.rerun_args)
    return items
