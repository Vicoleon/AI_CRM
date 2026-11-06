"""Executive EOS dashboard.

`GET /api/eos/dashboard/executive` returns a Bloom-Growth-style insights
payload — health summary KPIs, per-team rollup, and three weekly time
series — scoped to a quarter (default: current) and optionally narrowed
to one department.

Response shape (single round-trip, the frontend doesn't fan out):

  {
    "scope": {"kind": "company" | "department", "department_name": ...},
    "period": {id, name, start_date, end_date, is_current, prev_id,
               available_quarters: [...]},
    "available_departments": [...],
    "health_summary": {
       "overall_score": int 0-100,
       "overall_label": "Healthy" | "Watch" | "Needs attention",
       "meeting_rating": {avg, target, delta, samples},
       "rock_health": {pct, label, delta, done, not_done, total},
       "todo_completion": {pct, target, delta, done, total}
    },
    "team_performance": [{team_name, initials, color_seed,
                          health_score, meeting_rating, rock_health_pct,
                          todo_completion_pct, avg_issue_age_days, trend}, ...],
    "trends": {
       "meeting_health_over_time": [{week_start, label, avg_rating, samples}, ...],
       "rock_status_over_time": [{week_start, label, on_track, off_track, done}, ...],
       "issue_trend": [{week_start, label, opened}, ...]
    }
  }

Gated on ``current_user.is_exec`` or admin. 403 otherwise.

Keep this read-only — exec uses the dashboard to spot drift, then drills
into per-team pages to mutate.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Query  # noqa: F401 — for type hints only

from backend.routers.eos._shared import (
    CurrentUser,
    DBDep,
    canonical_quarters,
    sync_current_quarter,
)
from rubik_core.database.eos import (
    Issue,
    IssueStatus,
    L10Meeting,
    L10Session,
    Quarter,
    Rock,
    RockWeeklyUpdate,
    Seat,
    Todo,
)

router = APIRouter(prefix="/dashboard", tags=["EOS: Dashboard"])


# ── Constants ────────────────────────────────────────────────────────────

# Targets pulled from EOS canon — Bloom Growth uses these too.
MEETING_RATING_TARGET = 8.0
TODO_COMPLETION_TARGET_PCT = 90

# Composite health weights — sum to 1.0. Roughly: rocks are "what we
# committed to deliver this quarter", todos are "what we said we'd do
# this week", meeting rating is "how the team feels about cadence".
HEALTH_WEIGHTS = {"rocks": 0.45, "todos": 0.30, "meetings": 0.25}


# ── Permission gate ─────────────────────────────────────────────────────


def _require_exec_or_admin(current_user) -> None:
    is_admin = "admin" in (current_user.roles or [])
    is_exec = bool(getattr(current_user, "is_exec", False))
    if not (is_admin or is_exec):
        raise HTTPException(
            status_code=403,
            detail="Executive dashboard is restricted to exec team members and admins.",
        )


# ── Period helpers ──────────────────────────────────────────────────────


def _resolve_period(db, quarter_id: UUID | None) -> tuple[Quarter, Quarter | None]:
    """Return (target_quarter, previous_quarter) for delta computations.

    Default target = the row flagged ``is_current=True``; fallback to the
    most recent quarter by ``start_date`` if no current is set.
    """
    if quarter_id is not None:
        target = (
            db.query(Quarter)
            .filter(
                Quarter.id == quarter_id,
                Quarter.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if not target:
            raise HTTPException(status_code=404, detail="Quarter not found")
    else:
        # Default period must be a real quarter — never a test-pollution row
        # (e.g. "FT7 Q-…") that a fixture left flagged is_current.
        target = next(
            iter(
                canonical_quarters(
                    db.query(Quarter)
                    .filter(
                        Quarter.is_current == True,  # noqa: E712
                        Quarter.is_deleted == False,  # noqa: E712
                    )
                    .order_by(Quarter.start_date.desc())
                    .all()
                )
            ),
            None,
        )
        if target is None:
            target = next(
                iter(
                    canonical_quarters(
                        db.query(Quarter)
                        .filter(Quarter.is_deleted == False)  # noqa: E712
                        .order_by(Quarter.start_date.desc())
                        .all()
                    )
                ),
                None,
            )
        if target is None:
            raise HTTPException(
                status_code=404,
                detail="No quarters configured yet — set one up in /eos to populate the dashboard.",
            )

    prev = next(
        iter(
            canonical_quarters(
                db.query(Quarter)
                .filter(
                    Quarter.is_deleted == False,  # noqa: E712
                    Quarter.start_date < target.start_date,
                )
                .order_by(Quarter.start_date.desc())
                .all()
            )
        ),
        None,
    )
    return target, prev


def _to_datetime(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


def _weeks_in_period(start: date, end: date) -> list[date]:
    """List of Monday-of-week dates spanning [start, end] inclusive.

    Always returns plain ``date`` objects. `Quarter.start_date`/`end_date`
    are DateTime columns, so the caller hands us datetimes — and the trend
    builders below bucket rows by ``.date()`` then look those buckets up by
    these values. A ``datetime`` key never equals (nor hashes like) the
    ``date`` key it was derived from, so leaving them as datetimes silently
    zeroes out every weekly series.
    """
    if isinstance(start, datetime):
        start = start.date()
    if isinstance(end, datetime):
        end = end.date()
    if end < start:
        return []
    # Snap start back to its Monday so first bucket aligns with the first
    # full week the period overlaps (Bloom does it this way).
    first_mon = start - timedelta(days=start.weekday())
    weeks: list[date] = []
    cur = first_mon
    while cur <= end:
        weeks.append(cur)
        cur += timedelta(days=7)
    return weeks


def _week_label(d: date) -> str:
    """'Jan 5' style label for chart x-axis ticks."""
    return d.strftime("%b %-d")


# ── KPI computers ───────────────────────────────────────────────────────


def _meeting_metrics(
    db, start: datetime, end: datetime, department: str | None
) -> dict:
    """Avg L10Session.average_rating across finished sessions in the window."""
    q = db.query(L10Session).filter(
        L10Session.is_deleted == False,  # noqa: E712
        L10Session.average_rating.isnot(None),
        L10Session.actual_start >= start,
        L10Session.actual_start < end,
    )
    if department:
        q = q.join(L10Meeting, L10Session.meeting_id == L10Meeting.id).filter(
            L10Meeting.department_name == department
        )
    sessions = q.all()
    if not sessions:
        return {"avg": None, "samples": 0}
    avg = sum(int(s.average_rating) for s in sessions) / len(sessions)
    return {"avg": round(avg, 1), "samples": len(sessions)}


def _rock_metrics(db, quarter_id: UUID, department: str | None) -> dict:
    """Counts + percent-done for rocks belonging to a quarter."""
    q = db.query(Rock).filter(
        Rock.is_deleted == False,  # noqa: E712
        Rock.quarter_id == quarter_id,
    )
    # Rocks are department-tagged by their own department_name
    # column (matching rocks.py), NOT by the owner's Seat. The old owner→Seat
    # join returned zero rows whenever the owner had no matching Seat, so every
    # per-team dashboard KPI showed a dash.
    if department:
        q = q.filter(Rock.department_name == department)
    rows = q.all()

    done = 0
    on_track = 0
    off_track = 0
    for rock in rows:
        status = (
            rock.status.value if hasattr(rock.status, "value") else str(rock.status)
        )
        if status == "done":
            done += 1
        elif status == "off_track":
            off_track += 1
        else:
            on_track += 1
    total = done + on_track + off_track
    not_done = on_track + off_track
    pct = round(((done + on_track) / total) * 100) if total else None

    if pct is None:
        label = "—"
    elif pct >= 80:
        label = "Healthy"
    elif pct >= 60:
        label = "Watch"
    else:
        label = "Needs attention"

    return {
        "pct": pct,
        "label": label,
        "done": done,
        "on_track": on_track,
        "off_track": off_track,
        "not_done": not_done,
        "total": total,
    }


def _todo_metrics(db, start: datetime, end: datetime, department: str | None) -> dict:
    """Completion ratio of todos created in the window (any status)."""
    q = db.query(Todo).filter(
        Todo.is_deleted == False,  # noqa: E712
        Todo.created_at >= start,
        Todo.created_at < end,
    )
    # Todos are department-tagged by their own department_name
    # column (set at creation), NOT by the owner's Seat.
    if department:
        q = q.filter(Todo.department_name == department)
    rows = q.all()

    done = sum(
        1
        for t in rows
        if (t.status.value if hasattr(t.status, "value") else str(t.status)) == "done"
    )
    total = len(rows)
    pct = round((done / total) * 100) if total else None
    return {"pct": pct, "done": done, "total": total}


def _issue_avg_age_days(db, department: str | None) -> float | None:
    """Mean age of currently-open issues. None = no open issues."""
    q = db.query(Issue).filter(
        Issue.is_deleted == False,  # noqa: E712
        Issue.status == IssueStatus.OPEN,
        Issue.created_at.isnot(None),
    )
    if department:
        q = q.filter(Issue.department_name == department)
    issues = q.all()
    if not issues:
        return None
    now = datetime.utcnow()
    ages = [(now - i.created_at).total_seconds() / 86400 for i in issues]
    return round(sum(ages) / len(ages), 1)


# ── Composite health score ──────────────────────────────────────────────


def _composite_health(
    meeting_avg: float | None,
    rock_pct: int | None,
    todo_pct: int | None,
) -> tuple[int, str]:
    """Weighted 0-100 score, plus a friendly label.

    Components missing data don't contribute (and their weight gets
    redistributed) so the score still makes sense for a brand-new team
    that hasn't logged a meeting or completed a to-do yet.
    """
    parts: list[tuple[float, float]] = []  # (weight, normalized 0..100)
    if meeting_avg is not None:
        parts.append((HEALTH_WEIGHTS["meetings"], (meeting_avg / 10) * 100))
    if rock_pct is not None:
        parts.append((HEALTH_WEIGHTS["rocks"], rock_pct))
    if todo_pct is not None:
        parts.append((HEALTH_WEIGHTS["todos"], todo_pct))

    if not parts:
        return 0, "—"

    total_weight = sum(w for w, _ in parts)
    score = sum(w * v for w, v in parts) / total_weight
    score = max(0, min(100, round(score)))

    if score >= 80:
        label = "Healthy"
    elif score >= 60:
        label = "Watch"
    else:
        label = "Needs attention"
    return score, label


# ── Trends (weekly time series) ─────────────────────────────────────────


def _meeting_health_trend(db, weeks: list[date], department: str | None) -> list[dict]:
    if not weeks:
        return []
    period_start = _to_datetime(weeks[0])
    period_end = _to_datetime(weeks[-1] + timedelta(days=7))

    q = db.query(L10Session).filter(
        L10Session.is_deleted == False,  # noqa: E712
        L10Session.average_rating.isnot(None),
        L10Session.actual_start >= period_start,
        L10Session.actual_start < period_end,
    )
    if department:
        q = q.join(L10Meeting, L10Session.meeting_id == L10Meeting.id).filter(
            L10Meeting.department_name == department
        )
    sessions = q.all()

    # Bucket each session by its Monday-of-week.
    buckets: dict[date, list[int]] = defaultdict(list)
    for s in sessions:
        wk = s.actual_start.date()
        wk = wk - timedelta(days=wk.weekday())
        buckets[wk].append(int(s.average_rating))

    out: list[dict] = []
    for w in weeks:
        vals = buckets.get(w, [])
        out.append(
            {
                "week_start": w.isoformat(),
                "label": _week_label(w),
                "avg_rating": round(sum(vals) / len(vals), 1) if vals else None,
                "samples": len(vals),
            }
        )
    return out


def _rock_status_trend(
    db, weeks: list[date], quarter_id: UUID, department: str | None
) -> list[dict]:
    """For each week, the count of RockWeeklyUpdates by status (this
    quarter's rocks only). Shows execution shape over time."""
    if not weeks:
        return []
    period_start = weeks[0]
    period_end = weeks[-1] + timedelta(days=7)

    # Subquery: rock ids for this quarter (+ optional department).
    rock_q = db.query(Rock.id).filter(
        Rock.is_deleted == False,  # noqa: E712
        Rock.quarter_id == quarter_id,
    )
    if department:
        rock_q = rock_q.filter(Rock.department_name == department)
    rock_ids = [r.id for r in rock_q.all()]
    if not rock_ids:
        return [
            {
                "week_start": w.isoformat(),
                "label": _week_label(w),
                "on_track": 0,
                "off_track": 0,
                "done": 0,
            }
            for w in weeks
        ]

    updates = (
        db.query(RockWeeklyUpdate)
        .filter(
            RockWeeklyUpdate.rock_id.in_(rock_ids),
            RockWeeklyUpdate.is_deleted == False,  # noqa: E712
            RockWeeklyUpdate.update_date >= period_start,
            RockWeeklyUpdate.update_date < period_end,
        )
        .all()
    )

    buckets: dict[date, dict[str, int]] = defaultdict(
        lambda: {"on_track": 0, "off_track": 0, "done": 0}
    )
    for u in updates:
        wk = u.update_date - timedelta(days=u.update_date.weekday())
        # u.update_date is a Date, not DateTime — already a date object.
        status = u.status.value if hasattr(u.status, "value") else str(u.status)
        if status in buckets[wk]:
            buckets[wk][status] += 1

    return [
        {
            "week_start": w.isoformat(),
            "label": _week_label(w),
            **buckets.get(w, {"on_track": 0, "off_track": 0, "done": 0}),
        }
        for w in weeks
    ]


def _issue_trend(db, weeks: list[date], department: str | None) -> list[dict]:
    """Count of issues opened during each week."""
    if not weeks:
        return []
    period_start = _to_datetime(weeks[0])
    period_end = _to_datetime(weeks[-1] + timedelta(days=7))

    q = db.query(Issue).filter(
        Issue.is_deleted == False,  # noqa: E712
        Issue.created_at >= period_start,
        Issue.created_at < period_end,
    )
    if department:
        q = q.filter(Issue.department_name == department)
    issues = q.all()

    buckets: dict[date, int] = defaultdict(int)
    for i in issues:
        wk = i.created_at.date()
        wk = wk - timedelta(days=wk.weekday())
        buckets[wk] += 1

    return [
        {
            "week_start": w.isoformat(),
            "label": _week_label(w),
            "opened": buckets.get(w, 0),
        }
        for w in weeks
    ]


def _issue_solved_trend(db, weeks: list[date], department: str | None) -> list[dict]:
    """Weekly "% of issues solved" — the share of that week's issue *load*
    that got resolved.

    For each week W: ``solved`` = issues whose ``solved_at`` falls inside W;
    ``load`` = issues that were on the board at any point during W (created
    before W ends, and not already solved when W began). ``pct`` = solved /
    load, always 0–100.
    """
    if not weeks:
        return []
    period_end = _to_datetime(weeks[-1] + timedelta(days=7))
    q = db.query(Issue).filter(
        Issue.is_deleted == False,  # noqa: E712
        Issue.created_at < period_end,
    )
    if department:
        q = q.filter(Issue.department_name == department)
    issues = q.all()

    out: list[dict] = []
    for w in weeks:
        w_start = _to_datetime(w)
        w_end = _to_datetime(w + timedelta(days=7))
        solved = 0
        load = 0
        for i in issues:
            created = i.created_at
            if created is None or created >= w_end:
                continue  # not raised yet as of this week
            solved_at = i.solved_at
            # On the board during W = not solved before W began.
            if solved_at is None or solved_at >= w_start:
                load += 1
            if solved_at is not None and w_start <= solved_at < w_end:
                solved += 1
        pct = round(solved / load * 100) if load else None
        out.append(
            {
                "week_start": w.isoformat(),
                "label": _week_label(w),
                "solved": solved,
                "load": load,
                "pct": pct,
            }
        )
    return out


# ── Per-team rollup ─────────────────────────────────────────────────────


def _team_initials(name: str) -> str:
    parts = [p for p in name.replace("-", " ").replace("_", " ").split() if p]
    if not parts:
        return "??"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _team_rows(
    db,
    quarter: Quarter,
    prev_quarter: Quarter | None,
    departments: list[str],
) -> list[dict]:
    """Compute the full row for each department.

    Trend = direction of overall_score vs the previous quarter:
    up (+5 or more), down (-5 or less), flat otherwise. None when prev
    quarter data is missing — frontend renders a dash.
    """
    start = _to_datetime(quarter.start_date)
    end = _to_datetime(quarter.end_date + timedelta(days=1))
    prev_start = _to_datetime(prev_quarter.start_date) if prev_quarter else None
    prev_end = (
        _to_datetime(prev_quarter.end_date + timedelta(days=1))
        if prev_quarter
        else None
    )

    rows: list[dict] = []
    for dept in departments:
        m = _meeting_metrics(db, start, end, dept)
        r = _rock_metrics(db, quarter.id, dept)
        t = _todo_metrics(db, start, end, dept)
        score, _ = _composite_health(m["avg"], r["pct"], t["pct"])
        age = _issue_avg_age_days(db, dept)

        trend: str = "flat"
        if prev_quarter is not None:
            pm = _meeting_metrics(db, prev_start, prev_end, dept)  # type: ignore[arg-type]
            pr = _rock_metrics(db, prev_quarter.id, dept)
            pt = _todo_metrics(db, prev_start, prev_end, dept)  # type: ignore[arg-type]
            prev_score, _ = _composite_health(pm["avg"], pr["pct"], pt["pct"])
            if score - prev_score >= 5:
                trend = "up"
            elif score - prev_score <= -5:
                trend = "down"

        rows.append(
            {
                "team_name": dept,
                "initials": _team_initials(dept),
                # Deterministic 0..7 seed so the frontend can pick a
                # Tailwind palette index without bouncing the colour on
                # every reload.
                "color_seed": (sum(ord(c) for c in dept) % 8),
                "health_score": score,
                "meeting_rating": m["avg"],
                "rock_health_pct": r["pct"],
                "todo_completion_pct": t["pct"],
                "avg_issue_age_days": age,
                "trend": trend,
            }
        )

    # Sort by health_score descending so the most-attention-needed team
    # is NOT at the top (we want execs to see the healthy ones first,
    # then drill into the laggards). Reverse if you want the opposite.
    rows.sort(key=lambda r: r["health_score"], reverse=True)
    return rows


def _list_departments(db, restrict_to: str | None) -> list[str]:
    """Departments that have any EOS activity. When ``restrict_to`` is
    set, return only that one (per-team scoped view)."""
    if restrict_to:
        return [restrict_to]
    seen: set[str] = set()
    # Enumerate department names from L10Meeting and Seat. (Rocks/todos/issues
    # are department-tagged by their own department_name column since
    # Not via Seat — but seated people back every real
    # department, so meetings+seats cover the team list; a dept with only
    # rocks/todos and no meeting or seat would be omitted.)
    for (name,) in (
        db.query(L10Meeting.department_name)
        .filter(
            L10Meeting.department_name.isnot(None),
            L10Meeting.is_deleted == False,  # noqa: E712
        )
        .all()
    ):
        if name:
            seen.add(name)
    # Also include departments that have any Seat (i.e. any person).
    for (name,) in (
        db.query(Seat.department_name)
        .filter(
            Seat.department_name.isnot(None),
            Seat.is_deleted == False,  # noqa: E712
        )
        .all()
    ):
        if name:
            seen.add(name)
    return sorted(seen)


# ── Endpoint ────────────────────────────────────────────────────────────


@router.get("/executive")
def executive_dashboard(
    db: DBDep,
    current_user: CurrentUser,
    quarter_id: UUID | None = None,
    department_name: str | None = None,
):
    """Bloom-style execution insights for the current (or specified)
    quarter, optionally scoped to one department."""
    _require_exec_or_admin(current_user)

    # Advance the stored is_current flag to today's calendar quarter first, so
    # the default period below resolves to the quarter in play (not a stale
    # Q2-flagged-current row lingering into Q3). See sync_current_quarter.
    sync_current_quarter(db)
    target, prev = _resolve_period(db, quarter_id)
    start_dt = _to_datetime(target.start_date)
    end_dt = _to_datetime(target.end_date + timedelta(days=1))

    # ── Headline KPIs ───────────────────────────────────────────────
    meeting = _meeting_metrics(db, start_dt, end_dt, department_name)
    rocks = _rock_metrics(db, target.id, department_name)
    todos = _todo_metrics(db, start_dt, end_dt, department_name)
    score, label = _composite_health(meeting["avg"], rocks["pct"], todos["pct"])

    # Previous-period deltas (None when no prior quarter exists).
    meeting_delta = None
    rocks_delta = None
    todos_delta = None
    if prev is not None:
        prev_start = _to_datetime(prev.start_date)
        prev_end = _to_datetime(prev.end_date + timedelta(days=1))
        pm = _meeting_metrics(db, prev_start, prev_end, department_name)
        pr = _rock_metrics(db, prev.id, department_name)
        pt = _todo_metrics(db, prev_start, prev_end, department_name)
        if meeting["avg"] is not None and pm["avg"] is not None:
            meeting_delta = round(meeting["avg"] - pm["avg"], 1)
        if rocks["pct"] is not None and pr["pct"] is not None:
            rocks_delta = rocks["pct"] - pr["pct"]
        if todos["pct"] is not None and pt["pct"] is not None:
            todos_delta = todos["pct"] - pt["pct"]

    # ── Rock health label ──────────────────────────────────────────
    rock_label = rocks.get("label", "—")

    # ── Per-team rollup ─────────────────────────────────────────────
    departments = _list_departments(db, department_name)
    team_perf = _team_rows(db, target, prev, departments)

    # ── Trends (weekly) ─────────────────────────────────────────────
    weeks = _weeks_in_period(target.start_date, target.end_date)
    trends = {
        "meeting_health_over_time": _meeting_health_trend(db, weeks, department_name),
        "rock_status_over_time": _rock_status_trend(
            db, weeks, target.id, department_name
        ),
        "issue_trend": _issue_trend(db, weeks, department_name),
        "issue_solved_rate": _issue_solved_trend(db, weeks, department_name),
    }

    # ── Available quarters & departments for the filter dropdowns ──
    available_quarters = [
        {
            "id": str(q.id),
            "name": q.name,
            "start_date": q.start_date.isoformat(),
            "end_date": q.end_date.isoformat(),
            "is_current": bool(q.is_current),
        }
        for q in canonical_quarters(
            db.query(Quarter)
            .filter(Quarter.is_deleted == False)  # noqa: E712
            .order_by(Quarter.start_date.desc())
            .all()
        )
    ]
    available_departments = _list_departments(db, None)

    return {
        "scope": {
            "kind": "department" if department_name else "company",
            "department_name": department_name,
        },
        "period": {
            "id": str(target.id),
            "name": target.name,
            "start_date": target.start_date.isoformat(),
            "end_date": target.end_date.isoformat(),
            "is_current": bool(target.is_current),
            "prev_id": str(prev.id) if prev else None,
            "available_quarters": available_quarters,
        },
        "available_departments": available_departments,
        "health_summary": {
            "overall_score": score,
            "overall_label": label,
            "meeting_rating": {
                "avg": meeting["avg"],
                "target": MEETING_RATING_TARGET,
                "delta": meeting_delta,
                "samples": meeting["samples"],
            },
            "rock_health": {
                "pct": rocks["pct"],
                "label": rock_label,
                "delta": rocks_delta,
                "done": rocks["done"],
                "not_done": rocks["not_done"],
                "on_track": rocks["on_track"],
                "off_track": rocks["off_track"],
                "total": rocks["total"],
            },
            "todo_completion": {
                "pct": todos["pct"],
                "target": TODO_COMPLETION_TARGET_PCT,
                "delta": todos_delta,
                "done": todos["done"],
                "total": todos["total"],
            },
        },
        "team_performance": team_perf,
        "trends": trends,
    }
