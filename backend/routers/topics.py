from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import get_current_user
from backend.models.auth import User
from rubik_core.database.repository import SQLMetadataRegistry

router = APIRouter(prefix="/topics", tags=["topics"])

# Initialize topic manager
topic_manager = SQLMetadataRegistry()


class TopicRequest(BaseModel):
    topic: str


@router.get("/", response_model=list[str])
async def list_topics(user: User = Depends(get_current_user)):
    """Get all available topics. Any authenticated user can read.

    Topic is a required field in the (non-admin) document Upload modal, so the
    taxonomy must be readable by anyone allowed to upload — mirrors
    ``GET /api/departments/``. Managing topics (add/remove) stays admin-only.
    """
    return topic_manager.get_topics()


@router.post("/")
async def add_topic(request: TopicRequest, user: User = Depends(get_current_user)):
    """Add a new topic. Admin only."""
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Permission denied: only Admins can add topics"
        )

    success = topic_manager.add_topic(request.topic)
    if success:
        return {"message": f"Topic '{request.topic}' added successfully"}
    else:
        raise HTTPException(status_code=400, detail="Topic already exists")


@router.delete("/{topic}")
async def remove_topic(topic: str, user: User = Depends(get_current_user)):
    """Remove a topic. Admin only."""
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Permission denied: only Admins can remove topics"
        )

    success = topic_manager.remove_topic(topic)
    if success:
        return {"message": f"Topic '{topic}' removed successfully"}
    else:
        raise HTTPException(status_code=404, detail="Topic not found")
