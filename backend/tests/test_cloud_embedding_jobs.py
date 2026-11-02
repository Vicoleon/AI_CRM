"""A cloud index rebuild is a fenced workspace Job with visible progress."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from rubik_core.ai_providers import cloud_embedding_workflow as workflow
from rubik_core.ai_providers.cloud_vector_index import CloudVectorSpec
from rubik_core.ai_providers.embedding_index import cloud_corpus_key
from rubik_core.ai_providers.registry import IncompatibleModelRoute
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.tenancy.context import use_workspace

SLUG = "async-index-test"
RESOURCE = "projects/test/locations/us-central1/ragCorpora/async-index"


@pytest.fixture
def cloud_workspace(monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(workflow, "provider_connection_route", lambda *a, **k: object())
    with control_session() as db:
        db.add(
            Workspace(
                slug=SLUG,
                name="Async Index Test",
                status="active",
                corpus_ids={"default": RESOURCE},
            )
        )
        db.commit()
    try:
        with use_workspace(SLUG):
            yield
    finally:
        with control_session() as db:
            connector = db.get(WorkspaceConnector, (SLUG, "ai_embedding_indexes"))
            if connector is not None:
                db.delete(connector)
                db.flush()
            db.delete(db.get(Workspace, SLUG))
            db.commit()


def test_queue_runs_once_and_reports_status(cloud_workspace, monkeypatch):
    key = cloud_corpus_key(RESOURCE)
    queued = workflow.queue_cloud_embedding_rebuild("openai", "text-embedding-3-small")
    assert queued["status"] == "queued"
    assert queued["corpus"] == key
    with pytest.raises(IncompatibleModelRoute, match="already running"):
        workflow.queue_cloud_embedding_rebuild("openai", "text-embedding-3-small")

    observed = []

    def build(provider, model, *, corpus, run_id):
        observed.append((provider, model, corpus, run_id))
        status = workflow.cloud_embedding_status()
        assert status["build"]["status"] == "running"
        assert workflow._update_build(
            key, run_id, status="running", completed_sources=2, total_sources=3
        )
        return {
            "version": "1234567890abcdef",
            "indexed_chunks": 8,
            "indexed_sources": 3,
        }

    monkeypatch.setattr(workflow, "prepare_cloud_embedding_index", build)
    assert workflow.run_cloud_embedding_rebuild(queued["run_id"])
    assert observed == [("openai", "text-embedding-3-small", key, queued["run_id"])]
    status = workflow.cloud_embedding_status()
    assert status["build"]["status"] == "ready"
    assert status["build"]["completed_sources"] == 2
    assert status["build"]["total_sources"] == 3
    assert status["build"]["version"] == "1234567890abcdef"
    assert workflow.run_cloud_embedding_rebuild(queued["run_id"]) is False


def test_worker_backfill_updates_progress_before_candidate_is_ready(
    cloud_workspace, monkeypatch
):
    from rubik_core.adapters.base import Chunk
    from rubik_core.ai_providers import cloud_rag_bridge
    from rubik_core.ai_providers.cloud_sources import CloudSource
    from rubik_core.ai_providers.cloud_vector_index import IndexVerification

    uri = "gs://bucket/workspaces/async-index-test/rags/note.txt"
    source = CloudSource(
        uri,
        12,
        "a" * 64,
        [
            Chunk(
                chunk_id=f"chunk-{index}",
                doc_id="one",
                chunk_index=index,
                text="Useful note",
                metadata={"source_path": uri},
            )
            for index in range(33)
        ],
    )
    observed = []
    batches = []
    monkeypatch.setattr(workflow, "_source_uris", lambda _resource: {uri})
    monkeypatch.setattr(workflow, "load_gcs_source", lambda _uri: source)
    monkeypatch.setattr(workflow, "embed_connection_probe", lambda *_args: None)

    def embed(_route, texts, dimension):
        batches.append(len(texts))
        return [[0.1] * dimension for _ in texts]

    monkeypatch.setattr(workflow, "embed_with_route", embed)
    monkeypatch.setattr(
        workflow, "create_candidate", lambda spec: observed.append("created")
    )
    monkeypatch.setattr(
        workflow,
        "replace_source",
        lambda *_args: observed.append(
            workflow.cloud_embedding_status()["build"]["total_sources"]
        ),
    )
    monkeypatch.setattr(
        workflow,
        "verify_candidate",
        lambda *_args: IndexVerification(1, 33, "f" * 64),
    )
    monkeypatch.setattr(
        workflow, "build_ann_index", lambda *_args: observed.append("ann")
    )
    monkeypatch.setattr(cloud_rag_bridge, "reconcile_pending", lambda *_args: 0)

    queued = workflow.queue_cloud_embedding_rebuild("openai", "text-embedding-3-small")
    assert workflow.run_cloud_embedding_rebuild(queued["run_id"])
    assert observed == ["created", 1, "ann"]
    assert batches == [32, 1]
    status = workflow.cloud_embedding_status()
    assert status["candidate"]["version"] == status["build"]["version"]
    assert status["build"]["completed_sources"] == 1
    assert status["build"]["status"] == "ready"


def test_failed_and_replaced_runs_cannot_overwrite_retry(cloud_workspace, monkeypatch):
    first = workflow.queue_cloud_embedding_rebuild("openai", "text-embedding-3-small")
    workflow.fail_cloud_embedding_rebuild(first["corpus"], first["run_id"])
    second = workflow.queue_cloud_embedding_rebuild("openai", "text-embedding-3-small")
    assert workflow.run_cloud_embedding_rebuild(first["run_id"]) is False
    assert not workflow._update_build(first["corpus"], first["run_id"], status="ready")

    def fails(*_args, **_kwargs):
        raise RuntimeError("secret provider response")

    monkeypatch.setattr(workflow, "prepare_cloud_embedding_index", fails)
    assert workflow.run_cloud_embedding_rebuild(second["run_id"]) is False
    status = workflow.cloud_embedding_status()["build"]
    assert status["status"] == "failed"
    assert "secret" not in status["error"]


def test_failed_background_candidate_cannot_activate(cloud_workspace, monkeypatch):
    queued = workflow.queue_cloud_embedding_rebuild("openai", "text-embedding-3-small")
    workflow.fail_cloud_embedding_rebuild(queued["corpus"], queued["run_id"])
    spec = CloudVectorSpec(
        SLUG,
        queued["corpus"],
        "openai",
        "text-embedding-3-small",
        1536,
        "1234567890abcdef",
    )
    with control_session() as db:
        row = db.get(WorkspaceConnector, (SLUG, "ai_embedding_indexes"))
        config = dict(row.config)
        corpora = dict(config["corpora"])
        item = dict(corpora[queued["corpus"]])
        item["candidate"] = {"spec": asdict(spec)}
        corpora[queued["corpus"]] = item
        row.config = {**config, "corpora": corpora}
        db.commit()
    monkeypatch.setattr(workflow, "_snapshot", lambda _resource: {})
    monkeypatch.setattr(
        workflow,
        "verify_candidate",
        lambda *_args: pytest.fail("A failed build must not verify"),
    )
    with pytest.raises(IncompatibleModelRoute, match="not ready"):
        workflow.activate_cloud_embedding_index(spec.version)


def test_stalled_run_can_be_replaced_without_accepting_old_job(cloud_workspace):
    first = workflow.queue_cloud_embedding_rebuild("openai", "text-embedding-3-small")
    with control_session() as db:
        row = db.get(WorkspaceConnector, (SLUG, "ai_embedding_indexes"))
        config = dict(row.config)
        corpora = dict(config["corpora"])
        item = dict(corpora[first["corpus"]])
        build = dict(item["build"])
        build["updated_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=4)
        ).isoformat()
        item["build"] = build
        corpora[first["corpus"]] = item
        row.config = {**config, "corpora": corpora}
        db.commit()
    assert workflow.cloud_embedding_status()["build"]["status"] == "stalled"
    second = workflow.queue_cloud_embedding_rebuild("openai", "text-embedding-3-small")
    assert second["run_id"] != first["run_id"]
    assert workflow.run_cloud_embedding_rebuild(first["run_id"]) is False


def test_admin_dispatches_gcp_job_without_running_rebuild_in_request(monkeypatch):
    from backend.jobs import launcher
    from backend.routers import ai_providers

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    observed = []
    monkeypatch.setattr(launcher, "reject_support_background_work", lambda: None)
    monkeypatch.setattr(
        workflow,
        "queue_cloud_embedding_rebuild",
        lambda provider, model, *, corpus: (
            observed.append(("queue", provider, model, corpus))
            or {"status": "queued", "run_id": "a" * 32, "corpus": "c_test"}
        ),
    )
    monkeypatch.setattr(
        launcher,
        "launch_module_execution",
        lambda module, args: observed.append(("launch", module, args)) or {},
    )
    monkeypatch.setattr(ai_providers, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(
        ai_providers,
        "record_audit",
        lambda _db, **kwargs: observed.append(("audit", kwargs["action"])),
    )
    result = ai_providers.prepare_embedding_index_route(
        ai_providers.EmbeddingPrepareRequest(
            provider="openai", model="text-embedding-3-small"
        ),
        SimpleNamespace(email="admin@example.com"),
    )
    assert result["status"] == "queued"
    assert observed == [
        ("queue", "openai", "text-embedding-3-small", "default"),
        ("launch", "backend.jobs.cloud_embedding_rebuild_job", ["--run-id", "a" * 32]),
        ("audit", "WORKSPACE_EMBEDDING_INDEX_PREPARE"),
    ]


def test_admin_launch_failure_marks_run_failed(monkeypatch):
    from fastapi import HTTPException

    from backend.jobs import launcher
    from backend.routers import ai_providers

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    observed = []
    monkeypatch.setattr(launcher, "reject_support_background_work", lambda: None)
    monkeypatch.setattr(
        workflow,
        "queue_cloud_embedding_rebuild",
        lambda *_args, **_kwargs: {
            "status": "queued",
            "run_id": "b" * 32,
            "corpus": "c_test",
        },
    )
    monkeypatch.setattr(
        launcher,
        "launch_module_execution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private")),
    )
    monkeypatch.setattr(
        workflow,
        "fail_cloud_embedding_rebuild",
        lambda corpus, run_id: observed.append((corpus, run_id)),
    )
    with pytest.raises(HTTPException) as error:
        ai_providers.prepare_embedding_index_route(
            ai_providers.EmbeddingPrepareRequest(
                provider="openai", model="text-embedding-3-small"
            ),
            SimpleNamespace(email="admin@example.com"),
        )
    assert error.value.status_code == 503
    assert "private" not in error.value.detail
    assert observed == [("c_test", "b" * 32)]


def test_worker_refuses_to_run_without_registered_workspace(monkeypatch):
    from backend.jobs import bootstrap, cloud_embedding_rebuild_job

    monkeypatch.setattr(bootstrap, "bootstrap_job_workspace", lambda: False)
    assert cloud_embedding_rebuild_job.main(["--run-id", "a" * 32]) == 2
