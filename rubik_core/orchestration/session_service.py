"""Session service factory for the chat assistant.

Returns a Cloud SQL-backed ``DatabaseSessionService`` in GCP mode (so ADK
sessions — including parked HITL confirmations — survive across the 3 Cloud Run
instances), and an ``InMemorySessionService`` locally.

Background (chat-hallucination RCA — Part B2): the previous implementation
imported ``google.adk.sessions.FirestoreSessionService``, which does NOT exist
in google-adk 1.30.0. That import always raised, so the GCP branch *silently*
fell back to ``InMemorySessionService`` — per-instance state, meaning a HITL
"yes" could land on a different instance than the one that parked the pending
action and the resume would be lost. This module now:
  * uses the real ``DatabaseSessionService`` on Cloud SQL (shared across
    instances) via the Cloud SQL Python Connector's async support;
  * logs LOUDLY (ERROR) and records the fact when it has to fall back, instead
    of degrading in silence; and
  * exposes ``get_session_backend()`` so ``/health`` reports which backend is
    actually live.

ADK's ``DatabaseSessionService`` manages its own tables (``sessions``,
``events``, ``app_states``, ``user_states``) and creates them lazily on first
use — no Alembic migration is required. The runtime DB user must therefore have
CREATE privilege on the database.

THOUGHT SIGNATURES (Gemini 3.x): multi-turn calls must hand a model's
``Part.thought_signature`` back on the next turn, or the model loses the
reasoning chain behind its own earlier tool calls. This store already satisfies
that, and it is worth knowing WHY before anyone "optimizes" it: ADK persists
each event as ``event.model_dump(exclude_none=True, mode="json")`` and rehydrates
with ``Event.model_validate`` (``google.adk.sessions.schemas.v1.StorageEvent``),
and google-genai serializes the signature's raw bytes as base64 — so it survives
the JSON column intact. Assembling the next request then deep-copies each stored
Content, and function-call parts are explicitly never dropped even when flagged
as thoughts (``google.adk.flows.llm_flows.contents``). So: keep whole Events in
the store. Storing a text-only projection of a turn, or rebuilding model turns
by hand from ChatMessage rows, silently breaks the circulation.
``backend/tests/test_thought_signature_circulation.py`` locks this down.
"""

import asyncio
import logging
import threading

from google.adk.sessions import InMemorySessionService

from rubik_core.config import AppMode, settings
from rubik_core.control.models import WorkspaceConnectionSpec
from rubik_core.control.repository import get_workspace
from rubik_core.tenancy.context import NoWorkspaceContext, current_workspace
from rubik_core.tenancy.secrets import read_registered_db_password

logger = logging.getLogger(__name__)

# Human-readable identifier of the backend actually in use, surfaced via
# get_session_backend() / /health. One of:
#   "database:cloudsql"   — DatabaseSessionService on Cloud SQL (desired in prod)
#   "in_memory:local"     — InMemorySessionService, local/dev by design
#   "in_memory:fallback"  — InMemorySessionService because the DB backend failed
#                            to build (DEGRADED — HITL resume is instance-sticky)
_session_backends: dict[str, str] = {}
_session_services: dict[str, object] = {}
_session_lock = threading.RLock()


def _registered_spec(slug: str) -> WorkspaceConnectionSpec:
    """Resolve the active control row, never process-global DB credentials."""
    workspace = get_workspace(slug)
    if workspace is None or workspace.status != "active":
        raise ValueError(f"Workspace {slug!r} is unknown or inactive")
    if not all(
        isinstance(value, str) and value
        for value in (
            workspace.db_name,
            workspace.db_user,
            workspace.db_password_secret_ref,
        )
    ):
        raise ValueError("Workspace database identity is incomplete")
    return WorkspaceConnectionSpec(
        workspace.db_name, workspace.db_user, workspace.db_password_secret_ref
    )


def _build_database_session_service(slug: str):
    """Build a Cloud SQL-backed DatabaseSessionService, or raise.

    DatabaseSessionService builds its own async engine from the URL + kwargs, so
    we hand it a driver-only URL plus our async connector creator and a tightly
    bounded pool — dev Cloud SQL has a low max_connections and Cloud Run runs
    several instances, so this second pool is kept small on purpose (see the
    connection-budget note in database/db.py).
    """
    from google.adk.sessions import DatabaseSessionService
    from google.cloud.sql.connector import IPTypes, create_async_connector

    spec = _registered_spec(slug)
    if not read_registered_db_password(slug, spec):
        raise ValueError("Workspace database password is unavailable")
    instance = settings.CLOUDSQL_CONNECTION_NAME
    ip_type = IPTypes.PRIVATE if settings.USE_PRIVATE_IP else IPTypes.PUBLIC
    connector = None

    async def async_creator():
        nonlocal connector
        # A cached ADK service must not keep connecting after suspension or a
        # control-row DB identity change. Password rotation remains live.
        if await asyncio.to_thread(_registered_spec, slug) != spec:
            raise PermissionError("Workspace database binding changed")
        password = await asyncio.to_thread(read_registered_db_password, slug, spec)
        if not password:
            raise ValueError("Workspace database password is unavailable")
        if connector is None:
            connector = await create_async_connector()
        return await connector.connect_async(
            instance,
            "asyncpg",
            user=spec.db_user,
            password=password,
            db=spec.db_name,
            ip_type=ip_type,
        )

    return DatabaseSessionService(
        db_url="postgresql+asyncpg://",
        async_creator=async_creator,
        pool_size=2,
        max_overflow=3,
        pool_timeout=30,
        pool_recycle=1800,
    )


def get_session_service():
    """Lazy-init the ADK service bound to this workspace alone."""
    slug = current_workspace()
    with _session_lock:
        cached = _session_services.get(slug)
        if cached is not None:
            return cached

        use_cloud_sql = settings.APP_MODE == AppMode.GCP and bool(
            settings.CLOUDSQL_CONNECTION_NAME
        )
        if use_cloud_sql:
            try:
                service = _build_database_session_service(slug)
                backend = "database:cloudsql"
                logger.info("ADK sessions: using workspace %s Cloud SQL database", slug)
            except Exception:
                # A degraded store is visible and remains tenant-local.
                service = InMemorySessionService()
                backend = "in_memory:fallback"
                logger.error(
                    "ADK sessions: FAILED to build %s DatabaseSessionService; "
                    "HITL confirmations may not resume across instances",
                    slug,
                    exc_info=True,
                )
        else:
            service = InMemorySessionService()
            backend = "in_memory:local"
            logger.info("ADK sessions: using %s in-memory service", slug)
        _session_services[slug] = service
        _session_backends[slug] = backend
        return service


def get_session_backend() -> str:
    """Return the session backend actually in use (for /health).

    Initializes the service on first call so the answer reflects reality rather
    than intent. A value of ``in_memory:fallback`` in GCP indicates a DEGRADED
    state — the DB backend failed to build.
    """
    try:
        slug = current_workspace()
    except NoWorkspaceContext:
        # /health is unauthenticated. It must not choose a tenant or open a
        # control/tenant DB merely to report a process-wide backend label.
        return "workspace_scoped"
    get_session_service()
    return _session_backends[slug]
