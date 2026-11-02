"""An unusable goal repairs itself from its own authored label.

The Scorecard colours a weekly cell by comparing it against ``comparator`` +
``target_value``, but the Goal column shows ``goal_description``. So a
measurable can *show* a goal and colour nothing.

That is how the live Exec scorecard arrived entirely grey, and the cause was
sharper than a missing target: ``target_value`` held the goal LABEL —
``comparator='<'`` beside ``target_value='<9.25%'`` where ``0.0925`` belongs.
Present, so every emptiness check passed; uncomparable, so nothing coloured and
nothing warned. When the label already says what the target is, we read it back
out.

Three units under test:
  - ``parse_goal_text`` — the parse, including what it refuses.
  - ``is_usable_target`` — that present-but-uncomparable counts as missing.
  - the metrics/upload endpoints — that the repair is actually wired in and
    never overwrites a goal somebody set deliberately.

Temp SQLite per test; no mocks.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.eos.scorecard_goals import (
    derive_goal_fields,
    is_usable_target,
    parse_goal_text,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_scorecard_goal_derivation.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


def _profile(email, *, roles=None):
    now = "2026-08-25T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["admin"],
        department="general",
        is_exec=True,
        managed_departments=["*"],
        created_at=now,
        updated_at=now,
    )


def _make_owner(email="marin@x.example"):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person

    db = SessionLocal()
    try:
        p = Person(first_name="Marin", last_name="Vale", email=email, created_by="t")
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id
    finally:
        db.close()


def _as_admin():
    app.dependency_overrides[get_current_user] = lambda: _profile("admin@x.example")


# ---- the parse ------------------------------------------------------------


# Every goal label on the real Exec scorecard, plus the shapes people type.
@pytest.mark.parametrize(
    ("label", "value_type", "comparator", "target"),
    [
        ("> 33,000 usd", "currency", ">", "33000"),
        (">750,000 usd", "currency", ">", "750000"),
        ("<10,000 usd", "currency", "<", "10000"),
        ("> 1 unit", "number", ">", "1"),
        (">75 units", "number", ">", "75"),
        (">5 hours", "number", ">", "5"),
        ("> 98%", "percent", ">", "0.98"),
        ("< 2%", "percent", "<", "0.02"),
        ("<9.25%", "percent", "<", "0.0925"),
        (">75%", "percent", ">", "0.75"),
        # The symbols the app itself renders, and the transposed form.
        ("≥ 3", "number", ">=", "3"),
        ("≤ 3", "number", "<=", "3"),
        ("=> 4", "number", ">=", "4"),
        ("=< 4", "number", "<=", "4"),
    ],
)
def test_parse_goal_text_reads_real_labels(label, value_type, comparator, target):
    parsed = parse_goal_text(label, value_type)
    assert parsed is not None, label
    assert parsed.comparator == comparator
    assert parsed.target_value == target


def test_percent_target_is_exact_not_a_float_artifact():
    # A plain 9.25 / 100 gives 0.09250000000000001, which we would then STORE
    # as the canonical target — and a typed 9.25 would no longer match it.
    assert parse_goal_text("<9.25%", "percent").target_value == "0.0925"


@pytest.mark.parametrize(
    ("label", "value_type"),
    [
        # A percent goal with no "%" can't be told apart from an
        # already-fractional target — guessing is a silent 100× error.
        ("< 0.02", "percent"),
        ("< 2", "percent"),
        # A "%" on a measurable that isn't a percent contradicts its own type.
        ("> 50%", "number"),
        ("> 50%", "currency"),
        # Not goals.
        ("TBD", "number"),
        ("improve steadily", "number"),
        ("> 3 to 5", "number"),
        ("", "number"),
        (None, "number"),
    ],
)
def test_parse_goal_text_refuses_the_ambiguous(label, value_type):
    assert parse_goal_text(label, value_type) is None


# ---- what derivation will and won't touch --------------------------------


def test_fills_a_missing_target_beside_an_existing_comparator():
    # The exact live defect: comparator set, target NULL, label carries it.
    assert derive_goal_fields(
        goal_description="<9.25%",
        comparator="<",
        target_value=None,
        value_type="percent",
    ) == {"target_value": "0.0925"}


def test_fills_both_halves_when_neither_is_set():
    assert derive_goal_fields(
        goal_description="> 98%",
        comparator=None,
        target_value="",
        value_type="percent",
    ) == {"comparator": ">", "target_value": "0.98"}


def test_never_overwrites_a_goal_somebody_set():
    assert (
        derive_goal_fields(
            goal_description="> 98%",
            comparator=">",
            target_value="0.5",
            value_type="percent",
        )
        == {}
    )


def test_refuses_a_label_pointing_the_other_way():
    # Pairing 75 with the stored "<" would colour the whole row backwards —
    # worse than colouring nothing. Leave it for the UI to flag.
    assert (
        derive_goal_fields(
            goal_description="> 75",
            comparator="<",
            target_value=None,
            value_type="number",
        )
        == {}
    )


def test_a_bare_number_with_no_direction_anywhere_is_not_a_goal():
    assert (
        derive_goal_fields(
            goal_description="75 units",
            comparator=None,
            target_value=None,
            value_type="number",
        )
        == {}
    )
    # ...but it IS a target once a direction exists.
    assert derive_goal_fields(
        goal_description="75 units",
        comparator=">",
        target_value=None,
        value_type="number",
    ) == {"target_value": "75"}


# ---- the state the live Exec scorecard was actually in --------------------


@pytest.mark.parametrize(
    "stored",
    [
        # What 18 of the 20 live Exec measurables held: the goal LABEL sitting
        # where the number belongs. Present, so every emptiness check passes;
        # uncomparable, so nothing is ever coloured.
        "<9.25%",
        "> 98%",
        ">750,000 usd",
        "> 1 unit",
        ">75 units",
        ">5 hours",
        "< 2%",
        ">/= 50% within 90 Days",
        "TBD",
        "",
        None,
    ],
)
def test_unusable_targets_count_as_missing(stored):
    assert is_usable_target(stored) is False


@pytest.mark.parametrize("stored", ["0.98", "33000", "0.0925", "5", "-500", "$33,000"])
def test_canonical_targets_are_usable(stored):
    assert is_usable_target(stored) is True


def test_corrects_a_target_holding_the_goal_label():
    # The exact live row: Ad Hoc Capacity, <9.25%, percent.
    assert derive_goal_fields(
        goal_description="<9.25%",
        comparator="<",
        target_value="<9.25%",
        value_type="percent",
    ) == {"target_value": "0.0925"}


def test_leaves_a_contradictory_row_for_a_human():
    # The live Hot Fix Tickets row: label says "> 5", comparator says "<".
    # Nobody but Blair knows which is meant, and guessing colours it backwards.
    assert (
        derive_goal_fields(
            goal_description=">5 hours",
            comparator="<",
            target_value=">5 hours",
            value_type="number",
        )
        == {}
    )


def test_patch_corrects_a_target_holding_the_label():
    owner_id = _make_owner()
    _as_admin()
    mid = client.post(
        "/api/eos/scorecard/metrics",
        json={
            "owner_id": str(owner_id),
            "name": "Client Retention",
            "goal_description": "> 98%",
            "value_type": "percent",
            "comparator": ">",
        },
    ).json()["id"]

    r = client.patch(
        f"/api/eos/scorecard/metrics/{mid}", json={"target_value": "> 98%"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["target_value"] == "0.98"


# ---- wired into the API --------------------------------------------------


def test_create_completes_a_half_set_goal():
    owner_id = _make_owner()
    _as_admin()
    r = client.post(
        "/api/eos/scorecard/metrics",
        json={
            "owner_id": str(owner_id),
            "name": "Ad Hoc Capacity",
            "goal_description": "<9.25%",
            "value_type": "percent",
            "comparator": "<",
            "is_exec": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["target_value"] == "0.0925"


def test_create_leaves_an_explicit_target_alone():
    owner_id = _make_owner()
    _as_admin()
    r = client.post(
        "/api/eos/scorecard/metrics",
        json={
            "owner_id": str(owner_id),
            "name": "Utilization Rate",
            "goal_description": ">75%",
            "target_value": "0.8",
            "value_type": "percent",
            "comparator": ">",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["target_value"] == "0.8"


def test_patching_the_label_completes_the_goal():
    owner_id = _make_owner()
    _as_admin()
    mid = client.post(
        "/api/eos/scorecard/metrics",
        json={"owner_id": str(owner_id), "name": "eNPS (Acme)"},
    ).json()["id"]

    # Author the goal the way the Exec scorecard has it: prose + direction, no
    # number in the target field.
    r = client.patch(
        f"/api/eos/scorecard/metrics/{mid}",
        json={"goal_description": ">75 units", "comparator": ">"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["target_value"] == "75"


def test_patch_does_not_resurrect_a_target_that_disagrees():
    owner_id = _make_owner()
    _as_admin()
    mid = client.post(
        "/api/eos/scorecard/metrics",
        json={
            "owner_id": str(owner_id),
            "name": "Loss Revenue",
            "goal_description": "<191,000 usd",
            "target_value": "191000",
            "value_type": "currency",
            "comparator": "<",
        },
    ).json()["id"]

    r = client.patch(
        f"/api/eos/scorecard/metrics/{mid}", json={"target_value": "150000"}
    )
    assert r.status_code == 200, r.text
    # The label still says 191,000, but the typed target wins.
    assert r.json()["target_value"] == "150000"
