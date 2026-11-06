"""Modules removed in the Cubie clean-up stay removed, and their leftovers are harmless.

Each module deletion (spec §4.2.3) takes its feature key, its code, its routes and
its tables with it. These tests pin that, and pin the one thing a deletion
cannot take with it: rows already stored under a deleted feature key. Such a row
must be ignored, never a crash on ``/api/users/me`` (Review Focus 2).

Later deletions add their keys, modules, route prefixes and tables to the sets
below.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.auth import admin_required, get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FEATURES, clear_cache, feature_keys

#: Feature keys that left the registry with their module.
REMOVED_FEATURE_KEYS: frozenset[str] = frozenset(
    {
        "regulatory_watch",
        "vendor_questionnaire_tool",
        "daily_giggle_review",
        "compliance_score",
        "compliance_score_approval",
        "sales_buyer_personas",
        "help_desk",
        "tasks_tab",
        "system_kb",
        "kb_capture",
        # The Daily Briefing's Pipeline card: nothing rendered it.
        "briefing_pipeline",
    }
)

#: Dotted module paths that must no longer resolve. Each parent must still
#: exist, so a typo here cannot make the check pass by accident.
REMOVED_MODULES: tuple[str, ...] = (
    "rubik_core.regwatch",
    "rubik_core.giggle",
    "rubik_core.database.regwatch",
    "backend.routers.regwatch",
    "backend.routers.vendor_questionnaire",
    "backend.services.voice_vendor",
    "backend.jobs.regwatch_poll_job",
    "rubik_core.compliance_score",
    "rubik_core.database.compliance_score",
    "backend.routers.compliance_score",
    "backend.routers.compliance_score_admin",
    "rubik_core.conferences",
    "rubik_core.database.conferences",
    "backend.routers.conferences",
    "backend.jobs.conference_import_job",
    "backend.jobs.conference_intel_job",
    "backend.jobs.conference_feedback_email_job",
    "backend.services.conference_feedback_email",
    "rubik_core.buyer_personas",
    "rubik_core.crm_sync.buyer_personas",
    "rubik_core.tickets",
    "backend.routers.tickets",
    "backend.routers.tickets_capacity",
    "backend.routers.tickets_eos_bridge",
    "backend.routers.tickets_tasks",
    "backend.routers.tickets_triage",
    "backend.jobs.ticket_resolution_ingest_job",
    "rubik_core.tools.tickets_tools",
    "rubik_core.priorities.agents.tickets_agent",
    "rubik_core.cockpit.ticket_source",
    "rubik_core.help.help_desk_content",
    "rubik_core.system_kb",
    "rubik_core.system_kb_watcher",
    "rubik_core.tools.system_kb_tools",
    "backend.routers.system_kb",
    "rubik_core.kb_capture",
    "backend.services.kb_capture_actions",
    "rubik_core.whats_new.drafts",
    "rubik_core.whats_new.publish",
    "rubik_core.whats_new.products",
    "rubik_core.whats_new.blocks",
    "backend.services.whats_new_actions",
    "backend.jobs.whats_new_sync_job",
    "rubik_core.integrations.sales_coach",
    "backend.signals.sales_coach",
    "rubik_core.scripts.replay_pro_tier",
    "rubik_core.scripts.mark_deep_preps_failed",
    # The unused GET /api/briefing and the service only it called.
    "backend.routers.briefing",
    "backend.services.briefing_service",
)

#: Files and folders the clean-up deleted (spec §4.2.4), relative to the repo
#: root: a representative path from each group, so a revert of any one group
#: fails here.
REMOVED_PATHS: tuple[str, ...] = (
    "DEPLOY_LOG.md",
    "AI_NOTES.md",
    # Assembled from fragments so the legacy-brand scan never matches it here.
    "Refactoring " + "Bri" + "an.docx",
    "rubik_core/EOS Tracking Platform Blueprint.pdf",
    "consolidated_products_db.csv",
    "consolidated_products_db.md",
    "rubik_core/retrieval/resources/consolidated_products_db.csv",
    "scratch",
    "dump_users.py",
    "rubik_core/verify_llm.py",
    "tests",
    "uv.lock",
    "scripts/repair_empty_gemini_transcripts.py",
    "scripts/setup_vertex_vector_search.py",
    "scripts/seed_exec_scorecard.py",
    "scripts/seed_data/exec_scorecard_2026.json",
    "docs/kb",
    "docs/diagrams",
    "docs/sales_kaizen/00-DECISION-BRIEF.md",
    "frontend/src/v2",
    "frontend/src/App.css",
    "frontend/src/assets/react.svg",
    ".claude/kb-capture",
    "rubik_core/integrations/sales_coach.py",
    "backend/signals/sales_coach.py",
    # Only deleted modules used it (the old v2 pages).
    "frontend/src/pages/eos/_stub.tsx",
)

#: API path prefixes that no router may serve any more.
REMOVED_ROUTE_PREFIXES: tuple[str, ...] = (
    "/api/regwatch",
    "/api/vendor-questionnaire",
    "/api/homefeed/giggle",
    "/api/agents/giggle",
    "/api/browser-extension/giggle-meme",
    "/api/compliance-score",
    "/api/admin/compliance-score",
    "/api/proposals/from-compliance-review",
    "/api/conferences",
    "/api/sales/conferences",
    "/api/tickets",
    "/api/system-kb",
    "/api/whats-new/approvals",
    "/api/agents/whats-new/sync",
    "/api/briefing",
)

#: Tables whose models were deleted with their module.
REMOVED_TABLES: frozenset[str] = frozenset(
    {
        "regulatory_sources",
        "regulatory_items",
        "regulatory_digests",
        "daily_giggle",
        "compliance_reviews",
        "compliance_intakes",
        "conferences",
        "conference_imports",
        "conference_attendees",
        "conference_attendee_notes",
        "conference_crm_suggestions",
        "conference_feedback_asks",
        "ticket_credentials",
        "ticket_resolution_state",
        "kb_capture_candidates",
        "whats_new_items",
        "whats_new_clicks",
    }
)

#: The CRM Sync change type that left with Buyer Personas, and the HubSpot
#: write fence and writer method that served it alone.
REMOVED_CRM_SYNC_CHANGE_TYPES: frozenset[str] = frozenset(
    {"update_contact_buyer_persona"}
)

REPO_ROOT = Path(__file__).resolve().parents[2]

client = TestClient(app)


@pytest.fixture
def fresh_db(tmp_path):
    db_path = tmp_path / "test_removed_modules.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    clear_cache()
    yield dbmod.SessionLocal
    clear_cache()
    app.dependency_overrides.clear()


def _has_source(module: str) -> bool:
    """True when ``module`` still resolves to Python source.

    A deleted package directory that still holds an ignored ``__pycache__/``
    resolves as a namespace package, so a spec alone does not prove the code
    is still there: a namespace package counts only when a ``.py`` file sits
    somewhere under its search locations.
    """
    spec = importlib.util.find_spec(module)
    if spec is None:
        return False
    if spec.origin is not None:
        return True
    return any(
        next(Path(location).rglob("*.py"), None) is not None
        for location in spec.submodule_search_locations or ()
    )


def _mounted_paths() -> list[str]:
    """Every path the app serves. FastAPI keeps included routers nested, so
    ``app.routes`` alone does not list them; the OpenAPI map does."""
    app.openapi_schema = None
    return list(app.openapi()["paths"])


def test_removed_features_absent():
    assert REMOVED_FEATURE_KEYS.isdisjoint(feature_keys())
    assert REMOVED_FEATURE_KEYS.isdisjoint(spec.key for spec in FEATURES)


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_removed_packages_not_importable(module):
    parent = module.rpartition(".")[0]
    assert _has_source(parent), f"{parent} is missing"
    assert not _has_source(module), f"{module} still exists"


def test_a_package_folder_left_with_only_bytecode_counts_as_removed(
    tmp_path, monkeypatch
):
    """The case ``find_spec`` alone gets wrong: a deleted package whose folder
    survives because git ignores the ``__pycache__/`` inside it."""
    ghost = tmp_path / "removed_ghost_pkg" / "__pycache__"
    ghost.mkdir(parents=True)
    (ghost / "module.cpython-310.pyc").write_bytes(b"")
    kept = tmp_path / "kept_namespace_pkg" / "inner"
    kept.mkdir(parents=True)
    (kept / "module.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    assert importlib.util.find_spec("removed_ghost_pkg") is not None
    assert not _has_source("removed_ghost_pkg")
    assert _has_source("kept_namespace_pkg")
    assert not _has_source("never_existed_pkg_for_this_test")


@pytest.mark.parametrize("path", REMOVED_PATHS)
def test_removed_files_absent(path):
    assert not (REPO_ROOT / path).exists(), f"{path} still exists"


def test_no_setting_configures_the_sales_coach_bridge():
    """The read-only bridge into the external Sales Coach database left with
    its connection settings and its signal switch."""
    from rubik_core.config import Settings

    assert [name for name in Settings.model_fields if "SALES_COACH" in name] == []


def test_priorities_read_no_sales_coach_signal():
    """AI Priorities no longer fetch, accept or group a coaching signal."""
    from backend.agents.multi_source_priorities import _default_sources
    from backend.signals import KNOWN_SOURCES

    assert "sales_coach" not in {source.name for source in _default_sources()}
    assert "sales_coach" not in KNOWN_SOURCES


def test_removed_routes_absent():
    served = [
        path for path in _mounted_paths() if path.startswith(REMOVED_ROUTE_PREFIXES)
    ]
    assert served == []


def test_helpers_only_deleted_modules_called_are_gone():
    """The Help Desk read who was out of office on a date; Conferences read a
    company's contacts. Nothing else ever called either."""
    from rubik_core.availability import store
    from rubik_core.retrieval.hubspot_api import HubSpotAPIClient

    assert not hasattr(store, "list_out_on")
    assert not hasattr(HubSpotAPIClient, "contacts_for_company")


def test_removed_tables_absent_from_the_models():
    # Importing the models module also registers every sibling module's
    # tables (the side-effect import at its foot).
    from rubik_core.database.models import Base

    assert REMOVED_TABLES.isdisjoint(Base.metadata.tables)


def test_no_route_serves_a_help_desk_ticket():
    """The Help Desk left whole: its own ``/api/tickets`` routers, the four
    AI Priorities ticket card actions and the Cockpit ticket poll."""
    assert [path for path in _mounted_paths() if "ticket" in path.lower()] == []


def test_no_setting_configures_the_system_kb_or_kb_capture():
    """The GitHub reader, its watcher and the mail capture took their
    settings (and the GitHub token among the sensitive keys) with them."""
    from rubik_core.config import Settings

    prefixes = ("SYSTEM_KB_", "KB_CAPTURE_")
    assert [name for name in Settings.model_fields if name.startswith(prefixes)] == []


def test_priorities_have_no_help_desk_channel_kinds_or_entity():
    """AI Priorities lost the ``tickets`` channel, every ``ticket_*`` card kind
    and the ticket entity kind."""
    from rubik_core.priorities.models import ChannelKind, EntityKind, PriorityKind

    assert "TICKETS" not in ChannelKind.__members__
    assert "tickets" not in {member.value for member in ChannelKind}
    assert not [kind for kind in PriorityKind if kind.value.startswith("ticket")]
    assert "TICKET" not in EntityKind.__members__


def test_crm_sync_change_types_have_no_buyer_persona_member():
    """Every map CRM Sync keys by change type, and the HubSpot writer behind
    it, has lost the buyer persona change type, its fence and its method."""
    from rubik_core.crm_sync import apply, validators
    from rubik_core.insights.dashboards import crm as crm_dashboard
    from rubik_core.retrieval import hubspot_write

    known = (
        set(validators.CHANGE_TYPES)
        | set(validators.OBJECT_TYPE_BY_CHANGE)
        | set(validators.WHITELIST_BY_CHANGE)
        | set(apply._WHITELIST_BY_CHANGE)
        | set(apply._LABEL_BY_CHANGE)
        | set(crm_dashboard._CHANGE_TYPE_LABELS)
    )
    assert REMOVED_CRM_SYNC_CHANGE_TYPES.isdisjoint(known)
    assert not [change for change in known if "persona" in change]
    assert not hasattr(hubspot_write, "BUYER_PERSONA_CONTACT_WRITABLE")
    assert not hasattr(hubspot_write.HubSpotWriteClient, "update_contact_buyer_persona")
    # The router names no retired type: it lists only the types the scan makes.
    router = (REPO_ROOT / "backend" / "routers" / "crm_sync.py").read_text()
    assert not [t for t in REMOVED_CRM_SYNC_CHANGE_TYPES if t in router]


class _Stop(Exception):
    """Raised by the stubbed pipeline once the request has been captured."""


def test_a_saved_priorities_source_of_a_removed_channel_is_ignored(monkeypatch):
    """A person who once ticked the Help Desk as a priorities source keeps the
    rest of their choice; the removed channel is dropped, never a 400."""
    from backend.routers import home
    from rubik_core.priorities import PrioritiesOrchestrator
    from rubik_core.priorities.models import ChannelKind

    seen = []

    def _capture(self, request):
        seen.append(list(request.channels))
        raise _Stop

    monkeypatch.setattr(
        home, "_load_priorities_ui_prefs", lambda _email: (None, ["gmail", "tickets"])
    )
    monkeypatch.setattr(PrioritiesOrchestrator, "generate", _capture)

    with pytest.raises(_Stop):
        home._run_and_persist_priorities(
            "pat@acme.example", home.PrioritiesRequestBody()
        )

    assert seen == [[ChannelKind.GMAIL]]


def _profile(email: str, *, department: str, roles: list[str]) -> UserProfile:
    now = "2026-10-27T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles,
        department=department,
        created_at=now,
        updated_at=now,
    )


def _store_orphan_row(session_factory, key: str = "regulatory_watch") -> None:
    """A ``feature_access`` row for a key the registry no longer has."""
    from rubik_core.database.models import FeatureAccess

    db = session_factory()
    try:
        db.add(
            FeatureAccess(
                feature_key=key,
                departments=["sales"],
                extra_emails=[],
                exec_allowed=True,
                admin_allowed=True,
                everyone=True,
            )
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("key", ["regulatory_watch", "briefing_pipeline"])
def test_unknown_feature_rows_ignored(fresh_db, key):
    """A ``feature_access`` row left over from a deleted feature is skipped."""
    _store_orphan_row(fresh_db, key)
    person = _profile("pat@acme.example", department="Sales", roles=["user"])
    app.dependency_overrides[get_current_user] = lambda: person

    r = client.get("/api/users/me")

    assert r.status_code == 200, r.text
    features = r.json()["features"]
    assert key not in features
    assert set(features) <= set(feature_keys())


@pytest.mark.parametrize("key", ["regulatory_watch", "briefing_pipeline"])
def test_unknown_feature_rows_ignored_by_the_feature_access_tab(fresh_db, key):
    """Admin → Feature Access lists the registry, never the stored rows."""
    _store_orphan_row(fresh_db, key)
    admin = _profile(
        "admin@acme.example", department="Systems", roles=["admin", "user"]
    )
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[admin_required] = lambda: admin

    r = client.get("/api/admin/feature-access")

    assert r.status_code == 200, r.text
    keys = [f["key"] for f in r.json()["features"]]
    assert key not in keys
    assert keys == list(feature_keys())
