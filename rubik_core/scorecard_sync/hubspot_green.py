"""Weekly HubSpot → scorecard sync: the deal-derived ("green") measurables.

Scope (deliberately narrow): only measurables that are computable from HubSpot
deal data with no pipeline-hygiene decisions and no new data-entry fields.
Measurables that need a stage definition (SOW sent, leads qualified, ratios) or
a lead-source field are not handled here: a measurable the configuration does
not name is simply skipped ("held").

**What it fills is configuration, not code.** Departments and measurable names
differ per workspace, so the connector stores both with empty defaults:

* ``scorecard_sync_department`` — the scorecard team it fills, a department
  name compared by slug (``slugify_department``).
* ``scorecard_sync_metrics`` — which of that team's non-exec measurables it
  fills, and how: a JSON object of ``{"<measurable name>": "<recipe>"}``, the
  name matched exactly. The recipes (:data:`RECIPES`):

      company_won_amount  → won-deal $ sum, every rep    (closedate window)
      company_won_count   → won-deal count, every rep    (closedate window)
      rep_won_amount      → the measurable owner's won-deal $ sum
      rep_won_count       → the measurable owner's won-deal count
      rep_lost_count      → the measurable owner's lost-deal count

With either field or the pipeline allowlist empty the sync does nothing and says why
(:data:`NOT_CONFIGURED`); no HubSpot call is made.

Key correctness facts (the same won/lost primitives as
``rubik_core/retrieval/hubspot_api.py``):

* Won is detected by ``hs_is_closed_won == "true"`` (the ``_WON`` filter), NOT
  ``dealstage == "closedwon"`` — a portal with custom pipeline stages makes the
  string stage match only a handful of deals. Lost is ``hs_is_closed`` &
  ``!hs_is_closed_won`` (``_status_filters("lost")``).
* Deals are attributed to a rep by ``hubspot_owner_id``; a scorecard owner is a
  ``Person`` whose ``email`` is matched (lowercased) against HubSpot's owner
  directory. We NEVER match owners by name (first-name-only owners collide).
* A portal can also host a pipeline the company runs for a client. The HubSpot
  client applies the workspace pipeline filter, and this sync checks returned
  rows again before computing a scorecard value.
* Weeks are Sunday-anchored to line up with the grid, but the anchor Sunday ``S``
  is the Sunday BEFORE the week's Monday and the value covers the Mon–Sun span
  ``[S+1, S+8)`` (the week of Mon Jul 6 – Sun Jul 12 is stored at Jul 5). See
  ``week_anchor_for`` / ``week_window``.

The engine only ever writes into scorecard measurables that ALREADY EXIST; it
never creates them (creating a measurable is a per-owner business decision made
on the Scorecard page). If none of the configured measurables exist it says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from rubik_core.database.eos import Person, ScorecardEntry, ScorecardMetric
from rubik_core.rag.config import slugify_department

# These are the canonical, live-validated HubSpot deal primitives. We import the
# module-private helpers on purpose: won/lost detection is domain logic that must
# stay identical to the rest of the app (a copy would silently drift).
from rubik_core.retrieval.hubspot_api import (
    _WON,
    HubSpotAPIClient,
    HubSpotAPIError,
    _date_range_filters,
    _num,
    _owner_filter,
    _status_filters,
    resolve_window,
)
from rubik_core.tenancy.hubspot_config import (
    HubSpotConnectorConfig,
    get_hubspot_config,
)

logger = logging.getLogger(__name__)

#: created_by / updated_by stamp for rows this job writes (no human actor).
ACTOR = "system:hubspot-sales-sync"

#: Deal-derived recipes a measurable can be filled with, by the id
#: ``scorecard_sync_metrics`` recipe names.
#:   kind  — how to compute it from HubSpot deals
#:   scope — "company" (all reps, owner filter off) or "rep" (the measurable's
#:           owner). (``_compute`` also knows ``created_count``: deals created in
#:           the week, which rarely matches a team's own "new lead" definition,
#:           so no recipe offers it yet.)
RECIPES: dict[str, dict[str, str]] = {
    "company_won_amount": {"kind": "won_amount", "scope": "company"},
    "company_won_count": {"kind": "won_count", "scope": "company"},
    "rep_won_amount": {"kind": "won_amount", "scope": "rep"},
    "rep_won_count": {"kind": "won_count", "scope": "rep"},
    "rep_lost_count": {"kind": "lost_count", "scope": "rep"},
}

#: What the report and the log say when the sync has nothing to fill.
NOT_CONFIGURED = (
    "Scorecard sync is not set up: configure this workspace's HubSpot deal "
    "pipelines, scorecard department, and metric recipes. Nothing was read or written."
)


def configured_recipes(
    config: HubSpotConnectorConfig | None = None,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Workspace scorecard recipes resolved: ``(name → recipe, problems)``.

    An entry naming a recipe :data:`RECIPES` does not have is left out and
    reported, never guessed at.
    """
    raw = (config or get_hubspot_config()).scorecard_sync_metrics
    recipes: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for name, recipe_id in raw.items():
        metric = str(name or "").strip()
        recipe = RECIPES.get(str(recipe_id or "").strip())
        if not metric:
            continue
        if recipe is None:
            problems.append(
                f"scorecard_sync_metrics: {metric!r} names unknown recipe "
                f"{recipe_id!r} (one of {', '.join(sorted(RECIPES))})"
            )
            continue
        recipes[metric] = recipe
    return recipes, problems


# ── result types ──────────────────────────────────────────────────────────────


@dataclass
class SyncRow:
    """One (metric × week) outcome — the unit the dry-run report prints."""

    metric_name: str
    owner_label: str
    week_start: date
    kind: str
    computed_value: str | None
    existing_value: str | None
    action: str  # inserted | updated | unchanged | skipped | error
    detail: str = ""


@dataclass
class SyncReport:
    committed: bool
    rows: list[SyncRow] = field(default_factory=list)
    green_metrics_seen: int = 0
    held_metric_names: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Why nothing ran (:data:`NOT_CONFIGURED`), or "" when it did.
    note: str = ""

    def _count(self, action: str) -> int:
        return sum(1 for r in self.rows if r.action == action)

    @property
    def written(self) -> int:
        return self._count("inserted") + self._count("updated")

    def summary(self) -> str:
        return (
            f"committed={self.committed} green_metrics={self.green_metrics_seen} "
            f"held={len(self.held_metric_names)} inserted={self._count('inserted')} "
            f"updated={self._count('updated')} unchanged={self._count('unchanged')} "
            f"skipped={self._count('skipped')} error={self._count('error')} "
            f"errors={self.errors}" + (f" note={self.note}" if self.note else "")
        )


# ── helpers ─────────────────────────────────────────────────────────────────


def _fmt_number(x: float) -> str:
    """Render a number as the scorecard's string ``value``.

    Whole numbers drop the decimal ("8"); money keeps up to 2 places with
    trailing zeros trimmed ("66012.5", "3510"). Matches the seed's convention;
    the grid parser strips ``,``/``%`` so a plain numeric string renders and
    averages cleanly.
    """
    if x == int(x):
        return str(int(x))
    return f"{x:.2f}".rstrip("0").rstrip(".")


def week_anchor_for(d: date) -> date:
    """The seed's Sunday anchor for the Mon–Sun week containing ``d``.

    VERIFIED against the seeded data + the source workbook: the workbook's
    "July 6th – July 12th" (Mon–Sun) column is stored at ``2026-07-05``. So each
    Mon–Sun week is anchored to the Sunday IMMEDIATELY BEFORE its Monday, and its
    value covers the Mon–Sun span (see ``week_window`` below). We reproduce that
    exactly so HubSpot-fed values land in the same cell as the hand-entered ones.

    (This is NOT ``_current_week_start`` in ``backend/routers/eos/me.py``: that
    helper anchors a Sun–Sat week to the Sunday that *starts* it. The two agree
    for Mon–Sat but differ on a Sunday ``d``.)
    """
    monday = d - timedelta(days=d.weekday())  # Python weekday(): Mon=0..Sun=6
    return monday - timedelta(days=1)


def week_window(anchor: date) -> tuple[str, str]:
    """(start, end) ISO dates for the Mon–Sun span an ``anchor`` Sunday covers.

    ``anchor`` (a Sunday) → Monday ``anchor+1`` through Sunday ``anchor+7``.
    ``resolve_window`` makes ``end`` exclusive (+1 day), giving ``[Mon, next Mon)``.
    """
    monday = anchor + timedelta(days=1)
    sunday_end = anchor + timedelta(days=7)
    return monday.isoformat(), sunday_end.isoformat()


def _target_anchors(now: date, weeks: int, week_anchor: date | None) -> list[date]:
    """The Sunday anchors to (re)compute, newest first.

    ``week_anchor`` pins a single week (by any date in it). Otherwise the
    ``weeks`` most recent Mon–Sun weeks including the current in-progress one.
    """
    if week_anchor is not None:
        return [week_anchor_for(week_anchor)]
    current = week_anchor_for(now)
    return [current - timedelta(days=7 * i) for i in range(max(1, weeks))]


def _pipeline_filters(allowed: tuple[str, ...]) -> list[dict]:
    """``pipeline IN [the workspace's pipelines]`` keeps client deals out."""
    if not allowed:
        raise ValueError("No HubSpot deal pipelines configured for this workspace")
    return [{"propertyName": "pipeline", "operator": "IN", "values": list(allowed)}]


# ── per-kind HubSpot computation ──────────────────────────────────────────────


def _compute(
    client: HubSpotAPIClient,
    kind: str,
    owner_id: str | None,
    start_ms: int,
    end_ms: int,
    pipeline_ids: tuple[str, ...],
) -> str:
    """Compute one green metric's weekly value as a scorecard string.

    ``owner_id`` is a HubSpot owner id for rep-scoped metrics, ``None`` for
    company-wide. Window is the half-open ``[start_ms, end_ms)`` epoch-ms range.
    """
    pipeline = _pipeline_filters(pipeline_ids)
    owner = _owner_filter(owner_id)

    if kind == "won_count":
        n = client.count(
            "deals",
            [_WON]
            + pipeline
            + owner
            + _date_range_filters("closedate", start_ms, end_ms),
        )
        return _fmt_number(float(n))

    if kind == "won_amount":
        deals = client.search_all(
            "deals",
            [_WON]
            + pipeline
            + owner
            + _date_range_filters("closedate", start_ms, end_ms),
            ["amount", "pipeline"],
        )
        properties = [deal.get("properties") or {} for deal in deals]
        if any(
            str(props.get("pipeline") or "") not in pipeline_ids for props in properties
        ):
            raise ValueError("HubSpot returned a deal outside configured pipelines")
        total = sum((_num(props, "amount") or 0.0) for props in properties)
        return _fmt_number(total)

    if kind == "lost_count":
        n = client.count(
            "deals",
            _status_filters("lost")
            + pipeline
            + owner
            + _date_range_filters("closedate", start_ms, end_ms),
        )
        return _fmt_number(float(n))

    if kind == "created_count":
        n = client.count(
            "deals",
            pipeline + owner + _date_range_filters("createdate", start_ms, end_ms),
        )
        return _fmt_number(float(n))

    raise ValueError(f"unknown green-metric kind: {kind}")


# ── entry upsert (idempotent, direct ORM) ─────────────────────────────────────


def _find_entry(db: Session, metric_id, week_start: date) -> ScorecardEntry | None:
    """The existing non-deleted entry for this metric on this calendar day.

    Mirrors ``upsert_entry`` in ``backend/routers/eos/scorecard.py`` — matches by
    metric + the DAY of ``week_start_date`` (tolerant of time-of-day).
    """
    day_start = datetime(week_start.year, week_start.month, week_start.day)
    day_end = day_start + timedelta(days=1)
    return (
        db.query(ScorecardEntry)
        .filter(
            ScorecardEntry.metric_id == metric_id,
            ScorecardEntry.week_start_date >= day_start,
            ScorecardEntry.week_start_date < day_end,
            ScorecardEntry.is_deleted == False,  # noqa: E712
        )
        .first()
    )


# ── engine ────────────────────────────────────────────────────────────────────


def sync_sales_green_metrics(
    db: Session,
    *,
    weeks: int = 1,
    week_anchor: date | None = None,
    commit: bool = False,
    now: datetime | None = None,
    client: HubSpotAPIClient | None = None,
) -> SyncReport:
    """Fill the configured scorecard measurables from HubSpot for the week(s).

    Dry-run by default (``commit=False``): computes every value and the action it
    WOULD take, writes nothing. ``commit=True`` upserts idempotently.

    ``weeks`` — number of trailing weeks (incl. the current one) to compute when
    ``week_anchor`` is not given. ``week_anchor`` pins a single week (by any date
    in it). ``client`` is injectable for tests.

    With scorecard department, metric recipes, or pipelines unset it
    returns at once with ``report.note`` saying so, before any HubSpot call.
    """
    report = SyncReport(committed=commit)

    config = get_hubspot_config()
    department_slug = slugify_department(config.scorecard_sync_department)
    recipes, problems = configured_recipes(config)
    report.errors.extend(problems)
    if not config.pipeline_ids or not department_slug or not recipes:
        report.note = NOT_CONFIGURED
        logger.info("[scorecard-sync] %s", NOT_CONFIGURED)
        return report

    now_dt = now or datetime.now()
    anchors = _target_anchors(now_dt.date(), weeks, week_anchor)

    if client is None:
        try:
            client = HubSpotAPIClient()
        except HubSpotAPIError as e:
            report.errors.append(f"HubSpot client unavailable: {e}")
            logger.warning("[scorecard-sync] client unavailable: %s", e)
            return report

    # Owner directory: email(lowercased) → HubSpot owner id. Best-effort; empty
    # means we can't attribute rep metrics this run (treat as unknown, not zero).
    owner_dir = client.owner_directory()
    email_to_id = {d["email"]: oid for oid, d in owner_dir.items() if d.get("email")}
    directory_ok = bool(email_to_id)
    if not directory_ok:
        report.errors.append("HubSpot owner directory empty/unavailable")

    # The team's non-exec measurables, its department matched by slug.
    metrics = [
        m
        for m in db.query(ScorecardMetric)
        .filter(
            ScorecardMetric.is_exec == False,  # noqa: E712
            ScorecardMetric.is_deleted == False,  # noqa: E712
        )
        .all()
        if slugify_department(m.department_name or "") == department_slug
    ]

    green_metrics = []
    held: list[str] = []
    for m in metrics:
        recipe = recipes.get((m.name or "").strip())
        if recipe is None:
            held.append(m.name)
        else:
            green_metrics.append((m, recipe))
    report.green_metrics_seen = len(green_metrics)
    report.held_metric_names = sorted(set(held))

    if not green_metrics:
        msg = (
            f"no measurable named in scorecard_sync_metrics ({', '.join(sorted(recipes))}) "
            f"is on the {config.scorecard_sync_department!r} scorecard "
            "(non-exec). Create them on the Scorecard page, or fix the names; "
            "nothing was written."
        )
        report.errors.append(msg)
        logger.warning("[scorecard-sync] %s", msg)
        return report

    # Resolve owners once (owner_id → Person) for rep-scoped metrics.
    owner_ids = {m.owner_id for m, _ in green_metrics}
    people = {p.id: p for p in db.query(Person).filter(Person.id.in_(owner_ids)).all()}

    for m, recipe in green_metrics:
        kind = recipe["kind"]
        scope = recipe["scope"]

        # Resolve the HubSpot owner id for this metric (once per metric).
        hs_owner_id: str | None = None
        owner_label = "Company-wide"
        skip_reason: str | None = None
        if scope == "rep":
            person = people.get(m.owner_id)
            if person is None:
                skip_reason = "owner Person not found"
                owner_label = str(m.owner_id)
            else:
                owner_label = f"{person.first_name} {person.last_name} <{person.email}>"
                if not directory_ok:
                    skip_reason = "HubSpot owner directory unavailable"
                else:
                    hs_owner_id = email_to_id.get((person.email or "").lower())
                    if not hs_owner_id:
                        skip_reason = f"no HubSpot owner for {person.email}"

        for anchor in anchors:
            start_str, end_str = week_window(anchor)
            start_ms, end_ms, _label = resolve_window(None, start_str, end_str)
            existing = _find_entry(db, m.id, anchor)
            existing_value = existing.value if existing else None

            if skip_reason is not None:
                report.rows.append(
                    SyncRow(
                        metric_name=m.name,
                        owner_label=owner_label,
                        week_start=anchor,
                        kind=kind,
                        computed_value=None,
                        existing_value=existing_value,
                        action="skipped",
                        detail=skip_reason,
                    )
                )
                continue

            try:
                value = _compute(
                    client, kind, hs_owner_id, start_ms, end_ms, config.pipeline_ids
                )
            except (HubSpotAPIError, ValueError) as e:
                report.errors.append(f"{m.name} [{anchor}]: {e}")
                report.rows.append(
                    SyncRow(
                        metric_name=m.name,
                        owner_label=owner_label,
                        week_start=anchor,
                        kind=kind,
                        computed_value=None,
                        existing_value=existing_value,
                        action="error",
                        detail=str(e),
                    )
                )
                continue

            # Decide the action (and apply it only when committing).
            if existing is not None:
                if existing.value == value:
                    action = "unchanged"
                else:
                    action = "updated"
                    if commit:
                        existing.value = value
                        existing.updated_by = ACTOR
            else:
                action = "inserted"
                if commit:
                    db.add(
                        ScorecardEntry(
                            metric_id=m.id,
                            week_start_date=datetime(
                                anchor.year, anchor.month, anchor.day
                            ),
                            value=value,
                            created_by=ACTOR,
                        )
                    )

            report.rows.append(
                SyncRow(
                    metric_name=m.name,
                    owner_label=owner_label,
                    week_start=anchor,
                    kind=kind,
                    computed_value=value,
                    existing_value=existing_value,
                    action=action,
                )
            )

    if commit:
        db.commit()

    logger.info("[scorecard-sync] %s", report.summary())
    return report
