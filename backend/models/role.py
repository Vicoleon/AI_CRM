from pydantic import BaseModel


class ResourcePermissions(BaseModel):
    """Permissions for a specific resource type."""

    create: bool = False
    read: bool = False
    update: bool = False
    delete: bool = False


class Role(BaseModel):
    """Role with granular permissions."""

    name: str
    description: str = ""
    permissions: dict[str, ResourcePermissions] = {}

    class Config:
        json_schema_extra = {
            "example": {
                "name": "editor",
                "description": "Can create and edit documents",
                "permissions": {
                    "documents": {
                        "create": True,
                        "read": True,
                        "update": True,
                        "delete": False,
                    },
                    "topics": {
                        "create": False,
                        "read": True,
                        "update": False,
                        "delete": False,
                    },
                },
            }
        }
