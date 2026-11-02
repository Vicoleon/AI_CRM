"""Seed / admin-impersonation fixtures must NEVER enter the AI-Priorities
pipeline — neither generated for nor DM'd on Slack.

Regression for the recurring morning noise where example / impersonation
accounts (``boss@example.com``, ``dev@example.com``, ``exec@example.com``,
``orphan@example.com``, ``user2@example.com``, ``impersonated_user@sales.local``,
``impersonated_user@systems.local``) showed up in the Slack delivery's
"Got nothing (no bundle for their local today)" ops alert. Root cause: the
delivery roster enumerated ``is_exec``/opted-in-department users with NO identity
guard, while generation excluded these fixtures — so delivery was not a subset
of generation and every fixture became a perpetual empty-bundle recipient.

The fix is a single shared guard (``is_eligible_priorities_email`` /
``is_pipeline_fixture_email``) applied at every roster site.

The guard covers every reserved ``*.example`` name too, the local mock-auth
identity (``dev@acme.example``) included: a local batch must not make Workspace
Domain-Wide Delegation calls for a mailbox that cannot exist, even when the
install's Workspace domain is itself ``acme.example``. The roster tests below
that need ``@acme.example`` colleagues opt into the one test carve-out,
``conftest.roster_admits_acme_example``; the predicate tests never do.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from rubik_core.config import DEFAULT_DEV_USER_EMAIL
from rubik_core.priorities.batch import (
    is_eligible_priorities_email,
    is_pipeline_fixture_email,
)

# The exact addresses the user reported as leaking into Slack delivery.
REPORTED_FIXTURES = [
    "boss@example.com",
    "dev@example.com",
    "exec@example.com",
    "impersonated_user@sales.local",
    "impersonated_user@systems.local",
    "orphan@example.com",
    "user2@example.com",
]


# ── pure predicates ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "email",
    [
        *REPORTED_FIXTURES,
        "impersonated_user@compliance.local",
        "hank@example.org",
        "x@example.net",
        "anything@foo.local",
        # Any reserved ``*.example`` name: the local mock-auth identity, the
        # fixture colleagues' domain and an off-domain probe alike.
        "dev@acme.example",
        "Dev@ACME.Example",
        "rep@acme.example",
        "vendor@outside.example",
        "malformed-no-at",
        "",
        None,
    ],
)
def test_is_pipeline_fixture_email_true(email):
    assert is_pipeline_fixture_email(email) is True


@pytest.mark.parametrize(
    "email",
    [
        "someone@partner.alt",
        "a.b+tag@partner.alt",
        "user@example.partner.alt",  # 'example' as a subdomain label, not the TLD
        "user@examples.alt",
    ],
)
def test_is_pipeline_fixture_email_false(email):
    assert is_pipeline_fixture_email(email) is False


def test_the_local_dev_identity_never_enters_the_pipeline():
    """``CUBIE_DEV_USER_EMAIL``'s default is a fixture, and stays one when the
    install's Workspace domain is the same reserved name — no mailbox lives
    there, so a local batch must not delegate into it."""
    assert DEFAULT_DEV_USER_EMAIL.endswith(".example")
    assert is_pipeline_fixture_email(DEFAULT_DEV_USER_EMAIL) is True
    assert is_eligible_priorities_email(DEFAULT_DEV_USER_EMAIL, None) is False
    assert is_eligible_priorities_email(DEFAULT_DEV_USER_EMAIL, "acme.example") is False


def test_eligible_blocks_every_reported_fixture_with_and_without_domain():
    """Each reported fixture is ineligible whether or not a Workspace domain is
    configured — the fixture blocklist is hard, not merely a side effect of the
    domain allowlist."""
    for e in REPORTED_FIXTURES:
        assert is_eligible_priorities_email(e, "acme.example") is False
        assert is_eligible_priorities_email(e, None) is False


def test_eligible_real_user_passes_with_and_without_domain():
    assert is_eligible_priorities_email("pat@partner.alt", "partner.alt") is True
    assert is_eligible_priorities_email("pat@partner.alt", None) is True
    assert is_eligible_priorities_email("pat@partner.alt", frozenset()) is False


def test_eligible_offdomain_real_user_blocked_only_when_domain_set():
    # A real, non-fixture off-domain address: blocked when a domain is enforced
    # (can't DWD-impersonate), allowed when the allowlist is disabled (local dev).
    assert is_eligible_priorities_email("real@partner.alt", "acme.example") is False
    assert is_eligible_priorities_email("real@partner.alt", None) is True


def test_eligible_is_case_insensitive():
    assert is_eligible_priorities_email("Boss@Example.COM", "partner.alt") is False
    assert is_eligible_priorities_email("Pat@PARTNER.Alt", "partner.alt") is True


# ── generation roster (rubik_core.priorities.batch.list_active_users) ─────────


def _in_memory_users(rows):
    """Build an in-memory SQLite session factory seeded with ``(email, dept,
    is_exec)`` User rows."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from rubik_core.database.models import Base, User

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        s.add_all(
            [
                # last_login set: this module tests the FIXTURE-identity guard,
                # so rows must clear the never-signed-in filter that runs after
                # it — otherwise every row drops for the wrong reason.
                User(
                    email=email,
                    id=str(i),
                    name=email,
                    department=dept,
                    is_exec=exec_,
                    last_login=datetime(2026, 7, 1),
                )
                for i, (email, dept, exec_) in enumerate(rows)
            ]
        )
        s.commit()
    return Session


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_list_active_users_excludes_reported_fixtures(
    monkeypatch, set_test_workspace_domains
):
    """Generation never pre-warms a fixture, even one tagged ``is_exec`` or a
    real department — so no HomeCard is ever written for it."""
    import rubik_core.database.db as db_mod
    from rubik_core.priorities.batch import list_active_users

    Session = _in_memory_users(
        [
            ("rep@acme.example", "sales", False),
            ("boss@example.com", "sales", True),  # fixture w/ real dept + exec
            ("exec@example.com", "general", True),  # fixture exec
            ("impersonated_user@sales.local", "sales", True),  # impersonation target
            ("user2@example.com", "hr", False),  # seed fixture
        ]
    )
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    set_test_workspace_domains("acme.example")

    assert set(list_active_users()) == {"rep@acme.example"}


def test_a_local_batch_generates_for_nobody_on_a_reserved_domain(
    monkeypatch, set_test_workspace_domains
):
    """Without the test carve-out, the production rule: a signed-in local
    developer (``dev@acme.example``) and every other ``*.example`` colleague
    stay out of the batch, even with the Workspace domain set to that name."""
    import rubik_core.database.db as db_mod
    from rubik_core.priorities.batch import list_active_users

    Session = _in_memory_users(
        [
            (DEFAULT_DEV_USER_EMAIL, "engineering", True),
            ("rep@acme.example", "sales", False),
        ]
    )
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    set_test_workspace_domains("acme.example")

    assert list_active_users() == []


def test_the_carve_out_admits_only_the_fixture_colleagues(roster_admits_acme_example):
    """The test seam opens exactly ``acme.example``; every other fixture
    address is refused as in production. (Read through the module: the seam
    replaces ``batch.is_pipeline_fixture_email``, which every roster site
    resolves at call time.)"""
    from rubik_core.priorities import batch

    assert batch.is_pipeline_fixture_email("rep@acme.example") is False
    assert is_eligible_priorities_email("rep@acme.example", "acme.example") is True
    for fixture in (
        "vendor@outside.example",
        "boss@example.com",
        "impersonated_user@sales.local",
    ):
        assert is_eligible_priorities_email(fixture, None) is False


# ── Slack delivery roster (backend.agents.slack_delivery) ─────────────────────


class _FakeBot:
    is_available = True

    def __init__(self):
        self.dms: list[tuple[str, str]] = []

    def send_dm(self, email, text=None, blocks=None):
        self.dms.append((email, text))
        return "ts-1"


class _FakeRegistry:
    def get_user(self, email):
        return {"email": email}


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_delivery_automatic_roster_excludes_fixtures(
    monkeypatch, set_test_workspace_domains
):
    """The dept/exec delivery roster drops fixtures, so they never reach the
    per-user loop and never surface in the empty-bundle "Got nothing" alert."""
    import backend.agents.slack_delivery as sd

    Session = _in_memory_users(
        [
            ("real@acme.example", "sales", True),
            ("boss@example.com", "sales", True),
            ("impersonated_user@systems.local", "systems", True),
        ]
    )
    db = Session()

    set_test_workspace_domains("acme.example")
    bot = _FakeBot()
    monkeypatch.setattr(sd, "SlackBotClient", lambda: bot)
    monkeypatch.setattr(sd, "_enabled_departments", lambda _db: set())
    monkeypatch.setattr(sd, "_today_card_present", lambda _e: True)
    monkeypatch.setattr(sd, "_load_bundle_priority_items", lambda _email, **_k: [])
    monkeypatch.setattr(sd, "_ops_recipients", lambda _db: [])
    import rubik_core.auth.registry as registry_mod

    monkeypatch.setattr(registry_mod, "UserRegistry", _FakeRegistry)

    result = sd.deliver_slack_priorities(db)

    # Only the real Workspace exec was ever considered; fixtures never entered
    # the roster (not skipped-empty, not DM'd).
    assert result.users_skipped_empty == ["real@acme.example"]
    assert "boss@example.com" not in result.users_skipped_empty
    assert "impersonated_user@systems.local" not in result.users_skipped_empty
    assert bot.dms == []


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_delivery_targeted_resend_drops_fixtures(monkeypatch):
    """Even an explicitly-named targeted re-send drops fixtures — there is no
    real mailbox behind them, so a DM would only ever be noise."""
    import backend.agents.slack_delivery as sd

    bot = _FakeBot()
    monkeypatch.setattr(sd, "SlackBotClient", lambda: bot)
    monkeypatch.setattr(sd, "_ops_recipients", lambda _db: [])
    items = {
        "bellison@acme.example": [
            sd._BundlePriority(title="T", severity="info", body="b", link=None)
        ]
    }
    monkeypatch.setattr(
        sd, "_load_bundle_priority_items", lambda email, **_k: items.get(email, [])
    )
    import rubik_core.auth.registry as registry_mod

    monkeypatch.setattr(registry_mod, "UserRegistry", _FakeRegistry)

    result = sd.deliver_slack_priorities(
        MagicMock(),
        emails=[
            "bellison@acme.example",
            "boss@example.com",
            "impersonated_user@sales.local",
        ],
    )

    dm_targets = [e for (e, _t) in bot.dms]
    assert dm_targets == ["bellison@acme.example"]
    assert "boss@example.com" not in dm_targets
    assert "impersonated_user@sales.local" not in dm_targets
    assert result.users_delivered == 1
    assert result.users_skipped_empty == []
