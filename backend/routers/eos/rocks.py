"""EOS rocks — quarterly commitments.

Create endpoint accepts an ergonomic `owner_email` field as an
alternative to `owner_id`; if neither `quarter_id` nor `owner_id` is
provided, the router resolves them from "the current quarter" /
"person with this email" before inserting.

Weekly updates land on a separate `eos_rock_updates` table with
REQUIRED status + percent_complete. POST /rocks/{rock_id}/updates
records one, also bumping the cached `rocks.status` so existing
list/aggregate queries keep working. GET /rocks/{rock_id}/updates
returns the dated history (powers the rock detail page).

`POST /rocks/{rock_id}/ids` spawns an Issue on the team's IDS
list with a back-reference to the source rock. Used by the "Add to IDS"
button on the rock detail modal — when a rock conversation goes off
the rails, anyone can drop the topic onto the next L10's IDS list
without leaving the rock page.

Carry-over: `POST /rocks/{rock_id}/carry-over` carries an unfinished rock
into a later quarter as a NEW rock (the source stays in its quarter, untouched,
as the record of what happened), linked through `eos_rock_carryovers`.
`POST /rocks/{rock_id}/carry-over/ai-draft` asks Gemini to re-date it for the
target quarter and turn the remaining work into milestones; the user edits the
draft before saving.
"""

from __future__ import annotations

import json
from datetime import date as date_cls
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func as sa_func
from sqlalchemy import or_
from sqlalchemy.orm import aliased

from backend.models.eos.issues import (
    IDSFromRockCreate,
    IssueResponse,
    derive_issue_title,
)
from backend.models.eos.traction import (
    RockAIDraftRequest,
    RockAIDraftResponse,
    RockCarryOverAIDraftRequest,
    RockCarryOverCreate,
    RockCreate,
    RockMilestone,
    RockResponse,
    RockUpdate,
    RockWeeklyUpdateCreate,
    RockWeeklyUpdateResponse,
)
from backend.routers.eos._authz import (
    can_mutate,
    can_update_rock,
    require_can_mutate,
    require_can_read,
    require_can_update_rock,
    require_exec_scope_allowed,
    rock_write_flags,
    visible_rock_ids,
)
from backend.routers.eos._priority_sync import (
    sync_milestone_priority,
    sync_rock_priority,
)
from backend.routers.eos._shared import CurrentUser, DBDep
from rubik_core.cost.usage import attribute_to
from rubik_core.database.eos import (
    Issue,
    IssueStatus,
    Organization,
    Person,
    Quarter,
    Rock,
    RockCarryover,
    RockWeeklyUpdate,
    Seat,
    Visibility,
)

# ORM milestone row — aliased so it doesn't shadow the Pydantic ``RockMilestone``
# schema imported above (used by the AI-draft endpoint).
from rubik_core.database.eos import RockMilestone as RockMilestoneRow

router = APIRouter(prefix="/rocks", tags=["EOS: Rocks"])


def _quarter_bounds(year: int, q: int) -> tuple[datetime, datetime]:
    """Calendar-quarter [start, end] datetimes. Q1 Jan–Mar … Q4 Oct–Dec."""
    start_month = 3 * (q - 1) + 1
    start = datetime(year, start_month, 1)
    if q == 4:
        end = datetime(year, 12, 31, 23, 59, 59)
    else:
        end = datetime(year, start_month + 3, 1) - timedelta(seconds=1)
    return start, end


def _today() -> date_cls:
    """Today's date. The one seam tests patch to pin "today" (by string target:
    the suite reloads modules mid-run)."""
    return date_cls.today()


def _calendar_quarter(when: date_cls | datetime) -> tuple[int, int]:
    """``(year, quarter 1-4)`` of the calendar quarter ``when`` falls in."""
    return when.year, (when.month - 1) // 3 + 1


def _next_quarter(year: int, q: int) -> tuple[int, int]:
    return (year + 1, 1) if q == 4 else (year, q + 1)


def _quarter_label(year: int, q: int) -> str:
    """Same name ``_resolve_or_create_quarter`` gives a quarter it creates."""
    return f"Q{q} {year}"


def _default_carry_target(
    source_start: date_cls | datetime, today: date_cls
) -> tuple[int, int]:
    """Where a rock carries over to when nobody picks: the quarter after the
    source rock's, or today's quarter if that is later.

    A Q3 rock carried in October lands in Q4 (the usual quarter-end case) and a
    Q4 rock carried in December lands in Q1. A Q2 rock nobody carried until
    October lands in Q4, not Q3: a rock carried into a quarter that is already
    over would just be missed again.
    """
    return max(
        _next_quarter(*_calendar_quarter(source_start)), _calendar_quarter(today)
    )


def _resolve_or_create_quarter(db, year: int, q: int, user) -> Quarter:
    """Find (or create) the Quarter for a calendar year + quarter.

    Reuses an existing Quarter whose ``start_date`` falls within the target
    calendar quarter — so picking the current period links to the existing
    ``is_current`` quarter (and the rock shows in the L10) rather than making
    a duplicate. Otherwise creates one (``is_current=False``); advancing the
    current quarter is managed separately.
    """
    if not 1 <= q <= 4:
        raise HTTPException(status_code=400, detail="quarter_number must be 1-4")
    org = (
        db.query(Organization)
        .filter(Organization.is_deleted == False)  # noqa: E712
        .first()
    )
    if not org:
        raise HTTPException(status_code=400, detail="No organization configured")
    start, end = _quarter_bounds(year, q)
    existing = (
        db.query(Quarter)
        .filter(
            Quarter.organization_id == org.id,
            Quarter.is_deleted == False,  # noqa: E712
            Quarter.start_date >= start,
            Quarter.start_date <= end,
        )
        .first()
    )
    if existing:
        return existing
    quarter = Quarter(
        organization_id=org.id,
        name=f"Q{q} {year}",
        start_date=start,
        end_date=end,
        is_current=False,
        created_by=user.email,
    )
    db.add(quarter)
    db.commit()
    db.refresh(quarter)
    return quarter


def _apply_milestones(
    rock: Rock, milestones: list[RockMilestone] | None, user_email: str | None
) -> list[tuple[RockMilestoneRow, bool]]:
    """Reconcile a rock's ``eos_rock_milestones`` child rows against an incoming
    milestone array (whole-array replace, matched by ``id``).

    - Entries carrying an ``id`` that matches an existing row update that row in
      place (description / completed / due_date, plus ``sort_order`` taken from
      the array position — the incoming order is authoritative).
    - Entries without a matching id are inserted as new rows.
    - Existing rows whose id is absent from the incoming array are dropped
      (delete-orphan fires when the collection is reassigned).

    Mirrors the contract the milestone editor uses today — the frontend PATCHes
    the whole array, including the "complete" toggle. The legacy JSON column is
    intentionally left untouched (no longer written).
    """
    incoming = milestones or []
    existing_by_id = {m.id: m for m in rock.milestones}
    reconciled: list[RockMilestoneRow] = []
    # Milestone rows whose completed flag changed (new value), so the caller
    # can fire the AI-Priorities auto-resolve/reopen hook AFTER commit — once
    # each row's id is populated and stable.
    transitions: list[tuple[RockMilestoneRow, bool]] = []
    for idx, item in enumerate(incoming):
        new_completed = bool(item.completed)
        row = existing_by_id.get(item.id) if item.id is not None else None
        if row is not None:
            was_completed = bool(row.completed)
            row.description = item.description
            row.completed = new_completed
            row.due_date = item.due_date
            row.sort_order = idx
            row.updated_by = user_email
            if new_completed != was_completed:
                transitions.append((row, new_completed))
        else:
            row = RockMilestoneRow(
                description=item.description,
                completed=new_completed,
                due_date=item.due_date,
                sort_order=idx,
                created_by=user_email,
            )
            if new_completed:
                transitions.append((row, True))
        reconciled.append(row)
    # Reassigning the collection lets delete-orphan remove rows that are no
    # longer present in the incoming array.
    rock.milestones = reconciled
    return transitions


def _stamp_can_edit(rocks: list[Rock], user, db) -> list[Rock]:
    """Stamp each rock with whether THIS caller may write it.

    The read gate (``visible_rock_ids``) is deliberately wider than the write
    gate (``can_mutate``): a team sees its team's rocks, but only the owner,
    the creator, the owner's manager and the team's leadership may change one.
    Without these flags the Rocks page offered an Edit button to every viewer
    and the save came back 403, which the client rendered as "Verify the owner
    is a registered user".

    The two verbs differ: any member of the Rock's team may UPDATE it (the L10
    Rock Review is a team activity — see ``can_update_rock_in_scope``), while
    DELETE stays with the owner, the creator, the owner's manager and the
    team's leadership. Both flags are transient (computed per request, never
    persisted); the server still enforces both gates on every write.
    """
    flags = rock_write_flags(user, rocks, db)
    for rock in rocks:
        rock.can_edit, rock.can_delete = flags.get(rock.id, (False, False))
    return rocks


_CARRY_FIELDS = (
    "carried_from_rock_id",
    "carried_from_quarter_id",
    "carried_from_quarter_name",
    "carried_to_rock_id",
    "carried_to_quarter_id",
    "carried_to_quarter_name",
)


def _stamp_carryovers(rocks: list[Rock], db) -> list[Rock]:
    """Stamp each rock with its live carry-over links, in ONE query per list.

    ``carried_from_*`` names the rock (and quarter) this one was carried over
    from; ``carried_to_*`` the rock it was carried into, the most recent one if
    it was carried again after a delete. A link counts only while BOTH rocks
    are live: that is what lets a deleted carried copy free its source to be
    carried again, and what the carry-over endpoint checks before it creates a
    second copy.

    No read filter on the rock at the far end: the link shows only its quarter
    name, and hiding it would offer a Carry-over button that then answers 409.
    Transient like ``can_edit``: computed per request, never stored on a rock.
    """
    for rock in rocks:
        for field in _CARRY_FIELDS:
            setattr(rock, field, None)
    by_id = {rock.id: rock for rock in rocks}
    if not by_id:
        return rocks
    ids = list(by_id)
    source = aliased(Rock)
    carried = aliased(Rock)
    source_quarter = aliased(Quarter)
    carried_quarter = aliased(Quarter)
    rows = (
        db.query(
            RockCarryover.source_rock_id,
            RockCarryover.carried_rock_id,
            source.quarter_id,
            source_quarter.name,
            carried.quarter_id,
            carried_quarter.name,
        )
        .select_from(RockCarryover)
        .join(source, source.id == RockCarryover.source_rock_id)
        .join(carried, carried.id == RockCarryover.carried_rock_id)
        .outerjoin(source_quarter, source_quarter.id == source.quarter_id)
        .outerjoin(carried_quarter, carried_quarter.id == carried.quarter_id)
        .filter(
            RockCarryover.is_deleted == False,  # noqa: E712
            source.is_deleted == False,  # noqa: E712
            carried.is_deleted == False,  # noqa: E712
            or_(
                RockCarryover.source_rock_id.in_(ids),
                RockCarryover.carried_rock_id.in_(ids),
            ),
        )
        # Oldest first, so the most recent link from a source wins carried_to.
        .order_by(RockCarryover.created_at)
        .all()
    )
    for src_id, new_id, src_qid, src_qname, new_qid, new_qname in rows:
        if (rock := by_id.get(new_id)) is not None:
            rock.carried_from_rock_id = src_id
            rock.carried_from_quarter_id = src_qid
            rock.carried_from_quarter_name = src_qname
        if (rock := by_id.get(src_id)) is not None:
            rock.carried_to_rock_id = new_id
            rock.carried_to_quarter_id = new_qid
            rock.carried_to_quarter_name = new_qname
    return rocks


@router.get("/", response_model=list[RockResponse])
def list_rocks(
    db: DBDep,
    current_user: CurrentUser,
    quarter_id: UUID | None = None,
    owner_id: UUID | None = None,
    department_name: str | None = None,
    is_exec: bool | None = None,
):
    query = db.query(Rock).filter(Rock.is_deleted == False)  # noqa: E712
    # Row-level access: limit to rocks the caller may read (None = unrestricted).
    visible_ids = visible_rock_ids(current_user, db)
    if visible_ids is not None:
        query = query.filter(Rock.id.in_(visible_ids))
    if quarter_id:
        query = query.filter(Rock.quarter_id == quarter_id)
    if owner_id:
        query = query.filter(Rock.owner_id == owner_id)
    # Team filter (on top of RLAC): the Rocks page selector chooses either
    # the Exec team (is_exec=True) or one specific department. Access is
    # already enforced by visible_rock_ids above; this only narrows the view.
    if is_exec is not None:
        query = query.filter(Rock.is_exec == is_exec)
    if department_name:
        query = query.filter(
            sa_func.lower(Rock.department_name) == department_name.lower()
        )
    return _stamp_carryovers(_stamp_can_edit(query.all(), current_user, db), db)


@router.get("/{rock_id}", response_model=RockResponse)
def get_rock(rock_id: UUID, db: DBDep, current_user: CurrentUser):
    rock = (
        db.query(Rock)
        .filter(Rock.id == rock_id, Rock.is_deleted == False)  # noqa: E712
        .first()
    )
    if not rock:
        raise HTTPException(status_code=404, detail="Rock not found")
    require_can_read(current_user, rock, db)  # ET rocks stay exec-only
    rock.can_edit = can_update_rock(current_user, rock, db)
    rock.can_delete = can_mutate(current_user, rock, db)
    return _stamp_carryovers([rock], db)[0]


def _owner_seat_department(db, owner_id) -> str | None:
    """Department of the owner's live seat, or None (seatless, deleted, blank)."""
    if owner_id is None:
        return None
    row = (
        db.query(Seat.department_name)
        .join(Person, Person.seat_id == Seat.id)
        .filter(
            Person.id == owner_id,
            Person.is_deleted == False,  # noqa: E712
            Seat.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    dept = row[0] if row else None
    return dept or None


def _resolve_team_tag(
    db, *, is_exec: bool, visibility, department_name: str | None, owner_id
) -> str | None:
    """The team tag (``department_name``) a rock should carry.

    * An Exec-team rock NEVER carries one, whatever the client sent. It is
      keyed off ``is_exec`` and a department tag is the one thing that could
      ever match it into a department scope (the Rocks-page team filter, a
      department L10, the Dashboard's per-team queries all key on the tag).
    * A Department-visibility rock without an explicit team takes its owner's
      seat department — the same rule the L10 Rock Review already applied to
      legacy untagged rocks (``_rock_team_filter`` in l10.py), so the meeting
      and the Rocks page agree about whose rock it is. Without it a team's
      rocks could sit in its L10 yet be missing from its view of the Rocks
      page, Done ones included, because the column was added with no backfill
      and the create form leaves it empty for anyone who can file to several
      teams.
    * A seatless owner leaves the tag empty rather than guessing; Company-wide
      and Individual rocks keep whatever was sent (empty by convention).
    """
    if is_exec:
        return None
    if department_name:
        return department_name
    vis = getattr(visibility, "value", visibility)
    if vis == Visibility.DEPARTMENT.value:
        return _owner_seat_department(db, owner_id)
    return None


def _owner_id_from_email(db, email: str) -> UUID:
    """The Person id for an ``owner_email`` on a create payload, or 400."""
    person = db.query(Person).filter(Person.email == email).first()
    if not person:
        raise HTTPException(status_code=400, detail="Owner email not found")
    return person.id


@router.post("/", response_model=RockResponse, status_code=status.HTTP_201_CREATED)
def create_rock(db: DBDep, current_user: CurrentUser, rock_in: RockCreate):
    if rock_in.is_exec:
        require_exec_scope_allowed(current_user, True)
    # Resolve owner: prefer explicit ID, fall back to email lookup.
    if rock_in.owner_email:
        rock_in.owner_id = _owner_id_from_email(db, rock_in.owner_email)
    elif not rock_in.owner_id:
        raise HTTPException(status_code=400, detail="Owner ID or Email required")

    # The team this rock belongs to: never on an Exec rock; the owner's seat
    # department when a Department rock arrives without one.
    rock_in.department_name = _resolve_team_tag(
        db,
        is_exec=bool(rock_in.is_exec),
        visibility=rock_in.visibility,
        department_name=rock_in.department_name,
        owner_id=rock_in.owner_id,
    )

    # Resolve quarter: an explicit year + quarter pick wins; then an
    # explicit quarter_id; otherwise default to the current quarter.
    if rock_in.quarter_year and rock_in.quarter_number:
        quarter = _resolve_or_create_quarter(
            db, rock_in.quarter_year, rock_in.quarter_number, current_user
        )
        rock_in.quarter_id = quarter.id
    elif not rock_in.quarter_id:
        current_quarter = (
            db.query(Quarter).filter(Quarter.is_current == True).first()  # noqa: E712
        )
        if not current_quarter:
            raise HTTPException(status_code=400, detail="No current quarter configured")
        rock_in.quarter_id = current_quarter.id

    # quarter_year/quarter_number are request-only (not Rock columns).
    # milestones is a relationship now, not a column — set via child rows below.
    rock_data = rock_in.model_dump(
        exclude={"owner_email", "quarter_year", "quarter_number", "milestones"}
    )
    rock = Rock(**rock_data, created_by=current_user.email)
    milestone_transitions = _apply_milestones(
        rock, rock_in.milestones, current_user.email
    )
    db.add(rock)
    db.commit()
    db.refresh(rock)
    for _row, _completed in milestone_transitions:
        sync_milestone_priority(
            milestone_id=_row.id, completed=_completed, actor=current_user.email
        )
    # They just created it — the creator keeps the creator grant on both verbs.
    rock.can_edit = rock.can_delete = True
    return rock


@router.patch("/{rock_id}", response_model=RockResponse)
def update_rock(
    rock_id: UUID, db: DBDep, current_user: CurrentUser, rock_in: RockUpdate
):
    rock = (
        db.query(Rock)
        .filter(Rock.id == rock_id, Rock.is_deleted == False)  # noqa: E712
        .first()
    )
    if not rock:
        raise HTTPException(status_code=404, detail="Rock not found")

    # Any member of the Rock's team may update it; DELETE below stays narrow.
    require_can_update_rock(current_user, rock, db)
    updates = rock_in.model_dump(exclude_unset=True)
    # milestones is a relationship now — pull it out of the generic setattr
    # loop and reconcile child rows separately. Only touch them when the PATCH
    # actually included the field (whole-array replace).
    milestones_provided = "milestones" in updates
    updates.pop("milestones", None)
    # Re-scoping to/from the Exec team is exec-only — a non-exec owner
    # flipping is_exec off would declassify a confidential ET rock.
    if "is_exec" in updates and bool(updates["is_exec"]) != bool(rock.is_exec):
        require_exec_scope_allowed(current_user, bool(updates["is_exec"]))
    old_status = rock.status
    milestone_transitions: list[tuple[RockMilestoneRow, bool]] = []
    for field, value in updates.items():
        setattr(rock, field, value)
    # Settle the team tag against the row's final state: an Exec rock sheds
    # any tag (also when it was just re-scoped to the Exec team), a Department
    # rock still without one takes its owner's seat department. Runs on every
    # PATCH so a legacy untagged rock heals on its next edit.
    rock.department_name = _resolve_team_tag(
        db,
        is_exec=bool(rock.is_exec),
        visibility=rock.visibility,
        department_name=rock.department_name,
        owner_id=rock.owner_id,
    )
    if milestones_provided:
        milestone_transitions = _apply_milestones(
            rock, rock_in.milestones, current_user.email
        )

    rock.updated_by = current_user.email  # Attribute the last writer
    db.commit()
    db.refresh(rock)
    # Auto-clear/reopen AI-Priorities cards for this rock + its milestones (the
    # owner AND any manager/exec who was shown them). Fired post-commit so
    # milestone rows have stable ids.
    if rock.status != old_status:
        sync_rock_priority(
            rock_id=rock.id, status=rock.status, actor=current_user.email
        )
    for _row, _completed in milestone_transitions:
        sync_milestone_priority(
            milestone_id=_row.id, completed=_completed, actor=current_user.email
        )
    rock.can_edit = True  # require_can_update_rock above already proved it
    rock.can_delete = can_mutate(current_user, rock, db)
    return _stamp_carryovers([rock], db)[0]


@router.delete("/{rock_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rock(rock_id: UUID, db: DBDep, current_user: CurrentUser):
    rock = (
        db.query(Rock)
        .filter(Rock.id == rock_id, Rock.is_deleted == False)  # noqa: E712
        .first()
    )
    if not rock:
        raise HTTPException(status_code=404, detail="Rock not found")

    require_can_mutate(current_user, rock, db)
    rock.is_deleted = True
    rock.updated_by = current_user.email  # Attribute the delete
    db.commit()
    return None


# -- Weekly updates --------------------------------------------


@router.get(
    "/{rock_id}/updates",
    response_model=list[RockWeeklyUpdateResponse],
)
def list_rock_updates(rock_id: UUID, db: DBDep, current_user: CurrentUser):
    """Dated weekly-update history for a rock. Reverse-chronological so
    the rock detail page renders newest-first."""
    rock = (
        db.query(Rock)
        .filter(Rock.id == rock_id, Rock.is_deleted == False)  # noqa: E712
        .first()
    )
    if not rock:
        raise HTTPException(status_code=404, detail="Rock not found")
    require_can_read(current_user, rock, db)  # ET rock history stays exec-only

    return (
        db.query(RockWeeklyUpdate)
        .filter(
            RockWeeklyUpdate.rock_id == rock_id,
            RockWeeklyUpdate.is_deleted == False,  # noqa: E712
        )
        .order_by(RockWeeklyUpdate.update_date.desc())
        .all()
    )


@router.post(
    "/{rock_id}/updates",
    response_model=RockWeeklyUpdateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_rock_update(
    rock_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    update_in: RockWeeklyUpdateCreate,
):
    """Record a weekly status update on a rock.

    `status` and `percent_complete` are REQUIRED at the schema layer
    so 422 fires before we get here. Comment is optional. update_date
    defaults to today if not supplied. Side effect: bump the cached
    `rocks.status` to match this new update so existing list/aggregate
    queries surface the latest known status without a JOIN."""
    rock = (
        db.query(Rock)
        .filter(Rock.id == rock_id, Rock.is_deleted == False)  # noqa: E712
        .first()
    )
    if not rock:
        raise HTTPException(status_code=404, detail="Rock not found")

    require_can_read(current_user, rock, db)  # 404 before 403: hide ET rocks
    # Logging progress IS the L10 Rock Review — open to the whole team.
    require_can_update_rock(current_user, rock, db)
    update_data = update_in.model_dump()
    if update_data.get("update_date") is None:
        update_data["update_date"] = date_cls.today()

    weekly = RockWeeklyUpdate(
        rock_id=rock_id,
        **update_data,
        created_by=current_user.email,
    )
    db.add(weekly)

    # Cache the latest known status on the rock itself so list views
    # don't need to JOIN. We do NOT touch description / visibility — those
    # are managed by PATCH /rocks/{id}.
    rock.status = update_in.status
    rock.updated_by = current_user.email  # Who logged this progress

    db.commit()
    db.refresh(weekly)
    # A logged weekly update cures staleness and may bring the rock on-track or
    # done — clear the rock's attention card (or reopen it if still needed).
    sync_rock_priority(
        rock_id=rock_id, status=update_in.status, actor=current_user.email
    )
    return weekly


# -- "Add to IDS" — issues spawned from a rock ---------------


@router.post(
    "/{rock_id}/ids",
    response_model=IssueResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_ids_from_rock(
    rock_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    payload: IDSFromRockCreate,
):
    """Spawn an IDS issue from a rock.

    Used by the "Add to IDS" button on the rock detail modal. The new
    issue gets:
    - ``source_rock_id`` set to this rock for the soft back-pointer.
    - ``department_name`` resolved from the rock owner's Seat so it
      surfaces in the right team's IDS list — falling back to the
      caller's own department if the owner doesn't have a Seat yet.
    - ``priority`` defaults to MEDIUM since these are usually
      conversations that already feel important enough to drop the
      rock review for.
    - ``status`` OPEN.
    """
    rock = (
        db.query(Rock)
        .filter(Rock.id == rock_id, Rock.is_deleted == False)  # noqa: E712
        .first()
    )
    if not rock:
        raise HTTPException(status_code=404, detail="Rock not found")
    require_can_read(current_user, rock, db)  # can't IDS a rock you can't see

    if not payload.description or not payload.description.strip():
        raise HTTPException(status_code=422, detail="description must not be empty")

    # Department resolution: rock owner → seat → department_name; fall
    # back to the caller's own department if that chain is incomplete
    # (e.g. owner is a People row without a Seat yet, which the EOS
    # bootstrap data set up that way). An Exec-team ("ET") rock's issue
    # inherits the Exec scope instead — routing it to a department's IDS
    # list would leak the confidential Exec surface to that department.
    department_name: str | None = None
    if not rock.is_exec:
        owner = (
            db.query(Person)
            .filter(
                Person.id == rock.owner_id,
                Person.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if owner and owner.seat_id:
            seat = (
                db.query(Seat)
                .filter(
                    Seat.id == owner.seat_id,
                    Seat.is_deleted == False,  # noqa: E712
                )
                .first()
            )
            if seat and seat.department_name:
                department_name = seat.department_name
        if not department_name:
            department_name = getattr(current_user, "department", None)

    issue = Issue(
        title=derive_issue_title(payload.description),
        description=payload.description.strip(),
        priority=payload.priority,
        status=IssueStatus.OPEN,
        department_name=department_name,
        is_exec=rock.is_exec,
        source_rock_id=rock_id,
        # owner of the issue defaults to the rock's owner — the rock
        # owner is the most natural person to drive the discussion in
        # the next L10. The IDS list can reassign later.
        owner_id=rock.owner_id,
        created_by=current_user.email,
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return issue


# -- AI-assisted rock draft -------------------------------------


@router.post("/ai-draft", response_model=RockAIDraftResponse)
@attribute_to("eos")
async def ai_draft_rock(
    payload: RockAIDraftRequest,
    db: DBDep,
    current_user: CurrentUser,
):
    """Return a SMART rock + 3–5 milestones for a rough goal.

    Replaces the legacy "Draft with AI" button that redirected to chat
    and let the LLM scribble at the user. Now the response lands as a
    structured object the frontend can drop into the rock-create form,
    so the owner edits in place and saves a real `Rock` (with
    milestones) in one click.
    """
    goal = (payload.goal or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="goal must not be empty")

    prompt = (
        "Act as an EOS Implementer. Draft a single quarterly Rock and 3 to "
        "5 supporting milestones for the user's rough goal below.\n\n"
        "Rules:\n"
        "- The Rock description follows the 'Noun-Verb-by-Date' SMART pattern.\n"
        "- 3-5 milestones — each milestone is concrete and measurable.\n"
        "- Each milestone leaves `completed` as false.\n"
        "- Keep tone professional, focused on accountability.\n"
        "- Return STRICT JSON with this shape and nothing else:\n"
        '  {"description": str, "milestones": [{"description": str, "completed": false}, ...]}\n\n'
        f"User's rough goal: {goal}\n"
    )

    data = await _gemini_json(prompt)

    description = (data.get("description") or "").strip()
    raw_milestones = data.get("milestones") or []
    if not description or not raw_milestones:
        raise HTTPException(
            status_code=502,
            detail="AI draft missing description or milestones",
        )

    milestones = [
        RockMilestone(
            description=str(m.get("description", "")).strip(),
            completed=bool(m.get("completed", False)),
        )
        for m in raw_milestones
        if isinstance(m, dict) and m.get("description")
    ]
    return RockAIDraftResponse(description=description, milestones=milestones)


async def _gemini_json(prompt: str):
    """Ask Gemini for a JSON answer; 502 when the call fails or the answer is
    not JSON. Shared by the rock draft and the carry-over re-draft, which
    each attribute the spend to their own AI Cost category."""
    from rubik_core.config import settings as cfg
    from rubik_core.inference_workloads import run_text_async
    from rubik_core.models import get_genai_client

    model_id = getattr(cfg, "VERTEX_LLM_MODEL_PRO", None) or getattr(
        cfg, "VERTEX_LLM_MODEL", "gemini-2.0-flash-001"
    )

    async def managed(route):
        response = await get_genai_client().aio.models.generate_content(
            model=route.model,
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        return response.text or ""

    try:
        raw = await run_text_async(
            "eos_rock_drafts",
            prompt,
            default_model=model_id,
            managed=managed,
            json_mode=True,
        )
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="AI draft failed. Check this workspace's model route and provider.",
        ) from None

    raw = raw.strip()
    # Strip markdown fences if the model added them despite mime-type.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail="AI returned an invalid JSON draft.",
        ) from None
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=502, detail="AI returned an invalid JSON draft."
        )
    return data


# -- Carry over to a later quarter -----------------------------------


def _live_rock_or_404(db, rock_id: UUID) -> Rock:
    rock = (
        db.query(Rock)
        .filter(Rock.id == rock_id, Rock.is_deleted == False)  # noqa: E712
        .first()
    )
    if not rock:
        raise HTTPException(status_code=404, detail="Rock not found")
    return rock


def _carry_target(source: Rock, year: int | None, q: int | None) -> tuple[int, int]:
    """The ``(year, quarter)`` to carry ``source`` into: the caller's pick when
    both parts are given, else ``_default_carry_target``.

    400 unless strictly later than the source's own calendar quarter: "carrying
    over" into the same or an earlier quarter would only duplicate the rock.
    """
    source_quarter = source.quarter
    if year is not None and q is not None:
        target = (year, q)
    else:
        target = _default_carry_target(source_quarter.start_date, _today())
    if target <= _calendar_quarter(source_quarter.start_date):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Pick a quarter after {source_quarter.name}: a rock carries "
                "over to a later quarter."
            ),
        )
    return target


def _refuse_a_second_carry_over(source: Rock, db) -> None:
    """409 when ``source`` already has a live carry-over (both rocks live)."""
    _stamp_carryovers([source], db)
    if source.carried_to_rock_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This rock was already carried over to "
                f"{source.carried_to_quarter_name or 'a later quarter'}."
            ),
        )


@router.post(
    "/{rock_id}/carry-over",
    response_model=RockResponse,
    status_code=status.HTTP_201_CREATED,
)
def carry_over_rock(
    rock_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    payload: RockCarryOverCreate,
):
    """Carry an unfinished rock into a later quarter, as a NEW rock.

    EOS practice at quarter end: a Rock that wasn't finished is dropped or
    carried into the next quarter, usually re-dated and re-scoped. The carried
    rock is created from the dialog's edited draft (``payload``); the source is
    NOT modified, so it stays in its quarter with its status, milestones and
    weekly-update history as the record of what happened. An
    ``eos_rock_carryovers`` row links the two, and a rock has at most one live
    carry-over (409 on a second; deleting the carried rock frees it again).
    """
    source = _live_rock_or_404(db, rock_id)
    require_can_read(current_user, source, db)  # 404 before 403: hide ET rocks
    # Any member of the rock's team may carry it over, the same gate as editing
    # it: deciding what carries over is part of the team's quarter-end review.
    require_can_update_rock(current_user, source, db)

    description = (payload.description or "").strip()
    if not description:
        raise HTTPException(status_code=422, detail="description must not be empty")

    _refuse_a_second_carry_over(source, db)

    is_exec = bool(source.is_exec if payload.is_exec is None else payload.is_exec)
    # Moving the carried copy into or out of the Exec team is the re-scope a
    # PATCH guards (out of it would declassify a confidential ET rock). Keeping
    # the source's scope is not: the non-exec owner of an Exec rock may already
    # edit it, so they may carry it over as the Exec rock it already is, and
    # the page offers them the button on that same edit grant.
    if is_exec != bool(source.is_exec):
        require_exec_scope_allowed(current_user, is_exec)

    if payload.owner_email:
        owner_id = _owner_id_from_email(db, payload.owner_email)
    else:
        owner_id = payload.owner_id or source.owner_id
    visibility = payload.visibility or source.visibility
    # Left out of the payload = keep the source's team; sent, even as null,
    # = the dialog's choice. Either way the team-tag rule settles it.
    department_name = (
        payload.department_name
        if "department_name" in payload.model_fields_set
        else source.department_name
    )
    department_name = _resolve_team_tag(
        db,
        is_exec=is_exec,
        visibility=visibility,
        department_name=department_name,
        owner_id=owner_id,
    )

    year, q = _carry_target(source, payload.quarter_year, payload.quarter_number)
    # May commit (a new quarter row), so it runs BEFORE the lock below: a
    # commit would release the lock between the re-check and the insert.
    quarter = _resolve_or_create_quarter(db, year, q, current_user)

    # "One live carry-over per rock" is a rule in code, not a database
    # constraint (a link stays valid only while both rocks are live, which a
    # unique index can't express). Lock the source row and check again, so two
    # people saving the same rock's carry-over at once get one copy and a 409
    # rather than two copies. Postgres only; SQLite ignores FOR UPDATE.
    db.query(Rock).filter(Rock.id == source.id).with_for_update().one()
    _refuse_a_second_carry_over(source, db)

    rock = Rock(
        quarter_id=quarter.id,
        owner_id=owner_id,
        description=description,
        status=payload.status,
        visibility=visibility,
        is_exec=is_exec,
        department_name=department_name,
        created_by=current_user.email,
    )
    # Always brand-new rows on the new rock, whatever ids the client echoed
    # back: the source keeps its milestones as the record of its quarter.
    milestones = [
        m.model_copy(update={"id": None, "sort_order": None})
        for m in payload.milestones or []
    ]
    milestone_transitions = _apply_milestones(rock, milestones, current_user.email)
    db.add(rock)
    db.flush()  # the new rock's id, for the link row
    db.add(
        RockCarryover(
            source_rock_id=source.id,
            carried_rock_id=rock.id,
            created_by=current_user.email,
        )
    )
    db.commit()
    db.refresh(rock)
    for _row, _completed in milestone_transitions:
        sync_milestone_priority(
            milestone_id=_row.id, completed=_completed, actor=current_user.email
        )
    # Computed, not assumed True as create_rock does: a carry-over can hand an
    # Exec rock to another owner, and its non-exec carrier then has no grant on
    # the copy (the page would offer Edit and Delete that both answer 403).
    return _stamp_carryovers(_stamp_can_edit([rock], current_user, db), db)[0]


_CARRYOVER_MAX_MILESTONES = 6
_CARRYOVER_COMMENT_LIMIT = 500


def _carryover_prompt(
    source: Rock, updates: list[RockWeeklyUpdate], year: int, q: int
) -> str:
    """The re-draft prompt: the source rock, all its milestones and its latest
    weekly updates, re-dated for the target quarter."""
    start, end = _quarter_bounds(year, q)
    target = _quarter_label(year, q)
    first_day = f"{start:%B} {start.day}, {start.year}"
    last_day = f"{end:%B} {end.day}, {end.year}"
    status_text = str(getattr(source.status, "value", source.status) or "")
    status_text = status_text.replace("_", " ")
    source_quarter = source.quarter.name if source.quarter else "its quarter"

    milestone_lines = []
    for m in source.milestones:
        if m.is_deleted:
            continue
        line = f"- [{'done' if m.completed else 'not done'}] {m.description}"
        if m.due_date:
            line += f" (due {m.due_date.isoformat()})"
        milestone_lines.append(line)

    update_lines = []
    for u in updates:
        u_status = str(getattr(u.status, "value", u.status) or "").replace("_", " ")
        line = (
            f"- {u.update_date.isoformat()}: {u_status}, {u.percent_complete}% complete"
        )
        comment = (u.comment or "").strip()
        if comment:
            line += f". {comment[:_CARRYOVER_COMMENT_LIMIT]}"
        update_lines.append(line)

    return (
        "Act as an EOS Implementer. The quarterly Rock below was not finished "
        f"in {source_quarter} and is being carried over into {target}. "
        f"Re-draft it for {target}.\n\n"
        "Rules:\n"
        "- Keep the SAME commitment. Do not invent new scope.\n"
        "- Rewrite the Rock description in the 'Noun-Verb-by-Date' SMART "
        f"pattern, dated for {target} (no later than {last_day}).\n"
        "- Milestones are the work that remains (the ones not done), 0 to "
        f"{_CARRYOVER_MAX_MILESTONES} of them, each concrete and measurable. "
        "Done milestones stay with the old Rock; do not repeat them.\n"
        "- Each milestone has completed false and an optional due_date "
        f"(YYYY-MM-DD) between {first_day} and {last_day}, or null.\n"
        "- Return STRICT JSON with this shape and nothing else:\n"
        '  {"description": str, "milestones": [{"description": str, '
        '"due_date": "YYYY-MM-DD" | null}, ...]}\n\n'
        f"Rock ({source_quarter}, status: {status_text}): "
        f"{source.description}\n"
        "Milestones:\n"
        + ("\n".join(milestone_lines) or "- (none)")
        + "\nLatest weekly updates (newest first):\n"
        + ("\n".join(update_lines) or "- (none logged)")
        + "\n"
    )


def _iso_date(value) -> date_cls | None:
    if not value:
        return None
    try:
        return date_cls.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _carryover_draft(data: object, year: int, q: int) -> RockAIDraftResponse:
    """Settle the model's answer into a draft the dialog can trust.

    Every milestone comes back open (``completed`` False: it is the remaining
    work), a due date survives only if it parses and falls inside the target
    quarter (a date outside it would put the new rock's milestone in another
    quarter), blank milestones are dropped and the list is capped. No
    milestones at all is a valid answer — the remaining work may be one step.
    """
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="AI draft was not a JSON object")
    description = str(data.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=502, detail="AI draft missing description")
    start, end = _quarter_bounds(year, q)
    raw_milestones = data.get("milestones")
    milestones: list[RockMilestone] = []
    for m in raw_milestones if isinstance(raw_milestones, list) else []:
        if not isinstance(m, dict):
            continue
        text = str(m.get("description") or "").strip()
        if not text:
            continue
        due = _iso_date(m.get("due_date"))
        if due is not None and not start.date() <= due <= end.date():
            due = None
        milestones.append(
            RockMilestone(description=text, completed=False, due_date=due)
        )
        if len(milestones) == _CARRYOVER_MAX_MILESTONES:
            break
    return RockAIDraftResponse(description=description, milestones=milestones)


@router.post("/{rock_id}/carry-over/ai-draft", response_model=RockAIDraftResponse)
@attribute_to("eos_rock_carryover")
async def ai_draft_rock_carryover(
    rock_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    payload: RockCarryOverAIDraftRequest | None = None,
):
    """Re-draft a rock for the quarter it is being carried into, on demand.

    Rocks are written "Noun-Verb-by-Date", so a carried rock's description is
    out of date by definition, and its open milestones need new dates. Gemini
    keeps the commitment, re-dates it for the target quarter and turns the
    remaining work into milestones, from the source's milestones and its latest
    weekly updates. The answer only fills the carry-over dialog; the user edits
    it and nothing is saved until they do.
    """
    payload = payload or RockCarryOverAIDraftRequest()
    source = _live_rock_or_404(db, rock_id)
    require_can_read(current_user, source, db)  # 404 before 403: hide ET rocks
    require_can_update_rock(current_user, source, db)
    year, q = _carry_target(source, payload.quarter_year, payload.quarter_number)

    updates = (
        db.query(RockWeeklyUpdate)
        .filter(
            RockWeeklyUpdate.rock_id == source.id,
            RockWeeklyUpdate.is_deleted == False,  # noqa: E712
        )
        .order_by(
            RockWeeklyUpdate.update_date.desc(), RockWeeklyUpdate.created_at.desc()
        )
        .limit(3)
        .all()
    )
    data = await _gemini_json(_carryover_prompt(source, updates, year, q))
    return _carryover_draft(data, year, q)
