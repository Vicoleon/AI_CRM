"""Feature access — the registry, the policy layer, the admin API, and the rule.

Three things are under test, and the third is the one that matters most:

1. ``rubik_core.features`` resolves a feature's roster from an admin override
   when one exists and from the shipped default otherwise, fails CLOSED (the
   admin door at most) when the table is unreadable, and judges a user the way
   every ``user_can_access_*`` predicate in ``backend/auth.py`` used to.
2. ``/api/admin/feature-access`` lets an admin change a roster, previews who
   gains or loses, resets to default, and audits every write; ``/api/users/me``
   ships the verdicts the client now reads instead of mirroring rosters.
3. THE RULE (docs/ENGINEERING_NOTES.md "Feature access is data, not code"): no department roster
   may be hard-coded in ``backend/auth.py`` again, and every registered feature
   key is known to the client. A new gated feature that skips the registry goes
   red here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.auth import (
    admin_required,
    get_current_user,
    user_can_access_cockpit_manager,
    user_can_access_full_app,
    user_can_access_smart_teams,
    user_can_access_xray,
)
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.features import (
    FEATURES,
    FeaturePolicy,
    clear_cache,
    effective_policy,
    feature_allows,
    feature_by_key,
    feature_keys,
    resolve_all,
    save_policy,
    user_allowed,
)

_REPO = Path(__file__).resolve().parents[2]
_AUTH_PY = _REPO / "backend" / "auth.py"
_AUTH_CONTEXT = _REPO / "frontend" / "src" / "context" / "AuthContext.tsx"

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    # Roster behavior is tested with the environment switches on; module and
    # switch precedence have their own workspace-module tests.
    for spec in FEATURES:
        if spec.kill_switch:
            monkeypatch.setattr(settings, spec.kill_switch, True)
    db_path = tmp_path / "test_feature_access.db"
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


def _profile(email, *, department="general", roles=None, is_exec=False, managed=None):
    now = "2026-09-04T00:00:00"
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


ADMIN = _profile("admin@acme.example", department="Systems", roles=["admin", "user"])
MARKETING = _profile("mk@acme.example", department="Marketing")
HR = _profile("hr@acme.example", department="HR")
EXEC = _profile("exec@acme.example", department="Finance", is_exec=True)


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


# ── 1. the registry and the policy layer ─────────────────────────────────────


def test_every_feature_is_declared_once_with_admin_facing_copy():
    keys = feature_keys()
    assert len(set(keys)) == len(keys)
    for spec in FEATURES:
        assert spec.label and spec.description and spec.surface and spec.group, spec.key
        assert re.fullmatch(r"[a-z][a-z0-9_]*", spec.key), spec.key
        policy = spec.default()
        assert isinstance(policy, FeaturePolicy)
        if spec.kill_switch:
            assert hasattr(settings, spec.kill_switch), spec.kill_switch


def test_unknown_key_names_the_registry():
    with pytest.raises(KeyError, match="registry.py"):
        feature_by_key("not_a_feature")


def test_defaults_open_user_facing_features_and_keep_role_doors():
    """What each gate ships with (spec §4.3.4). The full per-key split is in
    test_default_access.py; this pins the shapes the gates below rely on."""
    copilot = feature_by_key("copilot").default()
    assert copilot.everyone and copilot.exec_allowed and copilot.admin_allowed
    assert not copilot.departments and not copilot.extra_emails

    xray = feature_by_key("customer_xray").default()
    assert xray.everyone and xray.exec_allowed

    # Open, but narrowed to departments seniority still does not open it.
    crm = feature_by_key("crm_sync").default()
    assert crm.everyone and not crm.exec_allowed and not crm.extra_emails

    assert feature_by_key("sales_cockpit").default().everyone
    for key in ("sales_cockpit_manager", "sales_cockpit_supervisor"):
        p = feature_by_key(key).default()
        assert not p.everyone and p.admin_allowed and not p.exec_allowed, key
        assert not p.departments and not p.extra_emails, key
    exec_view = feature_by_key("sales_cockpit_exec").default()
    assert exec_view.exec_allowed and not exec_view.everyone
    assert feature_by_key("sales_cockpit_manager").membership == "managed_only"

    st = feature_by_key("smart_teams").default()
    assert st.everyone and not st.admin_allowed and not st.exec_allowed


def test_user_allowed_judges_every_door_in_order():
    spec = feature_by_key("customer_xray")
    policy = FeaturePolicy(
        departments=frozenset({"sales"}),
        extra_emails=frozenset({"guest@acme.example"}),
        exec_allowed=True,
        admin_allowed=True,
    )
    assert user_allowed(spec, policy, ADMIN)
    assert user_allowed(spec, policy, EXEC)
    assert user_allowed(spec, policy, _profile("Guest@ACME.example"))
    assert user_allowed(spec, policy, _profile("s@x.example", department=" SALES "))
    assert user_allowed(spec, policy, _profile("m@x.example", managed=["Sales"]))
    assert not user_allowed(spec, policy, MARKETING)
    # Flags off → those doors close.
    strict = FeaturePolicy(departments=frozenset({"sales"}))
    assert not user_allowed(spec, strict, EXEC)
    assert user_allowed(spec, strict, ADMIN)  # admin_allowed defaults True
    assert not user_allowed(
        spec,
        FeaturePolicy(departments=frozenset({"sales"}), admin_allowed=False),
        ADMIN,
    )
    # Everyone opens it regardless of roster; an empty roster is closed.
    assert user_allowed(spec, FeaturePolicy(everyone=True, admin_allowed=False), HR)
    assert not user_allowed(spec, FeaturePolicy(admin_allowed=False), HR)


def test_managed_only_membership_ignores_the_primary_department():
    spec = feature_by_key("sales_cockpit_manager")
    policy = FeaturePolicy(departments=frozenset({"sales"}))
    assert not user_allowed(spec, policy, _profile("ae@x.example", department="Sales"))
    assert user_allowed(spec, policy, _profile("mgr@x.example", managed=["Sales"]))


def test_user_allowed_accepts_an_orm_row_shape():
    """The priorities pipeline hands in a ``users`` row, not a UserProfile."""
    from types import SimpleNamespace

    spec = feature_by_key("open_commitments")
    policy = FeaturePolicy(departments=frozenset({"sales"}), admin_allowed=True)
    row = SimpleNamespace(
        email="p@x.example",
        department="Sales",
        managed_departments=None,
        is_exec=False,
        roles=[SimpleNamespace(name="user")],
    )
    assert user_allowed(spec, policy, row)
    admin_row = SimpleNamespace(
        email="a@x.example",
        department="HR",
        managed_departments=[],
        is_exec=False,
        roles=[SimpleNamespace(name="admin")],
    )
    assert user_allowed(spec, policy, admin_row)


def test_an_override_row_replaces_the_default_and_a_reset_restores_it():
    assert user_can_access_xray(MARKETING)  # ships open to everyone
    db = _session()
    try:
        save_policy(
            db,
            "customer_xray",
            FeaturePolicy(
                departments=frozenset({"sales", "systems"}),
                exec_allowed=True,
            ),
            actor_email=ADMIN.email,
            note="Narrowed to the teams that own accounts",
        )
    finally:
        db.close()
    policy, customized = effective_policy("customer_xray")
    assert customized and not policy.everyone and "marketing" not in policy.departments
    assert not user_can_access_xray(MARKETING)

    from rubik_core.features import reset_policy

    db = _session()
    try:
        assert reset_policy(db, "customer_xray")
    finally:
        db.close()
    _, customized = effective_policy("customer_xray")
    assert not customized
    assert user_can_access_xray(MARKETING)


def test_the_gates_in_auth_py_read_the_override():
    """The predicates every router depends on are now data-driven end to end."""
    db = _session()
    try:
        save_policy(
            db,
            "copilot",
            FeaturePolicy(departments=frozenset({"sales"}), admin_allowed=True),
            actor_email=ADMIN.email,
        )
        save_policy(
            db,
            "sales_cockpit_manager",
            FeaturePolicy(departments=frozenset({"sales"})),
            actor_email=ADMIN.email,
        )
    finally:
        db.close()
    assert not user_can_access_full_app(HR)
    assert user_can_access_full_app(_profile("s@x.example", department="Sales"))
    assert user_can_access_cockpit_manager(_profile("d@x.example", managed=["Sales"]))
    assert not user_can_access_cockpit_manager(
        _profile("ae@x.example", department="Sales")
    )


def test_smart_teams_keeps_its_domain_check_over_the_roster():
    db = _session()
    try:
        save_policy(
            db,
            "smart_teams",
            FeaturePolicy(everyone=True, admin_allowed=False),
            actor_email=ADMIN.email,
        )
    finally:
        db.close()
    assert user_can_access_smart_teams(HR)
    # "Everyone" still means every EMPLOYEE — the Workspace-domain rule is
    # structural and not something the admin page can switch off.
    assert not user_can_access_smart_teams(
        _profile("outsider@gmail.com", department="HR")
    )


class _BrokenSession:
    """A session whose read fails the way an aborted transaction does."""

    def __init__(self):
        self.rollbacks = 0

    def get(self, *_a, **_k):
        raise RuntimeError("current transaction is aborted")

    def rollback(self):
        self.rollbacks += 1


def _narrow(key, policy):
    db = _session()
    try:
        save_policy(db, key, policy, actor_email=ADMIN.email)
    finally:
        db.close()
    clear_cache()


def test_an_unreadable_table_fails_closed(monkeypatch):
    """Never a 500, and never the shipped default, which is mostly "everyone":
    a failed read admits nobody but the admin door."""
    from rubik_core.features import policy as policy_mod

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr("rubik_core.database.db.SessionLocal", boom)
    clear_cache()
    assert policy_mod.stored_policy("customer_xray") is policy_mod.READ_FAILED
    assert not feature_allows("customer_xray", MARKETING)  # ships open
    assert not feature_allows("copilot", MARKETING)
    # The exec door shuts too; the admin door stays open where it ships open.
    assert not feature_allows("insights", EXEC)
    assert feature_allows("insights", ADMIN)
    assert feature_allows("customer_xray", ADMIN)
    # Not cached: the admin page and /users/me read the table again next call.
    from rubik_core.tenancy.context import current_workspace

    assert (current_workspace(), "customer_xray") not in policy_mod._CACHE


def test_a_failed_read_denies_an_open_and_a_narrowed_feature():
    _narrow("crm_sync", FeaturePolicy(departments=frozenset({"marketing"})))
    assert feature_allows("crm_sync", MARKETING)
    clear_cache()

    broken = _BrokenSession()
    assert not feature_allows("crm_sync", MARKETING, broken)  # narrowed to them
    assert not feature_allows("customer_xray", MARKETING, broken)  # ships open
    assert not feature_allows("loop_closure", MARKETING, broken)  # own mailbox
    assert broken.rollbacks == 3


def test_the_next_healthy_read_is_not_served_from_a_cached_error():
    clear_cache()
    assert not feature_allows("customer_xray", MARKETING, _BrokenSession())
    # The very next call, on a healthy session, reads the table again.
    assert feature_allows("customer_xray", MARKETING)
    _narrow("crm_sync", FeaturePolicy(departments=frozenset({"hr"})))
    assert not feature_allows("crm_sync", MARKETING, _BrokenSession())
    assert not feature_allows("crm_sync", MARKETING)
    assert feature_allows("crm_sync", HR)


def test_smart_teams_support_never_widens_after_an_error():
    """The support view's row narrows WHICH admins see raw answers. A failed
    read cannot tell which, so it admits no admin at all, and the narrowed row
    is back in force on the next healthy read."""
    hr_admin = _profile("hr.admin@acme.example", department="HR", roles=["admin"])
    _narrow(
        "smart_teams_support",
        FeaturePolicy(departments=frozenset({"hr"}), admin_allowed=False),
    )
    assert feature_allows("smart_teams_support", hr_admin)
    assert not feature_allows("smart_teams_support", ADMIN)
    clear_cache()  # the cached healthy read would otherwise answer

    broken = _BrokenSession()
    assert not feature_allows("smart_teams_support", ADMIN, broken)
    assert not feature_allows("smart_teams_support", hr_admin, broken)

    assert not feature_allows("smart_teams_support", ADMIN)
    assert feature_allows("smart_teams_support", hr_admin)


def test_the_lookup_is_cached_and_a_write_clears_it():
    from rubik_core.features import policy as policy_mod

    calls = {"n": 0}
    real = policy_mod._read_stored

    def counting(key, db):
        calls["n"] += 1
        return real(key, db)

    policy_mod._read_stored = counting
    try:
        clear_cache()
        feature_allows("crm_sync", MARKETING)
        feature_allows("crm_sync", MARKETING)
        assert calls["n"] == 1
        db = _session()
        try:
            save_policy(
                db,
                "crm_sync",
                FeaturePolicy(departments=frozenset({"marketing"})),
                actor_email=ADMIN.email,
            )
        finally:
            db.close()
        assert feature_allows("crm_sync", MARKETING)
        assert calls["n"] == 2
    finally:
        policy_mod._read_stored = real


def test_policy_cache_is_scoped_to_workspace(monkeypatch):
    """One workspace's open roster cannot authorize another workspace."""
    from rubik_core.control import repository as control_repo
    from rubik_core.features import policy as policy_mod
    from rubik_core.tenancy.context import current_workspace, use_workspace

    reads: list[str] = []

    def read(_key, _db):
        slug = current_workspace()
        reads.append(slug)
        return FeaturePolicy(everyone=slug == "alpha", admin_allowed=False)

    monkeypatch.setattr(policy_mod, "_read_stored", read)
    monkeypatch.setattr(
        control_repo,
        "module_flags",
        lambda slug: {"core": True, "eos": False, "sales_coach": False},
    )
    policy_mod.clear_cache()
    try:
        with use_workspace("alpha"):
            assert feature_allows("customer_xray", MARKETING)
        with use_workspace("beta"):
            assert not feature_allows("customer_xray", MARKETING)
            policy_mod.clear_cache("customer_xray")
            assert not feature_allows("customer_xray", MARKETING)
        with use_workspace("alpha"):
            assert feature_allows("customer_xray", MARKETING)
        assert reads == ["alpha", "beta", "beta"]
    finally:
        policy_mod.clear_cache()


def test_policy_write_cannot_republish_an_older_inflight_read(monkeypatch):
    """A save between read and cache insert must make the reader retry."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from rubik_core.features import policy as policy_mod

    started = Event()
    proceed = Event()
    current = {"policy": FeaturePolicy(everyone=True)}
    reads: list[FeaturePolicy] = []

    def delayed_read(_key, _db):
        value = current["policy"]
        reads.append(value)
        if len(reads) == 1:
            started.set()
            assert proceed.wait(timeout=5)
        return value

    monkeypatch.setattr(policy_mod, "_read_stored", delayed_read)
    policy_mod.clear_cache()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(policy_mod.stored_policy, "customer_xray")
            assert started.wait(timeout=5)
            current["policy"] = FeaturePolicy(admin_allowed=False)
            policy_mod.clear_cache("customer_xray")
            proceed.set()
            assert future.result(timeout=5) == current["policy"]
        assert len(reads) == 2
        assert policy_mod.stored_policy("customer_xray") == current["policy"]
        assert len(reads) == 2  # The retry, not the stale read, was cached.
    finally:
        proceed.set()
        policy_mod.clear_cache()


# ── 2. the admin API and /users/me ───────────────────────────────────────────


def _as(user):
    app.dependency_overrides[get_current_user] = lambda: user
    if user.is_admin:
        app.dependency_overrides[admin_required] = lambda: user


def test_users_me_ships_a_verdict_for_every_registered_feature():
    _as(_profile("support@acme.example", department="Support"))
    r = client.get("/api/users/me")
    assert r.status_code == 200, r.text
    body = r.json()
    features = body["features"]
    assert set(features) == set(feature_keys())
    assert features["copilot"] is True  # everyone, by default
    assert features["customer_xray"] is True
    assert features["insights"] is False  # exec + admin only
    assert features["fathom_auto_ingest"] is False  # ships off
    assert body["smart_teams_support"] is False  # not a support admin
    assert features["smart_teams_support"] is False


@pytest.mark.parametrize(
    "who, expected",
    [
        # Named on the row but not an admin: the raw roster says yes, the gate no.
        (_profile("hr@acme.example", department="HR"), False),
        (_profile("hr.admin@acme.example", department="HR", roles=["admin"]), True),
        (ADMIN, False),  # an admin the row leaves out
    ],
    ids=["named-non-admin", "named-admin", "other-admin"],
)
def test_users_me_ships_one_smart_teams_support_verdict(who, expected):
    _narrow(
        "smart_teams_support",
        FeaturePolicy(departments=frozenset({"hr"}), admin_allowed=False),
    )
    _as(who)
    body = client.get("/api/users/me").json()
    assert body["smart_teams_support"] is expected
    assert body["features"]["smart_teams_support"] is expected


def test_the_admin_page_lists_every_feature_with_its_live_roster_and_members():
    from backend.auth import user_registry

    # register_user() promotes the FIRST row to admin; say "user" explicitly or
    # the admin door admits the Marketing fixture.
    user_registry.register_user(
        email="mk@acme.example",
        name="Mk",
        id="mk",
        department="Marketing",
        roles=["user"],
    )
    user_registry.register_user(
        email="ae@acme.example",
        name="Ae",
        id="ae",
        department="Sales",
        roles=["user"],
    )
    _as(ADMIN)
    r = client.get("/api/admin/feature-access")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["departments"], list)
    by_key = {f["key"]: f for f in body["features"]}
    assert set(by_key) == set(feature_keys())
    xray = by_key["customer_xray"]
    assert xray["customized"] is False
    assert xray["policy"] == xray["default"]
    emails = {m["email"] for m in xray["members"]}
    assert {"ae@acme.example", "mk@acme.example"} <= emails
    # A feature that ships with nobody on it lists nobody.
    assert by_key["loop_closure_team"]["members"] == []
    assert by_key["sales_cockpit"]["kill_switch"]["name"] == "COCKPIT_ENABLED"


def test_a_non_admin_cannot_read_or_write_the_rosters():
    _as(MARKETING)
    assert client.get("/api/admin/feature-access").status_code == 403
    r = client.put(
        "/api/admin/feature-access/customer_xray",
        json={"departments": ["Sales"], "extra_emails": []},
    )
    assert r.status_code == 403
    assert user_can_access_xray(MARKETING)  # nothing was narrowed


def test_preview_then_save_then_reset_with_an_audit_trail():
    from backend.auth import user_registry
    from rubik_core.database.models import AuditLog

    user_registry.register_user(
        email="mk@acme.example",
        name="Mk",
        id="mk",
        department="Marketing",
        roles=["user"],
    )
    _as(ADMIN)
    body = {
        "departments": ["Sales", "Systems"],  # labels, not slugs
        "extra_emails": ["Guest@ACME.example"],
        "exec_allowed": True,
        "admin_allowed": True,
        "everyone": False,
        "note": "Narrowed to the teams that own accounts",
    }
    # What-if: nothing is saved.
    r = client.post("/api/admin/feature-access/customer_xray/preview", json=body)
    assert r.status_code == 200, r.text
    assert "mk@acme.example" not in {m["email"] for m in r.json()["members"]}
    assert user_can_access_xray(MARKETING)

    r = client.put("/api/admin/feature-access/customer_xray", json=body)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["customized"] is True
    assert saved["policy"]["departments"] == ["sales", "systems"]
    assert saved["policy"]["extra_emails"] == ["guest@acme.example"]
    assert saved["updated_by"] == ADMIN.email
    assert saved["note"] == body["note"]
    assert not user_can_access_xray(MARKETING)

    r = client.delete("/api/admin/feature-access/customer_xray")
    assert r.status_code == 200, r.text
    assert r.json()["customized"] is False
    assert user_can_access_xray(MARKETING)

    db = _session()
    try:
        actions = [
            a.action
            for a in db.query(AuditLog).filter(AuditLog.doc_id == "customer_xray").all()
        ]
    finally:
        db.close()
    assert actions == ["FEATURE_ACCESS_UPDATE", "FEATURE_ACCESS_RESET"]


def test_bad_input_is_refused():
    _as(ADMIN)
    r = client.put(
        "/api/admin/feature-access/customer_xray",
        json={"departments": [], "extra_emails": ["not-an-email"]},
    )
    assert r.status_code == 422
    assert client.put("/api/admin/feature-access/nope", json={}).status_code == 404


def test_resolve_all_matches_the_predicates():
    verdicts = resolve_all(EXEC)
    assert verdicts["copilot"] == user_can_access_full_app(EXEC)
    assert verdicts["customer_xray"] == user_can_access_xray(EXEC)
    assert verdicts["smart_teams"] == user_can_access_smart_teams(EXEC)


# ── 3. THE RULE — no roster returns to auth.py; the client knows every key ──


def test_no_department_roster_is_hard_coded_in_auth_py():
    """A new gate that ships with a frozenset instead of a registry entry.

    This is the tripwire behind docs/ENGINEERING_NOTES.md "Feature access is data, not code".
    Register the feature in rubik_core/features/registry.py and call
    feature_allows(<key>, user) from the gate.
    """
    import ast

    tree = ast.parse(_AUTH_PY.read_text(encoding="utf-8"), filename=str(_AUTH_PY))
    literal_rosters: list[int] = []
    settings_rosters: list[int] = []
    for node in ast.walk(tree):
        # frozenset({"sales", ...}) / {"sales", ...} of string constants — CODE,
        # not prose: comments and docstrings are not visited by ast.walk.
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "frozenset":
            if node.args and isinstance(node.args[0], ast.Set):
                elts = node.args[0].elts
                if elts and all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str)
                    for e in elts
                ):
                    literal_rosters.append(node.lineno)
        # settings.<X>_DEPARTMENTS read behind the registry's back.
        if (
            isinstance(node, ast.Attribute)
            and getattr(node.value, "id", None) == "settings"
            and node.attr.endswith("_DEPARTMENTS")
        ):
            settings_rosters.append(node.lineno)
    assert not literal_rosters, (
        "backend/auth.py hard-codes a department roster again (lines "
        f"{literal_rosters}). Onboarding a department must be an Admin → Feature "
        "Access action, not a deploy: register the feature in "
        "rubik_core/features/registry.py and call feature_allows()."
    )
    assert not settings_rosters, (
        f"backend/auth.py reads a settings.*_DEPARTMENTS list (lines "
        f"{settings_rosters}); route it through a FeatureSpec default instead."
    )


#: Where gates, jobs and agents live: a department name compared to a literal
#: anywhere here is a roster, or a special team, written into code.
_GATE_CODE_ROOTS = (
    "backend/auth.py",
    "backend/dependencies.py",
    "backend/routers",
    "backend/agents",
    "backend/jobs",
    "backend/services",
    "rubik_core",
)

#: Literals that are structure, not a team: the ``users.department`` default
#: that means "no department".
_STRUCTURAL_DEPARTMENTS = frozenset({"general"})


def _department_literal_compares(path: Path) -> list[str]:
    import ast

    source = path.read_text(encoding="utf-8")
    hits = []
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        normalised = any(
            isinstance(o, ast.Call)
            and (
                getattr(o.func, "attr", None) in {"lower", "strip", "casefold"}
                or getattr(o.func, "id", None) == "slugify_department"
            )
            for o in operands
        )
        literals = [
            o.value
            for o in operands
            if isinstance(o, ast.Constant)
            and isinstance(o.value, str)
            and o.value.strip().lower() not in _STRUCTURAL_DEPARTMENTS
        ]
        if not (normalised and literals):
            continue
        segment = ast.get_source_segment(source, node) or ""
        if "department" in segment.lower() or "dept" in segment.lower():
            hits.append(f"{path.relative_to(_REPO)}:{node.lineno}: {segment}")
    return hits


def test_no_gate_job_or_agent_compares_a_department_to_a_literal():
    """``user.department.lower() == "systems"`` is a roster written in code: it
    hands a team's powers to whoever's directory happens to use that name.
    Gate on ``feature_allows(<key>, user)`` or the admin role instead, and put
    a team a feature is FOR in a setting, compared by slug."""
    hits: list[str] = []
    for root in _GATE_CODE_ROOTS:
        path = _REPO / root
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for file in files:
            hits.extend(_department_literal_compares(file))
    assert hits == [], "department compared to a literal:\n" + "\n".join(hits)


def test_the_client_knows_every_registered_feature_key_and_holds_no_roster():
    ctx = _AUTH_CONTEXT.read_text(encoding="utf-8")
    for key in feature_keys():
        assert f"'{key}'" in ctx, (
            f"frontend/src/context/AuthContext.tsx does not list feature {key!r} "
            "in FEATURE — add it so the nav can read features.<key>"
        )
    for forbidden in (
        "FULL_APP_PILOT_DEPARTMENTS",
        "XRAY_DEPARTMENTS",
        "CRM_SYNC_DEPARTMENTS",
        "SALES_KAIZEN_PILOT_DEPARTMENTS",
        "COCKPIT_EXTRA_EMAILS",
        "SMART_TEAMS_ROLLOUT_DEPARTMENTS",
        "SMART_TEAMS_RAW_DEPARTMENTS",
    ):
        assert f"const {forbidden}" not in ctx, (
            f"AuthContext.tsx grew a client-side roster ({forbidden}). Rosters are "
            "server data; read features.<key> from /api/users/me instead."
        )
    # Nor a department compare standing in for one (the old Fathom auto-ingest
    # rule): a client gate reads features.<key>.
    assert not re.search(r"department\??\.toLowerCase\(\)\s*===", ctx), (
        "AuthContext.tsx gates on a department string again"
    )
    assert "hasFeature(currentUser, FEATURE.fathomAutoIngest)" in ctx
