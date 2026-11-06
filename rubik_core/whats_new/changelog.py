"""The Cubie changelog: the release notes What's New reads (spec §4.6.1).

RubikSoft writes each release note once, in the Platform area, and every
workspace reads it. This module is the seam between that note and the
reader: the drawer, its button, the spotlight bar and the morning post only
ever see :class:`ChangelogEntry` values, from whatever
:func:`get_changelog_source` returns.

Phase 1 has no control database yet, so the source is
:class:`EmptyChangelogSource` and the drawer says "No updates yet." Phase 3
adds the control-database source behind the same function; nothing that
reads an entry changes.

A source returns only notes that are published, in any order. Who may see
one (its ``feature_key``), when it may show (``published_at`` not in the
future) and whether its link stays inside the app (:func:`app_path`) are the
reader's checks, made on every read, so a source cannot widen them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

#: An entry id: a short plain token, safe in a URL path and a Slack link.
_ENTRY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def is_entry_id(value: object) -> bool:
    """True for a string a :class:`ChangelogEntry` may use as its ``id``."""
    return isinstance(value, str) and _ENTRY_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class ChangelogEntry:
    """One published release note.

    ``feature_key`` names the registry feature the note is about (None: it is
    for everyone who has What's New). ``path`` is the page "Try it" opens,
    inside the app. ``published_at`` is naive UTC, the house shape; an aware
    value is converted.
    """

    id: str
    title: str
    body: str
    feature_key: str | None
    path: str | None
    published_at: datetime

    def __post_init__(self) -> None:
        if not is_entry_id(self.id):
            raise ValueError(f"Changelog entry id {self.id!r} is not a plain token.")
        if not (self.title or "").strip():
            raise ValueError(f"Changelog entry {self.id} has no title.")
        when = self.published_at
        if when.tzinfo is not None:
            naive = when.astimezone(timezone.utc).replace(tzinfo=None)
            object.__setattr__(self, "published_at", naive)


class ChangelogSource(Protocol):
    """Where published release notes come from."""

    def list_published(self) -> list[ChangelogEntry]: ...


class EmptyChangelogSource:
    """No release notes: Phase 1, before the control database exists."""

    def list_published(self) -> list[ChangelogEntry]:
        return []


_EMPTY = EmptyChangelogSource()


def get_changelog_source() -> ChangelogSource:
    """The source every reader uses. Also the FastAPI dependency of the What's
    New router, so a test swaps it through ``app.dependency_overrides``."""
    return _EMPTY


def app_path(path: str | None) -> str | None:
    """``path`` when it is a page inside the app ("/eos/rocks", with any query
    or fragment), else None: a full address, a scheme, a protocol-relative
    ``//host`` or a backslash trick never becomes a "Try it" link."""
    if not isinstance(path, str):
        return None
    path = path.strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return None
    if any(ch.isspace() or ord(ch) < 32 for ch in path):
        return None
    return path
