from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    desc,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, REAL
from sqlalchemy.orm import DeclarativeBase, relationship

from rubik_core.database.encrypted_string import EncryptedString


class Base(DeclarativeBase):
    pass


# Many-to-Many relationship between Users and Roles.
# ON DELETE CASCADE: role membership is pure config — meaningless
# without the user — so offboarding (which deletes the users row) sweeps it.
user_roles = Table(
    "user_roles",
    Base.metadata,
    Column(
        "user_email",
        String,
        ForeignKey("users.email", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("role_name", String, ForeignKey("roles.name"), primary_key=True),
)

# Many-to-Many relationship between Users and Groups (CASCADE — see user_roles).
group_members = Table(
    "group_members",
    Base.metadata,
    Column(
        "user_email",
        String,
        ForeignKey("users.email", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("group_id", String, ForeignKey("groups.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    email = Column(String, primary_key=True)
    id = Column(String, nullable=False)  # Internal ID (e.g. manual or Google sub)
    name = Column(String, nullable=False)
    picture = Column(String, nullable=True)
    department = Column(String, default="general")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)
    last_ingest = Column(DateTime, nullable=True)
    workspace_groups = Column(JSON, default=list)
    # "Is part of the Exec team" — toggled in the user create/edit modal.
    # Members of the Exec team can edit the VTO; everyone else sees it
    # read-only per the EOS framework.
    is_exec = Column(Boolean, default=False, nullable=False)
    # A platform admin's FK-backed target identity. It grants no target role
    # and must never appear in employee rosters or mailbox batch jobs.
    is_platform_support = Column(
        Boolean, default=False, nullable=False, server_default=false()
    )
    # Departments this user is the team owner / integrator of, per the EOS
    # Accountability Chart. Drives the meeting-scope dropdown: an Exec
    # member with ``["Systems", "Compliance"]`` here can schedule L10s for
    # the Exec team OR for either of those departments. Regular users
    # leave this empty — their only scope is their own ``department``.
    # Stored as JSON list of department names so we don't need a separate
    # join table for a feature most users won't touch.
    managed_departments = Column(JSON, default=list, nullable=True)

    roles = relationship("Role", secondary=user_roles, back_populates="users")
    groups = relationship("Group", secondary=group_members, back_populates="users")
    assets = relationship("Asset", back_populates="owner")


class Role(Base):
    __tablename__ = "roles"

    name = Column(String, primary_key=True)
    description = Column(String, default="")

    users = relationship("User", secondary=user_roles, back_populates="roles")
    permissions = relationship(
        "Permission", back_populates="role", cascade="all, delete-orphan"
    )


class Permission(Base):
    """RBAC permission row attached to a role.

    Two co-existing shapes — both rows live in the same table:

    1. Legacy CRUD-boolean shape (``action`` is NULL). The row grants any
       subset of {create, read, update, delete} on ``resource`` (e.g.
       "assets", "topics"). Existing routers check via ``has_permission(
       user, resource, "read")`` and continue to work unchanged.

    2. Fine-grained action shape (``action`` is set). The row grants a
       single named capability identified by a string like
       ``"deal:advance_stage"`` or ``"notification:admin_view"``. The
       CRUD booleans are ignored for these rows. Callers check via
       ``has_permission(user, action="deal:advance_stage")`` (resource
       optional and ignored when ``action`` is supplied).

    The split exists so v2 features can ship fine-grained capabilities
    without disturbing the resource-CRUD path that 100% of v1 routers
    rely on.
    """

    __tablename__ = "permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role_name = Column(String, ForeignKey("roles.name"), nullable=False)
    resource = Column(String, nullable=False)  # e.g. "assets", "topics"
    can_create = Column(Boolean, default=False)
    can_read = Column(Boolean, default=False)
    can_update = Column(Boolean, default=False)
    can_delete = Column(Boolean, default=False)
    # v2: optional fine-grained action string (e.g. "deal:advance_stage").
    # NULL → row keeps the legacy CRUD-boolean semantics above.
    action = Column(String(64), nullable=True)

    role = relationship("Role", back_populates="permissions")


class MCPConnection(Base):
    __tablename__ = "mcp_connections"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    kind = Column(
        String, nullable=False
    )  # "google_remote", "toolbox", "custom", "local_stdio"
    command = Column(String, nullable=True)  # e.g. "python" (for local)
    args = Column(
        JSON, nullable=True
    )  # e.g. ["rubik_core/mcp_servers/hubspot_server.py"]
    server_url = Column(String, nullable=True)  # for remote
    auth_mode = Column(
        String, default="iam"
    )  # "iam", "oauth", "service_account_ref", "none"
    service_account_ref = Column(String, nullable=True)
    allowed_tools = Column(JSON, default=list)
    read_only = Column(Boolean, default=True)
    enabled = Column(Boolean, default=True)
    environment = Column(String, default="dev")  # "dev", "staging", "prod"
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Asset(Base):
    __tablename__ = "assets"

    doc_id = Column(String, primary_key=True)
    owner_email = Column(String, ForeignKey("users.email"), nullable=False)
    approver_email = Column(String, nullable=True)
    # Lifecycle marker. Uploads now land "approved" (live in the RAG) — there is
    # no approval/review step. "archived" means the doc has been pulled from the
    # vector store (kept here as a record); "failed" means ingestion errored.
    status = Column(String, default="approved")
    topic = Column(String, nullable=True)
    department = Column(String, nullable=True)
    # Free-text summary of the document. May be authored by the uploader or
    # auto-generated from the file's text at upload time (see /api/dam/describe).
    description = Column(String, nullable=True)
    # Free-form keyword tags (list[str]) beyond the single required topic.
    tags = Column(JSON, default=list)
    original_filename = Column(String, nullable=True)
    source_path = Column(String, nullable=True)
    is_company_wide = Column(Boolean, default=False)
    allowed_emails = Column(JSON, default=list)
    # SHA-256 of the raw uploaded bytes — used for deduplication. Indexed for fast lookup.
    content_hash = Column(String, nullable=True, index=True)
    # True while RAG ingestion is in flight; flipped to False once the file is queryable.
    processing = Column(Boolean, default=False, nullable=False)
    # Version counter. ``source_path`` / ``content_hash`` /
    # ``original_filename`` always describe the CURRENT version; every version
    # ever uploaded — this one included — has a row in ``asset_versions``.
    current_version = Column(Integer, default=1, nullable=False, server_default="1")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", back_populates="assets")
    chunks = relationship("Chunk", back_populates="asset", cascade="all, delete-orphan")
    versions = relationship(
        "AssetVersion",
        back_populates="asset",
        cascade="all, delete-orphan",
        order_by="AssetVersion.version_number",
    )


class AssetVersion(Base):
    """One uploaded revision of an asset.

    The asset row keeps its ``doc_id`` across versions — bookmarks, shares,
    chat citations and ACLs all key on it — while each revision's bytes live at
    their own ``source_path`` and are never deleted while the asset exists, so
    any version can be downloaded or restored. Only the current version is in
    the RAG; superseded versions are purged from the corpus but kept on disk.
    """

    __tablename__ = "asset_versions"
    __table_args__ = (
        UniqueConstraint("doc_id", "version_number", name="uq_asset_versions_doc_ver"),
    )

    id = Column(String, primary_key=True)
    doc_id = Column(
        String,
        ForeignKey("assets.doc_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number = Column(Integer, nullable=False)
    source_path = Column(String, nullable=True)
    original_filename = Column(String, nullable=True)
    content_hash = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    uploaded_by = Column(String, nullable=True)
    # Free-text note the uploader typed ("Q3 pricing update").
    note = Column(String, nullable=True)
    # AI-written "what changed vs. the previous version" — filled in during
    # background ingest; NULL for version 1 and when the comparison failed.
    change_summary = Column(Text, nullable=True)
    # For a restore: the version whose bytes this one re-publishes.
    restored_from = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    asset = relationship("Asset", back_populates="versions")


class Chunk(Base):
    __tablename__ = "chunks"

    id = Column(String, primary_key=True)  # doc_id + "_" + chunk_index
    doc_id = Column(String, ForeignKey("assets.doc_id"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    text = Column(String, nullable=False)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    asset = relationship("Asset", back_populates="chunks")


class MeetingParticipant(Base):
    """Authoritative ACL for meeting transcripts.

    One row per (transcript doc_id, participant email). Populated at ingest time
    from the meeting attendee list. Retrieval and DELETE endpoints consult this
    table — never cross-reference with anything else — to decide who may query
    a meeting. Asset.allowed_emails is a denormalized cache for vector-store
    metadata filtering only; this table is the source of truth.
    """

    __tablename__ = "meeting_participants"

    doc_id = Column(
        String, ForeignKey("assets.doc_id", ondelete="CASCADE"), primary_key=True
    )
    participant_email = Column(String, primary_key=True, index=True)
    meeting_id = Column(String, nullable=False, index=True)  # external Meet/Fathom ID
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    actor_email = Column(String, nullable=False)
    action = Column(String, nullable=False)
    doc_id = Column(String, nullable=True)
    status = Column(String, nullable=False)  # success, failure
    details = Column(JSON, nullable=True)


class LoginEvent(Base):
    """Append-only record of user sign-in *sessions* (who logged in, when).

    ``get_current_user`` validates the OAuth token on **every** authenticated
    request, so a naive write-per-auth would log thousands of rows per user
    per day. Instead the writer (``SQLUserRegistry.record_login``) debounces:
    a row is inserted only when the user has no login event within a trailing
    session window, collapsing a burst of requests into one "login". Admins
    read this via ``GET /api/admin/logins``.

    Not foreign-keyed to ``users.email`` (same rationale as ``Notification``):
    this is an audit trail, so deleting a user must not cascade-delete the
    record that they once had access, and a missing/virtual user row must not
    block the write.
    """

    __tablename__ = "login_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_email = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    # Client IP (first hop of X-Forwarded-For behind Cloud Run) + raw
    # User-Agent string. Both best-effort — null when not derivable.
    ip_address = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)

    __table_args__ = (
        # Covers the writer's debounce probe ("latest event for this user")
        # and the admin's per-user filter, newest-first.
        Index("ix_login_events_email_ts", "user_email", "timestamp"),
    )


class WorkspaceSetting(Base):
    __tablename__ = "workspace_settings"

    key = Column(String, primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CatalogItem(Base):
    """One product or service the workspace sells (Admin → Company setup).

    Uploaded as a CSV and replaced wholesale on every upload
    (``rubik_core/company/catalog.py``). Empty by default. Proposals, X-Ray,
    Meeting Prep and chat read it. ``price_cents`` is optional: a service
    priced per engagement can leave it blank.
    """

    __tablename__ = "catalog_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: File order, so the admin sees the catalog the way they wrote it.
    position = Column(Integer, nullable=False, default=0)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=False, default="")
    category = Column(String(120), nullable=False, default="")
    price_cents = Column(Integer, nullable=True)
    unit = Column(String(40), nullable=False, default="")
    url = Column(String(500), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProposalTemplate(Base):
    """A named service write-up a proposal can carry (Admin → Company setup).

    ``body_md`` prints verbatim after the proposal's AI-written scope; the
    model never sees or edits it. ``catalog_item_names`` names catalog rows
    whose prices seed the proposal's pricing table. Empty by default.
    """

    __tablename__ = "proposal_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: Stable slug the brief stores (``brief["template"]["key"]``).
    key = Column(String(64), nullable=False, unique=True)
    name = Column(String(200), nullable=False)
    body_md = Column(Text, nullable=False, default="")
    catalog_item_names = Column(JSON, nullable=False, default=list)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class FeatureAccess(Base):
    """Admin override of WHO may reach one gated feature (Admin → Feature Access).

    One row per key in ``rubik_core.features.registry.FEATURES``. No row means
    the feature runs on the roster it shipped with (the spec's ``default``);
    a row replaces that roster wholesale. ``departments`` holds RAG slugs,
    ``extra_emails`` lower-cased addresses; the three booleans are the
    admin / Exec-team / everyone doors. See ``rubik_core/features/policy.py``
    for how a row is read (cached, fail-safe to the default).
    """

    __tablename__ = "feature_access"

    feature_key = Column(String, primary_key=True)
    departments = Column(JSON, nullable=False, default=list)
    extra_emails = Column(JSON, nullable=False, default=list)
    exec_allowed = Column(Boolean, nullable=False, default=False)
    admin_allowed = Column(Boolean, nullable=False, default=True)
    everyone = Column(Boolean, nullable=False, default=False)
    note = Column(String, nullable=True)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Topic(Base):
    __tablename__ = "topics"

    name = Column(String, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DriveLabelPriority(Base):
    """Admin-managed map: a Google Drive label value → importance weight.

    Drives the Drive-labels AI-Priorities signal (``backend/signals/drive.py``):
    when a rep recently updates a Drive file carrying one of these labels, the
    signal scores it by ``priority``. ``priority`` is a 1.0–2.0 weight (1.0 = no
    boost), clamped in the router; the signal source maps it to a Signal
    ``severity_hint``. ``label_key`` is the optional Drive label/field name —
    NULL means "match on the value alone". Identity is the slug ``id`` =
    ``"{label_key}:{label_value}"`` (both lowercased), so the same label can't be
    added twice. Seeded with Important / Cubie / Project = 1.5.
    """

    __tablename__ = "drive_label_priorities"

    id = Column(String, primary_key=True)
    label_key = Column(String, nullable=True)
    label_value = Column(String, nullable=False)
    priority = Column(Float, default=1.0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("label_key", "label_value", name="uq_drive_label_priority_kv"),
    )


class Department(Base):
    __tablename__ = "departments"

    name = Column(String, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Per-department opt-in for outbound Slack AI-Priorities delivery.
    # NULL / False = off. Admins toggle this (per-dept Settings).
    slack_priorities_enabled = Column(Boolean, nullable=True, default=False)
    # Channel (ID or #name) for the manager digest; the Cubie bot must be
    # a member. NULL = no manager digest for this dept.
    slack_manager_channel = Column(String, nullable=True)


class ChatFolder(Base):
    """A user-defined folder for organizing chat sessions (UAT-03).

    Per-user; a chat session belongs to at most one folder via
    ``ChatSession.folder_id``. Deleting a folder ungroups its chats (their
    ``folder_id`` is set NULL) rather than deleting them.
    """

    __tablename__ = "chat_folders"

    id = Column(String, primary_key=True)  # UUID string
    # Plain string (no users FK): chat history is a historical
    # artifact that must survive offboarding, which deletes the users row.
    user_email = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(String, primary_key=True)  # UUID string
    # Plain string — see ChatFolder.user_email.
    user_email = Column(String, nullable=False)
    title = Column(String, nullable=False, default="New Chat")
    # Optional organizing folder (UAT-03). NULL = "Ungrouped". FK is intentionally
    # without a DB-level ON DELETE rule for SQLite portability — the delete-folder
    # endpoint unsets this explicitly so chats are never cascaded away.
    folder_id = Column(
        String,
        ForeignKey("chat_folders.id", name="fk_chat_sessions_folder_id"),
        nullable=True,
        index=True,
    )
    last_adk_invocation_id = Column(String, nullable=True)  # For HITL resumption
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True)  # UUID string
    session_id = Column(String, ForeignKey("chat_sessions.id"), nullable=False)
    role = Column(String, nullable=False)  # "user" or "assistant"
    content = Column(String, nullable=False)
    metadata_json = Column(
        JSON, nullable=True
    )  # citations, hubspot_data, generated_sql, etc.
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("ChatSession", back_populates="messages")


class ChatUpload(Base):
    """A file a person attached in Ask Cubie, PRIVATE to them.

    Not an ``assets`` row, on purpose: every reader of ``assets`` (the Library
    and its admin view, the department fallback in ``has_document_access``,
    sharing, the offboarding hand-off) would otherwise have to remember a
    privacy flag. Here privacy is the table: every read goes through
    ``owner_email == caller`` in ``rubik_core/chat_files/service.py``.

    No foreign keys (to ``users`` or ``chat_sessions``): creating one locks the
    referenced table, and both are hot, so a migration adding one can stall
    every request.
    """

    __tablename__ = "chat_uploads"

    id = Column(String, primary_key=True)  # UUID string
    owner_email = Column(String, nullable=False, index=True)
    # The conversation it was first attached in; set when the message is sent.
    session_id = Column(String, nullable=True, index=True)
    filename = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # pdf | word | spreadsheet | …
    mime_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=False, default=0)
    # SHA-256 of the original: the same file attached twice is read once.
    content_hash = Column(String, nullable=False, index=True)
    storage_uri = Column(String, nullable=True)
    summary = Column(Text, nullable=True)
    # What Cubie read: the file's own text, or the model's reading of a picture
    # or scan. Capped at CHAT_FILE_STORED_TEXT_CHARS (``text_truncated``).
    extracted_text = Column(Text, nullable=True)
    text_truncated = Column(Boolean, nullable=False, default=False)
    detail = Column(String, nullable=True)  # "12 pages", "3 sheets", "1280×720"
    read_by = Column(String, nullable=True)  # "file" | "ai" | "file+ai"
    # pending | private_library | chat_only (rubik_core/chat_files/__init__.py)
    index_status = Column(String, nullable=False, default="pending")
    index_note = Column(String, nullable=True)
    # The Vertex ragFile resource name in the private corpus, when filed there.
    rag_file_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class ChatUploadPublication(Base):
    """A private file its owner published to Assets as a shared copy.

    Publishing COPIES the file into the shared Library through the ordinary
    Assets upload (``backend/routers/dam.py``); the private file stays private
    and the asset then follows the Library's own rules. This row only records
    that it happened, so the owner's list can say "In Assets" and a second
    publish is a choice rather than an accident. Its own table, not columns on
    ``chat_uploads``, for the reason ``chat_uploads`` is its own table: no
    ALTER on a live table.
    """

    __tablename__ = "chat_upload_publications"

    id = Column(String, primary_key=True)  # UUID string
    upload_id = Column(String, nullable=False, index=True)
    owner_email = Column(String, nullable=False, index=True)
    doc_id = Column(String, nullable=False)  # assets.doc_id of the copy
    published_filename = Column(String, nullable=False)
    department = Column(String, nullable=True)
    is_company_wide = Column(Boolean, nullable=False, default=False)
    # True when Assets got a Word copy of Cubie's reading (a picture, a scan, a
    # spreadsheet), not the original file.
    rendition = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class InteractionLog(Base):
    __tablename__ = "interaction_logs"

    id = Column(String, primary_key=True)  # UUID string
    session_id = Column(String, nullable=True)
    user_email = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    user_input = Column(String, nullable=False)
    system_prompt = Column(String, nullable=True)
    llm_output = Column(String, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    duration_ms = Column(Integer, default=0)
    successful = Column(Boolean, default=True)
    error_message = Column(String, nullable=True)
    # V2 chat plumbing — request mode and scope context for downstream
    # routing / retrieval prefilter once those features land.
    mode = Column(String, nullable=True)  # "ask" | "draft" | "investigate"
    scope = Column(JSON, nullable=True)  # {kind, id, label} or null


class LLMUsageEvent(Base):
    """One row per LLM API call (``generate_content`` / ``embed_content``),
    tagged with the product ``feature`` that triggered it, so Admin → AI Cost
    can attribute token spend and dollar cost to each feature.

    ``cost_usd`` is computed at write time from the rate card in
    ``rubik_core/cost/pricing.py`` (Vertex per-1M-token rates), so historical
    rows stay accurate even if the rate card is later corrected. Rows are
    written in batches by the best-effort recorder in ``rubik_core/cost/usage``
    — it never blocks or fails the originating call.

    ``feature`` defaults to ``"unattributed"``: any LLM call that flows through
    the shared client without a ``feature_context`` is still counted (real
    spend is never lost), it's just bucketed as unattributed until the calling
    feature is tagged.
    """

    __tablename__ = "llm_usage_events"

    id = Column(String, primary_key=True)  # UUID string
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    feature = Column(String, nullable=False, default="unattributed", index=True)
    operation = Column(String, nullable=False, default="generate")  # generate | embed
    model = Column(String, nullable=False, default="")
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    cached_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)
    user_email = Column(String, nullable=True, index=True)
    request_id = Column(String, nullable=True)
    metadata_json = Column(JSON, nullable=True)

    __table_args__ = (
        # Feature-over-time is the dominant query (Admin → AI Cost groups by
        # feature within a date range), so index the pair.
        Index("ix_llm_usage_feature_ts", "feature", "timestamp"),
    )


class HomeCard(Base):
    __tablename__ = "home_cards"

    id = Column(String, primary_key=True)  # UUID string
    # Plain string (no users FK): a departed teammate's priorities
    # history must survive offboarding so their successor (and org-wide exec)
    # can still read it — the old NO-ACTION FK either blocked the users-row
    # delete or forced the history to be destroyed with it.
    user_email = Column(String, nullable=False)
    date = Column(String, nullable=False)  # e.g. "2026-03-04"
    category = Column(
        String, nullable=False
    )  # e.g. "daily_briefing", "alert", "insight", "crm_change"
    pipeline_stage = Column(
        String, nullable=True
    )  # e.g. "signal_collector", "enricher", "validator"
    content_md = Column(String, nullable=False)
    # Structured payload (e.g. PrioritiesBundle priorities[]) so the UI can render
    # what was generated overnight without re-running the pipeline.
    metadata_json = Column(JSON, nullable=True)
    priority = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class PrioritiesRun(Base):
    """One on-demand AI-Priorities run (UC-41 → #9 background-job hardening).

    Run state used to live in per-instance dicts in ``backend/routers/home.py``
    (run_id → pending/ready/failed). Two failure modes: a poll landing on a
    different Cloud Run instance never saw the run, and the CPU-throttled
    instance could be recycled mid-run, losing the run *and* its state. Rows
    here are the cross-instance source of truth: the trigger endpoint inserts
    ``pending``, the executor (a single-task ``cubie-priorities-batch`` Job execution
    via ``backend.jobs.priorities_on_demand_job``, or the local-dev
    BackgroundTask fallback) flips it to ``ready``/``failed``, and the poll
    endpoint only ever reads the DB.
    """

    __tablename__ = "priorities_runs"

    run_id = Column(String, primary_key=True)  # UUID string
    user_email = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending")  # pending|ready|failed
    trigger = Column(String, nullable=False, default="on_demand")  # on_demand|more
    executor = Column(String, nullable=False, default="inline")  # job|inline
    # Cloud Run execution resource name when executor == "job" (debugging aid).
    execution_name = Column(String, nullable=True)
    error = Column(String, nullable=True)
    queued_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (Index("ix_priorities_runs_user_status", "user_email", "status"),)


class CrmSyncRun(Base):
    """One CRM Sync scan (UC-46): read an AE's call transcripts, diff them
    against HubSpot, propose CRM updates.

    Same cross-instance run-state contract as ``PrioritiesRun``: the trigger
    endpoint inserts ``pending`` and returns 202, a ``cubie-worker`` execution
    flips it to ``ready``/``failed``, and the poll endpoint only reads the DB
    (a poll landing on another Cloud Run instance must still see the run).
    """

    __tablename__ = "crm_sync_runs"

    run_id = Column(String, primary_key=True)  # uuid4 hex
    user_email = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending")  # pending|ready|failed
    trigger = Column(String, nullable=False, default="on_demand")  # on_demand|nightly
    executor = Column(String, nullable=False, default="inline")  # job|inline
    execution_name = Column(String, nullable=True)
    error = Column(String, nullable=True)
    # Oldest transcript timestamp this run considered (what "since" resolved to).
    window_start = Column(DateTime, nullable=True)
    # Counters: transcripts_scanned, external_meetings, suggestions_created,
    # duplicates_skipped, validation_rejected, superseded. JSON because the set
    # of things worth counting keeps changing as the extraction is tuned.
    stats = Column(JSON, nullable=True)
    queued_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (Index("ix_crm_sync_runs_user_status", "user_email", "status"),)


class CrmSyncSuggestion(Base):
    """One proposed HubSpot change awaiting its owner's approval (UC-46).

    Nothing here is ever applied automatically — a row only reaches HubSpot
    when the AE clicks Approve on that specific item, and the write goes
    through ``rubik_core/retrieval/hubspot_write.py``, which re-enforces the
    pipeline / owner / property gates independently of whatever produced the row.

    ``payload["properties"]`` is ``{prop: {"before": …, "after": …}}``. The
    model never supplies ``before``: the scan fills it from the HubSpot records
    it just read, so the diff the AE approves is grounded in fetched state
    rather than in anything the transcript claimed.

    Dedupe across re-scans keys on ``(user_email, dedupe_key)``, where
    ``dedupe_key = target_key:hash(after)``. ``target_key`` identifies the
    *thing being changed* without the values, so a second scan proposing a
    different value for the same field supersedes the first pending row
    instead of stacking a second card for the same field.
    """

    __tablename__ = "crm_sync_suggestions"

    id = Column(String, primary_key=True)  # uuid4 hex
    user_email = Column(String, nullable=False, index=True)
    run_id = Column(String, nullable=False, index=True)

    # Provenance: the transcript this was extracted from.
    doc_id = Column(String, nullable=True)
    meeting_id = Column(String, nullable=True)
    meeting_title = Column(String, nullable=True)
    meeting_date = Column(DateTime, nullable=True)

    # create_contact | update_contact | create_deal | update_deal
    change_type = Column(String, nullable=False)
    hubspot_object_type = Column(String, nullable=False)  # contact|deal|company
    # NULL for create_* until the write lands and we learn the new id.
    hubspot_object_id = Column(String, nullable=True)

    # Identity of the target, values excluded — see the class docstring.
    target_key = Column(String, nullable=False, index=True)
    dedupe_key = Column(String, nullable=False)

    payload = Column(JSON, nullable=False)
    # {"quote": …, "speaker": …, "doc_id": …, "truncated": bool}. The quote is
    # verified to appear verbatim in the transcript before the row is written.
    evidence = Column(JSON, nullable=True)
    confidence = Column(Float, nullable=True)

    # pending | applying | applied | failed | dismissed | superseded | expired
    status = Column(String, nullable=False, default="pending")
    error = Column(String, nullable=True)  # friendly text, never a raw API body
    applied_at = Column(DateTime, nullable=True)
    applied_by = Column(String, nullable=True)
    hubspot_response = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_email", "dedupe_key", name="uq_crm_sync_user_dedupe"),
        Index("ix_crm_sync_suggestions_user_status", "user_email", "status"),
    )


class EosIngestState(Base):
    """Per-kind debounce/coalesce state for event-driven EOS re-ingestion (#9
    follow-up).

    The EOS edit hooks fire on every Issue / VTO / Process mutation and each
    run re-ingests EVERY doc of that kind, so a naive job-per-edit would spawn
    many redundant ``cubie-worker`` executions during a busy L10. Each kind
    instead has ONE row here acting as a request watermark + dispatch claim:
    every edit bumps ``requested_at``; the first edit to find no live claim
    CASes ``status`` idle→launching and launches ONE worker execution; the
    worker releases the claim only if no newer request arrived while it ran
    (otherwise it loops), so a burst coalesces and the last edit always gets
    ingested. State machine + full rationale:
    ``rubik_core/ingestion/eos/debounce.py``.
    """

    __tablename__ = "eos_ingest_state"

    kind = Column(String, primary_key=True)  # vto | issues | processes
    status = Column(String, nullable=False, default="idle")  # idle|launching|running
    # Watermark: when the most recent edit requested a re-ingest.
    requested_at = Column(DateTime, nullable=True)
    # When the live claim was taken; claims older than
    # EOS_REINGEST_CLAIM_STALE_SECONDS are treated as dead and reclaimable.
    claimed_at = Column(DateTime, nullable=True)
    executor = Column(String, nullable=True)  # job|inline (debugging aid)
    execution_name = Column(String, nullable=True)
    last_started_at = Column(DateTime, nullable=True)
    last_finished_at = Column(DateTime, nullable=True)
    last_result = Column(String, nullable=True)  # e.g. "12 succeeded, 0 failed"


class AgentRecommendation(Base):
    """Proactive recommendations produced by UC-10's department-pluggable OvernightAgent.

    Sourced from any registered plugin (Sales/HubSpot today; an HR system, an
    accounting system, etc. as the platform grows). One row per (user, entity,
    type). The orchestrator runs nightly via Cloud Scheduler, writes new rows
    here, and the homepage reads the top-N non-dismissed rows per user to
    surface in the "AI Priorities" zone. Dismissal is permanent: once
    `dismissed_at` is set, the row is never resurfaced, but it stays in place
    so the agent doesn't re-emit an identical recommendation on the next run.

    Departmental scope is derivable from `users.email → users.department`;
    `source_metadata.plugin` records which plugin produced each row so we
    can debug "which integration emitted this".
    """

    __tablename__ = "agent_recommendations"

    id = Column(String, primary_key=True)
    # Plain string — historical rows outlive the users row.
    user_email = Column(String, nullable=False, index=True)
    type = Column(String, nullable=False)
    lead_id = Column(String, nullable=True, index=True)
    lead_name = Column(String, nullable=True)
    recommendation_text = Column(String, nullable=False)
    evidence = Column(JSON, nullable=True)
    confidence = Column(Float, nullable=False, default=0.0)
    source_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    dismissed_at = Column(DateTime, nullable=True)
    # UC-10 actions: hide row until snoozed_until passes; record the most-recent
    # action taken (any one of email_sent, meeting_blocked, snoozed, hubspot_opened,
    # dismissed) for the briefing's per-row badge + the agent's duplicate-guard.
    snoozed_until = Column(DateTime, nullable=True)
    acted_on_at = Column(DateTime, nullable=True)
    action_type = Column(String, nullable=True)
    action_metadata = Column(JSON, nullable=True)
    # UC-17: multi-source provenance, correlation grouping, judge next_action.
    sources = Column(JSON, nullable=True)
    correlation_id = Column(String, nullable=True)
    next_action = Column(String, nullable=True)
    # When this rec was delivered to the user via Slack DM (NULL = not
    # yet). The morning Slack delivery pass dedups on this so a re-run never
    # resends an already-delivered priority.
    slack_delivered_at = Column(DateTime, nullable=True)
    # When a manager was DM'd to escalate this item (HIGH + open >48h);
    # NULL = not escalated. Dedups so the manager isn't re-pinged every run.
    manager_escalated_at = Column(DateTime, nullable=True)

    feedback = relationship(
        "AgentRecommendationFeedback",
        back_populates="recommendation",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index(
            "ix_agent_recommendations_user_active",
            "user_email",
            "dismissed_at",
            "created_at",
        ),
        Index(
            "ix_agent_recommendations_active_window",
            "user_email",
            "dismissed_at",
            "snoozed_until",
        ),
        Index(
            "ix_agent_recommendations_correlation_id",
            "correlation_id",
        ),
    )


class AgentRecommendationFeedback(Base):
    """Append-only history of every action a user took on a recommendation.

    A single recommendation can have multiple feedback rows (snoozed Mon,
    email-drafted Wed, dismissed Fri). The agent doesn't read this today;
    it accumulates so a future calibration step can score "which checks
    actually drove action" — used to retune confidence weights.
    """

    __tablename__ = "agent_recommendation_feedback"

    id = Column(String, primary_key=True)
    recommendation_id = Column(
        String,
        ForeignKey("agent_recommendations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_type = Column(String, nullable=False)
    action_metadata = Column(JSON, nullable=True)
    occurred_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    recommendation = relationship("AgentRecommendation", back_populates="feedback")


# Allowed values for AssistantFeedback. Kept here so the model, the router schema,
# and the WGLL helper all agree on the same vocabulary.
FEEDBACK_RATINGS = ("useful", "meh", "not_useful")
FEEDBACK_SURFACES = ("priorities", "chat", "meeting_prep", "xray", "crm_sync")


class AssistantFeedback(Base):
    """User rating on a single assistant-generated proposal/message.

    The reusable "what good looks like" (WGLL) signal. One generic table spans
    every surface (AI Priorities, Chat, Meeting Prep) so the dataset stays
    queryable in one place and can be exported / fed back to the assistant as
    eval examples.

    Identity is ``(user_email, surface, proposal_id)`` — re-rating the same item
    UPSERTs the existing row (clicking a different face changes the rating)
    rather than appending, so each row is a user's *settled* judgment on an item.
    ``proposal_text`` is a point-in-time snapshot so the feedback stays meaningful
    even if the underlying recommendation / message is later regenerated or
    deleted.
    """

    __tablename__ = "assistant_feedback"

    id = Column(String, primary_key=True)
    # "priorities" | "chat" | "meeting_prep" — see FEEDBACK_SURFACES.
    surface = Column(String, nullable=False)
    # The proposal/message id on its surface: an AgentRecommendation /
    # PriorityMemory id, a ChatMessage id, or a Meeting Prep event id. Not a hard
    # FK — surfaces differ and we want the row to survive the source being purged.
    proposal_id = Column(String, nullable=False)
    # Snapshot of what the user was actually rating, captured at click time.
    proposal_text = Column(String, nullable=False)
    # Extra snapshot context (title, urgency, suggested_action, citations,
    # session_id, origin, …) so an exported example is self-describing.
    proposal_metadata = Column(JSON, nullable=True)
    # "useful" | "meh" | "not_useful" — see FEEDBACK_RATINGS.
    rating = Column(String, nullable=False)
    comment = Column(String, nullable=True)
    # Plain string — feedback is training data that outlives the
    # rater's users row (offboarding deletes the row, never the ratings).
    user_email = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        # One settled rating per user per item per surface → upsert target.
        UniqueConstraint(
            "user_email",
            "surface",
            "proposal_id",
            name="uq_assistant_feedback_user_item",
        ),
        # Admin list / WGLL pulls filter by surface+rating, newest first.
        Index(
            "ix_assistant_feedback_surface_rating", "surface", "rating", "updated_at"
        ),
    )


class Group(Base):
    __tablename__ = "groups"

    id = Column(String, primary_key=True)  # UUID string
    name = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", secondary=group_members, back_populates="groups")


class AccessPolicy(Base):
    __tablename__ = "access_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # CASCADE: a per-user policy is config, swept with the users row.
    user_email = Column(
        String, ForeignKey("users.email", ondelete="CASCADE"), nullable=True
    )
    group_id = Column(String, ForeignKey("groups.id"), nullable=True)
    department = Column(String, nullable=True)  # If null, matches any department
    topic = Column(String, nullable=True)  # If null, matches any topic
    can_read = Column(Boolean, default=True)
    can_update = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    group = relationship("Group")


class UserPreference(Base):
    """Per-user settings injected into every agent output (UC-11) plus the
    General + "AI Personalization" Settings tabs.

    One row per user (1:1 with `users`). The `UserContextBuilder` reads this
    row to compose the system-prompt block prepended to email drafts, meeting
    prep briefs, Chat, and AI-Priorities reasoning. Every column added beyond
    the original UC-11 writing-style set is nullable; ``None`` means "not
    configured — fall back to the org/department default", so an unset row
    behaves exactly as before.
    """

    __tablename__ = "user_preferences"

    user_email = Column(
        String, ForeignKey("users.email", ondelete="CASCADE"), primary_key=True
    )
    # ── UC-11 writing-style (email voice) ────────────────────────────────
    writing_style = Column(String, nullable=True)
    tone = Column(String, nullable=True)  # formal | conversational | direct
    preferred_greeting = Column(String, nullable=True)
    email_signature = Column(String, nullable=True)
    additional_instructions = Column(String, nullable=True)
    # ── General tab ──────────────────────────────────────────────────────
    timezone = Column(String, nullable=True)  # IANA name, e.g. America/New_York
    working_hours_start = Column(String, nullable=True)  # "HH:MM" local
    working_hours_end = Column(String, nullable=True)  # "HH:MM" local
    # JSON list of Notification.kind values to hide from the in-app bell.
    muted_notification_kinds = Column(JSON, nullable=True)
    # ── AI Personalization tab ───────────────────────────────────────────
    role_title = Column(String, nullable=True)  # the user's role / job title
    about_me = Column(String, nullable=True)  # "what I do & focus on" context
    chat_verbosity = Column(String, nullable=True)  # concise | balanced | detailed
    response_language = Column(String, nullable=True)  # "auto" | BCP-47 (e.g. "es")
    priorities_focus = Column(String, nullable=True)  # persisted AI-Priorities focus
    priorities_max = Column(Integer, nullable=True)  # persisted max card count
    priorities_sources = Column(JSON, nullable=True)  # JSON list of ChannelKind values
    priorities_muted_topics = Column(JSON, nullable=True)  # JSON list of topics to skip
    # ── EOS team opt-ins ─────────────────────────────────────────────────
    # JSON list of department names granted as ADDITIONAL EOS teams, on top of
    # `users.department`. Admin-set (Admin → Users). Only ever half the answer:
    # the read path intersects it with the caller's CURRENT L10 rosters (see
    # `_authz.roster_dept_names`), so an entry can't grant a team they don't
    # actually meet with, and it goes inert by itself when they come off that
    # calendar invite. NULL ⇒ no additional teams (the pre-migration default).
    eos_team_optins = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = relationship("User")


class UserVoiceShortcut(Base):
    """Each person's voice command shortcut.

    ``shortcut`` is ``{"mac": <chord> | "off", "pc": <chord> | "off"}``, a chord
    being ``{"mods": ["alt", "meta"], "code": null | "Space" | "KeyK" …}``. No
    row, or a platform left out, means the default: Option+Command / Ctrl+Alt.

    ITS OWN TABLE ON PURPOSE, and with no foreign key: adding a column to
    ``user_preferences`` at boot can take the service down (the ALTER waits
    for a lock on a table nearly every request reads, and everything queues
    behind it). Keep new per-person settings out of hot tables.
    """

    __tablename__ = "user_voice_shortcuts"

    user_email = Column(String, primary_key=True)  # lower-cased
    shortcut = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class UserTourProgress(Base):
    """Which page tours each person has finished or skipped. The tours
    themselves are content in the client (``frontend/src/tours/content/``);
    this is only each person's memory of them, so a tour never repeats and
    follows them to another browser.

    One row per (person, tour). ``tour_id`` is a tour's permanent id
    (``rocks.intro``, ``chat.new.file-uploads``); ``status`` is ``completed``
    or ``skipped`` (both mean "don't start it again"; kept apart so the team
    can tell a tour people finish from one they skip). The reserved id
    ``TOURS_SWITCH_ID`` with status ``off`` is the person's own switch from
    Settings → General: tours no longer start by themselves.

    Its own table with no foreign key, for the reason UserVoiceShortcut gives.
    """

    __tablename__ = "user_tour_progress"

    user_email = Column(String, primary_key=True)  # lower-cased
    tour_id = Column(String, primary_key=True)
    status = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


#: ``UserTourProgress.tour_id`` of the per-person "tours start by themselves"
#: switch. Not a valid tour id (tour ids start with a letter or digit).
TOURS_SWITCH_ID = "*"


class SlackCredential(Base):
    """UC-15: per-user Slack OAuth tokens.

    One row per user. `access_token` is the user-token (xoxp-…) returned by
    the Slack OAuth flow — scoped to whatever `authed_user.scope` we requested
    in the manifest. The bot token (xoxb-…) is a workspace connector secret.
    Personal tokens are encrypted before SQLAlchemy writes them to this table.
    """

    __tablename__ = "slack_credentials"

    user_email = Column(
        String, ForeignKey("users.email", ondelete="CASCADE"), primary_key=True
    )
    slack_user_id = Column(String, nullable=False)
    slack_team_id = Column(String, nullable=False)
    slack_team_name = Column(String, nullable=True)
    # Google's verified hosted domain from the signed OAuth state. Email
    # aliases are not evidence of workspace membership; this binding is
    # rechecked against control data whenever the credential is used.
    hosted_domain = Column(String(253), nullable=False)
    access_token = Column(EncryptedString(), nullable=False)
    scope = Column(String, nullable=True)
    bot_scope = Column(String, nullable=True)
    connected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user = relationship("User")


class GmailAccount(Base):
    """An explicitly connected Google mailbox owned by one workspace user.

    The user's primary work mailbox remains the separate DWD default. An OAuth
    refresh token here can never authorize another person or workspace.
    """

    __tablename__ = "gmail_accounts"

    id = Column(String(36), primary_key=True)
    owner_email = Column(
        String, ForeignKey("users.email", ondelete="CASCADE"), nullable=False
    )
    google_sub = Column(String(255), nullable=False)
    email = Column(String(320), nullable=False)
    refresh_token = Column(EncryptedString(), nullable=False)
    scopes = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False, default="connected")
    # The owner accepted this AI Priorities provider/model when connecting or
    # after a workspace admin changed it. Null on older rows requires review.
    ai_provider_ack = Column(String(64), nullable=True)
    ai_provider_notice = Column(String(64), nullable=True)
    connected_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_email", "google_sub", name="uq_gmail_account_owner_sub"
        ),
        UniqueConstraint("owner_email", "email", name="uq_gmail_account_owner_email"),
        Index("ix_gmail_accounts_owner", "owner_email"),
    )


class GmailOAuthAttempt(Base):
    """Single-use authorization state, scoped to the user's workspace DB."""

    __tablename__ = "gmail_oauth_attempts"

    nonce_hash = Column(String(64), primary_key=True)
    browser_nonce_hash = Column(String(64), nullable=False)
    owner_email = Column(
        String, ForeignKey("users.email", ondelete="CASCADE"), nullable=False
    )
    code_verifier = Column(EncryptedString(), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (Index("ix_gmail_oauth_attempts_owner", "owner_email"),)


class SlackViewSubmission(Base):
    """One committed Slack modal submission per view in this workspace DB."""

    __tablename__ = "slack_view_submissions"

    view_id = Column(String(128), primary_key=True)
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=func.now()
    )


class Notification(Base):
    """V2 in-app notification feed for the TopBar bell.

    One row per delivered notification, scoped by ``actor_email`` (the
    recipient). The composite index supports both the "unread feed" query
    (``read_at IS NULL``) and the "all-notifications" query, newest first.

    Not foreign-keyed to ``users.email`` because the v2 spec treats this
    as an append-only fire-and-forget channel — we don't want a missing
    user row to block delivery, and we don't cascade-delete a user's
    history on user deletion (those tombstones can stay).

    Creation triggers (agent recommendations, deal stage changes, ingest
    failures, doc approvals) wire up as their respective features land —
    this V2_1 cut is the storage + read API only.
    """

    __tablename__ = "notifications"

    id = Column(String, primary_key=True)
    actor_email = Column(String, nullable=False)
    # 'agent_recommendation' | 'deal_stage_change' | 'ingest_failure' | 'doc_approval_request'
    kind = Column(String, nullable=False)
    title = Column(String, nullable=False)
    body = Column(String, nullable=False)
    # 'info' | 'warn' | 'urgent'
    severity = Column(String, nullable=False)
    link = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    read_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "ix_notifications_actor_read_created",
            "actor_email",
            "read_at",
            desc("created_at"),
        ),
    )


class UserBookmark(Base):
    """V2 Saved — one row per user-bookmarked target.

    Idempotent insertion is enforced by the UNIQUE constraint on
    (actor_email, target_kind, target_id); the router catches the
    integrity error and returns the existing row. Display metadata
    (title / subtitle / link) is NOT stored here — V2_1 derives it at
    read time from the target record's own endpoint to avoid drift.
    """

    __tablename__ = "user_bookmark"

    id = Column(String, primary_key=True)
    actor_email = Column(String, nullable=False, index=True)
    # asset | chat_session | chat_message | deal | meeting | contact
    target_kind = Column(String, nullable=False)
    target_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "actor_email",
            "target_kind",
            "target_id",
            name="uq_user_bookmark_actor_target",
        ),
    )


class UserRecentTouch(Base):
    """V2 Recent — one row per (user, target). Upserted on every touch.

    Unlike ``UserBookmark`` this table denormalizes ``title`` and ``link``
    at write time: the frontend pings ``POST /api/recent/touch`` with the
    label it just rendered, and the Recent list reads those columns back
    directly. That keeps the read path single-table and means a deleted
    target still shows in Recent until the nightly Cloud Scheduler prune
    job sweeps it (see v2 spec §Retention).

    ``UNIQUE (actor_email, target_kind, target_id)`` lets the upsert
    happen via "fetch-then-update / else-insert" without races.
    """

    __tablename__ = "user_recent_touch"

    id = Column(String, primary_key=True)
    actor_email = Column(String, nullable=False)
    # asset | chat_session | deal | meeting | contact
    target_kind = Column(String, nullable=False)
    target_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    link = Column(String, nullable=False)
    touched_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "actor_email",
            "target_kind",
            "target_id",
            name="uq_user_recent_touch_actor_target",
        ),
        # Composite index supports the list query:
        #   WHERE actor_email = ? ORDER BY touched_at DESC.
        Index(
            "ix_user_recent_touch_actor_touched",
            "actor_email",
            desc("touched_at"),
        ),
    )


class PriorityMemory(Base):
    """Per-user memory of UC-20 priorities the system has surfaced.

    UC-25. Keeps the same prep-debt for the same event from re-firing
    daily after the user has run meeting prep, and lets the user
    dismiss / snooze priorities they don't want to see.

    A row is keyed by ``(user_email, entity_kind, entity_id, priority_kind)``.
    States move via:

      active  →  resolved   (auto-resolve from another code path, or manual ✓)
      active  →  dismissed  (user clicked ✗)
                ↳ resurfaces when a NEW signal arrives with
                  ``timestamp > last_state_change_at``
      active  →  snoozed    (user picked a until-date)
                ↳ orchestrator post-filter treats it as active again once
                  ``snoozed_until`` is in the past
      resolved  is terminal (durable judgment) unless the user manually
                reopens via dismiss-then-new-evidence.
    """

    __tablename__ = "priority_memory"

    id = Column(String, primary_key=True)  # uuid
    # No single-column index: the unique key and both indexes below lead with
    # ``user_email``, so each of them already serves a lookup by it.
    user_email = Column(
        String,
        ForeignKey("users.email", ondelete="CASCADE"),
        nullable=False,
    )
    entity_kind = Column(String, nullable=False)
    entity_id = Column(String, nullable=False)
    priority_kind = Column(String, nullable=False)
    state = Column(String, nullable=False, default="active")  # see docstring
    snoozed_until = Column(DateTime, nullable=True)
    last_priority_id = Column(String, nullable=True)
    resolved_evidence = Column(JSON, nullable=True)
    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_state_change_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_state_change_by = Column(String, nullable=True)  # email or 'system'

    __table_args__ = (
        UniqueConstraint(
            "user_email",
            "entity_kind",
            "entity_id",
            "priority_kind",
            name="uq_priority_memory_user_entity_kind",
        ),
        Index(
            "ix_priority_memory_user_state",
            "user_email",
            "state",
        ),
        Index(
            "ix_priority_memory_snoozed_until",
            "user_email",
            "snoozed_until",
        ),
    )


class PriorityPlaybook(Base):
    """Admin-managed playbook for the UC-20 AI Priorities decision-maker.

    Free-form markdown text describing what counts as "high-value" for
    a given department — high-value entities, what to watch for, bias
    rules. UC-24 resolves *all active playbooks for the user's
    department* + *all active default playbooks* and injects the
    concatenation into the decision-maker system prompt.

    ``department`` is nullable: a NULL row is the **default** playbook
    that applies to every user regardless of department. Multiple
    active rows per department are supported (e.g. Ops can have
    Vendor-Mgmt + Audit + Production playbooks running simultaneously);
    ``sort_order`` controls injection order (lower = earlier in the
    prompt).
    """

    __tablename__ = "priority_playbook"

    id = Column(String, primary_key=True)  # uuid
    name = Column(String, nullable=False)  # admin-visible label
    department = Column(
        String,
        ForeignKey("departments.name", ondelete="CASCADE"),
        nullable=True,  # NULL = default for all
        index=True,
    )
    body = Column(String, nullable=False)  # markdown — injected
    is_active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=100)
    created_by = Column(String, nullable=False)  # author email
    updated_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_priority_playbook_dept_active",
            "department",
            "is_active",
        ),
    )


class DepartmentDriveFolder(Base):
    """Admin-curated shared Drive folders surfaced per department.

    People spend ~90% of their day in a small set of shared Drive folders that
    belong to their department. An admin curates those folder references here
    (folder id + label + link); the Productivity page shows a user the folders
    for *their* department plus any company-wide ones.

    ``department`` is nullable: a NULL row is **company-wide** and shows for
    everyone — mirrors the :class:`PriorityPlaybook` default-row convention.
    ``sort_order`` controls display order (lower = first). We store folder
    references the admin pastes rather than auto-listing, because the Drive
    client can't enumerate Shared Drives / shared-with-me today (``corpora=
    "user"``); broadening that is a tracked follow-up.
    """

    __tablename__ = "department_drive_folder"

    id = Column(String, primary_key=True)  # uuid
    department = Column(
        String,
        ForeignKey("departments.name", ondelete="CASCADE"),
        nullable=True,  # NULL = company-wide
        index=True,
    )
    folder_id = Column(String, nullable=False)  # Google Drive folder id
    folder_name = Column(String, nullable=False)  # admin-visible label
    web_view_link = Column(String, nullable=True)  # https://drive.google.com/…
    sort_order = Column(Integer, nullable=False, default=100)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by = Column(String, nullable=False)
    updated_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_department_drive_folder_dept_active",
            "department",
            "is_active",
        ),
    )


class IngestionCouncilVerdict(Base):
    """One Ingestion Council run: the chairman verdict + the rendered report.

    Written by the ingestion-review council (an adaptation of the LLM-council
    pattern) after the overnight/weekly ingestion jobs. Persisted so operators
    can (a) open the full HTML briefing and (b) trend ingestion health over time
    — a WARN that recurs three nights running is a different signal than a
    one-off. ``verdict_json`` is the full ``CouncilVerdict.to_dict()`` (auditor
    findings, peer reviews, evidence); ``report_html`` is the self-contained
    document served at ``/api/jobs/ingestion-council/runs/{id}/report``.
    """

    __tablename__ = "ingestion_council_verdicts"

    id = Column(String, primary_key=True)  # UUID string
    scope = Column(String, nullable=False)  # "nightly" | "weekly" | "all"
    overall_health = Column(String, nullable=False)  # "pass" | "warn" | "fail"
    headline = Column(String, nullable=False)
    verdict_json = Column(JSON, nullable=False)
    report_html = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_ingestion_council_scope_created", "scope", "created_at"),
    )


class MeetingPrepRun(Base):
    """One persisted Meeting-Prep generation: the brief plus its exact inputs.

    Nothing about a brief used to be stored — every open regenerated from
    scratch — so a bad brief (e.g. the one that called a long-won engagement a
    "previous unsuccessful bid") could not be reproduced, audited, or measured.
    We now snapshot each run: the assembled ``context_bundle`` fed to the model
    and the ``response`` it returned. That makes grounding regressions
    reproducible, lets us sweep already-delivered briefs for the same failure
    class, and gives the heavier async prep a row to hang delivery state on.

    The delivery/PDF/council columns are nullable and unused by the synchronous
    on-screen path; the async "deep prep" workflow populates them on the same
    table rather than needing its own.
    """

    __tablename__ = "meeting_prep_runs"

    id = Column(String, primary_key=True)  # uuid4 hex
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    user_email = Column(String, nullable=False, index=True)
    event_id = Column(String, nullable=True, index=True)
    title = Column(String, nullable=True)

    # Which persona/path produced the brief, and the routing flags behind it.
    prep_kind = Column(String, nullable=True)  # "sales_battle_brief" | "internal_ea"
    is_sales_user = Column(Boolean, default=False, nullable=False)
    is_internal = Column(Boolean, default=False, nullable=False)
    model_id = Column(String, nullable=True)

    # The exact prompt inputs and the model's structured output.
    context_bundle = Column(JSON, nullable=True)  # attendees, transcripts, emails…
    response = Column(JSON, nullable=True)  # the MeetingPrepResponse dict
    partial_errors = Column(JSON, default=list, nullable=True)

    # ── Async "deep prep" workflow (unused by the synchronous path) ──────────
    mode = Column(String, default="sync", nullable=False)  # "sync" | "deep"
    delivery_status = Column(String, nullable=True)  # queued|running|delivered|failed
    pdf_drive_file_id = Column(String, nullable=True)
    council_verdict = Column(JSON, nullable=True)

    # ── Versions ─────────────────────────────────────────────────────────────
    # Each on-screen brief is one numbered version per (reader, meeting). A full
    # regenerate, a one-section rewrite, a hand edit and a restore each append a
    # NEW row built on ``base_run_id``; nothing is overwritten, so any earlier
    # version can be reopened or restored. ``version`` stays NULL for deep runs
    # (emailed, not on screen) and for failed generations, which never become a
    # version a person could land on.
    version = Column(Integer, nullable=True)
    base_run_id = Column(String, nullable=True)
    # generated | regenerated | section_rewritten | section_edited | restored | failed
    change_kind = Column(String, nullable=True)
    change_note = Column(Text, nullable=True)  # the rewrite instruction, if any
    changed_section = Column(String, nullable=True)  # section key for section changes
    # Per-section provenance carried down the chain:
    # {section_key: {"origin": "edited"|"rewritten", "version": n, "at": iso,
    #                "instruction": str|None}}
    section_meta = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_meeting_prep_runs_user_created", "user_email", "created_at"),
        # One version number per reader per meeting; two simultaneous writers
        # racing for the same number lose on this and retry (versions.py).
        Index(
            "uq_meeting_prep_runs_user_event_version",
            "user_email",
            "event_id",
            "version",
            unique=True,
        ),
    )


class XraySnapshot(Base):
    """One Customer X-Ray generation — a shared, versioned relationship snapshot.

    UC-45. Both flavors write one of these rows: ``fast`` (inline synthesis, a
    few seconds) and ``deep`` (async multi-agent research). Snapshots are a
    **team asset** — any sales user can open the latest ``ready`` row for a
    company — and they **checkpoint**: a refresh loads the latest snapshot,
    pulls only data newer than ``covered_through`` (per-source cursors in
    ``source_cursors``), extends the append-only timeline, recomputes churn on
    the full current state, and writes a NEW row ``version = n+1`` with
    ``base_snapshot_id`` pointing at its parent. Nothing is overwritten.

    Like UC-41's placeholder HomeCard, the *pending* row IS the run: ``POST
    …/deep`` inserts it with ``status='pending'`` and ``GET …/runs/{id}`` polls
    it. ``progress`` holds the per-agent state the polling skeleton renders.

    ``sections`` / ``signals`` are JSON because the analyst output keeps
    evolving with prompt iterations (a migration per tweak would be silly); the
    two things that must stay queryable across time — the timeline and the
    churn score — are normalized (the timeline into its own table, the churn
    number carried inside ``signals``).
    """

    __tablename__ = "xray_snapshots"

    id = Column(String, primary_key=True)  # uuid4 hex
    # HubSpot company hs_object_id — the stable anchor across refreshes.
    company_id = Column(String, nullable=False, index=True)
    company_name = Column(String, nullable=True)
    company_domain = Column(String, nullable=True)

    version = Column(Integer, nullable=False, default=1)
    # Checkpoint lineage — the snapshot this one extended (NULL for a first run).
    base_snapshot_id = Column(String, nullable=True)
    kind = Column(String, nullable=False, default="fast")  # "fast" | "deep"
    # "pending" | "running" | "ready" | "failed". A run is never persisted as
    # ready with an empty brief (see rubik_core/xray/service.py).
    status = Column(String, nullable=False, default="ready")
    error = Column(String, nullable=True)
    focus = Column(String, nullable=True)  # optional user focus prompt

    # The analyst's prose + structured lists (executive_summary, opportunities,
    # relationship_health, commercial_health, recent_news, source_evidence, …).
    sections = Column(JSON, nullable=True)
    # Dashboard numbers incl. churn {score, band, factors[], rationale_md}.
    signals = Column(JSON, nullable=True)

    # Coverage watermark — a refresh fetches only data newer than this.
    covered_through = Column(DateTime, nullable=True)
    # Per-source incremental cursors (last engagement id, last gmail
    # internalDate, last news search date, …) so refreshes are delta-only.
    source_cursors = Column(JSON, nullable=True)
    # Deep runs: per-agent state for the polling skeleton.
    progress = Column(JSON, nullable=True)
    # Intelligence gaps — sources that failed soft (same UX as MeetingPrep).
    partial_errors = Column(JSON, default=list, nullable=True)

    model_id = Column(String, nullable=True)
    created_by = Column(String, nullable=True)  # requesting user email
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        # Latest snapshot for a company, and the per-company version chain.
        Index("ix_xray_snapshots_company_created", "company_id", "created_at"),
        Index("ix_xray_snapshots_company_version", "company_id", "version"),
        Index("ix_xray_snapshots_company_status", "company_id", "status"),
    )


class XrayTimelineEvent(Base):
    """One deduplicated moment in a customer relationship. Append-only.

    UC-45's centerpiece. The timeline OUTLIVES any single snapshot: a refresh
    inserts only unseen ``event_key``s, and the timeline a user sees is
    ``SELECT … WHERE company_id ORDER BY ts`` — which is what makes the
    checkpoint model work. ``event_key`` is unique per company (e.g.
    ``deal:12345:stage:closedwon``, ``gmail:thread:abc``, ``news:sha1(url)``)
    and is the dedupe key across refreshes forever.
    """

    __tablename__ = "xray_timeline_events"

    id = Column(String, primary_key=True)  # uuid4 hex
    company_id = Column(String, nullable=False, index=True)
    # Dedupe key — unique WITH company_id (see __table_args__).
    event_key = Column(String, nullable=False)
    ts = Column(DateTime, nullable=True, index=True)
    # "hubspot_deal" | "hubspot_activity" | "gmail" | "meeting" | "doc" |
    # "news" | "xero" | "status_change".
    source = Column(String, nullable=False)
    kind = Column(String, nullable=True)  # short machine label
    title = Column(String, nullable=False)
    detail = Column(String, nullable=True)
    evidence = Column(String, nullable=True)  # citation string / URL
    first_seen_snapshot_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "event_key", name="uq_xray_timeline_company_event"
        ),
        Index("ix_xray_timeline_company_ts", "company_id", "ts"),
        Index("ix_xray_timeline_company_source", "company_id", "source"),
    )


class OOOPeriod(Base):
    """A materialised out-of-office / PTO window for one user.

    Written by the daily availability-refresh job (which reads each roster
    user's calendar via domain-wide delegation ONCE) so chat, priorities, and
    the manager digest can answer "who's out this week?" from a cheap table
    instead of impersonating N calendars per question. Dates are ISO
    ``YYYY-MM-DD`` strings (they compare lexicographically == chronologically),
    keeping the table SQLite + Postgres portable without a Date column type.
    Upsert-keyed on ``(user_email, start_date)`` so a re-sync updates a period
    in place rather than duplicating it.
    """

    __tablename__ = "ooo_periods"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_email = Column(String, nullable=False, index=True)
    start_date = Column(String, nullable=False)  # ISO YYYY-MM-DD, inclusive
    end_date = Column(String, nullable=False)  # ISO YYYY-MM-DD, inclusive
    return_date = Column(String, nullable=False)  # first working day back
    synced_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_email", "start_date", name="uq_ooo_user_start"),
        Index("ix_ooo_user_dates", "user_email", "start_date", "end_date"),
    )


class AttendeeProfile(Base):
    """UC-47 — one researched meeting attendee: role, public activity, persona read.

    A cache, not a system of record. Person research costs a grounded LLM call
    (or a metered API credit) per attendee, and the same prospect gets prepped
    repeatedly — by one rep across a deal cycle, and by several reps across an
    account — so a cold lookup per prep would pay for the same answer many times
    over.

    **Shared across users, keyed on the lowercased email.** A prospect's title
    and posting themes are not reader-specific, so scoping the cache per user
    would multiply the cost by the size of the sales team for no added accuracy
    (the same call the X-Ray snapshots make in treating a snapshot as a team
    asset). One row per person, overwritten on refresh — history has no consumer
    here, and keeping versions of somebody's LinkedIn headline is data we have no
    reason to hold.

    ``profile`` / ``activity`` / ``persona`` are JSON because the research
    contract is still moving with prompt iterations; the fields that must stay
    queryable — freshness, provider, and whether the run errored — are columns.
    ``error`` being set is what makes a failed run cache for hours instead of
    weeks (see ``people/config.ERROR_CACHE_TTL_HOURS``), so a provider blip does
    not blank somebody's card for a fortnight.
    """

    __tablename__ = "attendee_profiles"

    id = Column(String, primary_key=True)  # uuid4 hex
    # Lowercased work email — the identity and the cache key. Unique so a
    # refresh updates in place instead of accumulating rows per person.
    email = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=True)
    linkedin_url = Column(String, nullable=True)
    # "web_search" | "linkedin_api" — which source produced this row, so a thin
    # card can be explained rather than just look broken.
    provider = Column(String, nullable=True)
    # Confidence of the persona read: "high" | "medium" | "low". Column (not just
    # JSON) so we can measure how often the feature is actually confident.
    confidence = Column(String, nullable=True)

    profile = Column(JSON, nullable=True)  # PersonProfile.as_dict()
    activity = Column(JSON, nullable=True)  # [ActivityItem.as_dict()]
    persona = Column(JSON, nullable=True)  # PersonaRead.as_dict()
    sources = Column(JSON, nullable=True)  # citation URLs

    # Set when research failed. Drives the short error TTL above.
    error = Column(String, nullable=True)

    researched_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class GraphEntity(Base):
    """One node in the customer relationship graph — a person, company, deal,
    calendar event or transcript.

    Phase 1 of ``docs/customer_relationship_graph.md``. Cubie already derived
    these relationships per feature (X-Ray joined on a HubSpot company id,
    Meeting Prep matched email-domain strings, the priorities pipeline built an
    adjacency index in memory) and threw them away each run. This table and
    ``graph_edges`` persist that layer once so every feature traverses the same
    identity instead of re-deriving its own.

    ``entity_key`` is ``f"{kind}:{id}"`` — the same convention as
    ``ChannelSignal.entity_key``, so priorities signals join against the graph
    directly. Normalization of the id (lowercased email, lowercased domain, …)
    belongs to ``rubik_core.graph.keys`` and nowhere else.

    METADATA ONLY. ``display_name`` and ``attrs`` hold ids, titles and dates —
    never transcript text, email bodies or document content. Content is always
    re-fetched through its own access path, so this table can never become a
    way around a permission check.
    """

    __tablename__ = "graph_entities"

    entity_key = Column(String, primary_key=True)  # "{kind}:{id}"
    kind = Column(String, nullable=False, index=True)  # EntityKind value
    entity_id = Column(String, nullable=False)  # canonical id for that kind
    display_name = Column(String, nullable=True)  # label for rendering, not content
    # Small, non-authoritative cache (e.g. a deal's stage at last sight). The
    # owning system stays the source of truth; nothing reads this as fact.
    attrs = Column(JSON, default=dict)

    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("kind", "entity_id", name="uq_graph_entity"),)


class GraphEdge(Base):
    """One typed, dated, attributed relationship between two nodes.

    Read an edge as "``src_key`` ``rel`` ``dst_key``" — e.g.
    ``person:jdoe@… attended transcript:abc123``.

    Every edge carries its provenance, because "says who?" is part of the
    answer: ``source`` (which system asserted it), ``method`` (deterministic
    join, heuristic, or — from Phase 2 — LLM extraction), ``confidence``, and
    ``evidence_ref`` (the doc_id / HubSpot object / event id behind it). A
    briefing that cites a graph fact can therefore point at what produced it,
    which is exactly what a bare vector hit cannot do.

    Edges are facts with history, not rows to churn: re-observing one bumps
    ``last_seen_at`` rather than inserting a duplicate, and an edge that stops
    being observed is soft-deleted via ``invalidated_at`` (re-observation clears
    it) so we keep "this was true until…" instead of silently losing it.
    """

    __tablename__ = "graph_edges"

    id = Column(String, primary_key=True)  # uuid4 hex
    src_key = Column(
        String,
        ForeignKey("graph_entities.entity_key", ondelete="CASCADE"),
        nullable=False,
    )
    dst_key = Column(
        String,
        ForeignKey("graph_entities.entity_key", ondelete="CASCADE"),
        nullable=False,
    )
    # attended | invited_to | has_contact | has_deal | owned_by | member_of
    rel = Column(String, nullable=False)

    # ── Provenance ──────────────────────────────────────────────────────────
    # meeting_participants | hubspot_associations | calendar | domain_heuristic
    source = Column(String, nullable=False)
    # deterministic | heuristic | llm (Phase 2)
    method = Column(String, nullable=False, default="deterministic")
    confidence = Column(Float, nullable=False, default=1.0)
    evidence_ref = Column(String, nullable=True)  # doc_id / object id / event id
    # When the fact HAPPENED (meeting date), as opposed to when we saw it.
    observed_at = Column(DateTime, nullable=True)

    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Soft delete: set when a previously-observed edge is gone (e.g. an attendee
    # removed from a re-synced transcript). Re-observation clears it.
    invalidated_at = Column(DateTime, nullable=True)
    attrs = Column(JSON, default=dict)

    __table_args__ = (
        # One row per asserted fact PER SOURCE — two systems may independently
        # claim the same relationship, and losing that is losing corroboration.
        UniqueConstraint("src_key", "dst_key", "rel", "source", name="uq_graph_edge"),
        Index("ix_graph_edges_src_rel", "src_key", "rel"),
        Index("ix_graph_edges_dst_rel", "dst_key", "rel"),
    )


class OffboardRecord(Base):
    """One offboarded person and who inherits their open work.

    Written when an admin offboards a user; the ``users`` row is deleted in the
    same flow, so this row is deliberately FK-free — it is the durable memory
    that the person existed, who succeeded them, and what the exit sweep found.

    ``sweep_status`` tells the reader how much the dossier can claim:

    - ``pending``  — sweep dispatched, not yet finished.
    - ``ok``       — full inventory, including a last-pass over the departed
      person's Gmail/Calendar/Drive (their Google account was still
      impersonable when the sweep ran).
    - ``degraded`` — Google already suspended the account, so the sweep could
      only mine what Cubie holds (EOS, HubSpot, priorities history). The
      dossier says so; run offboarding BEFORE IT suspends the account.
    - ``failed``   — the sweep crashed; ``sweep_detail`` has the trail.
    """

    __tablename__ = "offboard_records"

    id = Column(String, primary_key=True)  # UUID string
    # Snapshots taken at offboard time — the users row is gone afterwards.
    user_email = Column(String, nullable=False, index=True)  # lower-cased
    user_name = Column(String, nullable=False)
    department = Column(String, nullable=True)
    # Who inherits. A live user at creation time; deliberately not FK'd so a
    # successor's own later offboarding can't be blocked by this row.
    successor_email = Column(String, nullable=False, index=True)  # lower-cased
    created_by = Column(String, nullable=False)  # admin who ran the offboard
    offboarded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    sweep_status = Column(String, nullable=False, default="pending")
    # Ordered audit trail of what the sweep did / skipped and why.
    sweep_detail = Column(JSON, nullable=True)
    # Rendered handoff dossier (markdown) + structured extras (counts,
    # provenance, degraded-mode note). The Handoffs page renders these.
    dossier_md = Column(String, nullable=True)
    dossier_meta = Column(JSON, nullable=True)
    # "active" while any item is open; "closed" once the successor has
    # resolved or dismissed everything (maintained by the item endpoints).
    status = Column(String, nullable=False, default="active")

    items = relationship(
        "OffboardItem", back_populates="record", cascade="all, delete-orphan"
    )


class OffboardItem(Base):
    """One open thing a departed person left behind, now owned by the successor.

    Feeds two surfaces: the Handoffs page checklist, and the ``inherited``
    priorities channel, which keeps re-surfacing open items in the successor's
    daily AI Priorities until each is marked done or dismissed. ``entity_kind``
    / ``entity_id`` keep the ORIGINAL entity reference (deal, thread, event,
    EOS to-do…) so the decision-maker can correlate an inherited item with the
    forwarded email / transferred doc that later shows up in the successor's
    own channels.
    """

    __tablename__ = "offboard_items"

    id = Column(String, primary_key=True)  # UUID string
    record_id = Column(
        String,
        ForeignKey("offboard_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "l10" | "todo" | "rock" | "milestone" | "scorecard" | "deal" |
    # "commitment" — where the item came from, drives the checklist grouping.
    kind = Column(String, nullable=False)
    label = Column(String, nullable=False)
    detail = Column(String, nullable=False, default="")
    due = Column(String, nullable=True)  # ISO date, when the item has a clock
    urgency = Column(String, nullable=False, default="medium")
    # Original entity reference (see class docstring). Optional — an EOS
    # scorecard measurable, e.g., has no cross-channel entity.
    entity_kind = Column(String, nullable=True)
    entity_id = Column(String, nullable=True)
    source_links = Column(JSON, nullable=True)  # list[str]

    # "open" | "done" | "dismissed"
    status = Column(String, nullable=False, default="open", index=True)
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    record = relationship("OffboardRecord", back_populates="items")

    __table_args__ = (Index("ix_offboard_items_record_status", "record_id", "status"),)


class ScheduledTask(Base):
    """One recurring (or one-off) Cubie run a user asked for in Chat.

    The prompt is replayed later by ``cubie-worker`` through the same chat
    agent, as this user, so it must be SELF-CONTAINED: a scheduled run carries
    no conversation history.

    The schedule is structured rather than a cron string — ``schedule_kind``
    plus a local ``time_local`` and (for weekly/monthly) a day selector — so it
    can be validated when the model proposes it and described back in plain
    English. ``timezone`` is the user's, captured at creation: 8am means 8am
    where they are, across DST.

    ``user_email`` is a plain string with no FK, like
    ``ChatSession.user_email``, and is stored verbatim from the profile because
    chat's own lookups are case-exact.
    """

    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        Index("ix_scheduled_tasks_status_next_run", "status", "next_run_at"),
    )

    SCHEDULE_KINDS = ("once", "daily", "weekdays", "weekly", "monthly")
    STATUSES = ("active", "paused", "completed", "error_paused")

    id = Column(String, primary_key=True)  # uuid4 hex
    user_email = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    prompt = Column(Text, nullable=False)

    schedule_kind = Column(String, nullable=False)
    time_local = Column(String, nullable=True)  # "HH:MM" in `timezone`
    days_of_week = Column(JSON, nullable=True)  # weekly: [0..6], Monday=0
    day_of_month = Column(Integer, nullable=True)  # monthly: 1-31, clamped
    run_at = Column(DateTime, nullable=True)  # once: the single UTC instant
    timezone = Column(String, nullable=False)  # IANA name

    # "chat" is implicit and always delivered; this holds the extras the user
    # asked for ("slack", "email").
    delivery_channels = Column(JSON, nullable=False, default=list)

    status = Column(String, nullable=False, default="active")
    paused_reason = Column(String, nullable=True)

    # Next UTC instant to fire; NULL when nothing more will ever fire.
    next_run_at = Column(DateTime, nullable=True)
    # Set while a run is in flight. Blocks an overlapping manual run; the
    # dispatcher's reaper clears it when a worker died without reporting.
    claimed_at = Column(DateTime, nullable=True)

    last_run_at = Column(DateTime, nullable=True)
    last_run_status = Column(String, nullable=True)
    last_run_id = Column(String, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    run_count = Column(Integer, nullable=False, default=0)

    source_session_id = Column(String, nullable=True)
    created_via = Column(String, nullable=False, default="chat")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    runs = relationship(
        "ScheduledTaskRun",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="ScheduledTaskRun.created_at.desc()",
    )


class ScheduledTaskRun(Base):
    """One attempt at a scheduled task.

    The row is created by the dispatcher BEFORE the worker is launched, and the
    worker's first act is to move it ``queued -> running`` with a
    compare-and-swap. That claim is what makes Cloud Run's automatic retry
    safe: a second invocation of the same run finds the row already claimed and
    exits without re-delivering.
    """

    __tablename__ = "scheduled_task_runs"
    __table_args__ = (
        Index("ix_scheduled_task_runs_status_created", "status", "created_at"),
    )

    STATUSES = ("queued", "running", "succeeded", "failed", "skipped")

    id = Column(String, primary_key=True)
    task_id = Column(
        String,
        ForeignKey("scheduled_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_email = Column(String, nullable=False)
    # The occurrence this row accounts for ("now" for a manual run).
    scheduled_for = Column(DateTime, nullable=True)
    trigger = Column(String, nullable=False, default="schedule")  # schedule|manual
    status = Column(String, nullable=False, default="queued")
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    execution_name = Column(String, nullable=True)
    # The chat conversation the result was written into — the user's copy of
    # the answer, and where they continue the thread.
    chat_folder_id = Column(String, nullable=True)
    chat_session_id = Column(String, nullable=True)
    delivery = Column(JSON, nullable=True)  # {"slack": "sent", ...}
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    task = relationship("ScheduledTask", back_populates="runs")


class CloudVectorVersion(Base):
    """One immutable model/dimension choice for a workspace corpus index."""

    __tablename__ = "cloud_vector_versions"

    corpus = Column(String(40), primary_key=True)
    version = Column(String(16), primary_key=True)
    provider = Column(String(32), nullable=False)
    model = Column(String(128), nullable=False)
    dimension = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="building")
    ann_ready = Column(Boolean, nullable=False, default=False)
    source_fingerprint = Column(String(64), nullable=True)
    source_count = Column(Integer, nullable=False, default=0)
    chunk_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)


class CloudVectorSource(Base):
    """The exact GCS object version represented in a vector index."""

    __tablename__ = "cloud_vector_sources"

    corpus = Column(String(40), primary_key=True)
    version = Column(String(16), primary_key=True)
    source_key = Column(String(64), primary_key=True)
    source_uri = Column(String(2048), nullable=False)
    generation = Column(BigInteger, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    chunk_count = Column(Integer, nullable=False, default=0)
    deleted = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (Index("ix_cloud_vector_sources_version", "corpus", "version"),)


class CloudVectorChunk(Base):
    """Client-key-generated vector and its exact source inside one version."""

    __tablename__ = "cloud_vector_chunks"

    corpus = Column(String(40), primary_key=True)
    version = Column(String(16), primary_key=True)
    source_key = Column(String(64), primary_key=True)
    chunk_id = Column(String(255), primary_key=True)
    source_uri = Column(String(2048), nullable=False)
    doc_id = Column(String(512), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    metadata_json = Column("metadata", JSON, nullable=False, default=dict)
    embedding = Column(JSON().with_variant(ARRAY(REAL()), "postgresql"), nullable=False)

    __table_args__ = (
        Index("ix_cloud_vector_chunks_version", "corpus", "version"),
        Index("ix_cloud_vector_chunks_source", "corpus", "version", "source_key"),
        Index("ix_cloud_vector_chunks_doc", "corpus", "version", "doc_id"),
    )


class CloudVectorUpdate(Base):
    """Durable source change to reconcile before a candidate can activate."""

    __tablename__ = "cloud_vector_updates"

    id = Column(String(36), primary_key=True)
    sequence = Column(BigInteger, nullable=False, unique=True)
    corpus = Column(String(40), nullable=False)
    source_key = Column(String(64), nullable=False)
    source_uri = Column(String(2048), nullable=False)
    generation = Column(BigInteger, nullable=False)
    operation = Column(String(8), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    applied_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_cloud_vector_updates_pending", "corpus", "status"),
        Index("ix_cloud_vector_updates_source_seq", "corpus", "source_key", "sequence"),
    )


# Register the subdomain tables that live in sibling modules on Base.metadata, so
# Alembic autogenerate and Base.metadata.create_all() pick them up:
#   * the EOS tables;
#   * the Smart Teams tables (st_*);
#   * the call-capture tables (call_*);
#   * the Call Scripts tables (call_scripts, call_script_items, call_session_scripts,
#     call_company_product_defaults);
#   * the Sales Cockpit commitment tables (commitment*, digest_logs);
#   * the My To-Dos ledger (promises, promise_events);
#   * the Loose Ends tables (loop_items, loop_scan_runs, loop_mutes,
#     loop_digest_posts);
#   * the Insights telemetry tables (activity_daily, priority_events);
#   * the What's New opens (whats_new_opens);
#   * the deal defers (deal_defers);
#   * the Cubie for Chrome keys (browser_connections);
#   * the Meeting Skills tables (meeting_templates, meeting_skills,
#     meeting_skill_runs, meeting_routines…).
# Imported for side effects (table registration) only — references live in
# rubik_core.database.{browser_connections,call_capture,call_scripts,commitments,
# deal_defers,eos,insights,loop_closure,meeting_skills,promises,proposals,
# smart_teams,whats_new}. Dropping any name here does not fail loudly: autogenerate
# simply stops seeing those tables, and tests start passing against a schema that
# does not contain them.
from rubik_core.database import (  # noqa: E402, F401
    browser_connections,
    call_capture,
    call_scripts,
    commitments,
    deal_defers,
    eos,
    insights,
    loop_closure,
    meeting_skills,
    promises,
    proposals,
    smart_teams,
    whats_new,
)
