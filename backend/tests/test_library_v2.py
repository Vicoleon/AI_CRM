"""V2 Library extensions — server-side filters + bookmark endpoints.

The list endpoint already shipped; V2 only adds optional query params.
Bookmark endpoints are net-new but ride the ``user_bookmark`` table that
shipped with [V2] Saved & Recent."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.models.role import ResourcePermissions, Role
from rubik_core.auth.registry import UserRegistry
from rubik_core.database.db import SessionLocal, engine
from rubik_core.database.models import Asset, Base, User, UserBookmark


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_schema():
    """Pytest is the only place we ever call ``create_all`` — prod uses
    Alembic. The fixture is idempotent so re-runs against an existing
    SQLite file just no-op."""
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(scope="module", autouse=True)
def _seed_user_role(_bootstrap_schema):
    """The asset endpoints gate on ``has_permission(user, "assets", "read")``,
    which resolves roles from the DB-backed UserRegistry — so the fake
    caller's ``roles=["user"]`` needs a real "user" Role row with assets.read.
    Same seeding pattern as ``test_assets_rbac.py``; ``add_role`` merges by
    role name, so re-running against an existing DB is idempotent."""
    UserRegistry().add_role(
        Role(
            name="user",
            permissions={
                "assets": ResourcePermissions(
                    create=False, read=True, update=False, delete=False
                )
            },
        )
    )


@pytest.fixture()
def seeded_user_and_assets():
    """Seed a user + three approved assets owned by that user. Returns
    ``(email, [doc_ids])`` so the test can act under the caller and
    reference the doc_ids without re-querying."""
    db = SessionLocal()
    email = f"lib-test-{uuid.uuid4().hex[:6]}@acme.example"
    try:
        if db.get(User, email) is None:
            db.add(
                User(
                    email=email,
                    id=email,
                    name="Lib Test",
                    department="sales",
                )
            )
        doc_ids = []
        for i, (filename, topic, company_wide) in enumerate(
            [
                ("alpha_notes.md", "Playbooks", False),
                ("beta_research.pdf", "Research", True),
                ("gamma_alpha_v2.pdf", "Playbooks", False),
            ]
        ):
            doc_id = f"lib-{uuid.uuid4().hex[:6]}-{i}"
            db.add(
                Asset(
                    doc_id=doc_id,
                    owner_email=email,
                    status="approved",
                    topic=topic,
                    department="sales",
                    original_filename=filename,
                    is_company_wide=company_wide,
                    allowed_emails=[],
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )
            doc_ids.append(doc_id)
        db.commit()
        yield email, doc_ids
        # Cleanup
        db.query(UserBookmark).filter(UserBookmark.actor_email == email).delete()
        for doc_id in doc_ids:
            db.query(Asset).filter(Asset.doc_id == doc_id).delete()
        db.query(User).filter(User.email == email).delete()
        db.commit()
    finally:
        db.close()


def _auth_client(email: str) -> TestClient:
    """Build a TestClient that bypasses Google OAuth via the mock-auth path.

    The repo's existing tests use the same pattern — see ``test_assets_rbac.py``.
    """
    from backend.auth import get_current_user
    from backend.models.auth import User as AuthUser

    def _override():
        # Mirror the AuthUser shape the real dependency hands back.
        return AuthUser(
            email=email,
            id=email,
            name="Lib Test",
            department="sales",
            roles=["user"],
            picture=None,
            workspace_groups=[],
            groups=[],
        )

    app.dependency_overrides[get_current_user] = _override
    client = TestClient(app)
    client._cleanup_overrides = lambda: app.dependency_overrides.pop(
        get_current_user, None
    )
    return client


# ─── Filter query params ─────────────────────────────────────────────────


def test_list_assets_accepts_q_filter(seeded_user_and_assets):
    """`q` matches substring on filename and topic, case-insensitive."""
    email, doc_ids = seeded_user_and_assets
    client = _auth_client(email)
    try:
        # "alpha" appears in alpha_notes.md AND in gamma_alpha_v2.pdf
        resp = client.get("/api/assets/", params={"q": "ALPHA"})
        assert resp.status_code == 200
        names = [v["original_filename"] for v in resp.json()["assets"].values()]
        assert any("alpha_notes" in n for n in names)
        assert any("gamma_alpha" in n for n in names)
        # "Research" topic shouldn't appear under a name-only query
        assert not any("beta_research" in n for n in names)
    finally:
        client._cleanup_overrides()


def test_list_assets_accepts_is_company_wide_filter(seeded_user_and_assets):
    """The V2 purple-pill filter narrows to company-wide-only rows."""
    email, doc_ids = seeded_user_and_assets
    client = _auth_client(email)
    try:
        resp = client.get("/api/assets/", params={"is_company_wide": "true"})
        assert resp.status_code == 200
        rows = resp.json()["assets"]
        # Only beta_research is company-wide in the seed.
        for v in rows.values():
            assert v["is_company_wide"] is True
    finally:
        client._cleanup_overrides()


def test_list_assets_blank_q_does_not_zero_results(seeded_user_and_assets):
    """V2 frontend can pass ``q=""`` while typing — must equal no filter."""
    email, doc_ids = seeded_user_and_assets
    client = _auth_client(email)
    try:
        resp_blank = client.get("/api/assets/", params={"q": ""})
        resp_none = client.get("/api/assets/")
        assert resp_blank.status_code == 200
        assert resp_none.status_code == 200
        # Both should at least contain the user's three seeded rows
        # (counts may be larger because the table is shared across tests).
        assert len(resp_blank.json()["assets"]) == len(resp_none.json()["assets"])
    finally:
        client._cleanup_overrides()


def test_list_assets_response_exposes_is_company_wide(seeded_user_and_assets):
    """V2 reads `a.is_company_wide` to render the purple pill — confirm
    the response surface still carries the bool."""
    email, doc_ids = seeded_user_and_assets
    client = _auth_client(email)
    try:
        resp = client.get("/api/assets/")
        assert resp.status_code == 200
        for v in resp.json()["assets"].values():
            assert "is_company_wide" in v
            assert isinstance(v["is_company_wide"], bool)
    finally:
        client._cleanup_overrides()


def test_list_assets_excludes_meeting_transcripts(seeded_user_and_assets):
    """Meeting transcripts have their own tab (GET /api/transcripts/) and carry
    no real department, so they must never appear in the Documents list — even
    for their owner (owner-bypass in has_document_access would otherwise let
    them through, so their absence here proves the exclusion filter works)."""
    email, doc_ids = seeded_user_and_assets
    transcript_id = f"lib-transcript-{uuid.uuid4().hex[:6]}"
    db = SessionLocal()
    try:
        db.add(
            Asset(
                doc_id=transcript_id,
                owner_email=email,
                status="approved",
                topic="Meeting Transcripts",
                department=None,
                original_filename="Weekly 1:1 - Notes by Gemini.txt",
                is_company_wide=False,
                allowed_emails=[email],
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()

    client = _auth_client(email)
    try:
        resp = client.get("/api/assets/", params={"limit": 100})
        assert resp.status_code == 200
        returned = set(resp.json()["assets"].keys())
        # The transcript is filtered out…
        assert transcript_id not in returned
        # …while ordinary uploaded documents still come back.
        assert any(d in returned for d in doc_ids)
    finally:
        client._cleanup_overrides()
        db = SessionLocal()
        try:
            db.query(Asset).filter(Asset.doc_id == transcript_id).delete()
            db.commit()
        finally:
            db.close()


# ─── Bookmark endpoints ──────────────────────────────────────────────────


def test_bookmark_toggle_round_trip(seeded_user_and_assets):
    """POST /bookmark adds, second POST removes, returns the new state."""
    email, doc_ids = seeded_user_and_assets
    target = doc_ids[0]
    client = _auth_client(email)
    try:
        resp1 = client.post(f"/api/assets/{target}/bookmark")
        assert resp1.status_code == 200
        assert resp1.json() == {"doc_id": target, "bookmarked": True}

        resp2 = client.post(f"/api/assets/{target}/bookmark")
        assert resp2.status_code == 200
        assert resp2.json() == {"doc_id": target, "bookmarked": False}
    finally:
        client._cleanup_overrides()


def test_bookmarks_list_returns_only_my_bookmarks(seeded_user_and_assets):
    """`GET /api/assets/bookmarks` is scoped to the caller."""
    email, doc_ids = seeded_user_and_assets
    target = doc_ids[1]
    client = _auth_client(email)
    try:
        client.post(f"/api/assets/{target}/bookmark")
        resp = client.get("/api/assets/bookmarks")
        assert resp.status_code == 200
        rows = resp.json()
        assert any(r["doc_id"] == target for r in rows)
        # Bookmark count for THIS user matches the bookmarks we just made
        # (one). Doesn't matter if other tests left rows for other users.
        my_count = sum(1 for r in rows if r["doc_id"] in doc_ids)
        assert my_count == 1
    finally:
        client._cleanup_overrides()


def test_bookmark_missing_asset_returns_404(seeded_user_and_assets):
    """Trying to bookmark a non-existent asset is a 404, not 500."""
    email, _ = seeded_user_and_assets
    client = _auth_client(email)
    try:
        resp = client.post(f"/api/assets/never-existed-{uuid.uuid4().hex}/bookmark")
        assert resp.status_code == 404
    finally:
        client._cleanup_overrides()
