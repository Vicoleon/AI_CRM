"""EOS event log — read-only audit trail.

Mounted at `/api/eos/events`. Returns the 100 most recent events.
Writes go through application code (event_logger service) rather than
through HTTP — there is no POST surface on purpose.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.models.eos.event import EosEventResponse
from backend.routers.eos._shared import CurrentUser, DBDep
from rubik_core.database.eos import EosEvent

router = APIRouter(prefix="/events", tags=["EOS: Events"])


@router.get("/", response_model=list[EosEventResponse])
def list_events(db: DBDep, current_user: CurrentUser):
    """List the 100 most recent EOS events, newest first."""
    return db.query(EosEvent).order_by(EosEvent.timestamp.desc()).limit(100).all()
