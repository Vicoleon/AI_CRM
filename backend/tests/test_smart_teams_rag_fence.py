"""The guide RAG ingest may only run inside Cloud Run.

This file exists because the absence of this fence did real damage. The wave-2
integration test called ``approve`` and drained the worker, which really ran the
guide job, whose ``ingest_published_guide`` defaulted to the live ``RagManager``
with no guard at all. A workstation run therefore dialled Vertex and wrote
synthetic guides for its own test addresses into the LIVE companywide corpus in
the production project — thirteen real client constructions per run — and the
suite reported green the whole time, because the job catches the exception. Two
fake guides ended up retrievable in Chat by every employee, rewritten on each run.

**The discriminator is the runtime, not APP_MODE.** The first version of this
fence keyed on ``settings.APP_MODE != GCP`` and protected nothing, because
APP_MODE resolves to GCP on a developer workstation here. That was found by
executing the fence rather than reasoning about it, which is the only reason it
is not still in the tree. Local runs also resolve to the deployed dev database,
and dev is production for this app, so "it is only local" was never a defence
either.

``running_in_managed_runtime`` accepts Cloud Run Job markers only. A serving
revision or laptop cannot write the shared guide corpus.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from rubik_core.database.smart_teams import SmartTeamsDocument
from rubik_core.smart_teams import guide

SUBJECT = "subject@acme.example"


def _published_doc() -> SmartTeamsDocument:
    stamp = datetime(2026, 8, 20, 9, 0)
    return SmartTeamsDocument(
        id="fence-test",
        kind="personal_guide",
        subject_emails=[SUBJECT],
        subject_key=SUBJECT,
        profile_versions={SUBJECT: 1},
        profile_version_key=f"{SUBJECT}:1",
        status="ready",
        content_md="# A guide\n\nSome body.\n",
        published=True,
        approved_at=stamp,
        rag_ingested=False,
        created_at=stamp,
        updated_at=stamp,
    )


class _ExplodingManager:
    """Stands in for RagManager and fails if anything constructs it."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "RagManager was constructed outside Cloud Run — the fence is open and "
            "this run is about to write to the live companywide corpus"
        )


def test_a_workstation_cannot_reach_the_corpus(monkeypatch):
    """No Cloud Run markers means refuse, before any client is built."""
    for marker in ("K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION"):
        monkeypatch.delenv(marker, raising=False)

    with patch("rubik_core.rag.manager.RagManager", _ExplodingManager):
        with pytest.raises(guide.GuideError, match="outside the Cloud Run worker Job"):
            guide.ingest_published_guide(None, _published_doc(), display_name="Subject")


@pytest.mark.parametrize(
    "marker,value",
    [("CLOUD_RUN_JOB", "cubie-worker"), ("CLOUD_RUN_EXECUTION", "job-run")],
)
def test_the_managed_runtime_is_still_allowed_through(monkeypatch, marker, value):
    """The fence must not break the worker Job path it is protecting.

    A guard that refuses everywhere would look like a pass here and silently stop
    every published guide from ever reaching the corpus in production. Asserting
    that the client IS constructed under a Cloud Run marker is what distinguishes
    a fence from a wall.
    """
    for name in ("K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(marker, value)

    with patch("rubik_core.rag.manager.RagManager", _ExplodingManager):
        with pytest.raises(AssertionError, match="fence is open"):
            guide.ingest_published_guide(None, _published_doc(), display_name="Subject")


def test_a_serving_revision_cannot_write_the_guide_corpus(monkeypatch):
    for name in ("K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("K_SERVICE", "cubie")
    with patch("rubik_core.rag.manager.RagManager", _ExplodingManager):
        with pytest.raises(guide.GuideError, match="outside the Cloud Run worker Job"):
            guide.ingest_published_guide(None, _published_doc(), display_name="Subject")


def test_an_explicit_manager_factory_still_works_anywhere(monkeypatch):
    """Tests exercise the ingest by passing a factory, not by lifting the fence."""
    for name in ("K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION"):
        monkeypatch.delenv(name, raising=False)

    recorded: list[str] = []

    class _Recorder:
        def update_document(self, slug, path):
            recorded.append(slug)
            return {"gcs_uri": f"gs://fake/{path.name}"}

    document = _published_doc()
    with patch("rubik_core.rag.manager.RagManager", _ExplodingManager):
        guide.ingest_published_guide(
            None,
            document,
            display_name="Subject",
            manager_factory=_Recorder,
        )
    assert recorded, "the injected factory should have received the write"
    assert document.rag_ingested is True
