"""EOS scorecard — metrics + weekly entries.

Mounted at `/api/eos/scorecard`. Upstream's `Measurable` is our
`ScorecardMetric`; upstream's `measurable_id` FK is our `metric_id`.

The upstream HR-system sync endpoint is intentionally NOT ported — upstream
it is a stub with no real sync logic. A clean port would land alongside a
Workspace sync rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func as sa_func
from sqlalchemy.exc import IntegrityError

from backend.models.eos.scorecard import (
    ScorecardEntryCreate,
    ScorecardEntryResponse,
    ScorecardEntryUpdate,
    ScorecardMetricCreate,
    ScorecardMetricResponse,
    ScorecardMetricUpdate,
    ScorecardReorderRequest,
)
from backend.routers.eos._authz import (
    can_mutate,
    require_can_mutate,
    require_can_record_scorecard_value,
    require_exec_scope_allowed,
    visible_measurable_ids,
)
from backend.routers.eos._shared import CurrentUser, DBDep
from backend.routers.eos.me import EXEC_SCOPE, EXEC_SCOPE_LABEL, team_scopes_for
from backend.routers.eos.people import eligible_owner_people
from rubik_core.company.profile import company_name_for_documents, get_company_profile
from rubik_core.database.eos import Person, ScorecardEntry, ScorecardMetric
from rubik_core.eos.scorecard_goals import derive_goal_fields, parse_goal_text
from rubik_core.ingestion.eos.scorecard_xlsx import (
    XLSX_MEDIA_TYPE,
    ParsedRow,
    RawCell,
    ScorecardParseError,
    build_scorecard_template_workbook,
    parse_scorecard_workbook,
    past_sundays,
    snap_to_sunday,
    storage_value,
    template_filename,
)

router = APIRouter(prefix="/scorecard", tags=["EOS: Scorecard"])


def _fill_goal_from_label(metric: ScorecardMetric) -> dict[str, str]:
    """Complete a half-set goal from the measurable's own authored label.

    Colouring needs BOTH ``comparator`` and ``target_value``; the Goal column
    only needs ``goal_description``. So a measurable can read as having a goal
    ("> 98%") and colour nothing, which every user reports as broken colouring
    rather than as a missing target. When the label already says what the target
    is, we take it. Never overwrites a half somebody set deliberately — see
    ``derive_goal_fields``. Returns what it changed, for callers that count.
    """
    fields = derive_goal_fields(
        goal_description=metric.goal_description,
        comparator=metric.comparator,
        target_value=metric.target_value,
        value_type=metric.value_type,
    )
    for field, value in fields.items():
        setattr(metric, field, value)
    return fields


# -- Metrics ----------------------------------------------------------


@router.get("/metrics", response_model=list[ScorecardMetricResponse])
def list_metrics(
    db: DBDep,
    current_user: CurrentUser,
    owner_id: UUID | None = None,
    department_name: str | None = None,
    is_exec: bool | None = None,
):
    """List measurables the caller may read. Team filters mirror /rocks and
    /issues: ``is_exec=true`` → the Exec/Leadership scorecard,
    ``department_name`` → one team's (case-insensitive). RLAC applies first."""
    query = db.query(ScorecardMetric).filter(
        ScorecardMetric.is_deleted == False  # noqa: E712
    )
    # Row-level access: limit to metrics the caller may read (None = unrestricted).
    visible_ids = visible_measurable_ids(current_user, db)
    if visible_ids is not None:
        query = query.filter(ScorecardMetric.id.in_(visible_ids))
    if owner_id:
        query = query.filter(ScorecardMetric.owner_id == owner_id)
    if is_exec is not None:
        query = query.filter(ScorecardMetric.is_exec == is_exec)
    if department_name:
        query = query.filter(
            sa_func.lower(ScorecardMetric.department_name) == department_name.lower()
        )
    return query.all()


@router.post(
    "/metrics",
    response_model=ScorecardMetricResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_metric(
    db: DBDep, current_user: CurrentUser, metric_in: ScorecardMetricCreate
):
    if metric_in.is_exec:
        require_exec_scope_allowed(current_user, True)
    metric = ScorecardMetric(**metric_in.model_dump(), created_by=current_user.email)
    _fill_goal_from_label(metric)
    db.add(metric)
    db.commit()
    db.refresh(metric)
    return metric


@router.post("/metrics/reorder", response_model=list[ScorecardMetricResponse])
def reorder_metrics(
    db: DBDep, current_user: CurrentUser, payload: ScorecardReorderRequest
):
    """Set the display order for a block of measurables (drag-and-drop).

    ``ordered_ids`` is one owner's measurables in their new order; each one's
    array index becomes its ``sort_order``. Declared before ``/{metric_id}`` so
    the literal path wins over the UUID param. Every id is edit-checked with the
    same ``require_can_mutate`` gate as a field edit — you can only reorder
    measurables you could already edit. Unknown or duplicate ids are rejected as
    a whole (nothing lands half-done) so a stale client can't silently drop a
    measurable's position.
    """
    if not payload.ordered_ids:
        return []
    if len(set(payload.ordered_ids)) != len(payload.ordered_ids):
        raise HTTPException(status_code=400, detail="Duplicate ids in ordered_ids.")

    metrics = (
        db.query(ScorecardMetric)
        .filter(
            ScorecardMetric.id.in_(payload.ordered_ids),
            ScorecardMetric.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    by_id = {m.id: m for m in metrics}
    missing = [str(i) for i in payload.ordered_ids if i not in by_id]
    if missing:
        raise HTTPException(
            status_code=404, detail=f"Measurable(s) not found: {', '.join(missing)}"
        )

    for metric in metrics:
        require_can_mutate(current_user, metric, db)

    for position, metric_id in enumerate(payload.ordered_ids):
        metric = by_id[metric_id]
        metric.sort_order = position
        metric.updated_by = current_user.email

    db.commit()
    # Return in the order requested so the client can trust the echo.
    for metric in metrics:
        db.refresh(metric)
    return [by_id[i] for i in payload.ordered_ids]


@router.patch("/metrics/{metric_id}", response_model=ScorecardMetricResponse)
def update_metric(
    metric_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    metric_in: ScorecardMetricUpdate,
):
    metric = (
        db.query(ScorecardMetric)
        .filter(
            ScorecardMetric.id == metric_id,
            ScorecardMetric.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not metric:
        raise HTTPException(status_code=404, detail="Metric not found")

    require_can_mutate(current_user, metric, db)
    for field, value in metric_in.model_dump(exclude_unset=True).items():
        setattr(metric, field, value)
    _fill_goal_from_label(metric)

    metric.updated_by = current_user.email  # Who flipped on/off-track
    db.commit()
    db.refresh(metric)
    return metric


@router.delete("/metrics/{metric_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_metric(metric_id: UUID, db: DBDep, current_user: CurrentUser):
    metric = db.query(ScorecardMetric).filter(ScorecardMetric.id == metric_id).first()
    if not metric:
        raise HTTPException(status_code=404, detail="Metric not found")

    require_can_mutate(current_user, metric, db)
    metric.is_deleted = True
    metric.updated_by = current_user.email  # Attribute the delete
    db.commit()
    return None


# -- Entries ----------------------------------------------------------


@router.get("/entries", response_model=list[ScorecardEntryResponse])
def list_entries(
    db: DBDep,
    current_user: CurrentUser,
    metric_id: UUID | None = None,
    since: datetime | None = None,
    weeks: int | None = None,
):
    """List scorecard entries — optionally filtered to one metric.

    Rolling scorecard: the frontend needs entries ordered
    newest-first so it can render a rolling weekly grid. Two
    convenience filters help cap the payload:

    - ``since=<ISO datetime>`` — only entries whose ``week_start_date``
      is >= this value.
    - ``weeks=N`` — only the last N weeks (server computes the cutoff
      as today minus 7*N days). When both are passed, ``since`` wins.

    Ordering is descending by ``week_start_date`` so the response
    matches the "newest week on the left" UX of a rolling grid.
    """
    query = db.query(ScorecardEntry).filter(
        ScorecardEntry.is_deleted == False  # noqa: E712
    )
    # Row-level access: entries inherit their parent metric's visibility.
    visible_ids = visible_measurable_ids(current_user, db)
    if visible_ids is not None:
        query = query.filter(ScorecardEntry.metric_id.in_(visible_ids))
    if metric_id:
        query = query.filter(ScorecardEntry.metric_id == metric_id)

    cutoff: datetime | None = since
    if cutoff is None and weeks and weeks > 0:
        cutoff = datetime.utcnow() - timedelta(days=7 * weeks)
    if cutoff is not None:
        query = query.filter(ScorecardEntry.week_start_date >= cutoff)

    return query.order_by(ScorecardEntry.week_start_date.desc()).all()


# Rolling scorecard grid — one request returns everything the
# frontend needs to render the "metric × week" grid plus running
# averages. Saves N+1 round-trips when the page first opens.


@router.get("/grid")
def scorecard_grid(
    db: DBDep,
    current_user: CurrentUser,
    department_name: str | None = None,
    is_exec: bool = False,
    weeks: int = 13,
):
    """Rolling scorecard view.

    Returns:
    - ``weeks``: the last ``weeks`` Sundays, newest-first — a CALENDAR axis,
      independent of what data exists.
    - ``metrics``: list of metrics with per-week values, per-week notes, and
      rolling averages (last 4 weeks and last 13 weeks). Average is computed
      only when at least one value in the window parses as a number;
      string values that don't parse (e.g. "TBD") are skipped.

    Filters:
    - ``department_name``: restrict to one team. Caller controls scope.
    - ``weeks``: how many trailing weeks to include (default 13 — a
      quarter — matches the EOS retrospective cadence).

    The axis used to be the *union of weeks that had entries*, which made the
    grid lie about its own shape: a team three weeks into using the scorecard
    got a three-column grid, so there was no visible gap to fill in and the
    exec team read the sparseness as "the colouring is broken". A calendar axis
    renders every week whether or not anyone filled it, matching the page's
    ``getPastWeeks()`` and the Sunday-anchoring rule in ``scorecard_xlsx``.
    """
    if weeks <= 0:
        weeks = 13

    cutoff = datetime.utcnow() - timedelta(days=7 * weeks)

    metrics_q = db.query(ScorecardMetric).filter(
        ScorecardMetric.is_deleted == False  # noqa: E712
    )
    # Row-level access: cap to metrics the caller may read, before the
    # caller-controlled team scoping below (None = unrestricted).
    visible_ids = visible_measurable_ids(current_user, db)
    if visible_ids is not None:
        metrics_q = metrics_q.filter(ScorecardMetric.id.in_(visible_ids))
    # Team scoping. Exec/Leadership grid → is_exec metrics; a
    # department grid → that department's metrics; neither → unscoped
    # (every metric the caller can see, e.g. an admin overview).
    if is_exec:
        metrics_q = metrics_q.filter(ScorecardMetric.is_exec.is_(True))
    elif department_name:
        metrics_q = metrics_q.filter(
            sa_func.lower(ScorecardMetric.department_name) == department_name.lower()
        )
    metrics = metrics_q.all()

    entries = (
        db.query(ScorecardEntry)
        .filter(
            ScorecardEntry.is_deleted == False,  # noqa: E712
            ScorecardEntry.week_start_date >= cutoff,
            ScorecardEntry.metric_id.in_([m.id for m in metrics]) if metrics else False,
        )
        .order_by(ScorecardEntry.week_start_date.desc())
        .all()
    )

    # The canonical week-axis: the last ``weeks`` Sundays, newest first. Built
    # from the calendar, not from the data, so an unfilled week renders as an
    # empty cell instead of vanishing from the grid.
    week_axis = [d.isoformat() for d in past_sundays(weeks)]

    def _to_number(raw: str | None) -> float | None:
        if raw is None:
            return None
        try:
            return float(raw.replace(",", "").replace("%", "").strip())
        except (ValueError, AttributeError):
            return None

    by_metric: dict[str, dict[str, str | None]] = {
        str(m.id): {w: None for w in week_axis} for m in metrics
    }
    by_metric_num: dict[str, dict[str, float | None]] = {
        str(m.id): {w: None for w in week_axis} for m in metrics
    }
    notes_by_metric: dict[str, dict[str, str | None]] = {str(m.id): {} for m in metrics}
    known_weeks = set(week_axis)
    # Entries arrive newest-first, and each is filed under the SUNDAY of its
    # week rather than its literal stored date.
    #
    # Snapping matters because a non-Sunday-anchored row is the single most
    # common scorecard data bug (see ``scorecard_xlsx.snap_to_sunday``): the
    # page's axis is Sundays and matches exactly, so a Monday-dated entry has
    # always rendered as "—" there. The old union-of-entries axis here papered
    # over that by inventing a Monday column, which is why the two surfaces
    # disagreed about the same data. Snapping makes them agree AND surfaces the
    # rows that were invisible, instead of hiding a few more.
    #
    # When two rows snap to the same Sunday, the newest one carrying an actual
    # value wins (iteration is newest-first, and a value-less row doesn't claim
    # the cell — so a note-only entry can't hide a real number recorded a day
    # earlier in the same week).
    for e in entries:
        wk = snap_to_sunday(e.week_start_date.date()).isoformat()
        # Still possible to fall outside the axis — the cutoff is a rolling
        # 7*weeks days, which reaches a little further back than the oldest
        # Sunday column. Skip rather than KeyError.
        if wk not in known_weeks:
            continue
        metric_key = str(e.metric_id)
        if by_metric[metric_key][wk] is not None:
            continue
        by_metric[metric_key][wk] = e.value
        by_metric_num[metric_key][wk] = _to_number(e.value)
        if e.note:
            notes_by_metric[metric_key].setdefault(wk, e.note)

    def _avg(values: list[float | None]) -> float | None:
        nums = [v for v in values if v is not None]
        return round(sum(nums) / len(nums), 2) if nums else None

    out_metrics = []
    for m in metrics:
        nums_by_week = by_metric_num[str(m.id)]
        values_in_order = [nums_by_week[w] for w in week_axis]
        out_metrics.append(
            {
                "id": str(m.id),
                "name": m.name,
                "description": m.description,
                "owner_id": str(m.owner_id),
                # The team's drag order within an owner's block. Without it the
                # L10 grid fell back to alphabetical while the page showed the
                # saved order, so the two disagreed about the same scorecard.
                "sort_order": m.sort_order,
                "department_name": m.department_name,
                "is_exec": m.is_exec,
                "goal_description": m.goal_description,
                "target_value": m.target_value,
                "unit": m.unit,
                # Leadership Scorecard format: render hint + goal direction.
                "value_type": m.value_type,
                "comparator": m.comparator,
                "on_track": m.on_track,
                "entries": by_metric[str(m.id)],
                # Sparse on purpose — only the cells that actually carry a
                # note, keyed the same way as ``entries``.
                "notes": notes_by_metric[str(m.id)],
                "average_4w": _avg(values_in_order[:4]),
                "average_13w": _avg(values_in_order[:13]),
            }
        )

    return {
        "weeks": week_axis,
        "metrics": out_metrics,
    }


@router.post(
    "/entries",
    response_model=ScorecardEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_entry(db: DBDep, current_user: CurrentUser, entry_in: ScorecardEntryCreate):
    # Same gate as upsert_entry. The standalone Scorecard page hits this (POST)
    # for a brand-new cell and PATCH /entries/{id} for an existing one; both
    # must be gated so a stranger can't seed a confidential metric via the
    # never-existed path. The gate is the team-wide VALUE grant, not the
    # measurable's own mutate rule (see ``can_record_scorecard_value_in_scope``).
    metric = (
        db.query(ScorecardMetric)
        .filter(ScorecardMetric.id == entry_in.metric_id)
        .first()
    )
    if metric is None:
        raise HTTPException(status_code=404, detail="Metric not found")
    require_can_record_scorecard_value(current_user, metric, db)

    entry = ScorecardEntry(**entry_in.model_dump(), created_by=current_user.email)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.post(
    "/entries/upsert",
    response_model=ScorecardEntryResponse,
)
def upsert_entry(db: DBDep, current_user: CurrentUser, entry_in: ScorecardEntryCreate):
    """Idempotent insert-or-update of one week's scorecard value.

    The in-meeting Scorecard panel needs to record THIS week's number
    without the client tracking per-cell entry ids, but the unique
    ``(metric_id, week_start_date)`` index makes a naive repeat POST
    collide. This endpoint matches on ``metric_id`` + the calendar DAY of
    ``week_start_date`` (tolerant of time-of-day differences between the
    standalone page and the meeting panel) and updates in place if a row
    already exists, else inserts a new one.
    """
    # Recording a week's value is gated on the parent metric — the owner (a
    # non-exec owner of a Leadership measurable included), their manager,
    # leadership, admin/Exec, or any member of the measurable's team (never on
    # an Exec row). Without this gate any authenticated caller who clears the
    # EOS gate could overwrite ANY metric's weekly value, including
    # confidential Exec/Leadership-Scorecard rows. Mirrors POST and PATCH
    # /entries.
    metric = (
        db.query(ScorecardMetric)
        .filter(ScorecardMetric.id == entry_in.metric_id)
        .first()
    )
    if metric is None:
        raise HTTPException(status_code=404, detail="Metric not found")
    require_can_record_scorecard_value(current_user, metric, db)

    day_start = datetime(
        entry_in.week_start_date.year,
        entry_in.week_start_date.month,
        entry_in.week_start_date.day,
    )
    day_end = day_start + timedelta(days=1)
    existing = (
        db.query(ScorecardEntry)
        .filter(
            ScorecardEntry.metric_id == entry_in.metric_id,
            ScorecardEntry.week_start_date >= day_start,
            ScorecardEntry.week_start_date < day_end,
            ScorecardEntry.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if existing:
        existing.value = entry_in.value
        # Only overwrite the note when the caller actually sent one. The
        # in-meeting panel saves `{metric_id, week_start_date, value}` and
        # nothing else, so an unconditional assignment would silently erase a
        # note someone wrote on the page before the meeting. (Same rule as
        # ``update_attendance``'s per-field guards.)
        if "note" in entry_in.model_fields_set:
            existing.note = entry_in.note
        existing.updated_by = current_user.email
        db.commit()
        db.refresh(existing)
        return existing

    entry = ScorecardEntry(**entry_in.model_dump(), created_by=current_user.email)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.patch("/entries/{entry_id}", response_model=ScorecardEntryResponse)
def update_entry(
    entry_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    entry_in: ScorecardEntryUpdate,
):
    entry = (
        db.query(ScorecardEntry)
        .filter(
            ScorecardEntry.id == entry_id,
            ScorecardEntry.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    # An entry is gated on its parent metric (team-wide value grant).
    metric = (
        db.query(ScorecardMetric).filter(ScorecardMetric.id == entry.metric_id).first()
    )
    if metric is not None:
        require_can_record_scorecard_value(current_user, metric, db)
    for field, value in entry_in.model_dump(exclude_unset=True).items():
        setattr(entry, field, value)

    entry.updated_by = current_user.email  # Who edited this week's value
    db.commit()
    db.refresh(entry)
    return entry


# -- Template & upload (team self-service) -----------------------------
#
# Same recipe as the VTO uploader: download a filled-in-by-hand .xlsx template,
# upload it, and the rows land in the DB. The difference is the access model.
# The VTO is one company-wide document with a single Exec/admin write gate; a
# scorecard is many per-team rows, so the gate is PER ROW and is exactly the
# page's own:
#
#   * reads (what the template can pre-fill) — ``visible_measurable_ids``,
#     narrowed further to rows ``can_mutate`` allows, so the download can never
#     scaffold a row whose upload would be rejected;
#   * updating a measurable or one of its weekly values — ``can_mutate``, the
#     same gate PATCH /metrics and POST /entries run;
#   * creating a measurable — the team must be one of the caller's own scopes
#     (the vocabulary the page's Team dropdown is built from), and the Exec team
#     additionally goes through ``require_exec_scope_allowed``;
#   * owners — only people the caller may already assign (the same roster the
#     page's Owner dropdown shows).
#
# The upload is ALL-OR-NOTHING: every row is validated before anything is
# written, so a permission error or a typo can't leave a half-imported
# scorecard behind. Blank week cells are left alone (never cleared) — a bulk
# import must not silently wipe a number somebody typed on the page.

_DEFAULT_TEMPLATE_WEEKS = 13  # a quarter — the page's default window
_MAX_TEMPLATE_WEEKS = 52


@dataclass(frozen=True)
class _Team:
    """One team the caller may publish a measurable to."""

    label: str
    is_exec: bool
    department_name: str | None


@dataclass
class _RowPlan:
    """A validated row, ready to apply once every other row has passed too."""

    row: ParsedRow
    metric: ScorecardMetric | None = None  # None → create
    team: _Team | None = None  # creates only (team is fixed after create)
    owner: Person | None = None
    value_type: str = "number"  # effective type, governs unit conversion
    fields: dict = dc_field(default_factory=dict)  # metric fields to set


def _writable_teams(db, current_user) -> list[_Team]:
    """The teams the caller may add measurables to — the same list that
    populates the Team dropdown on the page (``GET /api/eos/me/meeting-scopes``),
    so the uploader can't be used to reach a team the UI wouldn't offer."""
    teams: list[_Team] = []
    for scope in team_scopes_for(db, current_user):
        value = (scope.get("value") or "").strip()
        label = (scope.get("label") or value).strip()
        if scope.get("kind") == "exec" or value == EXEC_SCOPE:
            teams.append(
                _Team(
                    label=label or EXEC_SCOPE_LABEL, is_exec=True, department_name=None
                )
            )
        elif value:
            teams.append(
                _Team(label=label or value, is_exec=False, department_name=value)
            )
    return teams


def _resolve_team(teams: list[_Team], raw: str | None) -> _Team | None:
    """Match a Team cell (or the ``team`` query param) against the caller's
    writable teams — by label ("Executive View"), by department name, or by the
    ``__exec__`` sentinel the frontend sends. ``None`` when it's not one of
    them; the caller reports that rather than silently retargeting the row."""
    key = (raw or "").strip().lower()
    if not key:
        return None
    for team in teams:
        candidates = {team.label.lower()}
        if team.is_exec:
            candidates.add(EXEC_SCOPE)
        if team.department_name:
            candidates.add(team.department_name.lower())
        if key in candidates:
            return team
    return None


def _team_label_for(metric: ScorecardMetric, teams: list[_Team]) -> str:
    """The Team cell to pre-fill for an existing measurable. Cosmetic only —
    the column is ignored when updating (a team change would declassify an Exec
    row, which ``require_exec_scope_allowed`` exists to prevent)."""
    if metric.is_exec:
        return next((t.label for t in teams if t.is_exec), EXEC_SCOPE_LABEL)
    return metric.department_name or ""


def _existing_entry(db, metric_id, week: date):
    """This metric's row for ``week``, matched on the calendar DAY so an entry
    written by the page (an ISO instant) and one written here (midnight) are the
    same week — mirrors ``upsert_entry``."""
    day_start = datetime(week.year, week.month, week.day)
    return (
        db.query(ScorecardEntry)
        .filter(
            ScorecardEntry.metric_id == metric_id,
            ScorecardEntry.week_start_date >= day_start,
            ScorecardEntry.week_start_date < day_start + timedelta(days=1),
            ScorecardEntry.is_deleted == False,  # noqa: E712
        )
        .first()
    )


def _target_value(goal: RawCell | None, value_type: str) -> str | None:
    """The canonical target for a template row's ``Goal`` cell.

    The column is headed "Goal", so people type a goal into it — "<9.25%",
    "> 1 unit" — where the bare number belongs. ``storage_value`` hands
    non-numeric cell text back verbatim, which is right for a weekly cell
    reading "TBD" and wrong for a target: the text is present but can never be
    compared against, so the measurable shows its goal and colours nothing. Read
    the number back out of it. This also rescues a percent goal typed WITH its
    sign ("9.25%"), which would otherwise store as 9.25 and compare 100× high
    against fractional weekly values.

    A properly numeric cell is untouched — ``parse_goal_text`` refuses a bare
    number on a percent measurable precisely so canonical storage passes
    straight through.
    """
    if goal is None:
        return None
    stored = storage_value(goal, value_type)
    parsed = parse_goal_text(stored, value_type)
    return parsed.target_value if parsed is not None else stored


@router.get("/template")
def download_scorecard_template(
    db: DBDep,
    current_user: CurrentUser,
    team: str | None = None,
    weeks: int = Query(_DEFAULT_TEMPLATE_WEEKS, ge=1, le=_MAX_TEMPLATE_WEEKS),
):
    """Download the **blank** .xlsx template the upload endpoint expects: the
    header row, the last ``weeks`` weekly columns, and empty rows to fill in.

    It deliberately carries **no scorecard content**. The first cut pre-filled
    the caller's own measurables and values, which made this endpoint a read of
    scorecard data — access-scoped, but for an admin or exec that still meant
    the downloaded file contained Exec/Leadership rows, i.e. confidential
    numbers sitting in a spreadsheet on a laptop. A bulk-entry sheet doesn't
    need them, so it doesn't get them.

    What *is* caller-specific: the Team and Owner dropdown vocabularies, both
    already access-scoped (``team_scopes_for`` / ``eligible_owner_people`` — the
    same lists the page's pickers show). ``team`` takes the same value the page's
    Team picker uses (a department name or the ``__exec__`` sentinel), defaults
    to the caller's first scope, and now only names the file and the sheet's
    "generated for" note.
    """
    teams = _writable_teams(db, current_user)
    if not teams:
        raise HTTPException(
            status_code=400,
            detail=(
                "You're not on a team yet, so there's no scorecard to import. "
                "Ask an admin to set your department first."
            ),
        )
    selected = _resolve_team(teams, team) if team else teams[0]
    if selected is None:
        raise HTTPException(
            status_code=403,
            detail="You can't import a scorecard for that team.",
        )

    payload = build_scorecard_template_workbook(
        weeks=past_sundays(weeks),
        team_labels=[t.label for t in teams],
        owner_emails=sorted(
            {p.email for p in eligible_owner_people(db, current_user) if p.email}
        ),
        current_team_label=selected.label,
        company_name=company_name_for_documents(get_company_profile(db)),
    )
    filename = template_filename(selected.label)
    return Response(
        content=payload,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _plan_existing_row(
    row: ParsedRow,
    *,
    db,
    current_user,
    teams: list[_Team],
    visible_ids,
    eligible_emails: set[str],
    errors: list[str],
    warnings: list[str],
) -> _RowPlan | None:
    """Validate a row that carries a Metric ID (an update)."""
    try:
        metric_id = UUID(str(row.metric_id))
    except (TypeError, ValueError):
        errors.append(
            f'Row {row.row_number}: "{row.metric_id}" isn\'t a valid Metric ID. '
            "Leave the column blank to add a new measurable."
        )
        return None

    metric = (
        db.query(ScorecardMetric)
        .filter(
            ScorecardMetric.id == metric_id,
            ScorecardMetric.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    # One message for "gone" and "not yours" so a caller probing ids can't tell
    # a confidential Exec measurable from a deleted one (same reason
    # ``require_can_read`` answers 404).
    if metric is None or (visible_ids is not None and metric.id not in visible_ids):
        errors.append(
            f"Row {row.row_number}: that measurable no longer exists, or you "
            "don't have access to it. Download a fresh template."
        )
        return None
    if not can_mutate(current_user, metric, db):
        errors.append(
            f"Row {row.row_number}: you don't have permission to change "
            f'"{metric.name}".'
        )
        return None

    plan = _RowPlan(
        row=row,
        metric=metric,
        value_type=row.value_type or metric.value_type or "number",
    )

    # The team is fixed at create time (the page doesn't let you change it
    # either) — moving an Exec measurable into a department would declassify it.
    if (
        row.team_label
        and _team_label_for(metric, teams).lower() != row.team_label.strip().lower()
    ):
        warnings.append(
            f"Row {row.row_number}: the Team column is ignored when updating an "
            f'existing measurable — "{metric.name}" stays on its current team.'
        )

    if row.owner_email:
        owner = _person_by_email(db, row.owner_email)
        if owner is None:
            errors.append(
                f"Row {row.row_number}: no person found for {row.owner_email}."
            )
            return None
        if (
            owner.id != metric.owner_id
            and row.owner_email.lower() not in eligible_emails
        ):
            errors.append(
                f"Row {row.row_number}: you can't hand this measurable to "
                f"{row.owner_email}."
            )
            return None
        plan.owner = owner

    if row.name:
        plan.fields["name"] = row.name
    if row.description is not None:
        plan.fields["description"] = row.description
    if row.goal_description is not None:
        plan.fields["goal_description"] = row.goal_description
    if row.value_type is not None:
        plan.fields["value_type"] = row.value_type
    if row.comparator is not None:
        plan.fields["comparator"] = row.comparator
    if row.goal is not None:
        plan.fields["target_value"] = _target_value(row.goal, plan.value_type)
    return plan


def _plan_new_row(
    row: ParsedRow,
    *,
    db,
    current_user,
    teams: list[_Team],
    eligible_emails: set[str],
    errors: list[str],
    warnings: list[str],
) -> _RowPlan | None:
    """Validate a row with no Metric ID (a new measurable)."""
    if not row.name:
        errors.append(
            f"Row {row.row_number}: add a Measurable name (or clear the row)."
        )
        return None
    if not row.owner_email:
        errors.append(f'Row {row.row_number}: "{row.name}" needs an Owner Email.')
        return None
    owner = _person_by_email(db, row.owner_email)
    if owner is None:
        errors.append(f"Row {row.row_number}: no person found for {row.owner_email}.")
        return None
    if row.owner_email.lower() not in eligible_emails:
        errors.append(
            f"Row {row.row_number}: you can't create a measurable owned by "
            f"{row.owner_email}."
        )
        return None

    team = _resolve_team(teams, row.team_label)
    if team is None:
        allowed = ", ".join(t.label for t in teams)
        got = f'"{row.team_label}"' if row.team_label else "nothing"
        errors.append(
            f'Row {row.row_number}: pick a Team for "{row.name}" from the '
            f"dropdown (you got {got}; yours are: {allowed})."
        )
        return None
    if team.is_exec:
        # Belt-and-braces: the scope list above can only offer the Exec team to
        # an exec, but this is the authoritative gate the create endpoint uses.
        try:
            require_exec_scope_allowed(current_user, True)
        except HTTPException:
            errors.append(
                f"Row {row.row_number}: only Exec team members can add "
                "Executive View measurables."
            )
            return None

    value_type = row.value_type or "number"

    # A blank-ID row naming a measurable that already exists (and that the
    # caller may edit) is almost always "I typed it in by hand instead of using
    # the pre-filled row" — update it rather than minting a confusing duplicate.
    existing = _find_matching_metric(db, name=row.name, owner_id=owner.id, team=team)
    if existing is not None and can_mutate(current_user, existing, db):
        warnings.append(
            f'Row {row.row_number}: "{row.name}" already exists for '
            f"{row.owner_email} on {team.label} — updated it instead of adding "
            "a second one."
        )
        plan = _RowPlan(
            row=row,
            metric=existing,
            value_type=row.value_type or existing.value_type or "number",
        )
        plan.fields["name"] = row.name
        if row.description is not None:
            plan.fields["description"] = row.description
        if row.goal_description is not None:
            plan.fields["goal_description"] = row.goal_description
        if row.value_type is not None:
            plan.fields["value_type"] = row.value_type
        if row.comparator is not None:
            plan.fields["comparator"] = row.comparator
        if row.goal is not None:
            plan.fields["target_value"] = _target_value(row.goal, plan.value_type)
        return plan

    return _RowPlan(row=row, metric=None, team=team, owner=owner, value_type=value_type)


def _person_by_email(db, email: str) -> Person | None:
    return (
        db.query(Person)
        .filter(
            sa_func.lower(Person.email) == email.strip().lower(),
            Person.is_deleted == False,  # noqa: E712
        )
        .first()
    )


def _find_matching_metric(db, *, name: str, owner_id, team: _Team):
    """An existing measurable with the same name+owner on the same team. Keyed
    on (team, name, owner) — NOT name alone, because the same measurable name
    legitimately repeats across the reps on a team scorecard."""
    query = db.query(ScorecardMetric).filter(
        sa_func.lower(ScorecardMetric.name) == name.strip().lower(),
        ScorecardMetric.owner_id == owner_id,
        ScorecardMetric.is_deleted == False,  # noqa: E712
    )
    if team.is_exec:
        query = query.filter(ScorecardMetric.is_exec.is_(True))
    else:
        query = query.filter(
            ScorecardMetric.is_exec.is_(False),
            sa_func.lower(ScorecardMetric.department_name)
            == (team.department_name or "").lower(),
        )
    return query.first()


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_scorecard(
    db: DBDep,
    current_user: CurrentUser,
    file: UploadFile = File(...),
):
    """Apply a filled-in scorecard template.

    Every row is checked against the page's own access rules first (see the
    section note above) and the whole workbook is applied only if all of them
    pass — otherwise nothing is written and the response lists what to fix,
    row by row.
    """
    raw = await file.read()
    try:
        parsed = parse_scorecard_workbook(raw)
    except ScorecardParseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    teams = _writable_teams(db, current_user)
    visible_ids = visible_measurable_ids(current_user, db)
    if visible_ids is not None:
        visible_ids = set(visible_ids)
    eligible_emails = {
        (p.email or "").lower() for p in eligible_owner_people(db, current_user)
    }

    errors: list[str] = []
    warnings: list[str] = []
    plans: list[_RowPlan] = []
    seen_metric_ids: dict[str, int] = {}
    seen_new_keys: dict[tuple, int] = {}

    for row in parsed.rows:
        if row.metric_id:
            first = seen_metric_ids.get(row.metric_id.strip().lower())
            if first is not None:
                errors.append(
                    f"Row {row.row_number}: the same measurable also appears on "
                    f"row {first}. Keep one row per measurable."
                )
                continue
            seen_metric_ids[row.metric_id.strip().lower()] = row.row_number
            plan = _plan_existing_row(
                row,
                db=db,
                current_user=current_user,
                teams=teams,
                visible_ids=visible_ids,
                eligible_emails=eligible_emails,
                errors=errors,
                warnings=warnings,
            )
        else:
            key = (
                (row.name or "").strip().lower(),
                (row.owner_email or "").strip().lower(),
                (row.team_label or "").strip().lower(),
            )
            first = seen_new_keys.get(key)
            if first is not None:
                errors.append(
                    f'Row {row.row_number}: "{row.name}" is already on row '
                    f"{first} for the same owner and team. Keep one row per "
                    "measurable."
                )
                continue
            seen_new_keys[key] = row.row_number
            plan = _plan_new_row(
                row,
                db=db,
                current_user=current_user,
                teams=teams,
                eligible_emails=eligible_emails,
                errors=errors,
                warnings=warnings,
            )
        if plan is not None:
            plans.append(plan)

    if errors:
        # Nothing has been written — the whole upload is rejected so a partial
        # scorecard never lands.
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    f"Nothing was imported — {len(errors)} row"
                    f"{'' if len(errors) == 1 else 's'} need fixing."
                ),
                "errors": errors,
            },
        )

    metrics_updated = entries_created = entries_updated = 0
    # Named, not just counted: with a blank template a row is matched to an
    # existing measurable by (team, name, owner), so a misspelled name silently
    # becomes a NEW measurable. Reporting exactly which ones were added is how
    # the user spots that immediately instead of finding a near-duplicate row on
    # the page next week.
    created_names: list[str] = []
    try:
        for plan in plans:
            metric = plan.metric
            if metric is None:
                metric = ScorecardMetric(
                    owner_id=plan.owner.id,
                    department_name=plan.team.department_name,
                    is_exec=plan.team.is_exec,
                    name=plan.row.name,
                    description=plan.row.description,
                    goal_description=plan.row.goal_description,
                    target_value=_target_value(plan.row.goal, plan.value_type),
                    value_type=plan.value_type,
                    comparator=plan.row.comparator,
                    created_by=current_user.email,
                )
                # A template row with Compare + Goal Label but a blank numeric
                # Goal is the commonest way a measurable lands uncolourable.
                _fill_goal_from_label(metric)
                db.add(metric)
                db.flush()  # assign metric.id for the entry FK
                created_names.append(plan.row.name or "")
            else:
                changed = False
                if plan.owner is not None and plan.owner.id != metric.owner_id:
                    metric.owner_id = plan.owner.id
                    changed = True
                for key, value in plan.fields.items():
                    if getattr(metric, key) != value:
                        setattr(metric, key, value)
                        changed = True
                # After the row's own fields, so the label completing a goal
                # sees the comparator/value_type this import just set.
                if _fill_goal_from_label(metric):
                    changed = True
                if changed:
                    metric.updated_by = current_user.email
                    metrics_updated += 1

            for week in sorted(plan.row.values, reverse=True):
                value = storage_value(plan.row.values[week], plan.value_type)
                existing = _existing_entry(db, metric.id, week)
                if existing is None:
                    db.add(
                        ScorecardEntry(
                            metric_id=metric.id,
                            # Sunday midnight — the page keys its columns on the
                            # Sunday date, so any other weekday renders as "—".
                            week_start_date=datetime(week.year, week.month, week.day),
                            value=value,
                            created_by=current_user.email,
                        )
                    )
                    entries_created += 1
                elif existing.value != value:
                    existing.value = value
                    existing.updated_by = current_user.email
                    entries_updated += 1
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "The scorecard changed while you were filling in the template, "
                "so nothing was imported. Download a fresh template and try "
                "again."
            ),
        )

    return {
        "weeks": [w.isoformat() for w in parsed.weeks],
        "rows": len(plans),
        "metrics_created": len(created_names),
        "metrics_created_names": created_names,
        "metrics_updated": metrics_updated,
        "entries_created": entries_created,
        "entries_updated": entries_updated,
        "ignored_columns": parsed.ignored_columns,
        "warnings": warnings,
    }
