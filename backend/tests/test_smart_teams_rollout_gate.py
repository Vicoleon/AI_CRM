"""A team-by-team rollout stage on top of the company-wide Smart Teams gate.

Two different gates live here and conflating them would break the feature in
opposite directions, so this file pins both.

``user_can_access_smart_teams`` answers "is this a real employee on the domain",
and plan.md decision 4 makes the interview COMPANY-WIDE on purpose: it is the one
Smart Teams surface meant to reach everybody. It ships that way — every employee
on the domain, department or not.

``_within_smart_teams_rollout`` is the rollout STAGE: the ``smart_teams`` entry
in Admin → Feature Access, which an admin narrows to one team for a dry run
before the company-wide announcement. The cases below install that narrowed
roster as data (``pin_rosters``), the way the admin would.

The deliberate difference from ``user_can_access_full_app``: that gate waves
through ``is_admin`` and ``is_exec``. This one does not. During a one-team
rehearsal, "the Executive Team can also see it" is not a footnote — they are
precisely who the rehearsal is for, and letting them in early is how a dry run
becomes the launch by accident.
"""

from __future__ import annotations

import pytest

from backend.auth import user_can_access_smart_teams
from backend.tests._rosters import pin_rosters
from rubik_core.auth.registry import UserProfile
from rubik_core.features import FeaturePolicy, feature_by_key

STAMP = "2026-08-21T00:00:00"


def _rollout(*departments: str) -> FeaturePolicy:
    """The narrowed roster an admin writes, flags as the registry ships them."""
    default = feature_by_key("smart_teams").default()
    return FeaturePolicy.from_dict(
        {
            "departments": list(departments),
            "exec_allowed": default.exec_allowed,
            "admin_allowed": default.admin_allowed,
            "everyone": False,
        }
    )


@pytest.fixture
def systems_dry_run(monkeypatch):
    pin_rosters(monkeypatch, {"smart_teams": _rollout("Systems")})


def _user(**overrides) -> UserProfile:
    base = dict(
        id="u1",
        email="person@acme.example",
        name="A Person",
        role="user",
        department="",
        is_admin=False,
        is_exec=False,
        managed_departments=[],
        created_at=STAMP,
        updated_at=STAMP,
    )
    base.update(overrides)
    return UserProfile(
        **{k: v for k, v in base.items() if k in UserProfile.model_fields}
    )


def test_it_ships_open_to_every_employee():
    for department in ("Sales", "HR", "Operations", "general", ""):
        assert user_can_access_smart_teams(_user(department=department)) is True
    # Still not a way in for a non-employee.
    assert user_can_access_smart_teams(_user(email="outsider@gmail.com")) is False


@pytest.mark.parametrize(
    "label,overrides,allowed",
    [
        ("Systems, as the row is spelled", {"department": "Systems"}, True),
        ("systems, already slugified", {"department": "systems"}, True),
        ("manages Systems on the chart", {"managed_departments": ["Systems"]}, True),
        ("Sales", {"department": "Sales"}, False),
        ("HR", {"department": "HR"}, False),
        # ``department`` is a non-nullable string on UserProfile, so "no
        # department" is the empty string rather than None — which is the
        # value the gate actually has to survive.
        ("no department row at all", {"department": ""}, False),
        # The four that a gate modelled on the copilot would have let in.
        ("an admin outside Systems", {"department": "Sales", "is_admin": True}, False),
        ("an exec outside Systems", {"department": "Sales", "is_exec": True}, False),
        (
            "an org-wide exec",
            {"department": "Sales", "is_exec": True, "managed_departments": ["*"]},
            False,
        ),
        ("off the Workspace domain", {"email": "outsider@gmail.com"}, False),
    ],
)
def test_a_one_team_dry_run_admits_only_that_team(
    systems_dry_run, label, overrides, allowed
):
    assert user_can_access_smart_teams(_user(**overrides)) is allowed, label


def test_an_empty_roster_closes_the_door_rather_than_opening_it(monkeypatch):
    """A blank or malformed department must hide the feature, never expose it.

    The failure to avoid is an empty value reading as "no restrictions" — that
    is how a staged rollout turns into an accidental launch.
    """
    for value in ([], [""], ["   "]):
        pin_rosters(monkeypatch, {"smart_teams": _rollout(*value)})
        assert user_can_access_smart_teams(_user(department="Systems")) is False, value


def test_widening_needs_no_code_change(monkeypatch):
    """Adding a department is a Feature Access edit, asserted so it stays that way."""
    pin_rosters(monkeypatch, {"smart_teams": _rollout("systems", "sales")})
    assert user_can_access_smart_teams(_user(department="Sales")) is True
    assert user_can_access_smart_teams(_user(department="Systems")) is True
    assert user_can_access_smart_teams(_user(department="HR")) is False
