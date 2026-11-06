"""Duplicate departments in the EOS team dropdown.

Two Sales, two Engineering and two Systems entries showed in the EOS view,
plus a `general` entry, which is the DEFAULT value of ``users.department``
("no department assigned"), never a team anyone can publish to.

``departments.name`` is the raw, case-sensitive PK, so a table can carry
case-variant rows (`sales`/`Sales`, `systems`/`Systems`,
`engineering`/`Engineering`), for example lowercase rows a stray test run
wrote. The admin and org-wide-exec branches of ``_team_scopes`` enumerated
those rows verbatim and de-duplicated on the literal string, so every pair
rendered twice. The org-wide exec holds the ``*`` wildcard, which is why they
saw the whole table.

These are read-side tests: the fix must hold without deleting the rows, and must
not depend on the database's collation for row order.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.routers.eos.me import EXEC_SCOPE, _canonical_departments
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

# A realistic shape: three case-variant pairs, the `general` sentinel, and
# rows that are legitimately distinct despite looking similar (`Finance` vs
# `Finance/Payroll` are different slugs → different teams).
LIVE_ROWS = [
    "Support",
    "Finance",
    "Finance/Payroll",
    "engineering",
    "Engineering",
    "general",
    "sales",
    "Sales",
    "systems",
    "Systems",
]


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_team_scopes_dedupe.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


def _profile(
    email: str,
    *,
    department: str = "general",
    roles: list[str] | None = None,
    is_exec: bool = False,
    managed: list[str] | None = None,
) -> UserProfile:
    now = "2026-08-19T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at=now,
        updated_at=now,
    )


def _as(profile: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: profile


def _seed_departments(rows: list[str]) -> None:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import Department

    with SessionLocal() as db:
        db.add_all([Department(name=r) for r in rows])
        db.commit()


def _dept_values(payload: dict) -> list[str]:
    return [s["value"] for s in payload["scopes"] if s["kind"] == "department"]


# ── the pure helper ─────────────────────────────────────────────────────────


def test_canonical_departments_folds_case_and_drops_the_general_sentinel():
    assert _canonical_departments(LIVE_ROWS) == [
        "Engineering",
        "Finance",
        "Finance/Payroll",
        "Sales",
        "Support",
        "Systems",
    ]


@pytest.mark.parametrize(
    "rows",
    [
        ["sales", "Sales"],
        ["Sales", "sales"],  # the winner must not depend on row order
    ],
)
def test_capitalised_spelling_wins_regardless_of_row_order(rows):
    assert _canonical_departments(rows) == ["Sales"]


def test_all_lowercase_department_keeps_its_only_spelling():
    """No canonical variant to promote to — better the lowercase label than a
    team that silently disappears from every picker."""
    assert _canonical_departments(["payroll"]) == ["payroll"]


def test_blank_and_whitespace_only_rows_are_dropped():
    assert _canonical_departments(["", "   ", None, "Sales"]) == ["Sales"]


# ── the endpoint ────────────────────────────────────────────────────────────


def test_orgwide_exec_sees_each_department_once():
    """The CEO shape: is_exec + the ``*`` wildcard. This is the exact payload
    behind the screenshot."""
    _seed_departments(LIVE_ROWS)
    _as(_profile("ceo@acme.example", department="Exec", is_exec=True, managed=["*"]))

    depts = _dept_values(client.get("/api/eos/me/meeting-scopes").json())

    # The caller's own department leads (pre-existing behaviour; the CEO's
    # `users.department` is "Exec", which has no `departments` row), then every
    # real team exactly once.
    assert depts == [
        "Exec",
        "Engineering",
        "Finance",
        "Finance/Payroll",
        "Sales",
        "Support",
        "Systems",
    ]
    assert "general" not in depts
    assert len(depts) == len({d.lower() for d in depts})


def test_admin_sees_each_department_once_plus_the_exec_scope():
    _seed_departments(LIVE_ROWS)
    _as(_profile("admin@acme.example", roles=["user", "admin"]))

    payload = client.get("/api/eos/me/meeting-scopes").json()

    assert payload["scopes"][0]["value"] == EXEC_SCOPE
    depts = _dept_values(payload)
    assert depts == [
        "Engineering",
        "Finance",
        "Finance/Payroll",
        "Sales",
        "Support",
        "Systems",
    ]


def test_own_lowercase_department_is_not_duplicated_by_the_canonical_row():
    """A user assigned to `sales` who ALSO holds the wildcard must see ONE Sales
    entry, in the spelling their own rows are tagged with — not both."""
    _seed_departments(LIVE_ROWS)
    _as(_profile("lead@acme.example", department="sales", is_exec=True, managed=["*"]))

    depts = _dept_values(client.get("/api/eos/me/meeting-scopes").json())

    assert depts.count("sales") == 1
    assert "Sales" not in depts


def test_managed_departments_list_folds_its_own_case_variants():
    """A Team Manager (non-exec) whose managed list carries both spellings gets
    one scope, and never the ``*`` sentinel as a literal team."""
    _seed_departments(LIVE_ROWS)
    _as(
        _profile(
            "manager@acme.example",
            department="Operations",
            managed=["sales", "Sales", "*"],
        )
    )

    depts = _dept_values(client.get("/api/eos/me/meeting-scopes").json())

    assert depts == ["Operations", "Sales"]
    assert "*" not in depts


def test_a_member_with_no_department_gets_no_team_scopes():
    """`general` is the unassigned default, so it must not become a team even
    when it is the caller's own stored department."""
    _seed_departments(LIVE_ROWS)
    _as(_profile("newhire@acme.example", department="general"))

    payload = client.get("/api/eos/me/meeting-scopes").json()

    assert payload["scopes"] == []
