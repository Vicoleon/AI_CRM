"""Resolving a feature's live roster and judging a user against it.

The stored override lives in ``feature_access`` (one row per feature key). The
lookup is cached per process for ``CACHE_TTL_SECONDS`` because the gates run
as FastAPI dependencies on every request; a write from this instance clears
the cache, and other Cloud Run instances catch up within the TTL.

Reads FAIL CLOSED. If the table cannot be read (the connection is down, the
transaction is aborted, the row is malformed), that one call gets the most
restrictive policy there is: nobody, except the workspace's admins when the
feature ships with the admin door open (:func:`closed_policy`). A permission
check must never become an outage, so it never raises; and it must never fail
OPEN, so it never falls back to the shipped default, which for most features
is "everyone". A failed read is logged and never cached: the next call reads
the table again, so the gate opens as soon as reads succeed.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.features.registry import (
    FEATURES,
    FeaturePolicy,
    FeatureSpec,
    feature_by_key,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60.0

# (workspace slug, feature key) -> (expiry, stored policy or None when no row).
# A failed read is never stored here.
_CACHE: dict[tuple[str, str], tuple[float, FeaturePolicy | None]] = {}
_LOCK = threading.Lock()
_GLOBAL_EPOCH = 0
_SLUG_EPOCH: dict[str, int] = {}
_KEY_EPOCH: dict[tuple[str, str], int] = {}


def _version(cache_key: tuple[str, str]) -> tuple[int, int, int]:
    slug, _ = cache_key
    return (
        _GLOBAL_EPOCH,
        _SLUG_EPOCH.get(slug, 0),
        _KEY_EPOCH.get(cache_key, 0),
    )


class _ReadFailed:
    """What :func:`_read_stored` returns when the table could not be read."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "READ_FAILED"


#: The sentinel for a failed read: distinct from None, which means "no row".
READ_FAILED = _ReadFailed()


def clear_cache(key: str | None = None, *, slug: str | None = None) -> None:
    """Clear all policies, or one key for its current/explicit workspace."""
    global _GLOBAL_EPOCH
    if key is not None and slug is None:
        from rubik_core.tenancy.context import current_workspace

        slug = current_workspace()
    with _LOCK:
        if key is None and slug is None:
            _GLOBAL_EPOCH += 1
            _CACHE.clear()
            _SLUG_EPOCH.clear()
            _KEY_EPOCH.clear()
        elif key is None:
            _SLUG_EPOCH[slug] = _SLUG_EPOCH.get(slug, 0) + 1
            for cache_key in [item for item in _CACHE if item[0] == slug]:
                _CACHE.pop(cache_key, None)
        else:
            cache_key = (slug, key)
            _KEY_EPOCH[cache_key] = _KEY_EPOCH.get(cache_key, 0) + 1
            _CACHE.pop(cache_key, None)


def _row_to_policy(row: Any) -> FeaturePolicy:
    return FeaturePolicy.from_dict(
        {
            "departments": list(row.departments or []),
            "extra_emails": list(row.extra_emails or []),
            "exec_allowed": bool(row.exec_allowed),
            "admin_allowed": bool(row.admin_allowed),
            "everyone": bool(row.everyone),
        }
    )


def _read_stored(key: str, db: Session | None) -> FeaturePolicy | None | _ReadFailed:
    """The override row for ``key``, None when there is none, or
    :data:`READ_FAILED` when the table could not be read. Never raises."""
    from rubik_core.database.models import FeatureAccess  # local: avoid cycles

    own_session = db is None
    try:
        if own_session:
            from rubik_core.database.db import SessionLocal

            db = SessionLocal()
        row = db.get(FeatureAccess, key)
        return _row_to_policy(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001 — the caller fails closed
        logger.error(
            "feature_access read failed for %r; denying all but the admin door "
            "for this call (%s)",
            key,
            exc,
        )
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return READ_FAILED
    finally:
        if own_session and db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def stored_policy(
    key: str, db: Session | None = None
) -> FeaturePolicy | None | _ReadFailed:
    """The admin override for ``key`` (cached), None when there is none, or
    :data:`READ_FAILED` when the table could not be read (never cached)."""
    from rubik_core.tenancy.context import current_workspace

    cache_key = (current_workspace(), key)
    for _ in range(3):
        now = time.monotonic()
        with _LOCK:
            hit = _CACHE.get(cache_key)
            if hit is not None and hit[0] > now:
                return hit[1]
            version = _version(cache_key)
        value = _read_stored(key, db)
        if value is READ_FAILED:
            return value
        with _LOCK:
            if _version(cache_key) == version:
                _CACHE[cache_key] = (time.monotonic() + CACHE_TTL_SECONDS, value)
                return value
    logger.warning(
        "feature_access policy changed during read for %s; denying", cache_key
    )
    return READ_FAILED


def closed_policy(spec: FeatureSpec) -> FeaturePolicy:
    """The most restrictive policy for ``spec``: what a failed read resolves to.

    Nobody, except the workspace's admins when the feature ships with the
    admin door open. A feature whose row exists to narrow WHICH admins may
    reach it (``spec.narrows_admins``) admits nobody at all: its row may have
    shut every admin but one team's out, and a failed read cannot tell.
    """
    admins = spec.default().admin_allowed and not spec.narrows_admins
    return FeaturePolicy(exec_allowed=False, admin_allowed=admins, everyone=False)


def effective_policy(key: str, db: Session | None = None) -> tuple[FeaturePolicy, bool]:
    """``(policy, customized)`` — the override when one exists, else the default.

    When the table cannot be read, :func:`closed_policy` for this call, reported
    as not customized.
    """
    spec = feature_by_key(key)
    stored = stored_policy(key, db)
    if stored is READ_FAILED:
        return closed_policy(spec), False
    if stored is not None:
        return stored, True
    return spec.default(), False


def kill_switch_on(spec: FeatureSpec) -> bool | None:
    """The env kill switch's value, or None when the feature has none."""
    if not spec.kill_switch:
        return None
    from rubik_core.config import settings  # local: keep leaf light

    return bool(getattr(settings, spec.kill_switch, False))


# ── Judging a user ───────────────────────────────────────────────────────────


def _is_admin(user: Any) -> bool:
    # ``UserProfile.is_admin`` is a property; a ``users`` ORM row has none and
    # is judged on its roles. Anything else that carries the attribute (test
    # doubles included) is taken at its truthiness, as ``user.is_admin`` was.
    val = getattr(user, "is_admin", None)
    if val is not None:
        return bool(val)
    names = set()
    for r in getattr(user, "roles", None) or []:
        names.add(r if isinstance(r, str) else str(getattr(r, "name", "") or ""))
    return "admin" in names


def _dept_slugs(user: Any, membership: str) -> set[str]:
    from rubik_core.rag.config import slugify_department  # local: keep leaf light

    managed = list(getattr(user, "managed_departments", None) or [])
    candidates = (
        managed
        if membership == "managed_only"
        else [
            getattr(user, "department", None),
            *managed,
        ]
    )
    # "*" (the org-wide exec wildcard) slugifies to nothing and is not a
    # department; org-wide reach is expressed by the exec flag instead. A
    # non-string department (None, or a stand-in object) is no department.
    return {slugify_department(d) for d in candidates if isinstance(d, str)} - {""}


def user_allowed(spec: FeatureSpec, policy: FeaturePolicy, user: Any) -> bool:
    """Pure verdict: does ``policy`` admit ``user`` to ``spec``?

    ``user`` may be a ``UserProfile``, a ``users`` ORM row, or any object with
    ``department`` / ``managed_departments`` / ``is_exec`` / ``email`` and either
    ``is_admin`` or ``roles``. Structural checks that are not about the roster
    (the Workspace-domain test for Smart Teams, the env kill switches) stay in
    the gates that own them.
    """
    if policy.admin_allowed and _is_admin(user):
        return True
    if policy.exec_allowed and bool(getattr(user, "is_exec", False)):
        return True
    email = (getattr(user, "email", "") or "").strip().lower()
    if email and email in policy.extra_emails:
        return True
    if policy.everyone:
        return True
    if not policy.departments:
        return False
    return bool(_dept_slugs(user, spec.membership) & policy.departments)


def feature_allows(
    key: str,
    user: Any,
    db: Session | None = None,
    *,
    ignore_kill_switch: bool = False,
) -> bool:
    """The platform ceiling, workspace switch and roster must all admit access.

    ``ignore_kill_switch`` is for routers that intentionally answer 403 to
    people outside the audience before revealing a disabled feature as 404.
    The caller must enforce the switch after this audience check.
    """
    spec = feature_by_key(key)
    if spec.kill_switch and not ignore_kill_switch and not kill_switch_on(spec):
        return False
    if not _module_flags().get(spec.module, False):
        return False
    policy, _ = effective_policy(key, db)
    return user_allowed(spec, policy, user)


def _module_flags() -> dict[str, bool]:
    from rubik_core.control.repository import module_flags
    from rubik_core.tenancy.context import current_workspace

    slug = current_workspace()  # An absent context is a caller error, not a default.
    try:
        return module_flags(slug)
    except Exception:  # noqa: BLE001 — failed control reads deny all features
        logger.exception("Workspace module lookup failed for %s", slug)
        return {"core": False, "eos": False, "sales_coach": False}


def resolve_all(
    user: Any, db: Session | None = None, *, modules: dict[str, bool] | None = None
) -> dict[str, bool]:
    """Every feature's verdict for ``user`` — what ``/api/users/me`` ships.

    Each verdict includes the environment switch, control module and tenant
    roster. ``modules`` lets an async caller supply its one offloaded control
    lookup instead of repeating it on the event loop.
    """
    modules = _module_flags() if modules is None else modules
    verdicts = {}
    for spec in FEATURES:
        if (spec.kill_switch and not kill_switch_on(spec)) or not modules.get(
            spec.module, False
        ):
            verdicts[spec.key] = False
            continue
        policy, _ = effective_policy(spec.key, db)
        verdicts[spec.key] = user_allowed(spec, policy, user)
    return verdicts


# ── Writes (Admin → Feature Access) ─────────────────────────────────────────


def save_policy(
    db: Session, key: str, policy: FeaturePolicy, *, actor_email: str, note: str = ""
) -> Any:
    """Upsert the override row for ``key`` and drop it from the cache."""
    from rubik_core.database.models import FeatureAccess  # local: avoid cycles

    feature_by_key(key)  # KeyError on an unregistered feature
    row = db.get(FeatureAccess, key)
    if row is None:
        row = FeatureAccess(feature_key=key)
        db.add(row)
    row.departments = sorted(policy.departments)
    row.extra_emails = sorted(policy.extra_emails)
    row.exec_allowed = policy.exec_allowed
    row.admin_allowed = policy.admin_allowed
    row.everyone = policy.everyone
    row.note = (note or "").strip() or None
    row.updated_by = actor_email
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    clear_cache(key)
    return row


def reset_policy(db: Session, key: str) -> bool:
    """Delete the override so ``key`` falls back to its code default."""
    from rubik_core.database.models import FeatureAccess  # local: avoid cycles

    feature_by_key(key)
    row = db.get(FeatureAccess, key)
    if row is None:
        clear_cache(key)
        return False
    db.delete(row)
    db.commit()
    clear_cache(key)
    return True
