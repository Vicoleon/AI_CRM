from __future__ import annotations

# rubik_core/dam/registry.py
from datetime import datetime
from typing import Any

from rubik_core.database.repository import SQLDAMRegistry
from rubik_core.database.schemas import AssetMetadata, AssetVersionInfo


class DAMRegistry:
    def __init__(self):
        self.repo = SQLDAMRegistry()

    def register_asset(self, asset: AssetMetadata):
        self.repo.register_asset(asset)

    def update_asset(
        self, doc_id: str, updates: dict[str, Any]
    ) -> AssetMetadata | None:
        return self.repo.update_asset(doc_id, updates)

    def delete_asset(self, doc_id: str) -> bool:
        return self.repo.delete_asset(doc_id)

    def get_asset(self, doc_id: str) -> AssetMetadata | None:
        return self.repo.get_asset(doc_id)

    def get_asset_by_source_path(self, source_path: str) -> AssetMetadata | None:
        return self.repo.get_asset_by_source_path(source_path)

    def get_asset_by_content_hash(self, content_hash: str) -> AssetMetadata | None:
        return self.repo.get_asset_by_content_hash(content_hash)

    def list_assets(
        self,
        limit: int | None = None,
        offset: int | None = None,
        *,
        after: tuple[datetime, str] | None = None,
        stable_cursor: bool = False,
        include_total: bool = True,
        q: str | None = None,
        status: str | None = None,
        topic: str | None = None,
        department: str | None = None,
        is_company_wide: bool | None = None,
        exclude_transcripts: bool = False,
    ) -> dict[str, Any]:
        """Forward filters to the SQL repository so the V2 Library can
        ask for ``q`` / ``status`` / ``topic`` / ``department`` /
        ``is_company_wide`` server-side instead of paginating through
        every approved asset client-side.

        ``exclude_transcripts`` drops ``Meeting Transcripts`` rows (they have
        their own tab), used by the Assets → Documents list."""
        return self.repo.list_assets(
            limit=limit,
            offset=offset,
            after=after,
            stable_cursor=stable_cursor,
            include_total=include_total,
            q=q,
            status=status,
            topic=topic,
            department=department,
            is_company_wide=is_company_wide,
            exclude_transcripts=exclude_transcripts,
        )

    # ───────────────────────────── versions ─────────────────────────────

    def list_versions(self, doc_id: str) -> list[AssetVersionInfo]:
        return self.repo.list_versions(doc_id)

    def get_version(self, doc_id: str, version_number: int) -> AssetVersionInfo | None:
        return self.repo.get_version(doc_id, version_number)

    def add_version(self, doc_id: str, **kwargs: Any) -> AssetVersionInfo | None:
        return self.repo.add_version(doc_id, **kwargs)

    def update_version(
        self, doc_id: str, version_number: int, updates: dict[str, Any]
    ) -> AssetVersionInfo | None:
        return self.repo.update_version(doc_id, version_number, updates)
