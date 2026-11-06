from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user
from backend.models.auth import User
from backend.services.generated_docs import (
    MEETING_PREP_RUN_TAG_PREFIX,
    MEETING_PREP_WORKSPACE_TAG_PREFIX,
    XRAY_COMPANY_TAG_PREFIX,
    XRAY_DOC_ID_PREFIX,
    is_generated_asset,
)
from rubik_core.auth.access import (
    has_document_access,
    load_document_access_policies,
)
from rubik_core.auth.permissions import has_permission
from rubik_core.dam.audit import log_action
from rubik_core.dam.registry import AssetMetadata, DAMRegistry
from rubik_core.dam.vectorstore import delete_document_vectors
from rubik_core.dam.versions import purge_version_bytes
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import UserBookmark
from rubik_core.database.repository import ASSET_LIST_MIN_CREATED_AT
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.platform_secrets import platform_secret

router = APIRouter(prefix="/assets", tags=["assets"])

# Initialize DAM registry
dam_registry = DAMRegistry()

# A live CRM ownership proof may need network I/O for every distinct X-Ray
# company. Limit work per request even when most matching assets are hidden.
_LIST_CHUNK_SIZE = 50
_LIST_SCAN_LIMIT = 250
_LIST_COMPANY_PROOF_LIMIT = 20
_LIST_PREP_PROOF_LIMIT = 20


def _asset_sort_key(asset: Any) -> tuple[datetime, str]:
    value = (
        asset.get("created_at")
        if isinstance(asset, dict)
        else getattr(asset, "created_at", None)
    )
    doc_id = (
        asset.get("doc_id")
        if isinstance(asset, dict)
        else getattr(asset, "doc_id", None)
    )
    if not isinstance(doc_id, str) or not doc_id:
        raise ValueError("Asset list row has no document ID")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value is None:
        value = ASSET_LIST_MIN_CREATED_AT
    if not isinstance(value, datetime):
        raise ValueError("Asset list row has invalid creation time")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value, doc_id


def _asset_cursor_scope(user: User, filters: dict[str, Any]) -> str:
    payload = {
        "workspace": current_workspace(),
        "email": user.email,
        "filters": filters,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _encode_asset_cursor(key: tuple[datetime, str], scope: str, seen: int) -> str:
    date, doc_id = key
    payload = {
        "v": 1,
        "date": date.isoformat(timespec="microseconds"),
        "doc_id": doc_id,
        "scope": scope,
        "seen": seen,
    }
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )
    signature = hmac.new(
        platform_secret("CUBIE_SESSION_SECRET").encode(),
        encoded.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def _decode_asset_cursor(token: str, scope: str) -> tuple[tuple[datetime, str], int]:
    try:
        body, signature = token.split(".")
        expected = hmac.new(
            platform_secret("CUBIE_SESSION_SECRET").encode(),
            body.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        encoded = body.encode("ascii")
        raw = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "date",
            "doc_id",
            "scope",
            "seen",
        }:
            raise ValueError
        if payload["v"] != 1 or payload["scope"] != scope:
            raise ValueError
        date = datetime.fromisoformat(payload["date"])
        doc_id = payload["doc_id"]
        seen = payload["seen"]
        if (
            date.tzinfo is not None
            or not isinstance(doc_id, str)
            or not doc_id
            or type(seen) is not int
            or seen < 0
        ):
            raise ValueError
        return (date, doc_id), seen
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=400, detail="Asset page cursor is invalid"
        ) from None


def _xray_company_id(asset: AssetMetadata) -> str | None:
    """None for ordinary assets, empty for old/unprovable X-Ray documents."""
    tags = getattr(asset, "tags", None) or []
    is_xray = (
        str(getattr(asset, "doc_id", "")).startswith(XRAY_DOC_ID_PREFIX)
        or "customer-xray" in tags
        or getattr(asset, "topic", None) == "Customer X-Ray"
        or str(getattr(asset, "original_filename", "") or "").startswith(
            "customer-xray-"
        )
    )
    if not is_xray:
        return None
    matches = [
        value[len(XRAY_COMPANY_TAG_PREFIX) :]
        for value in tags
        if isinstance(value, str) and value.startswith(XRAY_COMPANY_TAG_PREFIX)
    ]
    if len(matches) != 1 or not matches[0] or len(matches[0]) > 128:
        return ""
    if not all(char.isalnum() or char in "_-" for char in matches[0]):
        return ""
    return matches[0]


def _meeting_prep_run_id(asset: AssetMetadata) -> str | None:
    """None for unrelated assets, empty for unprovable Meeting Prep files."""
    tags = getattr(asset, "tags", None) or []
    is_prep = (
        "meeting-prep" in tags
        or any(
            isinstance(tag, str) and tag.startswith(MEETING_PREP_RUN_TAG_PREFIX)
            for tag in tags
        )
        or getattr(asset, "topic", None) == "Meeting Prep"
        or str(getattr(asset, "original_filename", "") or "").startswith(
            "meeting-prep-"
        )
    )
    if not is_prep:
        return None
    runs = [
        tag[len(MEETING_PREP_RUN_TAG_PREFIX) :]
        for tag in tags
        if isinstance(tag, str) and tag.startswith(MEETING_PREP_RUN_TAG_PREFIX)
    ]
    workspaces = [
        tag[len(MEETING_PREP_WORKSPACE_TAG_PREFIX) :]
        for tag in tags
        if isinstance(tag, str) and tag.startswith(MEETING_PREP_WORKSPACE_TAG_PREFIX)
    ]
    if len(runs) != 1 or len(workspaces) != 1:
        return ""
    try:
        if workspaces[0] != current_workspace():
            return ""
    except Exception:  # noqa: BLE001 - unscoped asset reads fail closed
        return ""
    if not runs[0] or len(runs[0]) > 128:
        return ""
    if not all(char.isalnum() or char in "_-" for char in runs[0]):
        return ""
    return runs[0]


def _visible_prep_run_ids(run_ids: set[str]) -> set[str]:
    from rubik_core.database.models import MeetingPrepRun
    from rubik_core.meeting_prep.provenance import contexts_visible

    with SessionLocal() as db:
        rows = [db.get(MeetingPrepRun, run_id) for run_id in run_ids]
        candidates = [row for row in rows if row is not None and row.response]
        live = contexts_visible([row.context_bundle for row in candidates])
        return {row.id for row, is_visible in zip(candidates, live) if is_visible}


async def _visible_xray_asset_ids(
    assets: dict[str, Any],
    checked_companies: dict[str, bool],
    checked_preps: dict[str, bool] | None = None,
) -> set[str]:
    """Batch the live source proof used by Assets metadata/list routes."""
    from rubik_core.xray.fetchers import visible_company_ids

    company_by_doc = {
        doc_id: _xray_company_id(AssetMetadata(**raw) if isinstance(raw, dict) else raw)
        for doc_id, raw in assets.items()
    }
    candidates = sorted(
        {
            company
            for company in company_by_doc.values()
            if company and company not in checked_companies
        }
    )
    if candidates:
        allowed = await run_in_threadpool(visible_company_ids, candidates)
        checked_companies.update(
            {company: company in allowed for company in candidates}
        )
    if checked_preps is None:
        checked_preps = {}
    prep_by_doc = {
        doc_id: _meeting_prep_run_id(
            AssetMetadata(**raw) if isinstance(raw, dict) else raw
        )
        for doc_id, raw in assets.items()
    }
    prep_candidates = {
        run_id
        for run_id in prep_by_doc.values()
        if run_id and run_id not in checked_preps
    }
    if prep_candidates:
        allowed_preps = await run_in_threadpool(_visible_prep_run_ids, prep_candidates)
        checked_preps.update(
            {run_id: run_id in allowed_preps for run_id in prep_candidates}
        )
    return {
        doc_id
        for doc_id, company_id in company_by_doc.items()
        if company_id is None or checked_companies.get(company_id, False)
        if prep_by_doc[doc_id] is None or checked_preps.get(prep_by_doc[doc_id], False)
    }


def _require_live_xray_asset_sync(asset: AssetMetadata) -> None:
    company_id = _xray_company_id(asset)
    if company_id is not None:
        if not company_id:
            raise HTTPException(status_code=404, detail="Asset not found")
        from rubik_core.xray.fetchers import company_available

        if not company_available(company_id):
            raise HTTPException(status_code=404, detail="Asset not found")
    prep_run_id = _meeting_prep_run_id(asset)
    if prep_run_id is not None and (
        not prep_run_id or prep_run_id not in _visible_prep_run_ids({prep_run_id})
    ):
        raise HTTPException(status_code=404, detail="Asset not found")


async def _require_live_xray_asset(asset: AssetMetadata) -> None:
    await run_in_threadpool(_require_live_xray_asset_sync, asset)


def _can_manage_asset(user: User, asset) -> bool:
    """Who may archive/update an asset.

    Owner and admins always. A holder of the ``assets:update`` permission may
    too, but only for documents within their access scope — otherwise an
    editor-type role could pull a doc from another department out of the RAG
    (archive is destructive: it removes the doc's chunks from the vector store).
    """
    if user.is_admin or asset.owner == user.email:
        return True
    return has_permission(user, "assets", "update") and has_document_access(user, asset)


class AssetUpdate(BaseModel):
    status: str | None = None
    topic: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class AssetBookmarkState(BaseModel):
    """Returned by ``POST /api/assets/{doc_id}/bookmark`` so the UI knows
    which icon (outline vs. solid) to render after the toggle."""

    doc_id: str
    bookmarked: bool


@router.get("/", response_model=dict[str, Any])
async def list_assets(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    cursor: str | None = Query(
        None,
        max_length=2048,
        description="Opaque cursor returned by the previous page.",
    ),
    q: str | None = Query(
        None, description="Case-insensitive substring match on filename + topic."
    ),
    status: str | None = Query(
        None,
        description="Filter by lifecycle status (draft / pending_approval / approved / archived).",
    ),
    topic: str | None = Query(None),
    department: str | None = Query(None),
    is_company_wide: bool | None = Query(
        None, description="V2 — restrict to (or exclude) company-wide assets."
    ),
    user: User = Depends(get_current_user),
):
    """List registered assets with pagination, RBAC and department filtering.

    V2: also accepts ``q`` (substring match on
    filename + topic), ``status``, ``topic``, ``department``, and
    ``is_company_wide`` so the V2 Library page can drop the legacy
    client-side filter step. Empty-string filters are treated as no
    filter so the frontend can pass blanks without zeroing results.
    """
    if not has_permission(user, "assets", "read"):
        raise HTTPException(status_code=403, detail="Not authorized to list assets")
    if offset:
        raise HTTPException(
            status_code=400,
            detail="Use the next page cursor for asset pagination; offset must be zero.",
        )

    # Normalize empty strings → None so blank inputs don't zero results.
    def _blank(v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    q = _blank(q)
    status = _blank(status)
    topic = _blank(topic)
    department = _blank(department)

    filter_kwargs = dict(
        q=q,
        status=status,
        topic=topic,
        department=department,
        is_company_wide=is_company_wide,
        # Meeting transcripts have their own tab (GET /api/transcripts/) and
        # carry no real department; keep them out of the Documents list so the
        # Department column isn't dominated by phantom-department transcript rows.
        exclude_transcripts=True,
    )

    # The SQL count includes inaccessible rows, so it cannot be returned as
    # the visible total. Scan in bounded chunks by immutable creation key;
    # updates or inserts between pages cannot shift earlier rows. The cursor
    # is scoped to this workspace, caller and exact filter set.
    scope = _asset_cursor_scope(user, filter_kwargs)
    after, visible_count = (
        _decode_asset_cursor(cursor, scope) if cursor is not None else (None, 0)
    )
    page: dict[str, Any] = {}
    scanned = 0
    more_possible = False
    checked_companies: dict[str, bool] = {}
    checked_preps: dict[str, bool] = {}
    policies = (
        []
        if user.is_admin
        else await run_in_threadpool(load_document_access_policies, user)
    )
    while scanned < _LIST_SCAN_LIMIT and len(page) < limit:
        chunk_limit = min(_LIST_CHUNK_SIZE, _LIST_SCAN_LIMIT - scanned)
        raw = await run_in_threadpool(
            dam_registry.list_assets,
            limit=chunk_limit,
            after=after,
            stable_cursor=True,
            include_total=False,
            **filter_kwargs,
        )
        raw_assets = raw.get("assets", {})
        if not raw_assets:
            more_possible = False
            break
        local_access: dict[str, Any] = {}
        new_companies: set[str] = set()
        new_preps: set[str] = set()
        examined = 0
        proof_limit_reached = False
        for doc_id, asset_data in raw_assets.items():
            asset_obj = (
                AssetMetadata(**asset_data)
                if isinstance(asset_data, dict)
                else asset_data
            )
            if user.is_admin or has_document_access(user, asset_obj, policies=policies):
                company_id = _xray_company_id(asset_obj)
                prep_run_id = _meeting_prep_run_id(asset_obj)
                if (
                    company_id
                    and company_id not in checked_companies
                    and company_id not in new_companies
                ):
                    if (
                        len(checked_companies) + len(new_companies)
                        >= _LIST_COMPANY_PROOF_LIMIT
                    ):
                        proof_limit_reached = True
                        break
                    new_companies.add(company_id)
                if (
                    prep_run_id
                    and prep_run_id not in checked_preps
                    and prep_run_id not in new_preps
                ):
                    if len(checked_preps) + len(new_preps) >= _LIST_PREP_PROOF_LIMIT:
                        proof_limit_reached = True
                        break
                    new_preps.add(prep_run_id)
                local_access[doc_id] = asset_data
            examined += 1
        visible = await _visible_xray_asset_ids(
            local_access, checked_companies, checked_preps
        )
        consumed = 0
        for doc_id, asset_data in list(raw_assets.items())[:examined]:
            consumed += 1
            after = _asset_sort_key(asset_data)
            if doc_id not in visible:
                continue
            visible_count += 1
            page[doc_id] = asset_data
            if len(page) == limit:
                break
        scanned += consumed
        more_possible = consumed < len(raw_assets) or len(raw_assets) == chunk_limit
        if len(raw_assets) < chunk_limit or proof_limit_reached:
            break

    # On an incomplete scan, total is a proven lower bound. `has_more` tells
    # clients to request the next cursor; it does not promise another visible
    # row among the unscanned assets.
    has_more = more_possible
    total = visible_count

    return {
        "assets": page,
        "total": total,
        "total_is_exact": not has_more,
        "has_more": has_more,
        "next_cursor": _encode_asset_cursor(after, scope, visible_count)
        if has_more and after
        else None,
        "limit": limit,
        "offset": offset,
    }


# Bookmark endpoints — V2 Library wraps the generic `user_bookmark` table
# from V2_1 with asset-scoped helpers so the per-row UI doesn't need to
# know about target_kind.
# Registered BEFORE the `{doc_id:path}` catch-all so `/bookmarks` doesn't
# get captured as a doc_id.


@router.get("/bookmarks", response_model=list[AssetMetadata])
async def list_bookmarked_assets(
    user: User = Depends(get_current_user),
):
    """All assets the current caller has bookmarked, newest-first.

    Joins ``user_bookmark`` (V2_1 table) to ``Asset`` via ``doc_id``.
    Rows where the underlying asset has been deleted are skipped — the
    bookmark row stays in place (no cascade) so a future restore can
    rehydrate the saved view without a re-bookmark step.
    """
    if not has_permission(user, "assets", "read"):
        raise HTTPException(status_code=403, detail="Not authorized to read assets")

    db = SessionLocal()
    try:
        rows = (
            db.query(UserBookmark)
            .filter(UserBookmark.actor_email == user.email)
            .filter(UserBookmark.target_kind == "asset")
            .order_by(UserBookmark.created_at.desc())
            .all()
        )
        # Two-step: gather doc_ids, then resolve via DAMRegistry so
        # access-check and DTO mapping stay in one place.
        candidates: dict[str, AssetMetadata] = {}
        for row in rows:
            asset = dam_registry.get_asset(row.target_id)
            if asset is None:
                continue
            if not has_document_access(user, asset):
                # Soft-skip — a bookmark on an asset the user lost access to
                # disappears from the Saved page without a 403.
                continue
            candidates[row.target_id] = asset
        visible_ids = await _visible_xray_asset_ids(candidates, {}, {})
        return [asset for doc_id, asset in candidates.items() if doc_id in visible_ids]
    finally:
        db.close()


@router.post("/{doc_id:path}/bookmark", response_model=AssetBookmarkState)
async def toggle_asset_bookmark(
    doc_id: str,
    user: User = Depends(get_current_user),
):
    """Toggle the caller's bookmark on this asset. Returns the new state.

    Implementation reads ``user_bookmark`` for an existing row; if found,
    deletes it (un-bookmark); otherwise inserts (bookmark). The path is
    declared with ``:path`` so doc_ids that look like nested paths
    (e.g. ``meet/2026-05-26/foo``) still resolve.
    """
    doc_id = doc_id.lstrip("/")

    if not has_permission(user, "assets", "read"):
        raise HTTPException(status_code=403, detail="Not authorized to bookmark assets")

    asset = dam_registry.get_asset(doc_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    if not has_document_access(user, asset):
        # Same shape as get_asset — don't leak existence to non-readers.
        raise HTTPException(
            status_code=403, detail="Not authorized to access this asset"
        )
    await _require_live_xray_asset(asset)

    db = SessionLocal()
    try:
        existing = (
            db.query(UserBookmark)
            .filter(UserBookmark.actor_email == user.email)
            .filter(UserBookmark.target_kind == "asset")
            .filter(UserBookmark.target_id == doc_id)
            .one_or_none()
        )
        if existing is not None:
            db.delete(existing)
            db.commit()
            log_action(user.email, "bookmark.remove", doc_id, "success")
            return AssetBookmarkState(doc_id=doc_id, bookmarked=False)

        row = UserBookmark(
            id=str(uuid.uuid4()),
            actor_email=user.email,
            target_kind="asset",
            target_id=doc_id,
            created_at=datetime.utcnow(),
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Lost a race against another request that bookmarked this
            # asset in the same instant. Treat as already-bookmarked.
            db.rollback()
        log_action(user.email, "bookmark.add", doc_id, "success")
        return AssetBookmarkState(doc_id=doc_id, bookmarked=True)
    finally:
        db.close()


@router.post("/{doc_id:path}/archive", response_model=AssetMetadata)
async def archive_asset(doc_id: str, user: User = Depends(get_current_user)):
    """Archive an asset: remove it from the vector store but keep the record.

    The doc stops appearing in Chat / Briefing answers immediately. The
    registry row (owner, filename, dates) and the source file are retained as
    an audit record; re-uploading the file restores it to the RAG. Registered
    before the ``{doc_id:path}`` catch-all so ``/archive`` isn't read as a
    doc_id (mirrors the bookmark route).
    """
    doc_id = doc_id.lstrip("/")

    asset = dam_registry.get_asset(doc_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    if not _can_manage_asset(user, asset):
        raise HTTPException(
            status_code=403, detail="Not authorized to archive this asset"
        )
    await _require_live_xray_asset(asset)

    # Cubie-generated docs are never ingested (no chunks to purge) — archiving
    # them is a pure status flip, and the corpus integrity check below would
    # always fail for them.
    if asset.status != "archived" and not is_generated_asset(asset):
        try:
            removed = delete_document_vectors(
                doc_id,
                source_path=asset.source_path,
                department=asset.department,
                is_company_wide=asset.is_company_wide,
                also_delete_gcs=False,  # retain the source object as a record
            )
        except Exception as e:
            log_action(user.email, "archive", doc_id, "failure", {"error": str(e)})
            raise HTTPException(
                status_code=500, detail=f"Failed to remove from vector store: {e}"
            )
        # Don't mark archived while the chunks are still live in the corpus —
        # that would silently keep the doc answering in Chat.
        if not removed:
            log_action(
                user.email, "archive", doc_id, "failure", {"error": "no ragFile found"}
            )
            raise HTTPException(
                status_code=500,
                detail="Could not locate this document in the search index to archive it.",
            )

    updated_asset = dam_registry.update_asset(doc_id, {"status": "archived"})
    log_action(user.email, "archive", doc_id, "success")
    return updated_asset


# ── Generated-doc actions: download / share / email-draft ───────────────────
# Registered BEFORE the `{doc_id:path}` catch-alls so their literal suffixes
# aren't read as part of a doc_id (same reason /bookmark and /archive sit up
# here). Download works for any asset whose bytes we hold (generated docs,
# uploads, transcripts); share/email-draft are how a Cubie-generated brief
# reaches a teammate — see backend/services/generated_docs.py for the rules.


class AssetShareRequest(BaseModel):
    emails: list[str]


class AssetEmailDraftRequest(BaseModel):
    to: list[str] = []
    note: str = ""


def _load_readable_asset(doc_id: str, user: User):
    """The asset behind a bytes-serving route, or the same refusal the metadata
    read gives.

    BOTH checks, in the order ``get_asset`` (the metadata read) applies them:
    the ``assets:read`` role permission first, then ``has_document_access`` for
    this particular row. The permission check was missing here, so a caller
    without ``assets:read`` got 403 asking for an asset's *metadata* and 200
    downloading its *bytes* — the weaker gate on the stronger disclosure.
    """
    if not has_permission(user, "assets", "read"):
        raise HTTPException(status_code=403, detail="Not authorized to read assets")
    asset = dam_registry.get_asset(doc_id.lstrip("/"))
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    if not has_document_access(user, asset):
        raise HTTPException(
            status_code=403, detail="Not authorized to access this asset"
        )
    _require_live_xray_asset_sync(asset)
    return asset


@router.get("/{doc_id:path}/download")
async def download_asset(doc_id: str, user: User = Depends(get_current_user)):
    """Stream the asset's stored bytes (GCS on Cloud Run, disk locally)."""
    import mimetypes

    from fastapi import Response

    from backend.services.generated_docs import load_doc_bytes

    asset = await run_in_threadpool(_load_readable_asset, doc_id, user)
    if not asset.source_path:
        raise HTTPException(status_code=404, detail="No stored file for this asset.")
    try:
        data = load_doc_bytes(asset.source_path)
    except Exception as e:  # noqa: BLE001 — surface storage misses as 404
        log_action(user.email, "download", asset.doc_id, "failure", {"error": str(e)})
        raise HTTPException(status_code=404, detail="Stored file unavailable.")
    filename = asset.original_filename or asset.doc_id.replace("/", "-")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    log_action(user.email, "download", asset.doc_id, "success")
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{doc_id:path}/share")
async def share_asset(
    doc_id: str,
    body: AssetShareRequest,
    user: User = Depends(get_current_user),
):
    """Grant teammates access (ACL append + bell notification + best-effort
    link email). Owner or admin only — access granting is a custody decision."""
    from backend.services import generated_docs

    asset = dam_registry.get_asset(doc_id.lstrip("/"))
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    if asset.owner != user.email and not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Only the owner or an admin can share this asset"
        )
    await _require_live_xray_asset(asset)
    try:
        return generated_docs.share_asset(user.email, asset, body.emails)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{doc_id:path}/email-draft")
async def create_asset_email_draft(
    doc_id: str,
    body: AssetEmailDraftRequest,
    user: User = Depends(get_current_user),
):
    """Draft (never send) a Gmail message in the caller's mailbox with this doc
    attached; the UI opens the returned draft URL for the human to review+send."""
    from backend.services import generated_docs

    asset = await run_in_threadpool(_load_readable_asset, doc_id, user)
    if not asset.source_path:
        raise HTTPException(status_code=404, detail="No stored file for this asset.")
    try:
        result = generated_docs.create_asset_email_draft(
            user.email, asset, to=body.to, note=body.note
        )
    except Exception as e:  # noqa: BLE001 — Gmail/DWD errors → actionable 502
        log_action(
            user.email, "email_draft", asset.doc_id, "failure", {"error": str(e)}
        )
        raise HTTPException(status_code=502, detail=f"Could not create draft: {e}")
    return result


@router.get("/{doc_id:path}", response_model=AssetMetadata)
async def get_asset(doc_id: str, user: User = Depends(get_current_user)):
    """Get a specific asset by doc_id."""
    # Normalize doc_id
    doc_id = doc_id.lstrip("/")

    if not has_permission(user, "assets", "read"):
        raise HTTPException(status_code=403, detail="Not authorized to read assets")

    asset = dam_registry.get_asset(doc_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    # Check access using centralized helper
    if not has_document_access(user, asset):
        raise HTTPException(
            status_code=403, detail="Not authorized to access this asset"
        )
    await _require_live_xray_asset(asset)

    return asset


@router.patch("/{doc_id:path}", response_model=AssetMetadata)
async def update_asset_status(
    doc_id: str, update: AssetUpdate, user: User = Depends(get_current_user)
):
    """Update asset status or topic."""
    # Normalize doc_id
    doc_id = doc_id.lstrip("/")

    if not has_permission(user, "assets", "update"):
        # Fallback: owner can always update? Product requirement says owner or editor can update
        pass

    # RBAC: owner/admin, or an assets:update holder within their access scope.
    asset = dam_registry.get_asset(doc_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    if not _can_manage_asset(user, asset):
        raise HTTPException(
            status_code=403, detail="Not authorized to update this asset"
        )
    await _require_live_xray_asset(asset)

    # Un-archiving via PATCH would leave the row "approved" while its chunks are
    # gone (archive purged them) — re-upload is the only restore path.
    if (
        asset.status == "archived"
        and update.status is not None
        and update.status != "archived"
    ):
        raise HTTPException(
            status_code=400,
            detail="Re-upload the document to restore it from archive.",
        )

    if (
        _xray_company_id(asset) is not None or _meeting_prep_run_id(asset) is not None
    ) and update.tags is not None:
        raise HTTPException(
            status_code=400,
            detail="Generated document provenance tags cannot be edited",
        )

    # Archiving must also pull the doc's chunks out of the corpus — Chat
    # retrieval is gated by corpus membership, so flipping only the DB row would
    # leave the doc answering. (Cubie-generated docs have no chunks — skip.)
    if (
        update.status == "archived"
        and asset.status != "archived"
        and not is_generated_asset(asset)
    ):
        try:
            removed = delete_document_vectors(
                doc_id,
                source_path=asset.source_path,
                department=asset.department,
                is_company_wide=asset.is_company_wide,
                also_delete_gcs=False,  # retain the source object as a record
            )
        except Exception as e:
            log_action(user.email, "archive", doc_id, "failure", {"error": str(e)})
            raise HTTPException(
                status_code=500, detail=f"Failed to remove from vector store: {e}"
            )
        if not removed:
            log_action(
                user.email, "archive", doc_id, "failure", {"error": "no ragFile found"}
            )
            raise HTTPException(
                status_code=500,
                detail="Could not locate this document in the search index to archive it.",
            )

    updated_asset = dam_registry.update_asset(
        doc_id, update.model_dump(exclude_unset=True)
    )

    # Audit log
    log_action(
        actor_email=user.email,
        action="update",
        doc_id=doc_id,
        status="success",
        details=update.model_dump(exclude_unset=True),
    )

    return updated_asset


@router.delete("/{doc_id:path}")
async def delete_asset(
    doc_id: str,
    user: User = Depends(get_current_user),
):
    """Delete an asset from registry and vector store."""
    # Normalize doc_id
    doc_id = doc_id.lstrip("/")

    asset = dam_registry.get_asset(doc_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    await _require_live_xray_asset(asset)

    # Product requirement: only owner can delete (or admin)
    if asset.owner != user.email and not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Only the owner or an admin can delete this asset"
        )

    # 1. Delete from Vector Store and Cloud Storage first.
    #    Cubie-generated docs were never ingested — just remove the stored file.
    if is_generated_asset(asset):
        from backend.services.generated_docs import delete_doc_bytes

        delete_doc_bytes(asset.source_path)
    else:
        try:
            delete_document_vectors(
                doc_id,
                source_path=asset.source_path,
                department=asset.department,
                is_company_wide=asset.is_company_wide,
            )
        except Exception as e:
            error_msg = f"Failed to delete from vector/storage backend: {str(e)}"
            log_action(user.email, "delete", doc_id, "failure", {"error": error_msg})
            raise HTTPException(status_code=500, detail=error_msg)

    # 1b. Superseded versions' files — the current one went above.
    purge_version_bytes(
        dam_registry.list_versions(doc_id), keep_source_path=asset.source_path
    )

    # 2. Delete from Registry only if vector store delete didn't raise
    success = dam_registry.delete_asset(doc_id)
    if success:
        log_action(user.email, "delete", doc_id, "success")
        return {"message": "Asset deleted successfully"}
    else:
        log_action(
            user.email,
            "delete",
            doc_id,
            "failure",
            {"error": "Failed to delete from registry"},
        )
        raise HTTPException(
            status_code=500, detail="Failed to delete asset from registry"
        )
