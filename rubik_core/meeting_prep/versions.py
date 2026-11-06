"""The per-meeting version chain on ``meeting_prep_runs``.

One reader's briefs for one calendar event form a numbered chain: v1, v2, v3…
Every change appends a NEW row — a full regenerate, a one-section rewrite by
Cubie, a hand edit of one section, a restore of an older version — so nothing a
person has read is ever overwritten and any earlier version can be reopened or
restored ("v1 → v2 → v3 = v1 restored", the DAM pattern).

Rules this module enforces (the HTTP layer in ``backend/routers/home.py`` only
maps them to status codes):

* Version numbers are per ``(user_email, event_id)``, = max + 1. The unique
  index ``uq_meeting_prep_runs_user_event_version`` makes two writers racing
  for the same number collide; the loser rolls back and retries once (the
  Proposal Generator's ``_append_version`` pattern). The collision is
  recognised by SQLSTATE, not only by ``IntegrityError``: the Cloud SQL
  connector's pg8000 driver reports it as a plain ``DatabaseError``
  (:func:`is_unique_violation`).
* A change that edits a brief must be built on the CURRENT latest version
  (``must_build_on_latest``). If someone else got there first —including the
  racing writer above — :class:`StaleVersionError` says which version is now
  the latest, instead of silently dropping the other change.
* Failed generations and Deep Prep runs keep ``version`` NULL: they are on the
  table for audit, but never a version a person could land on.
* A brief built in the background (``rubik_core/meeting_prep/background.py``)
  first exists as a PENDING row (``change_kind="pending"``, ``version`` NULL,
  no brief). When the build finishes, that SAME row is finalized in place as
  the next version (``existing_row=`` on :func:`append_version` /
  :func:`record_failed_run`), under exactly the same numbering, retry and
  must-build-on-latest rules as a version appended in a request.
* The stored ``response`` JSON holds only the brief (content + signals). Every
  version/run field on the way out is filled from the row's columns, never
  trusted from the JSON (:func:`row_to_response_dict`).

Plain functions over a SQLAlchemy ``Session`` so they test without HTTP.
"""

from __future__ import annotations

import copy
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from rubik_core.database.models import MeetingPrepRun
from rubik_core.meeting_prep.sections import get_section

logger = logging.getLogger(__name__)

CHANGE_KINDS = frozenset(
    {
        "generated",
        "regenerated",
        "section_rewritten",
        "section_edited",
        "restored",
        "failed",
    }
)

# A background build in flight (``rubik_core/meeting_prep/background.py``):
# ``version`` NULL and no brief yet, so — like a failed row — never part of
# the chain until it is finalized as a version.
PENDING_CHANGE_KIND = "pending"

# Fields of ``MeetingPrepResponse`` that describe the ROW (which version this
# is, where it sits in the chain), not the brief. They are stripped before a
# response is stored and re-filled from the columns when it is read back, so an
# old row whose JSON still carries ``run_id``/``version`` can never lie about
# itself.
VERSION_FIELDS: tuple[str, ...] = (
    "run_id",
    "version",
    "latest_version",
    "is_latest",
    "base_version",
    "created_at",
    "change_kind",
    "change_note",
    "changed_section",
    "section_meta",
    "generation_failed",
    # A background build in flight for this meeting: about the page's state at
    # read time, never part of a stored brief.
    "pending_run",
)

# How a failed generation used to be stored before ``generation_failed``
# existed (and how the failure placeholder still reads).
_LEGACY_ERROR_PREFIX = "Error generating analysis"

# Postgres SQLSTATE for a unique-constraint violation.
_UNIQUE_VIOLATION = "23505"


class StaleVersionError(Exception):
    """The change was asked of a version that is no longer the latest.

    ``latest`` is the row that is the latest now (``None`` when the chain has
    no version at all, e.g. the run asked for is a failed or deep run)."""

    def __init__(self, latest: MeetingPrepRun | None):
        self.latest = latest
        n = getattr(latest, "version", None)
        super().__init__(
            f"A newer version (v{n}) exists." if n else "Not the latest version."
        )


# ─── Small helpers ────────────────────────────────────────────────────────────


def utcnow_naive() -> datetime:
    """Naive UTC, matching ``created_at``'s column default and the rest of
    the API (which sends naive UTC and lets the client mark it as UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat()


def strip_version_fields(response: dict[str, Any] | None) -> dict[str, Any]:
    """A deep copy of ``response`` with every row/version field removed — the
    shape that is stored in ``meeting_prep_runs.response``."""
    data = copy.deepcopy(dict(response or {}))
    for key in VERSION_FIELDS:
        data.pop(key, None)
    return data


def is_unique_violation(exc: BaseException) -> bool:
    """True when ``exc`` is a unique-constraint collision, whichever driver
    reported it.

    SQLAlchemy only maps a collision to ``IntegrityError`` when the driver
    raises its own ``IntegrityError``. pg8000 (what the Cloud SQL connector
    uses in production) raises a plain ``DatabaseError`` whose first arg is
    the server's error dict, so the SQLSTATE has to be read off it; psycopg
    carries it as ``pgcode`` / ``sqlstate``.
    """
    if isinstance(exc, IntegrityError):
        return True
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    if _UNIQUE_VIOLATION in (
        getattr(orig, "pgcode", None),
        getattr(orig, "sqlstate", None),
    ):
        return True
    args = getattr(orig, "args", None) or ()
    return (
        bool(args)
        and isinstance(args[0], dict)
        and args[0].get("C") == _UNIQUE_VIOLATION
    )


def is_failed_row(row: MeetingPrepRun) -> bool:
    if row.change_kind == "failed":
        return True
    if row.version is not None:
        return False
    summary = (row.response or {}).get("executive_summary")
    return isinstance(summary, str) and summary.startswith(_LEGACY_ERROR_PREFIX)


# ─── Reading the chain ────────────────────────────────────────────────────────


def _chain_query(db: Session, user_email: str, event_id: str):
    return db.query(MeetingPrepRun).filter(
        MeetingPrepRun.user_email == user_email,
        MeetingPrepRun.event_id == event_id,
        MeetingPrepRun.version.isnot(None),
    )


def latest_version_row(
    db: Session, user_email: str, event_id: str | None
) -> MeetingPrepRun | None:
    """The reader's newest version for this meeting, or ``None``."""
    if not event_id:
        return None
    return (
        _chain_query(db, user_email, event_id)
        .order_by(MeetingPrepRun.version.desc())
        .first()
    )


def next_version(db: Session, user_email: str, event_id: str | None) -> int:
    """max(version) + 1 for this reader and meeting (1 when there is none, and
    always 1 for a brief with no event id — it has no chain)."""
    if not event_id:
        return 1
    current = (
        db.query(func.max(MeetingPrepRun.version))
        .filter(
            MeetingPrepRun.user_email == user_email,
            MeetingPrepRun.event_id == event_id,
        )
        .scalar()
    )
    return int(current or 0) + 1


def list_versions(
    db: Session, user_email: str, event_id: str | None
) -> list[MeetingPrepRun]:
    """Every version of this meeting's brief for this reader, newest first."""
    if not event_id:
        return []
    return (
        _chain_query(db, user_email, event_id)
        .order_by(MeetingPrepRun.version.desc())
        .all()
    )


def _version_of(
    db: Session, run_id: str | None, known: dict[str, int | None]
) -> int | None:
    if not run_id:
        return None
    if run_id in known:
        return known[run_id]
    base = db.get(MeetingPrepRun, run_id)
    known[run_id] = base.version if base is not None else None
    return known[run_id]


def version_summaries(
    db: Session, user_email: str, event_id: str | None
) -> list[dict[str, Any]]:
    """``MeetingPrepVersionSummary`` dicts for the version list, newest first."""
    rows = list_versions(db, user_email, event_id)
    known: dict[str, int | None] = {r.id: r.version for r in rows}
    latest_id = rows[0].id if rows else None
    return [
        {
            "run_id": r.id,
            "version": r.version,
            "created_at": iso(r.created_at),
            "change_kind": r.change_kind,
            "change_note": r.change_note,
            "changed_section": r.changed_section,
            "base_version": _version_of(db, r.base_run_id, known),
            "is_latest": r.id == latest_id,
        }
        for r in rows
    ]


def row_to_response_dict(
    db: Session,
    row: MeetingPrepRun,
    *,
    latest: MeetingPrepRun | None = None,
) -> dict[str, Any]:
    """The stored brief plus its version fields, ready for
    ``MeetingPrepResponse(**d)``.

    The brief comes from ``row.response`` with any stored version/run keys
    dropped; the version fields come from the columns. Old rows whose JSON
    predates the newer keys simply fall back to the model defaults.
    ``latest`` may be passed when the caller already has it (saves a query);
    it must be the latest of ``row``'s own chain.
    """
    data = strip_version_fields(row.response)
    if not isinstance(data.get("title"), str) or not data.get("title"):
        data["title"] = row.title or "Meeting Prep"
    if not data.get("meeting_time"):
        start = (row.context_bundle or {}).get("start")
        if isinstance(start, str) and start:
            data["meeting_time"] = start

    if row.version is not None and row.event_id:
        if latest is None:
            latest = latest_version_row(db, row.user_email, row.event_id)
        latest_version = latest.version if latest is not None else row.version
        is_latest = latest is None or latest.id == row.id
    elif row.version is not None:
        # No event id → no chain: the row is its own latest.
        latest_version, is_latest = row.version, True
    else:
        # Failed / deep / never-numbered rows are not a version of anything.
        if latest is None and row.event_id:
            latest = latest_version_row(db, row.user_email, row.event_id)
        latest_version = latest.version if latest is not None else None
        is_latest = False

    data.update(
        {
            "run_id": row.id,
            "version": row.version,
            "latest_version": latest_version,
            "is_latest": is_latest,
            "base_version": _version_of(db, row.base_run_id, {}),
            "created_at": iso(row.created_at),
            "change_kind": row.change_kind,
            "change_note": row.change_note,
            "changed_section": row.changed_section,
            "section_meta": copy.deepcopy(dict(row.section_meta or {})),
            "generation_failed": is_failed_row(row),
        }
    )
    return data


# ─── Writing the chain ────────────────────────────────────────────────────────


def carry_forward_edits(
    latest_row: MeetingPrepRun | None, new_response: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Keep the person's hand edits across a full regenerate.

    Sections the person edited BY HAND in ``latest_row``
    (``section_meta[key].origin == "edited"``) replace the freshly generated
    value, and keep their provenance entry. Sections Cubie rewrote are NOT
    carried: a regenerate is precisely Cubie writing them again.
    Returns ``(response, section_meta)`` for the new version.
    """
    response = strip_version_fields(new_response)
    meta: dict[str, dict[str, Any]] = {}
    if latest_row is None:
        return response, meta
    old_response = latest_row.response or {}
    old_meta = latest_row.section_meta or {}
    if not isinstance(old_meta, dict) or not isinstance(old_response, dict):
        return response, meta
    for key, entry in old_meta.items():
        if not isinstance(entry, dict) or entry.get("origin") != "edited":
            continue
        if get_section(key) is None or key not in old_response:
            continue
        response[key] = copy.deepcopy(old_response[key])
        meta[key] = copy.deepcopy(entry)
    return response, meta


def append_version(
    db: Session,
    *,
    user_email: str,
    event_id: str | None,
    title: str | None,
    prep_kind: str | None,
    is_sales_user: bool,
    is_internal: bool,
    model_id: str | None,
    context_bundle: dict[str, Any] | None,
    response: dict[str, Any],
    partial_errors: list[str] | None,
    change_kind: str,
    base_run_id: str | None = None,
    change_note: str | None = None,
    changed_section: str | None = None,
    section_meta: dict[str, dict[str, Any]] | None = None,
    must_build_on_latest: bool = False,
    existing_row: MeetingPrepRun | None = None,
    delivery_status: str | None = None,
) -> MeetingPrepRun:
    """Append the next version for ``(user_email, event_id)`` and commit.

    ``must_build_on_latest``: the change was made ON ``base_run_id`` (an edit,
    a rewrite, a regenerate that carried edits forward), so it is only valid
    while ``base_run_id`` is still the latest version — otherwise
    :class:`StaleVersionError`. ``base_run_id=None`` then means "the chain must
    still be empty" (a first generate). Checked again on the retry, so a
    racing writer that took the number first turns into a clean conflict.

    ``section_meta[changed_section]["version"]`` is stamped with the number
    actually assigned (it can change on the retry).

    Retries once on a unique-constraint collision (two writers raced for one
    number, :func:`is_unique_violation`); a second collision, or any other
    database error, propagates.

    ``existing_row``: finalize this row (a background build's pending row) as
    the version IN PLACE instead of inserting a new one — same id, every field
    below overwritten, ``created_at`` = now (the moment it became a version).
    The rules are identical; a rolled-back attempt leaves the row as it was.
    ``delivery_status`` is written as given (``"ready"`` for a finalized
    background build; ``None`` — the column's old value for on-screen briefs —
    otherwise).
    """
    if change_kind not in CHANGE_KINDS or change_kind == "failed":
        raise ValueError(f"Not a version change kind: {change_kind!r}")
    if existing_row is not None and existing_row.version is not None:
        raise ValueError(f"Run {existing_row.id} is already v{existing_row.version}")
    stored = strip_version_fields(response)
    for attempt in range(2):
        if must_build_on_latest and event_id:
            latest = latest_version_row(db, user_email, event_id)
            if (latest.id if latest is not None else None) != base_run_id:
                raise StaleVersionError(latest)
            version = int(latest.version) + 1 if latest is not None else 1
        else:
            version = next_version(db, user_email, event_id)
        now = utcnow_naive()
        meta = copy.deepcopy(dict(section_meta or {}))
        if changed_section and isinstance(meta.get(changed_section), dict):
            meta[changed_section]["version"] = version
            meta[changed_section].setdefault("at", now.isoformat())
        row = _write_row(
            db,
            existing_row,
            created_at=now,
            user_email=user_email,
            event_id=event_id,
            title=title,
            prep_kind=prep_kind,
            is_sales_user=bool(is_sales_user),
            is_internal=bool(is_internal),
            model_id=model_id,
            context_bundle=copy.deepcopy(context_bundle),
            response=copy.deepcopy(stored),
            partial_errors=list(partial_errors or []),
            mode="sync",
            delivery_status=delivery_status,
            version=version,
            base_run_id=base_run_id,
            change_kind=change_kind,
            change_note=change_note,
            changed_section=changed_section,
            section_meta=meta,
        )
        try:
            db.commit()
            return row
        except DBAPIError as exc:
            db.rollback()
            if attempt == 1 or not is_unique_violation(exc):
                raise
            # Two writers raced for the same version number; take the next one
            # (or, for an edit, find out that it is now stale).
            logger.info(
                "meeting_prep version v%s for %s/%s taken by a racing writer; retrying",
                version,
                user_email,
                event_id,
            )
    raise AssertionError("unreachable")


def record_failed_run(
    db: Session,
    *,
    user_email: str,
    event_id: str | None,
    title: str | None,
    prep_kind: str | None,
    is_sales_user: bool,
    is_internal: bool,
    model_id: str | None,
    context_bundle: dict[str, Any] | None,
    response: dict[str, Any],
    partial_errors: list[str] | None,
    base_run_id: str | None = None,
    change_note: str | None = None,
    existing_row: MeetingPrepRun | None = None,
    delivery_status: str | None = None,
) -> MeetingPrepRun:
    """Keep a failed generation for audit — never as a version (``version``
    NULL, ``change_kind`` ``"failed"``), so it can't become what a person
    lands on. Commits.

    ``existing_row`` / ``delivery_status``: as for :func:`append_version` — a
    background build's pending row is marked failed in place (its
    ``created_at``, the moment the build was asked for, is kept). A row that
    is already a version is refused (``ValueError``), exactly like
    :func:`append_version`: a late failure must never unmake a version."""
    if existing_row is not None and existing_row.version is not None:
        raise ValueError(f"Run {existing_row.id} is already v{existing_row.version}")
    row = _write_row(
        db,
        existing_row,
        created_at=(
            existing_row.created_at
            if existing_row is not None and existing_row.created_at is not None
            else utcnow_naive()
        ),
        user_email=user_email,
        event_id=event_id,
        title=title,
        prep_kind=prep_kind,
        is_sales_user=bool(is_sales_user),
        is_internal=bool(is_internal),
        model_id=model_id,
        context_bundle=copy.deepcopy(context_bundle),
        response=strip_version_fields(response),
        partial_errors=list(partial_errors or []),
        mode="sync",
        delivery_status=delivery_status,
        version=None,
        base_run_id=base_run_id,
        change_kind="failed",
        change_note=change_note,
    )
    db.commit()
    return row


def _write_row(
    db: Session, existing_row: MeetingPrepRun | None, **fields: Any
) -> MeetingPrepRun:
    """A new row with ``fields`` (added to the session), or ``existing_row``
    with every one of ``fields`` overwritten. Not committed."""
    if existing_row is None:
        row = MeetingPrepRun(id=uuid.uuid4().hex, **fields)
        db.add(row)
        return row
    for key, value in fields.items():
        setattr(existing_row, key, value)
    return existing_row


def copy_row_fields(row: MeetingPrepRun) -> dict[str, Any]:
    """The base row's identity and evidence, for a version built on it (an
    edit, a rewrite or a restore keep the inputs the brief came from)."""
    return {
        "user_email": row.user_email,
        "event_id": row.event_id,
        "title": row.title,
        "prep_kind": row.prep_kind,
        "is_sales_user": bool(row.is_sales_user),
        "is_internal": bool(row.is_internal),
        "model_id": row.model_id,
        "context_bundle": row.context_bundle,
        "partial_errors": list(row.partial_errors or []),
    }
