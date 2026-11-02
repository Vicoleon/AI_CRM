"""The six scheduled sweeps behind Sales Call Capture and the Sales Cockpit.

One module, two entry points, **one implementation of each sweep**:

* ``backend/routers/agents.py::scheduler_router`` calls the ``run_*`` functions
  below inside ``run_in_threadpool`` for the five sweeps that fit under Cloud
  Run's 300s request cap;
* ``python -m backend.jobs.sales_kaizen <sweep>`` runs the same function as one
  ``cubie-worker`` task — which is the LIVE path for ``ccs-sweep`` and the
  manual escape hatch for the rest.

Having the schedule call one function that a human can also run by hand is the
point. The alternative — a router that inlines the loop and a job module that
re-implements it — is how the hand-run repair stops matching what the scheduler
actually does, and that divergence is only ever discovered while something is
already broken.

**Every sweep reports NAMED OUTCOME BUCKETS, never a bare count.** The
failure that matters here is invisible in a total: until the Meet scopes are
granted on the DWD client id every Meet call 403s. "0 calls found" and "we were
refused 63 times" must not render the same.

**Where each sweep runs, and why** (Cloud Run throttles the Service's CPU to
near-zero the instant the response returns, so a ``BackgroundTask`` here would
be starved — ``backend/jobs/launcher.py`` records two production incidents):

===========================  ==========================================
``ccs-sweep``                Cloud Run Job when live; threadpool when
                             ``dry_run`` (a dry run does no ingest and
                             makes no LLM call, so it is a handful of
                             Meet reads and can answer inline).
``cockpit-recompute``        threadpool — one query plus in-memory
                             transitions over the live set.
``cockpit-stage-lag``        threadpool — one HubSpot read per deal
                             Cubie already holds evidence for.
``cockpit-sow-sent``         threadpool — bounded by the AEs who have a
                             live SOW commitment.
``cockpit-daily-digest``     threadpool, and it has no choice: see the
``cockpit-weekly-aging``     SLACK note below.
===========================  ==========================================

``ccs-sweep`` is the one that cannot be a request. Per AE it may run up to
``sweep.MAX_EXTRACTIONS_PER_USER_RUN`` (5) Gemini Pro extractions and as many
Vertex RAG imports, and Vertex serializes imports per corpus — backing off up to
~4.5 minutes on a single document while another operation holds the corpus. One
execution over the AE roster can therefore exceed 300s on a busy morning, and a
truncated ingest window is not recoverable: **Google deletes conference records,
transcripts and transcript entries 30 days after the conference ends.**

SLACK: a Job can send through the selected workspace's bot. ``SlackBotClient``
resolves its bound ``slack/bot_token`` secret through
``slack_service.get_workspace_bot_token``; the worker receives its control
database URL through the platform Job manifest. The digests' live path remains
the scheduler endpoint on the main service, whose cadence entries point there.
``--dry-run`` renders without sending either way.

Nothing in this module writes to HubSpot: the CCS sweep proposes items for a
human, and the stage-lag and SOW-sent sweeps read only. A HubSpot write from a
Job raises ``HubSpotWriteRefused`` today.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sales_kaizen_job")

#: The sweep completed. Includes a sweep that found nothing — a quiet morning is
#: a product outcome, and failing the execution for it makes the FAILED list
#: useless.
EXIT_OK = 0
#: The sweep itself raised. ``cubie-worker`` runs ``--max-retries=1`` and every
#: sweep here is idempotent, so a retry is safe.
EXIT_ERROR = 1

#: Returned in ``status`` when a feature flag is off. A scheduled endpoint that
#: 404s a scheduler target reads as a broken job in Cloud Scheduler's own
#: alerting, so the endpoints no-op with a log line and this status instead,
#: unlike the user-facing routers, which do 404.
STATUS_DISABLED = "disabled"
STATUS_COMPLETED = "completed"


# ── CCS: the polling ingest ──────────────────────────────────────────────────


def run_ccs_sweep(
    db,
    *,
    lookback_hours: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Poll every AE's Meet conference records; ingest and extract what is new.

    THE PRIMARY INGEST PATH, not a backstop. It also ages out the silence:
    a session that ended more than 30 minutes ago with zero transcript entries
    becomes ``no_transcript``, which is a real outcome (the host turned
    transcription off, the call was under a minute, it was confidential) and not
    an error — the wrap-up still opens, empty.

    Overlap is free and the window should never be narrowed to save quota:
    ingest is idempotent on ``(call_session_id, source, external_id)``, so
    re-reading a conference writes nothing, while a window missed during a
    scheduler outage is a permanently lost call after 30 days.

    ``sweep_all`` contains a per-AE failure itself (rollback, named bucket, move
    on) and keeps ``users_permission_denied`` separate from ``users_failed``, so
    this wrapper adds only the flag no-op and the log line.
    """
    from rubik_core.call_capture import sweep as ccs_sweep
    from rubik_core.config import settings

    hours = int(lookback_hours or settings.CCS_POLL_LOOKBACK_HOURS)

    if not settings.CCS_ENABLED:
        # No-op with a log line, not a 404: see STATUS_DISABLED.
        logger.info(
            "[ccs-sweep] CCS_ENABLED is off — nothing swept (lookback=%dh)", hours
        )
        return {
            "status": STATUS_DISABLED,
            "flag": "CCS_ENABLED",
            "dry_run": bool(dry_run),
            "lookback_hours": hours,
        }

    report = ccs_sweep.sweep_all(db, lookback_hours=hours, dry_run=dry_run)

    # Logged at WARNING and separately from the summary: until the scopes are
    # granted this is the expected shape of every run, and it has to read as
    # "the scopes are not granted" rather than as a quiet week.
    if report.get("users_permission_denied"):
        logger.warning(
            "[ccs-sweep] %d of %d AEs returned 403 from the Meet API — this is "
            "SCOPE NOT GRANTED (meetings.space.readonly on the DWD client "
            "id), not 'no calls'. Nothing was ingested for them.",
            report["users_permission_denied"],
            report.get("users", 0),
        )
    return {"status": STATUS_COMPLETED, **report}


# ── Cockpit: aging ───────────────────────────────────────────────────────────


def run_recompute_states(db, *, dry_run: bool = False) -> dict:
    """Age every live commitment one step along the escalation ladder.

    Transitions only ever move FORWARD, so this is idempotent and nothing
    un-breaches on a re-run. ``recompute_states`` flushes and does not commit,
    which is what makes ``dry_run`` exact rather than a re-implementation: the
    same code runs and the transaction is rolled back.
    """
    from rubik_core.cockpit import commitment_engine
    from rubik_core.config import settings

    if not settings.COCKPIT_ENABLED:
        logger.info("[cockpit-recompute] COCKPIT_ENABLED is off — nothing recomputed")
        return {
            "status": STATUS_DISABLED,
            "flag": "COCKPIT_ENABLED",
            "dry_run": bool(dry_run),
        }

    buckets = commitment_engine.recompute_states(db)
    if dry_run:
        db.rollback()
    else:
        db.commit()
    logger.info("[cockpit-recompute] %s (dry_run=%s)", buckets, dry_run)
    return {"status": STATUS_COMPLETED, "dry_run": bool(dry_run), **buckets}


def run_stage_lag(db, *, dry_run: bool = False, limit: int | None = None) -> dict:
    """Compare each evidenced deal's HubSpot stage against what Cubie knows.

    Iterates ``deal_ids_with_evidence`` rather than the pipeline. The detector is
    structurally incapable of emitting anything for a deal with no evidence, so
    walking every open deal in HubSpot would spend the search quota (5 req/s per
    account) proving that.

    **Commits per deal, not per batch.** ``detect_outcome``'s docstring says the
    hourly sweep commits once per batch, and that is true of the happy path — but
    one malformed deal rolling back a 50-deal batch discards 49 good detections,
    and containment is the whole reason this sweep reports buckets. A commit per
    deal is a handful of rows either way.

    One ``HubSpotWriteClient`` for the whole sweep so its read client and the
    owner/stage caches are shared. Nothing here writes to HubSpot — the client is
    the repo's only fetch-one-by-id and reads with the READ token.
    """
    from rubik_core.cockpit import stage_lag
    from rubik_core.config import settings
    from rubik_core.retrieval.hubspot_write import HubSpotWriteClient

    if not settings.COCKPIT_ENABLED:
        logger.info("[cockpit-stage-lag] COCKPIT_ENABLED is off — nothing detected")
        return {
            "status": STATUS_DISABLED,
            "flag": "COCKPIT_ENABLED",
            "dry_run": bool(dry_run),
        }
    if not stage_lag.configured_stages().configured:
        # Which stages mean what is the company's own pipeline; until an admin
        # lists them there is nothing to compare evidence against.
        logger.info(
            "[cockpit-stage-lag] COCKPIT_STAGE_LAG_STAGES is empty — nothing detected"
        )
        return {
            "status": STATUS_DISABLED,
            "flag": "COCKPIT_STAGE_LAG_STAGES",
            "dry_run": bool(dry_run),
        }

    report: dict = {
        "status": STATUS_COMPLETED,
        "dry_run": bool(dry_run),
        "deals_with_evidence": 0,
        "deals_scanned": 0,
        "deals_deferred": 0,
        "failed": 0,
        # Zero-filled from OUTCOME_REASONS: a dict holding only the buckets that
        # happened cannot tell "nothing was skipped" from "we never looked".
        **{reason: 0 for reason in stage_lag.OUTCOME_REASONS},
    }

    deal_ids = stage_lag.deal_ids_with_evidence(db)
    report["deals_with_evidence"] = len(deal_ids)
    if limit is not None and limit > 0 and len(deal_ids) > limit:
        report["deals_deferred"] = len(deal_ids) - limit
        deal_ids = deal_ids[:limit]

    hs = HubSpotWriteClient()
    for deal_id in deal_ids:
        try:
            outcome = stage_lag.detect_outcome(db, deal_id, hs=hs)
        except Exception:  # noqa: BLE001 — one deal, not the sweep
            db.rollback()
            report["failed"] += 1
            logger.exception("[cockpit-stage-lag] deal %s failed", deal_id)
            continue
        report["deals_scanned"] += 1
        report[outcome.reason] = report.get(outcome.reason, 0) + 1
        if dry_run:
            db.rollback()
        else:
            db.commit()

    logger.info(
        "[cockpit-stage-lag] deals=%d scanned=%d emitted=%d updated=%d "
        "read_failed=%d failed=%d dry_run=%s",
        report["deals_with_evidence"],
        report["deals_scanned"],
        report["emitted"],
        report["updated"],
        report["read_failed"],
        report["failed"],
        dry_run,
    )
    if report["read_failed"]:
        # Distinct from every other reason: the pipeline being accurate and the
        # HubSpot read being broken both produce zero exceptions.
        logger.warning(
            "[cockpit-stage-lag] %d deal read(s) failed — that is a HubSpot "
            "lookup problem, not an accurate pipeline",
            report["read_failed"],
        )
    return report


def run_sow_sent(
    db,
    *,
    dry_run: bool = False,
    since_days: int | None = None,
    owners: list[str] | None = None,
) -> dict:
    """Scan each AE's Sent mail for evidence their live SOW went out.

    Raises SUGGESTIONS only. A wrong auto-close hides a real breach, which is
    exactly the failure the cockpit exists to fix, so confirming is a human
    click that calls ``close_commitment``.

    ⚠️ **This sweep persists nothing today.** ``scan_sent_mail`` returns
    suggestions and there is no column to park them on — ``commitments`` has no
    ``attrs``/suggestion field and the models are final — so the durable path is
    the cockpit reading the detector live for the row it is rendering. Until
    something stores a suggestion, a *schedule* on this endpoint spends Gmail
    quota to produce a log line: run it by hand to verify the lane, and see the
    handoff note before creating the Cloud Scheduler entry.

    ``dry_run`` resolves the roster and **reads no mailbox**. That is the honest
    dry run for this sweep: the detector's own rule is that reading somebody's
    Sent folder needs a reason that exists before the read, and "who would we
    read" is answerable without reading anyone.

    **"Gmail is down" and "nobody sent an SOW" are different reports.**
    Before the outcome buckets below, a revoked DWD grant produced
    ``owners_scanned: 7, owners_no_suggestions: 7, suggestions: 0,
    owners_failed: 0`` — a report an operator reads as a working detector over a
    quiet week, while seven live SOW commitments age toward a breach nobody is
    told about. The roster is now partitioned:

    ``owners`` = ``owners_scanned`` + ``owners_no_commitments`` +
    ``owners_mailbox_unavailable`` + ``owners_lanes_failed`` +
    ``owners_failed``, and ``owners_scanned`` = ``owners_with_suggestions`` +
    ``owners_no_suggestions``. ``owners_lanes_degraded`` is a *flag* over the
    scanned set, not a sixth partition: those mailboxes were searched with one
    Gmail lane missing, so their suggestions are real but their absences are not
    trustworthy.

    None of this claims the detector works. ``owners_no_suggestions`` stays
    ambiguous — a genuinely quiet week and a world where PandaDoc sends every
    SOW look the same from here. What the buckets remove is Cubie's own failures
    hiding inside that ambiguity.
    """
    from datetime import datetime, timedelta

    from sqlalchemy import func

    from rubik_core.config import settings
    from rubik_core.database.commitments import Commitment

    if not settings.COCKPIT_ENABLED:
        logger.info("[cockpit-sow-sent] COCKPIT_ENABLED is off — no mailbox read")
        return {
            "status": STATUS_DISABLED,
            "flag": "COCKPIT_ENABLED",
            "dry_run": bool(dry_run),
        }

    report: dict = {
        "status": STATUS_COMPLETED,
        "dry_run": bool(dry_run),
        "persisted": False,  # see the docstring — nothing stores a suggestion
        "owners": 0,
        # A mailbox was searched, whole (ok) or in part (lanes_degraded).
        "owners_scanned": 0,
        "owners_would_scan": 0,
        "owners_with_suggestions": 0,
        # Searched, and quiet. Only ever incremented for a mailbox we actually
        # read — this is the bucket the outage buckets below were stealing from.
        "owners_no_suggestions": 0,
        # No live SOW commitment, so the detector deliberately read nothing.
        # Reachable only via --emails; the resolved roster cannot contain one.
        "owners_no_commitments": 0,
        # We never looked: no Gmail service (revoked DWD grant / rotated key /
        # not a Workspace user), or every search lane raised.
        "owners_mailbox_unavailable": 0,
        "owners_lanes_failed": 0,
        # Searched with a lane missing — a flag over owners_scanned, not a
        # partition of the roster. Absences here are not trustworthy.
        "owners_lanes_degraded": 0,
        # Individual Gmail lane failures summed across the roster, so one AE
        # losing both lanes and two AEs losing one each are distinguishable.
        "lanes_failed": 0,
        "owners_failed": 0,
        "suggestions": 0,
        # commitment id + confidence only. Never the message, never the subject.
        "detail": [],
    }

    if owners:
        roster = sorted(
            {(e or "").strip().lower() for e in owners if (e or "").strip()}
        )
    else:
        # Exactly the detector's own precondition: it reads no mailbox unless the
        # owner has a live SOW commitment a suggestion could close.
        roster = sorted(
            {
                (email or "").strip().lower()
                for (email,) in db.query(func.lower(Commitment.owner_email))
                .filter(Commitment.kind == "sow", Commitment.closed_at.is_(None))
                .distinct()
                .all()
                if email
            }
        )
    report["owners"] = len(roster)

    if dry_run:
        report["owners_would_scan"] = len(roster)
        logger.info(
            "[cockpit-sow-sent] dry run: %d AE(s) hold a live SOW commitment; "
            "no mailbox was read",
            len(roster),
        )
        return report

    since = (
        datetime.utcnow() - timedelta(days=since_days)
        if since_days and since_days > 0
        else None
    )

    # Imported lazily on purpose: this module is written alongside the detector,
    # and a scheduler target that cannot even be imported is worse than one that
    # reports a failure bucket.
    from rubik_core.cockpit import sow_sent_detector

    # The detailed entry point, not the list-shaped one: the list cannot say
    # whether an empty result came from a quiet mailbox or from never having
    # opened one, and that distinction is this sweep's entire report.
    unscanned_commitments = 0
    for owner in roster:
        try:
            outcome = sow_sent_detector.scan_sent_mail_detailed(db, owner, since)
        except Exception:  # noqa: BLE001 — one mailbox, not the sweep
            db.rollback()
            report["owners_failed"] += 1
            logger.exception("[cockpit-sow-sent] scan failed for %s", owner)
            continue

        report["lanes_failed"] += outcome.lanes_failed

        if outcome.status == sow_sent_detector.SCAN_MAILBOX_UNAVAILABLE:
            report["owners_mailbox_unavailable"] += 1
            unscanned_commitments += outcome.live_commitments
            continue
        if outcome.status == sow_sent_detector.SCAN_LANES_FAILED:
            report["owners_lanes_failed"] += 1
            unscanned_commitments += outcome.live_commitments
            continue
        if outcome.status == sow_sent_detector.SCAN_NO_COMMITMENTS:
            # Only reachable with an explicit --emails roster: nothing to close,
            # so nothing was read. Not a failure and not a quiet mailbox.
            report["owners_no_commitments"] += 1
            continue

        report["owners_scanned"] += 1
        if outcome.degraded:
            report["owners_lanes_degraded"] += 1
        if outcome.suggestions:
            report["owners_with_suggestions"] += 1
            report["suggestions"] += len(outcome.suggestions)
            report["detail"].extend(
                {
                    "owner_email": owner,
                    "commitment_id": s.get("commitment_id"),
                    "confidence": s.get("confidence"),
                    "match_reasons": s.get("match_reasons"),
                }
                for s in outcome.suggestions
            )
        else:
            report["owners_no_suggestions"] += 1

    logger.info(
        "[cockpit-sow-sent] owners=%d scanned=%d with_suggestions=%d "
        "suggestions=%d no_commitments=%d mailbox_unavailable=%d "
        "lanes_failed=%d lanes_degraded=%d failed=%d (nothing persisted)",
        report["owners"],
        report["owners_scanned"],
        report["owners_with_suggestions"],
        report["suggestions"],
        report["owners_no_commitments"],
        report["owners_mailbox_unavailable"],
        report["owners_lanes_failed"],
        report["owners_lanes_degraded"],
        report["owners_failed"],
    )

    blind = report["owners_mailbox_unavailable"] + report["owners_lanes_failed"]
    if blind:
        # The signature of a revoked DWD grant or a rotated Workspace key: every
        # mailbox refuses at once. Loud, and separate from the summary, because
        # the cost is invisible — those commitments keep aging with nobody
        # offered the close a working detector would have found.
        logger.warning(
            "[cockpit-sow-sent] %d of %d AE(s) were NOT SCANNED (%d mailbox "
            "unavailable, %d all lanes failed), covering %d live SOW "
            "commitment(s). This is a Gmail delegation/quota failure, NOT "
            "'nobody sent an SOW'.",
            blind,
            report["owners"],
            report["owners_mailbox_unavailable"],
            report["owners_lanes_failed"],
            unscanned_commitments,
        )
    if report["owners_lanes_degraded"]:
        logger.warning(
            "[cockpit-sow-sent] %d AE(s) were scanned with a Gmail lane missing "
            "(%d lane failure(s) total) — suggestions found are real, absences "
            "are not",
            report["owners_lanes_degraded"],
            report["lanes_failed"],
        )
    if report["owners_scanned"] and not report["suggestions"]:
        # Deliberately NOT phrased as "the detector is working": if SOWs are
        # delivered by PandaDoc, no copy ever passes through the AE's Sent
        # folder and this lane finds nothing, forever.
        logger.info(
            "[cockpit-sow-sent] %d mailbox(es) read, zero suggestions — either a "
            "genuinely quiet window or SOWs that never pass through the AE's "
            "mailbox (nobody has confirmed a copy of the SOW lands there)",
            report["owners_scanned"],
        )
    return report


# ── Cockpit: the one place a sales notification is sent ──────────────────────


def run_digest(
    db,
    *,
    kind: str,
    dry_run: bool = False,
    recipients: list[str] | None = None,
) -> dict:
    """Send every AE their one digest of ``kind`` for their local today.

    Rule 1 is enforced by the database, not by this loop: a
    ``UniqueConstraint(recipient_email, kind, local_date)`` on ``digest_logs``
    decides a race between two scheduler executions, and ``kind`` is what stops
    Monday's weekly roll-up and Monday's daily digest from suppressing each
    other depending on which job fires first.

    **A fresh Session per recipient.** ``send_digest`` owns its transaction —
    INSERT + flush to claim the day's slot, send, then stamp ``sent_at`` and
    commit only after a CONFIRMED send — and its docstring requires a session
    with no other pending work in it. Sharing one session across the roster
    would let one recipient's rollback discard another's committed claim. ``db``
    is used to resolve the roster and nothing else.

    The recipient roster is "everyone who owns a live commitment", which is
    exactly the set ``gather_actionable`` can return anything for: stage-lag
    exceptions only reach a digest through a deal the person already holds a
    live commitment on. Someone with nothing actionable is reported as
    ``nothing_actionable`` and gets no message and no row — silence is a
    feature, and a row would burn their slot for the day.
    """
    from sqlalchemy import func

    from rubik_core.cockpit import digest as digest_mod
    from rubik_core.config import settings
    from rubik_core.database.commitments import DIGEST_KINDS, Commitment
    from rubik_core.database.db import SessionLocal

    if kind not in DIGEST_KINDS:
        raise ValueError(
            f"not a digest kind: {kind!r} (expected one of {DIGEST_KINDS})"
        )

    # A dry run deliberately ignores both flags, mirroring ``send_digest``:
    # reading what WOULD be sent is how anyone decides whether to flip
    # COCKPIT_DIGEST_ENABLED, so refusing to render while it is off would make
    # the flag un-reviewable.
    if not dry_run:
        if not settings.COCKPIT_ENABLED:
            logger.info("[cockpit-digest] COCKPIT_ENABLED is off — nothing sent")
            return {
                "status": STATUS_DISABLED,
                "flag": "COCKPIT_ENABLED",
                "kind": kind,
                "dry_run": False,
            }
        if not settings.COCKPIT_DIGEST_ENABLED:
            logger.info("[cockpit-digest] COCKPIT_DIGEST_ENABLED is off — nothing sent")
            return {
                "status": STATUS_DISABLED,
                "flag": "COCKPIT_DIGEST_ENABLED",
                "kind": kind,
                "dry_run": False,
            }

    # Buckets named from the digest module's own reasons so the two vocabularies
    # cannot drift. "sent" is the only one that is not a refusal.
    #
    # ONE rename, and it is load-bearing: ``REASON_DRY_RUN`` is the string
    # "dry_run", which is also this report's own boolean. Zero-filling the
    # buckets over the top of it turned ``dry_run: true`` into ``dry_run: 0`` —
    # a dry run reporting that it was not a dry run. Renamed on the way in rather
    # than dropped, because on a dry run that bucket is the entire result.
    bucket_for = {digest_mod.REASON_DRY_RUN: "rendered"}
    reasons = tuple(
        bucket_for.get(reason, reason)
        for reason in (
            digest_mod.REASON_COCKPIT_DISABLED,
            digest_mod.REASON_DIGEST_DISABLED,
            digest_mod.REASON_NOTHING_ACTIONABLE,
            digest_mod.REASON_ALREADY_SENT,
            digest_mod.REASON_DRY_RUN,
            digest_mod.REASON_NO_SLACK_BOT,
            digest_mod.REASON_NOT_A_MEMBER,
            digest_mod.REASON_SEND_FAILED,
        )
    )
    report: dict = {
        "status": STATUS_COMPLETED,
        "kind": kind,
        "dry_run": bool(dry_run),
        "recipients": 0,
        "sent": 0,
        "failed": 0,
        **{reason: 0 for reason in reasons},
        "detail": [],
    }

    if recipients:
        roster = sorted(
            {(e or "").strip().lower() for e in recipients if (e or "").strip()}
        )
    else:
        roster = sorted(
            {
                (email or "").strip().lower()
                for (email,) in db.query(func.lower(Commitment.owner_email))
                .filter(Commitment.closed_at.is_(None))
                .distinct()
                .all()
                if email
            }
        )
    report["recipients"] = len(roster)

    for email in roster:
        session = SessionLocal()
        try:
            result = digest_mod.send_digest(session, email, kind=kind, dry_run=dry_run)
        except Exception:  # noqa: BLE001 — one recipient, not the roster
            session.rollback()
            report["failed"] += 1
            logger.exception("[cockpit-digest] %s digest failed for %s", kind, email)
            continue
        finally:
            session.close()

        if result.get("sent"):
            report["sent"] += 1
        else:
            reason = result.get("reason") or "unknown"
            bucket = bucket_for.get(reason, reason)
            report[bucket] = report.get(bucket, 0) + 1
        report["detail"].append(
            {
                "recipient": email,
                "sent": bool(result.get("sent")),
                "reason": result.get("reason"),
                "item_count": result.get("item_count", 0),
                "suppressed_count": result.get("suppressed_count", 0),
            }
        )

    logger.info(
        "[cockpit-digest] kind=%s recipients=%d sent=%d nothing_actionable=%d "
        "already_sent=%d not_a_member=%d no_bot=%d send_failed=%d failed=%d "
        "dry_run=%s",
        kind,
        report["recipients"],
        report["sent"],
        report[digest_mod.REASON_NOTHING_ACTIONABLE],
        report[digest_mod.REASON_ALREADY_SENT],
        report[digest_mod.REASON_NOT_A_MEMBER],
        report[digest_mod.REASON_NO_SLACK_BOT],
        report[digest_mod.REASON_SEND_FAILED],
        report["failed"],
        dry_run,
    )
    if report[digest_mod.REASON_NO_SLACK_BOT] and report["recipients"]:
        # The bot token lives in Cloud SQL (SlackCredential.bot_token), not in
        # an env var, so "unavailable for everyone" means no install row was
        # found — the Slack app is not installed, or this environment's
        # database has no credential. No digest_logs rows were written, so the
        # next run retries everyone.
        logger.warning(
            "[cockpit-digest] the Slack bot was unavailable for all %d "
            "recipient(s) — no workspace bot token is stored (is the Slack app "
            "installed in this environment?)",
            report["recipients"],
        )
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────


#: The sweep names, in the order an operator meets them. Deliberately IDENTICAL
#: to the ``WORKER_JOBS`` keys in ``backend/jobs/__init__.py``, so
#: ``worker_job_execute_command("ccs-sweep")`` and
#: ``python -m backend.jobs.sales_kaizen ccs-sweep`` name the same thing and a
#: runbook does not carry two vocabularies for one list.
SWEEPS: tuple[str, ...] = (
    "ccs-sweep",
    "cockpit-recompute",
    "cockpit-stage-lag",
    "cockpit-sow-sent",
    "cockpit-daily-digest",
    "cockpit-weekly-aging",
)


def _emails(value: str) -> list[str]:
    return [part.strip().lower() for part in value.split(",") if part.strip()]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sales_kaizen",
        description=(
            "One sweep of Sales Call Capture / Sales Cockpit maintenance. The "
            "same functions the Cloud Scheduler endpoints on "
            "backend/routers/agents.py call."
        ),
    )
    p.add_argument(
        "sweep",
        choices=list(SWEEPS),
        help="Which sweep to run.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report what WOULD happen and write nothing. Exact rather than "
            "re-implemented: the aging sweep rolls its own transaction back, "
            "the CCS sweep skips ingest and every LLM call, and the SOW-sent "
            "scan resolves the roster without reading a single mailbox."
        ),
    )
    p.add_argument(
        "--lookback-hours",
        type=int,
        default=None,
        help=(
            "ccs-sweep only. Overrides CCS_POLL_LOOKBACK_HOURS. Widen freely — "
            "ingest is idempotent, so overlap costs one read quota unit and "
            "writes nothing, while a missed window is a permanently lost call "
            "after Google's 30-day deletion."
        ),
    )
    p.add_argument(
        "--since-days",
        type=int,
        default=None,
        help=(
            "cockpit-sow-sent only. How far back in Sent mail to look "
            "(default: the detector's own 30-day window)."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "cockpit-stage-lag only. Cap how many evidenced deals this run "
            "reads from HubSpot. Anything past the cap is reported as "
            "deals_deferred and picked up next run."
        ),
    )
    p.add_argument(
        "--emails",
        type=_emails,
        default=None,
        help=(
            "Scope a digest or the SOW-sent scan to these addresses instead of "
            "resolving the roster. A targeted re-send / re-scan."
        ),
    )
    return p


def _dispatch(sweep: str, db, args: argparse.Namespace) -> dict:
    if sweep == "ccs-sweep":
        return run_ccs_sweep(
            db, lookback_hours=args.lookback_hours, dry_run=args.dry_run
        )
    if sweep == "cockpit-recompute":
        return run_recompute_states(db, dry_run=args.dry_run)
    if sweep == "cockpit-stage-lag":
        return run_stage_lag(db, dry_run=args.dry_run, limit=args.limit)
    if sweep == "cockpit-sow-sent":
        return run_sow_sent(
            db,
            dry_run=args.dry_run,
            since_days=args.since_days,
            owners=args.emails,
        )
    if sweep == "cockpit-daily-digest":
        return run_digest(
            db, kind="daily", dry_run=args.dry_run, recipients=args.emails
        )
    if sweep == "cockpit-weekly-aging":
        return run_digest(
            db, kind="weekly", dry_run=args.dry_run, recipients=args.emails
        )
    raise ValueError(f"unknown sweep: {sweep!r}")  # unreachable via argparse


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred: pulls the app graph (DB, settings, Vertex). Keeps --help fast and
    # surfaces an import failure inside the task rather than at parse time.
    from rubik_core.database.db import SessionLocal

    logger.info("sales_kaizen start sweep=%s dry_run=%s", args.sweep, args.dry_run)
    session = SessionLocal()
    try:
        report = _dispatch(args.sweep, session, args)
    except Exception:  # noqa: BLE001 — trace it, then fail the task loudly
        session.rollback()
        logger.exception("sales_kaizen failed sweep=%s", args.sweep)
        return EXIT_ERROR
    else:
        # The full report on stdout, not just the log line: a hand-run sweep is
        # usually being run *because* somebody needs the buckets.
        print(json.dumps(report, indent=2, default=str))
        logger.info(
            "sales_kaizen done sweep=%s status=%s", args.sweep, report.get("status")
        )
        return EXIT_OK
    finally:
        session.close()
        # Separate process: flush the AI-cost ledger explicitly, since a
        # terminated task may skip the atexit handler. ccs-sweep bills Gemini
        # Pro calls to "call_capture_extraction" via run_extraction's
        # @attribute_to, and dropping this flush is how a feature's spend lands
        # in the AI Cost tab's "Unattributed" row.
        try:
            from rubik_core.cost.usage import flush_usage

            flush_usage()
        except Exception:  # noqa: BLE001 — cost accounting never fails the job
            logger.debug("sales_kaizen: cost flush failed", exc_info=True)


if __name__ == "__main__":
    sys.exit(main())
