"""Pin a feature's roster in a test, the way an admin would in Feature Access.

The registry ships every user-facing feature open to everyone, so a test about
how a gate treats a NARROWED roster has to set that roster itself — as the same
override an admin writes in Admin → Feature Access — rather than lean on a
shipped default. ``pin_rosters`` answers the override lookup
(``rubik_core.features.policy.stored_policy``) for the given keys and leaves
every other key to the real lookup, so the verdict still comes from the real
``user_allowed``.

A shared helper module, not a test module: test modules import it from here and
never import each other.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from rubik_core.features import FeaturePolicy, feature_by_key
from rubik_core.features import policy as _policy


def pin_rosters(
    monkeypatch: pytest.MonkeyPatch, rosters: Mapping[str, FeaturePolicy]
) -> None:
    """Make ``rosters`` the live override for each of its feature keys."""
    for key in rosters:
        feature_by_key(key)  # KeyError on a key the registry does not know
    pinned = dict(rosters)
    real = _policy.stored_policy

    def stored_policy(key: str, db=None) -> FeaturePolicy | None:
        if key in pinned:
            return pinned[key]
        return real(key, db)

    monkeypatch.setattr(_policy, "stored_policy", stored_policy)


def narrow_copilot(monkeypatch: pytest.MonkeyPatch, *departments: str) -> None:
    """An admin narrowed the copilot to ``departments`` (Exec team and admins
    still in) — what a test about someone OUTSIDE the copilot needs now that it
    ships open to everyone."""
    from rubik_core.rag.config import slugify_department

    pin_rosters(
        monkeypatch,
        {
            "copilot": FeaturePolicy(
                departments=frozenset(slugify_department(d) for d in departments),
                exec_allowed=True,
                admin_allowed=True,
            )
        },
    )


def narrow_smart_teams_support(
    monkeypatch: pytest.MonkeyPatch, *departments: str
) -> None:
    """An admin handed Smart Teams support to the admins of ``departments``
    (Admins unticked, the teams named) — what a test about an admin OUTSIDE the
    support set needs now that support ships with every admin on it."""
    from rubik_core.rag.config import slugify_department

    pin_rosters(
        monkeypatch,
        {
            "smart_teams_support": FeaturePolicy(
                departments=frozenset(slugify_department(d) for d in departments),
                admin_allowed=False,
            )
        },
    )
