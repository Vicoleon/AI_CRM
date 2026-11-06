"""ST-4 profile writer — a scored interview becomes the next ``st_profiles`` row.

Append-only, by the versioning contract in
``rubik_core/database/smart_teams.py``: a retake never edits the row it
supersedes, because every published guide cites the exact profile version it was
generated from and rewriting a version in place would make each of those guides
describe a person who has since said something different.

## What this module guarantees, and how

**One row per scored interview.** :func:`score_and_store_interview` is
idempotent through ``st_interviews.profile_id``, which is both the audit link
and the claim ticket. The check is not a check-then-act read, which two workers
would sail through together — the profile row is inserted first and the
interview is then claimed with a *conditional* UPDATE
(``WHERE id = :id AND profile_id IS NULL``). Whoever's UPDATE reports zero rows
affected lost the race, rolls its own insert back, and returns the winner's
profile. On Postgres the loser blocks on the winner's row lock and re-evaluates
the predicate after the commit, so the outcome is decided by the database rather
than by timing.

**A version number is spent once.** ``max(version) + 1`` is a read followed by a
write and therefore racy on its own; ``uq_st_profile_user_version`` is what makes
the race safe, and :data:`MAX_VERSION_ATTEMPTS` is what makes it invisible. A
concurrent retake and an upload merge both computing version 3 means one of them
takes an ``IntegrityError``, rolls back, re-reads, and writes version 4. That is
the only correct resolution: renumbering the loser is right, and letting it wait
for a lock it cannot know about is not.

**Retirement sets ``is_current = None``, never ``False``.**
``uq_st_profile_one_current`` means "at most one current row per person" only
because SQL treats NULLs as distinct, and a stored ``False`` silently inverts it
into "at most one superseded row per person" — which lets the first retake
through and makes the second one a 500 for somebody who just spent half an hour
answering questions about themselves. The ORM's ``@validates`` hook coerces
falsy values to NULL and a CHECK constraint refuses a stored ``False``; this
module goes through instance assignment (never a Core ``update()`` on that
column) so both defences apply.

**Email case is folded on the way in and folded on the way out.**
``users.email`` is stored verbatim from the Google ID token, so every write here
folds and every read compares through ``func.lower()`` — the functional indexes
``ix_st_profiles_email_lower`` and friends exist for exactly these queries.

## Advisory only

A profile row informs collaboration and team formation. It is never an input to
performance review, promotion, compensation, or a judgement about someone's
fitness for a role, and the raw payloads it carries are visible only to their
subject and to support admins.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rubik_core.database.smart_teams import (
    PROFILE_ORIGINS,
    PROFILE_STATUSES,
    SmartTeamsInterview,
    SmartTeamsProfile,
)
from rubik_core.smart_teams import interview as interview_catalog
from rubik_core.smart_teams.contracts import (
    FRAMEWORKS,
    ScoringInput,
    validate_envelope,
)
from rubik_core.smart_teams.scoring import (
    ScoringReport,
    StrengthsExtractor,
    score_answers,
)

logger = logging.getLogger(__name__)

#: Tries at ``max(version) + 1`` before giving up. Three, because each retry
#: costs one round trip and a fourth simultaneous writer for the same person
#: means something is wrong that a retry will not fix (a stuck job re-firing,
#: say) and should surface as an error rather than as a slow success.
MAX_VERSION_ATTEMPTS = 3

STATUS_CURRENT = "current"
STATUS_DRAFT = "draft"
STATUS_SUPERSEDED = "superseded"

ORIGIN_INTERVIEW = "interview"


class ProfileError(RuntimeError):
    """A profile version could not be written.

    Raised rather than returned: every caller of this module is either an HTTP
    handler or a cubie-worker job, and both need to know the difference between
    "wrote version 4" and "wrote nothing". A silent no-op here reads downstream
    as a person who never took the interview.
    """


class _ClaimLost(Exception):
    """Internal: another writer already scored this interview. Never escapes."""


@dataclass(frozen=True)
class ProfileWrite:
    """The outcome of a write attempt.

    ``created`` is the field callers must branch on. ``False`` means this call
    scored nothing and stored nothing — it found an existing profile for the
    interview and handed it back — which is the correct answer to "score this
    interview" being asked twice, and is NOT an error. ``report`` is ``None`` on
    that path for the same reason: no scoring run happened, and returning a
    freshly-computed report next to a previously-stored row would invite a
    caller to render one while the database holds the other.
    """

    profile: SmartTeamsProfile
    created: bool
    report: ScoringReport | None = None
    #: How many insert attempts it took. > 1 means a version race was resolved.
    attempts: int = 1

    @property
    def version(self) -> int:
        return int(self.profile.version)


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


# ── Reads ─────────────────────────────────────────────────────────────────────


def current_profile(db: Session, user_email: str) -> SmartTeamsProfile | None:
    """This person's live profile version, or ``None``.

    Filters on ``is_current`` rather than ``status`` because ``is_current`` is
    the column the database constrains — ``status`` is the semantic label and a
    row written around the ORM could disagree with it. Reads through
    ``func.lower`` so a row written by a seed script with token casing still
    belongs to the right person.
    """
    email = _fold(user_email)
    rows = (
        db.query(SmartTeamsProfile)
        .filter(
            func.lower(SmartTeamsProfile.user_email) == email,
            SmartTeamsProfile.is_current.is_(True),
        )
        .order_by(SmartTeamsProfile.version.desc())
        .all()
    )
    if len(rows) > 1:
        # uq_st_profile_one_current makes this impossible through the database;
        # it is reachable only if the constraint was dropped or the table was
        # loaded around it. Say so loudly and answer with the newest, because
        # returning row order would make the profile a reader gets depend on the
        # query plan.
        logger.error(
            "[smart-teams] %s has %d rows marked current; "
            "uq_st_profile_one_current is not holding",
            email,
            len(rows),
        )
    return rows[0] if rows else None


def profile_history(db: Session, user_email: str) -> list[SmartTeamsProfile]:
    """Every version for this person, newest first. Nothing is ever deleted."""
    email = _fold(user_email)
    return (
        db.query(SmartTeamsProfile)
        .filter(func.lower(SmartTeamsProfile.user_email) == email)
        .order_by(SmartTeamsProfile.version.desc())
        .all()
    )


#: A completed interview with no profile after this long is not slow any more,
#: it is broken. Shared deliberately: ``backend/routers/smart_teams.py`` reports
#: ``delayed`` to the person on this threshold and
#: ``backend/jobs/smart_teams_skill_sweep_job.py`` raises the ops alert on it,
#: and if those two numbers drifted the alert would fire at a different moment
#: from the screen that tells somebody their result is missing.
#:
#: Generous because a cold Cloud Run Job task plus six instruments plus one LLM
#: extraction is minutes, not seconds.
SCORING_STALE_SECONDS = 20 * 60


def stale_scoring_interviews(
    db: Session,
    *,
    older_than_seconds: int = SCORING_STALE_SECONDS,
    limit: int = 200,
    now: datetime | None = None,
) -> list[SmartTeamsInterview]:
    """Interviews somebody finished answering that never became a profile.

    This is the dead-man's switch for a scoring run that failed WITHOUT
    failing: the job exited non-zero and something alerted (or it exited 0
    having written nothing, and nothing did), the endpoint had already returned
    202, and the person is left with a screen that says their answers are being
    scored. That state can last for days, noticed only if the person thinks
    to mention it.

    Why a server-side sweep rather than the router's ``delayed`` branch, which
    computes the same fact: the page polls for ~7.5 minutes
    (``MAX_POLLS * POLL_MS`` in ``SmartTeams.tsx``) and this threshold is 20, so
    the automatic poll STOPS while the state is still ``pending`` and
    ``delayed`` is only ever computed if a human comes back later and presses
    the button. An alert wired to that branch would therefore fire only for the
    people who chased it — exactly the ones who least need it — and stay silent
    for anyone who gave up. This query does not care whether anyone is looking.

    "Never became a profile" is deliberately *"no profile has appeared since
    this interview finished"* rather than *"this person has no profile"*:

    * ``profile_id is None`` is the authoritative link ST-4 writes, and is
      enough on its own for a first sitting;
    * the ``scored_at`` check is what makes a RETAKE visible. Someone with
      version 1 who retakes and loses the scoring run does have a live profile,
      so a person-level "has a profile?" test would call that healthy and the
      lost retake would never be reported;
    * and requiring the profile to post-date the interview is what stops a
      scoring engine that forgot to write ``profile_id`` back from being
      reported as broken — the same belt-and-braces the router applies when it
      treats a live current profile as proof that scoring finished.

    Ordered oldest-first and capped, so a backlog reports the longest-suffering
    person first and one bad week cannot turn a nightly log line into a flood.
    """
    cutoff = (now or datetime.utcnow()) - timedelta(seconds=older_than_seconds)
    scored_since = (
        db.query(SmartTeamsProfile.id)
        .filter(
            func.lower(SmartTeamsProfile.user_email)
            == func.lower(SmartTeamsInterview.user_email),
            SmartTeamsProfile.scored_at.isnot(None),
            SmartTeamsProfile.scored_at >= SmartTeamsInterview.completed_at,
        )
        .exists()
    )
    return (
        db.query(SmartTeamsInterview)
        .filter(
            SmartTeamsInterview.status == "completed",
            SmartTeamsInterview.profile_id.is_(None),
            SmartTeamsInterview.completed_at.isnot(None),
            SmartTeamsInterview.completed_at < cutoff,
            ~scored_since,
        )
        .order_by(SmartTeamsInterview.completed_at.asc())
        .limit(limit)
        .all()
    )


def _next_version(db: Session, user_email: str) -> int:
    """``max(version) + 1`` for this person. Racy alone — see the retry loop.

    Deliberately a plain read with no locking. ``SELECT ... FOR UPDATE`` would
    need a row to lock and the first version for a person has none, and an
    advisory lock would have to be held across the LLM-free but still
    multi-hundred-millisecond insert path. The unique constraint plus a retry is
    both simpler and strictly safer: it cannot be forgotten by a future writer
    that does not know about the lock.
    """
    top = (
        db.query(func.max(SmartTeamsProfile.version))
        .filter(func.lower(SmartTeamsProfile.user_email) == _fold(user_email))
        .scalar()
    )
    return int(top or 0) + 1


# ── Writes ────────────────────────────────────────────────────────────────────


def _validated_columns(report: ScoringReport) -> dict[str, Any]:
    """The six JSON columns, each checked against the envelope contract.

    Validated on the way in as well as on the way out (the guide generator
    validates on read) because a shape bug caught here is caught in the change
    that introduced it, while the same bug caught on read is caught weeks later
    in front of the person whose guide it is.
    """
    columns = report.output.as_profile_columns()
    for framework in FRAMEWORKS:
        validate_envelope(columns[framework])
    return columns


def _retire_current(db: Session, user_email: str, now: datetime) -> int:
    """Mark every row currently flagged live as superseded. Returns how many.

    Goes through instance assignment, not a Core ``update()``: the
    ``@validates`` hook on ``is_current`` is what coerces a falsy value to NULL,
    and ``update(...).values(is_current=False)`` — the obvious way to write this
    — bypasses it and hits the CHECK constraint instead. See the module
    docstring.
    """
    retired = 0
    for row in (
        db.query(SmartTeamsProfile)
        .filter(
            func.lower(SmartTeamsProfile.user_email) == _fold(user_email),
            SmartTeamsProfile.is_current.is_(True),
        )
        .all()
    ):
        row.is_current = None  # NOT False — see the module docstring
        row.status = STATUS_SUPERSEDED
        row.superseded_at = now
        retired += 1
    return retired


def _insert_version(
    db: Session,
    report: ScoringReport,
    *,
    origin: str,
    status: str,
    interview_id: str | None,
) -> SmartTeamsProfile:
    """Retire the predecessor and insert the next version. No commit.

    Flushes so that a version collision or a second-current-row violation
    surfaces here as an ``IntegrityError`` the retry loop can act on, rather
    than at commit time where it would be tangled up with whatever else the
    caller's transaction is doing.
    """
    email = _fold(report.output.user_email)
    now = report.scored_at
    if status == STATUS_CURRENT:
        _retire_current(db, email, now)
    profile = SmartTeamsProfile(
        id=uuid.uuid4().hex,
        user_email=email,
        version=_next_version(db, email),
        status=status,
        # True or NULL only. A draft never claims the live slot, so a partial
        # rescore cannot dethrone a complete profile.
        is_current=True if status == STATUS_CURRENT else None,
        provenance=dict(report.output.provenance),
        origin=origin,
        scored_at=now,
        **_validated_columns(report),
    )
    db.add(profile)
    db.flush()
    logger.info(
        "[smart-teams] wrote profile %s v%d for %s (origin=%s status=%s "
        "reportable=%s failed=%s interview=%s)",
        profile.id[:8],
        profile.version,
        email,
        origin,
        status,
        list(report.reportable_frameworks),
        list(report.failed_frameworks),
        (interview_id or "-")[:8],
    )
    return profile


def _with_version_retry(
    db: Session,
    work: Callable[[], SmartTeamsProfile],
    *,
    attempts: int,
) -> tuple[SmartTeamsProfile, int]:
    """Run ``work`` until the version number sticks, or give up loudly.

    An ``IntegrityError`` here means a concurrent writer took the version number
    (or the current slot) between our read and our flush. Rolling the whole
    transaction back and recomputing is the only safe response — the retired
    predecessor has to be re-retired too, which is why the rollback is total and
    ``work`` redoes everything rather than just re-picking a number.
    """
    last: IntegrityError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return work(), attempt
        except IntegrityError as exc:
            db.rollback()
            last = exc
            logger.warning(
                "[smart-teams] profile version collision (attempt %d/%d): %s",
                attempt,
                attempts,
                str(exc.orig or exc)[:200],
            )
    raise ProfileError(
        f"could not allocate a profile version after {attempts} attempts; "
        "another writer keeps winning the race"
    ) from last


def write_profile_version(
    db: Session,
    report: ScoringReport,
    *,
    origin: str = ORIGIN_INTERVIEW,
    status: str = STATUS_CURRENT,
    interview_id: str | None = None,
    attempts: int = MAX_VERSION_ATTEMPTS,
) -> ProfileWrite:
    """Append one scored profile version. The raw writer, with no idempotency.

    Use this for the paths that legitimately write a new version every time
    they run — an official upload being merged (ST-5), an admin seed — and use
    :func:`score_and_store_interview` for an interview, which must not produce
    two versions when its job is retried.

    ``status`` chooses whether this version becomes live. ``"current"`` retires
    the predecessor; ``"draft"`` does not touch it, which is what makes a draft
    safe to write next to somebody's real profile.
    """
    if status not in PROFILE_STATUSES or status == STATUS_SUPERSEDED:
        raise ProfileError(
            f"status {status!r} is not a state a new version may be written in; "
            f"expected {STATUS_CURRENT!r} or {STATUS_DRAFT!r}"
        )
    if origin not in PROFILE_ORIGINS:
        raise ProfileError(f"origin {origin!r} is not one of {PROFILE_ORIGINS}")

    def work() -> SmartTeamsProfile:
        return _insert_version(
            db, report, origin=origin, status=status, interview_id=interview_id
        )

    profile, used = _with_version_retry(db, work, attempts=attempts)
    db.commit()
    db.refresh(profile)
    return ProfileWrite(profile=profile, created=True, report=report, attempts=used)


# ── The idempotent interview entry point ──────────────────────────────────────


def _claimed_profile(
    db: Session, interview: SmartTeamsInterview
) -> SmartTeamsProfile | None:
    """The profile this interview was already scored into, if any."""
    profile_id = interview.profile_id
    if not profile_id:
        return None
    return db.get(SmartTeamsProfile, profile_id)


def score_and_store_interview(
    db: Session,
    interview_id: str,
    *,
    extractor: StrengthsExtractor | None = None,
    elapsed_seconds: float | None = None,
    scored_at: datetime | None = None,
    attempts: int = MAX_VERSION_ATTEMPTS,
) -> ProfileWrite:
    """Score a completed interview into a new profile version, exactly once.

    Idempotent on ``st_interviews.profile_id``: called again for the same
    interview it returns the profile that already exists with
    ``created=False``, having scored nothing. That matters because the caller is
    a cubie-worker job — Cloud Run retries a failed task, and "the LLM call
    succeeded but the commit timed out" must not leave somebody with two profile
    versions from one sitting.

    Two writers arriving at once are resolved by the database, not by timing:
    both insert, both try to claim the interview with a conditional UPDATE, and
    the one whose UPDATE matches zero rows rolls its insert back and returns the
    winner's row. See the module docstring.

    ``elapsed_seconds`` is the time the person actually spent answering, if the
    interview tracked it; it feeds DISC's implausibly-fast quality check. It is
    NOT derived from ``started_at``/``completed_at`` here, because a resumable
    interview spans days of idling and a wall-clock span would only ever fail to
    flag a genuinely rushed sitting — a check that cannot fire is worse than one
    that is switched off, because it looks like coverage.

    Raises :class:`ProfileError` when the interview does not exist or has
    sections left to do. Scoring a half-finished interview is refused rather
    than accommodated: the resulting row would claim ``profile_id`` and make the
    interview permanently un-rescorable, so the person's finished answers would
    never reach their profile. Call
    :func:`~rubik_core.smart_teams.scoring.score_answers` directly for a
    preview of work in progress — it writes nothing.
    """
    row = db.get(SmartTeamsInterview, interview_id)
    if row is None:
        raise ProfileError(f"no interview {interview_id!r}")

    existing = _claimed_profile(db, row)
    if existing is not None:
        logger.info(
            "[smart-teams] interview %s is already scored into profile %s v%d; "
            "returning it unchanged",
            interview_id[:8],
            existing.id[:8],
            existing.version,
        )
        return ProfileWrite(profile=existing, created=False, report=None, attempts=0)

    remaining = interview_catalog.remaining_sections(row.sections_completed or [])
    if remaining:
        raise ProfileError(
            f"interview {interview_id[:8]} still has "
            f"{[section.key for section in remaining]} to complete; a scored "
            "version would claim this interview and lock those answers out"
        )

    email = _fold(row.user_email)
    report = score_answers(
        ScoringInput(
            user_email=email,
            answers=row.answers or {},
            sections_completed=tuple(row.sections_completed or ()),
            elapsed_seconds=elapsed_seconds,
            provenance=ORIGIN_INTERVIEW,
            interview_id=row.id,
        ),
        extractor=extractor,
        scored_at=scored_at,
    )

    def work() -> SmartTeamsProfile:
        profile = _insert_version(
            db,
            report,
            origin=ORIGIN_INTERVIEW,
            status=STATUS_CURRENT,
            interview_id=row.id,
        )
        # The claim, and the whole idempotency guarantee. Conditional on
        # profile_id still being NULL, evaluated by the database: on Postgres a
        # concurrent claim holds the row lock until it commits, at which point
        # this predicate sees the winner's id and matches nothing.
        claimed = (
            db.query(SmartTeamsInterview)
            .filter(
                SmartTeamsInterview.id == row.id,
                SmartTeamsInterview.profile_id.is_(None),
            )
            .update(
                {
                    "profile_id": profile.id,
                    "status": "completed",
                    "completed_at": report.scored_at,
                    "last_activity_at": report.scored_at,
                },
                synchronize_session=False,
            )
        )
        if not claimed:
            raise _ClaimLost()
        return profile

    try:
        profile, used = _with_version_retry(db, work, attempts=attempts)
    except _ClaimLost:
        db.rollback()
        reloaded = db.get(SmartTeamsInterview, interview_id)
        winner = _claimed_profile(db, reloaded) if reloaded is not None else None
        if winner is None:
            # profile_id was non-NULL when we tried to claim it and names no
            # profile now. Either the row was deleted mid-flight or the FK is
            # not being enforced (SQLite does not enforce it by default). Fail
            # rather than write a second version on a guess.
            raise ProfileError(
                f"interview {interview_id[:8]} is claimed by a profile that "
                "does not exist; refusing to write a competing version"
            ) from None
        logger.info(
            "[smart-teams] lost the scoring race for interview %s; "
            "returning profile %s v%d",
            interview_id[:8],
            winner.id[:8],
            winner.version,
        )
        return ProfileWrite(profile=winner, created=False, report=None, attempts=0)

    db.commit()
    db.refresh(profile)
    db.refresh(row)
    return ProfileWrite(profile=profile, created=True, report=report, attempts=used)


def framework_provenance(profile: SmartTeamsProfile) -> Mapping[str, str]:
    """Where each layer on this row came from, with every framework present.

    ``default_provenance()`` seeds the full map on write, but a row written
    around the ORM (a seed script, a psql fixup) can carry a partial one, and a
    guide that says "from your interview" about a layer it has no provenance for
    is making it up. Missing keys come back as ``"unknown"`` so the caller has
    to decide what to say.
    """
    stored = profile.provenance or {}
    return {name: str(stored.get(name) or "unknown") for name in FRAMEWORKS}
