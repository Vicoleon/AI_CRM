"""The polling sweep — how Cubie finds out a sales call happened.

This is the PRIMARY ingest path, not a backstop. The first design was a Cloud
Pub/Sub push receiver fed by per-user Workspace Events subscriptions; v1 polls
``conferenceRecords.list`` on Cloud Scheduler instead, because there are zero
Pub/Sub push receivers in this repo, every scheduled endpoint in Cubie
authenticates on ``X-Scheduler-Key``, and
``rubik_core/scripts/transcript_sync.py`` already sweeps Meet every 15 minutes.
Polling costs latency (seconds becomes <=15 minutes, against a wrap-up that is a
next-morning workflow) and removes the design's most fragile part: a missed
subscription renewal makes Google PERMANENTLY delete the subscription.

The sweep does what the three event handlers were going to do, driven by what it
finds rather than by a ``ce-type`` match:

* **resolve or create the session.** A conference record with no matching
  ``call_sessions`` row becomes one with ``origin="adhoc"``. This is what closes
  gate 7 — a call the AE started directly in Meet is still recorded. The company
  is NOT guessed from attendee email domains; it is left NULL and the wrap-up
  asks "which company was this call about?", because a wrong binding writes a
  proposal against the wrong CRM record and the AE has no way to see that from
  the wrap-up screen.
* **ingest the transcript** and run the extractor when one exists — but only
  for a conference that has ENDED and a transcript that has stopped growing.
  Reading a live call gives you a prefix of it that looks, ever afterwards,
  exactly like the whole thing.
* **open the wrap-up commitment** the call owes its AE, on both the transcript
  path and the no-transcript one. This is the soft block: the wrap-up ages,
  breaches onto the sales supervisor's worklist and escalates, rather than
  locking the AE out of anything.
* **age out the silence.** A session that ended more than 30 minutes ago with
  zero transcript entries becomes ``no_transcript``. That is NOT an error
  state: the host may have turned transcription off, the call may have
  been under a minute, or it may have been confidential. The wrap-up still
  opens, empty, and the AE logs the call by hand.

**Reading the report.** Every function here returns named outcome buckets, never
a bare count, because the two failures that matter are indistinguishable from
success in a total: before the Meet scopes are granted on the DWD client id
EVERY user 403s, and that has to read as "scope not granted", not as
"nobody had any calls". ``permission_denied`` is therefore its own bucket with
its own log line, and one AE's 403 never aborts the sweep for the rest. The
same rule is why ``ingest_incomplete`` exists: a call whose entries landed but
whose corpus document did not is a LOST call, and reporting it as
``already_ingested`` is a clean report over a hole.

🔴 Google deletes conference records, transcripts and transcript entries 30 days
after the conference ends. A window missed during a scheduler outage is a
permanently lost call, which is why ``CCS_POLL_LOOKBACK_HOURS`` defaults to 24
against a 15-minute cadence. Re-reading a conference costs one read quota unit
and writes nothing, because ingest is idempotent — widen the window freely,
never narrow it to save API calls.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload, sessionmaker

from rubik_core.call_capture import transcript_ingest
from rubik_core.call_capture.provenance import stamp_session_source
from rubik_core.call_capture.transcript_ingest import ingest
from rubik_core.config import settings
from rubik_core.database.call_capture import CallSession, CallTranscriptEntry
from rubik_core.database.models import User
from rubik_core.tools.meet import GoogleMeetClient

logger = logging.getLogger(__name__)

#: How long after a call ends Cubie gives up waiting for a transcript. Google
#: publishes entries within a couple of minutes of the conference ending, so 30
#: is generous; the cost of being wrong is a session that says "no transcript"
#: and then gets one on a later pass, which the ingest handles because
#: ``no_transcript`` is not read as terminal by anything except the wrap-up list.
NO_TRANSCRIPT_AFTER_MINUTES = 30

#: Statuses the aging stage leaves alone. Everything here either already has
#: entries or has been decided by a human; ``scheduled`` / ``in_progress`` /
#: ``ended`` are the only three a sweep may move to ``no_transcript``.
_AGED_OUT_TERMINAL = (
    "transcript_ready",
    "extracted",
    "wrapped",
    "no_transcript",
    "failed",
)

#: Extraction is one Gemini PRO call per call session. The scheduled endpoint
#: runs the sweep synchronously under Cloud Run's 300s request cap, so a backlog
#: (the first pass after the flag flips, or after a scheduler outage) is spread
#: across passes rather than attempted in one. Sessions left at
#: ``transcript_ready`` are picked up next pass; nothing is lost.
MAX_EXTRACTIONS_PER_USER_RUN = 5

# Longer than the Cloud Run job's 300-second request cap. A process killed
# after claiming the row cannot strand the call indefinitely.
EXTRACTION_LEASE_MINUTES = 20
MANUAL_EXTRACTION_RETRY_COOLDOWN_SECONDS = 60

#: WHO the sweep reads: exactly the people Admin → Feature Access admits to
#: Sales Call Capture (``rubik_core/features/registry.py``) — the same roster
#: every ``/api/calls`` endpoint checks. It used to be "everyone in Sales, plus
#: anyone who owns a session", written before the roster was data. That read
#: the Meet calls of AEs who could not open the wrap-ups it produced, which the
#: feature's own rule forbids (an AE must not be the one to find out how capture
#: behaves on a real client call). Opening capture to Sales is a click on that
#: page now, and the sweep follows it with no deploy.
CCS_FEATURE_KEY = "sales_call_capture"

#: A lookback longer than Meet's own retention finds nothing to backfill.
_MAX_LOOKBACK_HOURS = 30 * 24

#: ``Transcript.state`` values that mean the transcription session is FINISHED
#: and the entry list is final. A host who starts transcription at 10:00 on a
#: 50-minute call has a ``STARTED`` transcript from minute one, and its entry
#: list grows for the next fifty minutes.
_SETTLED_TRANSCRIPT_STATES = frozenset({"ENDED", "FILE_GENERATED"})

#: SLA snapshot for the wrap-up commitment, in BUSINESS hours ("1 business
#: day"). Passed explicitly rather than resolved from ``commitment_sla_configs``
#: because the wrap-up's number is pinned at the call site; the config row still
#: wins for every other kind.
WRAPUP_SLA_HOURS = 8

#: Session statuses that owe their AE a wrap-up. ``wrapped`` is absent because
#: the obligation is discharged, and ``failed`` because nothing was captured.
_WRAPUP_OWED_STATUSES = ("transcript_ready", "extracted", "no_transcript")


def _is_permission_denied(exc: Exception) -> bool:
    """Is this the ungranted-scope verdict rather than a transient failure?

    Worth its own classifier because it is the expected state of every
    environment until the Meet scopes are granted, and a sweep reporting "0
    conference records" for that reason looks exactly like a quiet Tuesday.
    Matches on the HTTP status where there is one
    (``googleapiclient.errors.HttpError.resp.status``) and on the OAuth error
    strings otherwise — an ungranted DWD scope surfaces as a ``RefreshError``
    carrying ``unauthorized_client``, with no status at all. A false positive
    mislabels a bucket; it changes no behaviour.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in (401, 403):
        return True
    if getattr(exc, "status_code", None) in (401, 403):
        return True
    blob = str(exc).lower()
    return any(
        token in blob
        for token in (
            "permission_denied",
            "insufficientpermissions",
            "insufficient permission",
            "access_token_scope_insufficient",
            "unauthorized_client",
            "forbidden",
            "403",
        )
    )


def _space_of(record: dict) -> tuple[str | None, str | None]:
    """``(space_name, meeting_code)`` out of a conference record.

    ``space`` is documented as the space resource name, but the discovery client
    has returned it as an embedded object on some responses, so both shapes are
    read. The meeting code is frequently absent, which is why space name is the
    primary key for resolution and the code is only a fallback.
    """
    space = record.get("space")
    if isinstance(space, dict):
        return space.get("name"), space.get("meetingCode")
    if isinstance(space, str):
        return space, None
    return None, None


def _resolve_session(
    db: Session,
    *,
    ae_email: str,
    conference_record: str,
    space_name: str | None,
    meeting_code: str | None,
) -> CallSession | None:
    """Find the ``call_sessions`` row this conference record belongs to.

    Order matters. ``conference_record`` is UNIQUE and is the sweep's natural
    key, so a session already bound to this record is the answer and re-running
    the sweep is free. Only then does the space match, and only against a
    session that is not already bound to a DIFFERENT record: recurring meetings
    reuse ONE space, so every past occurrence shares a ``meet_space_name``, and
    without that filter the second occurrence would try to stamp its record onto
    the first occurrence's session and violate the unique constraint.
    """
    row = (
        db.query(CallSession)
        .filter(CallSession.conference_record == conference_record)
        .first()
    )
    if row is not None:
        return row

    unbound = db.query(CallSession).filter(
        CallSession.conference_record.is_(None),
        func.lower(CallSession.ae_email) == ae_email,
    )
    if space_name:
        row = unbound.filter(CallSession.meet_space_name == space_name).first()
        if row is not None:
            return row
    if meeting_code:
        row = (
            unbound.filter(
                func.lower(CallSession.meet_meeting_code) == meeting_code.lower()
            )
            .order_by(CallSession.created_at.desc())
            .first()
        )
        if row is not None:
            return row
    return None


def _space_name_free(db: Session, space_name: str | None) -> bool:
    """True when no session already claims ``space_name``.

    ``call_sessions.meet_space_name`` is UNIQUE, and two ordinary things put a
    second conference in a space that already has a session: the next
    occurrence of a recurring series (ONE space for the whole series) and a
    second AE on the same call. Storing the name again violated that constraint,
    so the second conference never got a session and failed again on every
    pass. The row matters more than the denormalised name — this record's
    ``conference_record`` is unique and is what :func:`_resolve_session` matches
    first. Same rule as ``backend/routers/calls.py::_space_name_free``, which
    ``rubik_core`` may not import.
    """
    if not space_name:
        return False
    return (
        db.query(CallSession.id)
        .filter(CallSession.meet_space_name == space_name)
        .first()
        is None
    )


def _new_report(user_email: str, dry_run: bool) -> dict:
    return {
        "user": user_email,
        "dry_run": dry_run,
        # One of: swept | ccs_disabled | meet_disabled | meet_unavailable |
        # permission_denied | failed.
        "outcome": "swept",
        "error": None,
        "conference_records": 0,
        "records_skipped": 0,
        "records_failed": 0,
        "sessions_created": 0,
        "sessions_matched": 0,
        "sessions_stamped": 0,
        "suppressed": 0,
        "still_live": 0,
        "awaiting_transcript": 0,
        "transcripts_in_progress": 0,
        "already_ingested": 0,
        # Entries are stored but the DAM asset / RAG import did not complete:
        # the call is in the citation index and invisible to Chat, RAG and the
        # graph. Its own bucket because the alternative — counting it as
        # ``already_ingested`` — is a clean report over a lost call.
        "ingest_incomplete": 0,
        "transcripts_seen": 0,
        "entries_written": 0,
        "would_ingest": 0,
        "extractions_run": 0,
        "extractions_empty": 0,
        "extractions_failed": 0,
        "extractions_deferred": 0,
        "no_transcript_marked": 0,
        "wrapups_opened": 0,
        "wrapups_existing": 0,
        "wrapups_failed": 0,
    }


def sweep_user(
    db: Session, user_email: str, *, lookback_hours: int, dry_run: bool = False
) -> dict:
    """Sweep one AE's Meet conference records and age out their silent calls.

    Two stages, in this order and for a reason. The Meet stage resolves,
    ingests and extracts; the aging stage then marks what is still empty. Run
    the other way round and a call that finished four minutes ago would be
    stamped ``no_transcript`` in the same pass that ingested it.

    The aging stage runs even when the Meet stage could not — a Meet grant that
    is missing or revoked does not make a call that ended two hours ago any less
    silent, and the AE still needs their empty wrap-up. It is pure DB work.

    Commits as it goes (``transcript_ingest.ingest`` commits too, by design), so
    one AE's failure never discards another's ingested transcript.
    """
    email = (user_email or "").strip().lower()
    report = _new_report(email, dry_run)
    if not settings.CCS_ENABLED:
        # A half-provisioned environment must not half-run the feature.
        report["outcome"] = "ccs_disabled"
        return report

    hours = max(1, min(int(lookback_hours or 0), _MAX_LOOKBACK_HOURS))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    if not settings.CCS_MEET_ENABLED:
        report["outcome"] = "meet_disabled"
    else:
        try:
            client = GoogleMeetClient(user_email=email)
            if client.service is None:
                # No Workspace key, or the token refresh failed. Distinct from a
                # 403: nothing was even attempted.
                report["outcome"] = "meet_unavailable"
            else:
                _sweep_conference_records(db, email, client, since, report, dry_run)
        except Exception as exc:  # noqa: BLE001 — classified, then reported
            db.rollback()
            if _is_permission_denied(exc):
                report["outcome"] = "permission_denied"
                logger.warning(
                    "[ccs-sweep] Meet scope not granted for %s — every Meet call "
                    "403s until the four Meet scopes are granted on the DWD "
                    "client id. This is NOT 'no calls'. %s",
                    email,
                    exc,
                )
            else:
                report["outcome"] = "failed"
                report["error"] = f"{type(exc).__name__}: {exc}"
                logger.exception("[ccs-sweep] Meet sweep failed for %s", email)

    try:
        report["no_transcript_marked"] = _mark_no_transcript(
            db, email, dry_run, report, since=_naive(since)
        )
    except Exception as exc:  # noqa: BLE001 — the Meet result is still worth returning
        db.rollback()
        report["error"] = report["error"] or f"{type(exc).__name__}: {exc}"
        logger.exception("[ccs-sweep] no-transcript aging failed for %s", email)

    logger.info(
        "[ccs-sweep] %s outcome=%s records=%d created=%d live=%d entries=%d "
        "extracted=%d incomplete=%d no_transcript=%d wrapups=%d/%d/%d "
        "dry_run=%s",
        email,
        report["outcome"],
        report["conference_records"],
        report["sessions_created"],
        report["still_live"],
        report["entries_written"],
        report["extractions_run"],
        report["ingest_incomplete"],
        report["no_transcript_marked"],
        report["wrapups_opened"],
        report["wrapups_existing"],
        report["wrapups_failed"],
        dry_run,
    )
    return report


def _sweep_conference_records(
    db: Session,
    email: str,
    client: GoogleMeetClient,
    since: datetime,
    report: dict,
    dry_run: bool,
) -> None:
    """Walk the window's conference records for one impersonated AE.

    A per-record failure is counted and skipped — one malformed record must not
    cost the other calls in the window — but a permission failure is re-raised,
    because it is a verdict on the whole user rather than on one record and the
    caller classifies it into its own bucket.
    """
    records = client.list_conference_records(start_after=since)
    report["conference_records"] = len(records)
    for record in records:
        try:
            _sweep_record(db, email, client, record, report, dry_run)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            if _is_permission_denied(exc):
                raise
            db.rollback()
            report["records_failed"] += 1
            logger.exception(
                "[ccs-sweep] record %s failed for %s", record.get("name"), email
            )


def _sweep_record(
    db: Session,
    email: str,
    client: GoogleMeetClient,
    record: dict,
    report: dict,
    dry_run: bool,
) -> None:
    """Resolve/create one session from a conference record, then ingest it.

    Three gates stand between a conference record and an ingest, and each one
    exists because skipping it produces something that looks finished:

    1. the conference must have ENDED (``endTime``), or the ingest captures a
       prefix of a live call and locks the rest out for ever;
    2. the transcript must have stopped growing (:func:`_is_settled`), because
       a host still running transcription has the same shape;
    3. the transcript must not already be COMPLETE — where complete means what
       ingest recorded, not whether entry rows exist.
    """
    conference_record = record.get("name")
    if not conference_record:
        report["records_skipped"] += 1
        return

    space_name, meeting_code = _space_of(record)
    started = _parse_ts(record.get("startTime"))
    ended = _parse_ts(record.get("endTime"))

    session = _resolve_session(
        db,
        ae_email=email,
        conference_record=conference_record,
        space_name=space_name,
        meeting_code=meeting_code,
    )
    if session is None:
        report["sessions_created"] += 1
        if dry_run:
            return
        # The ad-hoc path, and the one that makes "a call is never invisible"
        # true. ``hubspot_company_id`` stays NULL on purpose — see the module
        # docstring.
        session = CallSession(
            id=uuid.uuid4().hex,
            ae_email=email,
            origin="adhoc",
            status="ended" if ended else "in_progress",
            capture_consent="granted",
            # NULL when another session already holds the space — see
            # :func:`_space_name_free`. The conference record is the key here.
            meet_space_name=space_name if _space_name_free(db, space_name) else None,
            meet_meeting_code=meeting_code,
            conference_record=conference_record,
            started_at=_naive(started),
            ended_at=_naive(ended),
            attrs={
                "discovered_by": "sweep",
                "discovered_at": _now_iso(),
                "crm_provenance": stamp_session_source(None, None, None),
            },
        )
        db.add(session)
        db.commit()
    else:
        report["sessions_matched"] += 1
        if not dry_run and _stamp(session, conference_record, started, ended):
            report["sessions_stamped"] += 1
            db.commit()

    if (session.capture_consent or "granted") == "suppressed":
        # A confidential call. The fact of it is recorded; the content is never
        # read, so the transcript is not even listed. Its wrap-up is opened by
        # the aging stage, which is where a suppressed call lands.
        report["suppressed"] += 1
        return

    if ended is None:
        # ── THE CALL IS STILL HAPPENING ─────────────────────────────────────
        # Do not list, do not ingest, do not extract. A conference record with
        # no ``endTime`` is a live call, and its transcript is a growing
        # prefix: sweeping a 50-minute call at minute 20 would ingest the first
        # 20 minutes, mark the session ``transcript_ready``, extract from the
        # fragment, and every later pass would find the work done. The SOW ask
        # and the pricing question in the last half hour would never be read,
        # and Google deletes the source after 30 days. A partial transcript is
        # worse than no transcript: the empty case is visible, the partial one
        # looks complete.
        report["still_live"] += 1
        return

    transcripts = client.list_transcripts(conference_record)
    settled = [
        t
        for t in transcripts
        if isinstance(t, dict) and t.get("name") and _is_settled(t)
    ]
    in_progress = len(transcripts) - len(settled)
    report["transcripts_in_progress"] += in_progress
    if in_progress:
        # The conference ended but a transcription session has not been closed
        # out yet — Meet finalises within a couple of minutes. Waiting costs a
        # pass; reading it costs the tail of the call.
        logger.info(
            "[ccs-sweep] %s: %d transcript(s) of %s still transcribing — "
            "waiting for the final entry list",
            email,
            in_progress,
            conference_record,
        )
    if not settled:
        # Nothing final to ingest yet. Either still being published or never
        # transcribed; the aging stage decides which after 30 minutes.
        report["awaiting_transcript"] += 1
        return

    report["transcripts_seen"] += len(settled)
    if dry_run:
        report["would_ingest"] += len(settled)
        return

    names = [t["name"] for t in settled]
    # ── THE RECOVERY GATE ───────────────────────────────────────────────────
    # What ingest COMPLETED, never "does this session have entries". Entries
    # are committed before the DAM asset and the participant ACL are attempted,
    # so an entries-based gate reports a call whose Vertex import timed out as
    # ``already_ingested`` on every subsequent pass — for ever, with
    # ``records_failed: 0``.
    pending = [n for n in names if transcript_ingest.needs_ingest(session, n)]

    written = 0
    for name in pending:
        written += ingest(db, session, name, client)
    report["entries_written"] += written

    if written or session.status in ("scheduled", "in_progress", "ended"):
        session.status = "transcript_ready"
        db.commit()

    stranded = [n for n in names if not transcript_ingest.is_complete(session, n)]
    if stranded:
        report["ingest_incomplete"] += 1
        logger.error(
            "[ccs-sweep] call %s (%s): %d transcript(s) have entries but no "
            "corpus document — Chat, RAG and the graph cannot see this call. "
            "Attempts so far: %s (cap %d).",
            session.id,
            conference_record,
            len(stranded),
            [transcript_ingest.attempts(session, n) for n in stranded],
            transcript_ingest.MAX_ASSET_ATTEMPTS,
        )
    elif not pending:
        report["already_ingested"] += 1

    # The wrap-up is owed the moment the call has a transcript, and this is the
    # polled design's ``conference.v2.ended``.
    if session.status in _WRAPUP_OWED_STATUSES:
        _open_wrapup_commitment(db, session, report)
    # New utterances, or a session whose entries landed but whose extraction
    # never ran (an LLM failure, a process killed mid-pass) and which would
    # otherwise sit at ``transcript_ready`` for ever, never reaching an AE's
    # wrap-up list. Deliberately NOT "we attempted an ingest": a call being
    # re-attempted for its corpus document alone must not buy a second PRO call
    # every fifteen minutes.
    if (
        written
        or session.status == "transcript_ready"
        or _extraction_is_stale(db, session)
    ):
        _run_extraction(db, session, report, dry_run)


def _is_settled(transcript: dict) -> bool:
    """Is this transcript finished, or is the host still transcribing?

    ``state`` is the answer when Meet sends one (``STATE_UNSPECIFIED``,
    ``STARTED``, ``ENDED``, ``FILE_GENERATED``). When it does not, ``endTime``
    is: Meet stamps it when the transcription session stops. Neither present
    means "assume it is still running", which costs a pass and never costs the
    tail of a call — the asymmetry that matters, because an unfinished
    transcript that gets ingested anyway is indistinguishable from a complete
    one afterwards.
    """
    state = str(transcript.get("state") or "").strip().upper()
    if state in _SETTLED_TRANSCRIPT_STATES:
        return True
    if state == "STARTED":
        return False
    return bool(transcript.get("endTime"))


def _open_wrapup_commitment(db: Session, session: CallSession, report: dict) -> None:
    """Open the ``wrapup`` commitment this call owes its AE.

    THE SOFT BLOCK. Without this producer the wrap-up never ages, never
    breaches, never lands on the sales supervisor's worklist and never
    escalates — the whole "soft block, not hard block" decision would be one
    that was made, argued for, and then not built.

    Called on every pass while the call is unwrapped, which is safe by
    construction rather than by discipline: ``open_commitment`` folds a repeat
    into the live row through the commitments table's two partial unique
    constraints, and for the ad-hoc call that has neither company nor deal it
    dedupes on ``opened_evidence_ref`` — which is why ``evidence_ref`` is the
    session id and must stay that. Calling it every pass is also what makes a
    transient failure here recoverable at all.

    Never opened for a wrapped session: that obligation is discharged, and a
    fresh commitment for it would age towards a breach nobody can clear.

    Contained rather than raised: the transcript is already ingested and the
    AE's wrap-up screen works without the commitment, so a cockpit failure must
    not turn an ingested call into ``records_failed``.
    """
    if session.wrapped_at is not None or session.status == "wrapped":
        return
    from sqlalchemy import func as _func

    from rubik_core.cockpit.commitment_engine import open_commitment
    from rubik_core.database.commitments import Commitment

    try:
        before = db.query(_func.count(Commitment.id)).scalar() or 0
        open_commitment(
            db,
            kind="wrapup",
            owner_email=(session.ae_email or "").strip().lower(),
            company_id=session.hubspot_company_id,
            deal_id=session.hubspot_deal_id,
            # NULL when the sweep never learned when the call ended: an unknown
            # age does not age and does not escalate, which is the engine's own
            # rule and better than dating the obligation to "now".
            opened_at=session.ended_at,
            source="call",
            evidence_ref=str(session.id),
            sla_hours=WRAPUP_SLA_HOURS,
        )
        db.commit()
        after = db.query(_func.count(Commitment.id)).scalar() or 0
    except Exception as exc:  # noqa: BLE001 — see the docstring
        db.rollback()
        report["wrapups_failed"] += 1
        logger.exception(
            "[ccs-sweep] could not open the wrap-up commitment for call %s "
            "(%s) — this call will not age onto anyone's worklist this pass",
            session.id,
            exc,
        )
        return
    report["wrapups_opened" if after > before else "wrapups_existing"] += 1


def _entry_count(db: Session, session_id: str) -> int:
    return (
        db.query(func.count(CallTranscriptEntry.id))
        .filter(CallTranscriptEntry.call_session_id == session_id)
        .scalar()
        or 0
    )


def _extraction_is_stale(db: Session, session: CallSession) -> bool:
    """Find new entries even if ingest died before it reset the call's status."""
    previous = session.last_extracted_entry_count
    return previous is not None and _entry_count(db, session.id) > previous


def _claim_extraction(
    db: Session, session: CallSession, *, manual_retry: bool
) -> tuple[str, int] | None:
    """Compare-and-swap one session before any billable model request.

    The status and both time gates live in one UPDATE, so a sweep on another
    instance and a double-click of the manual retry cannot both win. The
    A random claim id stays stable while the lease deadline is renewed.
    """
    now = datetime.utcnow()
    until = now + timedelta(minutes=EXTRACTION_LEASE_MINUTES)
    claim_id = uuid.uuid4().hex
    starting_status = session.status
    previous_count = session.last_extracted_entry_count
    if starting_status not in ("transcript_ready", "extracted", "wrapped"):
        db.rollback()
        return None
    if (
        session.status in ("extracted", "wrapped")
        and session.last_extracted_entry_count is not None
        and not _extraction_is_stale(db, session)
    ):
        db.rollback()
        return None
    query = db.query(CallSession).filter(
        CallSession.id == session.id,
        CallSession.status == starting_status,
        or_(
            CallSession.extraction_claimed_until.is_(None),
            CallSession.extraction_claimed_until <= now,
        ),
    )
    if starting_status in ("extracted", "wrapped"):
        query = query.filter(
            CallSession.last_extracted_entry_count.is_(None)
            if previous_count is None
            else CallSession.last_extracted_entry_count == previous_count
        )
    values = {
        CallSession.extraction_claimed_until: until,
        CallSession.extraction_claim_id: claim_id,
    }
    if manual_retry:
        query = query.filter(
            or_(
                CallSession.extraction_manual_retry_after.is_(None),
                CallSession.extraction_manual_retry_after <= now,
            )
        )
        values[CallSession.extraction_manual_retry_after] = now + timedelta(
            seconds=MANUAL_EXTRACTION_RETRY_COOLDOWN_SECONDS
        )
    with db.no_autoflush:
        changed = query.update(values, synchronize_session=False)
    if changed != 1:
        db.rollback()
        return None
    db.commit()
    db.refresh(session)
    return claim_id, _entry_count(db, session.id)


def _release_extraction_claim(db: Session, session: CallSession, claim_id: str) -> bool:
    """Release only this run's claim; an expired claim may have a new owner."""
    with db.no_autoflush:
        changed = (
            db.query(CallSession)
            .filter(
                CallSession.id == session.id,
                CallSession.extraction_claim_id == claim_id,
            )
            .update(
                {
                    CallSession.extraction_claimed_until: None,
                    CallSession.extraction_claim_id: None,
                },
                synchronize_session=False,
            )
        )
    return changed == 1


def _renew_extraction_claim(db: Session, session_id: str, claim_id: str) -> bool:
    """Extend a live lease from a separate, workspace-pinned DB session."""
    now = datetime.utcnow()
    changed = (
        db.query(CallSession)
        .filter(
            CallSession.id == session_id,
            CallSession.extraction_claim_id == claim_id,
            CallSession.extraction_claimed_until > now,
        )
        .update(
            {
                CallSession.extraction_claimed_until: now
                + timedelta(minutes=EXTRACTION_LEASE_MINUTES)
            },
            synchronize_session=False,
        )
    )
    if changed != 1:
        db.rollback()
        return False
    db.commit()
    return True


def _start_extraction_heartbeat(
    db: Session, session_id: str, claim_id: str
) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """Keep a long, multi-chunk extraction claimed without sharing a Session."""
    stop = threading.Event()
    lost = threading.Event()
    bind = db.get_bind()
    context = contextvars.copy_context()

    def heartbeat() -> None:
        SessionFactory = sessionmaker(bind=bind)
        while not stop.wait(60):
            try:
                with SessionFactory() as renewal_db:
                    if not _renew_extraction_claim(renewal_db, session_id, claim_id):
                        lost.set()
                        return
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[ccs-sweep] extraction lease renewal failed for call %s (%s)",
                    session_id,
                    type(exc).__name__,
                )
                lost.set()
                return

    worker = threading.Thread(
        target=lambda: context.run(heartbeat),
        name="ccs-extraction-lease",
        daemon=True,
    )
    worker.start()
    return stop, lost, worker


def _run_extraction(
    db: Session,
    session: CallSession,
    report: dict,
    dry_run: bool,
    *,
    manual_retry: bool = False,
) -> None:
    """Run the extractor once for a session whose transcript just landed.

    Imported lazily: ``extraction`` imports the prompt module and the cost
    decorator, and importing it at module scope makes this module and the
    extractor mutually dependent through the package.
    """
    if dry_run:
        return
    # The cap counts ATTEMPTS, not successes: an extraction that proposes nothing
    # still spent a PRO call, and a pass full of empty transcripts would
    # otherwise blow through the 300s request budget unchecked.
    attempts = (
        report["extractions_run"]
        + report["extractions_empty"]
        + report["extractions_failed"]
    )
    if attempts >= MAX_EXTRACTIONS_PER_USER_RUN:
        report["extractions_deferred"] += 1
        return

    claim = _claim_extraction(db, session, manual_retry=manual_retry)
    if claim is None:
        report["extractions_deferred"] += 1
        return
    claim_id, entry_count = claim

    from rubik_core.call_capture.extraction import run_extraction

    stop, lost, heartbeat = _start_extraction_heartbeat(db, session.id, claim_id)
    error: Exception | None = None
    extraction = None
    try:
        extraction = run_extraction(db, session)
    except Exception as exc:  # noqa: BLE001 — one bad transcript must not stop the sweep
        error = exc
    finally:
        stop.set()
        heartbeat.join(timeout=5)

    if heartbeat.is_alive() or lost.is_set():
        db.rollback()
        report["extractions_failed"] += 1
        logger.error("[ccs-sweep] extraction lease lost for call %s", session.id)
        return

    if error is not None:
        db.rollback()
        report["extractions_failed"] += 1
        # ``failed`` is reserved for the session being unusable; the transcript
        # is fine and the next pass retries, so the status stays put. Persist
        # only the retry state. SDK errors can echo transcript text or key
        # material, so neither the exception nor its traceback is logged here.
        session.attrs = {
            **(session.attrs or {}),
            "extraction_retry": {"failed_at": _now_iso()},
        }
        if _release_extraction_claim(db, session, claim_id):
            db.commit()
        else:
            # A successor claimed an expired lease. Never overwrite its state.
            db.rollback()
        logger.error(
            "[ccs-sweep] extraction failed for call %s (%s); retrying next pass",
            session.id,
            type(error).__name__,
        )
        return

    if _entry_count(db, session.id) != entry_count:
        db.rollback()
        if _release_extraction_claim(db, session, claim_id):
            if session.status == "extracted":
                session.status = "transcript_ready"
            db.commit()
        else:
            db.rollback()
        report["extractions_failed"] += 1
        logger.warning(
            "[ccs-sweep] transcript changed during extraction for call %s",
            session.id,
        )
        return

    if "extraction_retry" in (session.attrs or {}):
        session.attrs = {
            key: value
            for key, value in (session.attrs or {}).items()
            if key != "extraction_retry"
        }
    if session.status != "wrapped":
        session.status = "extracted"
    session.last_extracted_entry_count = entry_count
    if _release_extraction_claim(db, session, claim_id):
        db.commit()
        if extraction is None:
            # The extractor proposed nothing. The AE still gets an honest
            # empty wrap-up they can close in seconds.
            report["extractions_empty"] += 1
        else:
            report["extractions_run"] += 1
    else:
        # The model call exceeded its lease. Its output must not supersede a
        # newer owner's proposals or advance the call's state.
        db.rollback()
        report["extractions_failed"] += 1
        logger.error("[ccs-sweep] extraction claim expired for call %s", session.id)


def _mark_no_transcript(
    db: Session, email: str, dry_run: bool, report: dict, since: datetime | None = None
) -> int:
    """Mark calls that ended long enough ago with nothing to show for it.

    Not an error path. Suppressed sessions land here too, and they must: the
    wrap-up list reads ``status in (extracted, no_transcript)``, so a
    confidential call that never reaches ``no_transcript`` never reaches its
    AE's wrap-up queue at all, and the call goes unlogged.

    A call with no transcript still owes a wrap-up (the AE logs it by hand), so
    this is the second producer of the ``wrapup`` commitment. It runs over the
    calls it just marked AND over the ones an earlier pass marked and that are
    still inside the sweep window, because the marking happens exactly once and
    a commitment lost to one transient failure would otherwise be lost for good.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=NO_TRANSCRIPT_AFTER_MINUTES)
    candidates = (
        db.query(CallSession)
        .filter(
            func.lower(CallSession.ae_email) == email,
            CallSession.ended_at.isnot(None),
            CallSession.ended_at <= cutoff,
            CallSession.status.notin_(_AGED_OUT_TERMINAL),
        )
        .all()
    )
    marked = 0
    owed: list[CallSession] = []
    for session in candidates:
        if (
            db.query(CallTranscriptEntry.id)
            .filter(CallTranscriptEntry.call_session_id == session.id)
            .first()
            is not None
        ):
            continue
        marked += 1
        if dry_run:
            continue
        session.status = "no_transcript"
        reason = (
            "suppressed"
            if (session.capture_consent or "granted") == "suppressed"
            else "no_entries_after_30m"
        )
        # Reassigned, not mutated: SQLAlchemy does not track in-place changes to
        # a JSON column, so ``session.attrs["x"] = y`` is silently discarded.
        session.attrs = {
            **(session.attrs or {}),
            "no_transcript_reason": reason,
            "no_transcript_at": _now_iso(),
        }
        owed.append(session)
    if marked and not dry_run:
        db.commit()

    if dry_run:
        return marked

    # Committed first, deliberately: ``_open_wrapup_commitment`` rolls back on
    # failure, and the aging decision must survive a cockpit failure.
    for session in _unwrapped_silent_calls(db, email, since, exclude=owed):
        owed.append(session)
    for session in owed:
        _open_wrapup_commitment(db, session, report)
    return marked


def _unwrapped_silent_calls(
    db: Session, email: str, since: datetime | None, *, exclude: list[CallSession]
) -> list[CallSession]:
    """Already-``no_transcript`` calls in the window that are still unwrapped.

    Bounded by the sweep window rather than by all history: a call older than
    the lookback is out of scope for this pass by the same argument that bounds
    the Meet read, and an unbounded query here would grow with the table.
    """
    seen = {s.id for s in exclude}
    query = db.query(CallSession).filter(
        func.lower(CallSession.ae_email) == email,
        CallSession.status == "no_transcript",
        CallSession.wrapped_at.is_(None),
    )
    if since is not None:
        query = query.filter(CallSession.ended_at >= since)
    return [s for s in query.all() if s.id not in seen]


def _ae_emails(db: Session) -> tuple[list[str], int]:
    """``(emails_to_sweep, departed_skipped)``.

    Everyone :data:`CCS_FEATURE_KEY` admits, judged by ``feature_allows`` on
    the live ``users`` rows — department, managed departments, named guests and
    the admin door, exactly as Feature Access judges them for the screens. A
    session on record does not enrol anyone: whoever started a call from Cubie
    had to be on this roster to do it, and someone taken off it stops being
    read the same day.

    ``departed_skipped`` counts session owners with no ``users`` row any more.
    ``call_sessions`` has no FK to ``users.email`` (offboarding DELETES that row
    and "no users row" is the load-bearing departed signal), so they are counted
    rather than swept: impersonating a suspended Google account would bank a 403
    per run, forever.
    """
    from rubik_core.features import feature_allows  # local: keep the import light

    users = db.query(User).options(selectinload(User.roles)).all()
    live = {(u.email or "").strip().lower() for u in users if u.email}
    roster = {
        (u.email or "").strip().lower()
        for u in users
        if u.email
        and not u.is_platform_support
        and feature_allows(CCS_FEATURE_KEY, u, db)
    }
    with_sessions = {
        (email or "").strip().lower()
        for (email,) in db.query(CallSession.ae_email).distinct().all()
        if email
    }
    departed = with_sessions - live
    return sorted(roster), len(departed)


def sweep_all(db: Session, *, lookback_hours: int, dry_run: bool = False) -> dict:
    """Sweep every AE. Returns aggregate buckets plus the per-user reports.

    One AE's failure is contained: the loop rolls back and moves on, so a single
    revoked Google account or a single malformed conference record cannot cost
    the whole roster its ingest. The aggregate deliberately keeps
    ``users_permission_denied`` separate from ``users_failed`` and from
    ``users_swept`` — before the scopes are granted the expected report is "every user
    permission_denied, zero records", and that must be legible at a glance
    rather than looking like a quiet day.
    """
    report: dict = {
        "dry_run": dry_run,
        "enabled": bool(settings.CCS_ENABLED),
        "meet_enabled": bool(settings.CCS_MEET_ENABLED),
        "lookback_hours": max(1, min(int(lookback_hours or 0), _MAX_LOOKBACK_HOURS)),
        "users": 0,
        "users_swept": 0,
        "users_meet_disabled": 0,
        "users_meet_unavailable": 0,
        "users_permission_denied": 0,
        "users_failed": 0,
        "users_departed_skipped": 0,
        "conference_records": 0,
        "sessions_created": 0,
        "sessions_matched": 0,
        "sessions_stamped": 0,
        "suppressed": 0,
        "still_live": 0,
        "awaiting_transcript": 0,
        "transcripts_in_progress": 0,
        "already_ingested": 0,
        "ingest_incomplete": 0,
        "transcripts_seen": 0,
        "entries_written": 0,
        "would_ingest": 0,
        "extractions_run": 0,
        "extractions_empty": 0,
        "extractions_failed": 0,
        "extractions_deferred": 0,
        "no_transcript_marked": 0,
        "wrapups_opened": 0,
        "wrapups_existing": 0,
        "wrapups_failed": 0,
        "records_failed": 0,
        "records_skipped": 0,
        "per_user": [],
    }
    if not settings.CCS_ENABLED:
        logger.info("[ccs-sweep] CCS_ENABLED is off — nothing swept")
        return report

    emails, departed = _ae_emails(db)
    report["users"] = len(emails)
    report["users_departed_skipped"] = departed

    _outcome_bucket = {
        "swept": "users_swept",
        "meet_disabled": "users_meet_disabled",
        "meet_unavailable": "users_meet_unavailable",
        "permission_denied": "users_permission_denied",
        "failed": "users_failed",
        "ccs_disabled": "users_failed",
    }
    _summed = (
        "conference_records",
        "sessions_created",
        "sessions_matched",
        "sessions_stamped",
        "suppressed",
        "still_live",
        "awaiting_transcript",
        "transcripts_in_progress",
        "already_ingested",
        "ingest_incomplete",
        "transcripts_seen",
        "entries_written",
        "would_ingest",
        "extractions_run",
        "extractions_empty",
        "extractions_failed",
        "extractions_deferred",
        "no_transcript_marked",
        "wrapups_opened",
        "wrapups_existing",
        "wrapups_failed",
        "records_failed",
        "records_skipped",
    )

    for email in emails:
        try:
            user_report = sweep_user(
                db, email, lookback_hours=lookback_hours, dry_run=dry_run
            )
        except Exception as exc:  # noqa: BLE001 — sweep_user should not raise
            db.rollback()
            logger.exception("[ccs-sweep] unhandled failure for %s", email)
            user_report = _new_report(email, dry_run)
            user_report["outcome"] = "failed"
            user_report["error"] = f"{type(exc).__name__}: {exc}"
        report[_outcome_bucket.get(user_report["outcome"], "users_failed")] += 1
        for key in _summed:
            report[key] += user_report.get(key, 0)
        report["per_user"].append(user_report)

    logger.info(
        "[ccs-sweep] all done: users=%d swept=%d denied=%d failed=%d "
        "records=%d created=%d entries=%d incomplete=%d no_transcript=%d "
        "wrapups_opened=%d dry_run=%s",
        report["users"],
        report["users_swept"],
        report["users_permission_denied"],
        report["users_failed"],
        report["conference_records"],
        report["sessions_created"],
        report["entries_written"],
        report["ingest_incomplete"],
        report["no_transcript_marked"],
        report["wrapups_opened"],
        dry_run,
    )
    if report["ingest_incomplete"]:
        logger.error(
            "[ccs-sweep] %d call(s) have transcript entries but no corpus "
            "document — they are invisible to Chat, RAG and the graph and their "
            "AEs may never get a wrap-up. This is NOT 'already ingested'.",
            report["ingest_incomplete"],
        )
    return report


def _stamp(
    session: CallSession,
    conference_record: str,
    started: datetime | None,
    ended: datetime | None,
) -> bool:
    """Fill in what the conference record knows. True when something changed.

    Only ever fills blanks or advances a state: ``started_at`` is written once,
    ``ended_at`` is written when the conference has ended, and the status is
    nudged forward but never backward — a session an AE has already wrapped must
    not be reopened by a later sweep re-reading the same record.
    """
    changed = False
    if session.conference_record != conference_record:
        session.conference_record = conference_record
        changed = True
    if started and session.started_at is None:
        session.started_at = _naive(started)
        changed = True
    if ended and session.ended_at is None:
        session.ended_at = _naive(ended)
        changed = True
    if ended and session.status in ("scheduled", "in_progress"):
        session.status = "ended"
        changed = True
    elif not ended and session.status == "scheduled":
        session.status = "in_progress"
        changed = True
    return changed


def _naive(value: datetime | None) -> datetime | None:
    """Aware UTC -> naive UTC, the only datetime shape this schema stores."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value) -> datetime | None:
    from rubik_core.scripts.transcript_sync import _to_datetime

    return _to_datetime(value)
