"""Drive-labels SignalSource (recent + importance-labeled Drive files).

Mocks the Drive client entirely (no live API, no DWD grant needed). Covers:
  - recent file with a mapped importance label → high-severity signal
  - file with an unmapped label → no signal
  - file whose only mapped label is the neutral 1.0 weight → no signal
  - severity derived from the catalog priority (high / medium)
  - feature-flag off → no signals
  - empty catalog → no signals
  - payload shape (file_name / label_values / web_view_link / modified_time)
  - extract_label_values: selection resolution, text fields, dict/list shapes
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.signals.drive import DriveSignalSource, _priority_to_severity
from rubik_core.config import settings
from rubik_core.tools.drive import extract_label_values

REP = "rep1@acme.example"
SINCE = datetime(2026, 6, 30, 0, 0, tzinfo=timezone.utc)

_MAP = {"important": 1.5, "priority": 1.5, "project": 1.2, "tracked": 1.0}


class _FakeDriveClient:
    def __init__(self, files):
        self._files = files

    def list_recent_labeled_files(self, *, since):  # noqa: ARG002
        return self._files


def _file(
    *,
    fid,
    name,
    labels,
    modified="2026-06-30T10:00:00Z",
    owner=REP,
    mime="application/vnd.google-apps.document",
):
    return {
        "id": fid,
        "name": name,
        "mime_type": mime,
        "web_view_link": f"https://drive.google.com/file/d/{fid}",
        "modified_time": modified,
        "owner": owner,
        "label_values": labels,
    }


@pytest.fixture(autouse=True)
def _flag_on():
    prev = settings.CUBIE_DRIVE_LABELS_SIGNAL_ENABLED
    settings.CUBIE_DRIVE_LABELS_SIGNAL_ENABLED = True
    yield
    settings.CUBIE_DRIVE_LABELS_SIGNAL_ENABLED = prev


def _fetch(files, label_map=_MAP):
    src = DriveSignalSource(_FakeDriveClient(files), label_map=label_map)
    return src.fetch(REP, since=SINCE)


def test_mapped_label_emits_high_severity_signal():
    sigs = _fetch([_file(fid="1", name="Phoenix Roadmap", labels=["Priority"])])
    assert len(sigs) == 1
    s = sigs[0]
    assert s.source == "drive"
    assert s.signal_type == "labeled_document_updated"
    assert s.severity_hint == "high"
    assert s.user_email == REP
    assert "Phoenix Roadmap" in s.summary
    assert s.entity_refs == {}  # orphan by default
    assert s.payload["file_name"] == "Phoenix Roadmap"
    assert s.payload["label_values"] == ["Priority"]
    assert s.payload["web_view_link"].endswith("/1")
    assert s.payload["priority"] == 1.5


def test_unmapped_label_emits_nothing():
    assert _fetch([_file(fid="2", name="Random Doc", labels=["Misc"])]) == []


def test_neutral_priority_label_is_skipped():
    # "tracked" is in the catalog but at the neutral 1.0 weight → not a signal.
    assert _fetch([_file(fid="3", name="Tracked Doc", labels=["Tracked"])]) == []


def test_medium_priority_maps_to_medium_severity():
    sigs = _fetch([_file(fid="4", name="Project Plan", labels=["Project"])])
    assert len(sigs) == 1
    assert sigs[0].severity_hint == "medium"


def test_best_priority_wins_when_multiple_labels():
    sigs = _fetch([_file(fid="5", name="Doc", labels=["Project", "Important"])])
    assert len(sigs) == 1
    assert sigs[0].severity_hint == "high"  # Important (1.5) beats Project (1.2)
    assert set(sigs[0].payload["label_values"]) == {"Project", "Important"}


def test_matching_is_case_insensitive():
    sigs = _fetch([_file(fid="6", name="Doc", labels=["iMPORTANT"])])
    assert len(sigs) == 1
    assert sigs[0].severity_hint == "high"


def test_feature_flag_off_emits_nothing():
    settings.CUBIE_DRIVE_LABELS_SIGNAL_ENABLED = False
    assert _fetch([_file(fid="7", name="Phoenix Doc", labels=["Priority"])]) == []


def test_empty_catalog_emits_nothing():
    assert (
        _fetch([_file(fid="8", name="Phoenix Doc", labels=["Priority"])], label_map={})
        == []
    )


# ─── content excerpt (top-N labeled files get their body read) ─────────────────

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _FakeDriveClientWithContent(_FakeDriveClient):
    """Fake that also serves file content, recording each read."""

    def __init__(self, files, content_by_id):
        super().__init__(files)
        self._content = content_by_id
        self.reads: list[tuple] = []

    def export_document_text(self, file_id, mime_type=None, *, max_chars=None):
        self.reads.append((file_id, mime_type, max_chars))
        return self._content.get(file_id)


@pytest.fixture
def _content_env():
    keys = (
        "CUBIE_DRIVE_LABEL_CONTENT_ENABLED",
        "CUBIE_DRIVE_LABEL_CONTENT_MAX_CHARS",
        "CUBIE_DRIVE_LABEL_CONTENT_MAX_FILES",
    )
    prev = {k: getattr(settings, k) for k in keys}
    settings.CUBIE_DRIVE_LABEL_CONTENT_ENABLED = True
    settings.CUBIE_DRIVE_LABEL_CONTENT_MAX_CHARS = 4000
    settings.CUBIE_DRIVE_LABEL_CONTENT_MAX_FILES = 7
    yield
    for k, v in prev.items():
        setattr(settings, k, v)


def _fetch_content(files, content, label_map=_MAP):
    client = _FakeDriveClientWithContent(files, content)
    sigs = DriveSignalSource(client, label_map=label_map).fetch(REP, since=SINCE)
    return sigs, client


def test_content_excerpt_is_read_and_folded_in(_content_env):
    sigs, client = _fetch_content(
        [
            _file(
                fid="c1",
                name="Vendor Audit Re-engagement",
                labels=["Important"],
                mime=_XLSX,
            )
        ],
        {"c1": "vendor audit re-engagement plan; auditor readiness Q3"},
    )
    assert len(sigs) == 1
    s = sigs[0]
    assert s.payload["content_excerpt"].startswith("vendor audit")
    assert "Content:" in s.summary
    assert "Vendor Audit Re-engagement" in s.summary
    # reader called once, with the file's mime + the configured char cap
    assert client.reads == [("c1", _XLSX, 4000)]


def test_content_flag_off_reads_nothing(_content_env):
    settings.CUBIE_DRIVE_LABEL_CONTENT_ENABLED = False
    sigs, client = _fetch_content(
        [_file(fid="c2", name="Phoenix Roadmap", labels=["Priority"])],
        {"c2": "secret content"},
    )
    assert len(sigs) == 1
    assert "content_excerpt" not in sigs[0].payload
    assert (
        sigs[0].summary
        == "Recently updated document 'Phoenix Roadmap' labeled Priority."
    )
    assert client.reads == []  # never attempted


def test_unsupported_content_falls_back_to_name_and_label(_content_env):
    # Reader returns None (unsupported type / no extractable text) → name+label.
    sigs, client = _fetch_content(
        [_file(fid="c3", name="Slide Deck", labels=["Project"])],
        {},  # no content registered for c3 → reader returns None
    )
    assert len(sigs) == 1
    assert "content_excerpt" not in sigs[0].payload
    assert sigs[0].summary == "Recently updated document 'Slide Deck' labeled Project."
    assert client.reads  # a read WAS attempted


def test_content_read_failure_degrades_gracefully(_content_env):
    class _Boom(_FakeDriveClientWithContent):
        def export_document_text(self, *a, **k):
            raise RuntimeError("boom")

    client = _Boom([_file(fid="c4", name="Doc", labels=["Important"])], {})
    sigs = DriveSignalSource(client, label_map=_MAP).fetch(REP, since=SINCE)
    assert len(sigs) == 1
    assert "content_excerpt" not in sigs[0].payload


def test_only_latest_n_by_modified_get_content(_content_env):
    settings.CUBIE_DRIVE_LABEL_CONTENT_MAX_FILES = 1
    sigs, client = _fetch_content(
        [
            # Older file carries the HIGHER-importance label…
            _file(
                fid="old",
                name="Old Doc",
                labels=["Important"],
                modified="2026-06-01T10:00:00Z",
            ),
            # …but the newer file is the one whose content gets read.
            _file(
                fid="new",
                name="New Doc",
                labels=["Project"],
                modified="2026-06-30T10:00:00Z",
            ),
        ],
        {"old": "old content", "new": "new content"},
    )
    # Both files still emit signals…
    assert len(sigs) == 2
    # …but only the single most-recently-modified file's content was read —
    # recency wins over importance for which files get deep-read.
    assert [r[0] for r in client.reads] == ["new"]
    by_id = {s.payload["file_id"]: s for s in sigs}
    assert by_id["new"].payload.get("content_excerpt") == "new content"
    assert "content_excerpt" not in by_id["old"].payload


def test_client_without_content_reader_is_safe(_content_env):
    # The plain fake lacks export_document_text → no crash, no excerpt.
    sigs = _fetch([_file(fid="c5", name="Phoenix Doc", labels=["Priority"])])
    assert len(sigs) == 1
    assert "content_excerpt" not in sigs[0].payload


def test_priority_to_severity_thresholds():
    assert _priority_to_severity(2.0) == "high"
    assert _priority_to_severity(1.4) == "high"
    assert _priority_to_severity(1.39) == "medium"
    assert _priority_to_severity(1.1) == "medium"
    assert _priority_to_severity(1.0) == "low"


# ─── extract_label_values (Drive labelInfo → human strings) ───────────────────


def test_extract_resolves_selection_via_choice_index():
    label_info = {
        "labels": [{"id": "L1", "fields": [{"id": "f1", "selection": ["choice_imp"]}]}]
    }
    assert extract_label_values(label_info, {"choice_imp": "Important"}) == [
        "Important"
    ]


def test_extract_reads_text_fields_directly():
    label_info = {"labels": [{"fields": [{"id": "f1", "text": ["Project Phoenix"]}]}]}
    assert extract_label_values(label_info) == ["Project Phoenix"]


def test_extract_handles_fields_as_dict_and_drops_unresolved_selection():
    label_info = {
        "labels": [
            {
                "fields": {
                    "f1": {"selection": ["unknown_choice"]},
                    "f2": {"text": ["Important"]},
                }
            }
        ]
    }
    assert extract_label_values(label_info, {}) == ["Important"]


def test_extract_empty_and_none_are_safe():
    assert extract_label_values(None) == []
    assert extract_label_values({}) == []
    assert extract_label_values({"labels": []}) == []
