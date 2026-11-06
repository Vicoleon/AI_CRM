"""Test-wide defaults for the backend suite.

Three things, all about not coupling feature tests to deployment configuration
or to each other, plus one opt-in carve-out (``roster_admits_acme_example``)
for the tests that need the ``@acme.example`` colleagues in the AI Priorities
roster.

**The test Workspace domain is ``acme.example`` in control data.** The
repo-root pytest bootstrap creates that row in a private per-worker SQLite
control DB. ``set_test_workspace_domains`` changes it for cases that exercise
another company or an unconfigured workspace, then restores it.

**The cached Workspace domain starts empty in every test.** The resolver
caches control rows per workspace, and tests swap domains and stubs freely.

**The business clock is pinned to ``America/Chicago``.** Configuration ships
with both time zones at UTC (``test_config_defaults.py`` pins that), but the
fixtures' dates, business-hour windows and "which day is it" cases were written
on a Central-time clock. Pinning the zone they were written in keeps every one
of them meaning what it says.
"""

from __future__ import annotations

import pytest

#: The Workspace domain every fixture colleague is written against.
FIXTURE_DOMAIN = "acme.example"

# Direct calls to a Job main in these suites need the same explicit input a
# Cloud Run execution receives. Keep it local to those tests: setting the env
# for all pytest workers changes Alembic's explicit `-x workspace=...` target.
_JOB_MAIN_TEST_MODULES = frozenset(
    {
        "test_ai_cost",
        "test_close_date_rollup",
        "test_crm_sync_job",
        "test_deep_prep_job",
        "test_eos_ingest_debounce",
        "test_job_bootstrap",
        "test_loop_closure_job",
        "test_meeting_prep_background",
        "test_priorities_on_demand_job",
        "test_smart_teams_comparison",
        "test_smart_teams_guide",
        "test_smart_teams_score_job",
        "test_smart_teams_uploads",
        "test_smart_teams_wave1_integration",
        "test_smart_teams_wave2_integration",
        "test_stale_work_sweep",
        "test_uc42_batch_runner",
    }
)


@pytest.fixture(autouse=True)
def _explicit_job_workspace_for_direct_main_tests(request, monkeypatch):
    if request.module.__name__.rsplit(".", 1)[-1] in _JOB_MAIN_TEST_MODULES:
        monkeypatch.setenv("CUBIE_WORKSPACE", "test")


@pytest.fixture
def set_test_workspace_domains():
    """Replace explicit test-workspace control domains for one test.

    Call with no domains to test an unconfigured workspace. Primary defaults
    to the first supplied domain. The original rows are restored afterward.
    """
    from sqlalchemy import delete, select

    from rubik_core.company.domain import clear_cache, fold_domain
    from rubik_core.control.db import control_session
    from rubik_core.control.models import WorkspaceDomain

    with control_session() as session:
        original = [
            (row.domain, row.is_primary)
            for row in session.scalars(
                select(WorkspaceDomain).where(WorkspaceDomain.workspace_slug == "test")
            )
        ]

    def replace(*domains: str, primary: str | None = None, slug: str = "test"):
        normalized = tuple(dict.fromkeys(fold_domain(value) for value in domains))
        if any(not value for value in normalized):
            raise ValueError("A domain cannot be blank")
        selected = (
            fold_domain(primary)
            if primary is not None
            else (normalized[0] if normalized else "")
        )
        if selected and selected not in normalized:
            raise ValueError("Primary domain must be in domains")
        with control_session() as session:
            session.execute(
                delete(WorkspaceDomain).where(WorkspaceDomain.workspace_slug == slug)
            )
            session.add_all(
                WorkspaceDomain(
                    workspace_slug=slug, domain=value, is_primary=value == selected
                )
                for value in normalized
            )
            session.commit()
        clear_cache(slug)

    try:
        yield replace
    finally:
        with control_session() as session:
            session.execute(
                delete(WorkspaceDomain).where(WorkspaceDomain.workspace_slug == "test")
            )
            session.add_all(
                WorkspaceDomain(
                    workspace_slug="test", domain=value, is_primary=is_primary
                )
                for value, is_primary in original
            )
            session.commit()
        clear_cache("test")


@pytest.fixture(autouse=True)
def _fresh_workspace_domain_cache():
    """No test reads a Workspace domain another test cached."""
    from rubik_core.company.domain import clear_cache

    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def roster_admits_acme_example(monkeypatch):
    """Let the ``@acme.example`` fixture colleagues into the AI Priorities roster.

    Production never admits a ``*.example`` address to that pipeline
    (``rubik_core.priorities.batch.is_pipeline_fixture_email``): no Workspace
    mailbox lives on a reserved name, so the batch would only make Domain-Wide
    Delegation calls into nothing. A test about WHO the batch, the Slack DM or
    the Loose Ends scan reaches still needs colleagues, and every fixture
    colleague is on ``acme.example``. This is the one, explicit carve-out: a
    module or test opts in by name (``pytest.mark.usefixtures``), it admits
    exactly that domain, and every other fixture address (``*.local``,
    ``example.com``, ``outside.example``) is refused as in production.
    """
    from rubik_core.priorities import batch

    production_rule = batch.is_pipeline_fixture_email

    def _admits_the_fixture_colleagues(email: str | None) -> bool:
        if (email or "").strip().lower().endswith(f"@{FIXTURE_DOMAIN}"):
            return False
        return production_rule(email)

    monkeypatch.setattr(
        batch, "is_pipeline_fixture_email", _admits_the_fixture_colleagues
    )


@pytest.fixture(autouse=True)
def _fresh_workspace_registry():
    """Do not retain workspace pools or process defaults between test cases."""
    from rubik_core.tenancy import context, registry

    process_workspace = context._process_workspace
    registry.clear_engines()
    try:
        yield
    finally:
        try:
            assert all(
                engine.dialect.name == "sqlite" for engine in registry.cached_engines()
            ), "Non-SQLite workspace registry engine during pytest"
        finally:
            registry.clear_engines()
            context._process_workspace = process_workspace
