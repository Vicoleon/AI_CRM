from __future__ import annotations

# rubik_core/auth/registry.py
import logging
from typing import Any

from backend.models.role import Role
from rubik_core.database.repository import SQLMetadataRegistry, SQLUserRegistry
from rubik_core.database.schemas import AccessPolicy, Group, UserProfile

logger = logging.getLogger(__name__)


class UserRegistry:
    def __init__(self):
        self.repo = SQLUserRegistry()
        self.meta_repo = SQLMetadataRegistry()
        # Initialize WorkspaceService for Google Workspace sync if needed
        try:
            from rubik_core.auth.workspace import WorkspaceService

            self.workspace_service = WorkspaceService(self)
        except (ImportError, Exception) as e:
            logger.debug(f"WorkspaceService not available: {e}")
            self.workspace_service = None

    def get_roles(self) -> list[Role]:
        return self.repo.get_roles()

    def get_role_names(self) -> list[str]:
        return [role.name for role in self.get_roles()]

    def add_role(self, role: Role | str) -> list[Role]:
        return self.repo.add_role(role)

    def update_role(self, role_name: str, updates: dict[str, Any]) -> Role | None:
        return self.repo.update_role(role_name, updates)

    def delete_role(self, role_name: str) -> list[Role]:
        return self.repo.delete_role(role_name)

    def get_user(self, email: str) -> UserProfile | None:
        return self.repo.get_user(email)

    def register_user(
        self,
        email: str,
        name: str,
        id: str,
        roles: list[str] | None = None,
        department: str = "general",
        picture: str | None = None,
    ) -> UserProfile:
        return self.repo.register_user(email, name, id, roles, department, picture)

    def update_user(self, email: str, updates: dict[str, Any]) -> UserProfile | None:
        return self.repo.update_user(email, updates)

    def record_login(
        self,
        email: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> bool:
        """Record a (debounced) login session for ``email``. See
        :meth:`SQLUserRegistry.record_login`."""
        return self.repo.record_login(email, ip_address, user_agent)

    def list_users(self) -> list[UserProfile]:
        return self.repo.list_users()

    def delete_user(self, email: str) -> bool:
        return self.repo.delete_user(email)

    def get_group_mappings(self) -> dict[str, list[str]]:
        return self.repo.get_group_mappings()

    def update_group_mappings(self, mappings: dict[str, list[str]]):
        self.repo.update_group_mappings(mappings)

    def get_workspace_settings(self) -> dict[str, Any]:
        return self.repo.get_workspace_settings()

    def update_workspace_settings(self, settings: dict[str, Any]):
        self.repo.update_workspace_settings(settings)

    def list_workspace_groups(self) -> list[dict[str, Any]]:
        """List all groups from Google Workspace."""
        if not self.workspace_service:
            return []
        return self.workspace_service.list_all_groups()

    def sync_user_workspace_roles(self, email: str) -> UserProfile | None:
        if not self.workspace_service:
            return self.get_user(email)
        try:
            self.workspace_service.sync_user_roles(email)
            return self.get_user(email)
        except Exception as e:
            logger.error(f"Failed to sync workspace roles for {email}: {e}")
            return self.get_user(email)

    # Groups
    def get_groups(self) -> list[Group]:
        return self.meta_repo.get_groups()

    def add_group(self, name: str, description: str | None = None) -> Group:
        return self.meta_repo.add_group(name, description)

    def delete_group(self, group_id: str) -> bool:
        return self.meta_repo.delete_group(group_id)

    def add_user_to_group(self, email: str, group_id: str) -> bool:
        return self.meta_repo.add_user_to_group(email, group_id)

    def remove_user_from_group(self, email: str, group_id: str) -> bool:
        return self.meta_repo.remove_user_from_group(email, group_id)

    # Access Policies
    def get_access_policies(
        self, user_email: str | None = None, group_id: str | None = None
    ) -> list[AccessPolicy]:
        return self.meta_repo.get_access_policies(user_email, group_id)

    def add_access_policy(
        self,
        user_email: str | None = None,
        group_id: str | None = None,
        department: str | None = None,
        topic: str | None = None,
        can_read: bool = True,
        can_update: bool = False,
    ) -> AccessPolicy:
        return self.meta_repo.add_access_policy(
            user_email, group_id, department, topic, can_read, can_update
        )

    def delete_access_policy(self, policy_id: int) -> bool:
        return self.meta_repo.delete_access_policy(policy_id)
