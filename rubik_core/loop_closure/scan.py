"""``run_scan`` — the morning Loose Ends scan, end to end (README §4).

One central run, every weekday, over every rostered AE:

1. **Roster** — ``roster.roster_emails`` (or the ``emails`` given).
2. **Lock** — a ``running`` ``loop_scan_runs`` row younger than 60 minutes
   blocks a second run, so a scheduler retry cannot scan everyone twice. A dry
   run takes no lock and leaves no row.
3. **Hear promises** for roster AEs the AI Priorities batch does not cover
   (``list_active_users``), so their promises reach the ledger too.
4. **Gather** each AE's Gmail (metadata, two lanes) and own Slack messages.
   A source that cannot be read is a named coverage state, never an empty one.
5. **One HubSpot index** for the whole run (``hubspot_index``), sized to the
   oldest item still open.
6. **Per AE** — detect candidates (``detect``), ask the judge about the new
   open-loop and Slack candidates (one Flash call), re-check every open item
   for closure, then write through ``store``. Each AE commits on their own, so
   one AE's failure costs only that AE's morning.
7. **Finish** — the run row gets its named-bucket report and per-AE coverage;
   then the daily channel digest and the weekly scorecard cell.

**Never raises for a single AE.** A failure while scanning one person is caught,
rolled back, named in the report's ``failed`` bucket (coverage ``scan:
failed``), and the next person is scanned. A failure of the run itself (the
database is gone), or a run in which EVERY AE failed, marks the run ``failed``
and returns the report with ``status: failed`` — no digest, no scorecard — so
the job exits non-zero and the Job-failure alert fires.

**Only rostered people are read.** An ``emails`` list is intersected with the
``loop_closure`` roster; anyone else is named in ``not_on_roster`` and never
scanned (a one-address admin run cannot reach an exec's mailbox).

**A dry run decides everything and keeps nothing.** It reads and judges exactly
like a real run (that is what it is for), then rolls back: no items, no run
row, no promise hearing, no digest, no scorecard write. It ignores
``LOOP_CLOSURE_ENABLED`` on purpose — rendering without writing is how the
switch gets reviewed before it is flipped (the close-date roll-up's rule).

**Jobs never write HubSpot.** The scan only reads it; "Log it in HubSpot" is a
click, in the request, on the Service (``crm_log``).

Every external dependency arrives through :class:`ScanDeps`, so the tests run
the whole orchestration against fakes and SQLite with no network at all.
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.cockpit.business_hours import pin_business_time_zone
from rubik_core.database.loop_closure import LoopItem, LoopScanRun
from rubik_core.logsafe import safe_error
from rubik_core.loop_closure import constants, detect, gmail_reader, judge, store
from rubik_core.loop_closure.hubspot_index import (
    HubSpotIndex,
    build_index,
    error_label,
)

logger = logging.getLogger(__name__)


@dataclass
class ScanDeps:
    """Every outside system the scan touches, injectable.

    ``None`` means "the real one": ``GmailClient(email)``, ``SlackService(email)``,
    ``HubSpotAPIClient()``, :func:`judge.judge`, the wall clock,
    ``run_accountability_pass``, ``SlackBotClient()`` and ``list_active_users``.
    """

    gmail_factory: Callable[[str], Any] | None = None
    slack_factory: Callable[[str], Any] | None = None
    hubspot_client: Any = None
    judge: Callable[..., Any] | None = None
    now: Callable[[], datetime] | None = None
    hear_promises: Callable[[str], Any] | None = None
    bot: Any = None
    # Emails the AI Priorities batch already hears promises for.
    batch_roster: Callable[[], Iterable[str]] | None = None
    # Handed to roster_emails (Slack / Google "has left" lookup).
    deactivated_lookup: Callable[[Iterable[str]], set[str]] | None = None
    # HubSpot read retry (tests pass one that never sleeps).
    hubspot_retry: Callable[..., Any] | None = None


@dataclass
class _AE:
    """One AE's gathered inputs for this run."""

    email: str
    name: str = ""
    gmail: str = "unavailable"
    slack: str = "unavailable"
    threads: dict[str, gmail_reader.Thread] = field(default_factory=dict)
    reread: dict[str, gmail_reader.Thread] = field(default_factory=dict)
    slack_messages: list[Any] = field(default_factory=list)
    mutes: list[str] = field(default_factory=list)
    items: dict[tuple[str, str], LoopItem] = field(default_factory=dict)


# ── Small helpers ────────────────────────────────────────────────────────────


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _naive(dt: datetime) -> datetime:
    return _aware(dt).astimezone(timezone.utc).replace(tzinfo=None)


def _business_today(now: datetime) -> date:
    from rubik_core.cockpit.business_hours import _business_tz

    return _aware(now).astimezone(_business_tz()).date()


#: Gmail states in which the mailbox WAS read (``partial`` = read, but a lane
#: had more threads than one morning reads). Detection and closure run on both.
_GMAIL_READ = ("ok", "partial")


def _execution() -> str | None:
    """This Cloud Run Job execution's name (None outside a Job)."""
    return (os.environ.get("CLOUD_RUN_EXECUTION") or "").strip() or None


def _task_attempt() -> int:
    """Cloud Run's 0-based retry index of this Job task (0 outside a Job)."""
    try:
        return int(os.environ.get("CLOUD_RUN_TASK_ATTEMPT") or 0)
    except ValueError:
        return 0


def _window_start(now: datetime, oldest: datetime, limits: dict) -> datetime:
    """Where the engagement index starts (README §4.3).

    ``min(now − lookback, oldest open item) − 1 day``, never earlier than the
    max age (+1 day). Kept as an instant, not whole days: flooring the oldest
    item's age to days would start the index up to a day late and miss the
    ``opened − 1 day`` evidence the closure rule promises.
    """
    start = min(now - timedelta(days=limits["lookback_days"]), oldest) - timedelta(
        days=1
    )
    return max(start, now - timedelta(days=limits["max_age_days"] + 1))


def _new_report(trigger: str, dry_run: bool) -> dict:
    return {
        "status": "running",
        "run_id": None,
        "trigger": trigger,
        "dry_run": bool(dry_run),
        "roster": 0,
        "scanned": 0,
        "new": {k: 0 for k in constants.KINDS},
        "reopened": 0,
        "still_open": 0,
        "closed": {},
        "aged_out": 0,
        "dismissed_not_reflagged": 0,
        # declined: the judge said no (remembered, never asked again);
        # remembered: declined on an earlier run, so not sent this time;
        # over_cap: more than one call reads, sent on a later run;
        # no_verdict: sent, but the reply had no verdict for it (asked again).
        "judge": {
            "calls": 0,
            "failed": [],
            "declined": 0,
            "remembered": 0,
            "over_cap": 0,
            "no_verdict": 0,
        },
        "coverage_problems": {
            "gmail_unavailable": [],
            "gmail_partial": [],
            "slack_not_connected": [],
            "slack_unavailable": [],
            "hubspot_no_owner": [],
            "ai_unavailable": [],
        },
        "hubspot": None,
        "partial_skipped": 0,
        "heard_for": 0,
        "failed": [],
    }


def _summary(report: dict) -> dict:
    """The one-line buckets the job logs — counts only."""
    problems = report.get("coverage_problems") or {}
    return {
        "status": report.get("status"),
        "roster": report.get("roster"),
        "scanned": report.get("scanned"),
        "new": sum((report.get("new") or {}).values()),
        "reopened": report.get("reopened"),
        "closed": sum((report.get("closed") or {}).values()),
        "aged_out": report.get("aged_out"),
        "hubspot": report.get("hubspot"),
        "failed": len(report.get("failed") or []),
        **{k: len(v) for k, v in problems.items()},
    }


def _owner_names(db: Session, emails: list[str]) -> dict[str, str]:
    from sqlalchemy import func

    from rubik_core.database.models import User

    if not emails:
        return {}
    rows = (
        db.query(User.email, User.name).filter(func.lower(User.email).in_(emails)).all()
    )
    return {(e or "").lower(): (n or "") for e, n in rows}


# ── Defaults for the real systems ────────────────────────────────────────────


def _default_gmail(email: str) -> Any:
    from rubik_core.tools.gmail import GmailClient

    return GmailClient(user_email=email)


def _default_slack(email: str) -> Any:
    from backend.services.slack_service import SlackService

    return SlackService(email)


def _default_batch_roster() -> list[str]:
    from rubik_core.priorities.batch import list_active_users

    return list_active_users()


def _default_hear(email: str) -> Any:
    from rubik_core.promises.accountability import run_accountability_pass

    return run_accountability_pass(email)


# ── The run lock ─────────────────────────────────────────────────────────────


def _take_lock(
    db: Session, now: datetime, trigger: str
) -> tuple[LoopScanRun | None, str | None]:
    """``(run, None)`` with a fresh ``running`` row, or ``(None, blocking_id)``.

    A ``running`` row older than the lock window is a run that died without
    finishing (an OOM, a killed container); it is marked failed so it stops
    reading as "in progress" anywhere.

    A young ``running`` row written by THIS Cloud Run execution, seen on a
    retry attempt (``CLOUD_RUN_TASK_ATTEMPT`` > 0), is the previous attempt's
    orphan — Cloud Run starts a retry only after that attempt exited — so it is
    marked failed (``task_retried``) and the retry scans, instead of reporting
    "locked", exiting 0 and hiding the failure from the Job alert.
    """
    cutoff = _naive(now) - timedelta(minutes=constants.RUN_LOCK_MINUTES)
    execution, attempt = _execution(), _task_attempt()
    running = db.query(LoopScanRun).filter(LoopScanRun.status == "running").all()
    retried: list[LoopScanRun] = []
    for row in running:
        if row.started_at < cutoff:
            continue
        mine = isinstance(row.report, dict) and row.report.get("execution")
        if execution and attempt > 0 and mine == execution:
            retried.append(row)
            continue
        return None, row.id
    for row in running:
        reason = "task_retried" if row in retried else "stale_lock"
        row.status = "failed"
        row.finished_at = _naive(now)
        row.report = {**(row.report or {}), "status": "failed", "reason": reason}
    run = LoopScanRun(
        started_at=_naive(now),
        status="running",
        trigger=trigger,
        dry_run=False,
        report={"execution": execution} if execution else None,
    )
    db.add(run)
    db.commit()
    return run, None


# ── Entry point ──────────────────────────────────────────────────────────────


@pin_business_time_zone
def run_scan(
    db: Session,
    *,
    emails: list[str] | None = None,
    dry_run: bool = False,
    trigger: str = "cli",
    now: datetime | None = None,
    deps: ScanDeps | None = None,
    post_digest: bool = True,
    write_scorecard: bool = True,
) -> dict:
    """Scan every rostered AE (or ``emails``) and return the named buckets."""
    from rubik_core.config import settings

    deps = deps or ScanDeps()
    started = time.monotonic()
    pinned = now is not None or deps.now is not None
    now_utc = _aware(now or (deps.now() if deps.now else datetime.now(timezone.utc)))
    trigger = trigger if trigger in constants.RUN_TRIGGERS else "cli"
    report = _new_report(trigger, dry_run)

    if not dry_run and not getattr(settings, "LOOP_CLOSURE_ENABLED", False):
        report["status"] = "disabled"
        return report

    if write_scorecard:
        from rubik_core.control.repository import module_enabled

        try:
            write_scorecard = module_enabled("eos")
        except Exception:  # noqa: BLE001 — omit EOS write on failed control read
            logger.warning("[loop-closure] EOS module lookup failed", exc_info=True)
            write_scorecard = False

    from rubik_core.loop_closure.roster import roster_emails

    if emails is not None:
        requested = sorted({(e or "").strip().lower() for e in emails} - {""})
        # A named list never widens the roster: whoever is not on
        # ``loop_closure`` (Admin → Feature Access) is not read, dry run or not.
        allowed = set(roster_emails(db, deactivated_lookup=_no_directory_lookup))
        roster = [e for e in requested if e in allowed]
        if len(roster) < len(requested):
            report["not_on_roster"] = [e for e in requested if e not in allowed]
            logger.warning(
                "[loop-closure] %d requested address(es) are not on the roster "
                "and were not scanned",
                len(requested) - len(roster),
            )
    else:
        roster = roster_emails(db, deactivated_lookup=deps.deactivated_lookup)
    report["roster"] = len(roster)

    run: LoopScanRun | None = None
    if not dry_run:
        run, blocking = _take_lock(db, now_utc, trigger)
        if run is None:
            report["status"] = "locked"
            report["blocking_run_id"] = blocking
            logger.info("[loop-closure] another scan is running; this one stopped")
            return report
        report["run_id"] = run.id
        if isinstance(run.report, dict) and run.report.get("execution"):
            report["execution"] = run.report["execution"]

    try:
        coverage = _scan(db, roster, report, now=now_utc, deps=deps, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 — the run fails loudly, the job reports it
        logger.error("[loop-closure] the scan failed: %s", type(exc).__name__)
        db.rollback()
        report["status"] = "failed"
        report["error"] = type(exc).__name__
        if run is not None:
            row = db.get(LoopScanRun, report["run_id"])
            if row is not None:
                row.status = "failed"
                row.finished_at = _naive(now_utc) if pinned else datetime.utcnow()
                row.report = dict(report)
                db.commit()
        report["seconds"] = round(time.monotonic() - started, 1)
        return report

    if dry_run:
        db.rollback()
        report["status"] = "completed"
        report["coverage"] = coverage
        report["seconds"] = round(time.monotonic() - started, 1)
        logger.info("[loop-closure] dry run done %s", _summary(report))
        return report

    if roster and report["scanned"] == 0 and report["failed"]:
        # Every AE failed inside their own scan (a regression in detection or
        # the store, an upstream shape change): the run looked at nobody. It is
        # a FAILED run — no digest from yesterday's rows, no scorecard rewrite —
        # so the job exits 1 and the alert fires.
        logger.error(
            "[loop-closure] every AE's scan failed (%d of %d)",
            len(report["failed"]),
            len(roster),
        )
        report["status"] = "failed"
        report["error"] = "every_ae_failed"
        report["seconds"] = round(time.monotonic() - started, 1)
        row = db.get(LoopScanRun, report["run_id"])
        row.status = "failed"
        row.finished_at = _naive(now_utc) if pinned else datetime.utcnow()
        row.coverage = coverage
        row.report = dict(report)
        db.commit()
        return report

    report["status"] = "completed"
    row = db.get(LoopScanRun, report["run_id"])
    row.status = "completed"
    row.finished_at = _naive(now_utc) if pinned else datetime.utcnow()
    row.coverage = coverage
    row.report = dict(report)
    db.commit()

    # The digest and the scorecard describe the WHOLE roster from the list in
    # the database, even after a one-AE run.
    full_roster: list[str] | None = roster if emails is None else None
    if (post_digest or write_scorecard) and full_roster is None:
        try:
            full_roster = roster_emails(db, deactivated_lookup=deps.deactivated_lookup)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[loop-closure] roster unreadable: %s", safe_error(exc))
            full_roster = roster
    # Whom the latest scan to cover them could NOT check (their scan failed, or
    # their mailbox could not be read): never counted as scanned in the digest,
    # never written as a scorecard zero.
    unchecked: set[str] = set()
    if post_digest or write_scorecard:
        from rubik_core.loop_closure.views import unchecked_emails

        try:
            unchecked = unchecked_emails(db, full_roster or [])
        except Exception as exc:  # noqa: BLE001 — fall back to this run's own view
            logger.warning(
                "[loop-closure] coverage history unreadable: %s", safe_error(exc)
            )
            unchecked = set(report["failed"]) | {
                e for e, c in coverage.items() if c.get("gmail") == "unavailable"
            }
        report["not_checked"] = len(unchecked)

    if post_digest:
        from rubik_core.loop_closure.digest import post_daily_digest

        try:
            report["digest"] = post_daily_digest(
                db, now=now_utc, bot=deps.bot, emails=full_roster, unchecked=unchecked
            )
        except Exception as exc:  # noqa: BLE001 — the list is written; the post can retry
            db.rollback()
            logger.warning("[loop-closure] digest failed: %s", safe_error(exc))
            report["digest"] = {"status": "failed", "error": type(exc).__name__}
    if write_scorecard:
        from rubik_core.loop_closure.scorecard import write_weekly

        try:
            report["scorecard"] = write_weekly(
                db, now=now_utc, emails=full_roster, unchecked=unchecked
            )
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("[loop-closure] scorecard write failed: %s", safe_error(exc))
            report["scorecard"] = {"status": "failed", "error": type(exc).__name__}

    report["seconds"] = round(time.monotonic() - started, 1)
    row = db.get(LoopScanRun, report["run_id"])
    if row is not None:
        row.report = dict(report)
        db.commit()
    logger.info("[loop-closure] done %s", _summary(report))
    return report


# ── The scan itself ──────────────────────────────────────────────────────────


def _scan(
    db: Session,
    roster: list[str],
    report: dict,
    *,
    now: datetime,
    deps: ScanDeps,
    dry_run: bool,
) -> dict[str, dict]:
    from rubik_core.config import settings

    limits = constants.thresholds()
    today = _business_today(now)

    # 3. Hear promises for AEs the priorities batch does not reach.
    if (
        roster
        and not dry_run
        and getattr(settings, "PROMISE_ACCOUNTABILITY_ENABLED", False)
    ):
        try:
            covered = {
                (e or "").strip().lower()
                for e in (deps.batch_roster or _default_batch_roster)()
            }
        except Exception as exc:  # noqa: BLE001 — unknown coverage: hear nobody extra
            logger.warning(
                "[loop-closure] priorities roster unreadable: %s", safe_error(exc)
            )
            covered = set(roster)
        hear = deps.hear_promises or _default_hear
        for email in roster:
            if email in covered:
                continue
            try:
                hear(email)
                report["heard_for"] += 1
            except Exception as exc:  # noqa: BLE001 — never raises in practice; belt and braces
                logger.warning(
                    "[loop-closure] promise hearing failed: %s", safe_error(exc)
                )

    # 4. Gather.
    names = _owner_names(db, roster)
    people: dict[str, _AE] = {}
    for email in roster:
        people[email] = _gather(db, email, names.get(email, ""), deps, limits, now)

    # 5. One HubSpot index.
    index = _index(people, deps, limits, now=now, today=today)
    report["hubspot"] = index.status

    # 6. Per AE.
    coverage: dict[str, dict] = {}
    for email in roster:
        try:
            coverage[email] = _scan_one(
                db,
                people[email],
                index,
                report,
                deps=deps,
                limits=limits,
                now=now,
                today=today,
            )
            if not dry_run:
                db.commit()
            report["scanned"] += 1
        except Exception as exc:  # noqa: BLE001 — one AE never costs the others
            db.rollback()
            logger.warning(
                "[loop-closure] scan failed for one AE (%s): %s",
                email,
                type(exc).__name__,
            )
            report["failed"].append(email)
            # Nothing was decided for this AE, so nothing is reported as read:
            # no counts (a "0 email threads" would read as an empty inbox) and
            # every source "unavailable", with ``scan: failed`` — the one
            # sentence the page shows (constants.coverage_notes).
            coverage[email] = {
                "gmail": "unavailable",
                "slack": "unavailable",
                "hubspot": "unavailable",
                "ai": "unavailable",
                "scan": "failed",
            }
    return coverage


def _no_directory_lookup(_emails: Iterable[str]) -> set[str]:
    """No Slack / Google "has left" sweep: for a roster MEMBERSHIP check only."""
    return set()


def _own_slack(svc: Any, *, since: datetime, limit: int = 100) -> list[Any]:
    """The AE's own Slack messages since ``since``, newest first.

    ``heard.fetch_own_slack``'s read, minus its quiet failures: an unknown
    Slack member id, or a token without ``search:read``, RAISES here, so the
    scan reports Slack ``unavailable`` instead of "read, nothing there".
    """
    uid = svc.slack_user_id()
    if not uid:
        raise RuntimeError("Slack member id unknown")
    after = (since - timedelta(days=1)).date().isoformat()
    return list(
        svc.search_messages(
            f"from:<@{uid}> after:{after}",
            limit=limit,
            sort="timestamp",
            raise_on_scope=True,
        )
        or []
    )


def _gather(
    db: Session, email: str, name: str, deps: ScanDeps, limits: dict, now: datetime
) -> _AE:
    ae = _AE(email=email, name=name)
    ae.mutes = [m.pattern for m in store.mutes_for(db, email)]
    ae.items = store.items_for(db, email)

    client = None
    try:
        client = (deps.gmail_factory or _default_gmail)(email)
        ae.threads, capped = gmail_reader.gather_all(
            client,
            lookback_days=limits["lookback_days"],
            max_per_lane=gmail_reader.MAX_THREADS_PER_LANE,
        )
        # More threads in the window than one morning reads: what was read
        # counts, and the coverage says the rest was not looked at.
        ae.gmail = "partial" if capped else "ok"
    except Exception as exc:  # noqa: BLE001 — "could not look" is a coverage state
        logger.warning(
            "[loop-closure] Gmail unavailable for %s: %s", email, type(exc).__name__
        )
        ae.gmail, ae.threads = "unavailable", {}
    if ae.gmail in _GMAIL_READ:
        stale = [
            i.thread_id
            for i in ae.items.values()
            if i.status == "open"
            and i.kind == "open_loop"
            and i.thread_id
            and i.thread_id not in ae.threads
        ]
        try:
            ae.reread = gmail_reader.reread(client, stale) if stale else {}
        except Exception as exc:  # noqa: BLE001 — those items stay undecided
            logger.warning(
                "[loop-closure] thread re-read failed for %s: %s",
                email,
                type(exc).__name__,
            )
            ae.reread = {}

    try:
        svc = (deps.slack_factory or _default_slack)(email)
        if not getattr(svc, "is_connected", False):
            ae.slack = "not_connected"
        else:
            ae.slack_messages = _own_slack(
                svc, since=now - timedelta(days=detect.SLACK_DAYS), limit=100
            )
            ae.slack = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[loop-closure] Slack unavailable for %s: %s", email, type(exc).__name__
        )
        ae.slack, ae.slack_messages = "unavailable", []
    return ae


def _index(
    people: dict[str, _AE], deps: ScanDeps, limits: dict, *, now: datetime, today: date
) -> HubSpotIndex:
    addresses: set[str] = set()
    oldest = now
    task_ids: list[str] = []
    for ae in people.values():
        for thread in ae.threads.values():
            for m in thread.messages:
                addresses.add(m.from_email)
                addresses.update(m.to)
                addresses.update(m.cc)
        for item in ae.items.values():
            if item.status != "open":
                continue
            if item.kind in ("unlogged", "open_loop") and item.opened_at is not None:
                oldest = min(oldest, _aware(item.opened_at))
            if item.source == "hubspot_task" and ":" in item.source_key:
                task_ids.append(item.source_key.split(":", 1)[1])
    if not people:
        return HubSpotIndex.unavailable("empty roster")
    client = deps.hubspot_client
    if client is None:
        try:
            from rubik_core.retrieval.hubspot_api import HubSpotAPIClient

            client = HubSpotAPIClient()
        except Exception as exc:  # noqa: BLE001 — no token is "unavailable", not a crash
            logger.warning(
                "[loop-closure] HubSpot client unavailable: %s", error_label(exc)
            )
            return HubSpotIndex.unavailable(type(exc).__name__)
    return build_index(
        client,
        owner_emails=list(people),
        addresses=addresses,
        window_start=_window_start(now, oldest, limits),
        now=now,
        today=today,
        task_grace_days=limits["todo_grace_days"],
        max_age_days=limits["max_age_days"],
        task_lookback_days=limits["task_lookback_days"],
        open_task_ids=task_ids,
        internal_domains=limits["internal_domains"],
        public_domains=constants.PUBLIC_EMAIL_DOMAINS,
        retry=deps.hubspot_retry,
    )


def _promises(
    db: Session, email: str, open_items: list[LoopItem]
) -> tuple[list[Any], dict[str, Any]]:
    """``(unkept heard promises, {id: promise} for closure)``."""
    from rubik_core.database.promises import Promise

    unkept = (
        db.query(Promise)
        .filter(
            Promise.owner_email == email,
            Promise.source.in_(detect.HEARD_SOURCES),
            Promise.status.in_(detect.UNKEPT_STATUSES),
        )
        .all()
    )
    by_id = {str(p.id): p for p in unkept}
    wanted = [
        i.source_key.split(":", 1)[1]
        for i in open_items
        if i.source == "promise" and ":" in i.source_key
    ]
    missing = [pid for pid in wanted if pid not in by_id]
    if missing:
        for p in db.query(Promise).filter(Promise.id.in_(missing)).all():
            by_id[str(p.id)] = p
    return unkept, by_id


def _judge_new(
    db: Session,
    ae: _AE,
    candidates: list[detect.Candidate],
    report: dict,
    *,
    deps: ScanDeps,
    now: datetime,
) -> tuple[list[detect.Candidate], str, int]:
    """Ask the judge about candidates that are not on the list yet.

    Returns the candidates to keep, the ``ai`` coverage state, and how many
    fresh candidates were left for a later run because one call reads only
    ``judge.MAX_EMAILS`` / ``MAX_SLACK``. Candidates already on the list (any
    status) are not re-judged: an open one is open, a dismissed one is never
    re-flagged and a closed open loop never re-opens. Candidates the judge
    DECLINED on an earlier run are remembered (``loop_judgements``) and never
    sent again, so the oldest declined threads cannot take every slot each
    morning while a newer real request waits.
    """
    judged = store.judged_keys(db, ae.email)
    keep: list[detect.Candidate] = []
    fresh: list[detect.Candidate] = []
    for c in candidates:
        key = (c.kind, c.source_key)
        if not c.needs_judge or key in ae.items:
            keep.append(c)
        elif key in judged:
            report["judge"]["remembered"] += 1
        else:
            fresh.append(c)
    if not fresh:
        return keep, "ok", 0
    fresh.sort(key=lambda c: c.opened_at)
    emails_in: list[judge.EmailInput] = []
    slack_in: list[judge.SlackInput] = []
    tags: dict[str, detect.Candidate] = {}
    for c in fresh:
        if c.kind == "open_loop":
            if len(emails_in) >= judge.MAX_EMAILS:
                continue
            tag = f"E{len(emails_in) + 1}"
            emails_in.append(judge.email_input(tag, c, owner=ae.email))
        else:
            if len(slack_in) >= judge.MAX_SLACK:
                continue
            tag = f"S{len(slack_in) + 1}"
            slack_in.append(judge.slack_input(tag, c))
        tags[tag] = c
    deferred = len(fresh) - len(tags)
    report["judge"]["over_cap"] += deferred
    report["judge"]["calls"] += 1
    verdicts = (deps.judge or judge.judge)(
        emails_in, slack_in, owner_name=ae.name, now=now
    )
    if verdicts is None:
        report["judge"]["failed"].append(ae.email)
        return keep, "unavailable", deferred
    for tag, c in tags.items():
        if tag.startswith("E"):
            v = verdicts.emails.get(tag)
            ok = (
                v is not None
                and v.expects_reply
                and (
                    c.detail.get("match") in ("contact", "domain") or v.business_request
                )
            )
            if ok:
                c.detail = {**c.detail, "judge": "expects_reply"}
        else:
            v = verdicts.slack.get(tag)
            ok = v is not None and v.reports_client_touch
            if ok:
                c.detail = {**c.detail, "judge": "client_touch"}
        if ok:
            c.headline = (v.headline or None) if v else None
            keep.append(c)
        elif v is None:
            # Asked, and the reply skipped it: not a "no". Asked again next run.
            report["judge"]["no_verdict"] += 1
        else:
            report["judge"]["declined"] += 1
            store.record_declined(db, ae.email, c.kind, c.source_key, now=now)
    return keep, "ok", deferred


def _scan_one(
    db: Session,
    ae: _AE,
    index: HubSpotIndex,
    report: dict,
    *,
    deps: ScanDeps,
    limits: dict,
    now: datetime,
    today: date,
) -> dict:
    rules = detect.Rules.from_thresholds(
        ae.email, now=now, today=today, limits=limits, mutes=ae.mutes
    )
    stats = detect.Stats()
    owner_id = index.owner_id(ae.email) if index.available else None
    # Re-read this AE's rows now: the gather loaded them before the HubSpot
    # index (minutes), and a click committed since — "Not a real miss", "Log it
    # in HubSpot" — must be what this scan sees.
    ae.items = store.items_for(db, ae.email, refresh=True)
    open_items = [i for i in ae.items.values() if i.status == "open"]
    store.prune_judgements(
        db, ae.email, before=now - timedelta(days=limits["lookback_days"] + 2)
    )
    gmail_read = ae.gmail in _GMAIL_READ

    candidates: list[detect.Candidate] = []
    if gmail_read:
        candidates += detect.unlogged_email_candidates(ae.threads, index, rules, stats)
        candidates += detect.open_loop_candidates(ae.threads, index, rules)
    if ae.slack == "ok":
        candidates += detect.slack_candidates(
            ae.slack_messages, index.ae_companies(owner_id), index, rules, stats
        )
    unkept, promises_by_id = _promises(db, ae.email, open_items)
    candidates += detect.promise_candidates(unkept, rules)
    owner_tasks = index.tasks.get(owner_id, []) if owner_id else []
    candidates += detect.task_candidates(owner_tasks, index, rules)

    candidates, ai, deferred = _judge_new(
        db, ae, candidates, report, deps=deps, now=now
    )

    # Closure re-check, every open item.
    ctx = detect.ClosureContext(
        now=now,
        max_age_days=limits["max_age_days"],
        index=index,
        owner=ae.email,
        gmail_ok=gmail_read,
        threads={**ae.threads, **ae.reread},
        promises=promises_by_id,
        today=today,
        todo_grace_days=limits["todo_grace_days"],
        todo_undated_days=limits["todo_undated_days"],
        internal_domains=tuple(limits["internal_domains"]),
        owner_id=owner_id,
    )
    closed = 0
    for item in open_items:
        verdict = detect.closure_for(item, ctx)
        if verdict is None:
            store.touch(item, now=now)
            continue
        reason, evidence = verdict
        # Conditional: a row another request changed since it was read (a
        # dismissal committed a moment ago) keeps that change.
        if not store.close_if_open(db, item, reason=reason, evidence=evidence, now=now):
            continue
        if reason == "aged_out":
            report["aged_out"] += 1
        else:
            report["closed"][reason] = report["closed"].get(reason, 0) + 1
            closed += 1

    # New, re-opened, or already there.
    new = 0
    outcomes: Counter[str] = Counter()
    for cand in candidates:
        existing = ae.items.get((cand.kind, cand.source_key))
        after = (
            detect.reopen_after(existing)
            if existing is not None and existing.status == "closed"
            else None
        )
        outcome, item = store.apply_candidate(
            db, ae.email, cand, existing, now=now, reopen_after=after
        )
        outcomes[outcome] += 1
        if outcome == "new":
            report["new"][cand.kind] += 1
            ae.items[(cand.kind, cand.source_key)] = item
            new += 1
        elif outcome == "reopened":
            report["reopened"] += 1
            new += 1
    report["still_open"] += outcomes["still_open"]
    report["dismissed_not_reflagged"] += outcomes["dismissed"]
    report["partial_skipped"] += stats.partial_skipped

    if not index.available:
        hubspot = "unavailable"
    elif owner_id is None:
        hubspot = "no_owner"
    elif index.status == "partial":
        hubspot = "partial"
    else:
        hubspot = "ok"
    problems = report["coverage_problems"]
    if ae.gmail == "partial":
        problems["gmail_partial"].append(ae.email)
    elif ae.gmail != "ok":
        problems["gmail_unavailable"].append(ae.email)
    if ae.slack == "not_connected":
        problems["slack_not_connected"].append(ae.email)
    elif ae.slack != "ok":
        problems["slack_unavailable"].append(ae.email)
    if hubspot == "no_owner":
        problems["hubspot_no_owner"].append(ae.email)
    if ai != "ok":
        problems["ai_unavailable"].append(ae.email)
    out = {
        "gmail": ae.gmail,
        "slack": ae.slack,
        "hubspot": hubspot,
        "ai": ai,
        "threads_scanned": len(ae.threads),
        "slack_messages_scanned": len(ae.slack_messages),
        "promises_scanned": len(unkept),
        "tasks_scanned": len(owner_tasks),
        "new": new,
        "closed": closed,
    }
    if deferred:
        # Waiting for a judge slot on a later run — said on the page, so an
        # incomplete morning never reads as an all-clear.
        out["judge_deferred"] = deferred
    return out
