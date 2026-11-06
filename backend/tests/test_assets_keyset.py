"""Assets pagination must remain stable while the library changes."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend.routers import assets
from rubik_core.database.models import Asset, Base, User
from rubik_core.database.repository import SQLDAMRegistry
from rubik_core.tenancy.context import use_workspace


def test_created_at_keyset_survives_update_and_new_insert_between_pages():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    base = datetime(2026, 10, 30, 12)
    with Session(engine) as db:
        db.add(User(email="owner@example.com", id="owner", name="Owner"))
        db.add_all(
            Asset(
                doc_id=doc_id,
                owner_email="owner@example.com",
                created_at=base - timedelta(minutes=index),
                updated_at=base - timedelta(minutes=index),
            )
            for index, doc_id in enumerate(("a", "b", "c", "d"))
        )
        db.commit()
        registry = SQLDAMRegistry(db)

        statements: list[str] = []

        def record_query(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_query)
        first = registry.list_assets(limit=2, stable_cursor=True, include_total=False)
        assert list(first["assets"]) == ["a", "b"]
        assert first["total"] is None
        after = (datetime.fromisoformat(first["assets"]["b"]["created_at"]), "b")

        # Updating the next unread document moves it to the top under the old
        # updated_at order. An inserted document is newer than page one.
        db.get(Asset, "c").updated_at = base + timedelta(days=1)
        db.add(
            Asset(
                doc_id="new",
                owner_email="owner@example.com",
                created_at=base + timedelta(hours=1),
                updated_at=base + timedelta(hours=1),
            )
        )
        db.commit()

        second = registry.list_assets(
            limit=2, after=after, stable_cursor=True, include_total=False
        )
        assert list(second["assets"]) == ["c", "d"]
        assert "new" not in second["assets"]
        assert second["total"] is None
        assert not any("count(" in sql.lower() for sql in statements)
        event.remove(engine, "before_cursor_execute", record_query)

        # Callers that do not request keyset mode retain the original
        # recently-updated ordering and exact SQL total.
        legacy = registry.list_assets(limit=2)
        assert list(legacy["assets"]) == ["c", "new"]
        assert legacy["total"] == 5


def test_asset_cursor_is_signed_and_bound_to_workspace_user_and_filters():
    user = SimpleNamespace(email="reader@example.com")
    filters = {"topic": "Notes"}
    with use_workspace("test"):
        scope = assets._asset_cursor_scope(user, filters)
        key = (datetime(2026, 10, 30, 12), "doc-1")
        token = assets._encode_asset_cursor(key, scope, 7)
        assert assets._decode_asset_cursor(token, scope) == (key, 7)

        with pytest.raises(HTTPException) as altered:
            assets._decode_asset_cursor(
                token[:-1] + ("0" if token[-1] != "0" else "1"), scope
            )
        assert altered.value.status_code == 400

        other_user = assets._asset_cursor_scope(
            SimpleNamespace(email="other@example.com"), filters
        )
        with pytest.raises(HTTPException) as different_user:
            assets._decode_asset_cursor(token, other_user)
        assert different_user.value.status_code == 400

    with use_workspace("other"):
        other_workspace = assets._asset_cursor_scope(user, filters)
        with pytest.raises(HTTPException) as different_workspace:
            assets._decode_asset_cursor(token, other_workspace)
        assert different_workspace.value.status_code == 400
