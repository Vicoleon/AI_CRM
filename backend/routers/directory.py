from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.auth import get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.tools.directory import GoogleDirectoryClient

router = APIRouter(prefix="/directory", tags=["directory"])


@router.get("/search", response_model=list[dict[str, Any]])
async def search_directory(
    q: str = Query(
        ..., min_length=1, description="Query string to search the directory"
    ),
    limit: int = Query(5, ge=1, le=20, description="Max results to return"),
    user: UserProfile = Depends(get_current_user),
):
    """
    Search the Google Workspace directory for users by name or email.
    """
    try:
        # Directory reads use the selected workspace's admin subject.
        client = GoogleDirectoryClient()
        users = client.search_users(query_text=q, max_results=limit)
        return users
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Directory search failed: {str(e)}"
        )
