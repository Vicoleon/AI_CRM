"""Tests for the /health endpoint's build-identity fields.

The deploy scripts stamp GIT_SHA / GIT_BRANCH onto the Cloud Run revision so
`curl $URL/health` reveals exactly which commit is live — Cloud Run revisions
otherwise carry no git identity, which made concurrent worktree deploys
indistinguishable.
"""

from fastapi.testclient import TestClient

from backend.main import app


def test_health_reports_build_identity(monkeypatch):
    """/health surfaces the deploy-time git identity + Cloud Run revision."""
    monkeypatch.setenv("GIT_SHA", "a3c30231f58f")
    monkeypatch.setenv("GIT_BRANCH", "main")
    monkeypatch.setenv("K_REVISION", "cubie-00390-nnl")

    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["git_sha"] == "a3c30231f58f"
    assert body["git_branch"] == "main"
    assert body["revision"] == "cubie-00390-nnl"


def test_health_defaults_to_unknown_without_env(monkeypatch):
    """Outside a stamped deploy (local run, bare image) the fields degrade to
    'unknown' rather than erroring — /health must always answer."""
    for var in ("GIT_SHA", "GIT_BRANCH", "K_REVISION"):
        monkeypatch.delenv(var, raising=False)

    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["git_sha"] == "unknown"
    assert body["git_branch"] == "unknown"
    assert body["revision"] == "unknown"


def test_health_reports_alembic_revision_drift(monkeypatch):
    """A drifted database no longer kills the container — start.sh boots it
    WITHOUT migrating. That trade is only safe if
    the drift is *visible*, so /health reports the markers start.sh exported."""
    monkeypatch.setenv("CUBIE_MIGRATIONS_SKIPPED", "1")
    monkeypatch.setenv("CUBIE_MIGRATION_DRIFT", "zc00000000040")
    monkeypatch.setenv("CUBIE_MIGRATION_DB_REVISION", "zc00000000040")
    monkeypatch.setenv("CUBIE_MIGRATION_IMAGE_HEAD", "zc00000000039")

    resp = TestClient(app).get("/health")
    # Still 200: uptime checks poll this endpoint, and a drift alert storm is not
    # the goal — the field is the signal.
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["migrations"] == {
        "skipped": True,
        "drift": "zc00000000040",
        "db_revision": "zc00000000040",
        "image_head": "zc00000000039",
        "stage": None,
    }


def test_health_migrations_block_is_quiet_on_a_healthy_boot(monkeypatch):
    """No markers = migrations ran normally. Nulls, not missing keys, so a
    post-deploy smoke check can assert on the shape unconditionally."""
    for var in (
        "CUBIE_MIGRATIONS_SKIPPED",
        "CUBIE_MIGRATION_DRIFT",
        "CUBIE_MIGRATION_DB_REVISION",
        "CUBIE_MIGRATION_IMAGE_HEAD",
        "CUBIE_MIGRATION_STAGE",
    ):
        monkeypatch.delenv(var, raising=False)

    body = TestClient(app).get("/health").json()
    assert body["migrations"] == {
        "skipped": False,
        "drift": None,
        "db_revision": None,
        "image_head": None,
        "stage": None,
    }


def test_health_reports_post_migration_revision_not_the_preflight_read(monkeypatch):
    """A deploy that carried a migration must report the revision it landed on.

    `start.sh` re-reads `alembic_version` after `alembic upgrade head` and flips
    the stage marker, because the boot preflight necessarily read the database
    BEFORE migrating. Without the re-read, a revision serves a ``db_revision``
    one step behind its ``image_head`` for the life of the container, which
    reads as a failed migration, while its own logs record the upgrade and
    success. `stage` is how a smoke check tells the refreshed value from the
    pre-migration one.
    """
    monkeypatch.setenv("CUBIE_MIGRATIONS_SKIPPED", "0")
    monkeypatch.delenv("CUBIE_MIGRATION_DRIFT", raising=False)
    monkeypatch.setenv("CUBIE_MIGRATION_DB_REVISION", "zc00000000049")
    monkeypatch.setenv("CUBIE_MIGRATION_IMAGE_HEAD", "zc00000000049")
    monkeypatch.setenv("CUBIE_MIGRATION_STAGE", "postmigrate")

    body = TestClient(app).get("/health").json()
    assert body["migrations"] == {
        "skipped": False,
        "drift": None,
        "db_revision": "zc00000000049",
        "image_head": "zc00000000049",
        "stage": "postmigrate",
    }


def test_public_health_reports_cached_platform_revisions_without_tenant_probe(
    monkeypatch,
):
    monkeypatch.setenv("CUBIE_CONTROL_SCHEMA_REVISION", "0001_cubie_control")
    monkeypatch.setenv("CUBIE_WORKSPACE_SCHEMA_REVISION", "0003_workspace")
    body = TestClient(app).get("/health").json()
    assert body["control_schema_revision"] == "0001_cubie_control"
    assert body["workspace_schema_revision"] == "0003_workspace"


def test_rag_diagnostic_requires_workspace_admin():
    response = TestClient(app).get("/api/health/rag")
    assert response.status_code in (401, 403)
    assert "sql_chunk_count" not in response.text
