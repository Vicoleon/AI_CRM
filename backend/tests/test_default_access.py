"""Default access: everyone in the domain gets the core app.

The spec (§4.3.4) removes the department pilot gate and the "no department, no
sign-in" lockout. What these tests pin:

* A person with no department signs in through the real session path and gets
  200 from ``/api/users/me`` with ``features.copilot`` true (Review Focus 3).
* No shipped default names a department or a person's email. Who may reach a
  feature beyond its default is an Admin → Feature Access row, never code.
* User-facing features open to every signed-in person by default. That
  includes Loose Ends, whose old default was the Sales department: being on it
  starts a morning read of the person's Gmail and Slack, so narrowing it never
  sweeps an admin or an exec in by their flags.
* Features scoped to admins, managers or execs and cross-person views stay
  closed to an ordinary person until an admin says otherwise.
"""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.tests._rosters import pin_rosters
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FEATURES, clear_cache, feature_allows, feature_by_key

_REGISTRY_PY = (
    Path(__file__).resolve().parents[2] / "rubik_core" / "features" / "registry.py"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    # These cases exercise shipped rosters; opt their environment switches in.
    for spec in FEATURES:
        if spec.kill_switch:
            monkeypatch.setattr(settings, spec.kill_switch, True)
    db_path = tmp_path / "test_default_access.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    clear_cache()
    yield
    clear_cache()
    app.dependency_overrides.clear()


def _person(
    email: str = "newhire@acme.example",
    *,
    department: str | None = "",
    roles: list[str] | None = None,
    is_exec: bool = False,
    managed: list[str] | None = None,
) -> UserProfile:
    now = "2026-10-27T00:00:00"
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


# ── Review Focus 3: a person with no department signs in ─────────────────────


def test_first_registered_person_is_not_promoted_to_admin():
    from backend.auth import user_registry

    first = user_registry.register_user(
        email="first@acme.example", name="First", id="first"
    )
    assert first.roles == ["user"]


@pytest.mark.parametrize("department", ["", "general"])
def test_no_department_user_gets_core(monkeypatch, department):
    """Through the real session-JWT path, so ``_apply_access_gates`` runs."""
    from backend.auth import user_registry
    from rubik_core.auth.session_tokens import mint_session_token

    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    # An unprovisioned user stays a user, even when they are the first row.
    user_registry.register_user(
        email="newhire@acme.example",
        name="New Hire",
        id="newhire",
        department=department,
        roles=["user"],
    )
    token, _, _ = mint_session_token("newhire@acme.example", "test")

    r = client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert "admin" not in body["roles"]
    assert body["features"]["copilot"] is True


# ── No department or person rosters in the shipped defaults ──────────────────


def test_registry_has_no_email_rosters():
    for spec in FEATURES:
        policy = spec.default()
        assert not policy.extra_emails, (spec.key, sorted(policy.extra_emails))
    # And nothing email-shaped is written into the registry module at all, so a
    # roster cannot come back through a constant a default reads later.
    tree = ast.parse(_REGISTRY_PY.read_text(encoding="utf-8"))
    hits = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _EMAIL.search(node.value)
    ]
    assert not hits, hits


def test_registry_defaults_name_no_department():
    named = {spec.key: spec.default().departments for spec in FEATURES}
    assert not {k: v for k, v in named.items() if v}, named


# ── User-facing features are open by default ─────────────────────────────────

CORE_KEYS = (
    "copilot",
    "todos",
    "scheduled_tasks",
    "voice_input",
    "voice_commands",
    "chat_file_upload",
    "meeting_skills",
    "page_tours",
    "whats_new",
    "open_commitments",
    "promise_accountability",
    "chrome_extension",
    "smart_teams",
    "customer_xray",
    "crm_sync",
    "sales_call_capture",
    "sales_meet_addon",
    "sales_cockpit",
    "proposal_generator",
    "sales_rhythm",
    "deal_defer",
    "loop_closure",
    "ai_priorities",
)


@pytest.mark.parametrize("key", CORE_KEYS)
def test_core_features_default_open(key):
    assert feature_allows(key, _person())


# ── Role-scoped features keep their role-based defaults ──────────────────────

CLOSED_TO_A_PLAIN_PERSON = (
    "sales_cockpit_manager",
    "sales_cockpit_supervisor",
    "sales_cockpit_exec",
    "insights",
    "insights_people",
    "close_date_rollup",
    "sales_rhythm_team",
    "loop_closure_team",
    "eos_rocks_all_teams",
    "fathom_auto_ingest",
    "smart_teams_support",
    "l10_meeting_restart",
)


@pytest.mark.parametrize("key", CLOSED_TO_A_PLAIN_PERSON)
def test_role_scoped_features_stay_closed_to_a_plain_person(key):
    assert not feature_allows(key, _person())


def test_every_feature_is_either_open_or_role_scoped():
    """A new feature has to say which kind it is, here and in its default."""
    keys = {spec.key for spec in FEATURES}
    assert keys == set(CORE_KEYS) | set(CLOSED_TO_A_PLAIN_PERSON)


def test_exec_and_admin_doors_follow_the_role():
    admin = _person("ops@acme.example", roles=["admin", "user"])
    exec_ = _person("chief@acme.example", is_exec=True)
    # Exec views open on the Exec-team flag, with no department.
    assert feature_allows("sales_cockpit_exec", exec_)
    assert feature_allows("insights", exec_)
    # Admins support the manager and supervisor views…
    assert feature_allows("sales_cockpit_manager", admin)
    assert feature_allows("sales_cockpit_supervisor", admin)
    # …but seniority alone opens neither: those read other people's work.
    assert not feature_allows("sales_cockpit_manager", exec_)
    assert not feature_allows("sales_cockpit_supervisor", exec_)
    # Cross-person rosters open by name only.
    for key in ("close_date_rollup", "sales_rhythm_team", "loop_closure_team"):
        assert not feature_allows(key, admin), key
        assert not feature_allows(key, exec_), key


def test_loose_ends_opens_to_everyone_but_narrowing_sweeps_in_no_flag_holder(
    monkeypatch,
):
    """Spec §4.3.4: the old Sales-only default opens to everyone. Being on the
    roster starts a morning read of the person's Gmail and Slack, so once an
    admin narrows it to a team (unticking Everyone keeps the shipped flags),
    holding the admin or Exec-team flag never adds anyone to the scan."""
    default = feature_by_key("loop_closure").default()
    assert default.everyone
    assert not default.admin_allowed
    assert not default.exec_allowed

    plain = _person()
    admin = _person("ops@acme.example", roles=["admin", "user"])
    exec_ = _person("chief@acme.example", is_exec=True)
    rep = _person("rep@acme.example", department="Sales")
    for who in (plain, admin, exec_, rep):
        assert feature_allows("loop_closure", who), who.email

    pin_rosters(
        monkeypatch,
        {
            "loop_closure": replace(
                default, everyone=False, departments=frozenset({"sales"})
            )
        },
    )
    assert feature_allows("loop_closure", rep)
    for who in (plain, admin, exec_):
        assert not feature_allows("loop_closure", who), who.email


def test_fathom_auto_ingest_ships_off_for_everyone():
    admin = _person("ops@acme.example", roles=["admin", "user"])
    exec_ = _person("chief@acme.example", is_exec=True)
    sales = _person("rep@acme.example", department="Sales")
    for who in (admin, exec_, sales):
        assert not feature_allows("fathom_auto_ingest", who), who.email
