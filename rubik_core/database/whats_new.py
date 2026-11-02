"""The one What's New table a workspace keeps: who opened a release note.

The notes themselves are the Cubie changelog, written once by RubikSoft and
read through ``rubik_core/whats_new/changelog.py`` (spec §4.6.1). What stays
in each workspace's own database is how its people reached them:

``whats_new_opens``
    One row per (entry, person, source): someone arrived from the channel
    post's link or pressed "Try it". ``entry_id`` is the changelog entry's id
    (a short token, not a foreign key: the note lives elsewhere). The person
    is stored as ``viewer_key``, an HMAC of the entry id and their email under
    a key only this feature holds, never as the email. That is PSEUDONYMOUS,
    not anonymous: whoever holds both the database and the key can
    re-identify an open. The UNIQUE ``(entry_id, viewer_key, source)`` is the
    dedup (a person counts once per entry per source) and the conflict target
    of an ``ON CONFLICT DO NOTHING`` insert, never a pre-check.

"Seen" (which notes a person has already read) stays in their browser
(``frontend/src/components/whatsNew/memory.ts``).

House rules (``loop_closure.py`` / ``insights.py``): no foreign keys,
naive-UTC instants, vocabularies as strings with ``CHECK (col IN (...))``.
This module imports only ``Base`` and the constraint helpers
(``backend/tests/test_database_import_order.py``).

The approval-era tables (``whats_new_items``, ``whats_new_clicks``) are gone:
no model maps them and the Phase 1 baseline migration does not create them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)

from rubik_core.database.constraints import in_check
from rubik_core.database.models import Base

# Duplicated as literals in alembic/versions/0001_cubie_baseline.py
# (a migration is a record of the shape that shipped).

#: How a person arrived: through the channel post's link, or "Try it".
OPEN_SOURCES: tuple[str, ...] = ("slack", "try_it")


class WhatsNewOpen(Base):
    """One person arriving at one release note, once per source. Never their
    email."""

    __tablename__ = "whats_new_opens"
    __table_args__ = (
        # The dedup and the insert's conflict target — never a pre-check.
        UniqueConstraint(
            "entry_id",
            "viewer_key",
            "source",
            name="uq_whats_new_opens_entry_viewer_source",
        ),
        Index("ix_whats_new_opens_entry_id", "entry_id"),
        CheckConstraint(
            in_check("source", OPEN_SOURCES), name="ck_whats_new_opens_source"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_id = Column(String(64), nullable=False)  # ChangelogEntry.id, no FK
    source = Column(String(20), nullable=False)
    # slugify_department(actor department) at open time; "" when none.
    department = Column(String(80), nullable=False, default="")
    # HMAC-SHA256 hex of f"{entry_id}:{email}" — pseudonymous, never the email.
    viewer_key = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
