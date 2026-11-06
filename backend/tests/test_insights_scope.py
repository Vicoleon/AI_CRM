"""Insights scope and population (README §1).

* Who sees which departments: admins and org-wide execs everything; anyone
  else what they manage plus their own, by RAG slug, never ``general``.
* Who counts: Workspace-domain ``users`` rows in those departments, minus the
  impersonation placeholder and workspace-owned exclusions, case-folded;
  departed people through the ``offboard_records`` snapshot.
* Eligibility follows Admin → Feature Access, overrides included.
* The two Insights rows ship admin + exec, managed-only, behind the kill switch.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from rubik_core.company.operations import OperationsConfig
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy, clear_cache, feature_by_key, save_policy
from rubik_core.insights import population
from rubik_core.insights.population import EVERYONE, load_population
from rubik_core.insights.scope import (
    ScopeError,
    parse_dept_param,
    resolve_scope,
)

DOMAIN = "company.com"


def _profile(email, *, department="general", roles=None, is_exec=False, managed=None):
    return UserProfile(
        id=email,
        email=email,
        name=email.split("@")[0],
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at="",
        updated_at="",
    )


@pytest.fixture
def db(tmp_path, monkeypatch, set_test_workspace_domains):
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database.models import (
        Base,
        Department,
        OffboardRecord,
        Role,
        User,
    )

    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'scope.db'}")
    set_test_workspace_domains(DOMAIN)
    monkeypatch.setattr(population, "get_operations_config", OperationsConfig)
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    clear_cache()
    session = dbmod.SessionLocal()
    admin_role = Role(name="admin")
    session.add_all([admin_role, Role(name="user")])
    # Case-variant duplicate department rows, as on the live table.
    for name in ("Sales", "sales", "Marketing", "Systems", "Compliance/Audit"):
        session.add(Department(name=name))
    rows = [
        ("ann@company.com", "Ann", "Sales"),
        ("Bob@Company.com", "Bob", "sales"),
        ("cy@company.com", "Cy", "Marketing"),
        ("dee@company.com", "Dee", "Systems"),
        ("eve@company.com", "Eve", "general"),
        ("vendor@elsewhere.com", "Vendor", "Sales"),
        ("impersonated_user@systems.local", "Imp", "Sales"),
        ("fay@company.com", "Fay", "Compliance/Audit"),
    ]
    for email, name, dept in rows:
        u = User(email=email, id=email, name=name, department=dept)
        if name == "Dee":
            u.roles = [admin_role]
        session.add(u)
    session.add(
        OffboardRecord(
            id="off-1",
            user_email="gone@company.com",
            user_name="Gone Person",
            department="Sales",
            successor_email="ann@company.com",
            created_by="dee@company.com",
            offboarded_at=datetime(2026, 9, 1),
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        clear_cache()
        engine.dispose()
        dbmod._engine = None


# ── scope ────────────────────────────────────────────────────────────────────


def test_admins_and_org_wide_execs_see_every_department_folded_by_slug(db):
    for viewer in (
        _profile("dee@company.com", department="Systems", roles=["admin"]),
        _profile("ceo@company.com", department="Finance", is_exec=True, managed=["*"]),
    ):
        scope = resolve_scope(db, viewer)
        assert scope.all_departments is True
        assert scope.allowed == ("compliance_audit", "marketing", "sales", "systems")
        assert scope.label("sales") == "Sales"  # the spelling with capitals wins
        assert scope.label("compliance_audit") == "Compliance/Audit"
        assert scope.company_wide is True


def test_anyone_else_sees_what_they_manage_plus_their_own(db):
    viewer = _profile(
        "mgr@company.com", department="Marketing", is_exec=True, managed=[" SALES "]
    )
    scope = resolve_scope(db, viewer)
    assert scope.all_departments is False
    assert scope.allowed == ("marketing", "sales")
    assert scope.selected == ("marketing", "sales")
    assert scope.company_wide is False


def test_a_scoped_exec_without_the_wildcard_is_not_org_wide(db):
    viewer = _profile(
        "x@company.com", department="general", is_exec=True, managed=["Sales"]
    )
    assert resolve_scope(db, viewer).allowed == ("sales",)
    nobody = _profile("y@company.com", department="general", is_exec=True)
    assert resolve_scope(db, nobody).allowed == ()


def test_a_department_filter_outside_scope_is_refused(db):
    viewer = _profile("mgr@company.com", department="Marketing", managed=["Sales"])
    assert resolve_scope(db, viewer, "Sales").selected == ("sales",)
    with pytest.raises(ScopeError, match="systems"):
        resolve_scope(db, viewer, "sales,systems")
    # Admins may filter to anything, including a department with no row.
    admin = _profile("dee@company.com", roles=["admin"])
    assert resolve_scope(db, admin, "legal").selected == ("legal",)


def test_the_dept_parameter_is_slugged_deduped_and_sorted():
    assert parse_dept_param("Sales, marketing,SALES,,") == ("marketing", "sales")
    assert parse_dept_param("") == ()
    assert parse_dept_param(None) == ()


# ── population ───────────────────────────────────────────────────────────────


def test_people_are_domain_users_in_scope_minus_general_and_placeholders(db):
    admin = _profile("dee@company.com", roles=["admin"])
    pop = load_population(db, resolve_scope(db, admin))
    assert set(pop.people) == {
        "ann@company.com",
        "bob@company.com",
        "cy@company.com",
        "dee@company.com",
        "fay@company.com",
    }
    assert pop.people["bob@company.com"].dept == "sales"
    assert pop.people["bob@company.com"].dept_label == "Sales"
    assert pop.people["dee@company.com"].is_admin is True
    assert set(pop.departed) == {"gone@company.com"}
    assert pop.dept_of("GONE@company.com") == "sales"
    assert pop.in_scope("gone@company.com") and not pop.in_scope("vendor@elsewhere.com")


def test_the_selected_departments_narrow_the_population(db):
    admin = _profile("dee@company.com", roles=["admin"])
    pop = load_population(db, resolve_scope(db, admin, "marketing"))
    assert set(pop.people) == {"cy@company.com"} and not pop.departed


@pytest.mark.parametrize(
    "value",
    [("ANN@company.com", "cy@company.com"), ["ann@company.com", "CY@company.com"]],
)
def test_excluded_emails_are_left_out(db, monkeypatch, value):
    monkeypatch.setattr(
        population,
        "get_operations_config",
        lambda: OperationsConfig(insights_excluded_emails=value),
    )
    admin = _profile("dee@company.com", roles=["admin"])
    pop = load_population(db, resolve_scope(db, admin))
    assert "ann@company.com" not in pop.people and "cy@company.com" not in pop.people
    assert "bob@company.com" in pop.people


def test_eligibility_follows_feature_access_including_overrides(db):
    admin = _profile("dee@company.com", roles=["admin"])
    pop = load_population(db, resolve_scope(db, admin))
    # The copilot ships to everyone.
    assert pop.eligible(None) == set(pop.people)
    assert pop.eligible(EVERYONE) == set(pop.people)
    save_policy(
        db,
        "copilot",
        FeaturePolicy(departments=frozenset({"compliance_audit"}), admin_allowed=False),
        actor_email="dee@company.com",
    )
    fresh = load_population(db, resolve_scope(db, admin))
    assert fresh.eligible(None) == {"fay@company.com"}


def test_one_person_population_keeps_only_that_person(db):
    admin = _profile("dee@company.com", roles=["admin"])
    pop = load_population(db, resolve_scope(db, admin))
    one = pop.restricted_to("Ann@company.com")
    assert set(one.people) == {"ann@company.com"} and not one.departed
    gone = pop.restricted_to("gone@company.com")
    assert not gone.people and set(gone.departed) == {"gone@company.com"}


# ── the two Feature Access rows ──────────────────────────────────────────────


def test_the_insights_rows_ship_to_admins_and_execs_managed_only():
    for key in ("insights", "insights_people"):
        spec = feature_by_key(key)
        policy = spec.default()
        assert policy.admin_allowed and policy.exec_allowed
        assert (
            not policy.departments and not policy.extra_emails and not policy.everyone
        )
        assert spec.membership == "managed_only"
        assert spec.kill_switch == "INSIGHTS_ENABLED"
        assert spec.group == "Insights"
