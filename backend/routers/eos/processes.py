"""EOS core processes (FBA documents) — list + create.

Mounted at `/api/eos/processes`. Upstream only exposes list and create;
we port exactly. Full CRUD can be added in a follow-up.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, status

from backend.jobs.eos_ingest_hooks import enqueue_process_ingest
from backend.jobs.launcher import reject_support_background_work
from backend.models.eos.process import ProcessCreate, ProcessResponse
from backend.routers.eos._shared import CurrentUser, DBDep
from rubik_core.database.eos import EosProcess

router = APIRouter(prefix="/processes", tags=["EOS: Processes"])


@router.get("/", response_model=list[ProcessResponse])
def list_processes(db: DBDep, current_user: CurrentUser):
    return (
        db.query(EosProcess)
        .filter(EosProcess.is_deleted == False)  # noqa: E712
        .all()
    )


@router.post("/", response_model=ProcessResponse, status_code=status.HTTP_201_CREATED)
def create_process(
    db: DBDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    process_in: ProcessCreate,
):
    reject_support_background_work()
    process = EosProcess(**process_in.model_dump(), created_by=current_user.email)
    db.add(process)
    db.commit()
    db.refresh(process)
    enqueue_process_ingest(background_tasks)
    return process
