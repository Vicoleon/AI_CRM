"""Never-signed-in gate for the AI-Priorities generation roster.

A roster reconciliation that gives real departments to accounts parked in
``'general'`` moves users who have NEVER opened Cubie past
``list_active_users``' ``department != 'general'`` clause, and daily generation
grows by people with no reader on the other end (more Vertex spend on the
morning batch, doubled again by the Friday next-week look-ahead).

``list_active_users`` now drops ``last_login IS NULL`` users — EXCEPT the two
groups Slack still DMs, because ``deliver_slack_priorities`` reads only the
HomeCard this batch writes and skipping a recipient turns their morning DM into
an empty bundle:

  * ``is_exec`` users, and
  * users whose department has ``slack_priorities_enabled = True``.
"""

from __future__ import annotations

from datetime import datetime

import pytest

#: Every ``@acme.example`` colleague here is a roster member, so this module
#: opts into the one fixture carve-out (``conftest.roster_admits_acme_example``).
pytestmark = pytest.mark.usefixtures("roster_admits_acme_example")

SIGNED_IN = datetime(2026, 7, 1)


@pytest.fixture(autouse=True)
def _workspace_domain(set_test_workspace_domains):
    set_test_workspace_domains("acme.example")


def _roster_db(monkeypatch, users, *, slack_opted_departments=()):
    """Build an in-memory roster. ``users`` = (email, dept, is_exec, last_login)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import rubik_core.database.db as db_mod
    from rubik_core.database.models import Base, Department, User

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    with TestSession() as s:
        s.add_all(
            [
                User(
                    email=email,
                    id=str(i),
                    name=email,
                    department=dept,
                    is_exec=is_exec,
                    last_login=last_login,
                )
                for i, (email, dept, is_exec, last_login) in enumerate(users)
            ]
        )
        s.add_all(
            [
                Department(name=d, slack_priorities_enabled=True)
                for d in slack_opted_departments
            ]
        )
        s.commit()
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)


def test_never_signed_in_user_is_dropped(monkeypatch):
    """The Jul-29 regression: a real department is not enough to earn a bundle."""
    from rubik_core.priorities.batch import list_active_users

    _roster_db(
        monkeypatch,
        [
            ("rep@acme.example", "Sales", False, SIGNED_IN),
            ("newhire@acme.example", "Delivery", False, None),
        ],
    )

    assert set(list_active_users()) == {"rep@acme.example"}


def test_never_signed_in_exec_is_kept(monkeypatch):
    """Superset invariant: Slack DMs execs regardless of department."""
    from rubik_core.priorities.batch import list_active_users

    _roster_db(
        monkeypatch,
        [
            ("ceo@acme.example", "general", True, None),
            ("newhire@acme.example", "Delivery", False, None),
        ],
    )

    # The exec survives on the exemption; the ordinary never-signed-in user does not.
    assert set(list_active_users()) == {"ceo@acme.example"}


def test_never_signed_in_kept_when_department_opted_into_slack(monkeypatch):
    """A Slack recipient is a reader even if they never open the web app."""
    from rubik_core.priorities.batch import list_active_users

    _roster_db(
        monkeypatch,
        [
            ("seller@acme.example", "Sales", False, None),
            ("warehouse@acme.example", "Delivery", False, None),
        ],
        slack_opted_departments=["Sales"],
    )

    assert set(list_active_users()) == {"seller@acme.example"}


def test_department_not_opted_in_does_not_exempt(monkeypatch):
    """slack_priorities_enabled NULL/False must not count as opted in."""
    import rubik_core.database.db as db_mod
    from rubik_core.database.models import Department
    from rubik_core.priorities.batch import list_active_users

    _roster_db(
        monkeypatch,
        [("warehouse@acme.example", "Delivery", False, None)],
    )
    # Explicitly present but switched off — the common real-world shape.
    with db_mod.SessionLocal() as s:
        s.add(Department(name="Delivery", slack_priorities_enabled=False))
        s.add(Department(name="Ops", slack_priorities_enabled=None))
        s.commit()

    assert list_active_users() == []


def test_signed_in_users_are_unaffected(monkeypatch):
    """The filter must not touch anyone who has actually used Cubie."""
    from rubik_core.priorities.batch import list_active_users

    _roster_db(
        monkeypatch,
        [
            ("a@acme.example", "Delivery", False, SIGNED_IN),
            ("b@acme.example", "Contractors", False, SIGNED_IN),
            ("c@acme.example", "Compliance", False, SIGNED_IN),
        ],
    )

    assert len(list_active_users()) == 3


def test_filter_composes_with_the_deactivation_lookup(monkeypatch):
    """Both roster filters apply; neither shadows the other."""
    from rubik_core.priorities.batch import list_active_users

    _roster_db(
        monkeypatch,
        [
            ("rep@acme.example", "Sales", False, SIGNED_IN),
            ("gone@acme.example", "Sales", False, SIGNED_IN),
            ("newhire@acme.example", "Delivery", False, None),
        ],
    )

    def _lookup(emails):
        # The never-signed-in user is already gone before detection runs, so we
        # don't spend a directory lookup on them.
        assert "newhire@acme.example" not in emails
        return {"gone@acme.example"}

    assert list_active_users(deactivated_lookup=_lookup) == ["rep@acme.example"]
