"""A new workspace's default rows, written once.

The baseline migration (``alembic/versions/0001_cubie_baseline.py``) builds the
schema and nothing else. The rows the old migration chain inserted, plus the
Smart Teams skills taxonomy, come from here (spec 4.2.6):

* the default AI Priorities playbook: the ``department IS NULL`` row every
  user gets until an admin writes department playbooks;
* the Drive label weights: Important, the product's name and Project, each
  at 1.5 (high);
* the four SLA placeholders, flagged ``is_placeholder`` until an owner
  confirms real numbers;
* the neutral starter skills taxonomy (``skill_taxonomy.seed_taxonomy``).

**Idempotent, and never against an admin.** Each part fills only an empty
slot: no default playbook yet, no Drive labels at all, no SLA rows at all. So a
second run adds nothing, and a row an admin has edited or removed is never
overwritten or brought back. (Only an admin who removes every Drive label, or
the default playbook, gets the defaults back on the next run.) The taxonomy
seeder keeps the same promise for retired skills; see its docstring.

**Safe when two instances boot at once.** Both can find a slot empty before
either commits. Every row this module writes therefore has a fixed primary key
(the default playbook's is a ``uuid5`` of a fixed name), and is written with an
insert that does nothing when that row already exists, so the slower instance
adds nothing and raises nothing. The taxonomy is keyed by its skill keys, so a
lost race there cannot duplicate a skill either: that run fails loudly, the
boot goes on (``scripts/start.sh``), and the next one finds the skills there.

Called by every boot that migrates (``scripts/start.sh`` runs
``python -m rubik_core.database.seed`` right after ``alembic upgrade head``), by
the local dev setup, and by tests that need the defaults.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.commitments import CommitmentSlaConfig
from rubik_core.database.models import DriveLabelPriority, PriorityPlaybook
from rubik_core.smart_teams.skill_taxonomy import seed_taxonomy

logger = logging.getLogger(__name__)

#: ``created_by`` / ``updated_by`` on the rows this module writes.
SEED_ACTOR = "system@seed"

DEFAULT_PLAYBOOK_NAME = "Default — applies to every department"
#: The default playbook's primary key: the same on every instance and in every
#: workspace, so two instances seeding at once write one row, not two.
DEFAULT_PLAYBOOK_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "urn:cubie:seed:priority-playbook:default")
)
DEFAULT_PLAYBOOK_SORT_ORDER = 50
DEFAULT_PLAYBOOK_BODY = """\
# Default playbook — applies to every user

The pipeline already runs five domain-agnostic correlation meta-rules
(action-without-response, commitment-without-fulfillment, future-
obligation-without-prep, deadline-without-calendar-match, high-value-
in-silence). This playbook lets us teach the decision-maker what
counts as **high-value** here, and what bias to apply when several
priorities compete.

## High-value entities

- Anything involving external counterparties over internal asks
- Any signal with an explicit future deadline within 5 business days
- Any signal carrying a dollar amount or a named contract term

## Watch for

- Long-running threads that have gone idle on the user's side
- Calendar events with no preparation evidence
- Promises the user made in past meetings without a follow-up email

## Bias rules

- External > internal
- Deadline-bearing > open-ended
- Decision-stage entities (deals, proposals, renewals) > exploratory ones

(Admins: replace this default with department-specific playbooks via the
**Admin → AI Priorities → Playbooks** panel.)
"""

#: ``(label_key, label_value, priority)``. ``label_key`` NULL matches on the
#: value alone, which is how a file's Drive label usually surfaces.
DRIVE_LABEL_DEFAULTS: tuple[tuple[str | None, str, float], ...] = (
    (None, "Important", 1.5),
    (None, PRODUCT_NAME, 1.5),
    (None, "Project", 1.5),
)

#: ``(id, kind, sow_kind, business hours)``; 8 business hours is one day.
#: Fixed ids, so every workspace holds the same placeholder rows and a later
#: migration can address one by id. ``sow_kind`` is the explicit ``"any"``
#: sentinel for kinds with no SOW variant; see ``CommitmentSlaConfig``.
SLA_PLACEHOLDERS: tuple[tuple[str, str, str, int], ...] = (
    # Standard SOW: request to sent, 3 business days.
    ("5c0a1e7b4d9f4a2b8e6c3d1f70a95b24", "sow", "standard", 24),
    # Non-standard SOW: request to sent, 10 business days.
    ("9f2b6d40c1834e57a9d2f0b83c6e17ad", "sow", "non_standard", 80),
    # Call wrap-up completed, 1 business day.
    ("3a7e5c19b8d24f6081c93e2ab5470fd6", "wrapup", "any", 8),
    # Lead first touch, 2 business days.
    ("c81d94f2a6b74e03859fd7c1620ea3b5", "lead_touch", "any", 16),
)


def drive_label_id(label_key: str | None, label_value: str) -> str:
    """The slug primary key: ``"{label_key}:{label_value}"``, both lower-cased.

    The same rule as the admin endpoints (``Repository._drive_label_id``), so a
    seeded label and one an admin adds by hand cannot both exist.
    """
    return f"{(label_key or '').strip().lower()}:{label_value.strip().lower()}"


def _has_rows(db: Session, model, *criteria) -> bool:
    """Does ``model`` hold a row (matching ``criteria``)? The empty-slot check."""
    return db.query(model).filter(*criteria).first() is not None


def _insert_missing(db: Session, model, rows: list[dict]) -> int:
    """Insert ``rows``, skipping any whose key already exists; the count added.

    ``INSERT … ON CONFLICT DO NOTHING`` on Postgres and SQLite, so a row another
    instance committed between our empty-slot check and this insert is left as
    it is, with no duplicate and no IntegrityError. Any other dialect reads each
    key first, which narrows the race to that read.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        missing = [row for row in rows if db.get(model, row["id"]) is None]
        db.add_all(model(**row) for row in missing)
        db.flush()
        return len(missing)
    result = db.execute(insert(model).values(rows).on_conflict_do_nothing())
    return max(result.rowcount or 0, 0)


def _seed_default_playbook(db: Session, now: datetime) -> bool:
    if _has_rows(db, PriorityPlaybook, PriorityPlaybook.department.is_(None)):
        return False
    row = {
        "id": DEFAULT_PLAYBOOK_ID,
        "name": DEFAULT_PLAYBOOK_NAME,
        "department": None,
        "body": DEFAULT_PLAYBOOK_BODY,
        "is_active": True,
        "sort_order": DEFAULT_PLAYBOOK_SORT_ORDER,
        "created_by": SEED_ACTOR,
        "updated_by": SEED_ACTOR,
        "created_at": now,
        "updated_at": now,
    }
    return _insert_missing(db, PriorityPlaybook, [row]) > 0


def _seed_drive_labels(db: Session, now: datetime) -> bool:
    if _has_rows(db, DriveLabelPriority):
        return False
    rows = [
        {
            "id": drive_label_id(label_key, label_value),
            "label_key": label_key,
            "label_value": label_value,
            "priority": priority,
            "created_at": now,
            "updated_at": now,
        }
        for label_key, label_value, priority in DRIVE_LABEL_DEFAULTS
    ]
    return _insert_missing(db, DriveLabelPriority, rows) > 0


def _seed_sla_placeholders(db: Session, now: datetime) -> bool:
    if _has_rows(db, CommitmentSlaConfig):
        return False
    rows = [
        {
            "id": row_id,
            "kind": kind,
            "sow_kind": sow_kind,
            "hours": hours,
            "is_placeholder": True,
            "created_at": now,
            "updated_at": now,
        }
        for row_id, kind, sow_kind, hours in SLA_PLACEHOLDERS
    ]
    return _insert_missing(db, CommitmentSlaConfig, rows) > 0


def seed_workspace(db: Session) -> None:
    """Write the defaults a new workspace starts with. Safe to run repeatedly."""
    now = datetime.utcnow()
    seeded = {
        "default playbook": _seed_default_playbook(db, now),
        "drive labels": _seed_drive_labels(db, now),
        "sla placeholders": _seed_sla_placeholders(db, now),
    }
    db.commit()
    seed_taxonomy(db, now=now)
    logger.info(
        "[seed] workspace defaults: %s",
        ", ".join(
            f"{part} {'added' if added else 'already there'}"
            for part, added in seeded.items()
        ),
    )


def main() -> None:
    """``python -m rubik_core.database.seed``: seed the configured database."""
    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()
    from rubik_core.database.db import SessionLocal

    db = SessionLocal()
    try:
        seed_workspace(db)
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
