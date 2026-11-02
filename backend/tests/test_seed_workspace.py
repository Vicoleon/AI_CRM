"""``seed_workspace`` gives a new workspace its defaults, once.

The rows the old migration chain inserted — the default AI Priorities
playbook, the Drive label weights, the SLA placeholders — and the neutral
Smart Teams skills taxonomy now come from one idempotent step (spec §4.2.6).
The baseline migration builds the schema only.

Idempotent means two things here: running it again adds nothing, and it never
overwrites or brings back what an admin has since changed or removed.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from rubik_core.brand import PRODUCT_NAME
from rubik_core.database import seed
from rubik_core.database.commitments import CommitmentSlaConfig
from rubik_core.database.models import Base, DriveLabelPriority, PriorityPlaybook
from rubik_core.database.seed import seed_workspace
from rubik_core.database.smart_teams import SmartTeamsSkill
from rubik_core.smart_teams.skill_taxonomy import SKILLS

SEEDED = (PriorityPlaybook, DriveLabelPriority, CommitmentSlaConfig, SmartTeamsSkill)


@pytest.fixture
def db(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'workspace.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _counts(db) -> dict[str, int]:
    return {model.__tablename__: db.query(model).count() for model in SEEDED}


def test_a_new_workspace_gets_every_default(db):
    seed_workspace(db)

    counts = _counts(db)
    assert all(counts.values()), counts

    default = (
        db.query(PriorityPlaybook).filter(PriorityPlaybook.department.is_(None)).one()
    )
    assert default.is_active

    labels = {row.label_value: row.priority for row in db.query(DriveLabelPriority)}
    assert labels == {"Important": 1.5, PRODUCT_NAME: 1.5, "Project": 1.5}

    slas = db.query(CommitmentSlaConfig).all()
    assert {(row.kind, row.sow_kind) for row in slas} == {
        ("sow", "standard"),
        ("sow", "non_standard"),
        ("wrapup", "any"),
        ("lead_touch", "any"),
    }
    assert all(row.is_placeholder for row in slas)

    assert counts[SmartTeamsSkill.__tablename__] == len(SKILLS)


def test_seeding_twice_leaves_the_row_counts_unchanged(db):
    seed_workspace(db)
    first = _counts(db)

    seed_workspace(db)

    assert _counts(db) == first


def test_a_reseed_keeps_what_an_admin_changed(db):
    seed_workspace(db)
    playbook = (
        db.query(PriorityPlaybook).filter(PriorityPlaybook.department.is_(None)).one()
    )
    playbook.body = "Our own playbook."
    db.get(DriveLabelPriority, ":important").priority = 2.0
    sla = db.query(CommitmentSlaConfig).filter_by(kind="wrapup").one()
    sla.hours = 4
    sla.is_placeholder = False
    db.commit()

    seed_workspace(db)

    db.expire_all()
    assert db.get(PriorityPlaybook, playbook.id).body == "Our own playbook."
    assert db.get(DriveLabelPriority, ":important").priority == 2.0
    kept = db.query(CommitmentSlaConfig).filter_by(kind="wrapup").one()
    assert (kept.hours, kept.is_placeholder) == (4, False)


def test_the_default_playbook_has_a_fixed_id():
    """Derived from a fixed name, so every instance writes the same row."""
    assert seed.DEFAULT_PLAYBOOK_ID == str(
        uuid.uuid5(uuid.NAMESPACE_URL, "urn:cubie:seed:priority-playbook:default")
    )


# ── two instances cold-starting together on a fresh database ─────────────────


@pytest.fixture
def two_instances(tmp_path):
    """Two sessions on one fresh database file: two instances booting at once."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'shared.db'}")
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine)
    first, second = make(), make()
    yield first, second
    first.close()
    second.close()
    engine.dispose()


def _other_instance_seeds_after_my_first_check(mine, theirs) -> None:
    """Interleave the race: ``mine`` runs its first "is the slot empty?" read,
    sees nothing, and only then does ``theirs`` seed and commit everything."""
    fired = []

    @event.listens_for(mine, "do_orm_execute")
    def _race(state):
        if fired or not state.is_select:
            return None
        fired.append(True)
        result = state.invoke_statement().freeze()  # my check: nothing there
        seed_workspace(theirs)  # the other instance wins the race
        return result()


def test_two_instances_seeding_at_once_leave_one_default_playbook(two_instances):
    mine, theirs = two_instances
    _other_instance_seeds_after_my_first_check(mine, theirs)

    seed_workspace(mine)  # must neither duplicate the row nor raise

    defaults = theirs.query(PriorityPlaybook).filter(
        PriorityPlaybook.department.is_(None)
    )
    assert defaults.count() == 1
    assert defaults.one().id == seed.DEFAULT_PLAYBOOK_ID


def test_every_part_is_a_no_op_when_its_rows_already_exist(db, monkeypatch):
    """Each part's "slot is empty" check read empty while another instance was
    seeding: the inserts that follow add nothing and raise nothing."""
    seed_workspace(db)
    first = _counts(db)
    monkeypatch.setattr(seed, "_has_rows", lambda *args, **kwargs: False)

    seed_workspace(db)

    assert _counts(db) == first


def test_a_reseed_does_not_bring_back_a_label_an_admin_removed(db):
    seed_workspace(db)
    db.delete(db.get(DriveLabelPriority, ":project"))
    db.commit()

    seed_workspace(db)

    assert db.get(DriveLabelPriority, ":project") is None


# ── the boot path runs it ────────────────────────────────────────────────────


def test_every_boot_that_migrates_seeds_the_workspace_after_migrating():
    """Local boot seeds after an explicit workspace migration succeeds.

    Production deploy has a separate migration job; a seed failure here does
    not stop the local boot.
    """
    from pathlib import Path

    start = (Path(__file__).resolve().parents[2] / "scripts" / "start.sh").read_text()
    # Guarded by an `if`, so `set -e` cannot end the boot on a failed seed.
    run_seed = "if python -m rubik_core.database.seed; then"
    assert start.count(run_seed) == 1
    migrate = start.index(
        'if alembic -x "workspace=$CUBIE_WORKSPACE" upgrade head; then'
    )
    seed = start.index(run_seed)
    drift = start.index('elif [ "$MIGRATION_PLAN" = "drift" ]; then')
    assert migrate < seed < drift
