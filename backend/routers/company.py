"""Admin → Company setup: the company profile, the catalog and proposal templates.

GET  /api/company/profile                   — the profile (empty fields when unset)
PUT  /api/company/profile                   — save it (admin)
GET  /api/company/catalog                   — the product/service catalog
POST /api/company/catalog/import            — replace it from a CSV body (admin)
GET  /api/company/proposal-templates        — every template, active or not
POST /api/company/proposal-templates        — create one (admin)
PUT  /api/company/proposal-templates/{id}   — edit one (admin)
DELETE /api/company/proposal-templates/{id} — delete one (admin)

Mounted on the ``core`` parent in ``backend/main.py``, so every route sits
behind the copilot gate. Reads are open to anyone who passes it: the profile
and catalog are what the copilot already tells people about their own
company. Writes need the ``admin`` role, and each one leaves an audit row.

Nothing here is department-scoped, so there is no Feature Access entry: who
may edit is a role, and who may read is the copilot.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.audit import record_audit
from backend.auth import admin_required, get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.company import catalog
from rubik_core.company.profile import (
    get_company_profile,
    profile_from_dict,
    profile_to_dict,
    save_company_profile,
)
from rubik_core.database.db import get_db
from rubik_core.proposals import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/company", tags=["company"])

#: A catalog of a few thousand rows is well under this; anything bigger is the
#: wrong file.
MAX_CSV_BYTES = 1_000_000


class ProfileIn(BaseModel):
    name: str = ""
    legal_name: str = ""
    website: str = ""
    address: str = ""
    blurb: str = ""
    industry: str = ""
    offering: str = ""
    customers: str = ""
    sales_process: str = ""
    products: list[str] = Field(default_factory=list)


class TemplateIn(BaseModel):
    name: str
    body_md: str = ""
    catalog_item_names: list[str] = Field(default_factory=list)
    active: bool = True


class TemplatePatch(BaseModel):
    name: str | None = None
    body_md: str | None = None
    catalog_item_names: list[str] | None = None
    active: bool | None = None


# ── Profile ──────────────────────────────────────────────────────────────────


@router.get("/profile")
def get_profile(
    _: UserProfile = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return profile_to_dict(get_company_profile(db))


@router.put("/profile")
def put_profile(
    payload: ProfileIn,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    saved = save_company_profile(db, profile_from_dict(payload.model_dump()))
    record_audit(
        db,
        actor_email=user.email,
        action="COMPANY_PROFILE_UPDATE",
        details={
            "fields_set": sorted(k for k, v in profile_to_dict(saved).items() if v)
        },
    )
    return profile_to_dict(saved)


# ── Catalog ──────────────────────────────────────────────────────────────────


@router.get("/catalog")
def get_catalog(
    _: UserProfile = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return {"items": [catalog.row_to_dict(r) for r in catalog.list_catalog(db)]}


@router.post("/catalog/import")
async def import_catalog(
    request: Request,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Replace the catalog from a ``text/csv`` body.

    All or nothing: with any error the catalog is left as it was and
    ``created`` is 0, so a file with one bad row never half-replaces it.
    """
    raw = await request.body()
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail="The file is larger than 1 MB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400, detail="Save the file as CSV (UTF-8) and upload it again."
        ) from None
    rows, errors = catalog.parse_catalog_csv(text)
    if errors:
        return {"created": 0, "errors": errors}
    created = catalog.replace_catalog(db, rows)
    record_audit(
        db,
        actor_email=user.email,
        action="COMPANY_CATALOG_IMPORT",
        details={"rows": created},
    )
    return {"created": created, "errors": []}


# ── Proposal templates ───────────────────────────────────────────────────────


@router.get("/proposal-templates")
def list_proposal_templates(
    _: UserProfile = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return {
        "templates": [
            templates.template_to_dict(t) for t in templates.list_templates(db)
        ]
    }


def _bad_request(exc: templates.TemplateError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/proposal-templates", status_code=201)
def create_proposal_template(
    payload: TemplateIn,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        created = templates.create_template(db, payload.model_dump())
    except templates.TemplateError as exc:
        raise _bad_request(exc) from exc
    record_audit(
        db,
        actor_email=user.email,
        action="PROPOSAL_TEMPLATE_CREATE",
        target=created.key,
    )
    return templates.template_to_dict(created)


@router.put("/proposal-templates/{template_id}")
def update_proposal_template(
    template_id: int,
    payload: TemplatePatch,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        updated = templates.update_template(
            db, template_id, payload.model_dump(exclude_none=True)
        )
    except templates.TemplateError as exc:
        raise _bad_request(exc) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="template not found")
    record_audit(
        db,
        actor_email=user.email,
        action="PROPOSAL_TEMPLATE_UPDATE",
        target=updated.key,
    )
    return templates.template_to_dict(updated)


@router.delete("/proposal-templates/{template_id}")
def delete_proposal_template(
    template_id: int,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    existing = templates.get_by_id(db, template_id)
    if existing is None or not templates.delete_template(db, template_id):
        raise HTTPException(status_code=404, detail="template not found")
    record_audit(
        db,
        actor_email=user.email,
        action="PROPOSAL_TEMPLATE_DELETE",
        target=existing.key,
    )
    return {"deleted": True}
