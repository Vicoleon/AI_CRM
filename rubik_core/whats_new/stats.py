"""Opens of a release note, counted as people — never who.

Spec §4.6.1: the "opened" counts stay in each workspace's own database
(``whats_new_opens``). Two rules make "people, never who" true:

* **No email is stored.** An open is stored as ``viewer_key``, an HMAC-SHA256
  of ``"<entry id>:<email>"`` under a key only this feature holds
  (``workspace_settings`` row :data:`OPEN_KEY_ROW`, generated once, never
  rotated or returned by anything). That is PSEUDONYMOUS, not anonymous:
  someone with the database AND that key could re-identify an open. No API
  returns a viewer key or the email of someone who opened a note.
* **A person counts once** per entry per source (the UNIQUE key on
  ``whats_new_opens``; the insert is ``ON CONFLICT DO NOTHING``, which is
  also safe on pg8000, where a duplicate is not an ``IntegrityError``).

Only people on the Workspace domain count; the impersonation placeholder and
the Insights exclusions never do.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from rubik_core.database.whats_new import OPEN_SOURCES, WhatsNewOpen
from rubik_core.tenancy.context import current_workspace

#: The ``workspace_settings`` row that holds this workspace's open key.
OPEN_KEY_ROW = "whats_new_open_key"

_open_keys: dict[str, bytes] = {}


def reset_caches() -> None:
    """Forget the cached key (tests; a new database)."""
    _open_keys.clear()


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def _insert(db: Session, table, values: dict, conflict: list) -> int | None:
    """Insert-or-nothing on Postgres and SQLite: the rowcount, or None on any
    other dialect (only reachable from an ad-hoc engine)."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        return None
    stmt = (
        insert(table).values(**values).on_conflict_do_nothing(index_elements=conflict)
    )
    return db.execute(stmt).rowcount


def _secret(db: Session) -> bytes:
    """The open key, generated once and then only ever read."""
    slug = current_workspace()
    cached = _open_keys.get(slug)
    if cached:
        return cached
    from rubik_core.database.models import WorkspaceSetting

    def _read() -> str | None:
        row = db.get(WorkspaceSetting, OPEN_KEY_ROW)
        value = row.value if row is not None else None
        secret = value.get("secret") if isinstance(value, dict) else None
        return secret if isinstance(secret, str) and len(secret) >= 32 else None

    secret = _read()
    if secret is None:
        table = WorkspaceSetting.__table__
        fresh = {"key": OPEN_KEY_ROW, "value": {"secret": secrets.token_hex(32)}}
        try:
            # Insert-or-nothing, never an overwrite: a key another instance
            # committed first stays, so every process ends up on the same key.
            if _insert(db, table, fresh, [table.c.key]) is None:
                db.add(WorkspaceSetting(**fresh))  # a duplicate raises below
            db.commit()
        except DBAPIError:
            # Caught as DBAPIError, not IntegrityError: pg8000 reports a
            # duplicate key as a ProgrammingError. The re-read below decides.
            db.rollback()
        # Re-read instead of trusting our own value: the first committed key
        # wins, in every process.
        db.expire_all()
        secret = _read()
    if secret is None:
        raise RuntimeError("What's New open key unavailable")
    _open_keys[slug] = secret.encode()
    return _open_keys[slug]


def viewer_key(db: Session, entry_id: str, email: str) -> str:
    """The pseudonymous id of one person for one release note."""
    message = f"{entry_id}:{_fold(email)}".encode()
    return hmac.new(_secret(db), message, hashlib.sha256).hexdigest()


def record_open(
    db: Session, entry_id: str, *, email: str, department: str, source: str
) -> bool:
    """Count one person arriving at one release note. True when this is their
    first arrival from ``source``; False for a repeat or a person who is not
    counted (no email, the impersonation placeholder, an address off the
    Workspace domain). Raises ``ValueError`` for an unknown source."""
    if source not in OPEN_SOURCES:
        raise ValueError(f"Unknown open source {source!r}.")
    from rubik_core.insights.population import excluded_emails, on_workspace_domain
    from rubik_core.rag.config import slugify_department

    folded = _fold(email)
    if not folded or folded in excluded_emails() or not on_workspace_domain(folded):
        return False
    values = {
        "entry_id": entry_id,
        "source": source,
        "department": slugify_department(department or "")[:80],
        "viewer_key": viewer_key(db, entry_id, folded),
        "created_at": datetime.utcnow(),
    }
    table = WhatsNewOpen.__table__
    conflict = [table.c.entry_id, table.c.viewer_key, table.c.source]
    rowcount = _insert(db, table, values, conflict)
    if rowcount is not None:
        db.commit()
        return rowcount == 1
    try:
        db.add(WhatsNewOpen(**values))
        db.commit()
        return True
    except Exception:  # noqa: BLE001 — a duplicate is the expected case
        db.rollback()
        return False
