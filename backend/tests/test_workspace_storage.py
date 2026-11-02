"""Stored object paths and reads stay inside the selected workspace."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rubik_core.chat_files import storage as chat_storage
from rubik_core.config import AppMode, settings
from rubik_core.smart_teams import uploads as smart_uploads
from rubik_core.tenancy.context import use_workspace
from rubik_core.tenancy.storage import (
    assert_own_uri,
    local_data_dir,
    object_name,
    safe_upload_name,
)


def test_safe_upload_name_normalizes_display_names_but_refuses_paths():
    assert safe_upload_name("report.pdf") == "report.pdf"
    assert safe_upload_name("Q3 report.pdf").endswith(".pdf")
    assert safe_upload_name("Q3 report.pdf") != safe_upload_name("Q3_report.pdf")
    for name in ("../report.pdf", "foo/bar.pdf", "foo\\bar.pdf", "%2e%2e.txt", ""):
        with pytest.raises(ValueError):
            safe_upload_name(name)


def test_corpus_ids_are_control_bound_per_workspace_and_audience(monkeypatch):
    from vertexai.preview import rag

    from rubik_core.control.db import control_session
    from rubik_core.control.models import Workspace
    from rubik_core.tenancy.corpora import (
        assert_own_rag_file,
        corpus_id,
        require_bound_corpus_id,
    )

    with control_session() as session:
        session.add_all(
            [
                Workspace(slug="alpha", name="Alpha", status="active"),
                Workspace(slug="beta", name="Beta", status="active"),
            ]
        )
        session.commit()

    created = []

    def fake_create(*, display_name):
        created.append(display_name)
        return SimpleNamespace(name=f"projects/p/locations/l/ragCorpora/{len(created)}")

    monkeypatch.setattr(rag, "create_corpus", fake_create)
    monkeypatch.setattr(rag, "list_corpora", lambda: pytest.fail("global scan"))
    monkeypatch.setattr(settings, "GCP_PROJECT_ID", "test-project")
    with use_workspace("alpha"):
        assert corpus_id("meetings") is None
        alpha_meetings = corpus_id("meetings", create=True)
        alpha_private = corpus_id("private_uploads", create=True)
        alpha_sales = corpus_id("rag", audience="sales", create=True)
        assert corpus_id("audience:sales") == alpha_sales
        assert corpus_id("meetings") == alpha_meetings
        assert_own_rag_file(f"{alpha_sales}/ragFiles/123", alpha_sales)
        with pytest.raises(ValueError):
            assert_own_rag_file(f"{alpha_private}/ragFiles/123", alpha_sales)
    with use_workspace("beta"):
        assert corpus_id("meetings") is None
        beta_meetings = corpus_id("meetings", create=True)
        beta_sales = corpus_id("rag", audience="sales", create=True)
        assert beta_meetings != alpha_meetings
        assert beta_sales != alpha_sales
        with pytest.raises(ValueError):
            require_bound_corpus_id(alpha_sales)
    assert created == [
        "alpha__meetings",
        "alpha__private_uploads",
        "alpha__sales_rag",
        "beta__meetings",
        "beta__sales_rag",
    ]


def test_provision_corpus_recovers_exact_resource_without_duplicate(monkeypatch):
    from vertexai.preview import rag

    from rubik_core.tenancy.corpora import (
        existing_corpus_resource,
        provision_corpus_resource,
    )

    monkeypatch.setattr(settings, "GCP_PROJECT_ID", "test-project")
    all_corpora = []
    monkeypatch.setattr(rag, "list_corpora", lambda: list(all_corpora))

    def create(*, display_name):
        result = SimpleNamespace(
            name="projects/p/locations/l/ragCorpora/42", display_name=display_name
        )
        all_corpora.append(result)
        return result

    monkeypatch.setattr(rag, "create_corpus", create)
    assert existing_corpus_resource("alpha", "meetings") is None
    first = provision_corpus_resource("alpha", "meetings")
    assert provision_corpus_resource("alpha", "meetings") == first
    assert len(all_corpora) == 1
    assert all_corpora[0].display_name == "alpha__meetings"


def test_rag_gcs_delete_refuses_foreign_uri_before_client_call(monkeypatch):
    from unittest.mock import MagicMock

    from rubik_core.rag.manager import RagManager

    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "shared-bucket")
    manager = RagManager.__new__(RagManager)
    manager.gcs_client = MagicMock()
    with use_workspace("alpha"):
        with pytest.raises(ValueError):
            manager._delete_from_gcs(
                "gs://shared-bucket/workspaces/beta/rags/sales/report.pdf"
            )
    manager.gcs_client.bucket.assert_not_called()


def test_vertex_delete_never_touches_object_without_matching_bound_rag_file(
    monkeypatch,
):
    from unittest.mock import MagicMock

    from rubik_core.adapters import vertex_rag

    resource = "projects/p/locations/l/ragCorpora/BOUND"
    target = "gs://shared-bucket/workspaces/alpha/rag/target.txt"
    other = "gs://shared-bucket/workspaces/alpha/rag/other.txt"
    adapter = vertex_rag.VertexRagAdapter.__new__(vertex_rag.VertexRagAdapter)
    adapter.corpus_id = resource
    adapter.gcs_client = MagicMock()
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "shared-bucket")
    monkeypatch.setattr(
        vertex_rag,
        "require_bound_corpus_id",
        lambda value: resource if value == resource else None,
    )
    monkeypatch.setattr(
        vertex_rag.rag,
        "list_files",
        lambda *, corpus_name: [
            SimpleNamespace(
                name=f"{resource}/ragFiles/other",
                gcs_source=SimpleNamespace(uris=[other]),
            )
        ],
    )
    delete_file = MagicMock()
    monkeypatch.setattr(vertex_rag.rag, "delete_file", delete_file)

    with use_workspace("alpha"):
        assert adapter.delete_document("target", source_path=target) is False
    adapter.gcs_client.bucket.assert_not_called()
    delete_file.assert_not_called()


def test_vertex_delete_removes_gcs_only_after_verified_rag_delete(monkeypatch):
    from unittest.mock import MagicMock

    from rubik_core.adapters import vertex_rag

    resource = "projects/p/locations/l/ragCorpora/BOUND"
    rag_file = f"{resource}/ragFiles/target"
    source = "gs://shared-bucket/workspaces/alpha/rag/target.txt"
    adapter = vertex_rag.VertexRagAdapter.__new__(vertex_rag.VertexRagAdapter)
    adapter.corpus_id = resource
    adapter.gcs_client = MagicMock()
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "shared-bucket")
    monkeypatch.setattr(vertex_rag, "require_bound_corpus_id", lambda _: resource)
    monkeypatch.setattr(vertex_rag, "assert_own_rag_file", lambda name, _: name)
    monkeypatch.setattr(vertex_rag, "source_is_published", lambda *_: False)
    monkeypatch.setattr(
        vertex_rag.rag,
        "list_files",
        lambda *, corpus_name: [
            SimpleNamespace(name=rag_file, gcs_source=SimpleNamespace(uris=[source]))
        ],
    )
    events = []
    blob = adapter.gcs_client.bucket.return_value.blob.return_value
    blob.exists.return_value = True
    blob.delete.side_effect = lambda: events.append("gcs")
    monkeypatch.setattr(
        vertex_rag.rag, "delete_file", lambda *, name: events.append("rag")
    )

    with use_workspace("alpha"):
        assert adapter.delete_document("target", source_path=source) is True
    assert events == ["rag", "gcs"]

    def fail_rag_delete(*, name):
        raise RuntimeError("Vertex unavailable")

    monkeypatch.setattr(vertex_rag.rag, "delete_file", fail_rag_delete)
    events.clear()
    adapter.gcs_client.reset_mock()
    with use_workspace("alpha"):
        assert adapter.delete_document("target", source_path=source) is False
    assert events == []
    adapter.gcs_client.bucket.assert_not_called()


def test_private_upload_rechecks_cached_control_binding_before_vertex(monkeypatch):
    from rubik_core.chat_files import private_index

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(settings, "CHAT_FILE_PRIVATE_INDEX_ENABLED", True)
    monkeypatch.setattr(settings, "CHAT_FILE_PRIVATE_CORPUS", "private-library")
    resource = "projects/p/locations/l/ragCorpora/OLD"
    binding = {"resource": resource, "active": True}
    uploads = []

    class FakeRag:
        def upload_file(self, **kwargs):
            uploads.append(kwargs)
            return SimpleNamespace(name=f"{kwargs['corpus_name']}/ragFiles/new")

    def lookup(kind, *, create=False, audience=None):
        assert (kind, audience) == ("private_uploads", None)
        if not binding["active"]:
            raise PermissionError("workspace suspended")
        return binding["resource"]

    monkeypatch.setattr(private_index, "_rag", FakeRag)
    monkeypatch.setattr("rubik_core.tenancy.corpora.corpus_id", lookup)
    private_index._corpus_names.clear()
    with use_workspace("alpha"):
        assert private_index.resolve_corpus() == resource
        binding["resource"] = None
        with pytest.raises(private_index.PrivateIndexError):
            private_index.add_document("one", "contents")
        binding["active"] = False
        with pytest.raises(private_index.PrivateIndexError):
            private_index.add_document("two", "contents")
    assert uploads == []
    assert "alpha" not in private_index._corpus_names


def test_chroma_persist_directory_is_per_workspace(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from rubik_core.adapters import chroma

    paths = []

    def client(*, path, settings):
        paths.append(path)
        return MagicMock()

    monkeypatch.setattr(settings, "CHROMA_PERSIST_DIR", str(tmp_path))
    monkeypatch.setattr(chroma.chromadb, "PersistentClient", client)
    with use_workspace("alpha"):
        chroma.ChromaAdapter(collection_name="test-path")
    with use_workspace("beta"):
        chroma.ChromaAdapter(collection_name="test-path")
    assert paths == [
        str(tmp_path / "workspaces" / "alpha"),
        str(tmp_path / "workspaces" / "beta"),
    ]


def test_dam_corpus_routing_uses_workspace_binding_without_global_scan(monkeypatch):
    from unittest.mock import MagicMock

    from vertexai.preview import rag

    from backend.routers.dam import FileProcessor

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(rag, "list_corpora", lambda: pytest.fail("global scan"))
    calls = []

    def lookup(kind, *, audience=None, create=False):
        calls.append((kind, audience, create))
        return f"bound-{kind}-{audience}"

    monkeypatch.setattr("rubik_core.tenancy.corpora.corpus_id", lookup)
    processor = FileProcessor(MagicMock())
    assert processor.get_or_create_corpus_id("companywide_rag") == (
        "bound-companywide_rag-None"
    )
    assert processor.get_or_create_corpus_id("sales_rag") == "bound-rag-sales"
    assert calls == [
        ("companywide_rag", None, True),
        ("rag", "sales", True),
    ]


def test_object_name_is_workspace_scoped_and_rejects_unsafe_segments():
    with use_workspace("alpha"):
        assert object_name("chat-uploads", "report.pdf") == (
            "workspaces/alpha/chat-uploads/report.pdf"
        )
        for segment in ("..", ".", "a/b", "a\\b", "/tmp/x", "%2e%2e", ""):
            with pytest.raises(ValueError):
                object_name("chat-uploads", segment)
    with use_workspace("beta"):
        assert object_name("chat-uploads", "report.pdf") == (
            "workspaces/beta/chat-uploads/report.pdf"
        )


def test_local_uri_rejects_foreign_symlink_traversal_and_bare_absolute(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    with use_workspace("alpha"):
        alpha = local_data_dir()
    with use_workspace("beta"):
        beta = local_data_dir()
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    (beta / "private.txt").write_text("beta")
    (alpha / "escape").symlink_to(beta, target_is_directory=True)

    with use_workspace("alpha"):
        assert assert_own_uri((alpha / "own.txt").as_uri()) == str(alpha / "own.txt")
        for uri in (
            (beta / "private.txt").as_uri(),
            (alpha / "escape" / "private.txt").as_uri(),
            f"file://{alpha}/../beta/private.txt",
            f"file://{alpha}/%2e%2e/beta/private.txt",
            str(alpha / "own.txt"),
        ):
            with pytest.raises(ValueError):
                assert_own_uri(uri)


def test_gcs_uri_requires_exact_bucket_workspace_and_safe_blob(monkeypatch):
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "shared-bucket")
    with use_workspace("alpha"):
        good = "gs://shared-bucket/workspaces/alpha/rags/report.pdf"
        assert assert_own_uri(good) == good
        for uri in (
            "gs://other/workspaces/alpha/rags/report.pdf",
            "gs://shared-bucket/workspaces/beta/rags/report.pdf",
            "gs://shared-bucket//workspaces/alpha/rags/report.pdf",
            "gs://shared-bucket/workspaces/alpha/../beta/report.pdf",
            "gs://shared-bucket/workspaces/alpha/rags/%2e%2e",
            "gs://shared-bucket/workspaces/alpha/rags/report.pdf?x=1",
        ):
            with pytest.raises(ValueError):
                assert_own_uri(uri)


def test_private_chat_bytes_are_isolated_and_foreign_uri_is_refused(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    with use_workspace("alpha"):
        alpha_uri = chat_storage.store_bytes(
            "same@example.com", "same-id", ".txt", b"alpha", "text/plain"
        )
    with use_workspace("beta"):
        beta_uri = chat_storage.store_bytes(
            "same@example.com", "same-id", ".txt", b"beta", "text/plain"
        )
        assert beta_uri != alpha_uri
        assert chat_storage.load_bytes(beta_uri) == b"beta"
        with pytest.raises(ValueError):
            chat_storage.load_bytes(alpha_uri)
        with pytest.raises(ValueError):
            chat_storage.delete_bytes(alpha_uri)
    with use_workspace("alpha"):
        assert chat_storage.load_bytes(alpha_uri) == b"alpha"
        assert chat_storage.delete_bytes(alpha_uri)


def test_smart_teams_reports_are_isolated_and_foreign_uri_is_refused(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    with use_workspace("alpha"):
        alpha_uri = smart_uploads.store_upload_bytes("same-id", "report.pdf", b"alpha")
    with use_workspace("beta"):
        beta_uri = smart_uploads.store_upload_bytes("same-id", "report.pdf", b"beta")
        assert beta_uri != alpha_uri
        assert smart_uploads.load_upload_bytes(beta_uri) == b"beta"
        with pytest.raises(ValueError):
            smart_uploads.load_upload_bytes(alpha_uri)
        with pytest.raises(ValueError):
            smart_uploads.delete_upload_bytes(alpha_uri)
    with use_workspace("alpha"):
        assert smart_uploads.load_upload_bytes(alpha_uri) == b"alpha"


def test_generated_docs_are_isolated_and_foreign_uri_is_refused(tmp_path, monkeypatch):
    from backend.services import generated_docs

    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    with use_workspace("alpha"):
        alpha_uri = generated_docs.store_doc_bytes("same-id", "brief.docx", b"alpha")
    with use_workspace("beta"):
        beta_uri = generated_docs.store_doc_bytes("same-id", "brief.docx", b"beta")
        assert beta_uri != alpha_uri
        assert generated_docs.load_doc_bytes(beta_uri) == b"beta"
        with pytest.raises(ValueError):
            generated_docs.load_doc_bytes(alpha_uri)
        with pytest.raises(ValueError):
            generated_docs.delete_doc_bytes(alpha_uri)
    with use_workspace("alpha"):
        assert generated_docs.load_doc_bytes(alpha_uri) == b"alpha"


def test_local_writers_reject_unsafe_ids_before_creating_files(tmp_path, monkeypatch):
    from backend.services import generated_docs

    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    with use_workspace("alpha"):
        for unsafe in ("../beta/escape", "nested/id", "..\\escape", "%2e%2e"):
            with pytest.raises(ValueError):
                chat_storage.store_bytes(
                    "owner@example.com", unsafe, ".txt", b"x", "text/plain"
                )
            with pytest.raises(ValueError):
                smart_uploads.store_upload_bytes(unsafe, "report.pdf", b"x")
            with pytest.raises(ValueError):
                generated_docs.store_doc_bytes(unsafe, "brief.docx", b"x")
    assert not (tmp_path / "workspaces").exists()
