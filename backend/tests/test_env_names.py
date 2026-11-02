"""Product-scoped settings and env vars carry the product's own prefix.

Every setting that is specific to this product (its kill switches, its tuning
knobs, the ``/health`` migration markers) is read from an env var named
``CUBIE_*``. The prefix the codebase used before the rebrand must not survive
in a ``Settings`` field, because a field name IS the env var a deployment sets.

The old prefix is assembled from fragments so this file never matches the
legacy-brand scan itself.
"""

from __future__ import annotations

from rubik_core.config import Settings
from rubik_core.database import migration_guard

OLD_PREFIX = "BRI" + "AN_"
PREFIX = "CUBIE_"


def test_no_settings_field_uses_the_old_prefix():
    stale = sorted(
        name for name in Settings.model_fields if name.startswith(OLD_PREFIX)
    )
    assert stale == [], f"settings still read old-prefixed env vars: {stale}"


def test_the_product_scoped_settings_use_the_cubie_prefix():
    """The kill switches that used to carry the old prefix now carry ours."""
    fields = set(Settings.model_fields)
    for name in (
        "CUBIE_DEEP_PREP_ENABLED",
        "CUBIE_CRM_SYNC_ENABLED",
        "CUBIE_PREP_RUNNER",
        "CUBIE_L10_SUMMARY_EMAIL_ENABLED",
        "CUBIE_PRIORITIES_GATE_MODE",
    ):
        assert name in fields, name


def test_the_health_migration_markers_use_the_cubie_prefix():
    markers = [
        migration_guard.DRIFT_MARKER_ENV,
        migration_guard.SKIPPED_MARKER_ENV,
        migration_guard.IMAGE_HEAD_MARKER_ENV,
        migration_guard.DB_REVISION_MARKER_ENV,
        migration_guard.STAGE_MARKER_ENV,
    ]
    assert all(name.startswith(PREFIX + "MIGRATION") for name in markers), markers
