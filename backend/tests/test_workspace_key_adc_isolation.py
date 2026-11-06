"""Workspace delegation signs with the selected control row, never a key file."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from rubik_core.tenancy.context import use_workspace

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SERVICE_ACCOUNT = "workspace-acme@project.iam.gserviceaccount.com"
SUBJECT = "admin@acme.example"


def _workspace(*, service_account=SERVICE_ACCOUNT, subject=SUBJECT):
    return SimpleNamespace(
        status="active",
        google_service_account=service_account,
        google_subject=subject,
    )


def test_config_source_never_writes_google_application_credentials():
    import rubik_core.config as config_module

    src = Path(config_module.__file__).read_text()
    for write_expr in (
        'os.environ["GOOGLE_APPLICATION_CREDENTIALS"]',
        "os.environ['GOOGLE_APPLICATION_CREDENTIALS']",
        'environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS"',
        'putenv("GOOGLE_APPLICATION_CREDENTIALS"',
    ):
        assert write_expr not in src


def test_loader_signs_with_cloud_run_runtime_identity_and_control_sa(monkeypatch):
    from rubik_core.tools._workspace_auth import load_workspace_credentials

    monkeypatch.setenv("K_SERVICE", "cubie-api")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/other-key.json")
    runtime_creds = MagicMock(name="runtime_creds")
    signer = MagicMock(name="signer")
    delegated_creds = MagicMock(name="delegated_creds")
    with (
        use_workspace("test"),
        patch(
            "rubik_core.control.repository.get_workspace", return_value=_workspace()
        ) as row,
        patch("google.auth.default") as adc_default,
        patch(
            "google.auth.compute_engine.Credentials", return_value=runtime_creds
        ) as metadata_creds,
        patch("google.auth.iam.Signer", return_value=signer) as signer_factory,
        patch(
            "google.oauth2.service_account.Credentials", return_value=delegated_creds
        ) as dwd_factory,
    ):
        creds = load_workspace_credentials(SCOPES)

    row.assert_called_once_with("test")
    adc_default.assert_not_called()
    metadata_creds.assert_called_once_with(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    signer_factory.assert_called_once()
    request, signer_identity, target_sa = signer_factory.call_args.args
    assert type(request).__name__ == "Request"
    assert signer_identity is runtime_creds
    assert target_sa == SERVICE_ACCOUNT
    dwd_factory.assert_called_once_with(
        signer=signer,
        service_account_email=SERVICE_ACCOUNT,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    assert creds is delegated_creds


def test_local_loader_uses_adc_only_as_iam_signer_identity(monkeypatch):
    from rubik_core.tools._workspace_auth import load_workspace_credentials

    monkeypatch.delenv("K_SERVICE", raising=False)
    runtime_creds = MagicMock()
    with (
        use_workspace("test"),
        patch("rubik_core.control.repository.get_workspace", return_value=_workspace()),
        patch("google.auth.default", return_value=(runtime_creds, "project")) as adc,
        patch("google.auth.compute_engine.Credentials") as metadata_creds,
        patch("google.auth.iam.Signer") as signer,
        patch("google.oauth2.service_account.Credentials"),
    ):
        load_workspace_credentials(SCOPES)

    adc.assert_called_once_with(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    metadata_creds.assert_not_called()
    assert signer.call_args.args[1] is runtime_creds


def test_gcp_job_uses_metadata_even_without_cloud_run_service_env(monkeypatch):
    from rubik_core.config import AppMode, settings
    from rubik_core.tools._workspace_auth import load_workspace_credentials

    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/other-key.json")
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    with (
        use_workspace("test"),
        patch("rubik_core.control.repository.get_workspace", return_value=_workspace()),
        patch("google.auth.default") as adc,
        patch("google.auth.compute_engine.Credentials") as metadata_creds,
        patch("google.auth.iam.Signer"),
        patch("google.oauth2.service_account.Credentials"),
    ):
        assert load_workspace_credentials(SCOPES) is not None
    adc.assert_not_called()
    metadata_creds.assert_called_once_with(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )


@pytest.mark.parametrize("marker", ["CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION"])
def test_cloud_run_job_uses_metadata_even_when_mode_is_local(monkeypatch, marker):
    from rubik_core.config import AppMode, settings
    from rubik_core.tools._workspace_auth import load_workspace_credentials

    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    monkeypatch.setenv(marker, "cubie-worker")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/hostile-key.json")
    with (
        use_workspace("test"),
        patch("rubik_core.control.repository.get_workspace", return_value=_workspace()),
        patch(
            "google.auth.default", side_effect=AssertionError("ADC is forbidden")
        ) as adc,
        patch("google.auth.compute_engine.Credentials") as metadata_creds,
        patch("google.auth.iam.Signer"),
        patch("google.oauth2.service_account.Credentials"),
    ):
        assert load_workspace_credentials(SCOPES) is not None

    adc.assert_not_called()
    metadata_creds.assert_called_once_with(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )


def test_loader_and_configuration_fail_closed_without_live_row():
    from rubik_core.tools._workspace_auth import (
        google_workspace_configured,
        load_workspace_credentials,
        workspace_dwd_subject,
    )

    with (
        use_workspace("test"),
        patch("rubik_core.control.repository.get_workspace", return_value=None),
        patch("google.auth.default") as adc,
        patch("google.auth.compute_engine.Credentials") as metadata_creds,
    ):
        assert google_workspace_configured() is False
        assert workspace_dwd_subject() is None
        assert load_workspace_credentials(SCOPES) is None
    adc.assert_not_called()
    metadata_creds.assert_not_called()


def test_configuration_fails_closed_on_control_error_or_missing_context(monkeypatch):
    from rubik_core.tenancy import context
    from rubik_core.tools._workspace_auth import (
        google_workspace_configured,
        load_workspace_credentials,
    )

    with (
        use_workspace("test"),
        patch(
            "rubik_core.control.repository.get_workspace",
            side_effect=RuntimeError("down"),
        ),
        patch("google.auth.default") as adc,
    ):
        assert not google_workspace_configured()
        assert load_workspace_credentials(SCOPES) is None
    adc.assert_not_called()

    monkeypatch.setattr(context, "_process_workspace", None)
    with patch("rubik_core.control.repository.get_workspace") as row:
        assert not google_workspace_configured()
        assert load_workspace_credentials(SCOPES) is None
    row.assert_not_called()


def test_configuration_reads_each_selected_workspace_and_requires_both_fields():
    from rubik_core.tools._workspace_auth import (
        google_workspace_configured,
        workspace_dwd_subject,
    )

    def lookup(slug):
        return {
            "acme": _workspace(),
            "beta": _workspace(subject=None),
        }[slug]

    with patch("rubik_core.control.repository.get_workspace", side_effect=lookup):
        with use_workspace("acme"):
            assert google_workspace_configured()
            assert workspace_dwd_subject() == SUBJECT
        with use_workspace("beta"):
            assert not google_workspace_configured()
            assert workspace_dwd_subject() is None


def test_fathom_fallback_subject_does_not_require_google_delegation_account():
    from rubik_core.tools._workspace_auth import (
        google_workspace_configured,
        workspace_dwd_subject,
    )

    with (
        use_workspace("test"),
        patch(
            "rubik_core.control.repository.get_workspace",
            return_value=_workspace(service_account=None),
        ),
    ):
        assert not google_workspace_configured()
        assert workspace_dwd_subject() == SUBJECT


def test_support_and_person_impersonation_refuse_before_control_or_signer():
    from rubik_core.auth.impersonation import reset_active, set_active
    from rubik_core.tenancy.support_context import SupportScope, use_support_session
    from rubik_core.tools._workspace_auth import (
        google_workspace_configured,
        load_workspace_credentials,
    )

    with (
        use_workspace("acme"),
        patch("rubik_core.control.repository.get_workspace") as row,
        patch("google.auth.default") as adc,
        patch("google.auth.compute_engine.Credentials") as metadata_creds,
        patch("google.auth.iam.Signer") as signer,
    ):
        with use_support_session(
            SupportScope(
                actor="staff@rubik-soft.com",
                sid="test-sid",
                workspace_slug="acme",
                workspace_name="Acme",
            )
        ):
            assert not google_workspace_configured()
            assert load_workspace_credentials(SCOPES) is None
        token = set_active(True)
        try:
            assert not google_workspace_configured()
            assert load_workspace_credentials(SCOPES) is None
        finally:
            reset_active(token)
    row.assert_not_called()
    adc.assert_not_called()
    metadata_creds.assert_not_called()
    signer.assert_not_called()


def test_directory_defaults_to_workspace_subject_without_adc_env(monkeypatch):
    from rubik_core.tools.directory import GoogleDirectoryClient

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    fake_creds = MagicMock()
    with (
        patch("rubik_core.tools.directory.workspace_dwd_subject", return_value=SUBJECT),
        patch(
            "rubik_core.tools._workspace_auth.load_workspace_credentials",
            return_value=fake_creds,
        ),
        patch("rubik_core.tools.directory.build") as build,
    ):
        GoogleDirectoryClient()
    fake_creds.with_subject.assert_called_once_with(SUBJECT)
    build.assert_called_once()


def test_workspace_service_keeps_with_subject_contract():
    from rubik_core.auth.workspace import WorkspaceService

    fake_creds = MagicMock()
    delegated = fake_creds.with_subject.return_value
    with patch(
        "rubik_core.auth.workspace.load_workspace_credentials", return_value=fake_creds
    ):
        creds = WorkspaceService(registry=MagicMock())._get_credentials(SUBJECT, SCOPES)
    assert creds is delegated
    fake_creds.with_subject.assert_called_once_with(SUBJECT)
