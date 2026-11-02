"""#10 delete-then-import — re-ingesting an EOS doc must not leave stale chunks.

EOS docs carry deterministic filenames (eos__<kind>__<id>.md) but every Vertex
import creates a NEW ragFile, so before this fix an edited doc's pre-edit text
stayed retrievable forever. ingest_docs now lists the target corpus
once per run and deletes every pre-existing ragFile whose source basename
matches a doc just before importing its fresh copy. All Vertex surfaces are
stubbed — no network/GCP.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import rubik_core.adapters.vertex_rag as vertex_rag_module
from rubik_core.ingestion.eos import pipeline


def _doc(filename="eos__rock__1.md", text="fresh text"):
    # ingest_docs only reads .filename and .text.
    return SimpleNamespace(filename=filename, text=text)


def _ragfile(name: str, basename: str):
    return SimpleNamespace(
        name=name,
        gcs_source=SimpleNamespace(
            uris=[f"gs://bucket/workspaces/test/rag/{basename}"]
        ),
    )


class _Recorder:
    def __init__(self):
        self.deleted: list[str] = []
        self.uploaded: list[str] = []
        self.imported: list[str] = []
        self.listed_corpora: list[str] = []


@pytest.fixture()
def vertex(monkeypatch):
    """Stub the Vertex surfaces ingest_docs touches; return the recorder."""
    rec = _Recorder()
    from rubik_core.config import settings
    from rubik_core.control.db import control_session
    from rubik_core.control.models import Workspace

    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "bucket")
    default = "projects/p/locations/l/ragCorpora/default"
    vto = "projects/p/locations/l/ragCorpora/vto"
    with control_session() as session:
        workspace = session.get(Workspace, "test")
        previous = dict(workspace.corpus_ids or {})
        workspace.corpus_ids = {**previous, "default": default, "vto_rag": vto}
        session.commit()
    monkeypatch.setattr(pipeline, "_is_vertex_mode", lambda: True)
    monkeypatch.setattr(vertex_rag_module, "_init_vertexai", lambda: None)

    files = [
        _ragfile("ragFiles/OLD-1", "eos__rock__1.md"),
        _ragfile("ragFiles/OLD-2", "eos__rock__1.md"),  # accumulated duplicate
        _ragfile("ragFiles/KEEP", "eos__issue__9.md"),
    ]

    from vertexai.preview import rag

    def _list_files(corpus_name):
        rec.listed_corpora.append(corpus_name)
        prefix = "rags/vto" if corpus_name == vto else "rag"
        return [
            SimpleNamespace(
                name=f"{corpus_name}/{f.name}",
                gcs_source=SimpleNamespace(
                    uris=[
                        f"gs://bucket/workspaces/test/{prefix}/{f.gcs_source.uris[0].rsplit('/', 1)[-1]}"
                    ]
                ),
            )
            for f in files
        ]

    monkeypatch.setattr(rag, "list_files", _list_files)
    monkeypatch.setattr(rag, "delete_file", lambda name: rec.deleted.append(name))

    class StubAdapter:
        corpus_id = default

        def upload_to_gcs(self, local_path):
            rec.uploaded.append(local_path.name)
            return f"gs://bucket/workspaces/test/rag/{local_path.name}"

        def import_gcs_uri_to_corpus(self, gcs_uri):
            rec.imported.append(gcs_uri)

        def upload_and_import(self, local_path):
            uri = self.upload_to_gcs(local_path)
            self.import_gcs_uri_to_corpus(uri)
            return uri

    monkeypatch.setattr(vertex_rag_module, "VertexRagAdapter", StubAdapter)
    yield rec
    with control_session() as session:
        workspace = session.get(Workspace, "test")
        workspace.corpus_ids = previous
        session.commit()


def test_reingest_purges_all_stale_copies_then_imports(vertex):
    result = pipeline.ingest_docs([_doc("eos__rock__1.md")])
    assert result.succeeded == ["eos__rock__1.md"]
    # Both accumulated copies gone, the unrelated file untouched.
    assert vertex.deleted == [
        "projects/p/locations/l/ragCorpora/default/ragFiles/OLD-1",
        "projects/p/locations/l/ragCorpora/default/ragFiles/OLD-2",
    ]
    assert vertex.imported == ["gs://bucket/workspaces/test/rag/eos__rock__1.md"]
    # The corpus was listed exactly once for the whole run.
    assert vertex.listed_corpora == ["projects/p/locations/l/ragCorpora/default"]


def test_first_time_ingest_purges_nothing(vertex):
    result = pipeline.ingest_docs([_doc("eos__todo__5.md")])
    assert result.succeeded == ["eos__todo__5.md"]
    assert vertex.deleted == []
    assert vertex.imported == ["gs://bucket/workspaces/test/rag/eos__todo__5.md"]


def test_list_failure_never_blocks_ingestion(vertex, monkeypatch):
    from vertexai.preview import rag

    def _boom(corpus_name):
        raise RuntimeError("Vertex listing outage")

    monkeypatch.setattr(rag, "list_files", _boom)
    result = pipeline.ingest_docs([_doc("eos__rock__1.md")])
    assert result.succeeded == ["eos__rock__1.md"]
    assert vertex.deleted == []
    assert vertex.imported == ["gs://bucket/workspaces/test/rag/eos__rock__1.md"]


def test_delete_failure_never_blocks_ingestion(vertex, monkeypatch):
    from vertexai.preview import rag

    def _boom(name):
        raise RuntimeError("delete denied")

    monkeypatch.setattr(rag, "delete_file", _boom)
    result = pipeline.ingest_docs([_doc("eos__rock__1.md")])
    assert result.succeeded == ["eos__rock__1.md"]
    assert vertex.imported == ["gs://bucket/workspaces/test/rag/eos__rock__1.md"]


def test_slug_path_purges_the_resolved_slug_corpus(vertex, monkeypatch):
    """The VTO/exec path routes through RagManager — the purge must scan the
    SLUG corpus (resolved without creating), not the shared default."""
    import rubik_core.rag as rag_pkg

    class StubManager:
        def __init__(self):
            pass

        def _resolve_corpus(self, slug, *, create_if_missing=True):
            assert create_if_missing is False
            return f"projects/p/locations/l/ragCorpora/{slug}"

        def add_document(self, slug, local_path):
            vertex.imported.append(f"rags/{slug}/{local_path.name}")
            return {
                "gcs_uri": f"gs://bucket/workspaces/test/rags/{slug}/{local_path.name}"
            }

    monkeypatch.setattr(rag_pkg, "RagManager", StubManager)

    result = pipeline.ingest_docs([_doc("eos__rock__1.md")], target_rag_slug="vto")
    assert result.succeeded == ["eos__rock__1.md"]
    assert vertex.listed_corpora == ["projects/p/locations/l/ragCorpora/vto"]
    assert vertex.deleted == [
        "projects/p/locations/l/ragCorpora/vto/ragFiles/OLD-1",
        "projects/p/locations/l/ragCorpora/vto/ragFiles/OLD-2",
    ]


def test_slug_path_first_use_skips_purge(vertex, monkeypatch):
    """A corpus that doesn't exist yet has nothing to purge — resolution
    failure must not break the ingest (add_document creates the corpus)."""
    import rubik_core.rag as rag_pkg

    class StubManager:
        def __init__(self):
            pass

        def _resolve_corpus(self, slug, *, create_if_missing=True):
            raise RuntimeError("corpus does not exist")

        def add_document(self, slug, local_path):
            vertex.imported.append(f"rags/{slug}/{local_path.name}")
            return {"gcs_uri": f"gs://bucket/rags/{slug}/{local_path.name}"}

    monkeypatch.setattr(rag_pkg, "RagManager", StubManager)

    result = pipeline.ingest_docs([_doc("eos__rock__1.md")], target_rag_slug="vto")
    assert result.succeeded == ["eos__rock__1.md"]
    assert vertex.listed_corpora == []
    assert vertex.deleted == []
