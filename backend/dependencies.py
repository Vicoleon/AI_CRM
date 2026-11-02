from fastapi import Depends

from backend.auth import get_current_user
from backend.models.auth import User


def fathom_access_required(user: User = Depends(get_current_user)) -> User:
    """
    Dependency to ensure the user has access to fathom integrations.
    Currently just requires an authenticated user, but could be expanded to check specific scopes or roles.
    """
    return user
