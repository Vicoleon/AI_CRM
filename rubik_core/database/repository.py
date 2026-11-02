from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import and_, delete, func, or_
from sqlalchemy.orm import Session, aliased

from backend.models.role import ResourcePermissions as PermDTO
from backend.models.role import Role as RoleDTO
from rubik_core.database import models, schemas
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import (
    Asset,
    AssetVersion,
    AuditLog,
    LoginEvent,
    MeetingParticipant,
    Permission,
    Role,
    User,
    WorkspaceSetting,
)
from rubik_core.database.schemas import AccessPolicy as PolicyDTO
from rubik_core.database.schemas import AssetMetadata, AssetVersionInfo, UserProfile
from rubik_core.database.schemas import Group as GroupDTO

logger = logging.getLogger(__name__)

ASSET_LIST_MIN_CREATED_AT = datetime(1970, 1, 1)


class SQLDAMRegistry:
    def __init__(self, db: Session | None = None):
        self._db = db

    def _get_db(self):
        return self._db if self._db else SessionLocal()

    def register_asset(self, asset_dto: AssetMetadata):
        db = self._get_db()
        try:
            asset = Asset(
                doc_id=asset_dto.doc_id,
                owner_email=asset_dto.owner,
                approver_email=asset_dto.approver,
                status=asset_dto.status,
                topic=asset_dto.topic,
                department=asset_dto.department,
                description=asset_dto.description,
                tags=asset_dto.tags,
                source_path=asset_dto.source_path,
                is_company_wide=asset_dto.is_company_wide,
                allowed_emails=asset_dto.allowed_emails,
                content_hash=asset_dto.content_hash,
                processing=asset_dto.processing,
                original_filename=asset_dto.original_filename,
                current_version=asset_dto.current_version or 1,
                created_at=asset_dto.created_at,
                updated_at=asset_dto.updated_at,
            )
            db.merge(asset)  # handles insert or update
            db.commit()
        finally:
            if not self._db:
                db.close()

    def update_asset(
        self, doc_id: str, updates: dict[str, Any]
    ) -> AssetMetadata | None:
        db = self._get_db()
        try:
            asset = db.get(Asset, doc_id)
            if not asset:
                return None

            for key, value in updates.items():
                if key == "owner":
                    asset.owner_email = value
                elif hasattr(asset, key):
                    setattr(asset, key, value)

            asset.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(asset)
            return self._to_asset_dto(asset)
        finally:
            if not self._db:
                db.close()

    def delete_asset(self, doc_id: str) -> bool:
        db = self._get_db()
        try:
            asset = db.get(Asset, doc_id)
            if asset:
                db.delete(asset)
                db.commit()
                return True
            return False
        finally:
            if not self._db:
                db.close()

    def get_asset(self, doc_id: str) -> AssetMetadata | None:
        db = self._get_db()
        try:
            asset = db.get(Asset, doc_id)
            return self._to_asset_dto(asset) if asset else None
        except Exception as e:
            logger.error(f"Database error getting asset: {e}")
            return None
        finally:
            if not self._db:
                db.close()

    def get_asset_by_source_path(self, source_path: str) -> AssetMetadata | None:
        db = self._get_db()
        try:
            asset = db.query(Asset).filter(Asset.source_path == source_path).first()
            return self._to_asset_dto(asset) if asset else None
        except Exception as e:
            logger.error(f"Database error getting asset by source path: {e}")
            return None
        finally:
            if not self._db:
                db.close()

    def get_asset_by_content_hash(self, content_hash: str) -> AssetMetadata | None:
        db = self._get_db()
        try:
            asset = db.query(Asset).filter(Asset.content_hash == content_hash).first()
            return self._to_asset_dto(asset) if asset else None
        except Exception as e:
            logger.error(f"Database error getting asset by content hash: {e}")
            return None
        finally:
            if not self._db:
                db.close()

    def list_assets(
        self,
        limit: int | None = None,
        offset: int | None = None,
        *,
        after: tuple[datetime, str] | None = None,
        stable_cursor: bool = False,
        include_total: bool = True,
        q: str | None = None,
        status: str | None = None,
        topic: str | None = None,
        department: str | None = None,
        is_company_wide: bool | None = None,
        exclude_transcripts: bool = False,
    ) -> dict[str, Any]:
        """List assets with optional server-side filters (V2 Library).

        Filters compose with AND. `q` does a case-insensitive substring
        match on ``original_filename`` OR ``topic``. Empty strings on
        ``q`` / ``status`` / ``topic`` / ``department`` are treated the
        same as ``None`` (no filter) so the V2 frontend can drop them
        without zeroing the result set.
        """
        db = self._get_db()
        try:
            query = db.query(Asset)
            if q:
                like = f"%{q.strip().lower()}%"
                query = query.filter(
                    func.lower(func.coalesce(Asset.original_filename, "")).like(like)
                    | func.lower(func.coalesce(Asset.topic, "")).like(like)
                )
            if status:
                query = query.filter(Asset.status == status)
            if topic:
                query = query.filter(Asset.topic == topic)
            if department:
                query = query.filter(Asset.department == department)
            if is_company_wide is not None:
                query = query.filter(Asset.is_company_wide.is_(bool(is_company_wide)))
            if exclude_transcripts:
                # Meeting transcripts have their own tab (/api/transcripts/) and
                # carry no real department, so keep them out of the document
                # Library list. coalesce() so genuinely NULL-topic docs survive.
                query = query.filter(
                    func.coalesce(Asset.topic, "") != "Meeting Transcripts"
                )

            total = query.count() if include_total else None

            if stable_cursor:
                # Creation time stays fixed across routine asset edits. Use it
                # for keyset pages; keep the legacy updated-at ordering for
                # callers that have not opted into cursor pagination.
                sort_date = func.coalesce(Asset.created_at, ASSET_LIST_MIN_CREATED_AT)
                if after is not None:
                    after_date, after_doc_id = after
                    query = query.filter(
                        or_(
                            sort_date < after_date,
                            and_(sort_date == after_date, Asset.doc_id > after_doc_id),
                        )
                    )
                query = query.order_by(sort_date.desc(), Asset.doc_id.asc())
            else:
                if after is not None:
                    raise ValueError("A keyset cursor requires stable_cursor=True")
                query = query.order_by(Asset.updated_at.desc(), Asset.doc_id.asc())

            if after is None and offset is not None:
                query = query.offset(offset)
            if limit is not None:
                query = query.limit(limit)

            assets = query.all()
            return {
                "assets": {
                    a.doc_id: self._to_asset_dto(a).model_dump(mode="json")
                    for a in assets
                },
                "total": total,
            }
        finally:
            if not self._db:
                db.close()

    def _to_asset_dto(self, asset: Asset) -> AssetMetadata:
        return AssetMetadata(
            doc_id=asset.doc_id,
            owner=asset.owner_email,
            approver=asset.approver_email,
            status=asset.status,
            topic=asset.topic,
            department=asset.department,
            description=getattr(asset, "description", None),
            tags=asset.tags
            if hasattr(asset, "tags") and asset.tags is not None
            else [],
            source_path=asset.source_path,
            is_company_wide=asset.is_company_wide,
            allowed_emails=asset.allowed_emails
            if hasattr(asset, "allowed_emails") and asset.allowed_emails is not None
            else [],
            content_hash=getattr(asset, "content_hash", None),
            processing=bool(getattr(asset, "processing", False)),
            original_filename=asset.original_filename,
            current_version=getattr(asset, "current_version", None) or 1,
            created_at=asset.created_at,
            updated_at=asset.updated_at,
        )

    # ───────────────────────────── versions ─────────────────────────────

    @staticmethod
    def _to_version_dto(row: AssetVersion, current: int) -> AssetVersionInfo:
        return AssetVersionInfo(
            doc_id=row.doc_id,
            version_number=row.version_number,
            is_current=row.version_number == current,
            source_path=row.source_path,
            original_filename=row.original_filename,
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            uploaded_by=row.uploaded_by,
            note=row.note,
            change_summary=row.change_summary,
            restored_from=row.restored_from,
            created_at=row.created_at,
        )

    @staticmethod
    def _ensure_initial_version(db: Session, asset: Asset) -> None:
        """Lazily record version 1 from the asset row itself.

        Assets are created by several paths (DAM upload, Drive "+ Knowledge",
        transcript sync, Save-to-Assets) and none of them wrote a version row
        before version history existed. Rather than backfill and chase every
        creator, the first time anyone looks at or extends a document's history
        we snapshot the row as it stands — the asset's ``source_path`` IS
        version 1's bytes.
        """
        has_rows = (
            db.query(AssetVersion.id)
            .filter(AssetVersion.doc_id == asset.doc_id)
            .first()
        )
        if has_rows:
            return
        db.add(
            AssetVersion(
                id=str(uuid.uuid4()),
                doc_id=asset.doc_id,
                version_number=asset.current_version or 1,
                source_path=asset.source_path,
                original_filename=asset.original_filename,
                content_hash=asset.content_hash,
                size_bytes=None,
                uploaded_by=asset.owner_email,
                note=None,
                change_summary=None,
                restored_from=None,
                created_at=asset.created_at or datetime.utcnow(),
            )
        )
        db.flush()

    def list_versions(self, doc_id: str) -> list[AssetVersionInfo]:
        """Every version of ``doc_id``, newest first. Empty if no such asset."""
        db = self._get_db()
        try:
            asset = db.get(Asset, doc_id)
            if not asset:
                return []
            self._ensure_initial_version(db, asset)
            db.commit()
            rows = (
                db.query(AssetVersion)
                .filter(AssetVersion.doc_id == doc_id)
                .order_by(AssetVersion.version_number.desc())
                .all()
            )
            current = asset.current_version or 1
            return [self._to_version_dto(r, current) for r in rows]
        finally:
            if not self._db:
                db.close()

    def get_version(self, doc_id: str, version_number: int) -> AssetVersionInfo | None:
        db = self._get_db()
        try:
            asset = db.get(Asset, doc_id)
            if not asset:
                return None
            self._ensure_initial_version(db, asset)
            db.commit()
            row = (
                db.query(AssetVersion)
                .filter(
                    AssetVersion.doc_id == doc_id,
                    AssetVersion.version_number == version_number,
                )
                .one_or_none()
            )
            return (
                self._to_version_dto(row, asset.current_version or 1) if row else None
            )
        finally:
            if not self._db:
                db.close()

    def add_version(
        self,
        doc_id: str,
        *,
        source_path: str,
        original_filename: str | None,
        content_hash: str | None,
        size_bytes: int | None,
        uploaded_by: str,
        note: str | None = None,
        restored_from: int | None = None,
    ) -> AssetVersionInfo | None:
        """Make a new revision the asset's current version.

        Inserts the ``asset_versions`` row and repoints the asset's
        ``source_path`` / ``content_hash`` / ``original_filename`` at it, with
        ``processing=True`` and status ``approved`` (a failed previous ingest
        is superseded by this one). The caller owns the vector-store work.
        Returns None when the asset doesn't exist.
        """
        db = self._get_db()
        try:
            asset = db.get(Asset, doc_id)
            if not asset:
                return None
            self._ensure_initial_version(db, asset)
            next_number = (asset.current_version or 1) + 1
            row = AssetVersion(
                id=str(uuid.uuid4()),
                doc_id=doc_id,
                version_number=next_number,
                source_path=source_path,
                original_filename=original_filename,
                content_hash=content_hash,
                size_bytes=size_bytes,
                uploaded_by=uploaded_by,
                note=(note or "").strip() or None,
                change_summary=None,
                restored_from=restored_from,
                created_at=datetime.utcnow(),
            )
            db.add(row)
            asset.current_version = next_number
            asset.source_path = source_path
            asset.content_hash = content_hash
            asset.original_filename = original_filename
            asset.processing = True
            asset.status = "approved"
            asset.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            return self._to_version_dto(row, next_number)
        finally:
            if not self._db:
                db.close()

    def update_version(
        self, doc_id: str, version_number: int, updates: dict[str, Any]
    ) -> AssetVersionInfo | None:
        db = self._get_db()
        try:
            row = (
                db.query(AssetVersion)
                .filter(
                    AssetVersion.doc_id == doc_id,
                    AssetVersion.version_number == version_number,
                )
                .one_or_none()
            )
            if not row:
                return None
            for key, value in updates.items():
                if key in {"note", "change_summary", "size_bytes"}:
                    setattr(row, key, value)
            db.commit()
            db.refresh(row)
            asset = db.get(Asset, doc_id)
            current = (asset.current_version if asset else None) or 1
            return self._to_version_dto(row, current)
        finally:
            if not self._db:
                db.close()


class SQLUserRegistry:
    def __init__(self, db: Session | None = None):
        self._db = db
        # Ensure tables exist
        # Ensure tables exist - Handled via Alembic Migrations

    def _get_db(self):
        return self._db if self._db else SessionLocal()

    def get_user(self, email: str) -> UserProfile | None:
        db = self._get_db()
        try:
            user = db.get(User, email)
            return self._to_user_dto(user) if user else None
        finally:
            if not self._db:
                db.close()

    def register_user(
        self,
        email: str,
        name: str,
        id: str,
        roles: list[str] | None = None,
        department: str = "general",
        picture: str | None = None,
    ) -> UserProfile:
        db = self._get_db()
        try:
            if roles is None:
                roles = ["user"]

            user = db.get(User, email)
            if user is not None and user.is_platform_support:
                raise ValueError(
                    "A platform support identity cannot be registered as a user"
                )
            if not user:
                user = User(
                    email=email,
                    id=id,
                    name=name,
                    picture=picture,
                    department=department,
                    workspace_groups=[],
                )
                db.add(user)
            else:
                user.name = name
                user.id = id
                if picture:
                    user.picture = picture
                user.department = department

            # Map roles
            user.roles = []  # Clear existing roles for register_user (overwrite)
            for role_name in roles:
                role = db.get(Role, role_name)
                if not role:
                    role = Role(name=role_name)
                    db.add(role)
                user.roles.append(role)

            db.commit()
            db.refresh(user)
            return self._to_user_dto(user)
        finally:
            if not self._db:
                db.close()

    def update_user(self, email: str, updates: dict[str, Any]) -> UserProfile | None:
        db = self._get_db()
        try:
            user = db.get(User, email)
            if not user:
                return None
            if user.is_platform_support:
                raise ValueError(
                    "A platform support identity cannot be edited as a user"
                )

            if "name" in updates:
                user.name = updates["name"]
            if "picture" in updates:
                user.picture = updates["picture"]
            if "department" in updates:
                user.department = updates["department"]
            if "is_exec" in updates:
                user.is_exec = bool(updates["is_exec"])
            if "managed_departments" in updates:
                # Coerce to a clean list[str] — drop empties / duplicates
                # so the dropdown logic doesn't have to defend against
                # malformed input.
                raw = updates["managed_departments"] or []
                seen: set[str] = set()
                cleaned: list[str] = []
                for d in raw:
                    if isinstance(d, str) and d.strip() and d not in seen:
                        seen.add(d)
                        cleaned.append(d.strip())
                user.managed_departments = cleaned
            if "roles" in updates:
                user.roles = []
                for role_name in updates["roles"]:
                    role = db.get(Role, role_name)
                    if role:
                        user.roles.append(role)

            db.commit()
            db.refresh(user)
            return self._to_user_dto(user)
        finally:
            if not self._db:
                db.close()

    def record_login(
        self,
        email: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        window_minutes: int = 30,
    ) -> bool:
        """Record a login *session* for ``email`` and refresh ``last_login``.

        ``get_current_user`` runs on every authenticated request, so this is
        debounced: a new ``login_events`` row (and a ``last_login`` bump) is
        written only when the user's most recent login event is older than
        ``window_minutes``. A burst of requests inside the window therefore
        counts as a single login. ``window_minutes=0`` forces a new row
        (used in tests).

        Returns ``True`` when a row was written, ``False`` when suppressed as
        part of an in-progress session.
        """
        db = self._get_db()
        try:
            now = datetime.utcnow()
            latest = (
                db.query(LoginEvent.timestamp)
                .filter(LoginEvent.user_email == email)
                .order_by(LoginEvent.timestamp.desc())
                .first()
            )
            if (
                latest is not None
                and window_minutes > 0
                and (now - latest[0]) < timedelta(minutes=window_minutes)
            ):
                return False

            db.add(
                LoginEvent(
                    user_email=email,
                    timestamp=now,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            )
            # Keep the denormalized convenience column in sync with the start
            # of the user's most recent session.
            user = db.get(User, email)
            if user:
                user.last_login = now

            db.commit()
            return True
        finally:
            if not self._db:
                db.close()

    def list_users(self) -> list[UserProfile]:
        db = self._get_db()
        try:
            users = db.query(User).filter(User.is_platform_support.is_(False)).all()
            return [self._to_user_dto(u) for u in users]
        finally:
            if not self._db:
                db.close()

    def delete_user(self, email: str) -> bool:
        db = self._get_db()
        try:
            user = db.get(User, email)
            if user:
                db.delete(user)
                db.commit()
                return True
            return False
        finally:
            if not self._db:
                db.close()

    def get_roles(self) -> list[RoleDTO]:
        db = self._get_db()
        try:
            roles = db.query(Role).all()
            return [self._to_role_dto(r) for r in roles]
        finally:
            if not self._db:
                db.close()

    def add_role(self, role_dto: RoleDTO | str) -> list[RoleDTO]:
        db = self._get_db()
        try:
            if isinstance(role_dto, str):
                role_dto = RoleDTO(name=role_dto, description="", permissions={})

            role = Role(name=role_dto.name, description=role_dto.description)
            for res, p in role_dto.permissions.items():
                perm = Permission(
                    resource=res,
                    can_create=p.create,
                    can_read=p.read,
                    can_update=p.update,
                    can_delete=p.delete,
                )
                role.permissions.append(perm)

            db.merge(role)
            db.commit()
            return self.get_roles()
        finally:
            if not self._db:
                db.close()

    def update_role(self, role_name: str, updates: dict[str, Any]) -> RoleDTO | None:
        db = self._get_db()
        try:
            role = db.get(Role, role_name)
            if not role:
                return None

            if "description" in updates:
                role.description = updates["description"]

            if "permissions" in updates:
                # Clear and recreate permissions
                db.execute(delete(Permission).where(Permission.role_name == role_name))
                for res, p in updates["permissions"].items():
                    # Assuming p is a dict or ResourcePermissions
                    perm = Permission(
                        role_name=role_name,
                        resource=res,
                        can_create=p.get("create", False)
                        if isinstance(p, dict)
                        else p.create,
                        can_read=p.get("read", False)
                        if isinstance(p, dict)
                        else p.read,
                        can_update=p.get("update", False)
                        if isinstance(p, dict)
                        else p.update,
                        can_delete=p.get("delete", False)
                        if isinstance(p, dict)
                        else p.delete,
                    )
                    db.add(perm)

            db.commit()
            db.refresh(role)
            return self._to_role_dto(role)
        finally:
            if not self._db:
                db.close()

    def delete_role(self, role_name: str) -> list[RoleDTO]:
        db = self._get_db()
        try:
            role = db.get(Role, role_name)
            if role:
                db.delete(role)
                db.commit()
            return self.get_roles()
        finally:
            if not self._db:
                db.close()

    def get_workspace_settings(self) -> dict[str, Any]:
        db = self._get_db()
        try:
            setting = db.get(WorkspaceSetting, "general")
            saved = setting.value if setting else {}
            # Old tenant rows may still carry these keys. Identity is owned by
            # the control database, so never serve stale editable values.
            allowed = {
                "workspace_sync_enabled",
                "session_lifetime_hours",
                "multi_gmail_enabled",
            }
            return {
                "workspace_sync_enabled": False,
                "multi_gmail_enabled": False,
                **{key: value for key, value in saved.items() if key in allowed},
            }
        finally:
            if not self._db:
                db.close()

    def update_workspace_settings(self, settings: dict[str, Any]):
        allowed = {
            "workspace_sync_enabled",
            "session_lifetime_hours",
            "multi_gmail_enabled",
        }
        unknown = set(settings) - allowed
        if unknown:
            raise ValueError(
                f"Workspace identity cannot be edited here: {sorted(unknown)}"
            )
        db = self._get_db()
        try:
            existing = self.get_workspace_settings()
            existing.update(settings)

            setting = WorkspaceSetting(key="general", value=existing)
            db.merge(setting)
            db.commit()
        finally:
            if not self._db:
                db.close()

    def get_group_mappings(self) -> dict[str, list[str]]:
        db = self._get_db()
        try:
            setting = db.get(WorkspaceSetting, "group_mappings")
            return setting.value if setting else {}
        finally:
            if not self._db:
                db.close()

    def update_group_mappings(self, mappings: dict[str, list[str]]):
        db = self._get_db()
        try:
            setting = WorkspaceSetting(key="group_mappings", value=mappings)
            db.merge(setting)
            db.commit()
        finally:
            if not self._db:
                db.close()

    def _to_user_dto(self, user: User) -> UserProfile:
        return UserProfile(
            id=user.id,
            email=user.email,
            name=user.name,
            picture=user.picture,
            department=user.department,
            roles=[r.name for r in user.roles],
            created_at=user.created_at.isoformat(),
            updated_at=user.updated_at.isoformat(),
            last_login=user.last_login.isoformat() if user.last_login else None,
            last_ingest=user.last_ingest.isoformat() if user.last_ingest else None,
            workspace_groups=user.workspace_groups,
            groups=[g.name for g in user.groups],
            is_exec=bool(getattr(user, "is_exec", False)),
            is_platform_support=bool(getattr(user, "is_platform_support", False)),
            managed_departments=list(getattr(user, "managed_departments", None) or []),
        )

    def _to_role_dto(self, role: Role) -> RoleDTO:
        perms = {}
        for p in role.permissions:
            perms[p.resource] = PermDTO(
                create=p.can_create,
                read=p.can_read,
                update=p.can_update,
                delete=p.can_delete,
            )
        return RoleDTO(name=role.name, description=role.description, permissions=perms)


# RFC 2606 / 6761 reserved & example domains. Meet/Gemini transcripts sometimes
# mention placeholder addresses (someone reads out "user@example.com", a template
# leaks "nobody@example.com"); the email-regex scraper picked these up and seeded
# them into the ACL. They can never authenticate (login is domain-restricted) so
# they're inert, but they're noise — drop them wherever participant emails are
# written.
_RESERVED_EMAIL_DOMAINS = {"example.com", "example.net", "example.org", "example.edu"}
_RESERVED_EMAIL_TLDS = {"test", "example", "invalid", "localhost"}


def is_placeholder_email(email: str | None) -> bool:
    """True for an unusable / reserved-domain address that must never be an ACL entry.

    The configured Workspace domain is never a placeholder, even when it is a
    reserved name (a local or test install on ``acme.example``): its people
    sign in, so their addresses are real here.
    """
    if not email or "@" not in email:
        return True
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain:
        return True
    from rubik_core.company.domain import workspace_domains  # local: keep it light

    if domain in workspace_domains():
        return False
    if domain in _RESERVED_EMAIL_DOMAINS:
        return True
    return domain.rsplit(".", 1)[-1] in _RESERVED_EMAIL_TLDS


class SharedMeeting(NamedTuple):
    """One transcript the reader attended together with ≥1 of the counterparts.

    ``when`` is ``Asset.created_at`` — the ingest-time meeting-date proxy (see
    ``list_recent_doc_ids_for_user``). ``overlap`` lists which counterparts had
    attendance rows for the doc (sorted, lowercased emails).
    """

    doc_id: str
    title: str | None
    when: datetime | None
    overlap: list[str]


class SQLMeetingParticipantsRepository:
    """Authoritative ACL store for meeting transcripts.

    Use this anywhere participant-level access is decided. Do not check participant
    access via Asset.allowed_emails alone — that field is a denormalized cache used
    only by the vector store metadata filter. This table is the source of truth.
    """

    def __init__(self, db: Session | None = None):
        self._db = db

    def _get_db(self):
        return self._db if self._db else SessionLocal()

    def upsert_participants(
        self, doc_id: str, meeting_id: str, emails: list[str]
    ) -> None:
        """Replace the participant set for a transcript with the given emails.

        Reserved/placeholder addresses (``is_placeholder_email``) are dropped so
        scraped junk like ``user@example.com`` never lands in the ACL.
        """
        normalized = sorted(
            {
                e_norm
                for e_norm in ((x or "").strip().lower() for x in emails)
                if e_norm and "@" in e_norm and not is_placeholder_email(e_norm)
            }
        )
        if not doc_id:
            return
        db = self._get_db()
        try:
            db.query(MeetingParticipant).filter(
                MeetingParticipant.doc_id == doc_id
            ).delete(synchronize_session=False)
            for email in normalized:
                db.add(
                    MeetingParticipant(
                        doc_id=doc_id,
                        participant_email=email,
                        meeting_id=meeting_id,
                        created_at=datetime.utcnow(),
                    )
                )
            db.commit()
        finally:
            if not self._db:
                db.close()

        # Mirror the attendance into the relationship graph. Deliberately AFTER
        # the commit and outside the try/finally, in its own session: this table
        # is the authoritative transcript ACL, and a graph bug must not be able
        # to roll back or poison the transaction that wrote it. The writer is
        # itself fail-soft, so this call cannot raise.
        from rubik_core.graph.writers import record_attendance

        record_attendance(doc_id, meeting_id, normalized)

    def delete_for_doc(self, doc_id: str) -> int:
        db = self._get_db()
        try:
            count = (
                db.query(MeetingParticipant)
                .filter(MeetingParticipant.doc_id == doc_id)
                .delete(synchronize_session=False)
            )
            db.commit()
            return count
        finally:
            if not self._db:
                db.close()

    def has_access(self, doc_id: str, email: str) -> bool:
        # Access boundary: a single positive lookup grants meeting Q&A access.
        # No fallbacks — if there's no row here, the user wasn't a participant.
        if not email or not doc_id:
            return False
        db = self._get_db()
        try:
            row = (
                db.query(MeetingParticipant)
                .filter(
                    MeetingParticipant.doc_id == doc_id,
                    MeetingParticipant.participant_email == email.strip().lower(),
                )
                .first()
            )
            return row is not None
        finally:
            if not self._db:
                db.close()

    def list_doc_ids_for_user(self, email: str) -> list[str]:
        if not email:
            return []
        db = self._get_db()
        try:
            rows = (
                db.query(MeetingParticipant.doc_id)
                .filter(MeetingParticipant.participant_email == email.strip().lower())
                .all()
            )
            return [r[0] for r in rows]
        finally:
            if not self._db:
                db.close()

    def list_recent_doc_ids_for_user(self, email: str, limit: int = 5) -> list[str]:
        """Doc IDs of the user's accessible transcripts, most recent first.

        Recency = ``Asset.created_at`` descending — ingest time is the best
        available proxy for the meeting date (Fathom/Drive sync ingests a
        transcript right after the meeting ends; there is no separate
        meeting-date column). Only live transcripts are returned (status
        ``approved``), excluding soft-deleted/failed ones. Same ACL as
        ``list_doc_ids_for_user``: filtered to meetings the user attended.
        """
        if not email:
            return []
        db = self._get_db()
        try:
            rows = (
                db.query(MeetingParticipant.doc_id)
                .join(Asset, Asset.doc_id == MeetingParticipant.doc_id)
                .filter(MeetingParticipant.participant_email == email.strip().lower())
                .filter(Asset.status == "approved")
                .order_by(Asset.created_at.desc())
                .limit(max(1, limit))
                .all()
            )
            return [r[0] for r in rows]
        finally:
            if not self._db:
                db.close()

    def list_shared_meetings(
        self,
        reader_email: str,
        counterpart_emails: list[str],
        limit: int = 8,
    ) -> list[SharedMeeting]:
        """Transcripts the reader attended together with ≥1 counterpart, newest first.

        Phase 0 of the customer relationship graph
        (docs/customer_relationship_graph.md). ACL-safe by construction: only
        docs where the READER has a meeting_participants row are eligible — the
        same gate ``search_past_meetings`` enforces, so a counterpart-only
        meeting can never surface. Recency = ``Asset.created_at`` (the
        ingest-time meeting-date proxy, as in ``list_recent_doc_ids_for_user``);
        only live transcripts (status ``approved``) are returned. Grouping
        happens in Python so the query also runs on the sqlite test DB (no
        ARRAY_AGG).
        """
        reader = (reader_email or "").strip().lower()
        counterparts = sorted(
            {
                e_norm
                for e_norm in ((x or "").strip().lower() for x in counterpart_emails)
                if e_norm and "@" in e_norm and e_norm != reader
            }
        )
        if not reader or not counterparts:
            return []
        db = self._get_db()
        try:
            mp_reader = aliased(MeetingParticipant)
            mp_other = aliased(MeetingParticipant)
            rows = (
                db.query(
                    mp_reader.doc_id,
                    mp_other.participant_email,
                    Asset.original_filename,
                    Asset.created_at,
                )
                .join(mp_other, mp_other.doc_id == mp_reader.doc_id)
                .join(Asset, Asset.doc_id == mp_reader.doc_id)
                .filter(
                    mp_reader.participant_email == reader,
                    mp_other.participant_email.in_(counterparts),
                    Asset.status == "approved",
                )
                .all()
            )
            grouped: dict[str, dict[str, Any]] = {}
            for doc_id, other_email, title, created_at in rows:
                entry = grouped.setdefault(
                    doc_id, {"title": title, "when": created_at, "overlap": set()}
                )
                entry["overlap"].add(other_email)
            ordered = sorted(
                grouped.items(),
                key=lambda kv: kv[1]["when"] or datetime.min,
                reverse=True,
            )
            return [
                SharedMeeting(
                    doc_id=doc_id,
                    title=entry["title"],
                    when=entry["when"],
                    overlap=sorted(entry["overlap"]),
                )
                for doc_id, entry in ordered[: max(0, limit)]
            ]
        finally:
            if not self._db:
                db.close()

    def list_all_transcript_doc_ids(self) -> list[str]:
        """Every approved Meeting-Transcript doc_id, IGNORING the participant
        ACL. For org-wide execs (``sees_all_departments``) only — callers must
        gate on that flag before calling this."""
        db = self._get_db()
        try:
            rows = (
                db.query(Asset.doc_id)
                .filter(Asset.topic == "Meeting Transcripts")
                .filter(Asset.status == "approved")
                .all()
            )
            return [r[0] for r in rows]
        finally:
            if not self._db:
                db.close()

    def list_all_recent_transcript_doc_ids(self, limit: int = 5) -> list[str]:
        """Approved Meeting-Transcript doc_ids, most recent first, IGNORING the
        participant ACL. Org-wide-exec counterpart of
        ``list_recent_doc_ids_for_user``; gate on ``sees_all_departments``."""
        db = self._get_db()
        try:
            rows = (
                db.query(Asset.doc_id)
                .filter(Asset.topic == "Meeting Transcripts")
                .filter(Asset.status == "approved")
                .order_by(Asset.created_at.desc())
                .limit(max(1, limit))
                .all()
            )
            return [r[0] for r in rows]
        finally:
            if not self._db:
                db.close()

    def list_participants_for_doc(self, doc_id: str) -> list[str]:
        db = self._get_db()
        try:
            rows = (
                db.query(MeetingParticipant.participant_email)
                .filter(MeetingParticipant.doc_id == doc_id)
                .all()
            )
            return [r[0] for r in rows]
        finally:
            if not self._db:
                db.close()

    def meeting_id_for_doc(self, doc_id: str) -> str | None:
        """The external ``meeting_id`` already recorded for this transcript.

        Used by the participant backfill to preserve the original meeting id
        (the Drive/Fathom source id) when re-upserting the ACL, rather than
        inventing a new one.
        """
        db = self._get_db()
        try:
            row = (
                db.query(MeetingParticipant.meeting_id)
                .filter(MeetingParticipant.doc_id == doc_id)
                .first()
            )
            return row[0] if row else None
        finally:
            if not self._db:
                db.close()


def log_audit_action(
    actor_email: str, action: str, doc_id: str, status: str, details: Any = None
):
    db = SessionLocal()
    try:
        log = AuditLog(
            actor_email=actor_email,
            action=action,
            doc_id=doc_id,
            status=status,
            details=details,
        )
        db.add(log)
        db.commit()
    finally:
        db.close()


class SQLMetadataRegistry:
    def __init__(self, db: Session | None = None):
        self._db = db

    def _get_db(self):
        return self._db if self._db else SessionLocal()

    # Topics
    def get_topics(self) -> list[str]:
        db = self._get_db()
        try:
            topics = db.query(models.Topic).all()
            return [t.name for t in topics]
        finally:
            if not self._db:
                db.close()

    def add_topic(self, topic: str) -> bool:
        db = self._get_db()
        try:
            if db.get(models.Topic, topic):
                return False
            db.add(models.Topic(name=topic))
            db.commit()
            return True
        finally:
            if not self._db:
                db.close()

    def remove_topic(self, topic: str) -> bool:
        db = self._get_db()
        try:
            t = db.get(models.Topic, topic)
            if t:
                db.delete(t)
                db.commit()
                return True
            return False
        finally:
            if not self._db:
                db.close()

    # Drive label priorities (admin-managed map for the Drive AI-Priorities signal)
    @staticmethod
    def _drive_label_id(label_key: str | None, label_value: str) -> str:
        """Deterministic slug PK so the same label can't be added twice."""
        return f"{(label_key or '').strip().lower()}:{label_value.strip().lower()}"

    @staticmethod
    def _to_drive_label_dto(
        row: models.DriveLabelPriority,
    ) -> schemas.DriveLabelPriority:
        return schemas.DriveLabelPriority(
            id=row.id,
            label_key=row.label_key,
            label_value=row.label_value,
            priority=row.priority,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def list_drive_label_priorities(self) -> list[schemas.DriveLabelPriority]:
        db = self._get_db()
        try:
            rows = db.query(models.DriveLabelPriority).all()
            return [self._to_drive_label_dto(r) for r in rows]
        finally:
            if not self._db:
                db.close()

    def upsert_drive_label_priority(
        self, *, label_value: str, label_key: str | None = None, priority: float = 1.0
    ) -> schemas.DriveLabelPriority:
        db = self._get_db()
        try:
            row_id = self._drive_label_id(label_key, label_value)
            row = db.get(models.DriveLabelPriority, row_id)
            if row is None:
                row = models.DriveLabelPriority(
                    id=row_id,
                    label_key=(label_key or None),
                    label_value=label_value.strip(),
                    priority=priority,
                )
                db.add(row)
            else:
                row.priority = priority
            db.commit()
            db.refresh(row)
            return self._to_drive_label_dto(row)
        finally:
            if not self._db:
                db.close()

    def update_drive_label_priority(
        self, row_id: str, *, priority: float
    ) -> schemas.DriveLabelPriority | None:
        db = self._get_db()
        try:
            row = db.get(models.DriveLabelPriority, row_id)
            if row is None:
                return None
            row.priority = priority
            db.commit()
            db.refresh(row)
            return self._to_drive_label_dto(row)
        finally:
            if not self._db:
                db.close()

    def remove_drive_label_priority(self, row_id: str) -> bool:
        db = self._get_db()
        try:
            row = db.get(models.DriveLabelPriority, row_id)
            if row:
                db.delete(row)
                db.commit()
                return True
            return False
        finally:
            if not self._db:
                db.close()

    def drive_label_priority_map(self) -> dict[str, float]:
        """Lookup map for the signal source, keyed by lowercased ``label_value``.

        Matching is on the value alone (case-insensitive), since a file's applied
        Drive label surfaces as a selection value (the field/key is often noise).
        If two rows share a value under different keys, the higher weight wins.
        """
        db = self._get_db()
        try:
            out: dict[str, float] = {}
            for r in db.query(models.DriveLabelPriority).all():
                key = (r.label_value or "").strip().lower()
                if not key:
                    continue
                out[key] = max(out.get(key, 0.0), float(r.priority or 0.0))
            return out
        finally:
            if not self._db:
                db.close()

    # Departments
    def get_departments(self) -> list[str]:
        db = self._get_db()
        try:
            depts = db.query(models.Department).all()
            return [d.name for d in depts]
        finally:
            if not self._db:
                db.close()

    def add_department(self, department: str) -> bool:
        # A department's identity is its RAG slug, so "Sales", "sales", and
        # " Sales " are the SAME team — they all map to the `sales_rag` corpus.
        # Reject any case/slug-equivalent of an existing row, not just an exact
        # PK collision; otherwise the list accumulates "Sales"/"sales" duplicates
        # that silently split file access on the exact-match ACL path.
        from rubik_core.rag.config import slugify_department

        db = self._get_db()
        try:
            if db.get(models.Department, department):
                return False
            new_slug = slugify_department(department)
            if new_slug:
                existing_slugs = {
                    slugify_department(d.name)
                    for d in db.query(models.Department).all()
                }
                if new_slug in existing_slugs:
                    return False
            db.add(models.Department(name=department))
            db.commit()
            return True
        finally:
            if not self._db:
                db.close()

    def remove_department(self, department: str) -> bool:
        db = self._get_db()
        try:
            d = db.get(models.Department, department)
            if d:
                db.delete(d)
                db.commit()
                return True
            return False
        finally:
            if not self._db:
                db.close()

    # Groups
    def get_groups(self) -> list[GroupDTO]:
        db = self._get_db()
        try:
            groups = db.query(models.Group).all()
            return [
                GroupDTO(
                    id=g.id,
                    name=g.name,
                    description=g.description,
                    created_at=g.created_at,
                )
                for g in groups
            ]
        finally:
            if not self._db:
                db.close()

    def add_group(self, name: str, description: str | None = None) -> GroupDTO:
        db = self._get_db()
        import uuid

        try:
            group = models.Group(
                id=str(uuid.uuid4()), name=name, description=description
            )
            db.add(group)
            db.commit()
            db.refresh(group)
            return GroupDTO(
                id=group.id,
                name=group.name,
                description=group.description,
                created_at=group.created_at,
            )
        finally:
            if not self._db:
                db.close()

    def delete_group(self, group_id: str) -> bool:
        db = self._get_db()
        try:
            group = db.get(models.Group, group_id)
            if group:
                db.delete(group)
                db.commit()
                return True
            return False
        finally:
            if not self._db:
                db.close()

    def add_user_to_group(self, email: str, group_id: str) -> bool:
        db = self._get_db()
        try:
            user = db.get(User, email)
            if user is not None and user.is_platform_support:
                raise ValueError("Platform support identity is reserved")
            group = db.get(models.Group, group_id)
            if user and group and group not in user.groups:
                user.groups.append(group)
                db.commit()
                return True
            return False
        finally:
            if not self._db:
                db.close()

    def remove_user_from_group(self, email: str, group_id: str) -> bool:
        db = self._get_db()
        try:
            user = db.get(User, email)
            group = db.get(models.Group, group_id)
            if user and group and group in user.groups:
                user.groups.remove(group)
                db.commit()
                return True
            return False
        finally:
            if not self._db:
                db.close()

    # Access Policies
    def get_access_policies(
        self, user_email: str | None = None, group_id: str | None = None
    ) -> list[PolicyDTO]:
        db = self._get_db()
        try:
            query = db.query(models.AccessPolicy)
            if user_email:
                query = query.filter(models.AccessPolicy.user_email == user_email)
            if group_id:
                query = query.filter(models.AccessPolicy.group_id == group_id)

            policies = query.all()
            return [
                PolicyDTO(
                    id=p.id,
                    user_email=p.user_email,
                    group_id=p.group_id,
                    department=p.department,
                    topic=p.topic,
                    can_read=p.can_read,
                    can_update=p.can_update,
                    created_at=p.created_at,
                )
                for p in policies
            ]
        finally:
            if not self._db:
                db.close()

    def add_access_policy(
        self,
        user_email: str | None = None,
        group_id: str | None = None,
        department: str | None = None,
        topic: str | None = None,
        can_read: bool = True,
        can_update: bool = False,
    ) -> PolicyDTO:
        db = self._get_db()
        try:
            policy = models.AccessPolicy(
                user_email=user_email,
                group_id=group_id,
                department=department,
                topic=topic,
                can_read=can_read,
                can_update=can_update,
            )
            db.add(policy)
            db.commit()
            db.refresh(policy)
            return PolicyDTO(
                id=policy.id,
                user_email=policy.user_email,
                group_id=policy.group_id,
                department=policy.department,
                topic=policy.topic,
                can_read=policy.can_read,
                can_update=policy.can_update,
                created_at=policy.created_at,
            )
        finally:
            if not self._db:
                db.close()

    def delete_access_policy(self, policy_id: int) -> bool:
        db = self._get_db()
        try:
            policy = db.get(models.AccessPolicy, policy_id)
            if policy:
                db.delete(policy)
                db.commit()
                return True
            return False
        finally:
            if not self._db:
                db.close()

    def get_user_access_policies(self, email: str) -> list[PolicyDTO]:
        db = self._get_db()
        try:
            user = db.get(User, email)
            if not user:
                return []

            group_ids = [g.id for g in user.groups]

            from sqlalchemy import or_

            query = db.query(models.AccessPolicy)

            # Build filters
            filters = [models.AccessPolicy.user_email == email]
            if group_ids:
                filters.append(models.AccessPolicy.group_id.in_(group_ids))

            policies = query.filter(or_(*filters)).all()

            return [
                PolicyDTO(
                    id=p.id,
                    user_email=p.user_email,
                    group_id=p.group_id,
                    department=p.department,
                    topic=p.topic,
                    can_read=p.can_read,
                    can_update=p.can_update,
                    created_at=p.created_at,
                )
                for p in policies
            ]
        finally:
            if not self._db:
                db.close()
