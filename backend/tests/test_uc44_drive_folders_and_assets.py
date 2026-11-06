"""UC-44 — folder picker + add-to-assets for the Productivity Documents tab."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, User, WorkspaceSetting

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


def mock_get_current_user():
    return UserProfile(
        id="user-1",
        email="me@acme.example",
        name="Me",
        department="sales",
        roles=["admin"],  # admin so `assets:create` succeeds in tests
        created_at="2026-06-04T10:00:00Z",
        updated_at="2026-06-04T10:00:00Z",
    )


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        # The add-to-assets endpoint runs an `assets:create` permission check
        # against the actual User row. Seed one so the test user can ingest.
        db.add(
            User(
                email="me@acme.example",
                id="user-1",
                name="Me",
                department="sales",
            )
        )
        db.commit()
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def override_deps():
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


# ── /api/drive/folders ───────────────────────────────────────────────────────


@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_uc44_folders_search(mock_client_cls):
    """Search returns the matching folders."""
    mock_client = MagicMock()
    mock_client.service = True
    mock_client.list_folders.return_value = [
        {
            "id": "f-deals",
            "name": "Sales / Deals",
            "webViewLink": "https://drive.google.com/drive/folders/f-deals",
        },
        {"id": "f-customer-research", "name": "Customer research"},
    ]
    mock_client_cls.return_value = mock_client

    client = TestClient(app)
    response = client.get("/api/drive/folders?q=sales&max_results=10")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 2
    assert items[0]["id"] == "f-deals"
    assert items[0]["name"] == "Sales / Deals"
    assert items[0]["web_view_link"].endswith("/f-deals")
    assert items[1]["web_view_link"] is None
    # `list_folders` now also takes the root_only flag — confirm it's
    # passed through. The router currently leaves it at the default.
    mock_client.list_folders.assert_called_once()
    kwargs = mock_client.list_folders.call_args.kwargs
    assert kwargs.get("query") == "sales"
    assert kwargs.get("max_results") == 10


@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_uc44_folders_empty_query_still_works(mock_client_cls):
    """Empty `q` → list_folders called with empty string (returns recent folders)."""
    mock_client = MagicMock()
    mock_client.service = True
    mock_client.list_folders.return_value = []
    mock_client_cls.return_value = mock_client

    client = TestClient(app)
    response = client.get("/api/drive/folders")
    assert response.status_code == 200
    mock_client.list_folders.assert_called_once()
    kwargs = mock_client.list_folders.call_args.kwargs
    assert kwargs.get("query") == ""
    assert kwargs.get("max_results") == 50


# ── /api/drive/preferences  GET / PUT ────────────────────────────────────────


def test_uc44_preferences_default_empty():
    """No saved prefs → empty folder list."""
    client = TestClient(app)
    response = client.get("/api/drive/preferences")
    assert response.status_code == 200
    assert response.json() == {"folders": []}


def test_uc44_preferences_round_trip():
    """PUT saves the folder selection, GET returns it."""
    client = TestClient(app)
    payload = {
        "folders": [
            {"id": "f-a", "name": "Folder A", "web_view_link": "https://x/f-a"},
            {"id": "f-b", "name": "Folder B"},
        ]
    }
    put_resp = client.put("/api/drive/preferences", json=payload)
    assert put_resp.status_code == 200, put_resp.text
    assert len(put_resp.json()["folders"]) == 2

    get_resp = client.get("/api/drive/preferences")
    assert get_resp.status_code == 200
    folders = get_resp.json()["folders"]
    assert {f["id"] for f in folders} == {"f-a", "f-b"}

    # And the underlying WorkspaceSetting row exists with the expected key.
    db = TestingSessionLocal()
    try:
        row = (
            db.query(WorkspaceSetting)
            .filter(WorkspaceSetting.key == "user:me@acme.example:drive_folder_ids")
            .one()
        )
        assert isinstance(row.value, dict)
        assert len(row.value["folders"]) == 2
    finally:
        db.close()


def test_uc44_preferences_overwrites():
    """Saving twice → second save wins (no append)."""
    client = TestClient(app)
    client.put(
        "/api/drive/preferences",
        json={"folders": [{"id": "f-a", "name": "A"}, {"id": "f-b", "name": "B"}]},
    )
    client.put(
        "/api/drive/preferences",
        json={"folders": [{"id": "f-c", "name": "C"}]},
    )
    response = client.get("/api/drive/preferences")
    folders = response.json()["folders"]
    assert {f["id"] for f in folders} == {"f-c"}


# ── /api/drive/recent-activity gets scoped to the saved folders ──────────────


@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_uc44_recent_activity_uses_folder_filter_when_set(mock_client_cls):
    """If the user has saved folders → list_files_in_folders is called, not
    list_recent_files."""
    mock_client = MagicMock()
    mock_client.service = True
    mock_client.list_files_in_folders.return_value = [
        {
            "id": "f-1",
            "name": "Doc inside picked folder.gdoc",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-06-04T10:00:00Z",
            "owners": [{"emailAddress": "me@acme.example"}],
            "shared": False,
        },
    ]
    mock_client_cls.return_value = mock_client

    client = TestClient(app)
    # Save a folder first.
    client.put(
        "/api/drive/preferences",
        json={"folders": [{"id": "f-deals", "name": "Sales / Deals"}]},
    )
    # Now hit the activity feed.
    response = client.get("/api/drive/recent-activity")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    mock_client.list_files_in_folders.assert_called_once()
    args, kwargs = mock_client.list_files_in_folders.call_args
    folder_ids_arg = args[0] if args else kwargs.get("folder_ids")
    assert folder_ids_arg == ["f-deals"]
    # When folders are set we do NOT fall back to the all-of-Drive helper.
    mock_client.list_recent_files.assert_not_called()


@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_uc44_recent_activity_falls_back_to_all_drive_when_no_prefs(mock_client_cls):
    """No prefs → existing UC-43 behavior (list_recent_files across all Drive)."""
    mock_client = MagicMock()
    mock_client.service = True
    mock_client.list_recent_files.return_value = []
    mock_client_cls.return_value = mock_client

    client = TestClient(app)
    response = client.get("/api/drive/recent-activity")
    assert response.status_code == 200
    mock_client.list_recent_files.assert_called_once()
    mock_client.list_files_in_folders.assert_not_called()


# ── /api/drive/{file_id}/add-to-assets ───────────────────────────────────────


@patch("backend.routers.drive.dam_registry")
@patch("backend.routers.ingest.get_processor")
@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_uc44_add_to_assets_happy_path(
    mock_client_cls,
    mock_get_processor,
    mock_registry,
):
    """Drive download → processor.process_file succeeds → 200 with doc_id."""
    mock_client = MagicMock()
    # `service` must be a chained MagicMock (the route calls
    # `drive.service.files().get(...).execute()`), not a bare bool.
    mock_client.service.files.return_value.get.return_value.execute.return_value = {
        "id": "drive-file-123",
        "name": "Customer Pricing Memo",
        "mimeType": "application/vnd.google-apps.document",
    }
    mock_client.download_file_bytes.return_value = b"fake-docx-bytes"
    mock_client.get_export_extension.return_value = ".docx"
    mock_client_cls.return_value = mock_client

    fake_processor = MagicMock()
    fake_processor.loader.generate_doc_id.return_value = "doc-abc"
    fake_result = MagicMock()
    fake_result.success = True
    fake_processor.process_file.return_value = {"result": fake_result}
    mock_get_processor.return_value = fake_processor

    client = TestClient(app)
    response = client.post(
        "/api/drive/drive-file-123/add-to-assets",
        json={
            "topic": "Pricing",
            "department": "sales",
            "is_company_wide": False,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["doc_id"] == "doc-abc"
    assert body["status"] == "ingested"
    assert body["original_filename"].endswith(".docx")

    # Asset registered + processor invoked with the right metadata.
    mock_registry.register_asset.assert_called_once()
    asset_arg = mock_registry.register_asset.call_args.args[0]
    assert asset_arg.topic == "Pricing"
    assert asset_arg.department == "sales"
    assert asset_arg.owner == "me@acme.example"

    fake_processor.process_file.assert_called_once()
    kwargs = fake_processor.process_file.call_args.kwargs
    assert kwargs["manual_metadata"]["topic"] == "Pricing"
    assert kwargs["manual_metadata"]["source"] == "drive"
    assert kwargs["manual_metadata"]["drive_file_id"] == "drive-file-123"


@patch("backend.routers.drive.dam_registry")
@patch("backend.routers.ingest.get_processor")
@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_uc44_add_to_assets_sanitizes_slashes_in_drive_name(
    mock_client_cls,
    mock_get_processor,
    mock_registry,
):
    """Drive names with "/" (e.g. a Gemini-notes date) must not be treated as
    directory separators on write — that produced a bare 500 (FileNotFoundError)."""
    mock_client = MagicMock()
    mock_client.service.files.return_value.get.return_value.execute.return_value = {
        "id": "drive-file-456",
        # The exact failing case: forward slashes from the date.
        "name": "L10 - 2026/06/24 17:26 EDT - Notes by Gemini",
        "mimeType": "application/vnd.google-apps.document",
    }
    mock_client.download_file_bytes.return_value = b"fake-docx-bytes"
    mock_client.get_export_extension.return_value = ".docx"
    mock_client_cls.return_value = mock_client

    fake_processor = MagicMock()
    fake_processor.loader.generate_doc_id.return_value = "doc-l10"
    fake_result = MagicMock()
    fake_result.success = True
    fake_processor.process_file.return_value = {"result": fake_result}
    mock_get_processor.return_value = fake_processor

    client = TestClient(app)
    response = client.post(
        "/api/drive/drive-file-456/add-to-assets",
        json={"topic": "Meeting Notes", "department": "systems"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["doc_id"] == "doc-l10"
    # The slashes were collapsed — the stored filename is one path component.
    assert "/" not in body["original_filename"]
    assert body["original_filename"].endswith(".docx")


@patch("backend.routers.ingest.get_processor")
@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_uc44_add_to_assets_502_when_download_returns_no_bytes(
    mock_client_cls,
    mock_get_processor,
):
    """Drive returns no bytes (unsupported mime / error) → 502, no ingestion attempted."""
    mock_client = MagicMock()
    mock_client.service.files.return_value.get.return_value.execute.return_value = {
        "id": "x",
        "name": "Mystery File",
        "mimeType": "application/x-weird",
    }
    mock_client.download_file_bytes.return_value = None
    mock_client_cls.return_value = mock_client

    client = TestClient(app)
    response = client.post(
        "/api/drive/x/add-to-assets",
        json={"topic": "t", "department": "sales"},
    )
    assert response.status_code == 502
    mock_get_processor.assert_not_called()
