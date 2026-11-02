"""The Cubie changelog seam What's New reads (spec §4.6.1).

``rubik_core/whats_new/changelog.py`` is the one place the reader learns what
was published. Phase 1 ships only :class:`EmptyChangelogSource`; Phase 3 adds
the control-database source behind the same :func:`get_changelog_source`.
Pinned here: the entry's shape and its checks, the empty default, and the
helpers the reader uses on an entry's link.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from rubik_core.whats_new.changelog import (
    ChangelogEntry,
    ChangelogSource,
    EmptyChangelogSource,
    app_path,
    get_changelog_source,
    is_entry_id,
)


def _entry(**over) -> ChangelogEntry:
    values = {
        "id": "rel-2026-10-27-a",
        "title": "Rocks can be reordered",
        "body": "Drag a Rock to move it up or down your list.",
        "feature_key": None,
        "path": "/eos/rocks",
        "published_at": datetime(2026, 10, 27, 14, 0),
    }
    values.update(over)
    return ChangelogEntry(**values)


def test_the_default_source_is_empty():
    source = get_changelog_source()
    assert isinstance(source, EmptyChangelogSource)
    assert source.list_published() == []


def test_the_empty_source_satisfies_the_protocol():
    source: ChangelogSource = EmptyChangelogSource()
    assert source.list_published() == []


def test_an_entry_is_frozen():
    entry = _entry()
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.title = "Something else"  # type: ignore[misc]


def test_an_entry_has_exactly_the_planned_fields():
    assert [f.name for f in dataclasses.fields(ChangelogEntry)] == [
        "id",
        "title",
        "body",
        "feature_key",
        "path",
        "published_at",
    ]


@pytest.mark.parametrize("bad", ["", " ", "a/b", "../x", "x" * 65, "-lead", "é"])
def test_an_entry_id_must_be_a_short_plain_token(bad):
    assert not is_entry_id(bad)
    with pytest.raises(ValueError):
        _entry(id=bad)


@pytest.mark.parametrize("good", ["1", "rel-2026-10-27", "a_b-C9", "x" * 64])
def test_plain_tokens_are_entry_ids(good):
    assert is_entry_id(good)
    assert _entry(id=good).id == good


def test_an_entry_needs_a_title():
    with pytest.raises(ValueError):
        _entry(title="   ")


def test_an_aware_publish_time_is_stored_as_naive_utc():
    aware = datetime(2026, 10, 27, 8, 0, tzinfo=timezone(timedelta(hours=-6)))
    assert _entry(published_at=aware).published_at == datetime(2026, 10, 27, 14, 0)


@pytest.mark.parametrize(
    ("path", "kept"),
    [
        ("/eos/rocks", "/eos/rocks"),
        ("/sales?tab=pipeline#top", "/sales?tab=pipeline#top"),
        ("/", "/"),
        (None, None),
        ("", None),
        ("//evil.example/x", None),
        ("https://evil.example", None),
        ("javascript:alert(1)", None),
        ("eos/rocks", None),
        ("/\\evil.example", None),
    ],
)
def test_only_a_path_inside_the_app_is_kept(path, kept):
    assert app_path(path) == kept
