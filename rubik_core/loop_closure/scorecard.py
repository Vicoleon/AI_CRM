"""The weekly "Loose ends over 7 days" scorecard number, per AE.

Every weekday morning the scan writes, into each AE's own Sales scorecard
workspace-configured measurable, the number of their loose
ends that have been open ``LOOP_CLOSURE_ESCALATION_DAYS`` or more. It is a
snapshot: Monday's run writes the week's cell, Friday's run overwrites it
with Friday's number, and the L10 reads the week's last word.

Conventions borrowed from ``rubik_core/scorecard_sync/hubspot_green.py``, on
purpose, so the two writers into the Sales scorecard agree:

* **Sunday anchor.** A cell is stored at the Sunday BEFORE its Mon–Sun week
  (``week_anchor_for``), or the grid renders every cell as "—". "Today" is the
  workspace's business-timezone day, never UTC.
* **Exact-name match, owner by email.** A metric is the AE's when its name is
  exactly the setting and its ``eos_people`` owner's email is the AE's.
* **Upsert, idempotent.** The same value twice is ``unchanged``.
* **It never creates metrics.** Creating a measurable is a decision a Sales
  leader makes once, from the Team tab (:func:`setup_metrics`); the morning
  job only fills rows that exist and says how many it found.

No AI and no HubSpot: the number is a count of rows in ``loop_items``.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.eos import Person, ScorecardEntry, ScorecardMetric
from rubik_core.database.loop_closure import LoopItem
from rubik_core.loop_closure import constants

logger = logging.getLogger(__name__)


class ScorecardMetricUnconfigured(RuntimeError):
    """This workspace has not chosen its Loose Ends measurable name."""


#: created_by / updated_by on rows this module writes (no human actor).
ACTOR = "system:loose-ends"
SALES_SLUG = "sales"

METRIC_DESCRIPTION = (
    f"Filled in by {PRODUCT_NAME} every weekday morning from Loose Ends: how many "
    "of this person's loose ends (client emails not in HubSpot, client emails waiting "
    "on a reply, promises and HubSpot tasks past their date) have been open "
    "7 days or more. Nobody types it; the goal is zero."
)


def _today(now: datetime | None) -> date:
    from rubik_core.cockpit.business_hours import _business_tz

    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(_business_tz()).date()


def _naive(now: datetime | None) -> datetime:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        return stamp
    return stamp.astimezone(timezone.utc).replace(tzinfo=None)


def week_anchor(now: datetime | None = None) -> date:
    """The Sunday cell this week's number belongs in."""
    from rubik_core.scorecard_sync.hubspot_green import week_anchor_for

    return week_anchor_for(_today(now))


def _is_sales(department: str | None) -> bool:
    from rubik_core.rag.config import slugify_department

    return slugify_department(department or "") == SALES_SLUG


def sales_metrics(db: Session, metric_name: str | None = None) -> list[ScorecardMetric]:
    """Every live Sales (non-exec) measurable with exactly this name."""
    name = (metric_name or constants.thresholds()["scorecard_metric"]).strip()
    if not name:
        return []
    rows = (
        db.query(ScorecardMetric)
        .filter(
            ScorecardMetric.name == name,
            ScorecardMetric.is_exec == False,  # noqa: E712
            ScorecardMetric.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    return [m for m in rows if _is_sales(m.department_name)]


def metric_owners(
    db: Session, emails: list[str], metric_name: str | None = None
) -> dict[str, ScorecardMetric]:
    """``{ae_email: metric}`` for the AEs in ``emails`` who have the row."""
    metrics = sales_metrics(db, metric_name)
    if not metrics:
        return {}
    owner_ids = {m.owner_id for m in metrics}
    people = {
        p.id: (p.email or "").strip().lower()
        for p in db.query(Person).filter(Person.id.in_(owner_ids)).all()
    }
    wanted = {(e or "").strip().lower() for e in emails}
    out: dict[str, ScorecardMetric] = {}
    for m in metrics:
        email = people.get(m.owner_id)
        if email and email in wanted and email not in out:
            out[email] = m
    return out


def _seated(db: Session, emails: list[str]) -> set[str]:
    """Roster emails that have a live Accountability Chart (``eos_people``) row
    — the only people a measurable can be owned by."""
    wanted = sorted({(e or "").strip().lower() for e in emails} - {""})
    if not wanted:
        return set()
    return {
        (p.email or "").strip().lower()
        for p in db.query(Person)
        .filter(func.lower(Person.email).in_(wanted), Person.is_deleted == False)  # noqa: E712
        .all()
    }


def status(db: Session, emails: list[str]) -> dict:
    """The Team tab's scorecard line: found, missing, and whether to offer setup.

    ``people_without_seat`` are the missing ones setup CANNOT help (no seat on
    the Accountability Chart, so nothing can own their row); ``can_setup`` is
    true only when someone who has a seat still lacks the row, so the button is
    not offered forever for people it skips every time.
    """
    name = constants.thresholds()["scorecard_metric"]
    roster = {e.strip().lower() for e in emails if e and e.strip()}
    found = metric_owners(db, list(roster), name)
    missing = roster - set(found)
    seated = _seated(db, sorted(missing))
    return {
        "metric_name": name,
        "metrics_found": len(found),
        "people_without_metric": len(missing),
        "people_without_seat": sorted(missing - seated),
        "can_setup": bool(name and missing & seated),
    }


def over_escalation_counts(
    db: Session, emails: list[str], *, now: datetime | None = None
) -> dict[str, int]:
    """Open items at or over the escalation age, per AE (0 for none).

    Ages and the listing rule come from ``views``, so this number is the one
    the AE's page and the Team tab show for "over 7 days"."""
    from rubik_core.loop_closure.views import days_open, listed

    limits = constants.thresholds()
    esc = limits["escalation_days"]
    cutoff = _naive(now)
    out = {e: 0 for e in emails}
    if not emails:
        return out
    rows = (
        db.query(LoopItem)
        .filter(LoopItem.owner_email.in_(emails), LoopItem.status == "open")
        .all()
    )
    for row in rows:
        if listed(row, cutoff, limits) and days_open(row, cutoff) >= esc:
            out[row.owner_email] = out.get(row.owner_email, 0) + 1
    return out


def write_weekly(
    db: Session,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    emails: list[str] | None = None,
    unchecked: set[str] | None = None,
) -> dict:
    """Upsert this week's cell for every rostered AE who has the row.

    Owns its transaction (commit, or roll back on ``dry_run``). Returns named
    buckets; a metric whose owner is not on the roster is ``skipped`` — the
    scan did not read their mail, so a zero would be a claim nobody checked.
    The same goes for a rostered AE in ``unchecked`` (their scan failed or their
    mailbox could not be read, from ``views.unchecked_emails``): their cell is
    left as it was and counted under ``skipped_not_checked``.
    """
    from rubik_core.loop_closure.roster import roster_emails
    from rubik_core.scorecard_sync.hubspot_green import _find_entry, _fmt_number

    name = constants.thresholds()["scorecard_metric"]
    anchor = week_anchor(now)
    roster = sorted(
        {
            (e or "").strip().lower()
            for e in (emails if emails is not None else roster_emails(db))
        }
        - {""}
    )
    metrics = sales_metrics(db, name)
    report: dict[str, Any] = {
        "metric_name": name,
        "week_start": anchor.isoformat(),
        "metrics_found": len(metrics),
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_not_on_roster": 0,
        "skipped_not_checked": 0,
        "dry_run": bool(dry_run),
    }
    if not metrics:
        logger.info(
            "[loop-closure] no Sales scorecard rows named %r yet — nothing written",
            name,
        )
        return report

    owners = metric_owners(db, roster, name)
    report["skipped_not_on_roster"] = len(metrics) - len(owners)
    skip = {(e or "").strip().lower() for e in (unchecked or ())}
    if skip:
        report["skipped_not_checked"] = sum(1 for e in owners if e in skip)
        owners = {e: m for e, m in owners.items() if e not in skip}
    counts = over_escalation_counts(db, list(owners), now=now)
    week_dt = datetime(anchor.year, anchor.month, anchor.day)
    for email, metric in owners.items():
        value = _fmt_number(float(counts.get(email, 0)))
        entry = _find_entry(db, metric.id, anchor)
        if entry is None:
            db.add(
                ScorecardEntry(
                    metric_id=metric.id,
                    week_start_date=week_dt,
                    value=value,
                    created_by=ACTOR,
                )
            )
            report["inserted"] += 1
        elif entry.value == value:
            report["unchanged"] += 1
        else:
            entry.value = value
            entry.updated_by = ACTOR
            report["updated"] += 1
    if dry_run:
        db.rollback()
    else:
        db.commit()
    return report


def _sales_department(db: Session) -> str | None:
    """The department name the Sales scorecard is filed under.

    The name the existing Sales measurables already use wins (department rows
    are free text and duplicated — "Sales" and "sales" both exist in dev), then
    a department row that slugifies to sales, preferring the exact "Sales".
    """
    from rubik_core.database.models import Department

    existing = [
        m.department_name
        for m in db.query(ScorecardMetric)
        .filter(ScorecardMetric.is_deleted == False)  # noqa: E712
        .all()
        if _is_sales(m.department_name)
    ]
    if existing:
        return Counter(existing).most_common(1)[0][0]
    names = [d.name for d in db.query(Department).all() if _is_sales(d.name)]
    if not names:
        return None
    return "Sales" if "Sales" in names else sorted(names)[0]


def setup_metrics(db: Session, *, actor: str, emails: list[str] | None = None) -> dict:
    """Create the missing per-AE measurables, once, from the Team tab.

    Owner = the AE's ``eos_people`` row (an AE with none is listed back in
    ``skipped_no_person`` rather than guessed at); ``number``, ``<= 0``,
    weekly, with a description saying Cubie fills it. Safe to press twice:
    existing rows count under ``already``. Commits.
    """
    from rubik_core.loop_closure.roster import roster_emails

    name = constants.thresholds()["scorecard_metric"]
    if not name:
        raise ScorecardMetricUnconfigured(
            "Set this workspace's Loose Ends Scorecard metric before setup"
        )
    roster = sorted(
        {
            (e or "").strip().lower()
            for e in (emails if emails is not None else roster_emails(db))
        }
        - {""}
    )
    have = metric_owners(db, roster, name)
    department = _sales_department(db)
    people = (
        {
            (p.email or "").strip().lower(): p
            for p in db.query(Person)
            .filter(func.lower(Person.email).in_(roster), Person.is_deleted == False)  # noqa: E712
            .all()
        }
        if roster
        else {}
    )
    created = 0
    skipped: list[str] = []
    who = (actor or "").strip().lower() or ACTOR
    for email in roster:
        if email in have:
            continue
        person = people.get(email)
        if person is None:
            skipped.append(email)
            continue
        db.add(
            ScorecardMetric(
                owner_id=person.id,
                department_name=department,
                is_exec=False,
                name=name,
                description=METRIC_DESCRIPTION,
                target_value="0",
                value_type="number",
                comparator="<=",
                frequency="weekly",
                created_by=who,
            )
        )
        created += 1
    db.commit()
    return {"created": created, "skipped_no_person": skipped, "already": len(have)}
