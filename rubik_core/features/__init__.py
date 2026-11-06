"""Feature access — WHO may reach each gated Cubie surface, as data.

Every department-scoped gate in the app resolves through this package instead
of a hard-coded roster. The registry (``registry.py``) declares each feature
once, with the roster it ships with; the policy layer (``policy.py``) lets an
admin override that roster from Admin → Feature Access and stores the override
in the ``feature_access`` table, so onboarding a department to a feature is a
click, not a code change and a deploy.

RULE FOR NEW FEATURES (also in docs/ENGINEERING_NOTES.md and CLAUDE.md): any new gated
surface adds a ``FeatureSpec`` to ``registry.FEATURES`` and its gate calls
``feature_allows(<key>, user)``. ``backend/tests/test_feature_access.py``
fails CI on a hard-coded department roster in ``backend/auth.py``.
"""

from rubik_core.features.policy import (
    clear_cache,
    effective_policy,
    feature_allows,
    reset_policy,
    resolve_all,
    save_policy,
    user_allowed,
)
from rubik_core.features.registry import (
    FEATURES,
    FeaturePolicy,
    FeatureSpec,
    feature_by_key,
    feature_keys,
)

__all__ = [
    "FEATURES",
    "FeaturePolicy",
    "FeatureSpec",
    "clear_cache",
    "effective_policy",
    "feature_allows",
    "feature_by_key",
    "feature_keys",
    "reset_policy",
    "resolve_all",
    "save_policy",
    "user_allowed",
]
