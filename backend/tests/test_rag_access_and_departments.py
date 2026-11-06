from __future__ import annotations

"""Tests for the audience policy (`rags_for_user_email`) and the
POST /api/departments/ → ensure_corpus hook."""

import base64
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _unique_dept(label: str) -> str:
    """Return a per-test-run-unique department name.

    The router uses a module-level `dept_manager = SQLMetadataRegistry()` bound
    to the real DB at import time, so the per-test tmp_path DB swap doesn't
    actually isolate department writes. Suffixing with a uuid keeps tests from
    colliding with each other or with stale state in dam_data/dam.db.
    """
    return f"{label}-{uuid.uuid4().hex[:8]}"


from backend.auth import get_current_user
from backend.main import app
from backend.models.auth import User
from rubik_core.auth.registry import UserRegistry
from rubik_core.auth.session_tokens import mint_session_token
from rubik_core.config import AppMode, settings
from rubik_core.rag import build_registry, rags_for_user_email, slugify_department
from rubik_core.tenancy.context import use_workspace

client = TestClient(app)


def _admin_session_headers() -> dict[str, str]:
    with use_workspace("test"):
        token, _, _ = mint_session_token("admin@example.com", "test", hours=1.0)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "CUBIE_SESSION_SECRET", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    db_path = tmp_path / "test_rag_access.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    from rubik_core.database.db import engine
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    dam_dir = tmp_path / "dam_data"
    dam_dir.mkdir()
    settings.DAM_DATA_DIR = str(dam_dir)
    yield
    app.dependency_overrides.clear()


# ── slugify ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label,expected",
    [
        ("HR", "hr"),
        ("Sales", "sales"),
        ("Compliance/Audit", "compliance_audit"),
        (" Finance ", "finance"),
        ("Eng & IT", "eng_it"),
        ("", ""),
        ("___", ""),
    ],
)
def test_slugify_department(label, expected):
    assert slugify_department(label) == expected


# ── rags_for_user_email ────────────────────────────────────────────────────


def test_rags_for_user_unknown_user_gets_companywide_only():
    with patch(
        "rubik_core.rag.access.current_registry",
        return_value=build_registry(["Sales", "HR"]),
    ):
        out = rags_for_user_email("nobody@example.com")
    assert out == ["companywide"]


def test_rags_for_user_dept_member_gets_dept_plus_companywide():
    UserRegistry().register_user(
        email="hank@example.com", name="Hank", id="h1", roles=["user"], department="HR"
    )
    with patch(
        "rubik_core.rag.access.current_registry",
        return_value=build_registry(["Sales", "HR"]),
    ):
        out = rags_for_user_email("hank@example.com")
    # Department first, companywide second — useful for log readability.
    assert out == ["hr", "companywide"]


def test_rags_for_user_admin_gets_everything():
    """An admin who is NOT on the Exec team sees every DEPARTMENT corpus
    (management/debug reach) but NEVER the Exec-only corpora (VTO / eos_exec).

    Being an admin is an app-super-user role; it does not clear the holder for
    Exec-team-confidential content. Exec-only access requires
    ``is_exec``/``sees_all_departments`` (otherwise an admin's AI surfaces
    leak Exec-team content)."""
    UserRegistry().register_user(
        email="boss@example.com",
        name="Boss",
        id="b1",
        roles=["admin"],
        department="Sales",
    )
    with patch(
        "rubik_core.rag.access.current_registry",
        return_value=build_registry(["Sales", "HR", "Finance"]),
    ):
        out = rags_for_user_email("boss@example.com")
    assert set(out) == {"companywide", "sales", "hr", "finance"}
    assert "vto" not in out and "eos_exec" not in out


def test_rags_for_user_admin_who_is_also_exec_gets_everything():
    """An admin who is *also* on the Exec team keeps full access — the
    exclusion above is strictly for NON-exec admins."""
    UserRegistry().register_user(
        email="execboss@example.com",
        name="ExecBoss",
        id="eb1",
        roles=["admin"],
        department="Sales",
    )
    UserRegistry().update_user("execboss@example.com", {"is_exec": True})
    with patch(
        "rubik_core.rag.access.current_registry",
        return_value=build_registry(["Sales", "HR", "Finance"]),
    ):
        out = rags_for_user_email("execboss@example.com")
    assert set(out) == {"companywide", "sales", "hr", "finance", "vto", "eos_exec"}


def test_rags_for_user_exec_gets_vto():
    """A non-admin Exec-team member gets their dept + companywide + the VTO."""
    UserRegistry().register_user(
        email="exec@example.com",
        name="Exec",
        id="e1",
        roles=["user"],
        department="Sales",
    )
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    with patch(
        "rubik_core.rag.access.current_registry",
        return_value=build_registry(["Sales", "HR"]),
    ):
        out = rags_for_user_email("exec@example.com")
    assert "vto" in out
    assert set(out) == {"sales", "companywide", "vto", "eos_exec"}


def test_rags_for_user_orgwide_exec_sees_every_department():
    """An org-wide exec (the CEO) — is_exec + managed_departments "*" — gets
    every corpus in the registry, like an admin, including the Exec-only VTO."""
    UserRegistry().register_user(
        email="ceo@example.com",
        name="CEO",
        id="c1",
        roles=["user"],
        department="general",
    )
    UserRegistry().update_user(
        "ceo@example.com",
        {"is_exec": True, "managed_departments": ["*"]},
    )
    with patch(
        "rubik_core.rag.access.current_registry",
        return_value=build_registry(["Sales", "HR", "Finance"]),
    ):
        out = rags_for_user_email("ceo@example.com")
    assert set(out) == {"companywide", "sales", "hr", "finance", "vto", "eos_exec"}


def test_rags_for_user_scoped_exec_gets_managed_departments():
    """A scoped exec (managed_departments=["HR"]) gets their own dept +
    companywide + each managed dept + the VTO — but NOT unrelated depts."""
    UserRegistry().register_user(
        email="scopedexec@example.com",
        name="Scoped Exec",
        id="se1",
        roles=["user"],
        department="Sales",
    )
    UserRegistry().update_user(
        "scopedexec@example.com",
        {"is_exec": True, "managed_departments": ["HR"]},
    )
    with patch(
        "rubik_core.rag.access.current_registry",
        return_value=build_registry(["Sales", "HR", "Finance"]),
    ):
        out = rags_for_user_email("scopedexec@example.com")
    assert set(out) == {"sales", "companywide", "hr", "vto", "eos_exec"}
    assert "finance" not in out


def test_rags_for_user_regular_member_excludes_vto():
    """A regular (non-exec, non-admin) user must never get the VTO."""
    UserRegistry().register_user(
        email="rank@example.com",
        name="Rank",
        id="r1",
        roles=["user"],
        department="Sales",
    )
    with patch(
        "rubik_core.rag.access.current_registry",
        return_value=build_registry(["Sales", "HR"]),
    ):
        out = rags_for_user_email("rank@example.com")
    assert "vto" not in out
    assert out == ["sales", "companywide"]


def test_rags_for_user_dept_not_in_registry_falls_back_to_companywide():
    """A user whose department was deleted shouldn't get a dangling slug."""
    UserRegistry().register_user(
        email="orphan@example.com",
        name="Orphan",
        id="o1",
        roles=["user"],
        department="Marketing",
    )
    with patch(
        "rubik_core.rag.access.current_registry",
        return_value=build_registry(["Sales", "HR"]),  # no Marketing
    ):
        out = rags_for_user_email("orphan@example.com")
    assert out == ["companywide"]


# ── POST /api/departments/ ensures a RAG corpus ────────────────────────────


def test_add_department_calls_ensure_corpus_in_gcp_mode():
    UserRegistry().register_user(
        email="admin@example.com",
        name="Admin",
        id="a1",
        roles=["admin"],
        department="Sales",
    )
    app.dependency_overrides[get_current_user] = lambda: User(
        id="a1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        department="Sales",
    )

    prev_mode = settings.APP_MODE
    settings.APP_MODE = AppMode.GCP
    try:
        # Patch RagManager so no Vertex/GCS calls happen.
        mock_mgr = MagicMock()
        mock_mgr.ensure_corpus.return_value = "projects/p/locations/l/ragCorpora/NEW"
        dept = _unique_dept("Compliance/Audit")
        expected_slug = slugify_department(dept)
        with patch("rubik_core.rag.RagManager", return_value=mock_mgr):
            resp = client.post(
                "/api/departments/",
                json={"department": dept},
                headers=_admin_session_headers(),
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "added successfully" in body["message"]
        assert body["rag"]["slug"] == expected_slug
        assert body["rag"]["rag_status"] == "ok"
        mock_mgr.ensure_corpus.assert_called_once_with(expected_slug)
    finally:
        settings.APP_MODE = prev_mode


def test_add_department_skips_rag_creation_outside_gcp_mode():
    UserRegistry().register_user(
        email="admin@example.com",
        name="Admin",
        id="a1",
        roles=["admin"],
        department="Sales",
    )
    app.dependency_overrides[get_current_user] = lambda: User(
        id="a1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        department="Sales",
    )

    prev_mode = settings.APP_MODE
    settings.APP_MODE = AppMode.LOCAL
    try:
        with patch("rubik_core.rag.RagManager") as RM:
            resp = client.post(
                "/api/departments/",
                json={"department": _unique_dept("Marketing")},
                headers=_admin_session_headers(),
            )
        assert resp.status_code == 200
        assert resp.json()["rag"]["rag_status"].startswith("skipped")
        RM.assert_not_called()
    finally:
        settings.APP_MODE = prev_mode


def test_add_department_does_not_roll_back_if_rag_creation_fails():
    """RAG corpus creation failing must NOT roll back the SQL insert. Admins
    can backfill via /api/rag/reconcile."""
    UserRegistry().register_user(
        email="admin@example.com",
        name="Admin",
        id="a1",
        roles=["admin"],
        department="Sales",
    )
    app.dependency_overrides[get_current_user] = lambda: User(
        id="a1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        department="Sales",
    )

    prev_mode = settings.APP_MODE
    settings.APP_MODE = AppMode.GCP
    try:
        mock_mgr = MagicMock()
        mock_mgr.ensure_corpus.side_effect = RuntimeError("vertex outage")
        with patch("rubik_core.rag.RagManager", return_value=mock_mgr):
            resp = client.post(
                "/api/departments/",
                json={"department": _unique_dept("Operations")},
                headers=_admin_session_headers(),
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "added successfully" in body["message"]
        assert body["rag"]["rag_status"].startswith("failed")
    finally:
        settings.APP_MODE = prev_mode


# ── department matching is case-insensitive (anti "Sales vs sales" hazard) ──


def test_add_department_rejects_case_or_slug_variant_of_existing():
    """ "Sales" and "sales" are the SAME team (same RAG slug), so the second
    insert must be rejected — otherwise the list accumulates case-variant
    duplicates that split file access on the exact-match ACL path."""
    from rubik_core.database.repository import SQLMetadataRegistry

    reg = SQLMetadataRegistry()
    label = _unique_dept("Sales")  # e.g. "Sales-1a2b3c4d"
    assert reg.add_department(label) is True
    # Differ only by case → slug-equivalent → rejected.
    assert reg.add_department(label.lower()) is False
    assert reg.add_department(label.upper()) is False
    # Only the single canonical row exists for that slug.
    same_slug = [
        d
        for d in reg.get_departments()
        if slugify_department(d) == slugify_department(label)
    ]
    assert same_slug == [label]


def test_has_document_access_department_match_is_case_insensitive():
    """A user assigned "sales" must still see a file tagged "Sales" on the
    legacy exact-match ACL path (named-document reads, DAM listing/detail)."""
    from datetime import datetime

    from rubik_core.auth.access import has_document_access
    from rubik_core.database.schemas import AssetMetadata, UserProfile

    now = datetime.utcnow()
    user = UserProfile(
        id="rep1",
        email="rep1@example.com",
        name="Rep One",
        department="sales",
        roles=["user"],
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    asset = AssetMetadata(
        doc_id="doc-sales-1",
        owner="someone.else@example.com",
        department="Sales",  # differs only by case
        topic="Playbook",
        is_company_wide=False,
        created_at=now,
        updated_at=now,
    )
    assert has_document_access(user, asset) is True


def test_has_document_access_still_denies_a_genuinely_different_department():
    """The case fix must NOT collapse two distinct departments into one."""
    from datetime import datetime

    from rubik_core.auth.access import has_document_access
    from rubik_core.database.schemas import AssetMetadata, UserProfile

    now = datetime.utcnow()
    user = UserProfile(
        id="rep2",
        email="rep2@example.com",
        name="Rep Two",
        department="sales",
        roles=["user"],
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    asset = AssetMetadata(
        doc_id="doc-finance-1",
        owner="someone.else@example.com",
        department="Finance",
        topic="Playbook",
        is_company_wide=False,
        created_at=now,
        updated_at=now,
    )
    assert has_document_access(user, asset) is False
