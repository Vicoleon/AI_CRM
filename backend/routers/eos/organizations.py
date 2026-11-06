"""EOS organizations CRUD.

Mounted at `/api/eos/organizations`. Soft-delete semantics: list/get/update
filter `is_deleted == False`; delete flips the flag instead of removing
the row.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from backend.models.eos.organization import (
    OrganizationCreate,
    OrganizationResponse,
    OrganizationUpdate,
)
from backend.routers.eos._shared import CurrentUser, DBDep
from rubik_core.database.eos import Organization

router = APIRouter(prefix="/organizations", tags=["EOS: Organizations"])


@router.get("/", response_model=list[OrganizationResponse])
def list_organizations(db: DBDep, current_user: CurrentUser):
    return (
        db.query(Organization)
        .filter(Organization.is_deleted == False)  # noqa: E712 — SQLAlchemy
        .all()
    )


@router.get("/{org_id}", response_model=OrganizationResponse)
def get_organization(org_id: UUID, db: DBDep, current_user: CurrentUser):
    org = (
        db.query(Organization)
        .filter(Organization.id == org_id, Organization.is_deleted == False)  # noqa: E712
        .first()
    )
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


@router.post(
    "/", response_model=OrganizationResponse, status_code=status.HTTP_201_CREATED
)
def create_organization(
    db: DBDep, current_user: CurrentUser, org_in: OrganizationCreate
):
    org = Organization(**org_in.model_dump(), created_by=current_user.email)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


@router.patch("/{org_id}", response_model=OrganizationResponse)
def update_organization(
    org_id: UUID, db: DBDep, current_user: CurrentUser, org_in: OrganizationUpdate
):
    org = (
        db.query(Organization)
        .filter(Organization.id == org_id, Organization.is_deleted == False)  # noqa: E712
        .first()
    )
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    for field, value in org_in.model_dump(exclude_unset=True).items():
        setattr(org, field, value)

    org.updated_by = current_user.email
    db.commit()
    db.refresh(org)
    return org


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_organization(org_id: UUID, db: DBDep, current_user: CurrentUser):
    org = (
        db.query(Organization)
        .filter(Organization.id == org_id, Organization.is_deleted == False)  # noqa: E712
        .first()
    )
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    org.is_deleted = True
    org.updated_by = current_user.email
    db.commit()
    return None
