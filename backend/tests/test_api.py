from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.auth import User

client = TestClient(app)


def mock_get_current_user():
    return User(
        id="test-user",
        email="test@acme.example",
        name="Test User",
        department="systems",  # non-sales: no CRM tool path
        roles=[],
    )


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # Build identity is stamped onto the revision at deploy time and falls
    # back to "unknown" off Cloud Run (see backend/main.py health_check).
    # session_backend reports the live ADK session store (RCA Part B2); off
    # Cloud Run (local/test) it is "in_memory:local".
    assert set(body) == {
        "status",
        "git_sha",
        "git_branch",
        "revision",
        "session_backend",
        # mock_auth surfaces the ENABLE_MOCK_AUTH flag so a smoke check can
        # assert the auth-bypass is off on the serving revision (SOC 2 H-2).
        "mock_auth",
        # migrations reports Alembic revision drift. A drifted database no
        # longer kills the container (a past incident) — it boots WITHOUT
        # migrating, so the drift has to be visible somewhere. Shape asserted in
        # test_health.py.
        "migrations",
        # Release controller stamps the two separately migrated schemas.
        "control_schema_revision",
        "workspace_schema_revision",
    }
    assert body["session_backend"] == "in_memory:local"


def test_chat_endpoint_structure():
    # The legacy chain path (get_rag_chain) was removed; the endpoint now
    # builds an ADK orchestrator via the _build_orchestrator factory.
    # Mock the factory so the endpoint runs without Vertex AI access and
    # check the response structure.
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_trace = MagicMock()
    mock_trace.model_dump.return_value = {}
    mock_trace.steps = []

    mock_result = MagicMock(
        answer="Test Answer",
        citations=[],
        hubspot_data=None,
        generated_sql=None,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        trace=mock_trace,
        confirmation=None,
    )

    mock_orchestrator = MagicMock()
    mock_orchestrator.run = AsyncMock(return_value=mock_result)

    with patch(
        "backend.routers.chat._build_orchestrator", return_value=mock_orchestrator
    ):
        response = client.post("/api/chat/", json={"query": "test"})
    assert response.status_code == 200
    assert response.json()["answer"] == "Test Answer"


def setup_module(module):
    app.dependency_overrides[get_current_user] = mock_get_current_user


def teardown_module(module):
    app.dependency_overrides.clear()
