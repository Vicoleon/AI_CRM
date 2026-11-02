from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.main import app
from rubik_core.database.db import SessionLocal, get_engine
from rubik_core.database.models import Base, Role, User


@pytest.fixture
def db_session():
    # Several suites drop every table on the shared engine when they finish, so
    # on a pytest-xdist worker this test can follow one of them: make sure the
    # tables it reads exist rather than relying on what ran before it.
    Base.metadata.create_all(bind=get_engine())
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def test_user_last_ingest_update(db_session: Session):
    client = TestClient(app)

    # Setup test user
    email = "test-ingest@acme.example"
    user = db_session.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email, id="test-id", name="Test User", department="internal")
        db_session.add(user)
        db_session.commit()

    # Ensure user has a role for Auth
    admin_role = db_session.query(Role).filter(Role.name == "admin").first()
    if not admin_role:
        admin_role = Role(name="admin")
        db_session.add(admin_role)
        db_session.commit()

    if admin_role not in user.roles:
        user.roles.append(admin_role)
        db_session.commit()

    from backend.auth import get_current_user
    from backend.models.auth import User as UserDTO

    def override_get_current_user():
        return UserDTO(
            id="test-id",
            email=email,
            name="Test User",
            department="internal",
            roles=["admin"],
        )

    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        # Initial check
        # We need to refresh user to ensure it's in the current session
        db_session.refresh(user)
        user.last_ingest = None
        db_session.commit()

        # The endpoint queues run_sync as a background task (TestClient runs it
        # synchronously). last_ingest is updated inside run_sync, so mock only
        # the Google boundary: no Admin SDK creds (falls back to local DB
        # users) and a Drive client that returns no transcripts.
        with (
            patch(
                "rubik_core.scripts.sync_meet_transcripts.WorkspaceService"
            ) as mock_ws_cls,
            patch(
                "rubik_core.scripts.sync_meet_transcripts.GoogleDriveClient"
            ) as mock_drive_cls,
            patch("rubik_core.scripts.sync_meet_transcripts.get_processor"),
            patch(
                "rubik_core.scripts.sync_meet_transcripts.corpus_id",
                return_value="test-meetings-corpus",
            ),
        ):
            mock_ws_cls.return_value._get_credentials.return_value = None
            mock_drive = mock_drive_cls.return_value
            mock_drive.service = MagicMock()
            mock_drive.get_meet_recordings_folder_id.return_value = None
            mock_drive.search_transcripts_by_pattern.return_value = []

            # Trigger ingestion
            response = client.post("/api/ingest/meet-transcripts")
            assert response.status_code == 200

            # Refresh and verify
            db_session.refresh(user)
            assert user.last_ingest is not None
            first_ingest = user.last_ingest

            # Trigger again and verify update
            import time

            time.sleep(1)  # Ensure timestamp changes
            response = client.post("/api/ingest/meet-transcripts")
            assert response.status_code == 200

            db_session.refresh(user)
            assert user.last_ingest > first_ingest
    finally:
        del app.dependency_overrides[get_current_user]
