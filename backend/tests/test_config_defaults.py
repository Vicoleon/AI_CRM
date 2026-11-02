"""Process configuration carries no company-specific values.

Workspace domains, impersonation lists, HubSpot properties, sender policy and
the business time zone belong to workspace-owned data. A fresh process names
no company and reaches no company's cloud project by accident.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from backend.jobs import worker_job_execute_command
from rubik_core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def _default(name: str) -> object:
    field = Settings.model_fields[name]
    return field.default_factory() if field.default_factory else field.default


@pytest.fixture
def fresh(monkeypatch) -> Settings:
    """``Settings()`` built from an empty environment and no ``.env`` file."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)


def test_a_fresh_install_names_no_company(fresh):
    assert "ADMIN_EMAILS" not in Settings.model_fields
    assert "ADMIN_EMAIL" not in Settings.model_fields
    assert "GOOGLE_WORKSPACE_SERVICE_ACCOUNT_KEY" not in Settings.model_fields
    for credential in (
        "HUBSPOT_PRIVATE_APP_TOKEN",
        "HUBSPOT_WRITE_TOKEN",
        "FATHOM_API_KEY",
        "FATHOM_WEBHOOK_SECRET",
        "SLACK_CLIENT_SECRET",
        "SLACK_SIGNING_SECRET",
        "LINKEDIN_DATA_API_KEY",
    ):
        assert credential not in Settings.model_fields
    assert "GOOGLE_WORKSPACE_DOMAIN" not in Settings.model_fields
    assert "INTERNAL_EMAIL_DOMAINS" not in Settings.model_fields
    assert "LOOP_CLOSURE_INTERNAL_DOMAINS" not in Settings.model_fields
    moved = {
        "IMPERSONATION_ALLOWED_EMAILS",
        "USER_IMPERSONATION_TARGETS",
        "CUBIE_DEEP_PREP_EMAIL_RECIPIENT_OVERRIDE",
        "CUBIE_DEEP_PREP_SENDER_NAME",
        "CUBIE_DEEP_PREP_SENDER_MAILBOX",
        "CUBIE_L10_SUMMARY_SENDER_MAILBOX",
        "CUBIE_L10_SUMMARY_EMAIL_RECIPIENT_OVERRIDE",
        "CUBIE_L10_SUMMARY_START_AFTER",
        "LOOP_CLOSURE_SCORECARD_METRIC",
        "INSIGHTS_EXCLUDED_EMAILS",
        "INGESTION_COUNCIL_RECIPIENTS",
        "INGESTION_COUNCIL_DEPARTMENT",
        "COCKPIT_BUSINESS_TZ",
        "PRIORITIES_DEFAULT_TZ",
        "SCHEDULED_TASKS_EMAIL_RECIPIENT_OVERRIDE",
    }
    assert moved.isdisjoint(Settings.model_fields)


def test_the_mock_auth_developer_is_fictional(fresh):
    assert fresh.CUBIE_DEV_USER_EMAIL == "dev@acme.example"


def test_hubspot_ids_and_properties_start_empty(fresh):
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    moved = {
        "HUBSPOT_PORTAL_ID",
        "HUBSPOT_PRIORITIES_PIPELINE_IDS",
        "HUBSPOT_CLOSED_WON_STAGE_IDS",
        "COCKPIT_STAGE_LAG_STAGES",
        "HUBSPOT_DEAL_PRODUCT_PROPERTY",
        "HUBSPOT_DEAL_TURNAROUND_PROPERTY",
        "XRAY_RENEWAL_DATE_PROPERTY",
        "XRAY_AUTO_RENEW_PROPERTIES",
        "XRAY_ACCOUNT_STATUS_PROPERTIES",
        "XRAY_CSM_OWNER_PROPERTY",
        "XRAY_EXTRA_DEAL_PROPERTIES",
        "XRAY_EXTRA_COMPANY_PROPERTIES",
        "CCS_VENDOR_FIELDS_DEFAULT",
        "CCS_VENDOR_FIELDS_EXTENDED",
        "SALES_RHYTHM_LATE_STAGES",
        "SCORECARD_SYNC_DEPARTMENT",
        "SCORECARD_SYNC_METRICS",
    }
    assert moved.isdisjoint(type(fresh).model_fields)
    assert fresh.BQ_DATA_AGENT_RESOURCE == ""
    assert not HubSpotConnectorConfig().pipeline_ids
    assert HubSpotConnectorConfig().portal_id is None


def test_the_scorecard_sync_and_the_ops_audience_name_no_team(fresh):
    from rubik_core.company.operations import OperationsConfig
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    assert HubSpotConnectorConfig().scorecard_sync_department == ""
    assert HubSpotConnectorConfig().scorecard_sync_metrics == {}
    assert OperationsConfig().ingestion_council_department == ""
    assert OperationsConfig().ingestion_council_recipients == ()
    assert OperationsConfig().loop_closure_scorecard_metric == ""


def test_new_control_workspace_time_zone_defaults_to_utc(fresh):
    from rubik_core.control.models import Workspace

    assert Workspace.__table__.c.time_zone.default.arg == "UTC"
    assert "COCKPIT_BUSINESS_TZ" not in type(fresh).model_fields
    assert "PRIORITIES_DEFAULT_TZ" not in type(fresh).model_fields


# The business clock is each company's own setting, so no copy a person reads
# (Help in both homes, page tours, Insights) may name one fixed zone.
_FIXED_CLOCK = re.compile(r"\bcentral\b|chicago", re.IGNORECASE)


def _code_strings(path: Path) -> list[str]:
    """Every string literal in a Python module except its docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _without_comments(source: str) -> str:
    """TS/TSX source with comments blanked; line numbers are kept."""
    source = re.sub(
        r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), source, flags=re.DOTALL
    )
    return re.sub(r"(?<![:\w])//.*$", "", source, flags=re.MULTILINE)


def test_backend_copy_names_no_fixed_business_clock():
    modules = [REPO_ROOT / "rubik_core/help/content.py"]
    modules += sorted((REPO_ROOT / "rubik_core/insights").rglob("*.py"))
    hits = [
        f"{path.relative_to(REPO_ROOT)}: {text[:90]!r}"
        for path in modules
        for text in _code_strings(path)
        if _FIXED_CLOCK.search(text)
    ]
    assert hits == []


def test_job_cadences_run_on_the_business_clock():
    """A worker job that wants a wall-clock cadence says it in the company's
    business time zone, the value its scheduler entry passes as --time-zone."""
    from backend.jobs import BUSINESS_CLOCK, WORKER_JOBS

    timed = {
        name: job.wants_cadence
        for name, job in WORKER_JOBS.items()
        if re.search(r"\d\d:\d\d", job.wants_cadence)
    }
    assert timed, "no job names a wall-clock cadence any more"
    for name, cadence in timed.items():
        assert cadence.endswith(BUSINESS_CLOCK), (name, cadence)
        assert not _FIXED_CLOCK.search(cadence), (name, cadence)


def test_frontend_copy_names_no_fixed_business_clock():
    src = REPO_ROOT / "frontend/src"
    files = sorted((src / "pages/help/topics").glob("*.tsx"))
    files += sorted((src / "tours/content").glob("*.ts"))
    files += sorted((src / "components/insights").glob("*.tsx"))
    files += sorted(
        path
        for path in (src / "pages/insights").glob("*.ts*")
        if ".test." not in path.name
    )
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:90]}"
        for path in files
        for number, line in enumerate(
            _without_comments(path.read_text(encoding="utf-8")).splitlines(), 1
        )
        if _FIXED_CLOCK.search(line)
    ]
    assert hits == []


def test_local_dev_keeps_its_origin_and_no_cloud_host(fresh):
    assert "http://localhost:5173" in fresh.ALLOWED_ORIGINS
    assert all(
        origin.startswith("http://localhost:") for origin in fresh.ALLOWED_ORIGINS
    )


#: Defaults that ARE a mailbox on purpose, each on the reserved ``.example``
#: domain (RFC 2606), so no real address can hide behind the exemption.
_FICTIONAL_MAILBOX_DEFAULTS = frozenset({"CUBIE_DEV_USER_EMAIL"})


@pytest.mark.parametrize("name", sorted(Settings.model_fields))
def test_no_default_names_a_mailbox_or_a_cloud_host(name):
    text = repr(_default(name))
    if name in _FICTIONAL_MAILBOX_DEFAULTS:
        assert text.endswith("@acme.example'"), (name, text)
    else:
        assert "@" not in text, (name, text)
    assert ".run.app" not in text, (name, text)


def test_an_empty_environment_has_no_process_delegation_subject(tmp_path):
    """No import-time path revives a process-global DWD identity."""
    keep = {"PATH", "HOME", "LANG", "SYSTEMROOT", "TMPDIR"}
    env = {key: value for key, value in os.environ.items() if key in keep}
    env.update(
        PYTHONPATH=str(REPO_ROOT),
        APP_MODE="dev_mock_auth",
        DATABASE_URL=f"sqlite:///{tmp_path / 'probe.db'}",
    )
    probe = (
        "from rubik_core.config import settings\n"
        "print(hasattr(settings, 'ADMIN_EMAIL'), hasattr(settings, 'GOOGLE_WORKSPACE_DOMAIN'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,  # no .env here
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == "False False"


def test_a_missing_gcp_project_is_a_clear_error(monkeypatch):
    from rubik_core.config import GcpProjectNotSet, gcp_project_id, settings

    monkeypatch.setattr(settings, "GCP_PROJECT_ID", None)
    with pytest.raises(GcpProjectNotSet, match="GCP_PROJECT_ID not set"):
        gcp_project_id()
    with pytest.raises(GcpProjectNotSet, match="GCP_PROJECT_ID not set"):
        worker_job_execute_command("transcript-sync", workspace="test")


def test_the_configured_gcp_project_is_used(monkeypatch):
    from rubik_core.config import gcp_project_id, settings

    monkeypatch.setattr(settings, "GCP_PROJECT_ID", "acme-dev")
    assert gcp_project_id() == "acme-dev"
    assert "--project=acme-dev " in worker_job_execute_command(
        "transcript-sync", workspace="test"
    )
