"""GCS vector backfill pins one source generation and never leaks its contents."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rubik_core.ai_providers import cloud_sources

URI = "gs://bucket/workspaces/acme/rags/notes.txt"


def test_source_chunks_use_bound_uri_and_pinned_generation(monkeypatch):
    monkeypatch.setattr(
        cloud_sources, "assert_own_uri", lambda uri: uri == URI or pytest.fail()
    )
    payload = b"A useful client note about next steps. " * 20

    class Blob:
        generation = 17
        size = len(payload)

        def reload(self):
            pass

        def download_as_bytes(self, *, if_generation_match):
            assert if_generation_match == 17
            return payload

    client = SimpleNamespace(
        bucket=lambda name: SimpleNamespace(blob=lambda object_name: Blob())
    )
    result = cloud_sources.load_gcs_source(URI, client=client)
    assert result.uri == URI and result.generation == 17
    assert len(result.chunks) >= 1
    assert all(chunk.metadata["source_path"] == URI for chunk in result.chunks)
    assert all(
        chunk.metadata["original_filename"] == "notes.txt" for chunk in result.chunks
    )
    assert all(chunk.doc_id == result.chunks[0].doc_id for chunk in result.chunks)


def test_source_download_failure_is_safe(monkeypatch):
    monkeypatch.setattr(cloud_sources, "assert_own_uri", lambda _uri: None)

    class Blob:
        generation = 17
        size = 10

        def reload(self):
            pass

        def download_as_bytes(self, *, if_generation_match):
            raise RuntimeError("PRIVATE-SOURCE-CONTENT")

    client = SimpleNamespace(
        bucket=lambda name: SimpleNamespace(blob=lambda object_name: Blob())
    )
    with pytest.raises(cloud_sources.CloudSourceError) as error:
        cloud_sources.load_gcs_source(URI, client=client)
    assert "PRIVATE-SOURCE-CONTENT" not in str(error.value)
