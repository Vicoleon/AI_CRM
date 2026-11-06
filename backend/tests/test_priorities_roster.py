"""Who the nightly AI Priorities batch generates for: one roster, read everywhere.

The batch, the run-all trigger's count and the Ingestion Council's two
"who should have a card?" checks used to carry their own copy of the query,
and every copy dropped anyone whose department was blank or ``general`` unless
they were on the Exec team. Departments are optional now (spec §4.3.4), so a
person with no department was signed in, on the copilot and never got a
briefing. The roster is now everyone on the Workspace domain who is not a
seed or impersonation fixture, has not left, and whom the registry admits to
both the copilot and ``ai_priorities``, judged once in
``rubik_core.priorities.batch.priorities_roster`` and read by every site.

The morning Slack DM still goes to the departments opted into it plus the Exec
team, and those departments are matched by RAG slug on both sides.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

#: Every ``@acme.example`` colleague here is a roster member, so this module
#: opts into the one fixture carve-out (``conftest.roster_admits_acme_example``).
pytestmark = pytest.mark.usefixtures("roster_admits_acme_example")

SIGNED_IN = datetime(2026, 10, 1)

#: (email, department, is_exec, last_login)
PEOPLE = [
    ("rep@acme.example", "Sales", False, SIGNED_IN),
    ("newhire@acme.example", None, False, SIGNED_IN),
    ("generalist@acme.example", "general", False, SIGNED_IN),
    ("blank@acme.example", "", False, SIGNED_IN),
    ("boss@example.com", "Sales", True, SIGNED_IN),  # seed fixture
    ("impersonated_user@sales.local", "Sales", False, SIGNED_IN),  # impersonation
    ("vendor@outside.example", "Sales", False, SIGNED_IN),  # off the domain
    ("leaver@acme.example", None, False, SIGNED_IN),  # left the company
]
STAFF = {
    "rep@acme.example",
    "newhire@acme.example",
    "generalist@acme.example",
    "blank@acme.example",
    "leaver@acme.example",
}


@pytest.fixture
def session_factory(monkeypatch, set_test_workspace_domains):
    import rubik_core.database.db as db_mod
    from rubik_core.database.models import Base, Department, User

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    with factory() as s:
        s.add_all(
            User(
                email=email,
                id=str(i),
                name=email,
                department=dept,
                is_exec=is_exec,
                last_login=last_login,
            )
            for i, (email, dept, is_exec, last_login) in enumerate(PEOPLE)
        )
        s.add(Department(name="Sales Ops", slack_priorities_enabled=True))
        s.commit()
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    set_test_workspace_domains("acme.example")
    return factory


def _leavers(emails):
    return {"leaver@acme.example"} & set(emails)


# ── Item 3: no department is no reason to be left out ───────────────────────


def test_a_person_with_no_department_gets_a_briefing(session_factory):
    from rubik_core.priorities.batch import list_active_users

    roster = set(list_active_users(deactivated_lookup=_leavers))

    assert {"newhire@acme.example", "generalist@acme.example"} <= roster
    assert "blank@acme.example" in roster


def test_fixtures_outsiders_and_leavers_are_left_out(session_factory):
    from rubik_core.priorities.batch import list_active_users

    roster = set(list_active_users(deactivated_lookup=_leavers))

    assert roster == STAFF - {"leaver@acme.example"}
    assert "boss@example.com" not in roster
    assert "impersonated_user@sales.local" not in roster
    assert "vendor@outside.example" not in roster


def test_every_roster_site_reads_the_same_list(session_factory):
    """The batch, the run-all count and the council's two checks agree, so the
    council never reports someone "missing" whom the batch never meant to
    generate for (or misses someone it did)."""
    from backend.routers.jobs import _list_active_users
    from rubik_core.ingestion_council.evidence import _priorities_daily_evidence
    from rubik_core.ingestion_council.quality_gate import gather_user_bundle_records
    from rubik_core.priorities.batch import list_active_users, priorities_roster

    with session_factory() as db:
        shared = priorities_roster(db)
        council_daily = _priorities_daily_evidence(db, datetime(2026, 10, 28, 12))
        with patch(
            "rubik_core.priorities.temporal.user_today_str",
            return_value="2026-10-28",
        ):
            gate = gather_user_bundle_records(db)

    assert sorted(shared) == sorted(STAFF)
    assert sorted(list_active_users()) == sorted(shared)
    assert sorted(_list_active_users()) == sorted(shared)
    assert sorted(r.email for r in gate) == sorted(shared)
    # No cards were written, so the council counts exactly the roster missing.
    assert council_daily.stats["active_users"] == len(shared)
    assert council_daily.stats["missing_users"] == len(shared)


# ── The registry decides who is read overnight ──────────────────────────────


@pytest.fixture
def fresh_policies():
    from rubik_core.features import clear_cache

    clear_cache()
    yield
    clear_cache()


@pytest.mark.parametrize("key", ["copilot", "ai_priorities"])
def test_someone_narrowed_out_of_either_feature_is_not_read(
    session_factory, fresh_policies, monkeypatch, key
):
    """Narrowing the copilot or AI Priorities to Sales takes everyone else's
    mailbox out of the night's batch, and its spend off the bill."""
    from backend.tests._rosters import pin_rosters
    from rubik_core.features import FeaturePolicy
    from rubik_core.priorities.batch import list_active_users, priorities_roster

    pin_rosters(monkeypatch, {key: FeaturePolicy(departments=frozenset({"sales"}))})

    with session_factory() as db:
        assert priorities_roster(db) == ["rep@acme.example"]
    assert list_active_users(deactivated_lookup=_leavers) == ["rep@acme.example"]


def test_a_failed_feature_access_read_reads_no_one(
    session_factory, fresh_policies, monkeypatch
):
    """Fail closed: a Feature Access read that errors admits no non-admin, so
    a database blip during the batch never widens a narrowed roster."""
    from rubik_core.features import policy
    from rubik_core.priorities.batch import priorities_roster

    monkeypatch.setattr(policy, "_read_stored", lambda key, db: policy.READ_FAILED)
    with session_factory() as db:
        assert priorities_roster(db) == []


# ── Item 4: Slack opt-in departments match by slug ──────────────────────────


def test_a_slack_opt_in_matches_its_department_by_slug(session_factory):
    from rubik_core.database.models import User
    from rubik_core.priorities.batch import (
        is_slack_priorities_recipient,
        slack_opted_department_slugs,
    )

    with session_factory() as db:
        db.add(
            User(
                email="ops@acme.example",
                id="ops",
                name="Ops",
                department="sales-ops",
                last_login=SIGNED_IN,
            )
        )
        db.commit()
        opted = slack_opted_department_slugs(db)
        ops = db.get(User, "ops@acme.example")
        rep = db.get(User, "rep@acme.example")

        assert opted == frozenset({"sales_ops"})
        assert is_slack_priorities_recipient(ops, opted)
        assert not is_slack_priorities_recipient(rep, opted)


def test_the_slack_dm_reaches_a_slug_matched_department(session_factory, monkeypatch):
    """ "Sales Ops" is opted in; a person filed under "sales-ops" is its member."""
    from backend.agents import slack_delivery as sd
    from rubik_core.database.models import User

    with session_factory() as db:
        db.add(
            User(
                email="ops@acme.example",
                id="ops",
                name="Ops",
                department="sales-ops",
            )
        )
        db.commit()

        bot = MagicMock(is_available=True)
        monkeypatch.setattr(sd, "SlackBotClient", lambda: bot)
        monkeypatch.setattr(
            sd, "deactivated_priorities_emails", lambda emails, bot=None: set()
        )
        # Nobody has a card, so delivery stops at its generation guard right
        # after choosing recipients; the guard asks about each one in turn.
        asked: list[str] = []

        def no_card(email):
            asked.append(email)
            return False

        monkeypatch.setattr(sd, "_today_card_present", no_card)
        result = sd.deliver_slack_priorities(db)

    assert result.generation_missing is True
    assert asked == ["ops@acme.example"]


def test_the_never_signed_in_exemption_matches_by_slug_too(session_factory):
    """A Slack recipient is a reader even if they never opened the web app, so
    the batch keeps them; the opt-in must be recognised the same way here."""
    from rubik_core.database.models import User
    from rubik_core.priorities.batch import list_active_users

    with session_factory() as db:
        db.add(
            User(
                email="ops@acme.example",
                id="ops",
                name="Ops",
                department="sales-ops",
                last_login=None,
            )
        )
        db.commit()

    assert "ops@acme.example" in list_active_users()
