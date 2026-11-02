"""Workspace-owned operational policy is isolated and fails closed."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rubik_core.company import operations
from rubik_core.database.models import AuditLog, WorkspaceSetting
from rubik_core.tenancy.context import current_workspace, use_workspace


@pytest.fixture
def operation_store(tmp_path, monkeypatch):
    engines = {}
    makers = {}
    for slug in ("acme", "bravo"):
        engine = create_engine(
            f"sqlite:///{tmp_path / (slug + '.db')}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        WorkspaceSetting.__table__.create(engine)
        AuditLog.__table__.create(engine)
        engines[slug] = engine
        makers[slug] = sessionmaker(bind=engine)
    monkeypatch.setattr(
        operations, "SessionLocal", lambda: makers[current_workspace()]()
    )
    yield makers
    for engine in engines.values():
        engine.dispose()


def test_distinct_workspaces_keep_distinct_operations(operation_store):
    with use_workspace("acme"):
        assert operations.get_operations_config().to_dict() == (
            operations.OperationsConfig().to_dict()
        )
        operations.patch_operations_config(
            {
                "ingestion_council_recipients": ["Ops@acme.example"],
                "insights_excluded_emails": ["Shared@acme.example"],
                "loop_closure_scorecard_metric": "Open promises",
                "deep_prep_sender_name": "Acme Sales",
                "deep_prep_sender_mailbox": "mail@acme.example",
                "l10_sender_mailbox": "recaps@acme.example",
                "l10_start_after": "2026-11-02",
            }
        )
    with use_workspace("bravo"):
        assert operations.get_operations_config().ingestion_council_recipients == ()
        operations.patch_operations_config(
            {
                "ingestion_council_recipients": ["ops@bravo.example"],
                "l10_start_after": "2026-12-01T09:00:00-06:00",
            }
        )
        assert operations.get_operations_config().l10_start_after == (
            "2026-12-01T09:00:00-06:00"
        )
    with use_workspace("acme"):
        config = operations.get_operations_config()
        assert config.ingestion_council_recipients == ("ops@acme.example",)
        assert config.insights_excluded_emails == ("shared@acme.example",)
        assert config.l10_start_after == "2026-11-02"


def test_missing_policy_has_safe_defaults(operation_store):
    with use_workspace("acme"):
        config = operations.get_operations_config()
        assert config.impersonation_allowed_emails == ()
        assert config.user_impersonation_targets == ()
        assert config.ingestion_council_recipients == ()
        assert config.loop_closure_scorecard_metric == ""
        assert config.deep_prep_sender_mailbox == ""
        assert config.l10_sender_mailbox == ""
        assert config.l10_start_after is None


def test_impersonation_policy_requires_explicit_platform_authority(operation_store):
    with use_workspace("acme"):
        with pytest.raises(operations.OperationsConfigInvalid):
            operations.patch_operations_config(
                {"impersonation_allowed_emails": ["admin@acme.example"]}
            )
        with pytest.raises(operations.OperationsConfigInvalid):
            operations.patch_operations_config(
                {"user_impersonation_targets": ["rep@acme.example"]}
            )
        with pytest.raises(operations.OperationsConfigInvalid, match="Audited"):
            operations.patch_operations_config(
                {"user_impersonation_targets": ["rep@acme.example"]},
                platform_authorized=True,
            )
        saved = operations.patch_operations_config(
            {
                "impersonation_allowed_emails": ["Admin@acme.example"],
                "user_impersonation_targets": ["Rep@acme.example"],
            },
            platform_authorized=True,
            audit_actor="platform@acme.example",
            audit_sid="support-1",
        )
        assert saved.impersonation_allowed_emails == ("admin@acme.example",)
        assert saved.user_impersonation_targets == ("rep@acme.example",)
        with operation_store["acme"]() as db:
            audit = db.query(AuditLog).one()
            assert audit.action == "IMPERSONATION_POLICY_UPDATE"
            assert audit.actor_email == "platform@acme.example"
            assert audit.details["sid"] == "support-1"
            assert audit.details["before"] == {
                "impersonation_allowed_emails": [],
                "user_impersonation_targets": [],
            }
            assert audit.details["after"] == {
                "impersonation_allowed_emails": ["admin@acme.example"],
                "user_impersonation_targets": ["rep@acme.example"],
            }


def test_platform_policy_and_audit_commit_together(operation_store):
    with use_workspace("acme"):
        bind = operation_store["acme"].kw["bind"]
        AuditLog.__table__.drop(bind)
        with pytest.raises(operations.OperationsConfigUnavailable):
            operations.patch_operations_config(
                {"impersonation_allowed_emails": ["admin@acme.example"]},
                platform_authorized=True,
                audit_actor="platform@acme.example",
                audit_sid="support-1",
            )
        assert operations.get_operations_config().impersonation_allowed_emails == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"other": True},
        {"l10_start_after": "next Monday"},
        {"l10_start_after": "2026-02-30"},
        {"l10_start_after": "2026-11-01\nBcc: attacker@example.com"},
        {"ingestion_council_recipients": ["outside", "ops@acme.example"]},
        {"insights_excluded_emails": "ops@acme.example"},
        {"deep_prep_sender_name": "Acme\r\nBcc: attacker@example.com"},
        {"deep_prep_sender_mailbox": "bad mailbox"},
    ],
)
def test_invalid_values_are_rejected(operation_store, changes):
    with use_workspace("acme"):
        with pytest.raises(operations.OperationsConfigInvalid):
            operations.patch_operations_config(changes)
        assert operations.get_operations_config() == operations.OperationsConfig()


def test_malformed_stored_row_and_database_failure_refuse_policy(
    operation_store, monkeypatch
):
    with use_workspace("acme"):
        with operation_store["acme"]() as db:
            db.add(WorkspaceSetting(key="workspace_operations", value=["bad"]))
            db.commit()
        with pytest.raises(operations.OperationsConfigUnavailable):
            operations.get_operations_config()
        with pytest.raises(operations.OperationsConfigUnavailable):
            operations.patch_operations_config({"l10_start_after": "2026-11-02"})

    def fail_session():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(operations, "SessionLocal", fail_session)
    with use_workspace("bravo"):
        with pytest.raises(operations.OperationsConfigUnavailable):
            operations.get_operations_config()


def test_partial_patch_preserves_unknown_and_unrelated_fields(operation_store):
    with use_workspace("acme"):
        with operation_store["acme"]() as db:
            db.add(
                WorkspaceSetting(
                    key="workspace_operations",
                    value={
                        "future_policy": {"enabled": True},
                        "deep_prep_sender_name": "Old name",
                    },
                )
            )
            db.commit()
        operations.patch_operations_config({"l10_sender_mailbox": "l10@acme.example"})
        with operation_store["acme"]() as db:
            row = db.get(WorkspaceSetting, "workspace_operations")
            assert row.value["future_policy"] == {"enabled": True}
            assert row.value["deep_prep_sender_name"] == "Old name"
            assert row.value["l10_sender_mailbox"] == "l10@acme.example"


def test_concurrent_partial_updates_keep_both_edits(operation_store):
    def edit(changes):
        with use_workspace("acme"):
            return operations.patch_operations_config(changes)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(edit, {"deep_prep_sender_name": "Acme"})
        second = pool.submit(edit, {"l10_start_after": "2026-11-02"})
        first.result()
        second.result()
    with use_workspace("acme"):
        config = operations.get_operations_config()
        assert config.deep_prep_sender_name == "Acme"
        assert config.l10_start_after == "2026-11-02"
