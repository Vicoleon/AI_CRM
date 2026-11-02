from __future__ import annotations

# rubik_core/auth/workspace.py
import logging
from typing import TYPE_CHECKING, Any

try:
    from googleapiclient.discovery import build
except ImportError:
    build = None
from rubik_core.config import settings
from rubik_core.tools._workspace_auth import (
    load_workspace_credentials,
    workspace_dwd_subject,
)

if TYPE_CHECKING:
    from rubik_core.auth.registry import UserRegistry

logger = logging.getLogger(__name__)

GROUP_SCOPES = ["https://www.googleapis.com/auth/admin.directory.group.readonly"]
USER_SCOPES = ["https://www.googleapis.com/auth/admin.directory.user.readonly"]


class WorkspaceService:
    def __init__(self, registry: UserRegistry):
        self.registry = registry
        # Scopes required for different operations
        self.GROUP_SCOPES = GROUP_SCOPES
        self.USER_SCOPES = USER_SCOPES

    def _get_credentials(self, subject_email: str, scopes: list[str]):
        if not subject_email:
            return None
        creds = load_workspace_credentials(scopes)
        if creds is None:
            return None

        # Delegate to the admin user
        return creds.with_subject(subject_email)

    def get_user_department(self, user_email: str) -> str | None:
        if settings.DISABLE_WORKSPACE_SYNC:
            logger.debug(
                "Workspace sync disabled via settings. Skipping get_user_department."
            )
            return None

        settings_db = self.registry.get_workspace_settings()
        if not settings_db.get("workspace_sync_enabled"):
            return None

        if build is None:
            logger.error(
                "google-api-python-client not installed. Cannot sync with Google Workspace."
            )
            return None

        admin_email = workspace_dwd_subject()
        if not admin_email:
            logger.error("Admin email for Workspace delegation not configured.")
            return None

        creds = self._get_credentials(admin_email, self.USER_SCOPES)
        if not creds:
            return None

        try:
            service = build(
                "admin", "directory_v1", credentials=creds, cache_discovery=False
            )
            user_res = service.users().get(userKey=user_email).execute()
            organizations = user_res.get("organizations", [])
            if organizations:
                # Return the department from the first organization entry
                return organizations[0].get("department")
            return None
        except Exception as e:
            logger.error(f"Failed to fetch user info for {user_email}: {e}")
            return None

    def get_user_groups(self, user_email: str) -> list[str]:
        if settings.DISABLE_WORKSPACE_SYNC:
            logger.debug(
                "Workspace sync disabled via settings. Skipping get_user_groups."
            )
            return []

        settings_db = self.registry.get_workspace_settings()
        if not settings_db.get("workspace_sync_enabled"):
            return []

        if build is None:
            logger.error(
                "google-api-python-client not installed. Cannot sync with Google Workspace."
            )
            return []

        admin_email = workspace_dwd_subject()
        if not admin_email:
            logger.error("Admin email for Workspace delegation not configured.")
            return []

        creds = self._get_credentials(admin_email, self.GROUP_SCOPES)
        if not creds:
            return []

        try:
            service = build(
                "admin", "directory_v1", credentials=creds, cache_discovery=False
            )
            results = service.groups().list(userKey=user_email).execute()
            groups = results.get("groups", [])
            return [g["email"] for g in groups]
        except Exception as e:
            logger.error(f"Failed to fetch groups for {user_email}: {e}")
            return []

    def list_all_groups(self) -> list[dict[str, Any]]:
        if settings.DISABLE_WORKSPACE_SYNC:
            logger.debug(
                "Workspace sync disabled via settings. Skipping list_all_groups."
            )
            return []

        settings_db = self.registry.get_workspace_settings()
        if not settings_db.get("workspace_sync_enabled"):
            return []

        if build is None:
            logger.error(
                "google-api-python-client not installed. Cannot sync with Google Workspace."
            )
            return []

        admin_email = workspace_dwd_subject()
        if not admin_email:
            logger.error("Admin email for Workspace delegation not configured.")
            return []

        creds = self._get_credentials(admin_email, self.GROUP_SCOPES)
        if not creds:
            return []

        try:
            service = build(
                "admin", "directory_v1", credentials=creds, cache_discovery=False
            )
            # Use 'my_customer' to get all groups in the account
            results = service.groups().list(customer="my_customer").execute()
            groups = results.get("groups", [])
            return [
                {"email": g["email"], "name": g.get("name", g["email"])} for g in groups
            ]
        except Exception as e:
            logger.error(f"Failed to list all workspace groups: {e}")
            # Raise or return a specific error that the frontend can use
            return []

    def sync_user_roles(self, user_email: str) -> list[str]:
        """Fetch groups and map to roles. Also sync department."""
        department = self.get_user_department(user_email)
        groups = self.get_user_groups(user_email)

        updates = {}
        if department:
            updates["department"] = department

        if groups:
            updates["workspace_groups"] = groups
            mappings = self.registry.get_group_mappings()
            new_roles = set(["user"])  # Base role
            found_mapped_group = False
            for group in groups:
                if group in mappings:
                    new_roles.update(mappings[group])
                    found_mapped_group = True

            if found_mapped_group:
                updates["roles"] = list(new_roles)

        if updates:
            self.registry.update_user(user_email, updates)
            return updates.get("roles", [])

        return []
