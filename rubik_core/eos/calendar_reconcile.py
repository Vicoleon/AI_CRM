"""Nightly: ask Google about every imported L10, not just the ones somebody opened.

``rubik_core.eos.calendar_sync`` knows how to reconcile ONE series against
Google Calendar. Until now the only thing that ever called it was
``GET /api/eos/l10/calendar-status``, which fires when a user opens EOS →
Meetings — and it writes back only for the meeting's manager. Stack those two
facts and the reconciliation has a hole the size of the bug it was written to
fix: **a manager who cancels their L10 on Google Calendar and then does not
open the Meetings board leaves the row exactly as stale as before.** Nobody
else can correct it for them, because nobody else is allowed to.

The consumers that read the stored answer are the ones that suffer, and they
are precisely the surfaces a person is NOT looking at the board while using:

* ``rubik_core/availability/handoff.py`` — the PTO hand-off brief, which asks
  somebody to cover a meeting that is not happening.
* ``backend/routers/eos/me.py::_find_next_l10_for`` — "your next L10", which
  keeps naming a dead meeting.

So this module does what the endpoint does, for every imported meeting,
without waiting for a human to trigger it: **one calendar client per manager,
one lookup per meeting, and the manager's own answer written to the manager's
own meetings.**

Three rules it inherits rather than reinvents, because re-deciding any of them
here is how the two copies drift apart:

1. **Who may write.** :func:`~rubik_core.eos.calendar_sync.may_persist` decides,
   exactly as it does for the endpoint. This job does not hold a special
   authority; it impersonates the manager and then asks the same question the
   manager's own browser session would. A meeting it cannot find an authority
   for is reported, never guessed at.
2. **UNKNOWN is never persisted.**
   :func:`~rubik_core.eos.calendar_sync.apply_to_meeting` enforces it and this
   module does not go around it. A nightly sweep is the single most dangerous
   possible caller of that rule: an endpoint that mis-fires during a Google
   outage mangles one meeting for one user, whereas an unguarded sweep would
   mark EVERY L10 in the company cancelled in one pass, overnight, with nobody
   watching.
3. **A cancellation is a finding, not a failure.** Finding fifty cancelled
   meetings means the sweep worked. See the exit codes in
   ``backend/jobs/l10_calendar_reconcile_job.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Imported at module scope, unlike everything else here: ``calendar_sync`` is a
# pure sibling — stdlib only, no database, no FastAPI, no Google client (it
# defers its own ``rubik_core.tools.calendar`` import) — so it costs nothing to
# pull in and binding it here keeps the three rules this module obeys visible
# at the top of the file rather than buried inside a function.
from rubik_core.eos.calendar_sync import (
    apply_to_meeting,
    may_persist,
    resolve_series_state,
)

logger = logging.getLogger(__name__)

LOG_TOKEN = "[l10-calendar-reconcile]"

#: Default ceiling on meetings touched in one execution. One Google round-trip
#: each, so this is a quota and runtime guard rather than a correctness one —
#: and because the sweep takes the STALEST rows first (see
#: :func:`_meetings_to_check`), a capped run is a fair rotation rather than the
#: same prefix every night. A company has tens of imported L10s, not thousands, so
#: in practice the cap never binds; it exists so that if that ever stops being
#: true the failure is a slower convergence and not a 3600s task timeout.
DEFAULT_LIMIT = 250


@dataclass
class ReconcileReport:
    """What one sweep did. The job turns this into a log line and an exit code."""

    #: Meetings we opened a calendar for and actually asked Google about.
    asked: int = 0
    #: Of those, how many came back with a usable answer (active or cancelled).
    #: ``asked`` without ``known`` is an outage — we looked and learned nothing.
    known: int = 0
    #: Known answers, split. These are STATES observed, not transitions.
    active: int = 0
    cancelled: int = 0
    #: Rows whose stored state or next-occurrence actually moved. This is the
    #: number that means something changed for a user tonight.
    changed: int = 0
    #: Asked, and Google did not answer (outage, revoked access, an event we
    #: cannot read). Left exactly as it was — never written.
    unknown: int = 0
    #: No email to impersonate: no manager on the meeting, the manager has no
    #: ``eos_people`` row, and the roster is not a single person. Nobody can
    #: speak for this series, so it can never be reconciled by anyone — worth
    #: naming out loud rather than quietly skipping.
    no_authority: int = 0
    #: We had a reader but ``may_persist`` refused. Should be zero by
    #: construction; a non-zero count means the candidate-reader choice and the
    #: authority rule have drifted apart, which is a bug, not a state.
    unauthorized: int = 0
    #: Distinct calendars opened — the cost of the sweep in credential terms.
    calendars: int = 0
    #: Meetings left untouched because ``--limit`` was reached.
    deferred: int = 0
    #: Meeting ids that moved, for the log. Capped; the count above is exact.
    changed_ids: list[str] = field(default_factory=list)

    @property
    def is_blind(self) -> bool:
        """We asked about meetings and learned nothing about any of them.

        Distinct from "nothing to do" (``asked == 0``, a legitimately empty
        sweep) and from "nothing changed" (``known > 0``, a successful check
        that found everything already correct). This one means the sweep ran
        and saw nothing — a credentials or API outage wearing the costume of a
        quiet, successful night.
        """
        return self.asked > 0 and self.known == 0


def _candidate_reader(meeting, manager_emails: dict) -> str | None:
    """Whose calendar to read for this meeting, or ``None`` if nobody's.

    The series lives on the ORGANIZER's calendar and an import resolves the
    manager from ``organizer.email``, so the manager is the answer whenever
    there is one. The fallback covers the one other shape
    :func:`~rubik_core.eos.calendar_sync.may_persist` already recognises: a
    meeting whose roster is exactly one person is that person's own slot, and
    they speak for it even with no manager set.

    Returning a candidate is NOT an authorization — :func:`may_persist` still
    has to agree before anything is written. This only picks whose calendar to
    open.
    """
    email = (manager_emails.get(meeting.manager_id) or "").strip().lower()
    if email:
        return email
    roster = [
        e.strip().lower()
        for e in (getattr(meeting, "roster_emails", None) or [])
        if isinstance(e, str) and e.strip()
    ]
    return roster[0] if len(roster) == 1 else None


def _live_imported_query(db):
    """Base query for the meetings this sweep is responsible for.

    Imported (``google_event_id`` is set) and not deleted. A manually-scheduled
    L10 has no calendar to ask about, and its stored pattern remains the only
    truth it has ever had — reconciling it is not merely unnecessary, it is
    impossible.
    """
    from rubik_core.database.eos import L10Meeting

    return db.query(L10Meeting).filter(
        L10Meeting.is_deleted.is_(False),
        L10Meeting.google_event_id.isnot(None),
    )


def _meetings_to_check(db, limit: int):
    """Every imported, live L10 — stalest first.

    Ordering by ``calendar_checked_at`` ascending with never-checked rows in
    front means a ``--limit``ed run picks up where the last one left off
    instead of re-asking about the same alphabetical prefix every night, and
    that the meetings nobody has ever opened the board for — exactly the ones
    this job exists for — are served first. The ``is_(None).desc()`` key does
    the NULLS-FIRST job portably; ``NULLS FIRST`` itself is not available on
    every backend this repo runs on (SQLite in tests, Postgres in Cloud SQL).
    """
    from rubik_core.database.eos import L10Meeting

    return (
        _live_imported_query(db)
        .order_by(
            L10Meeting.calendar_checked_at.is_(None).desc(),
            L10Meeting.calendar_checked_at.asc(),
            L10Meeting.id.asc(),
        )
        .limit(max(1, limit))
        .all()
    )


def _manager_emails(db, meetings) -> dict:
    from rubik_core.database.eos import Person

    manager_ids = {m.manager_id for m in meetings if m.manager_id}
    if not manager_ids:
        return {}
    return {
        p.id: p.email for p in db.query(Person).filter(Person.id.in_(manager_ids)).all()
    }


def reconcile_l10_calendars(
    *,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ReconcileReport | None:
    """Reconcile every imported L10 against Google. Never raises.

    Returns the report, or ``None`` when the sweep could not run at all — no
    database, a failed import. The caller must NOT read ``None`` as an
    all-clear: an empty :class:`ReconcileReport` means "checked, everything
    already correct", and ``None`` means "did not check".

    ``dry_run`` resolves every series and reports what it found while writing
    nothing. It is the right first command against real data — the sweep's
    whole job is to mark meetings cancelled, and seeing WHICH ones before it
    does is cheap.
    """
    try:
        # Deferred and guarded: these two pull the app graph (engine, settings,
        # Workspace credentials). A failure here is a sweep that did not run,
        # which the job turns into a non-zero exit rather than a quiet all-clear.
        from rubik_core.database.db import SessionLocal
        from rubik_core.tools.calendar import GoogleCalendarClient
    except Exception:  # noqa: BLE001 — an import failure is a failed sweep
        logger.exception("%s could not import its dependencies", LOG_TOKEN)
        return None

    now = now or datetime.now(timezone.utc)
    report = ReconcileReport()

    try:
        with SessionLocal() as db:
            meetings = _meetings_to_check(db, limit)
            report.deferred = max(0, _live_imported_query(db).count() - len(meetings))
            manager_emails = _manager_emails(db, meetings)

            # One client per reader, not per meeting: building one costs a
            # Workspace credential load plus an OAuth refresh, and a manager
            # who runs three L10s should pay for that once.
            by_reader: dict[str, list] = {}
            for m in meetings:
                reader = _candidate_reader(m, manager_emails)
                if reader is None:
                    report.no_authority += 1
                    logger.warning(
                        "%s meeting=%s name=%r has no manager and no single-person "
                        "roster — nobody can speak for its calendar, so its "
                        "schedule can never be reconciled by this job or by the "
                        "board. Set a manager on it.",
                        LOG_TOKEN,
                        m.id,
                        m.name,
                    )
                    continue
                by_reader.setdefault(reader, []).append(m)

            report.calendars = len(by_reader)

            for reader, group in by_reader.items():
                try:
                    _reconcile_one_calendar(
                        db,
                        GoogleCalendarClient,
                        reader=reader,
                        meetings=group,
                        manager_emails=manager_emails,
                        report=report,
                        now=now,
                        dry_run=dry_run,
                    )
                except Exception:  # noqa: BLE001 — one bad calendar, not a dead sweep
                    db.rollback()
                    report.unknown += len(group)
                    logger.exception(
                        "%s reader=%s failed; %d meeting(s) left untouched",
                        LOG_TOKEN,
                        reader,
                        len(group),
                    )
    except Exception:  # noqa: BLE001 — a backstop must not become a second outage
        logger.exception("%s sweep could not run", LOG_TOKEN)
        return None

    return report


def _reconcile_one_calendar(
    db,
    client_factory,
    *,
    reader: str,
    meetings: list,
    manager_emails: dict,
    report: ReconcileReport,
    now: datetime,
    dry_run: bool,
) -> None:
    """One reader's calendar, one client, every meeting they speak for.

    Commits this reader's group on its own so a crash mid-sweep keeps the
    calendars already reconciled rather than discarding the night's work.
    """
    client = client_factory(user_email=reader)

    for m in meetings:
        report.asked += 1
        state = resolve_series_state(client, m.google_event_id, reader_email=reader)

        if not state.is_known:
            # Left exactly as it was. This is the branch a Google outage takes,
            # and doing nothing is the entire point of it.
            report.unknown += 1
            logger.info(
                "%s meeting=%s reader=%s unknown (%s) — row untouched",
                LOG_TOKEN,
                m.id,
                reader,
                state.reason or "no reason given",
            )
            continue

        report.known += 1
        if state.is_cancelled:
            report.cancelled += 1
        else:
            report.active += 1

        if not may_persist(m, reader, manager_emails.get(m.manager_id)):
            # Unreachable by construction — the reader IS the authority we
            # picked. If it ever fires, _candidate_reader and may_persist have
            # drifted, and writing anyway would be the job inventing an
            # authority the endpoint does not grant.
            report.unauthorized += 1
            logger.error(
                "%s meeting=%s reader=%s resolved %s but is not permitted to "
                "persist it — candidate-reader and may_persist disagree",
                LOG_TOKEN,
                m.id,
                reader,
                state.state,
            )
            continue

        if dry_run:
            logger.info(
                "%s DRY RUN meeting=%s name=%r would set state=%s next=%s (stored: %s / %s)",
                LOG_TOKEN,
                m.id,
                m.name,
                state.state,
                state.next_start.isoformat() if state.next_start else "-",
                m.calendar_state or "-",
                m.next_occurrence_at.isoformat() if m.next_occurrence_at else "-",
            )
            continue

        if apply_to_meeting(m, state, now=now):
            report.changed += 1
            report.changed_ids.append(str(m.id))
            logger.info(
                "%s meeting=%s name=%r state=%s next=%s reason=%s",
                LOG_TOKEN,
                m.id,
                m.name,
                state.state,
                state.next_start.isoformat() if state.next_start else "-",
                state.reason or "-",
            )

    if dry_run:
        db.rollback()
        return
    # Commit even when nothing moved: ``calendar_checked_at`` advanced on every
    # known answer, and that freshness stamp is what tells a reader (and the
    # next operator looking at a stale row) that the answer was rechecked.
    db.commit()
