"""Browser connections — the long-lived key "Cubie for Chrome" calls Cubie with.

The extension cannot use the Cubie session. The web app keeps its session JWT
in the Cubie tab's own ``localStorage`` (no cookies), it expires after five
hours with no refresh, and Google sign-in will not run on a
``chrome-extension://`` page. So a signed-in person presses Connect on Cubie's
``/chrome/connect`` page, Cubie mints one random key for that browser, and the
extension keeps it. The key carries a signed workspace slug for routing before
any tenant database is opened, and the row records Google's verified hosted
domain so a removed domain invalidates every connection minted under it.

A row is one connected browser. The raw key is shown exactly once, at mint; only
its SHA-256 is stored, so a database read cannot replay it. The key is
deliberately narrow: ``backend.auth`` explicitly refuses its ``cbx_`` prefix
as a session JWT, and the only
routes that do are the three under ``/api/browser-extension`` the extension
calls (``backend/routers/browser_extension.py``).

* ``revoked_at`` NULL and ``expires_at`` in the future → **live**.
* ``revoked_reason`` says why a row stopped working: ``disconnected`` (the
  person pressed Disconnect in Cubie or Sign out in the extension),
  ``offboarded`` (the offboard step revoked every row for them) or
  ``replaced`` (the same browser connected again).

House rules, as in ``deal_defers.py``: emails lower-folded with NO foreign key
to ``users`` (a foreign key would lock that hot table during the migration and stall
every request), instants naive UTC, vocabularies strings with a CHECK.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, Index, String
from sqlalchemy.orm import validates

from rubik_core.database.constraints import in_check, lower_check
from rubik_core.database.models import Base

# Duplicated as literals in alembic/versions/0001_cubie_baseline.py
# (a migration is a record of the shape that shipped).
REVOKED_REASONS: tuple[str, ...] = ("disconnected", "offboarded", "replaced")


class BrowserConnection(Base):
    """One browser connected to one person's Cubie. See the module docstring."""

    __tablename__ = "browser_connections"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_email = Column(String, nullable=False)
    #: Google verified hosted domain at mint. NULL legacy rows must reconnect.
    hosted_domain = Column(String, nullable=True)
    #: SHA-256 hex of the raw key. The raw key is never stored.
    token_hash = Column(String, nullable=False, unique=True)
    #: The last four characters, so Settings can tell two browsers apart.
    token_hint = Column(String, nullable=False)
    #: What the person sees in Settings, e.g. "Chrome on macOS".
    label = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    #: Stamped at most every few minutes (see ``LAST_USED_RESOLUTION``), not
    #: on every call, so a busy extension is not a write per page load.
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    revoked_reason = Column(String, nullable=True)

    __table_args__ = (
        Index("ix_browser_connections_user_open", "user_email", "revoked_at"),
        CheckConstraint(
            in_check("revoked_reason", REVOKED_REASONS, nullable=True),
            name="ck_browser_connections_revoked_reason",
        ),
        CheckConstraint(
            lower_check("user_email"), name="ck_browser_connections_user_lower"
        ),
    )

    @validates("user_email")
    def _fold(self, _key, value):
        return value.strip().lower() if isinstance(value, str) else value

    @validates("hosted_domain")
    def _fold_domain(self, _key, value):
        return value.strip().lower() if isinstance(value, str) else value
