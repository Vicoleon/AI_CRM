from __future__ import annotations

from backend.models.role import ResourcePermissions
from rubik_core.auth.registry import UserProfile, UserRegistry

# CRUD short names accepted by the legacy ``has_permission`` path. Anything
# else passed as ``action`` is treated as a v2 fine-grained capability
# string (e.g. ``"deal:advance_stage"``).
_CRUD_ACTIONS = frozenset({"create", "read", "update", "delete"})

# Resources covered by the CRUD-boolean model. Mirrors ``RESOURCE_TYPES`` in
# the admin panel (frontend/src/pages/Admin.tsx) — the checkbox grid an admin
# uses to build a role.
RESOURCE_TYPES = ("assets", "documents", "topics", "users")

# Shown to the user when they lack ``assets:create``. Every surface that adds a
# document to the knowledge base (DAM upload, AI description, Drive one-click
# ingest) returns this exact text, so the message is the same wherever they
# hit it. Deliberately free of internal jargon — "DAM" means nothing to the
# salesperson reading it — and it names the fix: ask an admin.
KNOWLEDGE_BASE_UPLOAD_DENIED = (
    "You don't have permission to add documents to the knowledge base. "
    "Ask an admin to give your account upload access."
)


def get_effective_permissions(
    user_or_email: str | UserProfile,
) -> dict[str, ResourcePermissions]:
    """
    Merges permissions from all roles assigned to the user (logical OR).
    """
    registry = UserRegistry()

    if isinstance(user_or_email, str):
        user = registry.get_user(user_or_email)
    else:
        user = user_or_email

    if not user:
        return {}

    all_roles = registry.get_roles()
    user_role_names = set(user.roles)

    # Filter roles assigned to the user
    user_roles = [r for r in all_roles if r.name in user_role_names]

    effective_perms: dict[str, ResourcePermissions] = {}

    for role in user_roles:
        for resource, perms in role.permissions.items():
            if resource not in effective_perms:
                effective_perms[resource] = ResourcePermissions(
                    create=perms.create,
                    read=perms.read,
                    update=perms.update,
                    delete=perms.delete,
                )
            else:
                current = effective_perms[resource]
                effective_perms[resource] = ResourcePermissions(
                    create=current.create or perms.create,
                    read=current.read or perms.read,
                    update=current.update or perms.update,
                    delete=current.delete or perms.delete,
                )

    return effective_perms


def _get_user(user_or_email: str | UserProfile) -> UserProfile | None:
    if isinstance(user_or_email, str):
        return UserRegistry().get_user(user_or_email)
    return user_or_email


def has_action_permission(user_or_email: str | UserProfile, action: str) -> bool:
    """Return True if any role assigned to the user owns a Permission row
    whose ``action`` column equals ``action`` (e.g. ``"deal:advance_stage"``).

    Admins always pass. Reads the raw ``Permission`` rows directly — the
    ``ResourcePermissions`` DTO returned by ``get_effective_permissions``
    only models the CRUD booleans, so fine-grained actions cannot round-trip
    through it.
    """
    user = _get_user(user_or_email)
    if not user:
        return False
    if user.is_admin:
        return True

    # Inline import to avoid a circular dep at module load (db -> models -> auth).
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import Permission

    user_role_names = list(user.roles or [])
    if not user_role_names:
        return False

    db = SessionLocal()
    try:
        match = (
            db.query(Permission.id)
            .filter(
                Permission.role_name.in_(user_role_names),
                Permission.action == action,
            )
            .first()
        )
        return match is not None
    finally:
        db.close()


def has_permission(
    user_or_email: str | UserProfile, resource: str, action: str
) -> bool:
    """Return True if any role grants ``action`` on ``resource``.

    Two cases, both backed by the same ``permissions`` table:

    * **Legacy CRUD path** — ``action`` is one of ``create / read /
      update / delete``. Checks the merged CRUD booleans for
      ``resource`` (existing v1 behavior, unchanged).
    * **v2 fine-grained path** — ``action`` is a namespaced capability
      string (anything containing ``":"``, e.g.
      ``"deal:advance_stage"``). Matches against ``Permission.action``;
      ``resource`` is ignored for these rows because the action string
      already encodes the target.

    Admins always pass.
    """
    user = _get_user(user_or_email)
    if user and user.is_admin:
        return True

    # v2 fine-grained capability — namespaced strings are the discriminator.
    if action not in _CRUD_ACTIONS:
        return has_action_permission(user, action) if user else False

    # Legacy CRUD-boolean path — unchanged.
    if not user:
        return False
    effective_perms = get_effective_permissions(user)
    resource_perms = effective_perms.get(resource)
    if not resource_perms:
        return False
    return getattr(resource_perms, action, False)


def visible_permissions(
    user_or_email: str | UserProfile,
) -> dict[str, ResourcePermissions]:
    """The CRUD verdicts :func:`has_permission` would return, as a plain map.

    Exists so the UI can gate a control *before* the user does the work. The
    upload form used to be offered to every user and only rejected on submit —
    after the file, topic, description and tags had been filled in.

    Differs from :func:`get_effective_permissions` in two ways, both needed to
    stay faithful to what the endpoints actually enforce:

    * **Admins get everything**, mirroring the ``is_admin`` short-circuit in
      ``has_permission`` (role rows alone grant an admin nothing).
    * **Every resource in** :data:`RESOURCE_TYPES` **is present**, so callers
      can read ``perms["assets"].create`` without a ``None`` check.

    Only models CRUD booleans — v2 fine-grained capabilities (``"deal:…"``)
    aren't representable here and are still checked server-side only.
    """
    user = _get_user(user_or_email)
    if user and user.is_admin:
        return {
            resource: ResourcePermissions(
                create=True, read=True, update=True, delete=True
            )
            for resource in RESOURCE_TYPES
        }

    granted = get_effective_permissions(user) if user else {}
    return {
        resource: granted.get(resource) or ResourcePermissions()
        for resource in RESOURCE_TYPES
    }
