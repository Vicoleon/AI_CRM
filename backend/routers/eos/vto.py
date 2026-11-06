"""EOS Vision/Traction Organizer (VTO).

A VTOVersion is the parent envelope; each version owns N VTOSections
(vision, mission, 10-year-target, etc.). This router only exposes
the verbs the upstream EOS app exposes — list/get/create on versions,
list/create/patch on sections. No DELETE on either today; fill in a
follow-up if needed.

Confidentiality: the VTO used to be entirely Exec-only. It's now
**per-line**: each bullet in a section's content carries its own
``confidential`` flag (set on upload — see ``vto_xlsx.py``). Non-exec
readers see only the currently *active* version, and only its
non-confidential bullets; the Exec team (``is_exec``) or admins see
everything, including drafts/archived versions. All *writes* — creating a
version/section, updating a section, downloading the template, uploading a
new version — remain Exec/admin-only, gated by ``_require_exec_or_admin``.
Reads are gated by the narrower ``_visible_version_or_404`` +
per-section content filtering below. The API gate is the authoritative
defense — the frontend's role-based rendering is convenience only.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)

from backend.jobs.eos_ingest_hooks import enqueue_vto_ingest
from backend.jobs.launcher import reject_support_background_work
from backend.models.eos.vto import (
    VTOSectionCreate,
    VTOSectionResponse,
    VTOSectionUpdate,
    VTOVersionCreate,
    VTOVersionResponse,
)
from backend.routers.eos._shared import CurrentUser, DBDep
from rubik_core.company.profile import company_name_for_documents, get_company_profile
from rubik_core.database.eos import Organization, VTOSection, VTOVersion
from rubik_core.ingestion.eos.vto_xlsx import (
    TEMPLATE_FILENAME,
    XLSX_MEDIA_TYPE,
    VtoParseError,
    build_vto_template_workbook,
    bullets_from_content,
    bullets_to_content,
    parse_vto_workbook,
)

router = APIRouter(prefix="/vto", tags=["EOS: VTO"])


def _is_exec_or_admin(current_user) -> bool:
    return "admin" in (current_user.roles or []) or getattr(
        current_user, "is_exec", False
    )


def _require_exec_or_admin(current_user) -> None:
    """Gate for every *write* endpoint (versions/sections create+update, the
    template download, and upload) — only the Exec team or admins may
    publish or edit the VTO."""
    if _is_exec_or_admin(current_user):
        return
    raise HTTPException(
        status_code=403,
        detail="Only members of the Exec team can modify the VTO.",
    )


def _visible_version_or_404(version_id: UUID, db: DBDep, current_user) -> VTOVersion:
    """Fetch a version, enforcing that non-exec/admin callers can only ever
    reach the currently *active* one — never a draft or archived version.
    Without this, a non-exec caller could enumerate version ids and read a
    not-yet-published version's open bullets before the Exec team intended
    it to go live. 404 (not 403) so a draft's existence isn't disclosed."""
    version = (
        db.query(VTOVersion)
        .filter(VTOVersion.id == version_id, VTOVersion.is_deleted == False)  # noqa: E712
        .first()
    )
    if not version or (not _is_exec_or_admin(current_user) and not version.is_active):
        raise HTTPException(status_code=404, detail="VTO Version not found")
    return version


def _section_response(
    section: VTOSection, can_see_confidential: bool
) -> VTOSectionResponse:
    """Shape one section for the response, dropping confidential bullets
    for non-exec/admin callers server-side — they're never sent to the
    client to begin with, not merely hidden by it."""
    bullets = bullets_from_content(section.content)
    if not can_see_confidential:
        bullets = [b for b in bullets if not b.confidential]
    resp = VTOSectionResponse.model_validate(section)
    return resp.model_copy(update={"content": bullets_to_content(bullets)})


# -- Versions ----------------------------------------------------------


@router.get("/versions", response_model=list[VTOVersionResponse])
def list_vto_versions(
    db: DBDep,
    current_user: CurrentUser,
    organization_id: UUID | None = None,
):
    """Open to any authenticated user — non-exec/admin callers only ever see
    the currently active version (never drafts/archived); exec/admin see
    all of them. Version metadata (name/status) isn't sensitive on its own —
    the sensitive part is per-line content, filtered in ``list_vto_sections``."""
    query = db.query(VTOVersion).filter(VTOVersion.is_deleted == False)  # noqa: E712
    if organization_id:
        query = query.filter(VTOVersion.organization_id == organization_id)
    if not _is_exec_or_admin(current_user):
        query = query.filter(VTOVersion.is_active == True)  # noqa: E712
    return query.all()


@router.get("/versions/{version_id}", response_model=VTOVersionResponse)
def get_vto_version(version_id: UUID, db: DBDep, current_user: CurrentUser):
    return _visible_version_or_404(version_id, db, current_user)


@router.post(
    "/versions",
    response_model=VTOVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_vto_version(
    db: DBDep, current_user: CurrentUser, version_in: VTOVersionCreate
):
    _require_exec_or_admin(current_user)
    version = VTOVersion(**version_in.model_dump(), created_by=current_user.email)
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


# -- Sections ----------------------------------------------------------


@router.get("/versions/{version_id}/sections", response_model=list[VTOSectionResponse])
def list_vto_sections(version_id: UUID, db: DBDep, current_user: CurrentUser):
    """Open to any authenticated user, scoped to the visible version
    (``_visible_version_or_404``) with confidential bullets stripped out
    for non-exec/admin callers (``_section_response``)."""
    version = _visible_version_or_404(version_id, db, current_user)
    can_see = _is_exec_or_admin(current_user)
    sections = (
        db.query(VTOSection)
        .filter(
            VTOSection.vto_version_id == version.id,
            VTOSection.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    return [_section_response(s, can_see) for s in sections]


@router.post(
    "/sections",
    response_model=VTOSectionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_vto_section(
    db: DBDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    section_in: VTOSectionCreate,
):
    _require_exec_or_admin(current_user)
    reject_support_background_work()
    section = VTOSection(**section_in.model_dump(), created_by=current_user.email)
    db.add(section)
    db.commit()
    db.refresh(section)
    enqueue_vto_ingest(background_tasks)
    return section


@router.patch("/sections/{section_id}", response_model=VTOSectionResponse)
def update_vto_section(
    db: DBDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    section_id: UUID,
    section_in: VTOSectionUpdate,
):
    _require_exec_or_admin(current_user)
    section = (
        db.query(VTOSection)
        .filter(VTOSection.id == section_id, VTOSection.is_deleted == False)  # noqa: E712
        .first()
    )
    if not section:
        raise HTTPException(status_code=404, detail="VTO Section not found")

    reject_support_background_work()
    for key, value in section_in.model_dump(exclude_unset=True).items():
        setattr(section, key, value)

    section.updated_by = current_user.email  # Who edited the VTO section
    db.commit()
    db.refresh(section)
    enqueue_vto_ingest(background_tasks)
    return section


# -- Template & upload (Exec self-service) -----------------------------
#
# The VTO page is otherwise read-only. These two endpoints let the Exec team
# publish the company VTO from a filled-in Excel template instead of going
# through direct API/SQL. Both are Exec/admin-gated, matching the section
# write endpoints above.


@router.get("/template")
def download_vto_template(db: DBDep, current_user: CurrentUser):
    """Download the .xlsx template the upload endpoint expects."""
    _require_exec_or_admin(current_user)
    company = company_name_for_documents(get_company_profile(db))
    return Response(
        content=build_vto_template_workbook(company_name=company),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{TEMPLATE_FILENAME}"'},
    )


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_vto(
    db: DBDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """Publish a new VTO version from a filled-in template.

    Each upload creates a fresh **active** ``VTOVersion`` and archives the
    previously-active one (only one active version at a time) — the spreadsheet
    is the complete VTO. A blank section is simply omitted (published empty).
    """
    _require_exec_or_admin(current_user)
    reject_support_background_work()

    raw = await file.read()
    try:
        parsed = parse_vto_workbook(raw)
    except VtoParseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # A deployment is single-tenant in EOS land — pick the (one) organization.
    org = (
        db.query(Organization)
        .filter(Organization.is_deleted == False)  # noqa: E712
        .first()
    )
    if org is None:
        raise HTTPException(
            status_code=400,
            detail="No EOS organization found. Run scripts/seed_eos_minimum.py first.",
        )

    # Archive whatever is active today (enforce single-active in app code).
    active_versions = (
        db.query(VTOVersion)
        .filter(
            VTOVersion.organization_id == org.id,
            VTOVersion.is_active == True,  # noqa: E712
            VTOVersion.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    for v in active_versions:
        v.is_active = False
        v.status = "archived"
        v.updated_by = current_user.email

    version = VTOVersion(
        organization_id=org.id,
        version_name=f"Uploaded {datetime.utcnow():%Y-%m-%d %H:%M} UTC",
        is_active=True,
        status="published",
        created_by=current_user.email,
    )
    db.add(version)
    db.flush()  # assign version.id for the section FK

    for section_type, bullets in parsed.sections.items():
        db.add(
            VTOSection(
                vto_version_id=version.id,
                section_type=section_type,
                content=bullets_to_content(
                    bullets
                ),  # Bullet -> plain dict (JSON column)
                created_by=current_user.email,
            )
        )

    db.commit()
    db.refresh(version)
    enqueue_vto_ingest(background_tasks)

    return {
        "version_id": str(version.id),
        "version_name": version.version_name,
        "is_active": version.is_active,
        "status": version.status,
        "sections_set": sorted(parsed.sections.keys()),
        "ignored_labels": parsed.ignored_labels,
    }
